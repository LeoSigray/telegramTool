"""Проверки Mini App без Telegram и без сервера: подпись initData, баланс, канал, нажатия, диалоги, отправка.

Запуск: python tests/check_miniapp.py (проверки без Telegram и без сервера; печатают ALL OK).
Скрипты подменяют части модулей, поэтому pytest их не собирает: у файлов нет префикса test_.
"""
import asyncio
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import tempfile
import time
from urllib.parse import urlencode

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from api import miniapp_bot as bot  # noqa: E402
from api import miniapp_channel as chan  # noqa: E402
from api import miniapp_send as send  # noqa: E402
from data import inbox  # noqa: E402
from data import miniapp_store as store  # noqa: E402

TOKEN = "123456:TEST-token"


def sign(fields: dict, token: str = TOKEN) -> str:
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    h = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": h})


# 1. initData
user = {"id": 777, "first_name": "Иван", "username": "ivan"}
fields = {"auth_date": str(int(time.time())), "query_id": "AAA", "user": json.dumps(user, ensure_ascii=False)}
ok = bot.validate_init_data(sign(fields), token=TOKEN)
assert ok["user"]["id"] == 777, ok
for bad, why in [(sign(fields, "other:token"), "чужой токен"),
                 (sign({**fields, "auth_date": str(int(time.time()) - 90000)}), "устаревшая"),
                 (sign(fields).replace("ivan", "evil"), "подмена")]:
    try:
        bot.validate_init_data(bad, token=TOKEN)
        raise AssertionError("должно было упасть: " + why)
    except bot.InitDataError:
        pass
print("initData: ok")

# 2. канал: разбор ссылки
for raw, want in [("@neuroclub_ai", "neuroclub_ai"), ("t.me/neuroclub_ai", "neuroclub_ai"),
                  ("https://t.me/neuroclub_ai?start=1", "neuroclub_ai"), ("https://t.me/s/neuroclub_ai", "neuroclub_ai"),
                  ("neuroclub_ai", "neuroclub_ai")]:
    assert chan.normalize(raw) == want, raw
for raw in ["", "ab", "https://example.com/x", "@1abc", "привет", "t.me/joinchat/AAAA-BBBB", "https://t.me/+AbCdEf"]:
    try:
        chan.normalize(raw)
        raise AssertionError("должно было не подойти: " + raw)
    except chan.ChannelError:
        pass
assert "Здравствуйте" in chan._fallback_text({"channel": "x", "title": "НейроМагия | Евгений", "about": "✅ Делюсь опытом AI в бизнесе"})
assert chan._sanitize("<think>x</think>**Привет**\u2011мир") == "Привет-мир"
print("channel parsing: ok")

# 3. хранилище на временной базе
tmp = tempfile.mkdtemp()
dbp = os.path.join(tmp, "t.db")
store.DB_PATH = dbp
inbox.DB_PATH = dbp
inbox.init_inbox()
with sqlite3.connect(dbp) as c:
    c.executescript("""
      CREATE TABLE IF NOT EXISTS sends (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, account TEXT, target TEXT, peer_id TEXT,
        niche TEXT, template_id INTEGER, channel TEXT, status TEXT, error TEXT, sent_at TEXT, replied_at TEXT,
        reply_text TEXT, subscribed_at TEXT, outcome TEXT DEFAULT 'pending');
    """)
store.init_store()
store.init_store()  # повторно — без ошибок

store.upsert_user(1, username="a", first_name="A", last_name=None)
store.upsert_user(2, username="b", first_name="B", last_name=None)
assert store.claim_gift(1, 10) is True
assert store.claim_gift(1, 10) is False, "подарок второй раз"
assert store.get_user(1)["balance"] == 10
assert store.reserve(1, 11) is False, "в минус нельзя"
assert store.get_user(1)["channel"] is None

# канал: building → ready, правка текста
store.set_channel_building(1, "mychan")
u = store.get_user(1)
assert (u["channel"], u["channel_status"], u["offer_text"]) == ("mychan", "building", None)
assert [b["tg_id"] for b in store.building_channels()] == [1]
store.set_channel_ready(1, "Здравствуйте! Текст")
assert store.get_user(1)["channel_status"] == "ready" and not store.building_channels()
store.set_offer_text(1, "Другой текст рассылки")
assert store.get_user(1)["offer_text"] == "Другой текст рассылки"
store.set_channel_failed(2, "не нашли")
assert store.get_user(2)["channel_status"] == "failed"

# нажатия: списание, очередь, возврат при ошибке, идемпотентность
assert store.reserve(1, 1) and store.get_user(1)["balance"] == 9
t1 = store.enqueue_tap(1)
assert store.pending_taps(1) == 1 and store.sent_total(1) == 0
tap = store.claim_next_tap()
assert tap["id"] == t1 and store.claim_next_tap() is None, "одно нажатие берём один раз"
store.release_tap(t1)
assert store.claim_next_tap()["id"] == t1
assert store.recover_taps() == 1, "после перезапуска «отправляется» возвращается в очередь"
assert store.claim_next_tap()["id"] == t1
assert store.finish_tap(t1, status="failed", error="некому") is not None
assert store.get_user(1)["balance"] == 10, "ошибка возвращает сообщение"
assert store.finish_tap(t1, status="failed") is None, "повтор не возвращает второй раз"
assert store.pending_taps(1) == 0

# 4. отправка: поддельный клиент и аккаунт
class Ent:
    id = 100
    username = "anna"
    first_name = "Анна"
    last_name = "К."


class Sent:
    id = 555

    class date:
        @staticmethod
        def isoformat():
            return "2026-10-12T10:00:00+00:00"


class FakeClient:
    def __init__(self):
        self.sent = []
        self.err = None

    async def get_entity(self, x):
        self.last = x
        return Ent()

    async def send_message(self, ent, text):
        if self.err:
            raise self.err
        self.sent.append((ent.id, text))
        return Sent()


fake = FakeClient()
send.pool.clients.clear()
send.pool.clients["acc1"] = fake
send.health.is_available = lambda a: True
send.an.sends_last_hours = lambda a, h=24: 0
send.an.is_suppressed = lambda k: False
send.an.already_contacted = lambda names: {n for n in names if n == "banned"}
sends_rows = []


def fake_record(**kw):
    sends_rows.append(kw)
    with sqlite3.connect(dbp) as c:
        cur = c.execute("INSERT INTO sends (job_id, account, target, peer_id, status, error, sent_at) VALUES (?,?,?,?,?,?,'t')",
                        (kw["job_id"], kw["account"], kw["target"], kw["peer_id"], kw["status"], kw.get("error")))
        return cur.lastrowid


send.an.record_send = fake_record
send.TAP_GAP_SEC = 0


def run(coro):
    return asyncio.run(coro)


def lead_status(tg):
    with sqlite3.connect(dbp) as c:
        return {r[0]: r[1] for r in c.execute("SELECT username, status FROM mini_leads WHERE tg_id=?", (tg,))}


# клиенты покупателя 1 (их нашёл умный парсинг): горячие первыми, уже писавшим не пишем
assert store.add_leads(1, [
    {"username": "@Banned", "tier": "A", "interest": 99},
    {"username": "aaa", "tier": "A", "interest": 50, "name": "Аня"},
    {"username": "bbb", "tier": "B", "interest": 80},
    {"username": "ccc", "tier": "C", "interest": 10},
    {"username": "", "tier": "A"},
]) == 4
assert store.add_leads(1, [{"username": "AAA"}]) == 0, "дубль не добавляется"
assert store.add_leads(2, [{"username": "other_buyer_lead", "tier": "A", "interest": 1}]) == 1
assert store.leads_new(1) == 4 and store.leads_new(2) == 1
assert store.next_lead(1)["username"] == "banned", "сначала уровень A, внутри него больший интерес"

store.reserve(1, 1)
tap_a = store.enqueue_tap(1)
store.claim_next_tap()
assert run(send._process({"id": tap_a, "tg_id": 1})) is False
assert fake.sent == [(100, "Другой текст рассылки")], fake.sent
assert sends_rows[-1]["job_id"] == "tap-1" and sends_rows[-1]["target"] == "@aaa"
assert store.sent_total(1) == 1
assert lead_status(1) == {"banned": "failed", "aaa": "sent", "bbb": "new", "ccc": "new"}, lead_status(1)
assert store.leads_new(2) == 1, "клиенты другого покупателя не тронуты"

# диалог появился у покупателя сразу после отправки, у другого покупателя его нет
convs = store.user_conversations(1)
assert len(convs) == 1 and convs[0]["username"] == "anna" and convs[0]["replied"] == 0, convs
assert convs[0]["last_message_from_me"] == 1
assert store.user_conversations(2) == []
conv_id = convs[0]["id"]
assert store.conversation_owner(conv_id) == 1 and store.conversation_owner(999) is None
msgs = inbox.get_messages(conv_id)
assert [(m["direction"], m["text"], m["tg_id"]) for m in msgs] == [("out", "Другой текст рассылки", 555)]

# ответ человека: диалог получает метку «ответил» и счётчик непрочитанного
inbox.record_inbound("100", "acc1", tg_id=9, text="Сколько стоит?", sent_at="2026-10-12T11:00:00+00:00")
assert store.user_conversations(1)[0]["replied"] == 1 and store.unread_total(1) == 1

# человек закрыл ЛС: пробуем следующего клиента, нажатие не теряется
from telethon.errors import UserPrivacyRestrictedError  # noqa: E402
calls = {"n": 0}


class PickyClient(FakeClient):
    async def send_message(self, ent, text):
        calls["n"] += 1
        if calls["n"] == 1:
            raise UserPrivacyRestrictedError(request=None)
        return await super().send_message(ent, text)


send.pool.clients["acc1"] = PickyClient()
store.reserve(1, 1)
t2 = store.enqueue_tap(1)
store.claim_next_tap()
assert run(send._process({"id": t2, "tg_id": 1})) is False
assert store.sent_total(1) == 2 and calls["n"] == 2
assert lead_status(1)["bbb"] == "failed" and lead_status(1)["ccc"] == "sent", lead_status(1)
assert any(r["status"] == "failed" for r in sends_rows)

# клиенты кончились: нажатие не уходит, сообщение возвращается
send.pool.clients["acc1"] = fake
bal0 = store.get_user(1)["balance"]
store.reserve(1, 1)
t_empty = store.enqueue_tap(1)
store.claim_next_tap()
assert run(send._process({"id": t_empty, "tg_id": 1})) is False
assert store.get_user(1)["balance"] == bal0 and store.leads_new(1) == 0

# сбой аккаунта: нажатие остаётся в очереди, клиент не сгорает
from telethon.errors import PeerFloodError  # noqa: E402
store.add_leads(1, [{"username": "ddd", "tier": "A", "interest": 5}])
bad = FakeClient()
bad.err = PeerFloodError(request=None)
send.pool.clients["acc1"] = bad
flagged = []
send.health.on_error = lambda a, kind, d=None: flagged.append((a, kind))
store.reserve(1, 1)
t3 = store.enqueue_tap(1)
store.claim_next_tap()
bal_before = store.get_user(1)["balance"]
assert run(send._process({"id": t3, "tg_id": 1})) is True
assert flagged == [("acc1", "peer_flood")] and store.pending_taps(1) == 1
assert store.get_user(1)["balance"] == bal_before and store.leads_new(1) == 1

# нет свободного аккаунта: ждём; слишком долго — возврат
send.pool.clients.clear()
store.claim_next_tap()
assert run(send._process({"id": t3, "tg_id": 1})) is True
assert store.expired_taps(0.0) and not store.expired_taps(3600)
store.finish_tap(t3, status="failed", error="нет свободного аккаунта")
assert store.get_user(1)["balance"] == bal_before + 1

# нажатие без канала не уходит и возвращает сообщение
store.reserve(2, 0)
store.claim_gift(2, 5)
store.reserve(2, 1)
t4 = store.enqueue_tap(2)
store.claim_next_tap()
assert run(send._process({"id": t4, "tg_id": 2})) is False
assert store.get_user(2)["balance"] == 5 and store.sent_total(2) == 0
print("taps and sending: ok")

# поиск клиентов: очередь, прерванный поиск, повторный поиск
store.parse_enqueue(1)
assert store.get_user(1)["parse_status"] == "queued"
store.parse_enqueue(1)                                  # повторный вызов ничего не ломает
job = store.parse_claim()
assert job["tg_id"] == 1 and store.get_user(1)["parse_status"] == "running" and store.parse_claim() is None
store.parse_enqueue(1)                                  # идущий поиск не сбрасывается
assert store.get_user(1)["parse_status"] == "running"
assert store.parse_recover() == 1 and store.get_user(1)["parse_status"] == "queued"
store.parse_claim()
store.parse_finish(1, "done")
u = store.get_user(1)
assert u["parse_status"] == "done" and u["parse_runs"] == 1 and u["parse_finished_at"]
# клиентов почти нет, баланс есть, прошло время: ставим повторный поиск
assert store.parse_topup_candidates(5, 0, 12) == [1]
assert store.parse_topup_candidates(5, 3600, 12) == [], "рано: поиск закончился только что"
assert store.parse_topup_candidates(5, 0, 1) == [], "лимит поисков исчерпан"
store.add_leads(1, [{"username": f"u{i}"} for i in range(6)])
assert store.parse_topup_candidates(5, 0, 12) == [], "клиентов достаточно"
# другой канал или ниша: старые клиенты и счётчик поисков сбрасываются
store.set_channel_building(1, "mychan", "topic")
assert store.leads_new(1) == 0 and store.get_user(1)["parse_status"] is None and store.get_user(1)["parse_runs"] == 0
assert store.get_user(1)["channel"] == "mychan"
store.add_leads(1, [{"username": "keepme"}])
store.set_channel_building(1, "mychan", "topic")        # тот же ввод (перезапуск сервиса): ничего не сбрасываем
assert store.leads_new(1) == 1
print("lead search: ok")

# платежи
store.create_payment(1, pack_id="p500", messages=500, amount=800, currency="XTR", method="stars", payload="pay1")
assert store.has_paid(1) is False
bal = store.get_user(1)["balance"]
paid = store.mark_paid("pay1", charge_id="ch")
assert paid and store.get_user(1)["balance"] == bal + 500
assert store.mark_paid("pay1", charge_id="ch") is None, "повтор вебхука не начисляет второй раз"
assert store.has_paid(1) is True and len(store.list_payments(1)) == 1
print("payments: ok")
print("ALL OK")
