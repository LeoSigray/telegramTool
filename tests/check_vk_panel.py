"""VK-панель: доступ только двум, токены, подбор ЦА, рассылка с вариантами текста, ответы, DarkStore.

Без сети и без Telegram: поддельные Bot API, VK и DarkStore, отдельная временная база.
Запуск: python tests/check_vk_panel.py (печатает ALL OK).
"""
import asyncio
import json
import os
import random
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

tmp = tempfile.mkdtemp()
os.environ["VK_DB_PATH"] = os.path.join(tmp, "vk.db")
os.environ["VKPANEL_ADMINS"] = "Ivan_Admin, 777"
os.environ.pop("VK_REF_LINK", None)
os.environ.pop("VK_TOKEN", None)

from vk import audience, messages, replies, sender, store  # noqa: E402
from vk.api import VKError  # noqa: E402
from vkpanel import panel as pn  # noqa: E402
from vkpanel import shop  # noqa: E402

assert store.db_path() == os.path.join(tmp, "vk.db"), "VK живёт в своей базе"


# ── поддельные Telegram и VK ──────────────────────────────────────────────────
class FakeTg:
    def __init__(self):
        self.sent, self.edited, self.deleted, self.files, self._n = [], [], [], [], 0

    async def send(self, chat, text, kb=None):
        self._n += 1
        self.sent.append((chat, text, kb))
        return {"message_id": self._n}

    async def edit(self, chat, mid, text, kb=None):
        self.edited.append((chat, mid, text))

    async def delete(self, chat, mid):
        self.deleted.append((chat, mid))

    async def answer(self, *a, **k):
        pass

    async def send_file(self, chat, path, caption=""):
        self.files.append((chat, path, caption))

    def last(self):
        return self.sent[-1][1]


class FakeVK:
    """users.get / groups.* / messages.send / getConversations на выдуманных данных."""
    sent_to: list = []
    fail_send: dict = {}
    conversations: dict = {}

    def __init__(self, token, **kw):
        self.token = token

    def call(self, method, **p):
        if method == "users.get":
            if self.token.startswith("bad"):
                raise VKError(5, "invalid token")
            return [{"id": 1000 + len(self.token), "first_name": "Акк", "last_name": self.token[-3:]}]
        if method == "messages.send":
            if p["user_id"] in FakeVK.fail_send:
                raise VKError(FakeVK.fail_send[p["user_id"]], "can't send")
            FakeVK.sent_to.append((self.token, p["user_id"], p["message"]))
            return 1
        if method == "messages.getConversations":
            return {"items": FakeVK.conversations.get(self.token, [])}
        if method == "groups.search":
            return {"items": [{"id": 500 + k} for k in range(3)] if p["type"] == "group" else []}
        if method == "groups.getById":
            return [{"id": 500 + k, "name": f"СПб {k}", "screen_name": f"spb{k}", "members_count": 1000 * (k + 1)}
                    for k in range(len(p["group_ids"].split(",")))]
        if method == "groups.getMembers":
            ids = GROUPS[p["group_id"]]
            off, cnt = p.get("offset", 0), p.get("count", 1000)
            return {"count": len(ids), "items": [PROFILES[i] for i in ids[off:off + cnt]]}
        if method == "execute":
            raise VKError(13, "no execute in fake")
        if method == "groups.isMember":
            have = set(GROUPS[p["group_id"]])
            return [{"user_id": int(u), "member": 1 if int(u) in have else 0} for u in p["user_ids"].split(",")]
        raise AssertionError(method)


NOW = time.time()
PROFILES = {i: {"id": i, "first_name": f"Ваня{i}", "last_name": "Тестов", "sex": 2, "city": {"id": 2},
                "can_write_private_message": 1, "last_seen": {"time": NOW - 86400},
                "universities": [{"city": 2, "name": "СПбГУ", "graduation": 2029, "education_status": "Студент (бакалавр)"}]}
            for i in range(1, 61)}
# 3 сообщества (id 500, 501, 502), чтобы «минимум 3 из N» проверялось на малом числе
GROUPS = {500: list(range(1, 41)), 501: list(range(20, 51)), 502: list(range(30, 61))}
pn.VKApi = FakeVK
sender.VKApi = FakeVK
replies.VKApi = FakeVK
audience.cfg.MIN_COMMUNITIES = 2
pn.SLEEP = lambda s: None
sender.cfg.DM_DELAY_MIN = sender.cfg.DM_DELAY_MAX = 0
sender.cfg.SKIP_DELAY = (0, 0)


def run(coro):
    return asyncio.run(coro)


tg = FakeTg()
panel = pn.Panel(tg, pn.parse_admins())
admin = {"id": 5, "username": "Ivan_Admin"}
stranger = {"id": 9, "username": "random"}


def msg(frm, text, mid=1):
    return {"from": frm, "chat": {"id": frm["id"], "type": "private"}, "text": text, "message_id": mid}


def cb(frm, data):
    return {"id": "c", "from": frm, "data": data, "message": {"chat": {"id": frm["id"]}, "message_id": 1}}


# ── доступ: только двое ───────────────────────────────────────────────────────
run(panel.on_message(msg(stranger, "/start")))
run(panel.on_callback(cb(stranger, "st")))
assert tg.sent == [], "чужому панель не отвечает вовсе"
assert panel.is_admin(admin) and store.get_setting("panel_admin:ivan_admin") == "5", "id запоминается при первом входе"
assert not panel.is_admin({"id": 6, "username": "Ivan_Admin"}), "имя перешло к другому человеку — доступа нет"
assert panel.is_admin({"id": 777, "username": None}) and not panel.is_admin({"id": 778})
run(panel.on_message(msg(admin, "/start")))
assert "VK-панель" in tg.last() and len(tg.sent[-1][2]) == 5

# ── токены: вставили, проверили, сообщение с токеном удалено ────────────────────
good = "vk1.a." + "x" * 80
run(panel.on_message(msg(admin, good, mid=42)))
assert (5, 42) in tg.deleted and "✓" in tg.last() and len(store.list_accounts()) == 1
run(panel.on_message(msg(admin, "bad" + "y" * 60, mid=43)))
assert (5, 43) in tg.deleted and "✗" in tg.last() and len(store.list_accounts()) == 1
for k in range(2):
    run(panel.on_message(msg(admin, f"vk1.a.{k}" + "z" * 80)))
assert len(store.list_accounts()) == 3

# ── без ссылки рассылка не стартует ───────────────────────────────────────────
run(panel.on_callback(cb(admin, "bc")))
assert "не задана реферальная ссылка" in tg.last() and "нет целей" in tg.last()
run(panel.on_message(msg(admin, "/link не ссылка")))
assert "не похоже на ссылку" in tg.last() and not messages.ref_link()
run(panel.on_message(msg(admin, "/link https://example.com/ref?x=1")))
assert messages.ref_link() == "https://example.com/ref?x=1"

# ── подбор ЦА в фоне ──────────────────────────────────────────────────────────
async def wait_job(kind):
    for _ in range(400):
        if not panel.jobs.running(kind):
            return
        await asyncio.sleep(0.05)
    raise AssertionError("дело не закончилось")


async def collect_flow():
    await panel.on_callback(cb(admin, "au:auto"))
    await wait_job("collect")
    await asyncio.sleep(5.2)                        # watch() обновляет сообщение раз в 5 секунд
run(collect_flow())
done = [e for e in tg.edited if "Подбор ЦА" in e[2] and "✅" in e[2]]
assert done and "Топ-10" in done[-1][2] and "Ваня" in done[-1][2], tg.edited[-1:]
targets = store.list_targets("new")
assert targets and all(t["communities"] >= 2 for t in targets)
assert sorted(json.loads(store.get_setting("communities"))) == ["spb0", "spb1", "spb2"]

# ── топ и Excel ───────────────────────────────────────────────────────────────
run(panel.on_callback(cb(admin, "tp:0")))
assert "Топ ЦА" in tg.last() and "vk.com/id" in tg.last()
run(panel.on_callback(cb(admin, "xl")))
assert tg.files and tg.files[-1][2].startswith("ЦА:")

# ── рассылка: варианты, ссылка, лимит на аккаунт, остановка ───────────────────
FakeVK.sent_to.clear()
store.set_setting("dm_limit", "2")
assert sender.daily_limit() == 2
run(panel.on_callback(cb(admin, "bc")))
assert "Скольким отправить" in tg.last()
run(panel.on_callback(cb(admin, "bc:5")))
assert "Пример текста" in tg.last() and "example.com" in tg.last()


async def bc_flow(n):
    await panel.on_callback(cb(admin, f"bc:go:{n}"))
    await wait_job("broadcast")
    await asyncio.sleep(5.2)
run(bc_flow(5))
assert len(FakeVK.sent_to) == 5, FakeVK.sent_to
assert all("https://example.com/ref?x=1" in m for _, _, m in FakeVK.sent_to), "в каждом тексте реферальная ссылка"
assert all("{" not in m for _, _, m in FakeVK.sent_to), "подстановки заменены"
assert len({m.split("Ваня")[0][:12] for _, _, m in FakeVK.sent_to}) >= 1
assert len({t for t, _, _ in FakeVK.sent_to}) == 3, "по очереди с каждого аккаунта"
st = store.counts()
assert st.get("sent") == 5
stats = {v["variant"]: v for v in store.variant_stats()}
assert sum(v["sent"] for v in stats.values()) == 5 and all(k.startswith("v") for k in stats)
# лимит 2 в сутки на аккаунт: 3 аккаунта = 6, уже 5 — доступна ещё одна отправка
assert sum(a["id"] and (2 - store.sent_last_24h(a["id"])) for a in store.list_accounts()) == 1
run(panel.on_callback(cb(admin, "bc")))
assert "лимит на сейчас ≈1" in tg.last()

# остановка по просьбе
FakeVK.sent_to.clear()
store.set_setting("dm_limit", "50")
calls = {"n": 0}


def stopper():
    calls["n"] += 1
    return calls["n"] > 2
st = sender.run_broadcast(messages.variants(), 10, link="https://x", sleep=lambda s: None, should_stop=stopper, api_factory=FakeVK)
assert st["sent"] == 2 and st["stop"] == "остановлено вручную"

# нельзя написать / флуд: цель закрывается, аккаунт уходит в отдых
FakeVK.sent_to.clear()
nxt = store.list_targets("new")[0]["user_id"]
FakeVK.fail_send = {nxt: 902}
st = sender.run_broadcast(messages.variants(), 1, link="https://x", sleep=lambda s: None, api_factory=FakeVK)
assert st["closed"] == 1 and store.counts().get("closed") == 1
FakeVK.fail_send = {}

# ── тексты: без {link} не принимаются, имя убирается чисто ─────────────────────
assert messages.render("{name}, привет! {link}", {"first_name": ""}, "L") == "Привет! L"
assert messages.render("Привет, {name}! {link}", {"first_name": "Ваня"}, "L") == "Привет, Ваня! L"
assert all("{link}" in t for _, t in messages.DEFAULT_VARIANTS) and len(messages.DEFAULT_VARIANTS) >= 4
assert not any(w in t.lower() for _, t in messages.DEFAULT_VARIANTS for w in ("тысяч", "гарант", "₽", "руб", "в день", "в час", "доход")), "без обещаний заработка"
run(panel.on_callback(cb(admin, "se:varadd")))
run(panel.on_message(msg(admin, "без ссылки")))
assert "нужна вставка {link}" in tg.last()
run(panel.on_callback(cb(admin, "se:varadd")))
run(panel.on_message(msg(admin, "Свой текст {link}")))
assert messages.variants() == [("s1", "Свой текст {link}")]
messages.reset_variants()
assert len(messages.variants()) == len(messages.DEFAULT_VARIANTS)

# ── ответы людей ─────────────────────────────────────────────────────────────
who = FakeVK.sent_to and FakeVK.sent_to[0][1]
sent_user = next(t["user_id"] for t in store.list_targets("sent"))
tok = store.list_accounts()[0]["token"]
FakeVK.conversations = {tok: [
    {"conversation": {"peer": {"id": sent_user, "type": "user"}}, "last_message": {"id": 91, "out": 0, "text": "Привет, а сколько платят?"}},
    {"conversation": {"peer": {"id": 424242, "type": "user"}}, "last_message": {"id": 92, "out": 0, "text": "чужой человек"}},
    {"conversation": {"peer": {"id": sent_user, "type": "user"}}, "last_message": {"id": 93, "out": 1, "text": "наше сообщение"}}]}
fresh = replies.poll_replies(FakeVK)
assert [r["msg_id"] for r in fresh] == [91] and replies.poll_replies(FakeVK) == [], "тот же ответ второй раз не считается"
tg.sent.clear()
run(panel.on_callback(cb(admin, "rp")))
assert "сколько платят" in tg.last() and "vk.com/im?sel=" in tg.last()
assert any(v["replied"] for v in store.variant_stats())

# ── DarkStore: безопасные товары и потолок суммы ──────────────────────────────
class FakeDS:
    def __init__(self):
        self.orders = []

    def balance(self):
        return 50.0, "RUB"

    def products(self, **kw):
        base = {"quantity": 5, "attributes": [], "guarantee_time_seconds": 0}
        return [{**base, "id": 1, "name": "VK ручная регистрация", "price": 17.4},
                {**base, "id": 2, "name": "VK Спам блок: Вечный бан", "price": 0.4},
                {**base, "id": 3, "name": "VK авторег", "price": 80.0},
                {**base, "id": 4, "name": "VK ручной", "price": 5.0, "is_manual_order_delivery": True},
                {**base, "id": 5, "name": "VK нет в наличии", "price": 1.0, "quantity": 0}]

    def product(self, pid):
        return next(p for p in self.products() if p["id"] == pid)

    def create_order(self, pid, qty):
        self.orders.append((pid, qty))
        return {"id": 11, "link": None}

    def wait_completed(self, oid):
        return "completed"

    def order_link(self, oid):
        return "http://x/y"

    def download(self, link, oid):
        path = os.path.join(tmp, "order.txt")
        open(path, "w").write("login1:pass1\nlogin2:pass2:extra\n")
        return path


fake = FakeDS()
shop.api = lambda: fake
offers = shop.offers(10)
assert [o["id"] for o in offers] == [1, 3], "без спамблока, ручной выдачи и товаров не в наличии; по возрастанию цены"
try:
    shop.buy(3, 1)
    raise AssertionError("80 больше потолка 100? потолок по умолчанию 100, баланс 50")
except shop.ShopError as e:
    assert "не хватает баланса" in str(e)
shop.MAX_SPEND = 10
try:
    shop.buy(1, 1)
    raise AssertionError("17.4 больше потолка 10")
except shop.ShopError as e:
    assert "потолка" in str(e)
shop.MAX_SPEND = 100
assert fake.orders == [], "заказ не создавался, пока проверки не пройдены"
import accounts.darkstore as ds_mod  # noqa: E402
import accounts.darkstore_buyer as db_mod  # noqa: E402
db_mod.import_vk = lambda path, oid, name, unit: [store.add_purchased(l.split(":")[0], l.split(":")[1], None, str(oid), name, unit)
                                                  for l in open(path).read().split()] and 2
res = shop.buy(1, 1)
assert res["saved"] == 2 and res["order"] == 11 and fake.orders == [(1, 1)]
assert len(store.list_purchased(only_without_token=True)) == 2
tg.sent.clear()
run(panel.on_callback(cb(admin, "ac:buyer")))
assert "login1:pass1" in tg.last()
print("ALL OK")
