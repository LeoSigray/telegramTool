"""Поиск человека по нажатию: параметры для канала и ниши, выбор нового человека, лимиты Telegram.

Запуск: python tests/check_miniapp_parse.py (без Telegram и без сервера; печатает ALL OK).
Скрипт подменяет части модулей, поэтому pytest его не собирает: у файла нет префикса test_.
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

tmp = tempfile.mkdtemp()
store.DB_PATH = os.path.join(tmp, "t.db")
store.init_store()
finder.OUT_DIR = tmp

# параметры: один человек под канал покупателя, а не под канал по умолчанию
p = finder._params({"tg_id": 5, "channel": "my_shop", "channel_kind": "channel", "parse_runs": 0})
assert (p.channel, p.topic, p.rebuild_profile, p.people, p.reserve) == ("my_shop", "", True, 1, finder.RESERVE)
assert p.max_minutes == finder.MAX_MINUTES and p.out.endswith(".xlsx")
assert finder._params({"tg_id": 5, "channel": "my_shop", "channel_kind": "channel", "parse_runs": 2}).rebuild_profile is False
p = finder._params({"tg_id": 7, "channel": "маникюр в Москве", "channel_kind": "topic", "parse_runs": 0})
assert (p.channel, p.topic, p.brief) == ("topic-7", "маникюр в Москве", "маникюр в Москве")

# найденные люди: без username писать нельзя
au = NS(username="Anna_K", name="Анна К.")
people = [
    NS(author=au, msg=NS(sender_id=11, text="Ищу мастера\nна маникюр", msg_id=5), tier="A", interest=61.5, why=""),
    NS(author=NS(username="", name="Без имени"), msg=NS(sender_id=12, text="x", msg_id=6), tier="A", interest=50, why=""),
    NS(author=NS(username="second", name="Второй"), msg=NS(sender_id=13, text="", msg_id=0), tier="B", interest=30, why="тёплый"),
]
leads = finder._to_leads(people)
assert [ld["username"] for ld in leads] == ["Anna_K", "second"] and leads[0]["why"] == "Ищу мастера на маникюр"


class Client:
    """Поддельный клиент Telegram: проверка лимита на @имена либо проходит, либо даёт FloodWait."""

    def __init__(self, flood=0):
        self.flood = flood

    async def __call__(self, req):
        if self.flood:
            from telethon.errors import FloodWaitError
            raise FloodWaitError(request=None, capture=self.flood)
        return None


calls = []
seen_early = []


async def fake_parse(params, client=None, echo=True, *, on_people=None, on_found=None, handle_signals=True):
    calls.append((params.channel, params.people, client, handle_signals))
    first = [people[0]]
    on_found(first)                                   # первый человек пришёл раньше конца поиска
    seen_early.append(set(store.known_leads(8)))      # и уже лежит в списке клиентов
    on_found([p for p in people[1:]])
    on_people(people)                                 # в конце приходят те же люди: повторов быть не должно
    return params.out


finder.run_smart_parse = fake_parse
finder.health.is_available = lambda n: True
finder.an.sends_last_hours = lambda n, h=24: 0
finder.an.already_contacted = lambda names: set()
finder.an.is_suppressed = lambda k: False
finder.pool.clients.clear()
finder.pool.clients.update({"a1": Client(), "a2": Client()})

store.upsert_user(8, username="y", first_name="Y", last_name=None)
store.set_channel_building(8, "маникюр", "topic")
store.set_channel_ready(8, "Текст")
# нужен один человек: берём первого, остальных не берём
assert asyncio.run(finder.find_people(store.get_user(8), 1)) == 1
assert calls[-1][1] == 1 and calls[-1][3] is False and seen_early[-1] == {"anna_k"}
assert store.known_leads(8) == {"anna_k"}
# нужно двое: Анну уже находили, берётся следующий, без username пропускается
assert asyncio.run(finder.find_people(store.get_user(8), 2)) == 1
assert calls[-1][1] == 2 and store.known_leads(8) == {"anna_k", "second"}
assert asyncio.run(finder.find_people(store.get_user(8), 1)) == 0, "новых людей нет"

# аккаунт упёрся в лимит на @имена: помечаем его и ищем через другой
finder.pool.clients["a1"] = Client(flood=50000)
store.state_set("parse_used:a1", "0")
store.state_set("parse_used:a2", "999999999999")      # a1 дольше не искал, поэтому берётся первым
people.append(NS(author=NS(username="third", name="Третий"), msg=NS(sender_id=14, text="", msg_id=0), tier="C", interest=5, why=""))
assert asyncio.run(finder.find_people(store.get_user(8), 1)) == 1
assert "third" in store.known_leads(8) and calls[-1][2] is finder.pool.clients["a2"]
assert finder._flooded_until("a1") > 0 and finder._client(set())[0] == "a2"

# нажали три раза: нужен поиск на троих, найденные уже ждут своих нажатий
store.claim_gift(8, 5)
store.reserve(8, 3)
taps = [store.enqueue_tap(8) for _ in range(3)]
with __import__("sqlite3").connect(store.DB_PATH) as c:
    c.execute("UPDATE mini_leads SET status='sent' WHERE tg_id=8")
assert store.next_search() == 8 and store.search_need(8) == 3
store.add_leads(8, [{"username": "ready1"}])
assert store.search_need(8) == 2 and store.next_search() == 8, "двое нажатий всё ещё без человека"
upto = store.newest_queued_tap(8)
assert upto == taps[-1]
late = store.enqueue_tap(8)                           # нажали уже во время поиска
# найден один из двух нужных: второе нажатие не уйдёт и вернётся на баланс, старое и позднее остаются
before = store.get_user(8)["balance"]
finder._give_up(8, 1, upto, "не нашли подходящего человека")
assert store.get_user(8)["balance"] == before + 1
failed = store.last_failed_tap(8)
assert failed["id"] == taps[-1] and failed["error"] == "не нашли подходящего человека"
assert store.pending_taps(8) == 3 and store.oldest_waiting_tap(8)["id"] == taps[0] and store.newest_queued_tap(8) == late
store.parse_set(8, "failed")

# ── прогрев: ищем заранее, пока покупатель не нажал ──────────────────────────
# Первый поиск по новому каналу длится минуты. Ждать их у спиннера покупатель не станет,
# поэтому человек ищется в паузу между вводом канала и первым нажатием.
store.upsert_user(20, username="fresh", first_name="F", last_name=None)
store.set_channel_building(20, "fresh_shop", "channel")
store.set_channel_ready(20, "Текст")
assert store.next_search() != 20, "нажатий нет — обычный поиск этому не нужен"
assert store.next_prewarm(2) == 20, "каналу без людей нужен прогрев"

store.add_leads(20, [{"username": "warm1"}, {"username": "warm2"}])
assert store.next_prewarm(2) is None, "запас набран — прогрев больше не нужен"

# главное ради чего всё: первое нажатие НЕ запускает поиск, человек уже готов
store.claim_gift(20, 5)
store.enqueue_tap(20)
assert store.next_search() != 20, "человек найден заранее — ждать нечего"
assert store.search_need(20) == 0

# реальные нажатия важнее прогрева: пока их больше, чем людей, прогрев не вмешивается
store.upsert_user(21, username="busy", first_name="B", last_name=None)
store.set_channel_building(21, "busy_shop", "channel")
store.set_channel_ready(21, "Текст")
store.claim_gift(21, 5)
store.enqueue_tap(21)
# закрываем хвост прошлого блока, чтобы очередь дошла до 21
store.add_leads(8, [{"username": f"stub8_{i}"} for i in range(5)])
assert store.next_search() == 21, "нажатие без человека — обычный поиск"
assert store.next_prewarm(2) is None, "у кого есть нажатия, того греть не надо"




# ── Поломка аккаунта не должна выглядеть как «канала не существует» ──────────
# Покупатель подключал существующий канал и получал «Не нашли такой канал»: любая
# ошибка get_entity трактовалась одинаково, а пул в это время весь отдыхал.
import asyncio as _aio                                      # noqa: E402
from api import miniapp_channel as _chan                    # noqa: E402
from telethon.errors import UsernameNotOccupiedError        # noqa: E402


class _Boom:
    """Аккаунт с оборванным коннектом: именно так ведёт себя ослепший клиент."""
    def __init__(self, exc): self.exc = exc
    async def get_entity(self, handle): raise self.exc


class _Fine:
    def __init__(self, ent): self.ent = ent
    async def get_entity(self, handle): return self.ent
    async def get_messages(self, ent, limit=0): return []
    async def __call__(self, req): raise RuntimeError("описание не нужно")


from telethon.tl.types import Channel as _Channel           # noqa: E402

_real_candidates = _chan._candidate_clients
_ok_channel = _Channel(id=1, title="LINKTECH", photo=None, date=None,
                       creator=True, left=False, broadcast=True, megagroup=False)

# 1. первый аккаунт сломан, второй читает — покупатель не должен увидеть ошибку
_chan._candidate_clients = lambda limit=3: [_Boom(ConnectionError("Cannot send requests while disconnected")),
                                            _Fine(_ok_channel)]
prof = _aio.run(_chan.fetch_profile("linktek"))
assert prof["title"] == "LINKTECH", prof
print("channel: сломанный аккаунт не мешает соседнему: ok")

# 2. все аккаунты сломаны — честно просим подождать, канал не обвиняем
_chan._candidate_clients = lambda limit=3: [_Boom(ConnectionError("disconnected")),
                                            _Boom(OSError("proxy refused"))]
try:
    _aio.run(_chan.fetch_profile("linktek"))
    raise AssertionError("должна была быть ошибка")
except _chan.ChannelError as e:
    assert "не получается прочитать" in str(e), f"нельзя винить канал: {e}"
print("channel: поломка пула не выдаётся за отсутствие канала: ok")

# 3. а вот когда имени правда нет — говорим прямо
_chan._candidate_clients = lambda limit=3: [_Boom(UsernameNotOccupiedError(request=None))]
try:
    _aio.run(_chan.fetch_profile("нет_такого_канала_12345"))
    raise AssertionError("должна была быть ошибка")
except _chan.ChannelError as e:
    assert "Не нашли такой канал" in str(e), e
print("channel: несуществующее имя называем прямо: ok")

_chan._candidate_clients = _real_candidates

# ── Пустой статус канала = «читаем»: иначе покупатель смотрит в вечный спиннер ──
# Мини-апп показывает channel_status or "building", поэтому сервер обязан считать
# так же, иначе сборку никто не возобновит, а спиннер будет крутиться.
store.upsert_user(931, username="q", first_name="Q", last_name=None)
store.set_channel_building(931, "linktek", "channel")
with store._conn() as _c:
    _c.execute("UPDATE mini_users SET channel_status=NULL WHERE tg_id=931")
assert any(u["tg_id"] == 931 for u in store.building_channels()), \
    "канал с пустым статусом обязан попасть в возобновление"
print("channel: пустой статус подхватывается как «читаем»: ok")
print("ALL OK")
