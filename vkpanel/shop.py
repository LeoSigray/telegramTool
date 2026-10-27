# -*- coding: utf-8 -*-
"""DarkStore для панели: баланс, подбор безопасных товаров VK, покупка с лимитом на сумму. Без интерактива."""
import os

from config import CONFIG
from accounts import darkstore as ds

CATEGORY_VK = 29                          # «ВКонтакте» в каталоге DarkStore
GOOD_GROUPS = (189, 63, 59)               # ручная регистрация, авторег, раскрученные (без ретрива и спамблока)
MAX_SPEND = float(os.getenv("VKPANEL_MAX_SPEND", "100") or 100)    # потолок на одну покупку, в валюте магазина


class ShopError(Exception):
    pass


def api() -> "ds.DarkstoreAPI":
    key = CONFIG.get("DARKSTORE_API_KEY", "")
    if not key:
        raise ShopError("DARKSTORE_API_KEY не задан в .env на сервере")
    return ds.DarkstoreAPI(key)


def balance() -> tuple[float, str]:
    try:
        return api().balance()
    except ds.DarkstoreError as e:
        raise ShopError(str(e)) from e


def short(p: dict) -> dict:
    """Что показывать в панели про товар."""
    attrs = [str(a.get("value") if isinstance(a, dict) else a) for a in (p.get("attributes") or [])]
    return {"id": int(p["id"]), "name": " ".join(str(p.get("name") or "").split())[:70],
            "price": float(p.get("price") or 0), "qty": int(p.get("quantity") or 0),
            "country": ds.product_country(p) or "", "days": ds.product_days(p), "attrs": [a for a in attrs if a][:6],
            "guarantee": p.get("guarantee_time_seconds")}


def offers(limit: int = 8, max_price: float | None = None) -> list[dict]:
    """Самые дешёвые обычные аккаунты VK в наличии: без «ретрива», спамблока и накрутки."""
    a = api()
    items, seen = [], set()
    try:
        for gid in GOOD_GROUPS:
            for p in a.products(category_id=CATEGORY_VK, group_id=gid, price_to=max_price, per_page=100, max_pages=2):
                if p["id"] in seen or ds.is_risky(p) or int(p.get("quantity") or 0) < 1:
                    continue
                if p.get("is_manual_order_delivery") or p.get("manual"):
                    continue                                  # выдача вручную: деньги уйдут, а аккаунт придёт не сразу
                seen.add(p["id"])
                items.append(p)
    except ds.DarkstoreError as e:
        raise ShopError(str(e)) from e
    items.sort(key=lambda p: float(p.get("price") or 0))
    return [short(p) for p in items[:limit]]


def buy(product_id: int, qty: int = 1) -> dict:
    """Покупает qty штук и сохраняет логины/пароли в базу VK. Не больше MAX_SPEND за покупку, не больше баланса."""
    from accounts.darkstore_buyer import import_vk
    a = api()
    try:
        p = a.product(product_id)
        unit = float(p.get("price") or 0)
        total = round(unit * qty, 2)
        bal, cur = a.balance()
        if qty < 1 or qty > int(p.get("quantity") or 0):
            raise ShopError("столько штук нет в наличии")
        if total > MAX_SPEND:
            raise ShopError(f"сумма {total} {cur} больше потолка {MAX_SPEND} (VKPANEL_MAX_SPEND в .env)")
        if total > bal:
            raise ShopError(f"не хватает баланса: нужно {total} {cur}, на счёте {bal}. Пополните его на dark.shopping")
        order = a.create_order(product_id, qty)
        oid = order.get("id")
        if not oid:
            raise ShopError(f"заказ не создан: {order}")
        link = order.get("link")
        if not link:
            st = a.wait_completed(oid)
            if st != "completed":
                return {"order": oid, "saved": 0, "total": total, "currency": cur,
                        "note": f"заказ {oid} ещё не выполнен (статус {st}); деньги зарезервированы, заберите его позже"}
            link = a.order_link(oid)
        if not link:
            return {"order": oid, "saved": 0, "total": total, "currency": cur, "note": "заказ выполнен, но файла нет: скачайте на сайте"}
        path = a.download(link, oid)
        saved = import_vk(path, oid, short(p)["name"], unit)
        return {"order": oid, "saved": saved, "total": total, "currency": cur, "note": ""}
    except ds.DarkstoreError as e:
        raise ShopError(str(e)) from e
