"""Парсинг под покупателя: параметры для канала и для ниши, разбор найденных людей, запуск без Telegram.

Запуск: python tests/check_miniapp_parse.py (проверки без Telegram и без сервера; печатают ALL OK).
Скрипты подменяют части модулей, поэтому pytest их не собирает: у файлов нет префикса test_.
"""
import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace as NS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from api import miniapp_parse as finder  # noqa: E402
from data import miniapp_store as store  # noqa: E402

store.DB_PATH = os.path.join(tempfile.mkdtemp(), "t.db")
store.init_store()

# параметры: канал покупателя вместо канала по умолчанию
p = finder._params({"tg_id": 5, "channel": "my_shop", "channel_kind": "channel", "parse_runs": 0})
assert (p.channel, p.topic, p.rebuild_profile, p.people, p.days) == ("my_shop", "", True, finder.PEOPLE, finder.DAYS)
assert p.max_minutes == finder.MAX_MINUTES
p = finder._params({"tg_id": 5, "channel": "my_shop", "channel_kind": "channel", "parse_runs": 2})
assert p.rebuild_profile is False, "повторный поиск берёт уже собранный профиль"
p = finder._params({"tg_id": 7, "channel": "маникюр в Москве", "channel_kind": "topic", "parse_runs": 0})
assert (p.channel, p.topic, p.brief) == ("topic-7", "маникюр в Москве", "маникюр в Москве")
assert p.profile_path().endswith("topic-7.json")

# найденные люди → клиенты покупателя; без username писать нельзя
au = NS(username="Anna_K", name="Анна К.")
no_user = NS(username="", name="Без юзернейма")
people = [
    NS(author=au, msg=NS(sender_id=11, text="Ищу мастера\nна маникюр", msg_id=5), tier="A", interest=61.5, why="запрос"),
    NS(author=no_user, msg=NS(sender_id=12, text="x", msg_id=6), tier="A", interest=50, why=""),
    NS(author=NS(username="warm_one", name="Тихий"), msg=NS(sender_id=13, text="", msg_id=0), tier="B", interest=30, why="состоит в 6 чатах ниши"),
]
leads = finder._to_leads(people)
assert [l["username"] for l in leads] == ["Anna_K", "warm_one"]
assert leads[0]["why"] == "Ищу мастера на маникюр" and leads[1]["why"] == "состоит в 6 чатах ниши"
store.upsert_user(5, username="x", first_name="X", last_name=None)
assert store.add_leads(5, leads) == 2
assert [r["username"] for r in [store.next_lead(5)]] == ["anna_k"]

# запуск парсера внутри сервиса: берёт клиента из пула, отдаёт людей в callback, сигналы не трогает
calls = {}


async def fake_parse(params, client=None, echo=True, *, on_people=None, handle_signals=True):
    calls.update(channel=params.channel, topic=params.topic, client=client, echo=echo, signals=handle_signals)
    on_people(people[:1])
    return "report.xlsx"


finder.run_smart_parse = fake_parse
finder._client = lambda: ("acc1", "CLIENT")
notes = []


async def fake_send(tg, text, *, button=None, query=""):
    notes.append((tg, text, button))


finder.bot.send = fake_send
store.upsert_user(8, username="y", first_name="Y", last_name=None)
store.set_channel_building(8, "маникюр", "topic")
store.set_channel_ready(8, "Текст")
store.parse_enqueue(8)
job = store.parse_claim()
asyncio.run(finder._run(job))
assert calls == {"channel": "topic-8", "topic": "маникюр", "client": "CLIENT", "echo": False, "signals": False}, calls
u = store.get_user(8)
assert u["parse_status"] == "done" and u["parse_runs"] == 1 and store.leads_new(8) == 1
assert notes and notes[0][0] == 8 and "Нашли для вас клиентов: 1" in notes[0][1]

# нет свободного аккаунта: поиск помечается неудачным и повторится позже
finder._client = lambda: None
store.parse_enqueue(8)
job = store.parse_claim()
asyncio.run(finder._run(job))
assert store.get_user(8)["parse_status"] == "failed"

# упал сам парсер: уже найденное сохраняем, поиск помечается неудачным


async def boom(params, client=None, echo=True, *, on_people=None, handle_signals=True):
    on_people(people[2:])
    raise RuntimeError("упал")


finder.run_smart_parse = boom
finder._client = lambda: ("acc1", "CLIENT")
store.parse_enqueue(8)
job = store.parse_claim()
asyncio.run(finder._run(job))
assert store.get_user(8)["parse_status"] == "failed" and store.leads_new(8) == 2
print("ALL OK")
