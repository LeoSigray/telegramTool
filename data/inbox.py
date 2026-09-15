"""
data/inbox.py — хранилище «платформы диалогов».

Живёт в том же SQLite, что и data/db.py / data/analytics.py (data/database.db).

Идея:
  • Рассылка НЕ создаёт диалог. Диалог появляется только когда человек
    ОТВЕТИЛ на рассылку (api/listener.py ловит входящее и зовёт record_inbound).
  • После первого ответа в диалоге копятся ВСЕ сообщения в обе стороны —
    входящие (listener), исходящие с платформы (api/inbox_service.send_reply)
    и исходящие, отправленные вручную из самого Telegram (listener, outgoing).
  • При открытии диалога подтягивается реальная история переписки из Telegram
    (api/inbox_service.hydrate) — чтобы оператор видел весь контекст, а не
    только то, что прошло через платформу.

Таблицы:
  • inbox_conversations — один диалог = один peer (telegram user id)
  • inbox_messages      — лента сообщений диалога
"""

import sqlite3
from datetime import datetime, timedelta, timezone

from data.db import DB_PATH

# История считается свежей столько времени — иначе при открытии диалога
# тянем её из Telegram заново.
HYDRATE_TTL_MINUTES = 10


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _display_name(first: str | None, last: str | None) -> str | None:
    name = ((first or "") + (f" {last}" if last else "")).strip()
    return name or None


# ──────────────────────────────────────────────────────────────────────────
#  Схема
# ──────────────────────────────────────────────────────────────────────────

def init_inbox() -> None:
    """Создаёт таблицы платформы диалогов. Безопасно вызывать повторно."""
    with _conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS inbox_conversations (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                peer_id              TEXT NOT NULL UNIQUE,
                account              TEXT NOT NULL,
                username             TEXT,
                display_name         TEXT,
                phone                TEXT,
                first_send_id        INTEGER,
                niche                TEXT,
                created_at           TEXT NOT NULL,
                last_message_at      TEXT,
                last_message_text    TEXT,
                last_message_from_me INTEGER NOT NULL DEFAULT 0,
                unread               INTEGER NOT NULL DEFAULT 0,
                status               TEXT NOT NULL DEFAULT 'open',
                hydrated_at          TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_inbox_conv_last
                ON inbox_conversations(last_message_at DESC);
            CREATE INDEX IF NOT EXISTS ix_inbox_conv_status
                ON inbox_conversations(status, last_message_at DESC);

            CREATE TABLE IF NOT EXISTS inbox_messages (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                tg_id           INTEGER,
                direction       TEXT NOT NULL,          -- in | out
                text            TEXT,
                account         TEXT,
                status          TEXT NOT NULL DEFAULT 'ok',  -- ok | sending | failed | deleted
                error           TEXT,
                via             TEXT,                  -- platform | telegram | broadcast | history
                sent_at         TEXT NOT NULL,
                created_at      TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS ix_inbox_msg_conv
                ON inbox_messages(conversation_id, sent_at);
            CREATE UNIQUE INDEX IF NOT EXISTS ux_inbox_msg_tg
                ON inbox_messages(conversation_id, tg_id)
                WHERE tg_id IS NOT NULL;
        """)


# ──────────────────────────────────────────────────────────────────────────
#  Диалоги
# ──────────────────────────────────────────────────────────────────────────

def ever_sent_to_peer(peer_id: str) -> bool:
    """Писали ли мы этому человеку рассылку хоть когда-нибудь (без окна 72 ч).
    Нужно, чтобы поздний ответ на рассылку тоже завёл диалог."""
    with _conn() as c:
        return c.execute(
            "SELECT 1 FROM sends WHERE peer_id=? AND status='sent' LIMIT 1",
            (str(peer_id),)).fetchone() is not None


def find_conversation_by_peer(peer_id: str) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM inbox_conversations WHERE peer_id=?",
                      (str(peer_id),)).fetchone()
        return dict(r) if r else None


def get_conversation(conv_id: int) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM inbox_conversations WHERE id=?", (conv_id,)).fetchone()
        return dict(r) if r else None


def _upsert_conversation(c: sqlite3.Connection, *, peer_id: str, account: str,
                         username: str | None, display_name: str | None,
                         phone: str | None, send_id: int | None,
                         niche: str | None) -> int:
    row = c.execute("SELECT id FROM inbox_conversations WHERE peer_id=?",
                    (str(peer_id),)).fetchone()
    if row:
        # обновляем «портрет» контакта, но не трогаем account/статус
        c.execute(
            "UPDATE inbox_conversations SET"
            "  username = COALESCE(?, username),"
            "  display_name = COALESCE(?, display_name),"
            "  phone = COALESCE(?, phone),"
            "  first_send_id = COALESCE(first_send_id, ?),"
            "  niche = COALESCE(niche, ?)"
            " WHERE id=?",
            (username, display_name, phone, send_id, niche, row["id"]),
        )
        return row["id"]
    # last_message_at оставляем NULL — его проставит первый же _touch_last
    # (иначе исторический бэкофилл не смог бы «догнать» свежую метку создания)
    cur = c.execute(
        "INSERT INTO inbox_conversations"
        " (peer_id, account, username, display_name, phone, first_send_id, niche, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (str(peer_id), account, username, display_name, phone, send_id, niche, _now()),
    )
    return cur.lastrowid


def get_or_create_conversation(peer_id: str, account: str, *,
                               username: str | None = None,
                               display_name: str | None = None,
                               phone: str | None = None,
                               send_id: int | None = None,
                               niche: str | None = None) -> tuple[int, bool]:
    """Возвращает (conversation_id, created)."""
    with _conn() as c:
        existed = c.execute("SELECT 1 FROM inbox_conversations WHERE peer_id=?",
                            (str(peer_id),)).fetchone() is not None
        cid = _upsert_conversation(c, peer_id=peer_id, account=account,
                                   username=username, display_name=display_name,
                                   phone=phone, send_id=send_id, niche=niche)
        return cid, (not existed)


def _touch_last(c: sqlite3.Connection, conv_id: int, *, text: str | None,
                sent_at: str, from_me: bool, bump_unread: bool) -> None:
    # last_message_* двигаем только вперёд по времени — history-подкачка
    # старых сообщений не должна перебивать реальный «последний» месседж
    row = c.execute("SELECT last_message_at FROM inbox_conversations WHERE id=?",
                    (conv_id,)).fetchone()
    is_newer = not row or not row["last_message_at"] or sent_at >= row["last_message_at"]
    if is_newer:
        c.execute(
            "UPDATE inbox_conversations SET last_message_at=?, last_message_text=?,"
            " last_message_from_me=?, status=CASE WHEN status='closed' THEN 'open' ELSE status END"
            " WHERE id=?",
            (sent_at, (text or "")[:400], 1 if from_me else 0, conv_id),
        )
    if bump_unread:
        c.execute("UPDATE inbox_conversations SET unread = unread + 1 WHERE id=?", (conv_id,))


def _insert_message(c: sqlite3.Connection, *, conv_id: int, tg_id: int | None,
                    direction: str, text: str | None, account: str | None,
                    status: str, error: str | None, via: str | None,
                    sent_at: str) -> int | None:
    """Вставляет сообщение. При конфликте по tg_id — не дублирует, возвращает None."""
    if tg_id is not None:
        exists = c.execute(
            "SELECT id FROM inbox_messages WHERE conversation_id=? AND tg_id=?",
            (conv_id, tg_id)).fetchone()
        if exists:
            return None
    cur = c.execute(
        "INSERT INTO inbox_messages"
        " (conversation_id, tg_id, direction, text, account, status, error, via, sent_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (conv_id, tg_id, direction, text, account, status, error, via, sent_at),
    )
    return cur.lastrowid


# ──────────────────────────────────────────────────────────────────────────
#  Приём и отправка
# ──────────────────────────────────────────────────────────────────────────

def record_inbound(peer_id: str, account: str, *, tg_id: int | None,
                   text: str | None, sent_at: str | None,
                   username: str | None = None, first_name: str | None = None,
                   last_name: str | None = None, phone: str | None = None,
                   send_id: int | None = None, niche: str | None = None) -> tuple[int, bool]:
    """
    Входящее личное сообщение от человека. Создаёт диалог, если его ещё нет
    (это и есть «чат появляется только после ответа»). Возвращает (conv_id, created).

    Вызывающая сторона (listener) сама решает, тот ли это человек: диалог
    заводим, если сообщение сматчилось с нашей рассылкой (send_id не None)
    ИЛИ диалог с этим peer уже существует.
    """
    stamp = sent_at or _now()
    with _conn() as c:
        existed = c.execute("SELECT 1 FROM inbox_conversations WHERE peer_id=?",
                            (str(peer_id),)).fetchone() is not None
        cid = _upsert_conversation(
            c, peer_id=peer_id, account=account, username=username,
            display_name=_display_name(first_name, last_name), phone=phone,
            send_id=send_id, niche=niche)
        inserted = _insert_message(c, conv_id=cid, tg_id=tg_id, direction="in",
                                   text=text, account=account, status="ok",
                                   error=None, via="telegram", sent_at=stamp)
        if inserted is not None:
            _touch_last(c, cid, text=text, sent_at=stamp, from_me=False, bump_unread=True)
        return cid, (not existed)


def record_outbound(conv_id: int, *, account: str, text: str | None,
                    tg_id: int | None, status: str = "ok",
                    error: str | None = None, via: str = "platform",
                    sent_at: str | None = None) -> int | None:
    """Исходящее сообщение (с платформы или вручную из Telegram)."""
    stamp = sent_at or _now()
    with _conn() as c:
        if not c.execute("SELECT 1 FROM inbox_conversations WHERE id=?", (conv_id,)).fetchone():
            return None
        mid = _insert_message(c, conv_id=conv_id, tg_id=tg_id, direction="out",
                              text=text, account=account, status=status,
                              error=error, via=via, sent_at=stamp)
        if mid is not None and status != "failed":
            _touch_last(c, conv_id, text=text, sent_at=stamp, from_me=True, bump_unread=False)
            # исходящий ход оператора «закрывает» непрочитанное
            c.execute("UPDATE inbox_conversations SET unread=0 WHERE id=?", (conv_id,))
        if status != "failed" and account:
            # диалог теперь ведёт этот аккаунт (важно при фолбэке на другой аккаунт)
            c.execute("UPDATE inbox_conversations SET account=? WHERE id=?", (account, conv_id))
        return mid


def record_outbound_by_peer(peer_id: str, *, account: str, text: str | None,
                            tg_id: int | None, sent_at: str | None = None) -> int | None:
    """Исходящее, пойманное listener'ом из самого Telegram. Пишем только если
    диалог уже существует — вручную начатые переписки платформа не ведёт."""
    conv = find_conversation_by_peer(peer_id)
    if not conv:
        return None
    return record_outbound(conv["id"], account=account, text=text, tg_id=tg_id,
                           via="telegram", sent_at=sent_at)


# ──────────────────────────────────────────────────────────────────────────
#  Правки / удаления (listener)
# ──────────────────────────────────────────────────────────────────────────

def update_message_text(peer_id: str, tg_id: int, text: str | None) -> bool:
    conv = find_conversation_by_peer(peer_id)
    if not conv:
        return False
    with _conn() as c:
        cur = c.execute(
            "UPDATE inbox_messages SET text=? WHERE conversation_id=? AND tg_id=?",
            ((text or ""), conv["id"], tg_id))
        return cur.rowcount > 0


def mark_messages_deleted(peer_id: str | None, tg_ids: list[int]) -> int:
    if not tg_ids:
        return 0
    marks = ",".join("?" * len(tg_ids))
    with _conn() as c:
        if peer_id:
            conv = c.execute("SELECT id FROM inbox_conversations WHERE peer_id=?",
                             (str(peer_id),)).fetchone()
            if not conv:
                return 0
            cur = c.execute(
                f"UPDATE inbox_messages SET status='deleted'"
                f" WHERE conversation_id=? AND tg_id IN ({marks})",
                (conv["id"], *tg_ids))
        else:
            cur = c.execute(
                f"UPDATE inbox_messages SET status='deleted' WHERE tg_id IN ({marks})",
                tg_ids)
        return cur.rowcount


# ──────────────────────────────────────────────────────────────────────────
#  История из Telegram
# ──────────────────────────────────────────────────────────────────────────

def needs_hydration(conv_id: int) -> bool:
    conv = get_conversation(conv_id)
    if not conv:
        return False
    if not conv["hydrated_at"]:
        return True
    try:
        last = datetime.fromisoformat(conv["hydrated_at"])
    except (ValueError, TypeError):
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last > timedelta(minutes=HYDRATE_TTL_MINUTES)


def store_history(conv_id: int, messages: list[dict]) -> int:
    """
    messages: [{tg_id, direction, text, sent_at}] — из client.get_messages.
    Мерджим по tg_id, ничего не затирая. Возвращает число новых строк.
    """
    added = 0
    with _conn() as c:
        if not c.execute("SELECT 1 FROM inbox_conversations WHERE id=?", (conv_id,)).fetchone():
            return 0
        newest = None
        for m in messages:
            mid = _insert_message(
                c, conv_id=conv_id, tg_id=m.get("tg_id"),
                direction=m["direction"], text=m.get("text"),
                account=None, status="ok", error=None, via="history",
                sent_at=m["sent_at"])
            if mid is not None:
                added += 1
            if not newest or m["sent_at"] > newest["sent_at"]:
                newest = m
        c.execute("UPDATE inbox_conversations SET hydrated_at=? WHERE id=?",
                  (_now(), conv_id))
        if newest:
            _touch_last(c, conv_id, text=newest.get("text"), sent_at=newest["sent_at"],
                        from_me=(newest["direction"] == "out"), bump_unread=False)
    return added


# ──────────────────────────────────────────────────────────────────────────
#  Чтение для UI
# ──────────────────────────────────────────────────────────────────────────

def list_conversations(status: str = "open", q: str | None = None,
                       limit: int = 100, offset: int = 0) -> list[dict]:
    query = "SELECT * FROM inbox_conversations WHERE 1=1"
    args: list = []
    if status in ("open", "closed"):
        query += " AND status=?"
        args.append(status)
    query += " ORDER BY last_message_at DESC"
    with _conn() as c:
        rows = [dict(r) for r in c.execute(query, args)]

    # Поиск фильтруем в Python: SQLite lower()/LIKE не знают кириллицу,
    # а str.lower() — знает.
    if q:
        needle = q.lower().strip()
        rows = [
            r for r in rows
            if needle in (r["display_name"] or "").lower()
            or needle in (r["username"] or "").lower()
            or needle in (r["peer_id"] or "")
            or needle in (r["phone"] or "")
        ]

    off = max(0, int(offset))
    lim = max(1, min(int(limit), 500))
    return rows[off:off + lim]


def get_messages(conv_id: int, limit: int = 300) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id, tg_id, direction, text, account, status, error, via, sent_at"
            " FROM inbox_messages WHERE conversation_id=?"
            " ORDER BY sent_at ASC, id ASC LIMIT ?",
            (conv_id, max(1, min(int(limit), 1000)))).fetchall()
        return [dict(r) for r in rows]


def mark_read(conv_id: int) -> None:
    with _conn() as c:
        c.execute("UPDATE inbox_conversations SET unread=0 WHERE id=?", (conv_id,))


def set_status(conv_id: int, status: str) -> bool:
    if status not in ("open", "closed"):
        return False
    with _conn() as c:
        cur = c.execute("UPDATE inbox_conversations SET status=? WHERE id=?",
                        (status, conv_id))
        return cur.rowcount > 0


def stats() -> dict:
    with _conn() as c:
        r = c.execute(
            "SELECT"
            "  SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) open,"
            "  SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) closed,"
            "  SUM(CASE WHEN status='open' AND unread>0 THEN 1 ELSE 0 END) unread_chats,"
            "  COALESCE(SUM(unread),0) unread_total"
            " FROM inbox_conversations").fetchone()
        return {"open": r["open"] or 0, "closed": r["closed"] or 0,
                "unread_chats": r["unread_chats"] or 0,
                "unread_total": r["unread_total"] or 0}


# ──────────────────────────────────────────────────────────────────────────
#  Бэкофилл из истории рассылок
# ──────────────────────────────────────────────────────────────────────────

def seed_from_sends() -> dict:
    """
    Одноразовый посев: по каждой строке sends, где человек ОТВЕТИЛ
    (reply_text заполнен) и известен peer_id — заводим диалог и кладём два
    сообщения: наше (рассылка) и его ответ. Реальная история подтянется
    позже при открытии диалога.
    """
    created = messages = 0
    with _conn() as c:
        rows = c.execute(
            "SELECT id, account, peer_id, target, niche, sent_at, replied_at, reply_text"
            " FROM sends"
            " WHERE reply_text IS NOT NULL AND reply_text <> '' AND peer_id IS NOT NULL"
            "   AND (job_id IS NULL OR job_id <> 'demo-seed')"  # синтетику из демо не тянем
            " ORDER BY sent_at ASC").fetchall()
        for s in rows:
            existed = c.execute("SELECT id FROM inbox_conversations WHERE peer_id=?",
                                (str(s["peer_id"]),)).fetchone()
            cid = _upsert_conversation(
                c, peer_id=s["peer_id"], account=s["account"],
                username=(s["target"].lstrip("@") if s["target"] and not str(s["target"]).isdigit() else None),
                display_name=None, phone=None, send_id=s["id"], niche=s["niche"])
            if existed:
                continue  # диалог уже посеян/живёт — не задваиваем «сиротские» сообщения
            created += 1
            _insert_message(c, conv_id=cid, tg_id=None, direction="out",
                            text="(сообщение рассылки)", account=s["account"],
                            status="ok", error=None, via="broadcast",
                            sent_at=s["sent_at"])
            reply_stamp = s["replied_at"] or s["sent_at"]
            _insert_message(c, conv_id=cid, tg_id=None, direction="in",
                            text=s["reply_text"], account=s["account"],
                            status="ok", error=None, via="broadcast",
                            sent_at=reply_stamp)
            messages += 2
            _touch_last(c, cid, text=s["reply_text"], sent_at=reply_stamp,
                        from_me=False, bump_unread=False)
    return {"conversations_created": created, "messages_added": messages}
