"""Язык интерфейса: выбор в боте и в приложении, тексты уведомлений на выбранном языке.

Запуск: python tests/check_miniapp_lang.py (без Telegram и без сервера; печатает ALL OK).
Скрипт подменяет части модулей, поэтому pytest его не собирает: у файла нет префикса test_.
"""
import asyncio
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from api import miniapp as mini  # noqa: E402
from data import miniapp_store as store  # noqa: E402

store.DB_PATH = os.path.join(tempfile.mkdtemp(), "t.db")
store.init_store()
bot = mini.bot
sent, called = [], []


async def fake_send(chat_id, text, *, button=None, query="", keyboard=None):
    sent.append({"chat": chat_id, "text": text, "button": button, "keyboard": keyboard, "query": query})


async def fake_call(method, **params):
    called.append((method, params))


bot.send, bot.call = fake_send, fake_call


def run(coro):
    return asyncio.run(coro)


# тексты бота на двух языках
assert bot.lang_of(None) == "ru" and bot.lang_of({"lang": "en"}) == "en" and bot.lang_of({"lang": "de"}) == "ru"
assert "Здравствуйте" in bot.tx("welcome", "ru") and "Hello" in bot.tx("welcome", "en")
assert bot.paid_text(5, "en").startswith("Payment received") and bot.paid_text(5).startswith("Оплата")
assert "New message from Ann" in bot.reply_text("Ann", "hi", "en") and "Новое сообщение от Анна" in bot.reply_text("Анна", "привет")
assert "Support replied" in bot.support_text("ok", "en") and "Ответ поддержки" in bot.support_text("ок")
assert bot.reply_text(None, "", "en").endswith("Someone") and bot.reply_text(None, "").endswith("Человек")
kb = bot.lang_keyboard()["inline_keyboard"][0]
assert [b["callback_data"] for b in kb] == ["lang:ru", "lang:en"] and kb[0]["text"].endswith("Русский") and kb[1]["text"].endswith("English")

# первый разговор с ботом: сначала выбор языка (1 Русский, 2 English), приветствия ещё нет
start = {"message": {"chat": {"id": 42}, "from": {"id": 42, "username": "ann", "first_name": "Ann"}, "text": "/start"}}
run(mini._handle_update(start))
assert sent[-1]["text"] == bot.LANG_PROMPT and sent[-1]["keyboard"] == bot.lang_keyboard() and sent[-1]["button"] is None
assert store.get_user(42)["lang"] is None

# нажали «2 English»: язык сохраняется, вопрос о языке удаляется, приходит приветствие по-английски с кнопкой «Open the app»
sent.clear()
cb = {"callback_query": {"id": "cb1", "from": {"id": 42, "username": "ann", "first_name": "Ann"}, "data": "lang:en",
                         "message": {"message_id": 7, "chat": {"id": 42}}}}
run(mini._handle_update(cb))
assert store.get_user(42)["lang"] == "en"
assert ("answerCallbackQuery", {"callback_query_id": "cb1"}) in called
assert ("deleteMessage", {"chat_id": 42, "message_id": 7}) in called
assert sent[-1]["text"] == bot.tx("welcome", "en") and sent[-1]["button"] == "Open the app"

# дальше любое сообщение боту — приветствие на выбранном языке, без повторного вопроса
sent.clear()
run(mini._handle_update({"message": {"chat": {"id": 42}, "from": {"id": 42}, "text": "hello"}}))
assert sent[-1]["text"] == bot.tx("welcome", "en") and sent[-1]["keyboard"] is None

# чужие или кривые данные кнопки язык не меняют
sent.clear()
run(mini._handle_update({"callback_query": {"id": "cb2", "from": {"id": 42}, "data": "lang:xx", "message": {"message_id": 8, "chat": {"id": 42}}}}))
assert store.get_user(42)["lang"] == "en" and not sent

# выбор «1 Русский» у нового человека
run(mini._handle_update({"callback_query": {"id": "cb3", "from": {"id": 43, "first_name": "Ivan"}, "data": "lang:ru",
                                           "message": {"message_id": 9, "chat": {"id": 43}}}}))
assert store.get_user(43)["lang"] == "ru" and sent[-1]["button"] == "Открыть приложение"

import sqlite3  # noqa: E402
with sqlite3.connect(store.DB_PATH) as c:
    c.executescript("""
      CREATE TABLE IF NOT EXISTS sends (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, account TEXT, target TEXT, peer_id TEXT,
        niche TEXT, template_id INTEGER, channel TEXT, status TEXT, error TEXT, sent_at TEXT, replied_at TEXT, reply_text TEXT,
        subscribed_at TEXT, outcome TEXT DEFAULT 'pending');
    """)
from data import inbox  # noqa: E402
inbox.DB_PATH = store.DB_PATH
inbox.init_inbox()
store.ensure_stream(42)

# приложение: язык в /api/me и смена через /api/lang
user = store.get_user(42)
assert mini.me(user)["lang"] == "en"
assert mini.set_lang(mini.LangIn(lang="ru"), user) == {"lang": "ru"} and store.get_user(42)["lang"] == "ru"
store.upsert_user(44, username="x", first_name="X", last_name=None)
assert mini.me(store.get_user(44))["lang"] is None, "язык не выбран: приложение покажет экран выбора"
try:
    mini.LangIn(lang="de")
    raise AssertionError("только ru и en")
except ValueError:
    pass

# ответ человека приходит покупателю на его языке
store.set_lang(42, "en")
import sqlite3  # noqa: E402,F811
with sqlite3.connect(store.DB_PATH) as c:
    c.execute("INSERT INTO sends (job_id, account, target, peer_id, status, sent_at) VALUES ('tap-42','a','@z','900','sent','t')")
    c.execute("INSERT INTO inbox_conversations (peer_id, account, username, first_send_id, created_at) VALUES ('900','a','zed',1,'t')")
    c.execute("INSERT INTO inbox_messages (conversation_id, direction, text, sent_at) VALUES (1,'in','how much?','t')")
rows = store.inbound_since(0)
assert rows and rows[0]["lang"] == "en" and rows[0]["tg_id"] == 42
# уведомление о новом сообщении: бот пишет, только если приложение закрыто
import time  # noqa: E402
sent.clear()
mini._seen.clear()
cur = asyncio.run(mini._notify_replies(0))
assert cur >= 1 and sent[-1]["chat"] == 42 and sent[-1]["text"].startswith("💬 New message from @zed") and "how much?" in sent[-1]["text"]
assert sent[-1]["button"] == "Reply" and sent[-1]["query"] == "chat=1"
sent.clear()
mini._seen[42] = time.time()                           # приложение открыто: дублировать в бот незачем
assert asyncio.run(mini._notify_replies(0)) == cur and not sent and mini.app_is_open(42)
mini._seen[42] = time.time() - mini.APP_OPEN_GRACE_SEC - 1
assert not mini.app_is_open(42)
asyncio.run(mini._notify_replies(0))
assert len(sent) == 1, "приложение закрыли: уведомление приходит"
print("ALL OK")
