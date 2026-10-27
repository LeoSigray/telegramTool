# -*- coding: utf-8 -*-
"""
Подбор целевой аудитории по аналитике «Кто ЦА»: студенты из Санкт-Петербурга, мужчины.

Алгоритм из документа:
  1. ТОП-N популярных сообществ СПб (автоподбор или список из файла).
  2. Пол и город — из профиля (мужчины, город = Санкт-Петербург).
  3. Оставляем тех, кто состоит минимум в MIN_COMMUNITIES из них.
  4. a — доля сообществ, где состоит человек (3 из 10 -> 0.3).
  5. b — вероятность по ВУЗу СПб: чем ближе год начала обучения к текущему
     (|начало − текущий| <= FIT_YEARS), тем выше. В VK хранится год ОКОНЧАНИЯ,
     начало = окончание − срок обучения (по статусу: бакалавр 4, специалист 5, магистр 2).
  6. score = K1 * a + K2 * b, где K2 больше, если ВУЗ подошёл. Сортировка по score.

Что улучшено относительно «в лоб» (результат тот же, работы меньше):
  • Отсечение по принципу Дирихле. Чтобы состоять минимум в m из n сообществ, человек обязан быть
    хотя бы в одном из ЛЮБЫХ n−m+1 сообществ. Поэтому полностью читаем только самые маленькие n−m+1,
    а в самых больших (там миллионы участников) проверяем членство только у уже найденных
    кандидатов через groups.isMember (до 500 человек за запрос). Список «кто в ≥ m сообществах»
    при этом точно тот же, а запросов в разы меньше.
  • 8 вызовов getMembers за один запрос через execute (лимит VK считается по запросам).
  • «a» с весом редкости: членство в маленьком тематическом сообществе говорит о человеке больше,
    чем членство в «Подслушано» на миллион (как IDF в поиске). Простая доля a остаётся в таблице.
  • Активность: давно не заходившего отбрасываем, недавно заходившему — выше (он прочитает сообщение).
  • Возраст из даты рождения, статус «Выпускник» не считаем студентом.

Чистые функции (score_user / university_fit / rank) не ходят в сеть — их можно проверять отдельно.
"""
import math
import re
import time
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

def _exec_code(group_id: int, offset: int, pages: int) -> str:
    """VKScript: несколько страниц участников за один запрос."""
    return ('var res=[];var i=0;var total=0;'
            f'while(i<{int(pages)}){{'
            f'var r=API.groups.getMembers({{"group_id":{int(group_id)},"offset":{int(offset)}+i*{cfg.MEMBERS_PAGE},'
            f'"count":{cfg.MEMBERS_PAGE},"fields":"{cfg.MEMBER_FIELDS}"}});'
            'total=r.count;res.push(r.items);i=i+1;}'
            'return {"count":total,"pages":res};')


def _pages_plain(api: VKApi, group_id: int, offset: int, want: int) -> tuple[list, int]:
    r = api.call("groups.getMembers", group_id=group_id, offset=offset, count=want, fields=cfg.MEMBER_FIELDS)
    return r.get("items", []), r.get("count", 0)


def iter_members(api: VKApi, group_id: int, limit: int | None = None, on_page=None):
    """Участники сообщества. Пока можно, берём по EXECUTE_PAGES страниц за запрос (execute);
    при сбое execute переходим на обычные запросы."""
    offset, total = 0, None
    use_exec = cfg.USE_EXECUTE
    while True:
        left = None if not limit else limit - offset
        if left is not None and left <= 0:
            return
        items = None
        if use_exec and (left is None or left > cfg.MEMBERS_PAGE):
            pages = cfg.EXECUTE_PAGES if left is None else min(cfg.EXECUTE_PAGES, -(-left // cfg.MEMBERS_PAGE))
            try:
                r = api.call("execute", code=_exec_code(group_id, offset, pages))
            except VKError:
                r = None
            if isinstance(r, dict) and isinstance(r.get("pages"), list):
                total = r.get("count", 0)
                items = [x for chunk in r["pages"] for x in (chunk or [])]
            else:
                use_exec = False                      # execute не прошёл (лимит ответа, права): дальше по одной
        if items is None:
            want = cfg.MEMBERS_PAGE if left is None else min(cfg.MEMBERS_PAGE, left)
            items, total = _pages_plain(api, group_id, offset, want)
        if limit:
            items = items[: max(0, limit - offset)]
        yield from items
        offset += len(items)
        if on_page:
            on_page(offset, total)
        if not items or offset >= (total or 0) or (limit and offset >= limit):
            return


def _age(bdate: str | None, today: datetime) -> int | None:
    """Возраст из bdate «Д.М.ГГГГ» (если год скрыт — None)."""
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", bdate or "")
    if not m:
        return None
    d, mo, y = map(int, m.groups())
    return today.year - y - ((today.month, today.day) < (mo, d))


def is_candidate(u: dict, now: float | None = None) -> bool:
    """Пол и город — из профиля. Удалённых, тех, кому нельзя написать, давно не заходивших и слишком
    взрослых отсекаем."""
    if u.get("deactivated"):
        return False
    if u.get("sex") != cfg.SEX_MALE:
        return False
    if (u.get("city") or {}).get("id") != cfg.CITY_ID_SPB:
        return False
    if u.get("can_write_private_message") == 0:
        return False
    seen = (u.get("last_seen") or {}).get("time")
    if seen and ((now or time.time()) - seen) / 86400 > cfg.ACT_DROP_DAYS:
        return False
    age = _age(u.get("bdate"), datetime.now())
    if age is not None and not (cfg.AGE_MIN <= age <= cfg.AGE_MAX):
        return False
    return True


class Found(dict):
    """{user_id: {"user": профиль, "groups": {id сообществ}}} + веса сообществ и статистика отсечения."""
    weights: dict
    stats: dict


def _members_of(api: VKApi, group_id: int, user_ids: list[int]) -> set[int]:
    """Кто из user_ids состоит в сообществе (groups.isMember, до 500 за запрос)."""
    out: set[int] = set()
    for i in range(0, len(user_ids), cfg.IS_MEMBER_BATCH):
        chunk = user_ids[i:i + cfg.IS_MEMBER_BATCH]
        r = api.call("groups.isMember", group_id=group_id, user_ids=",".join(map(str, chunk)))
        for row in r or []:
            if isinstance(row, dict) and row.get("member"):
                out.add(int(row["user_id"]))
    return out


def community_weight(members_count: int) -> float:
    """Редкость сообщества: маленькое тематическое весит больше миллионника (как IDF)."""
    return math.log(1.0 + cfg.WEIGHT_REF / max(members_count, 1))


def collect(api: VKApi, communities: list[dict], limit_per_group: int | None = None,
            log=print, prune: bool | None = None) -> tuple["Found", int]:
    """
    Возвращает (Found, сколько сообществ прочитано). Недоступное сообщество (закрыто/скрыт список)
    пропускается, остальные продолжают. prune — отсечение по принципу Дирихле (по умолчанию cfg.PRUNE).
    """
    prune = cfg.PRUNE if prune is None else prune
    found = Found()
    found.weights = {g["id"]: community_weight(g["members_count"]) for g in communities}
    found.stats = {"read_full": 0, "checked": 0, "candidates": 0}
    m = cfg.MIN_COMMUNITIES
    order = sorted(communities, key=lambda g: g["members_count"])
    n_seed = len(order) - m + 1 if prune and len(order) > m else len(order)
    seeds, rest = order[:n_seed], order[n_seed:]
    scanned = 0
    for g in seeds:
        def progress(done, total, g=g):
            if total and (done % 10000 < cfg.MEMBERS_PAGE * cfg.EXECUTE_PAGES or done >= total):
                log(f"    {g['name'][:40]}: {done}/{total}")
        try:
            before = len(found)
            for u in iter_members(api, g["id"], limit_per_group, progress):
                if is_candidate(u):
                    found.setdefault(u["id"], {"user": u, "groups": set()})["groups"].add(g["id"])
            scanned += 1
            found.stats["read_full"] += 1
            log(f"  ✓ {g['name'][:45]}: подходящих мужчин из СПб (всего накоплено) — {len(found)} "
                f"(+{len(found) - before} новых)")
        except VKError as e:
            log(f"  ✗ {g['name'][:45]}: пропущено — {e.message}")
    found.stats["candidates"] = len(found)
    if rest and found:
        ids = _best_first(found)[:cfg.ISMEMBER_MAX_CANDIDATES]
        for idx, g in enumerate(rest):
            # кого ещё имеет смысл проверять: уже состоящий в k сообществах дотянет до m, только если
            # k + оставшиеся_сообщества >= m (остальные в итог не попадут, их не проверяем)
            left = len(rest) - idx
            todo = [uid for uid in ids if len(found[uid]["groups"]) + left >= m]
            try:
                have = _members_of(api, g["id"], todo)
            except VKError as e:
                log(f"  ✗ {g['name'][:45]}: пропущено — {e.message}")
                continue
            for uid in have:
                found[uid]["groups"].add(g["id"])
            scanned += 1
            found.stats["checked"] += 1
            log(f"  ✓ {g['name'][:45]}: из {len(todo)} кандидатов состоят {len(have)} (проверка по списку, без чтения всех участников)")
    elif rest:
        scanned += len(rest)
    return found, scanned


def _best_first(found: dict) -> list[int]:
    """Кандидатов, у которых членство в больших сообществах проверяем первыми: больше шансов на ВУЗ и активность."""
    year = datetime.now().year
    return sorted(found, key=lambda uid: (-(1 if university_fit(found[uid]["user"], year)[0] else 0),
                                          -len(found[uid]["groups"]), -activity(found[uid]["user"]), uid))


# ── оценка ──────────────────────────────────────────────────────────────────

def study_years(status: str | None) -> int:
    """Срок обучения по статусу из профиля VK («Студент (бакалавр)» и т. п.)."""
    s = (status or "").lower()
    if "магистр" in s:
        return 2
    if "специалист" in s:
        return 5
    if "аспирант" in s:
        return 3
    return cfg.DEGREE_YEARS


def university_fit(user: dict, year: int) -> tuple[bool, int | None, str | None, int | None]:
    """
    Лучшее совпадение по ВУЗам СПб: (подошёл, |начало − год|, название, год начала).
    Год начала = год окончания − срок обучения (в VK хранится только окончание).
    «Выпускник» с годом окончания в прошлом студентом не считается.
    """
    best = None
    for un in user.get("universities") or []:
        if un.get("city") != cfg.CITY_ID_SPB:
            continue
        grad = int(un.get("graduation") or 0)
        if not grad:
            continue
        status = str(un.get("education_status") or "")
        if status.lower().startswith("выпускник") and grad < year:
            continue
        start = grad - study_years(status)
        delta = abs(start - year)
        if best is None or delta < best[0]:
            best = (delta, un.get("name"), start)
    if best is None:
        return False, None, None, None
    delta, name, start = best
    return delta <= cfg.FIT_YEARS, delta, name, start


def activity(user: dict, now: float | None = None) -> float:
    """Вероятность, что человек скоро увидит сообщение: недавно заходил — выше."""
    seen = (user.get("last_seen") or {}).get("time")
    if not seen:
        return cfg.ACT_UNKNOWN
    days = ((now or time.time()) - seen) / 86400
    for limit, factor in cfg.ACT_STEPS:
        if days <= limit:
            return factor
    return cfg.ACT_OLD


def age_factor(age: int | None) -> float:
    if age is None:
        return cfg.AGE_UNKNOWN
    return 1.0 if cfg.AGE_BEST[0] <= age <= cfg.AGE_BEST[1] else cfg.AGE_OTHER


def score_user(user: dict, n_member: int, n_total: int, year: int,
               weights: dict | None = None, member_of: set | None = None) -> dict:
    """a, b и итог для одного человека. Без weights/member_of — как в документе: a = n_member / n_total."""
    a_simple = n_member / n_total if n_total else 0.0
    a_w = a_simple
    if weights and member_of is not None and cfg.WEIGHTED_A:
        total_w = sum(weights.values())
        a_w = sum(weights.get(g, 0.0) for g in member_of) / total_w if total_w else a_simple
    a = a_w if cfg.WEIGHTED_A else a_simple
    fit, delta, uni, start = university_fit(user, year)
    b = cfg.B_BY_DELTA.get(delta, 0.0) if fit else 0.0
    k2 = cfg.K2_FIT if fit else cfg.K2_NOFIT
    act = activity(user) if cfg.USE_ACTIVITY else 1.0
    age = _age(user.get("bdate"), datetime.now())
    base = cfg.K1 * a + k2 * b
    status = next((un.get("education_status") for un in (user.get("universities") or [])
                   if un.get("name") == uni), None) if uni else None
    return {"user_id": user["id"], "first_name": user.get("first_name") or "",
            "last_name": user.get("last_name") or "", "communities": n_member,
            "a": round(a_simple, 4), "a_w": round(a_w, 4), "b": round(b, 4),
            "score": round(base * act * (age_factor(age) if cfg.USE_ACTIVITY else 1.0), 4),
            "university": uni, "start_year": start, "fit": fit, "age": age,
            "last_seen": (user.get("last_seen") or {}).get("time"), "act": round(act, 3), "edu_status": status}


def rank(found: dict, n_total: int, year: int | None = None,
         min_communities: int = cfg.MIN_COMMUNITIES) -> list[dict]:
    """Оставляет состоящих минимум в min_communities сообществ и сортирует по score."""
    year = year or datetime.now().year
    weights = getattr(found, "weights", None)
    rows = [score_user(v["user"], len(v["groups"]), n_total, year, weights, v["groups"])
            for v in found.values() if len(v["groups"]) >= min_communities]
    rows.sort(key=lambda r: (-r["score"], -r["communities"], r["user_id"]))
    return rows
