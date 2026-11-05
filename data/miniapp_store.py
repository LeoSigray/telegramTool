"""
data/miniapp_store.py — хранилище Mini App «Клиент в один клик».

Покупатель (Telegram-пользователь, открывший Mini App), его баланс сообщений,
кампании и платежи. Таблицы живут в той же data/database.db, что и рассылки
(sends) и диалоги (inbox_*). Покупатель видит только свои диалоги: связь идёт
через sends.job_id его кампаний (inbox_conversations.first_send_id → sends.id).

Баланс меняется только атомарными UPDATE … WHERE balance >= n, чтобы два
одновременных запуска не ушли в минус.
"""
import json
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
                read        INTEGER NOT NULL DEFAULT 0,   -- out: покупатель прочитал
                op_read     INTEGER NOT NULL DEFAULT 0    -- in: оператор открыл обращение или ответил
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
                          ("parse_finished_at", "TEXT"), ("parse_runs", "INTEGER NOT NULL DEFAULT 0"),
                          ("lang", "TEXT"),
                          # ── воронка покупателя ──────────────────────────────────
                          # Считаем по событиям, а не запросом на лету: нажатий и платежей
                          # со временем станет много, а смотреть эти цифры нужно часто.
                          ("taps_total", "INTEGER NOT NULL DEFAULT 0"),       # всего нажатий
                          ("free_spent", "INTEGER NOT NULL DEFAULT 0"),       # из них подарочными
                          ("first_purchase", "INTEGER NOT NULL DEFAULT 0"),   # 0/1 — дошёл ли до оплаты
                          ("first_purchase_at", "TEXT"),
                          ("purchases_count", "INTEGER NOT NULL DEFAULT 0"),
                          ("packs_bought", "TEXT"),                           # JSON: сколько раз взят каждый пакет
                          ("first_sent_at", "TEXT"),                          # первое дошедшее сообщение
                          # Среднее держим как сумму и счётчик: так его можно обновлять
                          # по одному событию, не перечитывая всю историю.
                          ("send_time_total_s", "REAL NOT NULL DEFAULT 0"),
                          ("send_time_n", "INTEGER NOT NULL DEFAULT 0"),
                          ("sessions_count", "INTEGER NOT NULL DEFAULT 0"),
                          ("session_total_s", "REAL NOT NULL DEFAULT 0"),
                          ("session_open_at", "TEXT"),                        # текущая незакрытая сессия
                          ("session_ping_at", "TEXT")):                       # последний признак жизни
            if col not in have:
                c.execute(f"ALTER TABLE mini_users ADD COLUMN {col} {kind}")
        if "op_read" not in {r["name"] for r in c.execute("PRAGMA table_info(mini_support)")}:
            c.execute("ALTER TABLE mini_support ADD COLUMN op_read INTEGER NOT NULL DEFAULT 0")
            c.execute("UPDATE mini_support SET op_read=1")        # всё, что было до появления панели, уже прочитано
        # Человек найден, но отправить некем: отмечаем момент, когда воркер уткнулся в пустой пул.
        # Только в этом случае покупателю предлагается забрать попытку — обычный поиск человека
        # идёт как шёл, без кнопок, иначе мы бы дёргали его на каждом нажатии.
        if "stalled_at" not in {r["name"] for r in c.execute("PRAGMA table_info(mini_taps)")}:
            c.execute("ALTER TABLE mini_taps ADD COLUMN stalled_at TEXT")


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


def set_lang(tg_id: int, lang: str) -> None:
    """Язык интерфейса покупателя: ru или en. Пока не выбран, поле пустое."""
    with _conn() as c:
        c.execute("UPDATE mini_users SET lang=? WHERE tg_id=?", (lang, tg_id))


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
    """Покупатели, у которых канал читался в момент остановки сервиса: их надо запустить заново.

    Пустой статус считаем тем же «читаем»: именно так его показывает мини-апп
    (channel_status or "building"), и если сервер думает иначе, покупатель смотрит
    на вечный спиннер, за которым никто не работает.
    """
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT tg_id, channel, channel_kind FROM mini_users"
            " WHERE channel IS NOT NULL AND (channel_status='building' OR channel_status IS NULL)")]


# ──────────────────────────────────────────────────────────────────────────
#  Умный парсинг под канал или нишу покупателя и его клиенты
# ──────────────────────────────────────────────────────────────────────────

def next_search() -> int | None:
    """Покупатель, у которого нажатий больше, чем найденных людей: для него ищем следующих.

    Поиск запускается только нажатием: нажатие без готового человека — это человек, которого надо найти."""
    with _conn() as c:
        row = c.execute(
            "SELECT u.tg_id FROM mini_users u WHERE u.channel_status='ready'"
            " AND (SELECT COUNT(*) FROM mini_taps t WHERE t.tg_id=u.tg_id AND t.status='queued')"
            "   > (SELECT COUNT(*) FROM mini_leads l WHERE l.tg_id=u.tg_id AND l.status='new')"
            " ORDER BY (SELECT MIN(t.id) FROM mini_taps t WHERE t.tg_id=u.tg_id AND t.status='queued')"
            " LIMIT 1").fetchone()
        return int(row["tg_id"]) if row else None


def next_prewarm(min_leads: int) -> int | None:
    """Покупатель, которому стоит найти человека ЗАРАНЕЕ, пока он не нажал.

    Первый поиск по новому каналу — самый долгий: надо построить профиль, найти чаты,
    прочитать их. Если ждать нажатия, покупатель смотрит на спиннер несколько минут и
    уходит. Но между вводом канала и первым нажатием он читает и правит текст рассылки —
    это свободное время, за которое человек успевает найтись.

    Берём того, у кого канал готов, нажатий нет (иначе им займётся next_search) и
    свободных людей меньше min_leads. Когда запас наберётся, условие перестанет
    выполняться само.
    """
    with _conn() as c:
        row = c.execute(
            "SELECT u.tg_id FROM mini_users u WHERE u.channel_status='ready'"
            " AND (SELECT COUNT(*) FROM mini_taps t WHERE t.tg_id=u.tg_id AND t.status='queued') = 0"
            " AND (SELECT COUNT(*) FROM mini_leads l WHERE l.tg_id=u.tg_id AND l.status='new') < ?"
            " ORDER BY u.tg_id LIMIT 1", (min_leads,)).fetchone()
        return int(row["tg_id"]) if row else None


def search_need(tg_id: int) -> int:
    """Сколько людей ещё надо найти: ждущих нажатий минус уже найденные и не тронутые."""
    with _conn() as c:
        row = c.execute(
            "SELECT (SELECT COUNT(*) FROM mini_taps WHERE tg_id=? AND status='queued')"
            " - (SELECT COUNT(*) FROM mini_leads WHERE tg_id=? AND status='new') n", (tg_id, tg_id)).fetchone()
        return max(0, int(row["n"]))


def parse_set(tg_id: int, status: str | None) -> None:
    """Состояние поиска для экрана: running — ищем человека, done/failed — последний поиск закончен."""
    with _conn() as c:
        if status == "running":
            c.execute("UPDATE mini_users SET parse_status='running', parse_queued_at=? WHERE tg_id=?", (_now(), tg_id))
        else:
            c.execute("UPDATE mini_users SET parse_status=?, parse_finished_at=?, parse_runs=parse_runs+1 WHERE tg_id=?",
                      (status, _now(), tg_id))


def parse_recover() -> int:
    """После перезапуска сервиса прерванный поиск помечаем законченным: нажатие в очереди запустит его снова."""
    with _conn() as c:
        return c.execute("UPDATE mini_users SET parse_status='failed' WHERE parse_status='running'").rowcount


def known_leads(tg_id: int) -> set[str]:
    """Все люди, которых уже находили для покупателя (кому писали и кому не получилось)."""
    with _conn() as c:
        return {r["username"] for r in c.execute("SELECT username FROM mini_leads WHERE tg_id=?", (tg_id,))}


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
        _bump_tap_counters(c, tg_id)
        return int(cur.lastrowid)


def pending_taps(tg_id: int) -> int:
    with _conn() as c:
        row = c.execute("SELECT COUNT(*) n FROM mini_taps WHERE tg_id=? AND status IN ('queued','sending')",
                        (tg_id,)).fetchone()
        return int(row["n"])


def transit(tg_id: int) -> list[dict]:
    """Сообщения «в пути»: нажатия, которые ещё не ушли. stage: search — ищем человека, send — человек найден,
    сообщение отправляется. Людей находят в порядке нажатий, поэтому «отправляются» самые старые.

    waiting_s — сколько нажатие уже ждёт.

    can_cancel — предлагать ли забрать попытку назад. Только когда человек найден, а
    отправить им некем (пул пуст): ждать нечего, и решать должен покупатель. Пока идёт
    обычный поиск, кнопки нет — это штатная работа, и дёргать человека незачем.
    """
    now = datetime.now(timezone.utc)
    with _conn() as c:
        taps = c.execute("SELECT id, created_at, status, stalled_at FROM mini_taps"
                         " WHERE tg_id=? AND status IN ('queued','sending') ORDER BY id", (tg_id,)).fetchall()
        ready = c.execute("SELECT COUNT(*) n FROM mini_leads WHERE tg_id=? AND status='new'", (tg_id,)).fetchone()["n"]
    out = []
    for i, t in enumerate(taps):
        try:
            waited = (now - datetime.fromisoformat(t["created_at"])).total_seconds()
        except Exception:  # noqa: BLE001 — кривая дата не должна прятать карточку
            waited = 0.0
        out.append({"id": t["id"], "at": t["created_at"], "stage": "send" if i < ready else "search",
                    "waiting_s": int(waited),
                    # отменять можно только застрявшее и только пока оно не пошло в Telegram
                    "can_cancel": t["status"] == "queued" and bool(t["stalled_at"])})
    return out


def cancel_tap(tap_id: int, tg_id: int) -> bool:
    """Покупатель сам отменил застрявшее сообщение: возвращаем попытку на баланс.

    Отменить можно только своё, только пока отправка не началась и только если воркер
    уже уткнулся в пустой пул (stalled_at). Правило держится здесь, а не на фронте:
    иначе любой запрос к API забирал бы попытку у нажатия, которое спокойно ищет человека.
    Идемпотентно — повторный вызов ничего не начислит.
    """
    with _conn() as c:
        cur = c.execute("UPDATE mini_taps SET status='canceled', error=?, done_at=?"
                        " WHERE id=? AND tg_id=? AND status='queued' AND stalled_at IS NOT NULL",
                        ("отменено покупателем", _now(), tap_id, tg_id))
        if not cur.rowcount:
            return False
        c.execute("UPDATE mini_users SET balance=balance+1 WHERE tg_id=?", (tg_id,))
        return True


def sent_total(tg_id: int) -> int:
    with _conn() as c:
        row = c.execute("SELECT COUNT(*) n FROM mini_taps WHERE tg_id=? AND status='sent'", (tg_id,)).fetchone()
        return int(row["n"])


# ──────────────────────────────────────────────────────────────────────────
#  Воронка покупателя: нажатия, покупки, время до отправки, сессии
# ──────────────────────────────────────────────────────────────────────────

SESSION_IDLE_S = 90.0          # столько тишины — и считаем, что мини-апп закрыли


def _bump_tap_counters(c, tg_id: int) -> None:
    """Нажатие: +1 к общему счётчику и, пока покупок не было, к бесплатным.

    Бесплатным считаем всё, что потрачено до первой оплаты: подарочные сообщения и есть
    та проба, по которой человек решает, покупать ли. После оплаты счётчик замирает.
    """
    c.execute("UPDATE mini_users SET taps_total=taps_total+1,"
              " free_spent=free_spent+(CASE WHEN first_purchase=0 THEN 1 ELSE 0 END)"
              " WHERE tg_id=?", (tg_id,))


def note_sent(tg_id: int, tap_created_at: str | None) -> None:
    """Сообщение дошло: запоминаем первое и копим время от нажатия до отправки.

    Меряем именно весь путь, вместе с поиском человека и ожиданием аккаунта: покупателя
    интересует, когда его сообщение уйдёт, а не сколько заняла наша последняя операция.
    """
    secs = None
    if tap_created_at:
        try:
            secs = (datetime.now(timezone.utc) - datetime.fromisoformat(tap_created_at)).total_seconds()
        except Exception:  # noqa: BLE001 — кривая дата не повод терять сам факт отправки
            secs = None
    with _conn() as c:
        if secs is not None and secs >= 0:
            c.execute("UPDATE mini_users SET send_time_total_s=send_time_total_s+?, send_time_n=send_time_n+1"
                      " WHERE tg_id=?", (secs, tg_id))
        c.execute("UPDATE mini_users SET first_sent_at=? WHERE tg_id=? AND first_sent_at IS NULL",
                  (_now(), tg_id))


def note_purchase(tg_id: int, pack_id: str) -> None:
    """Оплата прошла: +1 к покупкам, отметка первой и счётчик по конкретному пакету."""
    with _conn() as c:
        row = c.execute("SELECT packs_bought FROM mini_users WHERE tg_id=?", (tg_id,)).fetchone()
        try:
            packs = json.loads((row["packs_bought"] if row else None) or "{}")
            if not isinstance(packs, dict):
                packs = {}
        except (json.JSONDecodeError, TypeError):
            packs = {}
        packs[pack_id] = int(packs.get(pack_id, 0)) + 1
        c.execute("UPDATE mini_users SET purchases_count=purchases_count+1, packs_bought=?,"
                  " first_purchase=1,"
                  " first_purchase_at=COALESCE(first_purchase_at, ?)"
                  " WHERE tg_id=?", (json.dumps(packs, ensure_ascii=False), _now(), tg_id))


def session_open(tg_id: int) -> None:
    """Мини-апп открыли. Незакрытую прошлую сессию сперва закрываем по последнему пингу."""
    with _conn() as c:
        row = c.execute("SELECT session_open_at, session_ping_at FROM mini_users WHERE tg_id=?",
                        (tg_id,)).fetchone()
        if row and row["session_open_at"]:
            _close_session(c, tg_id, row["session_open_at"], row["session_ping_at"])
        c.execute("UPDATE mini_users SET session_open_at=?, session_ping_at=? WHERE tg_id=?",
                  (_now(), _now(), tg_id))


def session_ping(tg_id: int) -> None:
    """Признак жизни: по нему закроем сессию, если приложение исчезнет без прощания."""
    with _conn() as c:
        c.execute("UPDATE mini_users SET session_ping_at=? WHERE tg_id=? AND session_open_at IS NOT NULL",
                  (_now(), tg_id))


def session_close(tg_id: int) -> None:
    with _conn() as c:
        row = c.execute("SELECT session_open_at, session_ping_at FROM mini_users WHERE tg_id=?",
                        (tg_id,)).fetchone()
        if row and row["session_open_at"]:
            _close_session(c, tg_id, row["session_open_at"], row["session_ping_at"])


def _close_session(c, tg_id: int, opened: str, last_ping: str | None) -> None:
    """Длительность считаем до последнего пинга, а не до «сейчас».

    Мобильный вебвью часто уходит молча: событие закрытия не доходит. Если брать текущее
    время, в среднее попадут часы висящей вкладки — поэтому верим последнему признаку жизни.
    """
    try:
        start = datetime.fromisoformat(opened)
        end = datetime.fromisoformat(last_ping) if last_ping else start
        secs = max((end - start).total_seconds(), 0.0)
    except Exception:  # noqa: BLE001 — битые даты не должны ронять закрытие сессии
        secs = 0.0
    c.execute("UPDATE mini_users SET sessions_count=sessions_count+1, session_total_s=session_total_s+?,"
              " session_open_at=NULL, session_ping_at=NULL WHERE tg_id=?", (secs, tg_id))


def close_stale_sessions() -> int:
    """Сессии, от которых давно нет признаков жизни: приложение закрыли, не сказав нам."""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=SESSION_IDLE_S)).isoformat()
    with _conn() as c:
        rows = c.execute("SELECT tg_id, session_open_at, session_ping_at FROM mini_users"
                         " WHERE session_open_at IS NOT NULL"
                         "   AND COALESCE(session_ping_at, session_open_at) < ?", (cutoff,)).fetchall()
        for r in rows:
            _close_session(c, r["tg_id"], r["session_open_at"], r["session_ping_at"])
        return len(rows)


def all_users() -> list[dict]:
    """Все покупатели — для сводной аналитики оператора."""
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT tg_id, username, first_name FROM mini_users"
                                           " ORDER BY tg_id")]


def funnel(tg_id: int, pack_ids: list[str] | None = None) -> dict:
    """Воронка одного покупателя. packs — счётчики в порядке pack_ids, как просили: [1, 4, 2]."""
    with _conn() as c:
        row = c.execute("SELECT * FROM mini_users WHERE tg_id=?", (tg_id,)).fetchone()
    if row is None:
        return {}
    d = dict(row)
    try:
        bought = json.loads(d.get("packs_bought") or "{}")
    except (json.JSONDecodeError, TypeError):
        bought = {}
    ids = pack_ids or sorted(bought)
    return {
        "tg_id": tg_id,
        "taps_total": d.get("taps_total") or 0,
        "free_spent": d.get("free_spent") or 0,
        "first_purchase": int(d.get("first_purchase") or 0),
        "first_purchase_at": d.get("first_purchase_at"),
        "purchases_count": d.get("purchases_count") or 0,
        "packs": [int(bought.get(p, 0)) for p in ids],
        "packs_by_id": {p: int(bought.get(p, 0)) for p in ids},
        "first_sent_at": d.get("first_sent_at"),
        "avg_send_s": round((d["send_time_total_s"] / d["send_time_n"]), 1) if d.get("send_time_n") else None,
        "sessions_count": d.get("sessions_count") or 0,
        "avg_session_s": round((d["session_total_s"] / d["sessions_count"]), 1) if d.get("sessions_count") else None,
    }


def recompute_funnel(tg_id: int | None = None) -> int:
    """Пересчёт счётчиков из первоисточников: нажатий и платежей.

    Нужен для тех, кто пользовался сервисом до появления этих полей, и как страховка от
    рассинхрона: событийные счётчики быстры, но историей правды остаются сами таблицы.
    Сессии не трогаем — их до сих пор никто не записывал, восстановить неоткуда.
    """
    with _conn() as c:
        ids = [tg_id] if tg_id else [r["tg_id"] for r in c.execute("SELECT tg_id FROM mini_users")]
        for uid in ids:
            taps = c.execute("SELECT COUNT(*) n FROM mini_taps WHERE tg_id=?", (uid,)).fetchone()["n"]
            pays = c.execute("SELECT pack_id, paid_at FROM mini_payments WHERE tg_id=? AND status='paid'"
                             " ORDER BY paid_at", (uid,)).fetchall()
            packs: dict[str, int] = {}
            for p in pays:
                packs[p["pack_id"]] = packs.get(p["pack_id"], 0) + 1
            first_pay = pays[0]["paid_at"] if pays else None
            # до первой оплаты человек тратил подарочные — именно это и есть бесплатная проба
            free = c.execute("SELECT COUNT(*) n FROM mini_taps WHERE tg_id=?"
                             " AND (? IS NULL OR created_at < ?)", (uid, first_pay, first_pay)).fetchone()["n"]
            sent = c.execute("SELECT created_at, done_at FROM mini_taps WHERE tg_id=? AND status='sent'"
                             " AND done_at IS NOT NULL ORDER BY done_at", (uid,)).fetchall()
            total_s, n = 0.0, 0
            for s in sent:
                try:
                    dt = (datetime.fromisoformat(s["done_at"]) - datetime.fromisoformat(s["created_at"])).total_seconds()
                except Exception:  # noqa: BLE001
                    continue
                if dt >= 0:
                    total_s += dt
                    n += 1
            c.execute("UPDATE mini_users SET taps_total=?, free_spent=?, purchases_count=?, packs_bought=?,"
                      " first_purchase=?, first_purchase_at=?, first_sent_at=?,"
                      " send_time_total_s=?, send_time_n=? WHERE tg_id=?",
                      (taps, free, len(pays), json.dumps(packs, ensure_ascii=False),
                       1 if pays else 0, first_pay,
                       sent[0]["done_at"] if sent else None, total_s, n, uid))
        return len(ids)


def claim_next_tap() -> dict | None:
    """Берёт самое старое нажатие, для которого человек уже найден, и помечает его «отправляется»."""
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM mini_taps t WHERE t.status='queued'"
            " AND EXISTS (SELECT 1 FROM mini_leads l WHERE l.tg_id=t.tg_id AND l.status='new')"
            " ORDER BY t.id LIMIT 1").fetchone()
        if row is None:
            return None
        cur = c.execute("UPDATE mini_taps SET status='sending' WHERE id=? AND status='queued'", (row["id"],))
        return dict(row) if cur.rowcount else None


def release_tap(tap_id: int, *, stalled: bool = False) -> None:
    """Вернуть нажатие в очередь (нет свободного аккаунта, сбой аккаунта).

    stalled — человек уже найден, но отправить им некем. Это единственный случай, когда
    покупателю показывают «Отменить»: ждать тут нечего, пул пуст, и решать должен он.
    Обычный поиск человека такой отметки не получает — он идёт своим чередом.
    """
    with _conn() as c:
        c.execute("UPDATE mini_taps SET status='queued', stalled_at=? WHERE id=? AND status='sending'",
                  (_now() if stalled else None, tap_id))


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
        out = dict(row)
    if status == "sent":
        note_sent(out["tg_id"], out.get("created_at"))
    return out


def expired_taps(max_age_s: float) -> list[dict]:
    """Нажатия, которые слишком долго ждут (свободного аккаунта или поиска человека)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max_age_s)).isoformat()
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM mini_taps WHERE status='queued' AND created_at < ?", (cutoff,))]


def oldest_waiting_tap(tg_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM mini_taps WHERE tg_id=? AND status='queued' ORDER BY id LIMIT 1",
                        (tg_id,)).fetchone()
        return dict(row) if row else None


def fail_waiting(tg_id: int, count: int, why: str) -> int:
    """Нужных людей не нашли: столько же ждущих нажатий не уйдёт, сообщения возвращаются на баланс.
    Закрываем самые новые: старые дождутся людей, которых уже нашли."""
    with _conn() as c:
        ids = [r["id"] for r in c.execute(
            "SELECT id FROM mini_taps WHERE tg_id=? AND status='queued' ORDER BY id DESC LIMIT ?", (tg_id, count))]
    return sum(1 for i in ids if finish_tap(i, status="failed", error=why))


def newest_queued_tap(tg_id: int) -> int:
    """Номер самого нового ждущего нажатия (0 — таких нет): граница того, под что идёт текущий поиск."""
    with _conn() as c:
        row = c.execute("SELECT COALESCE(MAX(id), 0) n FROM mini_taps WHERE tg_id=? AND status='queued'",
                        (tg_id,)).fetchone()
        return int(row["n"])


def fail_waiting(tg_id: int, count: int, why: str, upto_id: int | None = None) -> int:
    """Нужных людей не нашли: столько же ждущих нажатий не уйдёт, сообщения возвращаются на баланс.
    Закрываем самые новые из тех, под которые шёл поиск (upto_id): старые дождутся найденных людей."""
    with _conn() as c:
        ids = [r["id"] for r in c.execute(
            "SELECT id FROM mini_taps WHERE tg_id=? AND status='queued' AND id<=? ORDER BY id DESC LIMIT ?",
            (tg_id, upto_id if upto_id is not None else 1 << 62, count))]
    return sum(1 for i in ids if finish_tap(i, status="failed", error=why))


def last_failed_tap(tg_id: int) -> dict | None:
    """Последнее нажатие, которое не ушло (сообщение вернулось на баланс): экран говорит почему."""
    with _conn() as c:
        row = c.execute("SELECT id, error, done_at FROM mini_taps WHERE tg_id=? AND status='failed'"
                        " ORDER BY id DESC LIMIT 1", (tg_id,)).fetchone()
        return dict(row) if row else None


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
            "SELECT im.id msg_id, im.text, ic.id conv_id, ic.display_name, ic.username, m.tg_id, u.lang"
            " FROM inbox_messages im"
            " JOIN inbox_conversations ic ON ic.id = im.conversation_id"
            " JOIN sends s ON s.id = ic.first_send_id"
            " JOIN mini_campaigns m ON m.job_id = s.job_id"
            " LEFT JOIN mini_users u ON u.tg_id = m.tg_id"
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


def operator_registry() -> list[tuple[str, int]]:
    """(имя без @ в нижнем регистре, id) операторов, которые уже открывали бота или приложение."""
    with _conn() as c:
        return [(str(r["key"]).split(":", 1)[1], int(r["value"])) for r in c.execute(
            "SELECT key, value FROM mini_state WHERE key LIKE 'operator:%'") if str(r["value"]).isdigit()]


def ops_threads(order: str = "new", only_waiting: bool = False, q: str = "", limit: int = 300) -> dict:
    """Обращения в поддержку по людям: у каждого последнее сообщение и сколько у него непрочитанного.

    order: new — сначала те, кто писал позже всех; old — сначала те, кто писал раньше всех.
    only_waiting — только те, кому ещё не ответили (последним писал сам человек)."""
    with _conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT g.tg_id, u.first_name, u.last_name, u.username, m.id last_id, m.direction last_dir,"
            "  m.text last_text, m.created_at last_at,"
            "  (SELECT COUNT(*) FROM mini_support x WHERE x.tg_id=g.tg_id AND x.direction='in' AND x.op_read=0) unread,"
            "  (SELECT COUNT(*) FROM mini_support x WHERE x.tg_id=g.tg_id) total"
            " FROM (SELECT tg_id, MAX(id) mid FROM mini_support GROUP BY tg_id) g"
            " JOIN mini_support m ON m.id = g.mid LEFT JOIN mini_users u ON u.tg_id = g.tg_id")]
    counts = {"all": len(rows), "waiting": sum(1 for r in rows if r["last_dir"] == "in"),
              "unread": sum(1 for r in rows if r["unread"])}
    if only_waiting:
        rows = [r for r in rows if r["last_dir"] == "in"]
    needle = (q or "").strip().lower().lstrip("@")
    if needle:
        rows = [r for r in rows if needle in " ".join(str(r[k] or "") for k in
                                                       ("first_name", "last_name", "username", "last_text")).lower()]
    rows.sort(key=lambda r: r["last_id"], reverse=(order != "old"))
    return {"threads": rows[:limit], "counts": counts}


def ops_unread_threads() -> int:
    """Сколько людей написали в поддержку, а оператор ещё не открывал их обращение."""
    with _conn() as c:
        row = c.execute("SELECT COUNT(DISTINCT tg_id) n FROM mini_support WHERE direction='in' AND op_read=0").fetchone()
        return int(row["n"])


def ops_mark_read(tg_id: int) -> None:
    with _conn() as c:
        c.execute("UPDATE mini_support SET op_read=1 WHERE tg_id=? AND direction='in' AND op_read=0", (tg_id,))


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
    # Счётчики покупок — после закрытия транзакции: mark_paid идемпотентна, сюда мы
    # попадаем только на первом подтверждении платежа, повтор вебхука уходит выше.
    note_purchase(out["tg_id"], out["pack_id"])
    return out


def crypto_orders(statuses: tuple, tg_id: int | None = None, hours: float | None = None) -> list[dict]:
    """Заказы на оплату USDT с нужными статусами (pending, expired…); hours — не старше стольких часов."""
    q = ("SELECT * FROM mini_payments WHERE method='crypto' AND status IN (%s)" % ",".join("?" * len(statuses)))
    args: list = list(statuses)
    if tg_id is not None:
        q += " AND tg_id=?"
        args.append(tg_id)
    if hours is not None:
        q += " AND created_at >= ?"
        args.append((datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat())
    with _conn() as c:
        return [dict(r) for r in c.execute(q + " ORDER BY id", args)]


def crypto_expire(minutes: float) -> int:
    """Неоплаченные заказы старше срока закрываются (сумму ещё можно засчитать позднему переводу)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    with _conn() as c:
        return c.execute("UPDATE mini_payments SET status='expired' WHERE method='crypto' AND status='pending'"
                         " AND created_at < ?", (cutoff,)).rowcount


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
