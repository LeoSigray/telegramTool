# -*- coding: utf-8 -*-
"""
Подбор целевой аудитории по аналитике «Кто ЦА»: студенты из Санкт-Петербурга, мужчины.

  1. Берём ТОП-N популярных сообществ СПб (автоподбор или список из файла).
  2. Собираем участников; пол и город — из профиля (мужчины, город = Санкт-Петербург).
  3. Оставляем тех, кто состоит минимум в MIN_COMMUNITIES из них.
  4. a = доля сообществ, где состоит человек (3 из 10 -> 0.3).
  5. b = вероятность по ВУЗу СПб: чем ближе год начала обучения к текущему
     (|начало − текущий| <= FIT_YEARS), тем выше. В VK хранится год ОКОНЧАНИЯ,
     начало = окончание − DEGREE_YEARS.
  6. score = K1 * a + K2 * b, где K2 больше, если ВУЗ подошёл. Сортировка по score.

Чистые функции (score_user / university_fit / rank) не ходят в сеть — их можно проверять отдельно.
"""
import re
from datetime import datetime

from . import config as cfg
from .api import VKApi, VKError


# ── сообщества ──────────────────────────────────────────────────────────────

def parse_ref(raw: str) -> str | None:
    """'https://vk.com/spb_live', 'vk.com/club123', '@spb', 'spb_live' -> 'spb_live' / 'club123'."""
    s = (raw or "").strip().split("#")[0].strip()
    if not s:
        return None
    s = re.sub(r"^(https?://)?(m\.)?vk\.(com|ru)/", "", s, flags=re.I).lstrip("@").strip("/")
    s = s.split("?")[0]
    return s or None


def _norm_group(g: dict) -> dict:
    return {"id": int(g["id"]), "name": g.get("name") or str(g["id"]),
            "screen_name": g.get("screen_name") or f"club{g['id']}",
            "members_count": int(g.get("members_count") or 0),
            "is_closed": int(g.get("is_closed") or 0), "deactivated": g.get("deactivated")}


def _groups_by_id(api: VKApi, refs: list[str]) -> list[dict]:
    out: list[dict] = []
    for i in range(0, len(refs), 400):
        r = api.call("groups.getById", group_ids=",".join(refs[i:i + 400]), fields="members_count")
        items = r.get("groups", []) if isinstance(r, dict) else (r or [])   # формат ответа зависит от версии API
        out += [_norm_group(g) for g in items]
    return out


def resolve_communities(api: VKApi, refs: list[str]) -> list[dict]:
    """Список ссылок/коротких имён -> данные сообществ (только то, что нашлось)."""
    return _groups_by_id(api, [r for r in map(parse_ref, refs) if r])


def find_top_communities(api: VKApi, n: int = cfg.TOP_COMMUNITIES,
                         queries: list[str] | None = None, log=print) -> list[dict]:
    """
    Автоподбор: ищем сообщества города поиском VK и берём n самых больших ОТКРЫТЫХ
    (у закрытых список участников недоступен). Поиск у VK — по названию и городу,
    поэтому результат стоит глазами проверить: список можно поправить вручную.
    """
    ids: dict[int, None] = {}
    for q in queries or cfg.SEARCH_QUERIES:
        for kind in ("group", "page"):
            try:
                r = api.call("groups.search", q=q, type=kind, city_id=cfg.CITY_ID_SPB,
                             country_id=1, count=1000)
            except VKError as e:
                log(f"  поиск «{q}» ({kind}): {e.message}")
                continue
            for g in (r or {}).get("items", []):
                ids[int(g["id"])] = None
        log(f"  «{q}»: найдено уникальных сообществ — {len(ids)}")
    groups = _groups_by_id(api, [str(i) for i in ids])
    ok = [g for g in groups if not g["is_closed"] and not g["deactivated"] and g["members_count"] > 0]
    ok.sort(key=lambda g: g["members_count"], reverse=True)
    return ok[:n]


# ── участники ───────────────────────────────────────────────────────────────

def iter_members(api: VKApi, group_id: int, limit: int | None = None, on_page=None):
    offset, total = 0, None
    while True:
        want = cfg.MEMBERS_PAGE if not limit else min(cfg.MEMBERS_PAGE, limit - offset)
        r = api.call("groups.getMembers", group_id=group_id, offset=offset, count=want,
                     fields=cfg.MEMBER_FIELDS)
        items, total = r.get("items", []), r.get("count", 0)
        yield from items
        offset += len(items)
        if on_page:
            on_page(offset, total)
        if not items or offset >= total or (limit and offset >= limit):
            return


def is_candidate(u: dict) -> bool:
    """Пол и город — из профиля. Удалённых/заблокированных и тех, кому нельзя написать, отсекаем."""
    if u.get("deactivated"):
        return False
    if u.get("sex") != cfg.SEX_MALE:
        return False
    if (u.get("city") or {}).get("id") != cfg.CITY_ID_SPB:
        return False
    if u.get("can_write_private_message") == 0:
        return False
    return True


def collect(api: VKApi, communities: list[dict], limit_per_group: int | None = None,
            log=print) -> tuple[dict[int, dict], int]:
    """
    Возвращает ({user_id: {"user": профиль, "groups": {id сообществ}}}, сколько сообществ прочитано).
    Недоступное сообщество (закрыто/скрыт список) пропускается, остальные продолжают.
    """
    found: dict[int, dict] = {}
    scanned = 0
    for g in communities:
        def progress(done, total, g=g):
            if total and (done % 10000 < cfg.MEMBERS_PAGE or done >= total):
                log(f"    {g['name'][:40]}: {done}/{total}")
        try:
            before = len(found)
            for u in iter_members(api, g["id"], limit_per_group, progress):
                if is_candidate(u):
                    found.setdefault(u["id"], {"user": u, "groups": set()})["groups"].add(g["id"])
            scanned += 1
            log(f"  ✓ {g['name'][:45]}: подходящих мужчин из СПб (всего накоплено) — {len(found)} "
                f"(+{len(found) - before} новых)")
        except VKError as e:
            log(f"  ✗ {g['name'][:45]}: пропущено — {e.message}")
    return found, scanned


# ── оценка ──────────────────────────────────────────────────────────────────

def university_fit(user: dict, year: int) -> tuple[bool, int | None, str | None, int | None]:
    """
    Лучшее совпадение по ВУЗам СПб: (подошёл, |начало − год|, название, год начала).
    Год начала = год окончания − DEGREE_YEARS (в VK хранится только окончание).
    """
    best = None
    for un in user.get("universities") or []:
        if un.get("city") != cfg.CITY_ID_SPB:
            continue
        grad = int(un.get("graduation") or 0)
        if not grad:
            continue
        start = grad - cfg.DEGREE_YEARS
        delta = abs(start - year)
        if best is None or delta < best[0]:
            best = (delta, un.get("name"), start)
    if best is None:
        return False, None, None, None
    delta, name, start = best
    return delta <= cfg.FIT_YEARS, delta, name, start


def score_user(user: dict, n_member: int, n_total: int, year: int) -> dict:
    """a, b и итог k1*a + k2*b для одного человека."""
    a = n_member / n_total if n_total else 0.0
    fit, delta, uni, start = university_fit(user, year)
    b = cfg.B_BY_DELTA.get(delta, 0.0) if fit else 0.0
    k2 = cfg.K2_FIT if fit else cfg.K2_NOFIT
    return {"user_id": user["id"], "first_name": user.get("first_name") or "",
            "last_name": user.get("last_name") or "", "communities": n_member,
            "a": round(a, 4), "b": round(b, 4), "score": round(cfg.K1 * a + k2 * b, 4),
            "university": uni, "start_year": start, "fit": fit}


def rank(found: dict[int, dict], n_total: int, year: int | None = None,
         min_communities: int = cfg.MIN_COMMUNITIES) -> list[dict]:
    """Оставляет состоящих минимум в min_communities сообществ и сортирует по score."""
    year = year or datetime.now().year
    rows = [score_user(v["user"], len(v["groups"]), n_total, year)
            for v in found.values() if len(v["groups"]) >= min_communities]
    rows.sort(key=lambda r: (-r["score"], -r["communities"], r["user_id"]))
    return rows
