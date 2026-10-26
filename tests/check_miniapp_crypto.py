"""Оплата USDT (TRC20) и цены пакетов: без сети и без Telegram, на временной базе.

Запуск: python tests/check_miniapp_crypto.py (печатает ALL OK).
Скрипты подменяют части модулей, поэтому pytest их не собирает: у файла нет префикса test_.
"""
import asyncio
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

WALLET = "TNazA3VskRtRksK11ZbaQHqifRt46R9Boa"
os.environ.pop("MINIAPP_PACKS", None)
os.environ["MINIAPP_USDT_ADDRESS"] = WALLET

from api import miniapp as mini  # noqa: E402
from api import miniapp_crypto as crypto  # noqa: E402
from data import miniapp_store as store  # noqa: E402

store.DB_PATH = os.path.join(tempfile.mkdtemp(), "t.db")
store.init_store()


def run(coro):
    return asyncio.run(coro)


# цены: около 3 ₽ за сообщение (доллар ≈ 82,85 ₽, звезда ≈ 1,65 ₽, USDT = доллар)
for pk in mini.DEFAULT_PACKS:
    rub_stars = pk["stars"] * 1.65 / pk["messages"]
    rub_usdt = pk["usdt"] * 82.85 / pk["messages"]
    assert 2.9 <= rub_stars <= 3.1 and 2.9 <= rub_usdt <= 3.1, (pk, rub_stars, rub_usdt)

# адрес кошелька: контрольная сумма, длина, алфавит
assert crypto.valid_address(WALLET) and crypto.valid_address("TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t")
for bad in ("", WALLET[:-1], WALLET[:-1] + "c", "T" + WALLET[1:].replace("N", "0"), "0x" + "a" * 40, WALLET.lower()):
    assert not crypto.valid_address(bad), bad
assert crypto.wallet() == WALLET
os.environ["MINIAPP_USDT_ADDRESS"] = WALLET[:-1] + "b"
assert crypto.wallet() == "" and not crypto.enabled(), "с битым адресом оплата выключена"
os.environ["MINIAPP_USDT_ADDRESS"] = WALLET

# способы оплаты: криптовалюта доступна только с настроенным адресом
assert [m for m in mini.visible_methods() if m["id"] == "crypto"][0]["available"] is True
os.environ["MINIAPP_USDT_ADDRESS"] = ""
assert [m for m in mini.visible_methods() if m["id"] == "crypto"][0]["available"] is False
try:
    run(mini.invoice(mini.InvoiceIn(pack_id="p200", method="crypto"), {"tg_id": 1}))
    raise AssertionError("без кошелька заказ не создаётся")
except mini.HTTPException as e:
    assert e.status_code == 400
os.environ["MINIAPP_USDT_ADDRESS"] = WALLET

# суммы
assert crypto.to_micro(7.24) == 7_240_000 and crypto.to_micro("18.10") == 18_100_000 and crypto.show(7_241_000) == "7.241"
assert crypto.unique_amount(7_240_000, {7_241_000}) == 7_242_000

# заказы: у разных людей суммы разные, у одного человека тот же заказ
store.upsert_user(1, username="a", first_name="A", last_name=None)
store.upsert_user(2, username="b", first_name="B", last_name=None)
inv1 = run(mini.invoice(mini.InvoiceIn(pack_id="p200", method="crypto"), {"tg_id": 1}))
inv2 = run(mini.invoice(mini.InvoiceIn(pack_id="p200", method="crypto"), {"tg_id": 2}))
again = run(mini.invoice(mini.InvoiceIn(pack_id="p200", method="crypto"), {"tg_id": 1}))
assert (inv1["amount"], inv2["amount"]) == ("7.241", "7.242") and again["payload"] == inv1["payload"]
assert inv1["address"] == WALLET and inv1["network"] == "TRC20" and inv1["token"] == "USDT"
assert mini.invoice_status(inv1["payload"], {"tg_id": 1}) == {"status": "pending", "balance": 0}
try:
    mini.invoice_status(inv1["payload"], {"tg_id": 2})
    raise AssertionError("чужой заказ смотреть нельзя")
except mini.HTTPException as e:
    assert e.status_code == 404


def transfer(txid, micro, ts=None, to=WALLET, contract=crypto.USDT_CONTRACT):
    ts = ts or int((datetime.now(timezone.utc) + timedelta(seconds=5)).timestamp() * 1000)
    return {"transaction_id": txid, "token_info": {"symbol": "USDT", "address": contract, "decimals": 6},
            "block_timestamp": ts, "from": "TXsender", "to": to, "type": "Transfer", "value": str(micro)}


# перевод: ровно сумма заказа → сообщения начислены; повтор того же перевода ничего не добавляет
res = crypto.apply_transfers([transfer("tx1", 7_241_000)])
assert [k for k, _ in res] == ["paid"] and res[0][1]["tg_id"] == 1 and res[0][1]["messages"] == 200
assert store.get_user(1)["balance"] == 200 and store.get_user(2)["balance"] == 0
assert mini.invoice_status(inv1["payload"], {"tg_id": 1})["status"] == "paid"
assert crypto.apply_transfers([transfer("tx1", 7_241_000)]) == [] and store.get_user(1)["balance"] == 200
assert store.list_payments(1)[0]["currency"] == "USDT" and store.list_payments(1)[0]["amount"] == 7_241_000

# чужие и неподходящие переводы
other = [transfer("tx2", 7_242_000, contract="TXother"), transfer("tx3", 7_242_000, to="TElse"),
         transfer("tx4", 7_000_000)]
res = crypto.apply_transfers(other)
assert [k for k, _ in res] == ["unmatched"] and res[0][1]["amount"] == "7.000" and store.get_user(2)["balance"] == 0, "не тот токен и не тот адрес игнорируются, не та сумма — сообщение операторам"
# перевод, пришедший до создания заказа, заказ не закрывает
old_ts = int((datetime.now(timezone.utc) - timedelta(days=1)).timestamp() * 1000)
assert [k for k, _ in crypto.apply_transfers([transfer("tx5", 7_242_000, ts=old_ts)])] == ["unmatched"]
assert store.get_user(2)["balance"] == 0

# заказ истёк, а человек заплатил позже: перевод засчитывается (в пределах суток)
with sqlite3.connect(store.DB_PATH) as c:
    c.execute("UPDATE mini_payments SET created_at=? WHERE payload=?",
              ((datetime.now(timezone.utc) - timedelta(minutes=crypto.PAY_WINDOW_MIN + 30)).isoformat(), inv2["payload"]))
assert store.crypto_expire(crypto.PAY_WINDOW_MIN) == 1
assert mini.invoice_status(inv2["payload"], {"tg_id": 2})["status"] == "expired"
res = crypto.apply_transfers([transfer("tx6", 7_242_000)])
assert [k for k, _ in res] == ["paid"] and store.get_user(2)["balance"] == 200
# оплаченные заказы сумму освобождают, неоплаченные (в том числе истёкшие за сутки) держат
store.upsert_user(3, username="c", first_name="C", last_name=None)
store.upsert_user(4, username="d", first_name="D", last_name=None)
assert run(mini.invoice(mini.InvoiceIn(pack_id="p200", method="crypto"), {"tg_id": 3}))["amount"] == "7.241"
assert run(mini.invoice(mini.InvoiceIn(pack_id="p200", method="crypto"), {"tg_id": 4}))["amount"] == "7.242"
sent = []


async def fake_send(chat_id, text, *, button=None, query="", keyboard=None):
    sent.append((chat_id, text))


mini.bot.send = fake_send
mini._seen.clear()
run(mini._crypto_notify("paid", {"tg_id": 1, "messages": 200}))
assert sent and sent[-1][0] == 1 and "200" in sent[-1][1], "покупателю — «оплата получена»"
sent.clear()
mini._seen[1] = __import__("time").time()
run(mini._crypto_notify("paid", {"tg_id": 1, "messages": 200}))
assert not sent, "приложение открыто — в бота не дублируем"
os.environ["MINIAPP_OPERATORS"] = "ops_guy"
store.state_set("operator:ops_guy", "555")
run(mini._crypto_notify("unmatched", {"amount": "7.000", "from": "TX", "transaction_id": "abc"}))
assert sent == [(555, sent[0][1])] and "7.000" in sent[0][1] and "abc" in sent[0][1]
print("ALL OK")
