"""
seed_demo_dashboards.py — демо-данные для дашбордов аналитики.

Дашборды сами по себе полностью динамические — читают data/database.db вживую
(опрос раз в 5–20 сек). Этот скрипт лишь кладёт в ту же БД синтетические строки,
чтобы было на что смотреть до запуска реальной рассылки.

Все демо-строки помечены видимо:
  • аккаунты   — имена demo_lzt00, demo_td30, demo_ses36
  • ниши       — «демо·стоматологии» и т.п.
  • каналы     — @demo_dental / @demo_fitness / @demo_law
  • продавцы   — «демо·shop_alpha» и т.п.
  • отправки   — job_id = 'demo-seed'

Режимы:
    python seed_demo_dashboards.py           # снести старое демо и залить свежее
    python seed_demo_dashboards.py --clear   # ТОЛЬКО снести демо (перед боевым запуском)

Реальные данные не трогаются ни в одном режиме — удаляются только помеченные строки.
"""
import argparse
import random
import sqlite3
from datetime import datetime, timedelta, timezone

from data.db import DB_PATH, init_db
from data.analytics import init_analytics

DEMO_JOB = "demo-seed"
NICHES = ["демо·стоматологии", "демо·фитнес", "демо·юристы"]
CHANNELS = ["@demo_dental", "@demo_fitness", "@demo_law"]
SELLERS = ["демо·shop_alpha", "демо·shop_beta", "демо·cheap_bob", "демо·prem_kate", "демо·random_lot"]


def purge_demo(c: sqlite3.Connection) -> dict:
    """Удаляет только помеченные демо-строки. Безопасно на боевой базе."""
    n = {}
    n["sends"] = c.execute("DELETE FROM sends WHERE job_id = ?", (DEMO_JOB,)).rowcount
    n["account_meta"] = c.execute(
        r"DELETE FROM account_meta WHERE name LIKE 'demo\_%' ESCAPE '\'").rowcount
    n["account_events"] = c.execute(
        r"DELETE FROM account_events WHERE account LIKE 'demo\_%' ESCAPE '\'").rowcount
    n["channel_members"] = c.execute(
        r"DELETE FROM channel_members WHERE channel LIKE '@demo\_%' ESCAPE '\'").rowcount
    n["templates"] = c.execute("DELETE FROM templates WHERE niche LIKE 'демо·%'").rowcount
    n["suppression"] = c.execute(
        r"DELETE FROM suppression WHERE key LIKE 'demo\_%' ESCAPE '\'"
        r" OR key GLOB '9[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]'").rowcount
    return n


def seed(c: sqlite3.Connection) -> dict:
    random.seed(42)
    now = datetime.now(timezone.utc)
    iso = lambda dt: dt.astimezone(timezone.utc).isoformat()

    seller_q = {
        "демо·shop_alpha": (1.4, 1.3), "демо·shop_beta": (1.0, 1.0),
        "демо·cheap_bob": (0.45, 0.6), "демо·prem_kate": (1.6, 1.5),
        "демо·random_lot": (0.8, 0.85),
    }

    accounts = []
    for i in range(40):
        if i < 30:
            source, seller = "lzt", random.choice(SELLERS)
            cost = round(random.choice([15, 22, 28, 35, 48, 55, 70, 85, 95, 110, 140])
                         + random.uniform(-3, 3), 2)
            name = f"demo_lzt{i:02d}"
        elif i < 36:
            source, seller, cost, name = "tdata", None, 0.0, f"demo_td{i:02d}"
        else:
            source, seller, cost, name = "session", None, 0.0, f"demo_ses{i:02d}"

        age = random.randint(3, 70)
        acquired = now - timedelta(days=age, hours=random.randint(0, 23))
        first_used = acquired + timedelta(days=random.randint(0, 3))
        qlife, qreply = seller_q.get(seller, (0.95, 0.95))
        lifespan = 22 * qlife * (0.6 if cost and cost < 40 else 1.0) * random.uniform(0.75, 1.25)
        dead = age > lifespan and random.random() < 0.85
        status = "dead" if dead else random.choice(["active", "active", "active", "resting"])
        dead_at = iso(acquired + timedelta(days=min(age, lifespan) + random.uniform(-1, 1))) if dead else None

        c.execute(
            "INSERT INTO account_meta (name, source, cost, seller, acquired_at, first_used_at,"
            " status, cap_multiplier, dead_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (name, source, cost, seller, iso(acquired), iso(first_used), status,
             round(random.uniform(0.5, 1.0), 2), dead_at))
        accounts.append(dict(name=name, first_used=first_used, dead_at=dead_at,
                             qreply=qreply, weight=qlife * random.uniform(0.7, 1.3)))

    tpl_text = {
        "A": "Здравствуйте! Помогаем компаниям вашего профиля с потоком клиентов из Telegram. Можно показать пару кейсов?",
        "B": "Добрый день. Вы сейчас как-то ведёте заявки через мессенджеры? Есть идея, которая может добавить 10–15 обращений в неделю.",
        "C": "Привет! Увидел ваш профиль — занимаемся продвижением в нише. Актуально обсудить?",
    }
    tpl_q = {"A": 1.25, "B": 1.5, "C": 0.7}
    tpl_block = {"A": 0.6, "B": 0.5, "C": 1.6}
    pos = ["да, интересно, расскажите", "скиньте кейсы пожалуйста", "сколько стоит?",
           "можно подробнее по срокам", "ок, давайте созвон", "а какие гарантии?"]
    rej = ["нет, спасибо", "не нужно", "не интересно", "спасибо, не надо", "не актуально",
           "не пойдёт", "нам не нужно", "спам, отписался", "не пишите больше", "мне это не надо"]
    neu = ["кто это?", "откуда у вас мой контакт", "я подумаю", "напишите позже"]

    tpl_ids = {}
    for niche in NICHES:
        for variant, text in tpl_text.items():
            cur = c.execute("INSERT INTO templates (niche, variant, text) VALUES (?,?,?)"
                            " ON CONFLICT(niche,variant) DO UPDATE SET text=excluded.text",
                            (niche, variant, text))
            rid = cur.lastrowid or c.execute(
                "SELECT id FROM templates WHERE niche=? AND variant=?", (niche, variant)).fetchone()[0]
            tpl_ids[(niche, variant)] = rid

    names = [a["name"] for a in accounts]
    weights = [a["weight"] for a in accounts]
    by_name = {a["name"]: a for a in accounts}
    for sid in range(3200):
        a = by_name[random.choices(names, weights=weights, k=1)[0]]
        end = datetime.fromisoformat(a["dead_at"]) if a["dead_at"] else now
        if end <= a["first_used"]:
            continue
        sent_at = a["first_used"] + (end - a["first_used"]) * random.random()
        if sent_at > now:
            continue
        niche = random.choice(NICHES)
        variant = random.choices(["A", "B", "C"], weights=[3, 3, 2], k=1)[0]
        tpl_id = tpl_ids[(niche, variant)]
        channel = random.choice(CHANNELS) if random.random() < 0.5 else None
        status = "sent" if random.random() < 0.9 else random.choice(["failed", "skipped"])
        outcome, replied_at, subscribed_at, reply_text = status, None, None, None
        if status == "sent":
            outcome = "no_reply"
            answered_p = 0.16 * a["qreply"] * tpl_q[variant]
            block_p = 0.05 * tpl_block[variant]
            roll = random.random()
            if roll < block_p:
                outcome = "blocked"
            elif roll < block_p + answered_p:
                replied_at = iso(sent_at + timedelta(hours=random.uniform(1, 40)))
                sr = random.random()
                if sr < 0.20:
                    outcome, reply_text = "lead", random.choice(pos)
                elif sr < 0.62:
                    outcome, reply_text = "success", random.choice(pos + neu)
                else:
                    outcome, reply_text = "rejected", random.choice(rej)
            elif (now - sent_at).total_seconds() < 72 * 3600 and random.random() < 0.3:
                outcome = "pending"
            if channel and random.random() < 0.18:
                hrs = random.choice([random.uniform(0, 30)] * 3 + [random.uniform(0, 24 * 20)])
                sub_dt = now - timedelta(hours=hrs)
                if sub_dt > sent_at:
                    subscribed_at = iso(sub_dt)
        c.execute(
            "INSERT INTO sends (job_id, account, target, peer_id, niche, template_id, channel,"
            " status, error, sent_at, replied_at, reply_text, subscribed_at, outcome)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (DEMO_JOB, a["name"], f"@demo_u{sid}", str(900_000_000 + sid),
             niche, tpl_id, channel, status, None,
             iso(sent_at), replied_at, reply_text, subscribed_at, outcome))

    return {
        "accounts": c.execute("SELECT COUNT(*) FROM account_meta WHERE name LIKE 'demo\\_%' ESCAPE '\\'").fetchone()[0],
        "sends": c.execute("SELECT COUNT(*) FROM sends WHERE job_id=?", (DEMO_JOB,)).fetchone()[0],
        "replies": c.execute("SELECT COUNT(*) FROM sends WHERE job_id=? AND reply_text IS NOT NULL", (DEMO_JOB,)).fetchone()[0],
        "templates": c.execute("SELECT COUNT(*) FROM templates WHERE niche LIKE 'демо·%'").fetchone()[0],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--clear", action="store_true", help="только удалить демо-строки, не заливать новые")
    args = ap.parse_args()

    init_db()
    init_analytics()
    c = sqlite3.connect(DB_PATH)
    c.execute("PRAGMA journal_mode=WAL")

    removed = purge_demo(c)
    print("удалено демо-строк:", {k: v for k, v in removed.items() if v})

    if args.clear:
        c.commit()
        c.close()
        print("готово — демо очищено, реальные данные не тронуты")
        return

    stats = seed(c)
    c.commit()
    c.close()
    print(f"OK  {DB_PATH}")
    print(f"демо: аккаунтов={stats['accounts']}  отправок={stats['sends']}  "
          f"ответов_с_текстом={stats['replies']}  шаблонов={stats['templates']}")


if __name__ == "__main__":
    main()
