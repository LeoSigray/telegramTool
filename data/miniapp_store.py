"""
data/miniapp_store.py — хранилище Mini App «Клиент в один клик».

Покупатель (Telegram-пользователь, открывший Mini App), его баланс сообщений,
кампании и платежи. Таблицы живут в той же data/database.db, что и рассылки
(sends) и диалоги (inbox_*). Покупатель видит только свои диалоги: связь идёт
через sends.job_id его кампаний (inbox_conversations.first_send_id → sends.id).

Баланс меняется только атомарными UPDATE … WHERE balance >= n, чтобы два
одновременных запуска не ушли в минус.
"""
import sqlite3
from datetime import datetime, timedelta, timezone

from data.db import DB_PATH


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_store() -> None:
    """Создаёт таблицы Mini App. Безопасно вызывать повторно."""
    with _conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS mini_users (
                tg_id         INTEGER PRIMARY KEY,
                username      TEXT,
                first_name    TEXT,
                last_name     TEXT,
                balance       INTEGER NOT NULL DEFAULT 0,
                gift_claimed  INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT NOT NULL,
                last_seen_at  TEXT
            );

            CREATE TABLE IF NOT EXISTS mini_campaigns (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id        INTEGER NOT NULL,
                job_id       TEXT,
                text         TEXT NOT NULL,
                total        INTEGER NOT NULL,
                reserved     INTEGER NOT NULL,
                sent         INTEGER NOT NULL DEFAULT 0,
                failed       INTEGER NOT NULL DEFAULT 0,
                status       TEXT NOT NULL DEFAULT 'running',  -- running | done | stopped | failed
                created_at   TEXT NOT NULL,
                finished_at  TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_mini_camp_user ON mini_campaigns(tg_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS ix_mini_camp_job  ON mini_campaigns(job_id);

            CREATE TABLE IF NOT EXISTS mini_payments (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id       INTEGER NOT NULL,
                pack_id     TEXT NOT NULL,
                messages    INTEGER NOT NULL,
                amount      INTEGER NOT NULL,
                currency    TEXT NOT NULL,
                method      TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'pending',   -- pending | paid | failed
                payload     TEXT NOT NULL UNIQUE,
                charge_id   TEXT,
                created_at  TEXT NOT NULL,
                paid_at     TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_mini_pay_user ON mini_payments(tg_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS mini_state (
                key   TEXT PRIMARY KEY,
                value TEXT
            );

            -- клиенты покупателя: люди, которых нашёл умный парсинг по его каналу или нише
            CREATE TABLE IF NOT EXISTS mini_leads (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id       INTEGER NOT NULL,
                username    TEXT NOT NULL,                  -- без @, в нижнем регистре
                user_id     INTEGER,
                name        TEXT,
                tier        TEXT,                           -- A | B | C: насколько горячий
                interest    REAL NOT NULL DEFAULT 0,
                why         TEXT,
                status      TEXT NOT NULL DEFAULT 'new',    -- new | sent | failed
                created_at  TEXT NOT NULL,
                UNIQUE (tg_id, username)
            );
            CREATE INDEX IF NOT EXISTS ix_mini_leads_user ON mini_leads(tg_id, status);

            -- нажатия на большую кнопку: каждое нажатие = одно сообщение одному человеку
            CREATE TABLE IF NOT EXISTS mini_taps (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id       INTEGER NOT NULL,
                status      TEXT NOT NULL DEFAULT 'queued',   -- queued | sending | sent | failed
                target      TEXT,
                account     TEXT,
                error       TEXT,
                created_at  TEXT NOT NULL,
                done_at     TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_mini_taps_user ON mini_taps(tg_id, status);
            CREATE INDEX IF NOT EXISTS ix_mini_taps_queue ON mini_taps(status, id);

            -- чат поддержки: покупатель пишет «поддержке», отвечает оператор через бота
            CREATE TABLE IF NOT EXISTS mini_support (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id       INTEGER NOT NULL,
                direction   TEXT NOT NULL,                -- in: покупатель → поддержка | out: поддержка → покупателю
                text        TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                delivered   INTEGER NOT NULL DEFAULT 0,   -- in: передано оператору
                read        INTEGER NOT NULL DEFAULT 0    -- out: покупатель прочитал
            );
            CREATE INDEX IF NOT EXISTS ix_mini_support_user ON mini_support(tg_id, id);

            -- копии сообщений покупателей у операторов: ответ (Reply) на копию находит покупателя
            CREATE TABLE IF NOT EXISTS mini_support_ops (
                op_chat_id  INTEGER NOT NULL,
                op_msg_id   INTEGER NOT NULL,
                support_id  INTEGER NOT NULL,
                PRIMARY KEY (op_chat_id, op_msg_id)
            );

            -- какие исходящие сообщения покупатель менял: ярлык «изменено» под пузырём
            CREATE TABLE IF NOT EXISTS mini_msg_edits (
                conversation_id INTEGER NOT NULL,
                tg_id           INTEGER NOT NULL,
                edited_at       TEXT NOT NULL,
                PRIMARY KEY (conversation_id, tg_id)
            );
        """)
        have = {r["name"] for r in c.execute("PRAGMA table_info(mini_users)")}
        for col, kind in (("channel", "TEXT"), ("offer_text", "TEXT"), ("channel_status", "TEXT"),
                          ("channel_error", "TEXT"), ("channel_kind", "TEXT"),
                          ("parse_status", "TEXT"), ("parse_queued_at", "TEXT"),
                          ("parse_finished_at", "TEXT"), ("parse_runs", "INTEGER NOT NULL DEFAULT 0")):
            if col not in have:
                c.execute(f"ALTER TABLE mini_users ADD COLUMN {col} {kind}")


# ──────────────────────────────────────────────────────────────────────────
#  Покупатели и баланс
# ──────────────────────────────────────────────────────────────────────────

def upsert_user(tg_id: int, *, username: str | None, first_name: str | None,
                last_name: str | None) -> dict:
    now = _now()
    with _conn() as c:
        c.execute(
            "INSERT INTO mini_users (tg_id, username, first_name, last_name, created_at, last_seen_at)"
            " VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(tg_id) DO UPDATE SET username=excluded.username,"
            "  first_name=excluded.first_name, last_name=excluded.last_name,"
            "  last_seen_at=excluded.last_seen_at",
            (tg_id, username, first_name, last_name, now, now))
        return dict(c.execute("SELECT * FROM mini_users WHERE tg_id=?", (tg_id,)).fetchone())


def get_user(tg_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM mini_users WHERE tg_id=?", (tg_id,)).fetchone()
        return dict(row) if row else None


def claim_gift(tg_id: int, amount: int) -> bool:
    """Начисляет подарок один раз на человека. False — подарок уже получен."""
    with _conn() as c:
        cur = c.execute(
            "UPDATE mini_users SET balance=balance+?, gift_claimed=1"
            " WHERE tg_id=? AND gift_claimed=0", (amount, tg_id))
        return cur.rowcount > 0


def reserve(tg_id: int, n: int) -> bool:
    """Списывает n сообщений, если на балансе хватает. False — не хватает."""
    if n <= 0:
        return True
    with _conn() as c:
        cur = c.execute(
            "UPDATE mini_users SET balance=balance-? WHERE tg_id=? AND balance>=?",
            (n, tg_id, n))
        return cur.rowcount > 0


def credit(tg_id: int, n: int) -> None:
    if n <= 0:
        return
    with _conn() as c:
        c.execute("UPDATE mini_users SET balance=balance+? WHERE tg_id=?", (n, tg_id))


# ──────────────────────────────────────────────────────────────────────────
#  Канал покупателя и текст рассылки
# ──────────────────────────────────────────────────────────────────────────

def set_channel_building(tg_id: int, channel: str, kind: str = "channel") -> None:
    """channel — имя канала (kind 'channel') либо описание продукта или ниши (kind 'topic')."""
    with _conn() as c:
        same = c.execute("SELECT 1 FROM mini_users WHERE tg_id=? AND channel=? AND channel_kind=?",
                         (tg_id, channel, kind)).fetchone() is not None
        c.execute("UPDATE mini_users SET channel=?, channel_kind=?, channel_status='building',"
                  " channel_error=NULL, offer_text=NULL WHERE tg_id=?", (channel, kind, tg_id))
        if not same:    # другой канал или ниша: прежние клиенты и поиск не подходят
            c.execute("DELETE FROM mini_leads WHERE tg_id=? AND status='new'", (tg_id,))
            c.execute("UPDATE mini_users SET parse_status=NULL, parse_runs=0, parse_finished_at=NULL WHERE tg_id=?",
                      (tg_id,))


def set_channel_ready(tg_id: int, text: str) -> None:
    with _conn() as c:
        c.execute("UPDATE mini_users SET channel_status='ready', channel_error=NULL, offer_text=? WHERE tg_id=?",
                  (text, tg_id))


def set_channel_failed(tg_id: int, error: str) -> None:
    with _conn() as c:
        c.execute("UPDATE mini_users SET channel_status='failed', channel_error=? WHERE tg_id=?",
                  (error[:300], tg_id))


def set_offer_text(tg_id: int, text: str) -> None:
    with _conn() as c:
        c.execute("UPDATE mini_users SET offer_text=? WHERE tg_id=?", (text, tg_id))


def building_channels() -> list[dict]:
    """Покупатели, у которых канал читался в момент остановки сервиса: их надо запустить заново."""
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT tg_id, channel, channel_kind FROM mini_users WHERE channel_status='building'")]


# ──────────────────────────────────────────────────────────────────────────
#  Умный парсинг под канал или нишу покупателя и его клиенты
# ──────────────────────────────────────────────────────────────────────────

def parse_enqueue(tg_id: int) -> None:
    """Поставить поиск клиентов в очередь (повторный вызов не сбрасывает уже идущий поиск)."""
    with _conn() as c:
        c.execute("UPDATE mini_users SET parse_status='queued', parse_queued_at=? WHERE tg_id=?"
                  " AND COALESCE(parse_status,'') NOT IN ('queued','running')", (_now(), tg_id))


def parse_claim() -> dict | None:
    """Берёт из очереди самого раннего покупателя и помечает его «ищем»."""
    with _conn() as c:
        row = c.execute("SELECT * FROM mini_users WHERE parse_status='queued' ORDER BY parse_queued_at LIMIT 1").fetchone()
        if row is None:
            return None
        cur = c.execute("UPDATE mini_users SET parse_status='running' WHERE tg_id=? AND parse_status='queued'",
                        (row["tg_id"],))
        return dict(row) if cur.rowcount else None


def parse_enqueue_missing() -> int:
    """Покупатели с готовым каналом или нишей, для которых поиск клиентов ещё ни разу не ставили
    (подключились до появления умного парсинга в приложении)."""
    with _conn() as c:
        return c.execute(
            "UPDATE mini_users SET parse_status='queued', parse_queued_at=? WHERE channel_status='ready'"
            " AND parse_status IS NULL", (_now(),)).rowcount


def parse_recover() -> int:
    """После перезапуска сервиса прерванный поиск идёт заново."""
    with _conn() as c:
        return c.execute("UPDATE mini_users SET parse_status='queued' WHERE parse_status='running'").rowcount


def parse_finish(tg_id: int, status: str) -> None:
    with _conn() as c:
        c.execute("UPDATE mini_users SET parse_status=?, parse_finished_at=?, parse_runs=parse_runs+1 WHERE tg_id=?",
                  (status, _now(), tg_id))


def parse_topup_candidates(min_new: int, interval_s: float, max_runs: int) -> list[int]:
    """Покупатели с балансом, у которых клиенты почти кончились, а прошлый поиск был давно."""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=interval_s)).isoformat()
    with _conn() as c:
        rows = c.execute(
            "SELECT u.tg_id FROM mini_users u WHERE u.channel_status='ready' AND u.balance > 0"
            " AND u.parse_status IN ('done','failed') AND u.parse_runs < ?"
            " AND COALESCE(u.parse_finished_at,'') < ?"
            " AND (SELECT COUNT(*) FROM mini_leads l WHERE l.tg_id=u.tg_id AND l.status='new') < ?",
            (max_runs, cutoff, min_new)).fetchall()
        return [int(r["tg_id"]) for r in rows]


def add_leads(tg_id: int, leads: list[dict]) -> int:
    """Новые клиенты покупателя; кто уже есть в списке, не дублируется. Возвращает, сколько добавлено."""
    n = 0
    with _conn() as c:
        for ld in leads:
            uname = (ld.get("username") or "").strip().lstrip("@").lower()
            if not uname:
                continue
            cur = c.execute(
                "INSERT OR IGNORE INTO mini_leads (tg_id, username, user_id, name, tier, interest, why, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (tg_id, uname, ld.get("user_id"), ld.get("name"), ld.get("tier"),
                 float(ld.get("interest") or 0), (ld.get("why") or "")[:300], _now()))
            n += cur.rowcount
    return n


def leads_new(tg_id: int) -> int:
    with _conn() as c:
        row = c.execute("SELECT COUNT(*) n FROM mini_leads WHERE tg_id=? AND status='new'", (tg_id,)).fetchone()
        return int(row["n"])


def next_lead(tg_id: int) -> dict | None:
    """Самый горячий из ещё не тронутых клиентов покупателя (уровень A, потом B, потом C)."""
    with _conn() as c:
        row = c.execute("SELECT * FROM mini_leads WHERE tg_id=? AND status='new'"
                        " ORDER BY COALESCE(tier,'C'), interest DESC, id LIMIT 1", (tg_id,)).fetchone()
        return dict(row) if row else None


def mark_lead(lead_id: int, status: str) -> None:
    with _conn() as c:
        c.execute("UPDATE mini_leads SET status=? WHERE id=?", (status, lead_id))


# ──────────────────────────────────────────────────────────────────────────
#  Нажатия: одно нажатие — одно сообщение
# ──────────────────────────────────────────────────────────────────────────

def stream_job_id(tg_id: int) -> str:
    return f"tap-{tg_id}"


def ensure_stream(tg_id: int) -> str:
    """Метка, по которой диалоги принадлежат покупателю: sends.job_id всех его нажатий."""
    job = stream_job_id(tg_id)
    with _conn() as c:
        if not c.execute("SELECT 1 FROM mini_campaigns WHERE job_id=?", (job,)).fetchone():
            c.execute("INSERT INTO mini_campaigns (tg_id, job_id, text, total, reserved, status, created_at)"
                      " VALUES (?,?,?,?,?,?,?)", (tg_id, job, "", 0, 0, "stream", _now()))
    return job


def enqueue_tap(tg_id: int) -> int:
    with _conn() as c:
        cur = c.execute("INSERT INTO mini_taps (tg_id, created_at) VALUES (?,?)", (tg_id, _now()))
        return int(cur.lastrowid)


def pending_taps(tg_id: int) -> int:
    with _conn() as c:
        row = c.execute("SELECT COUNT(*) n FROM mini_taps WHERE tg_id=? AND status IN ('queued','sending')",
                        (tg_id,)).fetchone()
        return int(row["n"])


def sent_total(tg_id: int) -> int:
    with _conn() as c:
        row = c.execute("SELECT COUNT(*) n FROM mini_taps WHERE tg_id=? AND status='sent'", (tg_id,)).fetchone()
        return int(row["n"])


def claim_next_tap() -> dict | None:
    """Берёт самое старое нажатие из очереди и помечает его «отправляется»."""
    with _conn() as c:
        row = c.execute("SELECT * FROM mini_taps WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
        if row is None:
            return None
        cur = c.execute("UPDATE mini_taps SET status='sending' WHERE id=? AND status='queued'", (row["id"],))
        return dict(row) if cur.rowcount else None


def release_tap(tap_id: int) -> None:
    """Вернуть нажатие в очередь (нет свободного аккаунта, сбой аккаунта)."""
    with _conn() as c:
        c.execute("UPDATE mini_taps SET status='queued' WHERE id=? AND status='sending'", (tap_id,))


def recover_taps() -> int:
    """После перезапуска сервиса «отправляющиеся» нажатия снова в очередь."""
    with _conn() as c:
        return c.execute("UPDATE mini_taps SET status='queued' WHERE status='sending'").rowcount


def finish_tap(tap_id: int, *, status: str, target: str | None = None,
               account: str | None = None, error: str | None = None) -> dict | None:
    """Закрывает нажатие. При ошибке сообщение возвращается на баланс. Идемпотентно."""
    with _conn() as c:
        row = c.execute("SELECT * FROM mini_taps WHERE id=?", (tap_id,)).fetchone()
        if row is None or row["status"] in ("sent", "failed"):
            return None
        c.execute("UPDATE mini_taps SET status=?, target=?, account=?, error=?, done_at=? WHERE id=?",
                  (status, target, account, (error or "")[:300] or None, _now(), tap_id))
        if status == "failed":
            c.execute("UPDATE mini_users SET balance=balance+1 WHERE tg_id=?", (row["tg_id"],))
        return dict(row)


def expired_taps(max_age_s: float) -> list[dict]:
    """Нажатия, которые слишком долго ждут свободного аккаунта."""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max_age_s)).isoformat()
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM mini_taps WHERE status='queued' AND created_at < ?", (cutoff,))]


def sending_targets() -> set[str]:
    with _conn() as c:
        return {str(r["target"]).lower().lstrip("@") for r in c.execute(
            "SELECT target FROM mini_taps WHERE status='sending' AND target IS NOT NULL")}


# ──────────────────────────────────────────────────────────────────────────
#  Диалоги покупателя
# ──────────────────────────────────────────────────────────────────────────

_OWNED = (" FROM inbox_conversations ic"
          " JOIN sends s ON s.id = ic.first_send_id"
          " JOIN mini_campaigns m ON m.job_id = s.job_id")


def user_conversations(tg_id: int) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT ic.id, ic.username, ic.display_name, ic.last_message_at,"
            "       ic.last_message_text, ic.last_message_from_me, ic.unread,"
            "       EXISTS(SELECT 1 FROM inbox_messages im WHERE im.conversation_id = ic.id"
            "              AND im.direction='in' AND im.status != 'deleted') AS replied"
            + _OWNED + " WHERE m.tg_id=? ORDER BY ic.last_message_at DESC LIMIT 300",
            (tg_id,)).fetchall()
        return [dict(r) for r in rows]


def conversation_owner(conv_id: int) -> int | None:
    with _conn() as c:
        row = c.execute("SELECT m.tg_id" + _OWNED + " WHERE ic.id=?", (conv_id,)).fetchone()
        return int(row["tg_id"]) if row else None


def mark_edited(conv_id: int, tg_id: int) -> None:
    with _conn() as c:
        c.execute("INSERT INTO mini_msg_edits (conversation_id, tg_id, edited_at) VALUES (?,?,?)"
                  " ON CONFLICT(conversation_id, tg_id) DO UPDATE SET edited_at=excluded.edited_at",
                  (conv_id, tg_id, _now()))


def edited_ids(conv_id: int) -> set[int]:
    with _conn() as c:
        rows = c.execute("SELECT tg_id FROM mini_msg_edits WHERE conversation_id=?", (conv_id,)).fetchall()
        return {int(r["tg_id"]) for r in rows}


def refresh_last_message(conv_id: int) -> None:
    """После правки или удаления пересчитывает «последнее сообщение» диалога для списка чатов."""
    with _conn() as c:
        row = c.execute(
            "SELECT text, sent_at, direction FROM inbox_messages"
            " WHERE conversation_id=? AND status NOT IN ('deleted','failed')"
            " ORDER BY sent_at DESC, id DESC LIMIT 1", (conv_id,)).fetchone()
        if row:
            c.execute("UPDATE inbox_conversations SET last_message_text=?, last_message_at=?,"
                      " last_message_from_me=? WHERE id=?",
                      ((row["text"] or "")[:400], row["sent_at"], 1 if row["direction"] == "out" else 0, conv_id))


def unread_total(tg_id: int) -> int:
    with _conn() as c:
        row = c.execute("SELECT COALESCE(SUM(ic.unread),0) n" + _OWNED + " WHERE m.tg_id=?",
                        (tg_id,)).fetchone()
        return int(row["n"] or 0)


def inbound_since(last_id: int, limit: int = 200) -> list[dict]:
    """Новые входящие в диалогах покупателей — для уведомлений от бота."""
    with _conn() as c:
        rows = c.execute(
            "SELECT im.id msg_id, im.text, ic.id conv_id, ic.display_name, ic.username, m.tg_id"
            " FROM inbox_messages im"
            " JOIN inbox_conversations ic ON ic.id = im.conversation_id"
            " JOIN sends s ON s.id = ic.first_send_id"
            " JOIN mini_campaigns m ON m.job_id = s.job_id"
            " WHERE im.direction='in' AND im.id > ? ORDER BY im.id LIMIT ?",
            (last_id, limit)).fetchall()
        return [dict(r) for r in rows]


def max_inbound_id() -> int:
    with _conn() as c:
        row = c.execute("SELECT COALESCE(MAX(id),0) n FROM inbox_messages").fetchone()
        return int(row["n"] or 0)


# ──────────────────────────────────────────────────────────────────────────
#  Поддержка: покупатель видит просто «Поддержку», оператор отвечает из Telegram через бота
# ──────────────────────────────────────────────────────────────────────────

def support_add(tg_id: int, direction: str, text: str) -> int:
    with _conn() as c:
        cur = c.execute("INSERT INTO mini_support (tg_id, direction, text, created_at) VALUES (?,?,?,?)",
                        (tg_id, direction, text, _now()))
        return int(cur.lastrowid)


def support_messages(tg_id: int, limit: int = 300) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id, direction, text, created_at FROM mini_support WHERE tg_id=?"
            " ORDER BY id DESC LIMIT ?", (tg_id, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]


def support_unread(tg_id: int) -> int:
    with _conn() as c:
        row = c.execute("SELECT COUNT(*) n FROM mini_support WHERE tg_id=? AND direction='out' AND read=0",
                        (tg_id,)).fetchone()
        return int(row["n"])


def support_mark_read(tg_id: int) -> None:
    with _conn() as c:
        c.execute("UPDATE mini_support SET read=1 WHERE tg_id=? AND direction='out' AND read=0", (tg_id,))


def support_undelivered(limit: int = 50) -> list[dict]:
    """Сообщения покупателей, которые ещё не дошли до оператора."""
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT s.id, s.tg_id, s.text, u.first_name, u.last_name, u.username"
            " FROM mini_support s LEFT JOIN mini_users u ON u.tg_id = s.tg_id"
            " WHERE s.direction='in' AND s.delivered=0 ORDER BY s.id LIMIT ?", (limit,))]


def support_mark_delivered(msg_id: int, copies: list[tuple[int, int]]) -> None:
    """copies — (чат оператора, номер сообщения у него) для каждого оператора, кому ушла копия."""
    with _conn() as c:
        c.execute("UPDATE mini_support SET delivered=1 WHERE id=?", (msg_id,))
        c.executemany("INSERT OR REPLACE INTO mini_support_ops (op_chat_id, op_msg_id, support_id) VALUES (?,?,?)",
                      [(chat, mid, msg_id) for chat, mid in copies])


def support_find_by_operator_msg(op_chat_id: int, op_msg_id: int) -> dict | None:
    """Сообщение покупателя, копия которого пришла оператору под этим номером."""
    with _conn() as c:
        row = c.execute(
            "SELECT s.* FROM mini_support_ops o JOIN mini_support s ON s.id = o.support_id"
            " WHERE o.op_chat_id=? AND o.op_msg_id=?", (op_chat_id, op_msg_id)).fetchone()
        return dict(row) if row else None


def operator_ids() -> list[int]:
    """Telegram-аккаунты операторов, которые уже открывали бота (id запоминаем при первом сообщении)."""
    with _conn() as c:
        return [int(r["value"]) for r in c.execute(
            "SELECT value FROM mini_state WHERE key LIKE 'operator:%'") if str(r["value"]).isdigit()]


# ──────────────────────────────────────────────────────────────────────────
#  Платежи
# ──────────────────────────────────────────────────────────────────────────

def create_payment(tg_id: int, *, pack_id: str, messages: int, amount: int,
                   currency: str, method: str, payload: str) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO mini_payments (tg_id, pack_id, messages, amount, currency, method, payload, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (tg_id, pack_id, messages, amount, currency, method, payload, _now()))
        return int(cur.lastrowid)


def get_payment_by_payload(payload: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM mini_payments WHERE payload=?", (payload,)).fetchone()
        return dict(row) if row else None


def mark_paid(payload: str, *, charge_id: str | None) -> dict | None:
    """Отмечает платёж оплаченным и начисляет сообщения. Идемпотентно: второй
    вызов по тому же платежу (повтор вебхука) ничего не начисляет и вернёт None."""
    with _conn() as c:
        row = c.execute("SELECT * FROM mini_payments WHERE payload=?", (payload,)).fetchone()
        if row is None or row["status"] == "paid":
            return None
        c.execute("UPDATE mini_payments SET status='paid', charge_id=?, paid_at=? WHERE id=?",
                  (charge_id, _now(), row["id"]))
        c.execute("UPDATE mini_users SET balance=balance+? WHERE tg_id=?",
                  (row["messages"], row["tg_id"]))
        out = dict(row)
        out["status"] = "paid"
        return out


def has_paid(tg_id: int) -> bool:
    with _conn() as c:
        return c.execute("SELECT 1 FROM mini_payments WHERE tg_id=? AND status='paid' LIMIT 1",
                         (tg_id,)).fetchone() is not None


def list_payments(tg_id: int, limit: int = 50) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id, pack_id, messages, amount, currency, method, status, created_at, paid_at"
            " FROM mini_payments WHERE tg_id=? AND status IN ('paid','failed')"
            " ORDER BY id DESC LIMIT ?", (tg_id, limit)).fetchall()
        return [dict(r) for r in rows]


# ──────────────────────────────────────────────────────────────────────────
#  Служебное
# ──────────────────────────────────────────────────────────────────────────

def state_get(key: str) -> str | None:
    with _conn() as c:
        row = c.execute("SELECT value FROM mini_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None


def state_set(key: str, value: str) -> None:
    with _conn() as c:
        c.execute("INSERT INTO mini_state (key, value) VALUES (?,?)"
                  " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
