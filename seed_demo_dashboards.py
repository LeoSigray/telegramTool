"""
seed_demo_dashboards.py — наполняет data/database.db синтетикой, чтобы посмотреть
новые дашборды (Закупка аккаунтов / Эффективность аккаунтов / Подписки) без
реальных данных.

ВНИМАНИЕ: очищает таблицы sends / account_meta / account_events / channel_members
/ suppression в data/database.db. Не запускай на боевой базе.

    .venv\\Scripts\\python.exe seed_demo_dashboards.py
"""
import os
import random
import sqlite3
from datetime import datetime, timedelta, timezone

from data.db import DB_PATH, init_db
from data.analytics import init_analytics

random.seed(42)
NOW = datetime.now(timezone.utc)
iso = lambda dt: dt.astimezone(timezone.utc).isoformat()

init_db()
init_analytics()
c = sqlite3.connect(DB_PATH)
c.execute("PRAGMA journal_mode=WAL")
for t in ("sends", "account_meta", "account_events", "channel_members", "suppression", "templates"):
    c.execute(f"DELETE FROM {t}")

SELLERS = ["shop_alpha", "shop_beta", "cheap_bob", "prem_kate", "random_lot"]
SELLER_Q = {  # (множитель живучести, множитель reply-rate)
    "shop_alpha": (1.4, 1.3), "shop_beta": (1.0, 1.0), "cheap_bob": (0.45, 0.6),
    "prem_kate": (1.6, 1.5), "random_lot": (0.8, 0.85),
}
CHANNELS = ["@client_dental", "@client_fitness", "@client_law"]

accounts = []
for i in range(40):
    if i < 30:
        source = "lzt"
        seller = random.choice(SELLERS)
        cost = round(random.choice([15, 22, 28, 35, 48, 55, 70, 85, 95, 110, 140])
                     + random.uniform(-3, 3), 2)
    elif i < 36:
        source, seller, cost = "tdata", None, 0.0
    else:
        source, seller, cost = "session", None, 0.0

    age = random.randint(3, 70)
    acquired = NOW - timedelta(days=age, hours=random.randint(0, 23))
    first_used = acquired + timedelta(days=random.randint(0, 3))
    qlife, qreply = SELLER_Q.get(seller, (0.95, 0.95))
    lifespan = 22 * qlife * (0.6 if cost and cost < 40 else 1.0) * random.uniform(0.75, 1.25)
    dead = age > lifespan and random.random() < 0.85
    status = "dead" if dead else random.choice(["active", "active", "active", "resting"])
    dead_at = iso(acquired + timedelta(days=min(age, lifespan) + random.uniform(-1, 1))) if dead else None

    name = f"{source if source != 'lzt' else 'lzt'}{i:02d}"
    c.execute(
        "INSERT INTO account_meta (name, source, cost, seller, acquired_at, first_used_at,"
        " status, cap_multiplier, dead_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (name, source, cost, seller, iso(acquired), iso(first_used), status,
         round(random.uniform(0.5, 1.0), 2), dead_at))
    accounts.append(dict(name=name, first_used=first_used, dead_at=dead_at,
                         qreply=qreply, weight=qlife * random.uniform(0.7, 1.3)))

NICHES = ["стоматологии", "фитнес", "юристы"]

# шаблоны текста по нишам (A/B/C) + «истинная» позитивность варианта
TEMPLATE_TEXT = {
    "A": "Здравствуйте! Помогаем клиникам вашего профиля с потоком пациентов из Telegram. Можно показать пару кейсов?",
    "B": "Добрый день. Вы сейчас как-то ведёте запись через мессенджеры? Есть идея, которая может добавить 10–15 записей в неделю.",
    "C": "Привет! Увидел ваш профиль — занимаемся продвижением в нише. Актуально обсудить?",
}
TEMPLATE_Q = {"A": 1.25, "B": 1.5, "C": 0.7}   # множитель позитивности
TEMPLATE_BLOCK = {"A": 0.6, "B": 0.5, "C": 1.6}  # множитель «бесит → блок/удаление»
POS_REPLIES = ["да, интересно, расскажите", "скиньте кейсы пожалуйста", "сколько стоит?",
               "можно подробнее по срокам", "ок, давайте созвон", "а какие гарантии?"]
REJECT_REPLIES = ["нет, спасибо", "не нужно", "не интересно", "спасибо, не надо",
                  "не актуально", "не пойдёт", "нам не нужно", "спам, отписался",
                  "не пишите больше", "мне это не надо"]
NEU_REPLIES = ["кто это?", "откуда у вас мой контакт", "я подумаю", "напишите позже"]

tpl_ids: dict = {}
for niche in NICHES:
    for variant, text in TEMPLATE_TEXT.items():
        cur = c.execute(
            "INSERT INTO templates (niche, variant, text) VALUES (?,?,?)"
            " ON CONFLICT(niche,variant) DO UPDATE SET text=excluded.text",
            (niche, variant, text))
        rid = cur.lastrowid or c.execute(
            "SELECT id FROM templates WHERE niche=? AND variant=?", (niche, variant)).fetchone()[0]
        tpl_ids[(niche, variant)] = (rid, variant)

names = [a["name"] for a in accounts]
weights = [a["weight"] for a in accounts]
by_name = {a["name"]: a for a in accounts}
for sid in range(3200):
    a = by_name[random.choices(names, weights=weights, k=1)[0]]
    end = datetime.fromisoformat(a["dead_at"]) if a["dead_at"] else NOW
    if end <= a["first_used"]:
        continue
    sent_at = a["first_used"] + (end - a["first_used"]) * random.random()
    if sent_at > NOW:
        continue
    niche = random.choice(NICHES)
    variant = random.choices(["A", "B", "C"], weights=[3, 3, 2], k=1)[0]
    tpl_id, _ = tpl_ids[(niche, variant)]
    channel = random.choice(CHANNELS) if random.random() < 0.5 else None
    status = "sent" if random.random() < 0.9 else random.choice(["failed", "skipped"])
    outcome, replied_at, subscribed_at, reply_text = status, None, None, None
    if status == "sent":
        outcome = "no_reply"
        answered_p = 0.16 * a["qreply"] * TEMPLATE_Q[variant]
        block_p = 0.05 * TEMPLATE_BLOCK[variant]
        roll = random.random()
        if roll < block_p:
            outcome = "blocked"                              # не дошло / удалил чат
        elif roll < block_p + answered_p:
            replied_at = iso(sent_at + timedelta(hours=random.uniform(1, 40)))
            sub_roll = random.random()
            if sub_roll < 0.20:
                outcome, reply_text = "lead", random.choice(POS_REPLIES)
            elif sub_roll < 0.62:
                outcome, reply_text = "success", random.choice(POS_REPLIES + NEU_REPLIES)
            else:
                outcome, reply_text = "rejected", random.choice(REJECT_REPLIES)
        elif (NOW - sent_at).total_seconds() < 72 * 3600 and random.random() < 0.3:
            outcome = "pending"
        if channel and random.random() < 0.18:
            hrs = random.choice([random.uniform(0, 30)] * 3 + [random.uniform(0, 24 * 20)])
            sub_dt = NOW - timedelta(hours=hrs)
            if sub_dt > sent_at:
                subscribed_at = iso(sub_dt)
    c.execute(
        "INSERT INTO sends (job_id, account, target, peer_id, niche, template_id, channel,"
        " status, error, sent_at, replied_at, reply_text, subscribed_at, outcome)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"job{random.randint(1, 8)}", a["name"], f"@u{sid}", str(10_000 + sid),
         niche, tpl_id, channel, status, None,
         iso(sent_at), replied_at, reply_text, subscribed_at, outcome))

c.commit()
row = c.execute(
    "SELECT (SELECT COUNT(*) FROM account_meta), (SELECT COUNT(*) FROM account_meta WHERE status='dead'),"
    " (SELECT COUNT(*) FROM sends), (SELECT COUNT(*) FROM sends WHERE subscribed_at IS NOT NULL),"
    " (SELECT COUNT(*) FROM sends WHERE reply_text IS NOT NULL), (SELECT COUNT(*) FROM templates)").fetchone()
c.close()
print(f"OK  {DB_PATH}")
print(f"аккаунтов={row[0]} (dead={row[1]})  отправок={row[2]}  подписок={row[3]}  "
      f"ответов_с_текстом={row[4]}  шаблонов={row[5]}")
