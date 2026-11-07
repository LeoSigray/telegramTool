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
# человека под это нажатие ещё нет: отправлять нечего, сначала поиск
assert store.claim_next_tap() is None and store.next_search() == 1
store.add_leads(1, [{"username": "gate"}])
assert store.next_search() is None, "человек найден: поиск больше не нужен"
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
with sqlite3.connect(dbp) as c:
    c.execute("DELETE FROM mini_leads WHERE tg_id=1")

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

# поиск человека: только по нажатию и по одному
with sqlite3.connect(dbp) as c:
    c.execute("DELETE FROM mini_leads WHERE tg_id=1")
    c.execute("UPDATE mini_taps SET status='failed' WHERE status IN ('queued','sending')")
assert store.next_search() is None, "без нажатия поиск не запускается"
store.reserve(1, 1)
t5 = store.enqueue_tap(1)
assert store.next_search() == 1 and store.claim_next_tap() is None
assert [(x["id"], x["stage"]) for x in store.transit(1)] == [(t5, "search")], "нажатие без человека: ищем"
store.add_leads(1, [{"username": "transit1"}])
t6 = store.enqueue_tap(1)
assert [x["stage"] for x in store.transit(1)] == ["send", "search"], "старое нажатие отправляется, новое ищет"
with sqlite3.connect(dbp) as c:
    c.execute("DELETE FROM mini_taps WHERE id=?", (t6,))
    c.execute("DELETE FROM mini_leads WHERE username='transit1'")
store.parse_set(1, "running")
assert store.get_user(1)["parse_status"] == "running"
assert store.parse_recover() == 1 and store.get_user(1)["parse_status"] == "failed"
store.parse_set(1, "done")
assert store.get_user(1)["parse_runs"] >= 1 and store.get_user(1)["parse_finished_at"]
assert store.oldest_waiting_tap(1)["id"] == t5
store.finish_tap(t5, status="failed", error="не нашли подходящего человека")
assert store.last_failed_tap(1)["error"] == "не нашли подходящего человека"
assert store.next_search() is None
assert store.known_leads(1) == set()
store.add_leads(1, [{"username": "Known1"}])
assert store.known_leads(1) == {"known1"}
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

# ── ослепший аккаунт: @имена не находятся ───────────────────────────────────
# Ограниченный аккаунт получает «No user has … as username» на живые имена. Раньше
# это считалось «человека нет», и лиды списывались один за другим: на сервере так
# сгорели все найденные люди подряд, а в мини-аппе показывалось «не нашлось».
blind_calls, good_calls = [], []


class BlindClient:
    """Ничего не находит — как аккаунт под тихим ограничением Telegram."""

    def __init__(self):
        self.sent = []

    async def get_entity(self, x):
        blind_calls.append(x)
        raise ValueError('No user has "someone" as username')

    async def send_message(self, ent, text):
        raise AssertionError("ослепший аккаунт не должен дойти до отправки")


class SeeingClient(FakeClient):
    async def get_entity(self, x):
        good_calls.append(x)
        return Ent()


blind, seeing = BlindClient(), SeeingClient()
send.pool.clients.clear()
send.pool.clients["blind_acc"] = blind
send.pool.clients["good_acc"] = seeing
rested = []
send.health.on_error = lambda a, kind, detail=None: rested.append((a, kind))
send._resolve_fails.clear()

store.upsert_user(77, username="z", first_name="Z", last_name=None)
store.set_channel_building(77, "shoes", "channel")
store.set_channel_ready(77, "Привет")
store.claim_gift(77, 5)
store.add_leads(77, [{"username": "live_person", "tier": "A", "interest": 9}])
t = store.enqueue_tap(77)
store.claim_next_tap()

assert run(send._process({"id": t, "tg_id": 77})) is False
# лид не потерян: его подобрал второй аккаунт и сообщение ушло
assert seeing.sent == [(100, "Привет")], f"зрячий аккаунт должен был отправить: {seeing.sent}"
assert blind_calls, "ослепший аккаунт обязан был попробовать первым"
assert lead_status(77) == {"live_person": "sent"}, lead_status(77)
print("blind account fallback: ok")

# три промаха подряд — и аккаунт уходит отдыхать, а не продолжает жечь лиды
send._resolve_fails.clear(); rested.clear()
for _ in range(3):
    await_res = run(send._send_one("blind_acc", blind, {"id": 0, "username": "x"}, "t", "j"))
assert ("blind_acc", "resolve_blocked") in rested, f"аккаунт должен уйти отдыхать: {rested}"
print("blind account rested: ok")

# ── отмена ожидающего нажатия: попытку возвращает покупатель, а не таймер ──
store.upsert_user(91, username="c", first_name="C", last_name=None)
store.set_channel_building(91, "shoes", "channel")
store.set_channel_ready(91, "Привет")
store.claim_gift(91, 5)
bal0 = store.get_user(91)["balance"]

store.reserve(91, 1)
tap_id = store.enqueue_tap(91)
assert store.get_user(91)["balance"] == bal0 - 1, "нажатие списывает сообщение"

# пока идёт обычный поиск человека — кнопки отмены нет, это штатная работа
tr = store.transit(91)
assert len(tr) == 1 and tr[0]["can_cancel"] is False, f"во время поиска отмену не предлагаем: {tr}"

# а вот когда человек найден и воркер уткнулся в пустой пул — отмена появляется.
# Повторяем путь воркера: лид есть, нажатие взято в работу, аккаунта не нашлось.
store.add_leads(91, [{"username": "waiting_person", "tier": "A", "interest": 9}])
assert store.claim_next_tap()["id"] == tap_id
store.release_tap(tap_id, stalled=True)
tr = store.transit(91)
assert tr[0]["can_cancel"] is True, f"застрявшее нажатие можно забрать назад: {tr}"
assert "waiting_s" in tr[0], "фронту полезно показать, сколько ждём"

assert store.cancel_tap(tap_id, 91) is True, "своё ожидающее нажатие отменяется"
assert store.get_user(91)["balance"] == bal0, "попытка вернулась на баланс"
assert store.transit(91) == [], "отменённое нажатие исчезло из «в пути»"
assert store.cancel_tap(tap_id, 91) is False, "повторная отмена ничего не начисляет"
assert store.get_user(91)["balance"] == bal0, "баланс не раздувается от повторов"
print("cancel pending tap: ok")

# чужое нажатие отменить нельзя — даже застрявшее
store.reserve(91, 1)
mine = store.enqueue_tap(91)
store.add_leads(91, [{"username": "p2", "tier": "A", "interest": 9}])
assert store.claim_next_tap()["id"] == mine
store.release_tap(mine, stalled=True)
assert store.cancel_tap(mine, 777) is False, "чужое нажатие недоступно"
assert store.cancel_tap(mine, 91) is True

# начатую отправку отменять поздно: сообщение могло уйти в Telegram.
# Воркер берёт нажатие только когда человек уже найден — поэтому заводим лида.
store.add_leads(91, [{"username": "ready_person", "tier": "A", "interest": 9}])
store.reserve(91, 1)
busy = store.enqueue_tap(91)
got = store.claim_next_tap()
assert got and got["id"] == busy, f"воркер должен был взять именно это нажатие: {got}"
assert store.cancel_tap(busy, 91) is False, "отправляющееся нажатие не отменяется"
assert store.transit(91)[0]["can_cancel"] is False, "фронт не должен предлагать отмену"
# соседнее нажатие в том же списке: оно ещё ищет человека — кнопки у него быть не должно
store.reserve(91, 1)
fresh_tap = store.enqueue_tap(91)
rows = {x["id"]: x for x in store.transit(91)}
assert rows[fresh_tap]["can_cancel"] is False, "ищущее нажатие отменять не предлагаем"
assert store.cancel_tap(fresh_tap, 91) is False, "сервер тоже не даст отменить ищущее"
print("cancel only when stalled: ok")

print("cancel guards: ok")




# ── Воронка покупателя: нажатия, проба, покупки, время, сессии ──────────────
import json as _json                                        # noqa: E402
import time as _time                                        # noqa: E402

PACK_IDS = ["p200", "p500", "p1000"]
store.upsert_user(55, username="f", first_name="F", last_name=None)
store.set_channel_building(55, "shop", "channel")
store.set_channel_ready(55, "Привет")
store.claim_gift(55, 10)

# три нажатия до любой покупки — всё это бесплатная проба
for _ in range(3):
    store.reserve(55, 1)
    store.enqueue_tap(55)
f = store.funnel(55, PACK_IDS)
assert f["taps_total"] == 3, f
assert f["free_spent"] == 3, f"до покупки всё бесплатное: {f}"
assert f["first_purchase"] == 0 and f["purchases_count"] == 0, f
assert f["packs"] == [0, 0, 0], f
print("funnel: бесплатная проба считается: ok")

# покупка пакета p500 дважды и p200 один раз
for i, (pack, msgs) in enumerate((("p500", 500), ("p500", 500), ("p200", 200))):
    pl = f"test-pay-{i}"
    store.create_payment(55, pack_id=pack, messages=msgs, amount=1, currency="XTR",
                         method="stars", payload=pl)
    store.mark_paid(pl, charge_id="ch")
f = store.funnel(55, PACK_IDS)
assert f["first_purchase"] == 1, "после оплаты отметка первой покупки обязана встать"
assert f["purchases_count"] == 3, f
assert f["packs"] == [1, 2, 0], f"счётчики по пакетам в порядке p200,p500,p1000: {f['packs']}"
assert f["first_purchase_at"], "время первой покупки должно быть записано"
print("funnel: покупки и массив пакетов: ok")

# нажатия после покупки бесплатными уже не считаются
store.reserve(55, 1)
store.enqueue_tap(55)
f = store.funnel(55, PACK_IDS)
assert f["taps_total"] == 4 and f["free_spent"] == 3, f"после оплаты проба не растёт: {f}"
print("funnel: после покупки проба заморожена: ok")

# время от нажатия до отправки и отметка первого сообщения
store.add_leads(55, [{"username": "p_x", "tier": "A", "interest": 9}])
store.reserve(55, 1)
t_id = store.enqueue_tap(55)
store.claim_next_tap()
store.finish_tap(t_id, status="sent", target="@p_x", account="acc")
f = store.funnel(55, PACK_IDS)
assert f["first_sent_at"], "первое дошедшее сообщение должно быть отмечено"
assert f["avg_send_s"] is not None and f["avg_send_s"] >= 0, f
print("funnel: время до отправки пишется: ok")

# сессия: открыли, пожили, закрыли
store.session_open(55)
_time.sleep(0.2)
store.session_ping(55)
store.session_close(55)
f = store.funnel(55, PACK_IDS)
assert f["sessions_count"] == 1, f
assert f["avg_session_s"] is not None and f["avg_session_s"] >= 0, f
print("funnel: сессия закрывается и усредняется: ok")

# брошенная сессия закрывается уборщиком по последнему пингу, а не «до сейчас»
store.session_open(55)
with store._conn() as _c:
    _c.execute("UPDATE mini_users SET session_open_at=?, session_ping_at=? WHERE tg_id=55",
               ("2020-01-01T00:00:00+00:00", "2020-01-01T00:00:30+00:00"))
assert store.close_stale_sessions() >= 1, "молча брошенная сессия обязана закрыться"
f = store.funnel(55, PACK_IDS)
assert f["sessions_count"] == 2, f
assert f["avg_session_s"] < 60, f"длительность берётся до последнего пинга, а не до сейчас: {f}"
print("funnel: брошенная сессия не раздувает среднее: ok")

# пересчёт из первоисточников даёт те же цифры
before = store.funnel(55, PACK_IDS)
store.recompute_funnel(55)
after = store.funnel(55, PACK_IDS)
for k in ("taps_total", "purchases_count", "packs", "first_purchase"):
    assert before[k] == after[k], f"пересчёт разошёлся по {k}: {before[k]} -> {after[k]}"
print("funnel: пересчёт из таблиц сходится: ok")




# ── Ответы лидов: отклик — главный признак, что лид был живой ───────────────
from data import inbox as _inbox                            # noqa: E402

store.upsert_user(66, username="r", first_name="R", last_name=None)
store.set_channel_building(66, "shop", "channel")
store.set_channel_ready(66, "Привет")
store.claim_gift(66, 10)
store.add_leads(66, [{"username": "talker", "tier": "A", "interest": 9},
                     {"username": "silent", "tier": "B", "interest": 5}])
store.mark_lead(store.next_lead(66)["id"], "sent")
store.mark_lead(store.next_lead(66)["id"], "sent")

f = store.funnel(66, PACK_IDS)
assert f["replied_leads"] == 0 and f["replies_total"] == 0, f
assert f["reply_rate"] == 0.0, f"два отправленных, ответов нет: {f['reply_rate']}"

# первый ответ от болтливого лида
store.note_reply(username="talker", peer_id=1)
f = store.funnel(66, PACK_IDS)
assert f["replied_leads"] == 1 and f["replies_total"] == 1, f
assert f["reply_rate"] == 0.5, f"один из двух ответил: {f['reply_rate']}"

# он написал ещё дважды — реплик больше, а ответивших людей столько же
store.note_reply(username="talker", peer_id=1)
store.note_reply(username="@TALKER", peer_id=1)
f = store.funnel(66, PACK_IDS)
assert f["replied_leads"] == 1, f"один разговорчивый не должен выглядеть как трое: {f}"
assert f["replies_total"] == 3, f
assert f["reply_rate"] == 0.5, f
print("replies: откликнувшиеся и реплики считаются раздельно: ok")

# ответ от чужого или неизвестного имени ничего не ломает
store.note_reply(username="nobody_here", peer_id=2)
store.note_reply(username=None, peer_id=3)
f = store.funnel(66, PACK_IDS)
assert f["replied_leads"] == 1 and f["replies_total"] == 3, f"чужой ответ не наш: {f}"
print("replies: чужие и пустые имена игнорируются: ok")




# ── Проверка доступности сервиса: фронт показывает баннер техработ по ней ────
# Маршрута /api/service не было, фронт получал 404 и бессрочно показывал
# «Ведутся технические работы» при полностью живом сервисе.
import os as _os                                            # noqa: E402
from api import miniapp as _mapp                            # noqa: E402

_os.environ.pop("MINIAPP_MAINTENANCE", None)
store.state_set("maintenance", "0")
assert _mapp.service_state() == {"maintenance": False}, _mapp.service_state()

store.state_set("maintenance", "1")
assert _mapp.service_state()["maintenance"] is True, "флаг в базе включает режим"

store.state_set("maintenance", "0")
_os.environ["MINIAPP_MAINTENANCE"] = "1"
r = _mapp.service_state()
assert r["maintenance"] is True and r["reason"] == "env", f"переменная перекрывает базу: {r}"
_os.environ.pop("MINIAPP_MAINTENANCE", None)
assert _mapp.service_state()["maintenance"] is False
print("service: режим техработ управляется базой и переменной: ok")

print("ALL OK")
