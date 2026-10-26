"""
api/miniapp_crypto.py — оплата USDT в сети TRON (TRC20) на кошелёк владельца.

Как это работает, без бирж и платёжных шлюзов:
  1. Покупатель выбирает пакет и «Криптовалюта». Сервис создаёт заказ с УНИКАЛЬНОЙ суммой:
     цена пакета плюс доли цента (7.24 → 7.241). По сумме перевод и находится среди прочих.
  2. Покупатель переводит ровно эту сумму USDT (TRC20) на адрес из MINIAPP_USDT_ADDRESS.
  3. Фоновая задача раз в 15 секунд (раз в минуту, пока заказов нет) читает входящие
     переводы USDT на этот адрес через TronGrid, сопоставляет сумму с заказом и начисляет сообщения.
     Берутся только подтверждённые переводы.
Перевод, который не подошёл ни к одному заказу (другая сумма, заказ давно истёк), не теряется:
операторам уходит сообщение в бот, начислить можно вручную.

Настройки (.env):
  MINIAPP_USDT_ADDRESS   адрес кошелька (T…); без него способ «Криптовалюта» не показывается;
  TRONGRID_API_KEY       необязательно: ключ TronGrid, чтобы не упираться в общий лимит;
  TRONGRID_URL           необязательно: другой узел TronGrid.
"""
import asyncio
import hashlib
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

import httpx

from data import miniapp_store as store

log = logging.getLogger(__name__)

USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"     # USDT в сети TRON
NETWORK = "TRC20"
PAY_WINDOW_MIN = 120          # сколько действует заказ
LATE_HOURS = 24               # поздний перевод по истёкшему заказу ещё засчитываем
OFFSET_STEP = 1000            # доли USDT, по которым различаются заказы: 0.001
OFFSET_MAX = 199
FAST_SEC, SLOW_SEC = 15, 60

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def valid_address(addr: str) -> bool:
    """Адрес TRON: base58, 25 байт, первый байт 0x41, контрольная сумма двойного SHA-256."""
    if not addr or len(addr) != 34 or addr[0] != "T":
        return False
    n = 0
    for ch in addr:
        i = _B58.find(ch)
        if i < 0:
            return False
        n = n * 58 + i
    raw = n.to_bytes(25, "big")
    return raw[0] == 0x41 and hashlib.sha256(hashlib.sha256(raw[:21]).digest()).digest()[:4] == raw[21:]


def wallet() -> str:
    addr = os.getenv("MINIAPP_USDT_ADDRESS", "").strip()
    return addr if valid_address(addr) else ""


def enabled() -> bool:
    return bool(wallet())


def to_micro(usdt) -> int:
    """USDT → миллионные доли (у токена 6 знаков)."""
    return int((Decimal(str(usdt)) * 1_000_000).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def show(micro: int) -> str:
    """Сумма для покупателя: 7.241"""
    return f"{Decimal(micro) / 1_000_000:.3f}"


def unique_amount(base_micro: int, taken: set[int]) -> int:
    for k in range(1, OFFSET_MAX + 1):
        amount = base_micro + k * OFFSET_STEP
        if amount not in taken:
            return amount
    raise RuntimeError("слишком много неоплаченных заказов")


def _parse(iso: str) -> datetime:
    d = datetime.fromisoformat(iso)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _view(pay: dict) -> dict:
    expires = _parse(pay["created_at"]) + timedelta(minutes=PAY_WINDOW_MIN)
    return {"method": "crypto", "payload": pay["payload"], "address": wallet(), "network": NETWORK,
            "token": "USDT", "amount": show(int(pay["amount"])), "expires_at": expires.isoformat(),
            "status": pay["status"]}


def create_invoice(tg_id: int, pack: dict) -> dict:
    """Заказ на оплату пакета в USDT. Тот же человек и пакет, пока заказ жив, получают тот же заказ."""
    if not enabled():
        raise RuntimeError("оплата USDT не настроена")
    store.crypto_expire(PAY_WINDOW_MIN)
    for pay in store.crypto_orders(("pending",), tg_id=tg_id):
        if pay["pack_id"] == pack["id"]:
            return _view(pay)
    taken = {int(p["amount"]) for p in store.crypto_orders(("pending", "expired"), hours=LATE_HOURS)}
    amount = unique_amount(to_micro(pack["usdt"]), taken)
    payload = "mini-" + secrets.token_hex(12)
    store.create_payment(tg_id, pack_id=pack["id"], messages=int(pack["messages"]), amount=amount,
                         currency="USDT", method="crypto", payload=payload)
    return _view(store.get_payment_by_payload(payload))


def status(tg_id: int, payload: str) -> dict | None:
    pay = store.get_payment_by_payload(payload)
    if pay is None or pay["tg_id"] != tg_id or pay["method"] != "crypto":
        return None
    return {"status": pay["status"], "balance": (store.get_user(tg_id) or {}).get("balance", 0)}


def apply_transfers(transfers: list[dict]) -> list[tuple[str, dict]]:
    """Входящие переводы USDT → начисления. Возвращает [("paid", платёж) | ("unmatched", перевод)].
    Каждый перевод разбирается один раз (по номеру транзакции), повторный опрос ничего не дублирует."""
    out: list[tuple[str, dict]] = []
    me = wallet()
    for t in transfers:
        txid = t.get("transaction_id")
        info = t.get("token_info") or {}
        if not txid or t.get("to") != me or info.get("address") != USDT_CONTRACT or t.get("type") != "Transfer":
            continue
        if store.state_get(f"crypto_tx:{txid}"):
            continue
        store.state_set(f"crypto_tx:{txid}", "1")
        value = int(t.get("value") or 0)
        when = datetime.fromtimestamp(int(t.get("block_timestamp") or 0) / 1000, timezone.utc)
        pay = next((p for p in store.crypto_orders(("pending",)) + store.crypto_orders(("expired",), hours=LATE_HOURS)
                    if int(p["amount"]) == value and _parse(p["created_at"]) <= when), None)
        if pay is None:
            out.append(("unmatched", {**t, "amount": show(value)}))
            continue
        res = store.mark_paid(pay["payload"], charge_id=txid)
        if res:
            out.append(("paid", res))
    return out


async def fetch_transfers(http: httpx.AsyncClient, since_ms: int) -> list[dict]:
    headers = {"TRON-PRO-API-KEY": os.getenv("TRONGRID_API_KEY", "").strip()} if os.getenv("TRONGRID_API_KEY") else {}
    r = await http.get(f"{os.getenv('TRONGRID_URL', 'https://api.trongrid.io').rstrip('/')}/v1/accounts/{wallet()}/transactions/trc20",
                       params={"only_to": "true", "only_confirmed": "true", "limit": 200, "contract_address": USDT_CONTRACT,
                               "min_timestamp": since_ms, "order_by": "block_timestamp,asc"}, headers=headers)
    r.raise_for_status()
    return r.json().get("data") or []


async def loop(notify) -> None:
    """Фоновая проверка переводов. notify(kind, data) — сообщить покупателю или операторам."""
    if not enabled():
        log.info("[miniapp-crypto] MINIAPP_USDT_ADDRESS не задан — оплата USDT выключена")
        return
    since = int(store.state_get("crypto_since") or 0)
    if not since:                       # первый запуск: историю кошелька не трогаем, смотрим с этого момента
        since = int(datetime.now(timezone.utc).timestamp() * 1000)
        store.state_set("crypto_since", str(since))
    failing = 0
    async with httpx.AsyncClient(timeout=20) as http:
        while True:
            await asyncio.sleep(FAST_SEC if store.crypto_orders(("pending",)) else SLOW_SEC)
            try:
                store.crypto_expire(PAY_WINDOW_MIN)
                transfers = await fetch_transfers(http, max(0, since - 5 * 60_000))
                for kind, data in apply_transfers(transfers):
                    await notify(kind, data)
                if transfers:
                    since = max(since, max(int(t.get("block_timestamp") or 0) for t in transfers))
                    store.state_set("crypto_since", str(since))
                failing = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — сеть, лимит TronGrid: попробуем в следующий раз
                failing += 1
                if failing in (1, 10, 100):
                    log.warning("[miniapp-crypto] не удалось прочитать переводы (%d подряд): %s", failing, str(e)[:120])
