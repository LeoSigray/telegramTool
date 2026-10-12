"""Правка и удаление сообщений покупателя: без Telegram, на временной базе и поддельном клиенте.

Запуск: python tests/check_miniapp_edit_delete.py (проверки без Telegram и без сервера; печатают ALL OK).
Скрипты подменяют части модулей, поэтому pytest их не собирает: у файлов нет префикса test_.
"""
import asyncio
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from data import inbox  # noqa: E402
from data import miniapp_store as store  # noqa: E402
from api import inbox_service as svc  # noqa: E402
from api import miniapp as mini  # noqa: E402

tmp = os.path.join(tempfile.mkdtemp(), "t.db")
inbox.DB_PATH = tmp
store.DB_PATH = tmp
inbox.init_inbox() if hasattr(inbox, "init_inbox") else None
with sqlite3.connect(tmp) as c:
    c.executescript("""
      CREATE TABLE IF NOT EXISTS sends (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, account TEXT, target TEXT, peer_id TEXT,
        niche TEXT, template_id INTEGER, channel TEXT, status TEXT, error TEXT, sent_at TEXT, replied_at TEXT,
        reply_text TEXT, subscribed_at TEXT, outcome TEXT DEFAULT 'pending');
    """)
store.init_store()
if not any(True for _ in sqlite3.connect(tmp).execute("SELECT name FROM sqlite_master WHERE name='inbox_messages'")):
    raise SystemExit("нет таблиц inbox: найдите функцию создания схемы в data/inbox.py")

with sqlite3.connect(tmp) as c:
    c.execute("INSERT INTO inbox_conversations (peer_id, account, username, created_at, last_message_at, last_message_text) "
              "VALUES ('100','acc1','anna','t','2026-10-12T10:00:00+00:00','Сколько это стоит?')")
    c.executemany("INSERT INTO inbox_messages (conversation_id, tg_id, direction, text, account, status, sent_at) VALUES (1,?,?,?,?,?,?)", [
        (11, "out", "Здравствуйте!", "acc1", "ok", "2026-10-12T09:00:00+00:00"),
        (12, "in", "Сколько это стоит?", None, "ok", "2026-10-12T10:00:00+00:00"),
        (None, "out", "не ушло", "acc1", "failed", "2026-10-12T10:05:00+00:00"),
    ])


class FakeClient:
    def __init__(self):
        self.calls = []
        self.fail = None

    async def get_entity(self, x):
        return "ENT"

    async def edit_message(self, ent, mid, text):
        self.calls.append(("edit", mid, text))
        if self.fail:
            raise self.fail

    async def delete_messages(self, ent, ids, revoke=True):
        self.calls.append(("delete", ids, revoke))


fake = FakeClient()
svc.pool.clients.clear()
svc.pool.clients["acc1"] = fake


def run(coro):
    return asyncio.run(coro)


def expect_err(coro, part):
    try:
        run(coro)
    except svc.InboxError as e:
        assert part in str(e), (part, str(e))
        return
    raise AssertionError("ждали InboxError: " + part)


# правка
assert run(svc.edit_message(1, 1, "  Здравствуйте, Анна!  ")) == {"ok": True, "tg_id": 11}
assert fake.calls[-1] == ("edit", 11, "Здравствуйте, Анна!")
assert [m["text"] for m in inbox.get_messages(1)][0] == "Здравствуйте, Анна!"
# чужое (входящее) и неотправленное нельзя
expect_err(svc.edit_message(1, 2, "хак"), "не найдено")
expect_err(svc.edit_message(1, 3, "хак"), "не отправилось")
expect_err(svc.edit_message(1, 1, "   "), "пустое")
# «ничего не изменилось» — не ошибка
class NotModified(Exception):
    pass
NotModified.__name__ = "MessageNotModifiedError"
fake.fail = NotModified()
assert run(svc.edit_message(1, 1, "Здравствуйте, Анна!"))["ok"]
# истёкшее окно правки — понятный текст
class Expired(Exception):
    pass
Expired.__name__ = "MessageEditTimeExpiredError"
fake.fail = Expired()
expect_err(svc.edit_message(1, 1, "поздно"), "двух суток")
fake.fail = None
# аккаунт не в пуле
svc.pool.clients.clear()
expect_err(svc.edit_message(1, 1, "x"), "не в сети")
svc.pool.clients["acc1"] = fake

# удаление
assert run(svc.delete_message(1, 1))["ok"]
assert fake.calls[-1] == ("delete", [11], True), fake.calls[-1]
assert [m["status"] for m in inbox.get_messages(1)][0] == "deleted"
expect_err(svc.delete_message(1, 1), "не найдено")          # повторно уже нельзя

# ярлык «изменено» и пересчёт последнего сообщения
store.mark_edited(1, 11)
assert store.edited_ids(1) == {11}
store.refresh_last_message(1)
with sqlite3.connect(tmp) as c:
    row = c.execute("SELECT last_message_text, last_message_from_me FROM inbox_conversations WHERE id=1").fetchone()
assert row == ("Сколько это стоит?", 0), row if False else row

# окно правки: 47 часов
from datetime import datetime, timedelta, timezone
now = datetime.now(timezone.utc)
assert mini._editable_age((now - timedelta(hours=5)).isoformat()) is True
assert mini._editable_age((now - timedelta(hours=60)).isoformat()) is False
assert mini._editable_age("мусор") is True
print("ALL OK")
