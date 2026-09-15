"""
accounts/backfill.py — восстановление метаданных приобретения аккаунтов.

Аккаунты, заведённые до появления учёта покупки, лежат в account_meta с
source='own', cost=0 и acquired_at = момент первого старта сервера. Здесь мы
достаём что можем из файловой системы:

  • accounts/{name}.txt  — сохраняется LZT-байером; берём source=lzt,
    price/seller/bought_at если он их дописал, иначе mtime файла как дату;
  • sessions/{name}.session — берём min(mtime, первая отправка) как оценку
    даты появления аккаунта; source не трогаем.

Идемпотентно: по умолчанию правит только нетронутые записи (source='own' и
cost=0). force=True — проходит по всем.
"""

import os
from datetime import datetime, timezone

from config import ACCOUNTS_DIR, SESSIONS_DIR
from data import analytics as an


def _mtime_iso(path: str) -> str | None:
    try:
        return datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc).isoformat()
    except OSError:
        return None


def _parse_txt(path: str) -> dict:
    data: dict = {}
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if "=" in line:
                    k, _, v = line.partition("=")
                    data[k.strip()] = v.strip()
    except OSError:
        pass
    return data


def backfill_account_meta(force: bool = False) -> dict:
    changed: list[dict] = []
    skipped = 0

    for meta in an.list_accounts():
        name = meta["name"]
        untouched = (meta.get("source") in (None, "own")) and not (meta.get("cost") or 0)
        if not untouched and not force:
            skipped += 1
            continue

        fields: dict = {}
        estimated_date = False

        txt_path = os.path.join(ACCOUNTS_DIR, f"{name}.txt")
        sess_path = os.path.join(SESSIONS_DIR, f"{name}.session")

        if os.path.isfile(txt_path):
            txt = _parse_txt(txt_path)
            fields["source"] = "lzt"
            price = txt.get("price") or txt.get("rub_price")
            if price:
                try:
                    fields["cost"] = float(price)
                except ValueError:
                    pass
            if txt.get("seller"):
                fields["seller"] = txt["seller"]
            bought_at = txt.get("bought_at")
            if bought_at:
                fields["acquired_at"] = bought_at
            else:
                mt = _mtime_iso(txt_path)
                if mt:
                    fields["acquired_at"] = mt
                    estimated_date = True
        elif os.path.isfile(sess_path):
            candidates = [t for t in (_mtime_iso(sess_path), an.first_send_at(name)) if t]
            if candidates:
                fields["acquired_at"] = min(candidates)
                estimated_date = True

        if not fields:
            skipped += 1
            continue

        # не двигаем acquired_at в будущее относительно уже записанного
        if "acquired_at" in fields and meta.get("acquired_at") and \
                fields["acquired_at"] >= meta["acquired_at"] and not force:
            fields.pop("acquired_at")
        if not fields:
            skipped += 1
            continue

        an.update_account(name, **fields)
        changed.append({"account": name, **fields, "estimated_date": estimated_date})

    return {"ok": True, "changed": changed, "changed_count": len(changed), "skipped": skipped}
