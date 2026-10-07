"""Параметры запуска умного парсинга: всё, что можно поменять из CLI или меню."""
from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass

_HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "smart_parser.db")
PROFILES_DIR = os.path.join(DATA_DIR, "smart_profiles")
OUT_DIR = os.path.join(DATA_DIR, "smart_leads")


def normalize_channel(raw: str) -> str:
    """@name, t.me/name, https://t.me/s/name → name."""
    s = (raw or "").strip()
    s = re.sub(r"^(?:https?://)?(?:www\.)?t(?:elegram)?\.me/", "", s, flags=re.IGNORECASE)
    if s.startswith("s/"):
        s = s[2:]
    return s.split("?")[0].strip("/").lstrip("@")


@dataclass
class Params:
    channel: str                      # канал клиента: @name или ссылка
    brief: str = ""                   # путь к файлу с брифом или сам текст брифа
    days: int = 14                    # окно свежести сообщений
    # источники
    max_sources: int = 30             # сколько источников читать
    min_members: int = 300
    max_members: int = 500_000
    max_queries: int = 10             # поисковых запросов по названиям чатов
    search_limit: int = 20            # результатов на запрос
    max_seeds: int = 15               # ссылок из постов клиента, которые резолвим
    max_dialogs: int = 300            # сколько своих диалогов просматривать
    dialog_search_queries: int = 8    # фраз покупателя для поиска по своим чатам
    dialog_search_limit: int = 30
    sources_file: str = ""            # ручной список ссылок на чаты (по одной на строку)
    include_own: bool = False         # читать комментарии своего канала клиента
    # сбор
    msgs_per_source: int = 500
    posts_per_channel: int = 30
    comments_per_post: int = 200
    profile_posts: int = 80
    wait_time: float = 1.0            # пауза между пачками сообщений Telethon, сек
    max_flood_wait: int = 300         # дольше этого FloodWait не ждём, пропускаем шаг
    # LLM (только бесплатный облачный, через api/llm.py)
    no_llm: bool = False
    llm_budget: int = 25              # максимум вызовов LLM за запуск
    llm_batch: int = 20               # сообщений в одном вызове
    llm_delay: float = 4.0            # пауза между вызовами (бесплатные лимиты по RPM)
    # локальные эмбеддинги (бесплатно, но тяжёлая зависимость; по умолчанию выкл.)
    embeddings: bool = False
    embeddings_model: str = "paraphrase-multilingual-MiniLM-L12-v2"
    # авторы
    enrich_top: int = 150             # скольким авторам запрашивать био
    # скоринг
    tau_hours: float = 72.0           # «период полураспада» свежести
    min_intent: float = 0.25
    min_rel: float = 0.15
    tier_a: float = 25.0              # стартовые пороги, уточняются по разметке (evaluate.py)
    tier_b: float = 10.0
    top: int = 500                    # максимум строк на листе «Люди»
    # служебное
    session: str = ""                 # имя .session; пусто — первая
    out: str = ""                     # путь к xlsx; пусто — data/smart_leads/...
    offline: bool = False             # без Telegram: пересчёт по уже собранному
    rebuild_profile: bool = False
    check_contacted: bool = True      # исключать тех, кому уже писали (data/database.db)
    retention_days: int = 30          # хранить собранные сообщения не дольше
    db_path: str = DB_PATH
    profiles_dir: str = PROFILES_DIR

    def __post_init__(self) -> None:
        self.channel = normalize_channel(self.channel)

    def brief_text(self) -> str:
        b = (self.brief or "").strip()
        if b and len(b) < 400 and os.path.isfile(b):
            with open(b, "r", encoding="utf-8") as f:
                return f.read().strip()
        return b

    def profile_path(self) -> str:
        return os.path.join(self.profiles_dir, f"{self.channel.lower()}.json")

    def to_dict(self) -> dict:
        return asdict(self)
