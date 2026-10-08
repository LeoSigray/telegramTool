"""Структуры данных умного парсинга."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class Source:
    """Чат или канал с комментариями, откуда берём людей."""
    chat_id: int
    title: str = ""
    username: str = ""
    kind: str = "group"            # group | channel (канал с обсуждением)
    about: str = ""
    members: int = 0
    linked_chat_id: int = 0
    is_member: bool = False
    last_msg_id: int = 0
    harvested_at: str = ""
    # решение по источнику в рамках конкретного клиента
    found_via: str = ""
    hits: int = 0                  # совпадений поиска фраз покупателя
    meta_score: float = 0.0
    status: str = ""               # queued | selected | rejected | error
    reason: str = ""
    # результат пробы (probe.py)
    lang: str = ""
    lang_share: float = 0.0
    activity: float = 0.0          # сообщений в день
    y_est: float = 0.0             # оценка: запросов по теме в неделю

    def link(self) -> str:
        if self.username:
            return f"https://t.me/{self.username}"
        return f"https://t.me/c/{self.chat_id}"

    def label(self) -> str:
        return f"@{self.username}" if self.username else (self.title or str(self.chat_id))

    def add_via(self, via: str) -> None:
        parts = [p for p in self.found_via.split(",") if p]
        if via not in parts:
            parts.append(via)
        self.found_via = ",".join(parts)


@dataclass
class Message:
    source_id: int
    msg_id: int
    date: datetime
    sender_id: int
    sender_kind: str               # user | channel | none
    text: str
    reply_to: int = 0
    post_id: int = 0               # для комментария — id поста канала
    is_post: bool = False
    is_fwd: bool = False

    @property
    def key(self) -> tuple:
        return (self.source_id, self.msg_id, self.is_post)

    def link(self, src: Source) -> str:
        base = src.link()
        if self.post_id:
            return f"{base}/{self.post_id}?comment={self.msg_id}"
        return f"{base}/{self.msg_id}"


@dataclass
class Author:
    user_id: int
    username: str = ""
    first_name: str = ""
    last_name: str = ""
    is_bot: bool = False
    is_deleted: bool = False
    premium: bool = False
    status: str = ""               # online | recently | last_week | last_month | offline | empty
    was_online: str = ""           # ISO, если статус offline
    about: Optional[str] = None    # None — био ещё не запрашивали
    about_at: str = ""
    access_hash: int = 0           # ключ доступа к профилю (для био и проверок без кеша сессии)

    def input_user(self):
        """InputUser для запросов к Telegram: с ключом доступа, если он известен."""
        if self.access_hash:
            from telethon.tl.types import InputUser
            return InputUser(self.user_id, self.access_hash)
        return self.user_id

    @property
    def name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p).strip()


@dataclass
class Verdict:
    """Ответ LLM по одному сообщению."""
    role: str = "other"            # buyer | seller | other
    is_request: bool = False
    topic: float = 0.0
    specific: float = 0.0
    intent: str = "none"           # vendor | problem | advice | none
    dm: float = 0.0
    why: str = ""
