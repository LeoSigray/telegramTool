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

# ── Классификация ответа ─────────────────────────────────────────────────
# Ответ = человек написал в ответ на рассылку. Делим на:
#   rejected — отказ (любой), → стоп-лист навсегда
#   success  — всё остальное, что написали
# Молчание → no_reply, недоставка/удаление чата → blocked (см. record_reply,
# dm_runner, listener). Списки ниже — редактируемые; после правки прогнать
# reclassify_replies(), чтобы применить к уже сохранённым текстам.

# Жёсткий отказ / жалоба — помимо стоп-листа это репутационный риск.
HARD_OPT_OUT = (
    "не пишите", "не пиши", "отпишись", "отписаться", "отстаньте", "отстань",
    "спам", "жалоба", "пожалуюсь", "заблокирую", "в бан", "unsubscribe", "stop",
)

# Мягкий отказ — вежливое «нет». Тоже в стоп-лист (по решению: не заходим повторно).
SOFT_REJECT = (
    "нет спасибо", "спасибо нет", "нет, спасибо", "спасибо, не", "не спасибо",
    "не нужно", "ненужно", "не надо", "ненадо", "нам не нужно", "мне не нужно",
    "не интересно", "неинтересно", "не интересует", "не заинтересован", "не заинтересованы",
    "не актуально", "неактуально", "не актуальн",
    "не пойдёт", "не пойдет", "нам это не", "мне это не",
    "не рассматриваю", "не рассматриваем", "не работаем с", "уже есть подрядчик",
    "нет, не", "нет не ", "нет.", "не сейчас", "не в этом", "не по адресу",
    "нет, спасибо, не", "спасибо, но нет", "спс нет",
)

# Полный набор для матчинга (жёсткие + мягкие).
REJECT_MARKERS = HARD_OPT_OUT + SOFT_REJECT

# для обратной совместимости — часть кода/тестов ещё ссылается на старое имя
OPT_OUT_MARKERS = HARD_OPT_OUT


def classify_reply(text: str | None) -> tuple[str, str | None]:
    """(outcome, stop_reason). outcome: 'rejected' | 'success'.
    stop_reason не None → добавить в стоп-лист с этой причиной."""
    low = (text or "").lower().strip()
    if not low:
        return "success", None            # пустой/сервисный — не наказываем текст
    if any(m in low for m in HARD_OPT_OUT):
        return "rejected", "жёсткий отказ / жалоба"
    if any(m in low for m in SOFT_REJECT):
        return "rejected", "отказ в ответе"
    return "success", None


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
                seller         TEXT,
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
        c.execute("CREATE INDEX IF NOT EXISTS ix_sends_subscribed ON sends(subscribed_at)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_meta_acquired ON account_meta(acquired_at)")
        # Переименование исходов: replied → success, negative → rejected.
        # Идемпотентно (на новых базах этих значений уже нет).
        c.execute("UPDATE sends SET outcome='success'  WHERE outcome='replied'")
        c.execute("UPDATE sends SET outcome='rejected' WHERE outcome='negative'")


def _migrate_columns(c: sqlite3.Connection) -> None:
    """ALTER TABLE ADD COLUMN для баз, созданных до появления channel/subscribed_at/seller."""
    sends_cols = {r["name"] for r in c.execute("PRAGMA table_info(sends)")}
    if "channel" not in sends_cols:
        c.execute("ALTER TABLE sends ADD COLUMN channel TEXT")
    if "subscribed_at" not in sends_cols:
        c.execute("ALTER TABLE sends ADD COLUMN subscribed_at TEXT")

    meta_cols = {r["name"] for r in c.execute("PRAGMA table_info(account_meta)")}
    if "seller" not in meta_cols:
        c.execute("ALTER TABLE account_meta ADD COLUMN seller TEXT")


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

    Классифицирует: отказ (→ стоп-лист) или успех. См. classify_reply().
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

        outcome, stop_reason = classify_reply(text)
        c.execute(
            "UPDATE sends SET replied_at=?, reply_text=?, outcome=? WHERE id=?",
            (_now(), (text or "")[:2000], outcome, row["id"]),
        )
        if stop_reason:
            for key in (str(peer_id), row["target"].lower().lstrip("@")):
                c.execute("INSERT INTO suppression (key, reason) VALUES (?,?) "
                          "ON CONFLICT(key) DO NOTHING", (key, stop_reason))
        return row["id"]


def mark_send_blocked(send_id: int, detail: str = "blocked") -> None:
    """Не удалось доставить: заблокирован / закрытая приватность (dm_runner)."""
    with _conn() as c:
        c.execute("UPDATE sends SET outcome='blocked', error=? WHERE id=?", (detail, send_id))


def record_chat_deleted(peer_id: str) -> int | None:
    """
    Best-effort: пришло событие удаления сообщений в 1:1. Если этому человеку
    недавно писали и он не отвечал — помечаем blocked (удалил чат). Если уже
    ответил — реакция важнее, не трогаем. Telegram для 1:1 часто не передаёт
    чат события удаления, а «удалить у себя» вообще не долетает — сигнал неполный.
    """
    cutoff = _ago(SUBSCRIBE_WINDOW_DAYS)
    with _conn() as c:
        row = c.execute(
            "SELECT id FROM sends WHERE peer_id=? AND status='sent'"
            " AND outcome IN ('pending','no_reply') AND sent_at >= ?"
            " ORDER BY sent_at DESC LIMIT 1", (str(peer_id), cutoff)).fetchone()
        if row is None:
            return None
        c.execute("UPDATE sends SET outcome='blocked', error='чат удалён' WHERE id=?", (row["id"],))
        return row["id"]


def mark_outcome(send_id: int, outcome: str) -> bool:
    """Финальный статус из CRM. Принимает и новые имена (success/rejected/blocked),
    и старые (replied/negative) — нормализует."""
    norm = {"replied": "success", "negative": "rejected"}.get(outcome, outcome)
    with _conn() as c:
        cur = c.execute("UPDATE sends SET outcome=? WHERE id=?", (norm, send_id))
        return cur.rowcount > 0


def reclassify_replies() -> dict:
    """Прогоняет обновлённые списки фраз по уже сохранённым reply_text.
    Возвращает {moved_to_rejected, moved_to_success}. Стоп-лист пополняет,
    но НЕ вычищает (снятие — вручную)."""
    moved_r = moved_s = 0
    with _conn() as c:
        rows = c.execute(
            "SELECT id, peer_id, target, reply_text, outcome FROM sends"
            " WHERE reply_text IS NOT NULL AND outcome IN ('success','rejected')").fetchall()
        for r in rows:
            want, stop_reason = classify_reply(r["reply_text"])
            if want == r["outcome"]:
                continue
            c.execute("UPDATE sends SET outcome=? WHERE id=?", (want, r["id"]))
            if want == "rejected":
                moved_r += 1
                for key in (str(r["peer_id"] or ""), (r["target"] or "").lower().lstrip("@")):
                    if key:
                        c.execute("INSERT INTO suppression (key, reason) VALUES (?,?) "
                                  "ON CONFLICT(key) DO NOTHING", (key, stop_reason or "отказ в ответе"))
            else:
                moved_s += 1
    return {"moved_to_rejected": moved_r, "moved_to_success": moved_s}


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


def record_subscription(peer_id: str, channel: str | None = None,
                        joined_at: str | None = None) -> int | None:
    """
    Матчит нового участника канала с последней отправкой этому человеку.
    Возвращает id записи sends или None если подходящей отправки не было
    (человек подписался сам по себе, не через эту рассылку).

    joined_at — реальная дата вступления из Telegram (participant.date). Если
    её нет — ставим текущее время (момент синка). Для 10-минутного графика
    подписок важно именно реальное время, поэтому channel_watch его передаёт.
    """
    cutoff = _ago(SUBSCRIBE_WINDOW_DAYS)
    q = ("SELECT id, sent_at FROM sends WHERE peer_id=? AND status='sent'"
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
        # не раньше отправки: если Telegram отдал дату вступления до нашего
        # сообщения — значит человек был в канале и так, засчитываем моментом синка
        stamp = joined_at if (joined_at and joined_at >= row["sent_at"]) else _now()
        c.execute("UPDATE sends SET subscribed_at=? WHERE id=?", (stamp, row["id"]))
        return row["id"]


def correct_subscription_dates(channel: str, id_to_joined: dict) -> int:
    """
    Одноразовая правка: заменяет время часового синка на реальную дату
    вступления для уже сматченных подписок этого канала. id_to_joined —
    {telegram_user_id: joined_at_iso}. Возвращает число исправленных строк.
    """
    if not id_to_joined:
        return 0
    fixed = 0
    with _conn() as c:
        for uid, joined in id_to_joined.items():
            if not joined:
                continue
            cur = c.execute(
                "UPDATE sends SET subscribed_at=? WHERE channel=? AND peer_id=?"
                " AND subscribed_at IS NOT NULL AND subscribed_at > ? AND sent_at <= ?",
                (joined, channel, str(uid), joined, joined))
            fixed += cur.rowcount
    return fixed


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
    """Гарантирует, что строка аккаунта есть. Повторный вызов не трогает данные
    (DO NOTHING) — это горячий путь из server.py, он не должен затирать
    source/cost/seller/acquired_at, проставленные в точке покупки."""
    with _conn() as c:
        c.execute(
            "INSERT INTO account_meta (name, source, cost) VALUES (?,?,?) "
            "ON CONFLICT(name) DO NOTHING", (name, source, cost))


def register_purchase(name: str, source: str, cost: float = 0.0,
                      seller: str | None = None, acquired_at: str | None = None) -> None:
    """
    Фиксирует метаданные приобретения аккаунта в точке покупки/импорта.
    В отличие от ensure_account — заполняет реальные source/cost/seller/acquired_at,
    но только там, где они ещё дефолтные (server.py мог зарегистрировать аккаунт
    раньше с source='own', cost=0). acquired_at перезаписываем лишь если новая
    дата РАНЬШЕ (дата покупки всегда ≤ даты «первый раз увидели в пуле»).
    """
    name = str(name)
    acquired_at = acquired_at or _now()
    with _conn() as c:
        c.execute(
            "INSERT INTO account_meta (name, source, cost, seller, acquired_at)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(name) DO UPDATE SET"
            "   source = CASE WHEN account_meta.source='own'"
            "                 THEN excluded.source ELSE account_meta.source END,"
            "   cost   = CASE WHEN account_meta.cost=0"
            "                 THEN excluded.cost ELSE account_meta.cost END,"
            "   seller = COALESCE(account_meta.seller, excluded.seller),"
            "   acquired_at = CASE WHEN excluded.acquired_at < account_meta.acquired_at"
            "                      THEN excluded.acquired_at ELSE account_meta.acquired_at END",
            (name, source, cost or 0.0, seller, acquired_at))


def set_account_cost(name: str, cost: float, source: str | None = None,
                     seller: str | None = None, acquired_at: str | None = None) -> None:
    ensure_account(name)
    fields: dict = {"cost": cost}
    if source:
        fields["source"] = source
    if seller is not None:
        fields["seller"] = seller
    if acquired_at is not None:
        fields["acquired_at"] = acquired_at
    sets = ", ".join(f"{k}=?" for k in fields)
    with _conn() as c:
        c.execute(f"UPDATE account_meta SET {sets} WHERE name=?", (*fields.values(), name))


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


def first_send_at(account: str) -> str | None:
    """Время самой первой отправки аккаунта — грубая нижняя оценка даты покупки
    для бэкофилла."""
    with _conn() as c:
        r = c.execute("SELECT MIN(sent_at) t FROM sends WHERE account=?", (account,)).fetchone()
        return r["t"] if r and r["t"] else None


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

    # replied/negative — алиасы для старых дашбордов: replied = «написал в ответ»
    # (success + rejected + lead), negative = rejected.
    out = {"attempted": 0, "sent": 0, "skipped": 0, "failed": 0,
           "success": 0, "rejected": 0, "blocked": 0,
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
                    out["leads"] += n; out["success"] += n; out["replied"] += n
                elif key == "success":
                    out["success"] += n; out["replied"] += n
                elif key == "rejected":
                    out["rejected"] += n; out["negative"] += n; out["replied"] += n
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
    out["success_rate"] = round(out["success"] / out["sent"], 4) if out["sent"] else 0.0
    out["reject_rate"] = round(out["rejected"] / out["sent"], 4) if out["sent"] else 0.0
    out["blocked_rate"] = round(out["blocked"] / out["sent"], 4) if out["sent"] else 0.0
    out["lead_rate"] = round(out["leads"] / out["sent"], 4) if out["sent"] else 0.0
    out["subscribe_rate"] = round(out["subscribed"] / out["sent"], 4) if out["sent"] else 0.0
    return out


# Выражение группировки по времени — то же самое, что "цена деления" у Y-зума,
# только для оси X. День — совместимо со старым поведением; час/минута нужны,
# чтобы можно было зумиться во времени вплоть до минутной детализации.
_BUCKET_EXPR = {
    "day":    "date(sent_at)",
    "hour":   "strftime('%Y-%m-%d %H:00', sent_at)",
    "minute": "strftime('%Y-%m-%d %H:%M', sent_at)",
}


def timeseries(days: float = 14, niche: str | None = None, granularity: str = "day") -> list[dict]:
    bucket = _BUCKET_EXPR.get(granularity, _BUCKET_EXPR["day"])
    q = (f"SELECT {bucket} d,"
         " SUM(CASE WHEN status='sent' THEN 1 ELSE 0 END) sent,"
         " SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) failed,"
         " SUM(CASE WHEN outcome IN ('success','lead','rejected') THEN 1 ELSE 0 END) replied,"
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
         " SUM(CASE WHEN s.outcome IN ('success','lead') THEN 1 ELSE 0 END) replies,"
         " SUM(CASE WHEN s.outcome='lead' THEN 1 ELSE 0 END) leads,"
         " SUM(CASE WHEN s.outcome='rejected' THEN 1 ELSE 0 END) rejected,"
         " SUM(CASE WHEN s.outcome='blocked' THEN 1 ELSE 0 END) blocked,"
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
        r["negatives"] = r["rejected"]  # алиас для старого кода бандита
        decided = (r["replies"] or 0) + (r["no_reply"] or 0) + (r["rejected"] or 0)
        r["decided"] = decided
        r["reply_rate"] = round((r["replies"] or 0) / decided, 4) if decided else None
    return rows


def account_stats(days: float = 7) -> list[dict]:
    q = ("SELECT a.name, a.source, a.cost, a.status, a.first_used_at, a.rest_until,"
         " a.cap_multiplier,"
         " COUNT(s.id) attempts,"
         " SUM(CASE WHEN s.status='sent' THEN 1 ELSE 0 END) sent,"
         " SUM(CASE WHEN s.status='failed' THEN 1 ELSE 0 END) failed,"
         " SUM(CASE WHEN s.outcome IN ('success','lead') THEN 1 ELSE 0 END) replies"
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


# ──────────────────────────────────────────────────────────────────────────
#  Дашборд «Закупка аккаунтов»
# ──────────────────────────────────────────────────────────────────────────

_ACQ_BUCKET = {
    "day":   "date({col})",
    "week":  "strftime('%Y-W%W', {col})",
    "month": "strftime('%Y-%m', {col})",
}


def acquisition_series(bucket: str = "week", days: float | None = None) -> list[dict]:
    """
    Покупки аккаунтов во времени. bucket: day | week | month.
    days не задан → всё время. Если задан — выдача урезается окном (накопительные
    суммы при этом считаются внутри окна, не с начала времён).

    Возвращает по бакету: bought / dead за бакет, bought_cum, alive_cum
    (куплено − умерло), spend / spend_cum, by_source.
    """
    tmpl = _ACQ_BUCKET.get(bucket, _ACQ_BUCKET["week"])
    b_acq, b_dead = tmpl.format(col="acquired_at"), tmpl.format(col="dead_at")

    acq_where, dead_where = "acquired_at IS NOT NULL", "dead_at IS NOT NULL"
    acq_args: list = []
    dead_args: list = []
    if days:
        since = _ago(days)
        acq_where += " AND acquired_at >= ?"
        dead_where += " AND dead_at >= ?"
        acq_args.append(since)
        dead_args.append(since)

    with _conn() as c:
        acq = c.execute(
            f"SELECT {b_acq} b, COALESCE(source,'own') src, COUNT(*) n,"
            f" COALESCE(SUM(cost),0) spend FROM account_meta"
            f" WHERE {acq_where} GROUP BY b, src", acq_args).fetchall()
        deaths = c.execute(
            f"SELECT {b_dead} b, COUNT(*) n FROM account_meta"
            f" WHERE {dead_where} GROUP BY b", dead_args).fetchall()

    buckets: dict[str, dict] = {}
    for r in acq:
        d = buckets.setdefault(r["b"], {"bought": 0, "dead": 0, "spend": 0.0, "by_source": {}})
        d["bought"] += r["n"]
        d["spend"] += r["spend"] or 0
        d["by_source"][r["src"]] = d["by_source"].get(r["src"], 0) + r["n"]
    for r in deaths:
        d = buckets.setdefault(r["b"], {"bought": 0, "dead": 0, "spend": 0.0, "by_source": {}})
        d["dead"] += r["n"]

    out, bought_cum, dead_cum, spend_cum = [], 0, 0, 0.0
    for key in sorted(buckets):
        d = buckets[key]
        bought_cum += d["bought"]
        dead_cum += d["dead"]
        spend_cum += d["spend"]
        out.append({
            "t": key,
            "bought": d["bought"], "dead": d["dead"],
            "bought_cum": bought_cum, "alive_cum": bought_cum - dead_cum,
            "spend": round(d["spend"], 2), "spend_cum": round(spend_cum, 2),
            "by_source": d["by_source"],
        })
    return out


# ──────────────────────────────────────────────────────────────────────────
#  Дашборд «Эффективность аккаунтов» — разрезы по цене и по продавцу
# ──────────────────────────────────────────────────────────────────────────

# Ценовые корзины (руб.). Легко править. Верхняя граница None = «и выше».
PRICE_BRACKETS: list[tuple[int, int | None]] = [(10, 40), (40, 70), (70, 120), (120, None)]
_NO_PRICE = "без цены"


def _price_bucket(cost: float | None) -> str:
    if not cost or cost <= 0:
        return _NO_PRICE
    if cost < PRICE_BRACKETS[0][0]:
        return f"< {PRICE_BRACKETS[0][0]} ₽"
    for lo, hi in PRICE_BRACKETS:
        if cost >= lo and (hi is None or cost < hi):
            return f"{lo}–{hi} ₽" if hi is not None else f"{lo}+ ₽"
    return "прочее"


def _price_order() -> list[str]:
    order = [_NO_PRICE, f"< {PRICE_BRACKETS[0][0]} ₽"]
    for lo, hi in PRICE_BRACKETS:
        order.append(f"{lo}–{hi} ₽" if hi is not None else f"{lo}+ ₽")
    return order


def _days_alive(meta: dict) -> float:
    """Сколько дней аккаунт прожил: от первого использования (или покупки)
    до бана (или до сейчас, если ещё жив)."""
    start = meta.get("first_used_at") or meta.get("acquired_at")
    if not start:
        return 0.0
    end = meta.get("dead_at") or _now()
    try:
        d0 = datetime.fromisoformat(start)
        d1 = datetime.fromisoformat(end)
    except (ValueError, TypeError):
        return 0.0
    if d0.tzinfo is None:
        d0 = d0.replace(tzinfo=timezone.utc)
    if d1.tzinfo is None:
        d1 = d1.replace(tzinfo=timezone.utc)
    return max(0.0, (d1 - d0).total_seconds() / 86400)


def account_cohorts(by: str = "price", days: float = 90) -> list[dict]:
    """
    Аккаунты, сгруппированные по цене (корзины PRICE_BRACKETS) или по продавцу.
    На корзину — средний пробег, дни жизни, % банов, reply-rate и «выхлоп за рубль»
    (sends/replies/leads на потраченный рубль). Отвечает на вопрос «зависит ли
    качество аккаунта от цены и от продавца».

    days — окно для метрик рассылки (reply_rate, *_per_rub считаются по sends в
    этом окне). Состав корзин — по всем аккаунтам, окно на него не влияет.
    lifetime_sends — всегда за всё время (это метрика износа, а не периода).
    """
    win_since = _ago(days) if days else "1970-01-01T00:00:00"
    accounts = list_accounts()

    with _conn() as c:
        life = {r["account"]: r["n"] for r in c.execute(
            "SELECT account, COUNT(*) n FROM sends WHERE status='sent' GROUP BY account")}
        win = {r["account"]: r for r in c.execute(
            "SELECT account,"
            " SUM(CASE WHEN status='sent' THEN 1 ELSE 0 END) sent,"
            " SUM(CASE WHEN outcome IN ('success','lead') THEN 1 ELSE 0 END) replies,"
            " SUM(CASE WHEN outcome='lead' THEN 1 ELSE 0 END) leads"
            " FROM sends WHERE sent_at >= ? GROUP BY account", (win_since,))}

    groups: dict[str, list] = {}
    for a in accounts:
        key = _price_bucket(a.get("cost")) if by == "price" else (a.get("seller") or "неизвестен")
        groups.setdefault(key, []).append(a)

    out = []
    for key, accs in groups.items():
        n = len(accs)
        names = [a["name"] for a in accs]
        total_cost = sum((a.get("cost") or 0) for a in accs)
        total_sent = sum((win.get(nm) or {})["sent"] or 0 for nm in names if win.get(nm))
        total_repl = sum((win.get(nm) or {})["replies"] or 0 for nm in names if win.get(nm))
        total_lead = sum((win.get(nm) or {})["leads"] or 0 for nm in names if win.get(nm))
        dead = sum(1 for a in accs if a.get("status") == "dead")
        out.append({
            "key": key,
            "accounts": n,
            "avg_cost": round(total_cost / n, 2) if n else 0.0,
            "avg_lifetime_sends": round(sum(life.get(nm, 0) for nm in names) / n, 1) if n else 0.0,
            "avg_days_alive": round(sum(_days_alive(a) for a in accs) / n, 1) if n else 0.0,
            "pct_dead": round(dead / n, 4) if n else 0.0,
            "reply_rate": round(total_repl / total_sent, 4) if total_sent else 0.0,
            "sends_per_rub": round(total_sent / total_cost, 3) if total_cost else None,
            "replies_per_rub": round(total_repl / total_cost, 4) if total_cost else None,
            "leads_per_rub": round(total_lead / total_cost, 4) if total_cost else None,
            "enough_data": n >= 5,
        })

    if by == "price":
        order = _price_order()
        out.sort(key=lambda r: order.index(r["key"]) if r["key"] in order else 99)
    else:
        out.sort(key=lambda r: -r["accounts"])
    return out


# ──────────────────────────────────────────────────────────────────────────
#  Дашборд «Подписки» — темп подписок мелкими бакетами (деф. 10 минут)
# ──────────────────────────────────────────────────────────────────────────

def _bucket_minutes_expr(col: str, n: int) -> str:
    """SQL-выражение: ключ N-минутного бакета вида '2026-08-31T14:20'."""
    n = max(1, min(int(n), 60))
    return (f"strftime('%Y-%m-%dT%H:', {col}) || "
            f"substr('0' || ((CAST(strftime('%M', {col}) AS INTEGER) / {n}) * {n}), -2)")


def subscription_series(bucket_minutes: int = 10, hours: float = 24,
                        channel: str | None = None) -> dict:
    """
    Подписки по N-минутным бакетам за последние `hours` часов, по РЕАЛЬНОМУ
    времени вступления (sends.subscribed_at, которое channel_watch пишет из
    participant.date). subs_cum включает подписки до начала окна.

    При широком окне бакет укрупняется (иначе точек слишком много):
    > 24 ч → минимум 30 мин, > 72 ч → минимум 60 мин.

    Возвращает {bucket_minutes, window_hours, series:[{t, subs, subs_cum, by_channel}]}.
    """
    if hours > 72:
        bucket_minutes = max(bucket_minutes, 60)
    elif hours > 24:
        bucket_minutes = max(bucket_minutes, 30)
    since = _ago(hours / 24)
    bexpr = _bucket_minutes_expr("subscribed_at", bucket_minutes)

    q = (f"SELECT {bexpr} b, channel, COUNT(*) n FROM sends"
         f" WHERE subscribed_at IS NOT NULL AND subscribed_at >= ?")
    args: list = [since]
    base_q = "SELECT COUNT(*) n FROM sends WHERE subscribed_at IS NOT NULL AND subscribed_at < ?"
    base_args: list = [since]
    if channel:
        q += " AND channel = ?"
        args.append(channel)
        base_q += " AND channel = ?"
        base_args.append(channel)
    q += " GROUP BY b, channel ORDER BY b"

    with _conn() as c:
        rows = c.execute(q, args).fetchall()
        base = c.execute(base_q, base_args).fetchone()["n"] or 0

    buckets: dict[str, dict] = {}
    for r in rows:
        d = buckets.setdefault(r["b"], {"subs": 0, "by_channel": {}})
        d["subs"] += r["n"]
        if r["channel"]:
            d["by_channel"][r["channel"]] = d["by_channel"].get(r["channel"], 0) + r["n"]

    out, cum = [], base
    for key in sorted(buckets):
        d = buckets[key]
        cum += d["subs"]
        out.append({"t": key, "subs": d["subs"], "subs_cum": cum, "by_channel": d["by_channel"]})
    return {"bucket_minutes": bucket_minutes, "window_hours": hours, "series": out}


def subscription_channels(days: float = 30) -> list[str]:
    """Каналы, по которым были подписки за период — для фильтра на дашборде."""
    with _conn() as c:
        return [r["channel"] for r in c.execute(
            "SELECT DISTINCT channel FROM sends WHERE subscribed_at IS NOT NULL"
            " AND channel IS NOT NULL AND subscribed_at >= ? ORDER BY channel",
            (_ago(days),))]


# ──────────────────────────────────────────────────────────────────────────
#  Дашборд «Ответы на сообщения» — отклик по вариантам текста во времени
# ──────────────────────────────────────────────────────────────────────────
#  Исходы: success (ответил чем-то не-отказным) | rejected (отказ, → стоп-лист)
#  | blocked (не доставлено / удалил чат) | no_reply (молчит) | lead (из CRM).
#  «ответили» = success + lead + rejected (написал в ответ хоть что-то).
#  decided = всё, кроме pending (blocked тоже терминальный).

def reply_series(niche: str | None = None, days: float = 30, bucket: str = "day") -> dict:
    """
    Отклик по вариантам текста во времени. Бакет — по дате ОТПРАВКИ (когорта):
    из отправок этого дня какая доля ответила / отказала / заблокировала.
    Свежие бакеты (моложе REPLY_WINDOW_HOURS) ещё дозревают — фронт их помечает.
    """
    tmpl = _ACQ_BUCKET.get(bucket, _ACQ_BUCKET["day"])
    bexpr = tmpl.format(col="s.sent_at")

    q = (f"SELECT {bexpr} b, COALESCE(t.variant,'—') v,"
         " COUNT(*) sent,"
         " SUM(CASE WHEN s.outcome IN ('success','lead') THEN 1 ELSE 0 END) success,"
         " SUM(CASE WHEN s.outcome='rejected' THEN 1 ELSE 0 END) rejected,"
         " SUM(CASE WHEN s.outcome='blocked' THEN 1 ELSE 0 END) blocked,"
         " SUM(CASE WHEN s.outcome='lead' THEN 1 ELSE 0 END) leads,"
         " SUM(CASE WHEN s.outcome='no_reply' THEN 1 ELSE 0 END) no_reply,"
         " SUM(CASE WHEN s.outcome='pending' THEN 1 ELSE 0 END) pending"
         " FROM sends s LEFT JOIN templates t ON t.id = s.template_id"
         " WHERE s.status='sent' AND s.sent_at >= ?")
    args: list = [_ago(days)]
    if niche:
        q += " AND s.niche = ?"
        args.append(niche)
    q += " GROUP BY b, v ORDER BY b"

    variants: set[str] = set()
    buckets: dict[str, dict] = {}
    with _conn() as c:
        for r in c.execute(q, args):
            variants.add(r["v"])
            succ, rej, blk, nr = (r["success"] or 0), (r["rejected"] or 0), (r["blocked"] or 0), (r["no_reply"] or 0)
            decided = succ + rej + blk + nr
            answered = succ + rej
            buckets.setdefault(r["b"], {})[r["v"]] = {
                "sent": r["sent"] or 0, "decided": decided, "pending": r["pending"] or 0,
                "success": succ, "rejected": rej, "blocked": blk, "no_reply": nr,
                "answered": answered, "leads": r["leads"] or 0,
                "success_rate":  round(succ / decided, 4) if decided else None,
                "reject_rate":   round(rej / decided, 4) if decided else None,
                "blocked_rate":  round(blk / decided, 4) if decided else None,
                "answer_rate":   round(answered / decided, 4) if decided else None,
                "lead_rate":     round((r["leads"] or 0) / decided, 4) if decided else None,
            }
    return {
        "bucket": bucket,
        "variants": sorted(variants),
        "series": [{"t": k, "per_variant": buckets[k]} for k in sorted(buckets)],
        "reply_window_hours": REPLY_WINDOW_HOURS,
    }


def reply_breakdown(niche: str | None = None, days: float = 90) -> list[dict]:
    """По каждому варианту текста за окно: отправлено, из решённых —
    успех / отказ / блок / молчание, доли. Строится поверх template_stats()."""
    out = []
    for r in template_stats(niche=niche, days=days):
        success = r["replies"] or 0          # в template_stats replies = success+lead
        rejected = r["rejected"] or 0
        blocked = r["blocked"] or 0
        no_reply = r["no_reply"] or 0
        leads = r["leads"] or 0
        decided = success + rejected + blocked + no_reply
        answered = success + rejected
        out.append({
            "template_id": r["id"], "niche": r["niche"], "variant": r["variant"],
            "text": r["text"], "active": bool(r["active"]),
            "sent": r["sent"] or 0, "decided": decided, "pending": r["pending"] or 0,
            "answered": answered, "success": success, "rejected": rejected,
            "blocked": blocked, "leads": leads, "no_reply": no_reply,
            "answer_rate":   round(answered / decided, 4) if decided else None,
            "success_rate":  round(success / decided, 4) if decided else None,
            "reject_rate":   round(rejected / decided, 4) if decided else None,
            "blocked_rate":  round(blocked / decided, 4) if decided else None,
            "lead_rate":     round(leads / decided, 4) if decided else None,
        })
    return out


def recent_replies(niche: str | None = None, limit: int = 40) -> list[dict]:
    """Последние входящие ответы на рассылку: получатель, вариант текста, исход,
    сам текст ответа. Качественный сигнал для подбора текста — и ровно те данные,
    на которых потом будет учиться нейронка-редактор."""
    q = ("SELECT s.sent_at, s.replied_at, s.niche, s.target, s.outcome,"
         " s.reply_text, t.variant"
         " FROM sends s LEFT JOIN templates t ON t.id = s.template_id"
         " WHERE s.reply_text IS NOT NULL AND s.reply_text <> ''")
    args: list = []
    if niche:
        q += " AND s.niche = ?"
        args.append(niche)
    q += " ORDER BY s.replied_at DESC LIMIT ?"
    args.append(max(1, min(int(limit), 200)))
    with _conn() as c:
        return [dict(r) for r in c.execute(q, args)]


def reply_niches() -> list[str]:
    """Ниши, по которым есть отправки с шаблоном — для фильтра дашборда ответов."""
    with _conn() as c:
        return [r["niche"] for r in c.execute(
            "SELECT DISTINCT niche FROM sends WHERE niche IS NOT NULL"
            " AND template_id IS NOT NULL ORDER BY niche")]


# ──────────────────────────────────────────────────────────────────────────
#  Дашборд «Потраченные средства»
# ──────────────────────────────────────────────────────────────────────────

def spend_series(days: float | None = None, bucket: str = "week") -> dict:
    """
    Траты на аккаунты во времени (по acquired_at) + эффективность денег:
    running-цена сообщения / ответа / лида = потрачено_к_дате ÷ выхлоп_к_дате.
    Running, а не за период — меньше шума, видно тренд «дешевеет ли лид».
    """
    tmpl = _ACQ_BUCKET.get(bucket, _ACQ_BUCKET["week"])
    b_acq, b_sent = tmpl.format(col="acquired_at"), tmpl.format(col="sent_at")

    acq_where, sent_where = "acquired_at IS NOT NULL", "status='sent'"
    a_args: list = []
    s_args: list = []
    if days:
        since = _ago(days)
        acq_where += " AND acquired_at >= ?"
        sent_where += " AND sent_at >= ?"
        a_args.append(since)
        s_args.append(since)

    with _conn() as c:
        spend_rows = c.execute(
            f"SELECT {b_acq} b, COALESCE(source,'own') src, COUNT(*) n,"
            f" COALESCE(SUM(cost),0) spend FROM account_meta"
            f" WHERE {acq_where} GROUP BY b, src", a_args).fetchall()
        out_rows = c.execute(
            f"SELECT {b_sent} b, COUNT(*) sent,"
            f" SUM(CASE WHEN outcome='lead' THEN 1 ELSE 0 END) leads,"
            f" SUM(CASE WHEN outcome IN ('success','lead','rejected') THEN 1 ELSE 0 END) answered"
            f" FROM sends WHERE {sent_where} GROUP BY b", s_args).fetchall()

    keys: set[str] = set()
    spend_by: dict[str, dict] = {}
    for r in spend_rows:
        keys.add(r["b"])
        d = spend_by.setdefault(r["b"], {"spend": 0.0, "bought": 0, "by_source": {}})
        d["spend"] += r["spend"] or 0
        d["bought"] += r["n"]
        d["by_source"][r["src"]] = round(d["by_source"].get(r["src"], 0) + (r["spend"] or 0), 2)
    out_by: dict[str, dict] = {}
    for r in out_rows:
        keys.add(r["b"])
        out_by[r["b"]] = {"sent": r["sent"] or 0, "leads": r["leads"] or 0,
                          "answered": r["answered"] or 0}

    series = []
    spend_cum = 0.0
    sent_cum = leads_cum = answered_cum = 0
    for k in sorted(keys):
        s = spend_by.get(k, {"spend": 0.0, "bought": 0, "by_source": {}})
        o = out_by.get(k, {"sent": 0, "leads": 0, "answered": 0})
        spend_cum += s["spend"]
        sent_cum += o["sent"]
        leads_cum += o["leads"]
        answered_cum += o["answered"]
        series.append({
            "t": k,
            "spend": round(s["spend"], 2), "spend_cum": round(spend_cum, 2),
            "bought": s["bought"], "by_source": s["by_source"],
            "sent_cum": sent_cum, "leads_cum": leads_cum,
            "cpm_running": round(spend_cum / sent_cum, 4) if sent_cum else None,
            "cost_per_answer_running": round(spend_cum / answered_cum, 3) if answered_cum else None,
            "cpl_running": round(spend_cum / leads_cum, 2) if leads_cum else None,
        })

    return {"bucket": bucket, "series": series, "totals": totals()}


def spend_by_seller(days: float | None = None) -> list[dict]:
    """Сколько денег ушло каждому продавцу."""
    q = ("SELECT COALESCE(seller,'неизвестен') seller, COUNT(*) accounts,"
         " COALESCE(SUM(cost),0) spend, COALESCE(AVG(NULLIF(cost,0)),0) avg_cost"
         " FROM account_meta WHERE cost > 0")
    args: list = []
    if days:
        q += " AND acquired_at >= ?"
        args.append(_ago(days))
    q += " GROUP BY seller ORDER BY spend DESC"
    with _conn() as c:
        return [{"seller": r["seller"], "accounts": r["accounts"],
                 "spend": round(r["spend"], 2), "avg_cost": round(r["avg_cost"], 2)}
                for r in c.execute(q, args)]
