"""
data/analytics.py — хранилище аналитики рассылок.

Живёт в том же SQLite, что и data/db.py (data/database.db).

Таблицы:
  • templates       — варианты текста по нишам (для A/B)
  • sends           — каждое отправленное сообщение + его исход
  • channel_members — снимок участников целевого канала (для диффа подписок)
  • account_meta    — стоимость аккаунта, возраст, статус (active/resting/dead)
  • account_events  — журнал flood_wait / peer_flood / bans
  • suppression     — стоп-лист: кому больше НИКОГДА не писать

Сейчас в воронке РЕАЛЬНО считаются два сигнала:
  • ответы    — sends.peer_id ← listener матчит входящее по telegram user_id
  • подписки  — sends.channel/subscribed_at ← api/channel_watch сверяет
                участников канала со списком, кому писали

"Отказ" (negative) и "лид" — пока заглушки: отказ ловится грубым поиском
стоп-слов, лид проставляется только вручную из CRM. Их не трогаем и не
усложняем, пока не понадобится.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

from data.db import DB_PATH

# Окно, внутри которого ответ засчитывается как реакция на рассылку.
REPLY_WINDOW_HOURS = 72

# Стоп-слова: если человек так ответил — в стоп-лист, больше не пишем.
OPT_OUT_MARKERS = (
    "не пишите", "не пиши", "отпишись", "отписаться", "отстань", "стоп",
    "спам", "не интересно", "неинтересно", "не интересует", "жалоба",
    "unsubscribe", "stop",
)


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


# ──────────────────────────────────────────────────────────────────────────
#  Схема
# ──────────────────────────────────────────────────────────────────────────

def init_analytics() -> None:
    """Создаёт таблицы аналитики. Безопасно вызывать повторно."""
    with _conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS templates (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                niche      TEXT NOT NULL,
                variant    TEXT NOT NULL,
                text       TEXT NOT NULL,
                active     INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(niche, variant)
            );

            CREATE TABLE IF NOT EXISTS sends (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id        TEXT,
                account       TEXT NOT NULL,
                target        TEXT NOT NULL,
                peer_id       TEXT,
                niche         TEXT,
                template_id   INTEGER,
                channel       TEXT,
                status        TEXT NOT NULL,
                error         TEXT,
                sent_at       TEXT NOT NULL,
                replied_at    TEXT,
                reply_text    TEXT,
                subscribed_at TEXT,
                outcome       TEXT NOT NULL DEFAULT 'pending'
            );
            CREATE INDEX IF NOT EXISTS ix_sends_peer    ON sends(peer_id, outcome);
            CREATE INDEX IF NOT EXISTS ix_sends_sent_at ON sends(sent_at);
            CREATE INDEX IF NOT EXISTS ix_sends_account ON sends(account, sent_at);
            CREATE INDEX IF NOT EXISTS ix_sends_tpl     ON sends(template_id);
            CREATE INDEX IF NOT EXISTS ix_sends_job     ON sends(job_id);

            CREATE TABLE IF NOT EXISTS channel_members (
                channel  TEXT NOT NULL,
                user_id  TEXT NOT NULL,
                seen_at  TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (channel, user_id)
            );

            CREATE TABLE IF NOT EXISTS account_meta (
                name           TEXT PRIMARY KEY,
                source         TEXT NOT NULL DEFAULT 'own',
                cost           REAL NOT NULL DEFAULT 0,
                acquired_at    TEXT NOT NULL DEFAULT (datetime('now')),
                first_used_at  TEXT,
                status         TEXT NOT NULL DEFAULT 'active',
                rest_until     TEXT,
                cap_multiplier REAL NOT NULL DEFAULT 1.0,
                dead_at        TEXT
            );

            CREATE TABLE IF NOT EXISTS account_events (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                account TEXT NOT NULL,
                event   TEXT NOT NULL,
                detail  TEXT,
                ts      TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS ix_events_acc ON account_events(account, ts);

            CREATE TABLE IF NOT EXISTS suppression (
                key        TEXT PRIMARY KEY,
                reason     TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
        """)
        # ALTER TABLE — ДО индекса по channel: на старых базах колонки ещё нет,
        # а CREATE INDEX .. ON sends(channel) упадёт, если её не завести первой.
        _migrate_columns(c)
        c.execute("CREATE INDEX IF NOT EXISTS ix_sends_channel ON sends(channel, peer_id)")


def _migrate_columns(c: sqlite3.Connection) -> None:
    """ALTER TABLE ADD COLUMN для баз, созданных до появления channel/subscribed_at."""
    existing = {r["name"] for r in c.execute("PRAGMA table_info(sends)")}
    if "channel" not in existing:
        c.execute("ALTER TABLE sends ADD COLUMN channel TEXT")
    if "subscribed_at" not in existing:
        c.execute("ALTER TABLE sends ADD COLUMN subscribed_at TEXT")


# ──────────────────────────────────────────────────────────────────────────
#  Шаблоны
# ──────────────────────────────────────────────────────────────────────────

def add_template(niche: str, text: str, variant: str | None = None) -> int:
    """Добавляет вариант текста для ниши. variant авто: A, B, C..."""
    with _conn() as c:
        if variant is None:
            used = {r["variant"] for r in
                    c.execute("SELECT variant FROM templates WHERE niche=?", (niche,))}
            for letter in "ABCDEFGH":
                if letter not in used:
                    variant = letter
                    break
            else:
                variant = f"V{len(used) + 1}"
        cur = c.execute(
            "INSERT INTO templates (niche, variant, text) VALUES (?,?,?) "
            "ON CONFLICT(niche, variant) DO UPDATE SET text=excluded.text, active=1",
            (niche, variant, text),
        )
        if cur.lastrowid:
            return cur.lastrowid
        row = c.execute("SELECT id FROM templates WHERE niche=? AND variant=?",
                        (niche, variant)).fetchone()
        return row["id"]


def get_templates(niche: str, active_only: bool = True) -> list[dict]:
    q = "SELECT * FROM templates WHERE niche=?"
    if active_only:
        q += " AND active=1"
    with _conn() as c:
        return [dict(r) for r in c.execute(q + " ORDER BY variant", (niche,))]


def get_template(template_id: int) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM templates WHERE id=?", (template_id,)).fetchone()
        return dict(r) if r else None


def set_template_active(template_id: int, active: bool) -> None:
    with _conn() as c:
        c.execute("UPDATE templates SET active=? WHERE id=?", (1 if active else 0, template_id))


def list_niches() -> list[str]:
    with _conn() as c:
        return [r["niche"] for r in
                c.execute("SELECT DISTINCT niche FROM templates ORDER BY niche")]


# ──────────────────────────────────────────────────────────────────────────
#  Отправки
# ──────────────────────────────────────────────────────────────────────────

def record_send(*, job_id: str | None, account: str, target: str,
                peer_id: str | None, niche: str | None, template_id: int | None,
                status: str, error: str | None = None, channel: str | None = None) -> int:
    """Пишет факт отправки. status: sent | skipped | failed.

    channel — какой канал этому человеку предлагали (если рассылка ведёт
    на подписку); используется api/channel_watch для матчинга новых участников.
    """
    outcome = "pending" if status == "sent" else status
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO sends (job_id, account, target, peer_id, niche, template_id,"
            " channel, status, error, sent_at, outcome) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, account, target, peer_id, niche, template_id, channel,
             status, error, _now(), outcome),
        )
        if status == "sent":
            c.execute(
                "UPDATE account_meta SET first_used_at = COALESCE(first_used_at, ?) WHERE name=?",
                (_now(), account),
            )
        return cur.lastrowid


def record_reply(peer_id: str, text: str | None, account: str | None = None) -> int | None:
    """
    Матчит входящее сообщение с последней отправкой этому же человеку.
    Возвращает id записи sends или None если это не ответ на рассылку.

    Если текст похож на отказ — добавляет человека в стоп-лист.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=REPLY_WINDOW_HOURS)).isoformat()
    with _conn() as c:
        row = c.execute(
            "SELECT id, target FROM sends WHERE peer_id=? AND outcome='pending'"
            " AND status='sent' AND sent_at >= ? ORDER BY sent_at DESC LIMIT 1",
            (str(peer_id), cutoff),
        ).fetchone()
        if row is None:
            return None

        low = (text or "").lower()
        negative = any(m in low for m in OPT_OUT_MARKERS)
        outcome = "negative" if negative else "replied"

        c.execute(
            "UPDATE sends SET replied_at=?, reply_text=?, outcome=? WHERE id=?",
            (_now(), (text or "")[:2000], outcome, row["id"]),
        )
        if negative:
            c.execute(
                "INSERT INTO suppression (key, reason) VALUES (?,?) "
                "ON CONFLICT(key) DO NOTHING", (str(peer_id), "opt-out в ответе"))
            c.execute(
                "INSERT INTO suppression (key, reason) VALUES (?,?) "
                "ON CONFLICT(key) DO NOTHING", (row["target"].lower().lstrip("@"), "opt-out в ответе"))
        return row["id"]


def mark_outcome(send_id: int, outcome: str) -> bool:
    """Финальный статус из CRM: lead | negative | replied | no_reply."""
    with _conn() as c:
        cur = c.execute("UPDATE sends SET outcome=? WHERE id=?", (outcome, send_id))
        return cur.rowcount > 0


def expire_pending() -> int:
    """Отправки старше окна без ответа → no_reply (это 'неудача' для бандита)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=REPLY_WINDOW_HOURS)).isoformat()
    with _conn() as c:
        cur = c.execute(
            "UPDATE sends SET outcome='no_reply' WHERE outcome='pending'"
            " AND status='sent' AND sent_at < ?", (cutoff,))
        return cur.rowcount


# ──────────────────────────────────────────────────────────────────────────
#  Подписки на канал
# ──────────────────────────────────────────────────────────────────────────
#  Реальный, а не заглушечный сигнал в воронке — наравне с ответами.
#  Живёт отдельно от sends.outcome (reply/negative/no_reply/lead), потому
#  что человек может и ответить, и подписаться — это два независимых факта,
#  а не взаимоисключающие состояния одной машины состояний.

# Окно, в течение которого подписку засчитываем этой рассылке.
# Шире, чем окно ответа — на канал подписываются не сразу, а подумав.
SUBSCRIBE_WINDOW_DAYS = 30


def record_subscription(peer_id: str, channel: str | None = None) -> int | None:
    """
    Матчит нового участника канала с последней отправкой этому человеку.
    Возвращает id записи sends или None если подходящей отправки не было
    (человек подписался сам по себе, не через эту рассылку).
    """
    cutoff = _ago(SUBSCRIBE_WINDOW_DAYS)
    q = ("SELECT id FROM sends WHERE peer_id=? AND status='sent'"
         " AND subscribed_at IS NULL AND sent_at >= ?")
    args: list = [str(peer_id), cutoff]
    if channel:
        q += " AND (channel=? OR channel IS NULL)"
        args.append(channel)
    q += " ORDER BY sent_at DESC LIMIT 1"

    with _conn() as c:
        row = c.execute(q, args).fetchone()
        if row is None:
            return None
        c.execute("UPDATE sends SET subscribed_at=? WHERE id=?", (_now(), row["id"]))
        return row["id"]


def known_channel_members(channel: str) -> set:
    with _conn() as c:
        return {r["user_id"] for r in
                c.execute("SELECT user_id FROM channel_members WHERE channel=?", (channel,))}


def remember_channel_members(channel: str, user_ids: list) -> None:
    if not user_ids:
        return
    now = _now()
    with _conn() as c:
        c.executemany(
            "INSERT INTO channel_members (channel, user_id, seen_at) VALUES (?,?,?) "
            "ON CONFLICT(channel, user_id) DO NOTHING",
            [(channel, str(uid), now) for uid in user_ids],
        )


def list_recent_channels(days: float = 30) -> list:
    with _conn() as c:
        return [r["channel"] for r in c.execute(
            "SELECT DISTINCT channel FROM sends WHERE channel IS NOT NULL AND sent_at >= ?",
            (_ago(days),))]


# ──────────────────────────────────────────────────────────────────────────
#  Стоп-лист и дедупликация
# ──────────────────────────────────────────────────────────────────────────

def suppress(key: str, reason: str = "manual") -> None:
    with _conn() as c:
        c.execute("INSERT INTO suppression (key, reason) VALUES (?,?) "
                  "ON CONFLICT(key) DO NOTHING", (key.lower().lstrip("@"), reason))


def is_suppressed(key: str) -> bool:
    with _conn() as c:
        return c.execute("SELECT 1 FROM suppression WHERE key=?",
                         (key.lower().lstrip("@"),)).fetchone() is not None


def suppression_list() -> list[dict]:
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM suppression ORDER BY created_at DESC")]


def already_contacted(targets: list[str]) -> set[str]:
    """Из списка возвращает тех, кому уже писали (в любой момент) — чтобы не платить дважды."""
    if not targets:
        return set()
    norm = [t.lower().lstrip("@") for t in targets]
    out: set[str] = set()
    with _conn() as c:
        for i in range(0, len(norm), 400):
            chunk = norm[i:i + 400]
            marks = ",".join("?" * len(chunk))
            for r in c.execute(
                f"SELECT DISTINCT lower(ltrim(target,'@')) AS t FROM sends"
                f" WHERE status='sent' AND lower(ltrim(target,'@')) IN ({marks})", chunk
            ):
                out.add(r["t"])
    return out


# ──────────────────────────────────────────────────────────────────────────
#  Аккаунты
# ──────────────────────────────────────────────────────────────────────────

def ensure_account(name: str, source: str = "own", cost: float = 0.0) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO account_meta (name, source, cost) VALUES (?,?,?) "
            "ON CONFLICT(name) DO NOTHING", (name, source, cost))


def set_account_cost(name: str, cost: float, source: str | None = None) -> None:
    ensure_account(name)
    with _conn() as c:
        if source:
            c.execute("UPDATE account_meta SET cost=?, source=? WHERE name=?", (cost, source, name))
        else:
            c.execute("UPDATE account_meta SET cost=? WHERE name=?", (cost, name))


def get_account(name: str) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM account_meta WHERE name=?", (name,)).fetchone()
        return dict(r) if r else None


def list_accounts() -> list[dict]:
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM account_meta ORDER BY name")]


def update_account(name: str, **fields) -> None:
    if not fields:
        return
    ensure_account(name)
    sets = ", ".join(f"{k}=?" for k in fields)
    with _conn() as c:
        c.execute(f"UPDATE account_meta SET {sets} WHERE name=?", (*fields.values(), name))


def log_event(account: str, event: str, detail: str | None = None) -> None:
    ensure_account(account)
    with _conn() as c:
        c.execute("INSERT INTO account_events (account, event, detail, ts) VALUES (?,?,?,?)",
                  (account, event, detail, _now()))


def account_events(account: str | None = None, days: float = 14) -> list[dict]:
    q = "SELECT * FROM account_events WHERE ts >= ?"
    args: list = [_ago(days)]
    if account:
        q += " AND account=?"
        args.append(account)
    with _conn() as c:
        return [dict(r) for r in c.execute(q + " ORDER BY ts DESC LIMIT 500", args)]


def sends_today(account: str) -> int:
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    with _conn() as c:
        r = c.execute("SELECT COUNT(*) n FROM sends WHERE account=? AND status='sent' AND sent_at >= ?",
                      (account, start)).fetchone()
        return r["n"]


def account_error_rate(account: str, days: float = 3) -> tuple[int, int]:
    """(ошибок, всего попыток) за окно — для контура троттлинга."""
    since = _ago(days)
    with _conn() as c:
        r = c.execute(
            "SELECT COUNT(*) total, SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) failed"
            " FROM sends WHERE account=? AND sent_at >= ?", (account, since)).fetchone()
        return (r["failed"] or 0), (r["total"] or 0)


def account_lifetime_sends(account: str) -> int:
    with _conn() as c:
        r = c.execute("SELECT COUNT(*) n FROM sends WHERE account=? AND status='sent'",
                      (account,)).fetchone()
        return r["n"]


# ──────────────────────────────────────────────────────────────────────────
#  Агрегаты для графиков
# ──────────────────────────────────────────────────────────────────────────

def funnel(days: float = 30, niche: str | None = None, job_id: str | None = None) -> dict:
    q = "SELECT status, outcome, COUNT(*) n FROM sends WHERE sent_at >= ?"
    args: list = [_ago(days)]
    if niche:
        q += " AND niche=?"
        args.append(niche)
    if job_id:
        q += " AND job_id=?"
        args.append(job_id)
    q += " GROUP BY status, outcome"

    out = {"attempted": 0, "sent": 0, "skipped": 0, "failed": 0,
           "replied": 0, "negative": 0, "leads": 0, "pending": 0, "no_reply": 0,
           "subscribed": 0}
    with _conn() as c:
        for r in c.execute(q, args):
            n = r["n"]
            out["attempted"] += n
            if r["status"] == "sent":
                out["sent"] += n
                key = r["outcome"]
                if key == "lead":
                    out["leads"] += n
                    out["replied"] += n
                elif key == "replied":
                    out["replied"] += n
                elif key == "negative":
                    out["negative"] += n
                    out["replied"] += n
                elif key in out:
                    out[key] += n
            elif r["status"] == "skipped":
                out["skipped"] += n
            elif r["status"] == "failed":
                out["failed"] += n

        # подписки — отдельное поле, не часть outcome (см. коммент над record_subscription)
        sub_q = "SELECT COUNT(*) n FROM sends WHERE status='sent' AND subscribed_at IS NOT NULL AND sent_at >= ?"
        sub_args = [_ago(days)]
        if niche:
            sub_q += " AND niche=?"
            sub_args.append(niche)
        if job_id:
            sub_q += " AND job_id=?"
            sub_args.append(job_id)
        out["subscribed"] = c.execute(sub_q, sub_args).fetchone()["n"] or 0

    out["reply_rate"] = round(out["replied"] / out["sent"], 4) if out["sent"] else 0.0
    out["lead_rate"] = round(out["leads"] / out["sent"], 4) if out["sent"] else 0.0
    out["subscribe_rate"] = round(out["subscribed"] / out["sent"], 4) if out["sent"] else 0.0
    return out


def timeseries(days: int = 14, niche: str | None = None) -> list[dict]:
    q = ("SELECT date(sent_at) d,"
         " SUM(CASE WHEN status='sent' THEN 1 ELSE 0 END) sent,"
         " SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) failed,"
         " SUM(CASE WHEN outcome IN ('replied','lead','negative') THEN 1 ELSE 0 END) replied,"
         " SUM(CASE WHEN outcome='lead' THEN 1 ELSE 0 END) leads,"
         " SUM(CASE WHEN subscribed_at IS NOT NULL THEN 1 ELSE 0 END) subscribed"
         " FROM sends WHERE sent_at >= ?")
    args: list = [_ago(days)]
    if niche:
        q += " AND niche=?"
        args.append(niche)
    with _conn() as c:
        return [dict(r) for r in c.execute(q + " GROUP BY d ORDER BY d", args)]


def template_stats(niche: str | None = None, days: float = 90) -> list[dict]:
    """Успехи/провалы по каждому варианту текста — вход для бандита и для A/B-графика.

    reply_rate/decided считаются только по ответам (см. optimizer/bandit.py) —
    subscribed идёт отдельной информационной колонкой, в решение бандита
    пока не подмешивается.
    """
    q = ("SELECT t.id, t.niche, t.variant, t.text, t.active,"
         " COUNT(s.id) sent,"
         " SUM(CASE WHEN s.outcome IN ('replied','lead') THEN 1 ELSE 0 END) replies,"
         " SUM(CASE WHEN s.outcome='lead' THEN 1 ELSE 0 END) leads,"
         " SUM(CASE WHEN s.outcome='negative' THEN 1 ELSE 0 END) negatives,"
         " SUM(CASE WHEN s.outcome='no_reply' THEN 1 ELSE 0 END) no_reply,"
         " SUM(CASE WHEN s.outcome='pending' THEN 1 ELSE 0 END) pending,"
         " SUM(CASE WHEN s.subscribed_at IS NOT NULL THEN 1 ELSE 0 END) subscribed"
         " FROM templates t LEFT JOIN sends s"
         "   ON s.template_id = t.id AND s.status='sent' AND s.sent_at >= ?")
    args: list = [_ago(days)]
    if niche:
        q += " WHERE t.niche=?"
        args.append(niche)
    with _conn() as c:
        rows = [dict(r) for r in c.execute(q + " GROUP BY t.id ORDER BY t.niche, t.variant", args)]
    for r in rows:
        decided = (r["replies"] or 0) + (r["no_reply"] or 0) + (r["negatives"] or 0)
        r["decided"] = decided
        r["reply_rate"] = round((r["replies"] or 0) / decided, 4) if decided else None
    return rows


def account_stats(days: float = 7) -> list[dict]:
    q = ("SELECT a.name, a.source, a.cost, a.status, a.first_used_at, a.rest_until,"
         " a.cap_multiplier,"
         " COUNT(s.id) attempts,"
         " SUM(CASE WHEN s.status='sent' THEN 1 ELSE 0 END) sent,"
         " SUM(CASE WHEN s.status='failed' THEN 1 ELSE 0 END) failed,"
         " SUM(CASE WHEN s.outcome IN ('replied','lead') THEN 1 ELSE 0 END) replies"
         " FROM account_meta a LEFT JOIN sends s"
         "   ON s.account = a.name AND s.sent_at >= ?"
         " GROUP BY a.name ORDER BY a.name")
    with _conn() as c:
        rows = [dict(r) for r in c.execute(q, (_ago(days),))]
    for r in rows:
        r["error_rate"] = round((r["failed"] or 0) / r["attempts"], 4) if r["attempts"] else 0.0
        r["lifetime_sends"] = account_lifetime_sends(r["name"])
    return rows


def totals() -> dict:
    with _conn() as c:
        s = c.execute(
            "SELECT COUNT(*) attempts,"
            " SUM(CASE WHEN status='sent' THEN 1 ELSE 0 END) sent,"
            " SUM(CASE WHEN outcome='lead' THEN 1 ELSE 0 END) leads,"
            " SUM(CASE WHEN subscribed_at IS NOT NULL THEN 1 ELSE 0 END) subscribed"
            " FROM sends").fetchone()
        a = c.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(cost),0) spend,"
            " SUM(CASE WHEN status='dead' THEN 1 ELSE 0 END) dead FROM account_meta").fetchone()
    return {
        "attempts": s["attempts"] or 0,
        "sent": s["sent"] or 0,
        "leads": s["leads"] or 0,
        "subscribed": s["subscribed"] or 0,
        "accounts": a["n"] or 0,
        "accounts_dead": a["dead"] or 0,
        "spend": round(a["spend"] or 0, 2),
    }
