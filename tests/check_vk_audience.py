"""VK: подбор ЦА. Отсечение по принципу Дирихле даёт тот же результат, что чтение всех сообществ,
но с меньшим числом запросов; оценка a/b/активность. Без сети: поддельный VK.

Запуск: python tests/check_vk_audience.py (печатает ALL OK).
"""
import os
import random
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from vk import audience as aud  # noqa: E402
from vk import config as cfg  # noqa: E402
from vk.api import VKError  # noqa: E402

NOW = time.time()


class FakeVK:
    """groups.getMembers / execute / groups.isMember на выдуманных данных."""

    def __init__(self, groups: dict[int, list[int]], profiles: dict[int, dict], execute_ok=True):
        self.groups, self.profiles, self.execute_ok = groups, profiles, execute_ok
        self.calls: dict[str, int] = {}

    def _members(self, group_id, off, cnt):
        ids = self.groups[group_id]
        return {"count": len(ids), "items": [self.profiles[i] for i in ids[off:off + cnt]]}

    def requests(self):
        """Сколько HTTP-запросов ушло бы в VK (вложенные вызовы execute — один запрос)."""
        return sum(self.calls.values())

    def call(self, method, **p):
        self.calls[method] = self.calls.get(method, 0) + 1
        if method == "groups.getMembers":
            return self._members(p["group_id"], p.get("offset", 0), p.get("count", 1000))
        if method == "execute":
            if not self.execute_ok:
                raise VKError(13, "runtime error")
            code = p["code"]
            g = int(re.search(r'"group_id":(\d+)', code).group(1))
            off = int(re.search(r'"offset":(\d+)\+i', code).group(1))
            pages = int(re.search(r"while\(i<(\d+)\)", code).group(1))
            res = [self._members(g, off + i * 1000, 1000)["items"] for i in range(pages)]
            return {"count": len(self.groups[g]), "pages": res}
        if method == "groups.isMember":
            have = set(self.groups[p["group_id"]])
            return [{"user_id": int(u), "member": 1 if int(u) in have else 0} for u in p["user_ids"].split(",")]
        raise AssertionError(method)


def profile(uid, *, male=True, spb=True, seen_days=2, uni_grad=None, status="Студент (бакалавр)", bdate=None, can=1):
    u = {"id": uid, "first_name": f"И{uid}", "last_name": f"Ф{uid}", "sex": 2 if male else 1,
         "city": {"id": 2 if spb else 1}, "can_write_private_message": can,
         "last_seen": {"time": NOW - seen_days * 86400}}
    if uni_grad:
        u["universities"] = [{"id": 1, "city": 2, "name": "СПбГУ", "graduation": uni_grad, "education_status": status}]
    if bdate:
        u["bdate"] = bdate
    return u


# ── та же выдача, что у чтения всех сообществ, но дешевле ──────────────────────
rng = random.Random(7)
N_USERS = 200_000


class Universe(dict):
    """Профили создаются по требованию: в тесте 200 тысяч человек, но читаются не все."""

    def __missing__(self, uid):
        r = random.Random(uid)
        self[uid] = profile(uid, male=r.random() < 0.6, spb=r.random() < 0.7, seen_days=r.choice([1, 5, 20, 90, 200]),
                            uni_grad=r.choice([None, 2026, 2027, 2028, 2029]))
        return self[uid]


universe = Universe()
sizes = [150_000, 120_000, 4000, 3500, 3000, 2500, 2000, 1500, 1000, 800]
groups = {100 + k: rng.sample(range(1, N_USERS + 1), n) for k, n in enumerate(sizes)}
comms = [{"id": gid, "name": f"G{gid}", "screen_name": f"g{gid}", "members_count": len(ids)} for gid, ids in groups.items()]
rng.shuffle(comms)

full_api = FakeVK(groups, universe)
found_full, scanned_full = aud.collect(full_api, comms, log=lambda *a: None, prune=False)
pruned_api = FakeVK(groups, universe)
found_pruned, scanned_pruned = aud.collect(pruned_api, comms, log=lambda *a: None, prune=True)
assert scanned_full == scanned_pruned == 10


def at_least(found, m):
    return {uid: v["groups"] for uid, v in found.items() if len(v["groups"]) >= m}


assert at_least(found_full, 3) == at_least(found_pruned, 3), "отсечение не должно терять и добавлять людей"
assert len(at_least(found_full, 3)) > 20, "в тесте должны быть люди в трёх и более сообществах"
assert pruned_api.calls.get("groups.isMember", 0) > 0
assert pruned_api.requests() < full_api.requests(), (pruned_api.requests(), full_api.requests())
assert found_pruned.stats["read_full"] == 8 and found_pruned.stats["checked"] == 2
rows_full = aud.rank(found_full, 10, 2026)
rows_pruned = aud.rank(found_pruned, 10, 2026)
assert [r["user_id"] for r in rows_full] == [r["user_id"] for r in rows_pruned], "порядок в итоге тот же"
assert rows_full[0]["score"] >= rows_full[-1]["score"]

# execute сломался — тот же результат обычными запросами
slow_api = FakeVK(groups, universe, execute_ok=False)
found_slow, _ = aud.collect(slow_api, comms, log=lambda *a: None, prune=True)
assert at_least(found_slow, 3) == at_least(found_pruned, 3)
assert slow_api.calls["execute"] == 8 and slow_api.requests() > pruned_api.requests(), "без execute запросов больше"

# ограничение числа участников на сообщество
lim_api = FakeVK(groups, universe)
list(aud.iter_members(lim_api, comms[0]["id"], limit=1500))
assert sum(1 for _ in aud.iter_members(FakeVK(groups, universe), comms[0]["id"], limit=1500)) == 1500

# недоступное сообщество пропускается, остальные работают
class Closed(FakeVK):
    def call(self, method, **p):
        if method in ("groups.getMembers", "execute", "groups.isMember") and (p.get("group_id") == 100 or "group_id\":100" in p.get("code", "")):
            raise VKError(15, "access denied")
        return super().call(method, **p)

found_c, scanned_c = aud.collect(Closed(groups, universe), comms, log=lambda *a: None)
assert scanned_c == 9

# ── кто подходит ──────────────────────────────────────────────────────────────
ok = profile(1)
assert aud.is_candidate(ok, NOW)
assert not aud.is_candidate(profile(2, male=False), NOW) and not aud.is_candidate(profile(3, spb=False), NOW)
assert not aud.is_candidate(profile(4, can=0), NOW) and not aud.is_candidate(profile(5, seen_days=cfg.ACT_DROP_DAYS + 5), NOW)
year = 2026
assert not aud.is_candidate(profile(6, bdate=f"1.1.{year - 40}"), NOW) and aud.is_candidate(profile(7, bdate=f"1.1.{year - 20}"), NOW)
assert aud.is_candidate(profile(8, bdate="5.6"), NOW), "год рождения скрыт — не отсекаем"

# ── оценка: a, b, срок обучения, выпускники, активность, редкость ─────────────
assert aud.study_years("Студент (магистр)") == 2 and aud.study_years("Студент (специалист)") == 5 and aud.study_years(None) == 4
fit_user = profile(10, uni_grad=2029)                      # начало 2025: |2025−2026| = 1
assert aud.university_fit(fit_user, 2026)[:2] == (True, 1)
assert aud.university_fit(profile(11, uni_grad=2025, status="Выпускник (бакалавр)"), 2026)[0] is False, "выпускник — не студент"
assert aud.university_fit(profile(12, uni_grad=2028, status="Студент (магистр)"), 2026)[:3] == (True, 0, "СПбГУ"), "магистр: начало 2026"
assert aud.university_fit(profile(13, uni_grad=2031, status="Студент (специалист)"), 2026)[0] is True

cfg_wa, cfg_act = cfg.WEIGHTED_A, cfg.USE_ACTIVITY
cfg.WEIGHTED_A = False
r = aud.score_user(fit_user, 3, 10, 2026)
assert r["a"] == 0.3 and r["b"] == 0.85 and r["fit"] is True
assert abs(r["score"] - (cfg.K1 * 0.3 + cfg.K2_FIT * 0.85) * r["act"] * aud.age_factor(None)) < 1e-3
no_fit = aud.score_user(profile(14), 3, 10, 2026)
assert no_fit["b"] == 0 and abs(no_fit["score"] - 0.3 * no_fit["act"] * aud.age_factor(None)) < 1e-3
cfg.USE_ACTIVITY = False
assert aud.score_user(fit_user, 3, 10, 2026)["score"] == round(cfg.K1 * 0.3 + cfg.K2_FIT * 0.85, 4)
cfg.USE_ACTIVITY = True
fresh, stale = profile(15, seen_days=1), profile(16, seen_days=120)
assert aud.score_user(fresh, 3, 10, 2026)["score"] > aud.score_user(stale, 3, 10, 2026)["score"], "недавно заходил — выше"

# редкость: те же 3 из 4 сообществ, но в маленьких весят больше, чем в миллионнике
cfg.WEIGHTED_A = True
weights = {1: aud.community_weight(1_000_000), 2: aud.community_weight(1_000_000), 3: aud.community_weight(5_000), 4: aud.community_weight(8_000)}
big = aud.score_user(fresh, 2, 4, 2026, weights, {1, 2})
small = aud.score_user(fresh, 2, 4, 2026, weights, {3, 4})
assert big["a"] == small["a"] == 0.5 and small["a_w"] > big["a_w"] and small["score"] > big["score"]
cfg.WEIGHTED_A, cfg.USE_ACTIVITY = cfg_wa, cfg_act

# в таблице: Вася в 10 сообществах выше Вани в 3, Петя в 2 не попал
found = aud.Found()
found.weights = {i: 1.0 for i in range(10)}
found[1] = {"user": profile(1), "groups": set(range(10))}
found[2] = {"user": profile(2), "groups": {0, 1, 2}}
found[3] = {"user": profile(3), "groups": {0, 1}}
rows = aud.rank(found, 10, 2026)
assert [r["user_id"] for r in rows] == [1, 2] and rows[0]["a"] == 1.0 and rows[1]["a"] == 0.3
print(f"запросов к VK: читать всё без ухищрений ≈ {sum(-(-n // 1000) for n in sizes)}, с execute {full_api.requests()}, с отсечением и execute {pruned_api.requests()}")
print("ALL OK")
