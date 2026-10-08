"""SQLite-кеш умного парсинга (data/smart_parser.db).

Зачем: повторный запуск не читает Telegram заново и не тратит вызовы LLM на
уже проверенные сообщения. Отдельный файл, чтобы не трогать схему data/database.db.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from .models import Author, Message, Source

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    chat_id INTEGER PRIMARY KEY,
    title TEXT, username TEXT, kind TEXT, about TEXT, members INTEGER,
    linked_chat_id INTEGER, is_member INTEGER DEFAULT 0,
    last_msg_id INTEGER DEFAULT 0, harvested_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS run_sources (
    channel TEXT, chat_id INTEGER, found_via TEXT, hits INTEGER, meta_score REAL,
    status TEXT, reason TEXT, updated_at TEXT,
    PRIMARY KEY (channel, chat_id)
);
CREATE TABLE IF NOT EXISTS messages (
    source_id INTEGER, msg_id INTEGER, is_post INTEGER DEFAULT 0,
    post_id INTEGER DEFAULT 0, date TEXT, sender_id INTEGER, sender_kind TEXT,
    text TEXT, reply_to INTEGER DEFAULT 0, is_fwd INTEGER DEFAULT 0,
    PRIMARY KEY (source_id, msg_id, is_post)
);
CREATE INDEX IF NOT EXISTS ix_messages_src_date ON messages(source_id, date);
CREATE TABLE IF NOT EXISTS authors (
    user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, last_name TEXT,
    is_bot INTEGER, is_deleted INTEGER, premium INTEGER, status TEXT, was_online TEXT,
    about TEXT, about_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS llm_cache (
    key TEXT PRIMARY KEY, result TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS client_audience (
    channel TEXT, user_id INTEGER, updated_at TEXT,
    PRIMARY KEY (channel, user_id)
);
CREATE TABLE IF NOT EXISTS chat_members (
    chat_id INTEGER, user_id INTEGER,
    PRIMARY KEY (chat_id, user_id)
);
CREATE INDEX IF NOT EXISTS ix_chat_members_user ON chat_members(user_id);
CREATE TABLE IF NOT EXISTS client_seeds (
    channel TEXT, chat_id INTEGER, score REAL, depth INTEGER DEFAULT 0, updated_at TEXT,
    PRIMARY KEY (channel, chat_id)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT, started_at TEXT,
    finished_at TEXT, out_path TEXT, stats TEXT
);
"""


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _now() -> str:
    return iso(datetime.now(timezone.utc))


class Store:
    def __init__(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.c = sqlite3.connect(path, timeout=15)
        self.c.row_factory = sqlite3.Row
        self.c.execute("PRAGMA journal_mode=WAL")
        self.c.executescript(SCHEMA)
        self._migrate()
        self.c.commit()

    def _migrate(self) -> None:
        cols = {r["name"] for r in self.c.execute("PRAGMA table_info(sources)")}
        acols = {r["name"] for r in self.c.execute("PRAGMA table_info(authors)")}
        if "access_hash" not in acols:
            self.c.execute("ALTER TABLE authors ADD COLUMN access_hash INTEGER DEFAULT 0")
        for col, typ in (("junk_reason", "TEXT"), ("junk_at", "TEXT"),
                         # каталог чатов: результат пробы, общий для всех клиентов
                         ("lang", "TEXT"), ("lang_share", "REAL"), ("activity", "REAL"),
                         ("probe_terms", "TEXT"), ("graph_hints", "TEXT"), ("probed_at", "TEXT"),
                         ("members_at", "TEXT"), ("productive", "INTEGER DEFAULT 0")):
            if col not in cols:
                self.c.execute(f"ALTER TABLE sources ADD COLUMN {col} {typ}")

    def close(self) -> None:
        self.c.close()

    # ── источники ──────────────────────────────────────────────────────────

    def upsert_source(self, s: Source) -> None:
        self.c.execute(
            "INSERT INTO sources (chat_id, title, username, kind, about, members, linked_chat_id,"
            " is_member, updated_at) VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, username=excluded.username,"
            " kind=excluded.kind, about=CASE WHEN excluded.about != '' THEN excluded.about ELSE about END,"
            " members=CASE WHEN excluded.members > 0 THEN excluded.members ELSE members END,"
            " linked_chat_id=CASE WHEN excluded.linked_chat_id != 0 THEN excluded.linked_chat_id"
            " ELSE linked_chat_id END,"
            " is_member=MAX(is_member, excluded.is_member), updated_at=excluded.updated_at",
            (s.chat_id, s.title, s.username, s.kind, s.about or "", s.members or 0,
             s.linked_chat_id or 0, int(s.is_member), _now()))
        self.c.commit()

    def get_source(self, chat_id: int) -> Optional[Source]:
        r = self.c.execute("SELECT * FROM sources WHERE chat_id=?", (chat_id,)).fetchone()
        return self._source_row(r) if r else None

    @staticmethod
    def _source_row(r) -> Source:
        return Source(chat_id=r["chat_id"], title=r["title"] or "", username=r["username"] or "",
                      kind=r["kind"] or "group", about=r["about"] or "", members=r["members"] or 0,
                      linked_chat_id=r["linked_chat_id"] or 0, is_member=bool(r["is_member"]),
                      last_msg_id=r["last_msg_id"] or 0, harvested_at=r["harvested_at"] or "")

    def save_probe(self, chat_id: int, lang: str, lang_share: float, activity: float,
                   terms: list, hints: list) -> None:
        self.c.execute(
            "UPDATE sources SET lang=?, lang_share=?, activity=?, probe_terms=?, graph_hints=?,"
            " probed_at=? WHERE chat_id=?",
            (lang, lang_share, activity, " ".join(terms), json.dumps(hints, ensure_ascii=False),
             _now(), chat_id))
        self.c.commit()

    def get_probe(self, chat_id: int, max_age_hours: float) -> Optional[dict]:
        """Свежий результат пробы или None."""
        r = self.c.execute("SELECT lang, lang_share, activity, probe_terms, graph_hints, probed_at"
                           " FROM sources WHERE chat_id=?", (chat_id,)).fetchone()
        if not r or not r["probed_at"]:
            return None
        if datetime.now(timezone.utc) - parse_iso(r["probed_at"]) > timedelta(hours=max_age_hours):
            return None
        try:
            hints = json.loads(r["graph_hints"] or "[]")
        except ValueError:
            hints = []
        return {"lang": r["lang"] or "", "lang_share": r["lang_share"] or 0.0,
                "activity": r["activity"] or 0.0, "terms": (r["probe_terms"] or "").split(),
                "hints": hints}

    def catalog(self, max_age_days: int) -> list:
        """Каталог: публичные чаты, которые уже пробовали (для любых клиентов), без мусорных.
        Возвращает [(Source, термины пробы)]."""
        cutoff = iso(datetime.now(timezone.utc) - timedelta(days=max_age_days))
        rows = self.c.execute(
            "SELECT * FROM sources WHERE probed_at >= ? AND username != '' AND probe_terms != ''"
            " AND productive = 1 AND (junk_reason IS NULL OR junk_reason = '')", (cutoff,)).fetchall()
        return [(self._source_row(r), (r["probe_terms"] or "").split()) for r in rows]

    # ── участники чатов (для пересечений) ─────────────────────────────────

    def save_members(self, chat_id: int, ids) -> None:
        self.c.execute("DELETE FROM chat_members WHERE chat_id=?", (chat_id,))
        self.c.executemany("INSERT OR IGNORE INTO chat_members (chat_id, user_id) VALUES (?,?)",
                           [(chat_id, int(u)) for u in ids])
        self.c.execute("UPDATE sources SET members_at=? WHERE chat_id=?", (_now(), chat_id))
        self.c.commit()

    def members_fresh(self, chat_id: int, ttl_days: int) -> bool:
        r = self.c.execute("SELECT members_at FROM sources WHERE chat_id=?", (chat_id,)).fetchone()
        if not r or not r["members_at"]:
            return False
        return datetime.now(timezone.utc) - parse_iso(r["members_at"]) < timedelta(days=ttl_days)

    def members(self, chat_ids) -> dict:
        """chat_id → множество user_id участников (что есть в кеше)."""
        out: dict = {}
        ids = list(chat_ids)
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            marks = ",".join("?" * len(chunk))
            for r in self.c.execute(f"SELECT chat_id, user_id FROM chat_members WHERE chat_id IN ({marks})",
                                    chunk):
                out.setdefault(r["chat_id"], set()).add(r["user_id"])
        return out

    def save_seed(self, channel: str, chat_id: int, score: float, depth: int = 0) -> None:
        """Зерно клиента: хороший чат, от которого стоит идти по графу в следующих запусках."""
        self.c.execute(
            "INSERT INTO client_seeds (channel, chat_id, score, depth, updated_at) VALUES (?,?,?,?,?)"
            " ON CONFLICT(channel, chat_id) DO UPDATE SET score=MAX(score, excluded.score),"
            " depth=MIN(depth, excluded.depth), updated_at=excluded.updated_at",
            (channel.lower(), chat_id, float(score), int(depth), _now()))
        self.c.commit()

    def clear_seeds(self, channel: str) -> int:
        cur = self.c.execute("DELETE FROM client_seeds WHERE channel=?", (channel.lower(),))
        self.c.commit()
        return cur.rowcount or 0

    def load_seeds(self, channel: str) -> list:
        """[(Source, score)] зёрна клиента, без ставших мусорными."""
        rows = self.c.execute(
            "SELECT s.*, z.score AS zscore FROM client_seeds z JOIN sources s ON s.chat_id = z.chat_id"
            " WHERE z.channel=? AND (s.junk_reason IS NULL OR s.junk_reason = '')"
            " ORDER BY z.score DESC", (channel.lower(),)).fetchall()
        return [(self._source_row(r), r["zscore"] or 0.0) for r in rows]

    def mark_productive(self, chat_id: int) -> None:
        """Чат дал людей выше порога: попадает в каталог для любых клиентов."""
        self.c.execute("UPDATE sources SET productive=1 WHERE chat_id=?", (chat_id,))
        self.c.commit()

    def mark_junk(self, chat_id: int, reason: str) -> None:
        self.c.execute("UPDATE sources SET junk_reason=?, junk_at=? WHERE chat_id=?",
                       (reason, _now(), chat_id))
        self.c.commit()

    def junk_reason(self, chat_id: int, recheck_days: int) -> str:
        """Причина, по которой чат ранее признан мусорным (пусто — не мусор или пора перепроверить)."""
        r = self.c.execute("SELECT junk_reason, junk_at FROM sources WHERE chat_id=?",
                           (chat_id,)).fetchone()
        if not r or not r["junk_reason"] or not r["junk_at"]:
            return ""
        if datetime.now(timezone.utc) - parse_iso(r["junk_at"]) > timedelta(days=recheck_days):
            return ""
        return r["junk_reason"]

    def set_harvested(self, chat_id: int, last_msg_id: int) -> None:
        self.c.execute("UPDATE sources SET last_msg_id=MAX(last_msg_id, ?), harvested_at=? "
                       "WHERE chat_id=?", (last_msg_id, _now(), chat_id))
        self.c.commit()

    def save_decision(self, channel: str, s: Source) -> None:
        self.c.execute(
            "INSERT INTO run_sources (channel, chat_id, found_via, hits, meta_score, status, reason,"
            " updated_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(channel, chat_id) DO UPDATE SET"
            " found_via=excluded.found_via, hits=excluded.hits, meta_score=excluded.meta_score,"
            " status=excluded.status, reason=excluded.reason, updated_at=excluded.updated_at",
            (channel.lower(), s.chat_id, s.found_via, s.hits, s.meta_score, s.status, s.reason, _now()))
        self.c.commit()

    def load_decisions(self, channel: str) -> list[Source]:
        rows = self.c.execute(
            "SELECT s.*, r.found_via, r.hits, r.meta_score, r.status, r.reason FROM run_sources r"
            " JOIN sources s ON s.chat_id = r.chat_id WHERE r.channel=?", (channel.lower(),)).fetchall()
        out = []
        for r in rows:
            s = self._source_row(r)
            s.found_via, s.hits = r["found_via"] or "", r["hits"] or 0
            s.meta_score, s.status, s.reason = r["meta_score"] or 0.0, r["status"] or "", r["reason"] or ""
            out.append(s)
        return out

    # ── сообщения ──────────────────────────────────────────────────────────

    def add_messages(self, msgs: Iterable[Message]) -> int:
        rows = [(m.source_id, m.msg_id, int(m.is_post), m.post_id, iso(m.date), m.sender_id,
                 m.sender_kind, m.text, m.reply_to, int(m.is_fwd)) for m in msgs]
        if not rows:
            return 0
        before = self.c.total_changes
        self.c.executemany(
            "INSERT OR IGNORE INTO messages (source_id, msg_id, is_post, post_id, date, sender_id,"
            " sender_kind, text, reply_to, is_fwd) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        self.c.commit()
        return self.c.total_changes - before

    def max_comment_id(self, source_id: int, post_id: int) -> int:
        r = self.c.execute("SELECT MAX(msg_id) m FROM messages WHERE source_id=? AND post_id=?"
                           " AND is_post=0", (source_id, post_id)).fetchone()
        return int(r["m"] or 0)

    @staticmethod
    def _msg_row(r) -> Message:
        return Message(source_id=r["source_id"], msg_id=r["msg_id"], date=parse_iso(r["date"]),
                       sender_id=r["sender_id"] or 0, sender_kind=r["sender_kind"] or "none",
                       text=r["text"] or "", reply_to=r["reply_to"] or 0, post_id=r["post_id"] or 0,
                       is_post=bool(r["is_post"]), is_fwd=bool(r["is_fwd"]))

    def load_messages(self, source_ids: list[int], since: datetime) -> list[Message]:
        out: list[Message] = []
        for i in range(0, len(source_ids), 400):
            chunk = source_ids[i:i + 400]
            marks = ",".join("?" * len(chunk))
            for r in self.c.execute(
                    f"SELECT * FROM messages WHERE is_post=0 AND date >= ? AND source_id IN ({marks})",
                    [iso(since)] + chunk):
                out.append(self._msg_row(r))
        return out

    def load_posts(self, source_ids: list[int]) -> dict[tuple[int, int], str]:
        out: dict[tuple[int, int], str] = {}
        for i in range(0, len(source_ids), 400):
            chunk = source_ids[i:i + 400]
            marks = ",".join("?" * len(chunk))
            for r in self.c.execute(
                    f"SELECT source_id, msg_id, text FROM messages WHERE is_post=1"
                    f" AND source_id IN ({marks})", chunk):
                out[(r["source_id"], r["msg_id"])] = r["text"] or ""
        return out

    # ── авторы ─────────────────────────────────────────────────────────────

    def upsert_authors(self, authors: Iterable[Author]) -> None:
        rows = [(a.user_id, a.username, a.first_name, a.last_name, int(a.is_bot), int(a.is_deleted),
                 int(a.premium), a.status, a.was_online, int(a.access_hash or 0), _now()) for a in authors]
        if not rows:
            return
        self.c.executemany(
            "INSERT INTO authors (user_id, username, first_name, last_name, is_bot, is_deleted,"
            " premium, status, was_online, access_hash, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,"
            " first_name=excluded.first_name, last_name=excluded.last_name, is_bot=excluded.is_bot,"
            " is_deleted=excluded.is_deleted, premium=excluded.premium, status=excluded.status,"
            " was_online=excluded.was_online,"
            " access_hash=CASE WHEN excluded.access_hash != 0 THEN excluded.access_hash ELSE access_hash END,"
            " updated_at=excluded.updated_at", rows)
        self.c.commit()

    def get_authors(self, ids) -> dict[int, Author]:
        ids = list({int(i) for i in ids if i})
        out: dict[int, Author] = {}
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            marks = ",".join("?" * len(chunk))
            for r in self.c.execute(f"SELECT * FROM authors WHERE user_id IN ({marks})", chunk):
                out[r["user_id"]] = Author(
                    user_id=r["user_id"], username=r["username"] or "", first_name=r["first_name"] or "",
                    last_name=r["last_name"] or "", is_bot=bool(r["is_bot"]),
                    is_deleted=bool(r["is_deleted"]), premium=bool(r["premium"]),
                    status=r["status"] or "", was_online=r["was_online"] or "",
                    about=r["about"], about_at=r["about_at"] or "",
                    access_hash=r["access_hash"] or 0)
        return out

    def set_about(self, user_id: int, about: str) -> None:
        self.c.execute("UPDATE authors SET about=?, about_at=? WHERE user_id=?",
                       (about or "", _now(), user_id))
        self.c.commit()

    # ── аудитория клиента ──────────────────────────────────────────────────

    def save_audience(self, channel: str, ids) -> None:
        ch = channel.lower()
        self.c.execute("DELETE FROM client_audience WHERE channel=?", (ch,))
        now = _now()
        self.c.executemany("INSERT OR IGNORE INTO client_audience (channel, user_id, updated_at)"
                           " VALUES (?,?,?)", [(ch, int(i), now) for i in ids])
        self.c.commit()

    def add_audience(self, channel: str, user_id: int) -> None:
        self.c.execute("INSERT OR REPLACE INTO client_audience (channel, user_id, updated_at)"
                       " VALUES (?,?,?)", (channel.lower(), int(user_id), _now()))
        self.c.commit()

    def load_audience(self, channel: str, max_age_hours: float) -> tuple[set, bool]:
        """(множество id, свежий ли список). Пустой список свежим не считается."""
        rows = self.c.execute("SELECT user_id, updated_at FROM client_audience WHERE channel=?",
                              (channel.lower(),)).fetchall()
        ids = {r["user_id"] for r in rows}
        if not rows:
            return ids, False
        oldest = min(parse_iso(r["updated_at"]) for r in rows)
        fresh = datetime.now(timezone.utc) - oldest < timedelta(hours=max_age_hours)
        return ids, fresh

    # ── LLM-кеш ────────────────────────────────────────────────────────────

    def llm_get(self, key: str) -> Optional[dict]:
        r = self.c.execute("SELECT result FROM llm_cache WHERE key=?", (key,)).fetchone()
        if not r:
            return None
        try:
            return json.loads(r["result"])
        except ValueError:
            return None

    def llm_put(self, key: str, result: dict) -> None:
        self.c.execute("INSERT OR REPLACE INTO llm_cache (key, result, created_at) VALUES (?,?,?)",
                       (key, json.dumps(result, ensure_ascii=False), _now()))
        self.c.commit()

    # ── обслуживание ───────────────────────────────────────────────────────

    def purge(self, days: int) -> int:
        """Удаляет сообщения старше N дней: минимум хранимых данных о людях."""
        cutoff = iso(datetime.now(timezone.utc) - timedelta(days=days))
        cur = self.c.execute("DELETE FROM messages WHERE date < ?", (cutoff,))
        # списки участников старше срока хранения — тоже удаляем
        self.c.execute("DELETE FROM chat_members WHERE chat_id IN"
                       " (SELECT chat_id FROM sources WHERE members_at IS NOT NULL AND members_at < ?)",
                       (cutoff,))
        self.c.execute("DELETE FROM authors WHERE user_id NOT IN"
                       " (SELECT DISTINCT sender_id FROM messages)"
                       " AND user_id NOT IN (SELECT DISTINCT user_id FROM chat_members)")
        self.c.commit()
        return cur.rowcount or 0

    def start_run(self, channel: str) -> int:
        cur = self.c.execute("INSERT INTO runs (channel, started_at) VALUES (?,?)",
                             (channel.lower(), _now()))
        self.c.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, out_path: str, stats: dict) -> None:
        self.c.execute("UPDATE runs SET finished_at=?, out_path=?, stats=? WHERE id=?",
                       (_now(), out_path, json.dumps(stats, ensure_ascii=False, default=str), run_id))
        self.c.commit()
