"""Параметры запуска умного парсинга: всё, что можно поменять из CLI или меню."""
from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field

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
    topic: str = ""                   # у клиента нет канала: продукт или ниша своими словами (тогда channel — условное имя)
    days: int = 14                    # окно свежести сообщений
    # источники
    people: int = 100                 # сколько людей нужно найти (главный параметр)
    # Волна маленькая намеренно: после неё людей проверяют и отдают покупателю. Пять
    # чатов читаются ~75 с, и всё это время он смотрит на спиннер, хотя подходящий
    # человек нашёлся уже в первом. Лучше проверить дважды, чем ждать впятеро дольше.
    wave: int = 2                     # сколько источников читаем за одну волну
    source_cap: int = 60              # предел источников на запуск (защитный)
    overshoot: float = 1.5            # собираем с запасом: после LLM и проверок часть отпадёт
    topup_rounds: int = 5             # сколько раз дочитать волну, если после проверок людей не хватает
    min_pqi: float = 0.0              # порог по PQI запроса: по умолчанию НЕТ (0), решает итоговый «Интерес»
    min_interest: float = 0.0         # порог по итоговому «Интересу» (0 — без порога)
    stop_pqi: float = 10.0            # только чтобы решать, когда хватит читать чаты: считаем людей с PQI от этого
    warm: bool = True                 # «тёплые»: без запроса, но активны в нескольких чатах ниши
    search_messages: bool = True      # искать фразы покупателя по сообщениям публичных чатов
    # Глобальный поиск отдаёт сообщения только из чатов аккаунта. Если их мало, искать
    # бессмысленно: на живом прогоне это 78 с впустую за два раунда при нуле находок.
    search_min_groups: int = 15       # меньше чатов у аккаунта — поиск по сообщениям не запускаем
    # параметры подходящего чата (для любого клиента)
    languages: str = "ru"             # языки чатов через запятую: ru, uk, en, kk, uz…
    min_members_group: int = 100
    min_members_channel: int = 300    # для каналов с комментариями
    max_members: int = 500_000
    min_lang_share: float = 0.5       # доля сообщений на нужном языке
    min_activity: float = 0.3         # сообщений в день (меньше — «мёртвый» чат)
    min_topic_share: float = 0.03     # доля сообщений по теме клиента (меньше — чат не про то)
    search_limit: int = 50            # результатов на запрос (Telegram обычно отдаёт меньше)
    # привязанные чаты каналов: у канала пишет автор, а в его чате — подписчики,
    # поэтому людей ищем именно там (особенно при source=youtube, где приходят одни каналы)
    linked_chats: bool = True
    # Привязанные чаты приходят из каналов, уже прошедших пробу, поэтому их тема
    # подтверждена. Но каждый всё равно стоит запроса: 25 штук давали +67 с на раунд.
    linked_chats_per_round: int = 10  # сколько привязанных чатов раскрывать за раунд
    linked_min_members: int = 25      # порог для них: чат обсуждения всегда меньше канала
    probe_concurrency: int = 10       # сколько чатов пробовать одновременно (они независимы)
    resolve_concurrency: int = 5      # параллельных резолвов @имён: по одному это ~30 с на раунд
    check_cap: int = 60               # сколько кандидатов проверять детально за раунд
    # Проба — это запросы к Telegram на каждый чат, а людей нужно единицы. Предел держит
    # время раунда: на живом прогоне 32 чата стоили 98 с, и ради двух найденных человек.
    probe_cap: int = 15               # сколько лучших кандидатов пробовать за раунд
    total_check_cap: int = 400        # и за весь запуск (каждый — запрос к Telegram)
    # поиск чатов через интернет (статьи, подборки, форумы) вместо поиска Telegram по названиям
    web_search: bool = True           # искать чаты вне Telegram (False — по названиям в Telegram)
    source: str = "youtube"           # где искать вне Telegram: youtube (обучающие видео) или web (статьи)
    yt_videos: int = 20               # сколько АВТОРОВ из выдачи YouTube разбирать на один запрос
                                      # (голос даёт автор, а не видео; вся выдача приходит одним
                                      # ответом, поэтому лимит стоит на авторах)
    web_queries_target: int = 20      # сколько запросов для поисковика нейросеть пишет в начале
    web_queries_per_round: int = 4    # запросов к поисковику за раунд
    web_results: int = 8              # страниц из выдачи на запрос
    web_resolves: int = 25            # сколько найденных @username проверять в Telegram за раунд
    web_ttl_days: int = 7             # кеш выдачи и страниц
    # пул поисковых запросов и раунды
    queries_target: int = 50          # сколько запросов нейросеть генерирует в начале
    queries_per_round: int = 10       # сколько запросов берём в один раунд поиска
    max_rounds: int = 12              # предел раундов поиска за запуск
    max_empty_rounds: int = 3         # стоп после стольких раундов подряд без новых чатов
    query_refills: int = 3            # сколько раз просить нейросеть новые 50 запросов, когда пул кончился
    # проверка слов товара и фраз покупателя глобальным поиском Telegram (один раз на профиль)
    validate: bool = True
    validate_days: int = 90           # совпадения не старше стольких дней
    validate_limit: int = 100         # сообщений на одно слово в проверке
    validate_phrases: int = 30        # сколько фраз покупателя проверять
    # точечное чтение: в чате ищем по словам товара и фразам покупателя, а не читаем всё подряд
    targeted: bool = True
    targeted_terms: int = 6           # (устарело) слов товара + фраз в одном чате
    search_terms: int = 8             # маркеров намерения («ищу», «посоветуйте») на чат:
                                      # по ним поиск внутри чата реально что-то находит,
                                      # в отличие от названий товара и длинных фраз
    targeted_limit: int = 60          # сообщений на один поиск
    targeted_tail: int = 300          # и столько последних сообщений чата подряд: основа выборки,
                                      # правила и LLM отберут из них нужных
    # проба чата перед чтением
    probe_msgs: int = 100             # сколько последних сообщений смотреть
    probe_ttl_hours: int = 24         # не пробовать заново чаще
    # граф: новые чаты из пересылок, ссылок и «похожих каналов» уже найденных хороших чатов
    graph: bool = True
    min_seeds: int = 5                # сколько зёрен нужно, чтобы перейти от поиска к графу
    seed_min_y: float = 1.0           # зерно: чат с плотностью запросов по теме от N в неделю (проба)
    graph_depth: int = 2              # сколько ступеней графа от зёрен из поиска
    graph_seeds: int = 15             # от скольких зёрен расширяться за раунд графа
    graph_resolves: int = 20          # сколько ссылок t.me/@ разрешать за запуск (лимит Telegram)
    # каталог: чаты, найденные для прошлых клиентов
    index_candidates: int = 40
    phrases_per_round: int = 12       # фраз покупателя для поиска по сообщениям за раунд
    phrase_hits: int = 100            # сколько сообщений просматривать на фразу
    phrases_target: int = 50          # сколько фраз покупателя держим в пуле
    # мусорные чаты: если сработал хоть один порог, чат выбрасывается и запоминается
    junk_min_msgs: int = 20           # меньше сообщений — по содержимому не судим
    junk_bots: float = 0.5            # доля сообщений от ботов, каналов, анонимов и пересылок
    junk_links: float = 0.5           # доля сообщений со ссылками, @упоминаниями, телефонами
    junk_dups: float = 0.3            # доля копипаста (один текст повторяется в чате)
    junk_topic: float = 0.15          # доля сообщений с мусорной тематикой (lexicon/junk_chat.txt)
    junk_ads: float = 0.5             # доля рекламы исполнителей
    junk_recheck_days: int = 30       # через сколько дней перепроверить мусорный чат

    # --- Ниша, привязанная к городу (profile.geo) ---
    # Городская барахолка — не помойка, а рынок: там объявления по шаблону и море
    # анонимов, и обычные пороги бракуют самый живой чат города. У Новокузнецка так
    # отпал @chat_nvkzonline42: 15 790 человек, 2400 сообщений в неделю — «копипаст».
    local_junk_dups: float = 0.75     # вместо 0.3: шаблонные объявления в барахолке — норма
    local_junk_bots: float = 0.8      # вместо 0.5: в городских чатах много анонимов и пересылок
    local_probe_cap: int = 30         # вместо 15: городских чатов находится больше сотни
    local_days: int = 90              # вместо 14: в малом городе запрос на услугу редкий
    local_search_terms: int = 16      # вместо 8: иначе обрезается на «ищу/нужен» и не доходит
                                      # до «посоветуйте», «подскажите», «кто делает»
    # пересечения: кто есть в нескольких чатах ниши (списки участников — без вступления в чаты)
    members: bool = True
    members_chats: int = 12           # у скольких лучших чатов ниши брать участников
    members_limit: int = 2000         # сколько участников брать с чата
    members_ttl_days: int = 7         # не обновлять список чаще
    # интерес человека = текст (горячий запрос) + пересечения (близость к нише)
    w_hot: float = 0.65               # вес запроса
    w_aff: float = 0.35               # вес пересечений
    hot_ref: float = 40.0             # PQI, который считаем «максимально горячим»
    min_affinity: float = 0.40        # «тёплый» без запроса: порог близости к нише
    silent_min_chats: int = 3         # «тёплый», который ничего не писал: минимум чатов ниши
    seller_share: float = 0.5         # доля рекламных сообщений, с которой человек — продавец
    interest_a: float = 40.0          # уровни по интересу
    interest_b: float = 20.0
    # аудитория клиента (исключаем тех, кто уже в его группе/канале)
    audience_members: int = 10_000    # сколько участников запрашивать списком
    audience_msgs: int = 3000         # сколько последних комментариев обсуждения смотреть
    audience_check_top: int = 150     # скольких лучших проверять точечно (GetParticipant)
    # сбор
    msgs_per_source: int = 500
    posts_per_channel: int = 30
    comments_per_post: int = 200
    profile_posts: int = 80
    wait_time: float = 0.5            # пауза между пачками сообщений Telethon, сек
    max_flood_wait: int = 300         # дольше этого FloodWait не ждём, пропускаем шаг
    # LLM (только бесплатный облачный, через api/llm.py)
    no_llm: bool = False
    llm_chain: str = ""               # свои провайдеры через запятую: "openrouter:модель,groq:модель"
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
    llm_min_topic: float = 0.3        # если LLM проверил сообщение: тема ниже этого — не запрос клиента
    tier_a: float = 25.0              # стартовые пороги, уточняются по разметке (evaluate.py)
    tier_b: float = 10.0
    reserve: int = 0                  # лист «Запас» (ниже порога): по умолчанию не ведём «Люди»
    # служебное
    session: str = field(default_factory=lambda: os.getenv("SMART_SESSION", ""))  # имя .session; пусто — первая
    out: str = ""                     # путь к xlsx; пусто — data/smart_leads/...
    offline: bool = False             # без Telegram: пересчёт по уже собранному
    max_minutes: float = 45.0         # лимит времени: по истечении отчёт собирается по готовому (0 — без лимита)
    finish: bool = False              # дособрать отчёт по уже прочитанным чатам: без поиска и чтения,
                                      # но с Telegram (проверка аудитории клиента, био)
    rebuild_profile: bool = False
    reset_seeds: bool = False         # забыть сохранённые зёрна клиента и начать с поиска
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
