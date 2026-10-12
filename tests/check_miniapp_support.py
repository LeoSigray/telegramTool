"""Поддержка и «что продвигаем»: без Telegram, на временной базе и поддельном боте.

Запуск: python tests/check_miniapp_support.py (проверки без Telegram и без сервера; печатают ALL OK).
Скрипты подменяют части модулей, поэтому pytest их не собирает: у файлов нет префикса test_.
"""
import asyncio
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
os.environ["MINIAPP_OPERATORS"] = "VanLove2048, @other_op"

from api import miniapp as mini  # noqa: E402
from api import miniapp_channel as chan  # noqa: E402
from data import miniapp_store as store  # noqa: E402

store.DB_PATH = os.path.join(tempfile.mkdtemp(), "t.db")
store.init_store()
store.init_store()


def run(coro):
    return asyncio.run(coro)


# ── что продвигаем: канал, ссылка, ниша ────────────────────────────────────────
assert chan.parse("@neuroclub_ai") == ("channel", "neuroclub_ai", True)
assert chan.parse("https://t.me/neuroclub_ai") == ("channel", "neuroclub_ai", True)
assert chan.parse("yoga") == ("channel", "yoga", False), "слово латиницей пробуем как канал, но не строго"
assert chan.parse("маникюр в Москве") == ("topic", "маникюр в Москве", False)
assert chan.parse("  онлайн-школа   английского  ") == ("topic", "онлайн-школа английского", False)
assert chan.parse("x" * 500)[0] == "topic" and len(chan.parse("x " * 500)[1]) <= 300
for raw in ["", "  ", "аб", "@a", "https://t.me/+AbCdEf", "t.me/joinchat/AAAA"]:
    try:
        chan.parse(raw)
        raise AssertionError("не должно подойти: " + repr(raw))
    except chan.ChannelError:
        pass
p = chan._topic_profile("маникюр в Москве")
assert "маникюр в Москве" in chan._prompt(p) and "канал" not in chan._prompt(p).split("Требования")[1].lower().replace("не называй каналы", "")
assert "маникюр в Москве" in chan._fallback_text(p)

store.upsert_user(1, username="ivan", first_name="Иван", last_name="П")
store.upsert_user(2, username="olga", first_name="Ольга", last_name=None)
store.set_channel_building(1, "маникюр в Москве", "topic")
u = store.get_user(1)
assert (u["channel"], u["channel_kind"], u["channel_status"]) == ("маникюр в Москве", "topic", "building")
assert store.building_channels()[0]["channel_kind"] == "topic"
store.set_channel_ready(1, "Текст рассылки готов")
assert mini._channel_view(store.get_user(1))["kind"] == "topic"
assert mini._channel_view(store.get_user(2)) is None

# ── поддержка ────────────────────────────────────────────────────────────────
sent, delivered = [], []


async def fake_send(chat_id, text, *, button=None, query=""):
    sent.append((chat_id, text, button, query))


next_id = {"n": 500}


async def fake_deliver(chat_id, text):
    next_id["n"] += 1
    delivered.append((chat_id, next_id["n"], text))
    return next_id["n"]


mini.bot.send = fake_send
mini.bot.deliver = fake_deliver

# оператора ещё нет: сообщение копится
m1 = store.support_add(1, "in", "Как оплатить картой?")
run(mini._deliver_support())
assert delivered == [] and len(store.support_undelivered()) == 1

# чужой аккаунт с похожим именем оператором не становится
stranger = {"from": {"id": 999, "username": "stranger"}, "chat": {"id": 999}, "text": "/start"}
assert run(mini._handle_operator(stranger)) is False and store.operator_ids() == []

# оператор по имени из MINIAPP_OPERATORS (регистр не важен) открывает бота: запоминаем id, копим доставляем
op_start = {"from": {"id": 777, "username": "vanlove2048"}, "chat": {"id": 777}, "text": "/start"}
assert run(mini._handle_operator(op_start)) is True
assert store.operator_ids() == [777] and store.state_get("operator:vanlove2048") == "777"
assert "подключены как поддержка" in sent[-1][1]
assert len(delivered) == 1 and delivered[0][0] == 777
assert "Иван П" in delivered[0][2] and "@ivan" in delivered[0][2] and "Как оплатить картой?" in delivered[0][2]
assert store.support_undelivered() == []

# дальше аккаунт определяется по id: тот же человек с другим именем всё равно оператор
# а новый аккаунт, занявший имя после первого входа, оператором не становится
hijack = {"from": {"id": 123, "username": "vanlove2048"}, "chat": {"id": 123}, "text": "/start"}
assert run(mini._handle_operator(hijack)) is False

# ответ оператора (Reply на копию) попадает в чат покупателя; покупателю уходит уведомление «Поддержка»
op_msg_id = delivered[0][1]
reply = {"from": {"id": 777, "username": "renamed"}, "chat": {"id": 777}, "text": "Нажмите «Пополнить» в профиле.",
         "reply_to_message": {"message_id": op_msg_id}}
sent.clear()
assert run(mini._handle_operator(reply)) is True
msgs = store.support_messages(1)
assert [(m["direction"], m["text"]) for m in msgs] == [("in", "Как оплатить картой?"), ("out", "Нажмите «Пополнить» в профиле.")]
buyer_note = [x for x in sent if x[0] == 1][0]
assert "Ответ поддержки" in buyer_note[1] and buyer_note[3] == "support=1"
assert "vanlove" not in buyer_note[1].lower() and "operator" not in buyer_note[1].lower()
assert any(x[0] == 777 and "отправлен" in x[1] for x in sent)
assert store.support_unread(1) == 1
view = mini._support_view(store.support_messages(1))
assert view[0]["out"] is True and view[1]["out"] is False, "для покупателя «out» — его собственные сообщения"
assert not any("vanlove" in str(v).lower() for v in view)
store.support_mark_read(1)
assert store.support_unread(1) == 0

# без Reply оператору подсказываем, как отвечать, покупателям ничего не уходит
sent.clear()
assert run(mini._handle_operator({"from": {"id": 777}, "chat": {"id": 777}, "text": "привет"})) is True
assert "Reply" in sent[0][1] and all(x[0] == 777 for x in sent)
# Reply на чужое (не копию клиента) сообщение
sent.clear()
assert run(mini._handle_operator({"from": {"id": 777}, "chat": {"id": 777}, "text": "x",
                                  "reply_to_message": {"message_id": 1}})) is True
assert "Reply" in sent[0][1]

# обычный покупатель: операторская ветка его не трогает
assert run(mini._handle_operator({"from": {"id": 1, "username": "ivan"}, "chat": {"id": 1}, "text": "привет"})) is False

# покупатель пишет ещё раз: копия идёт оператору сразу
m2 = store.support_add(2, "in", "Не пришла оплата")
run(mini._deliver_support())
assert delivered[-1][0] == 777 and "Не пришла оплата" in delivered[-1][2] and "Ольга" in delivered[-1][2]
# у покупателей свои переписки
assert [m["text"] for m in store.support_messages(2)] == ["Не пришла оплата"]
print("ALL OK")
