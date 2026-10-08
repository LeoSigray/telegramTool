"""Тесты умного парсинга без Telegram и без LLM: python -m unittest tests.test_smart_parser"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from parsing.smart import features as ft
from parsing.smart import textproc as tp
from parsing.smart.models import Author, Message, Source, Verdict
from parsing.smart.profile import Profile, top_terms
from parsing.smart.settings import Params, normalize_channel
from parsing.smart.store import Store

NOW = datetime.now(timezone.utc)


def P(**kw):
    """Params для тестов механики: мягкие пороги, как до ужесточения (боевой порог PQI — 35)."""
    kw.setdefault("min_pqi", 5.0)
    kw.setdefault("warm", True)
    kw.setdefault("reserve", 100)
    return Params(**kw)


def ago(hours: float) -> datetime:
    return NOW - timedelta(hours=hours)



def col_values(ws, name: str) -> list:
    """Значения столбца листа по его заголовку (без строки заголовка)."""
    header = [c.value for c in ws[1]]
    j = header.index(name)
    return [r[j] for r in ws.iter_rows(min_row=2, values_only=True)]

class TextTests(unittest.TestCase):
    def test_normalize_channel(self):
        for raw in ("@crm_news", "https://t.me/crm_news", "t.me/s/crm_news", "crm_news"):
            self.assertEqual(normalize_channel(raw), "crm_news")

    def test_phrase_matcher_by_lemmas(self):
        lex = tp.Lexicon()
        hits = lex.intent.find(tp.lemmas("Ищем подрядчика на интеграцию"))
        self.assertTrue(any("подрядчик" in h for h in hits), hits)

    def test_stemmer_fallback_matches_forms(self):
        saved = tp._LEM
        try:
            lem = tp._Lemmatizer()
            lem._morph, lem._cache = None, {}
            tp._LEM = lem
            m = tp.PhraseMatcher(["ищу подрядчика"])
            self.assertTrue(m.find(tp.lemmas("Ищем подрядчика срочно")))
        finally:
            tp._LEM = saved

    def test_top_terms(self):
        terms = top_terms(["Интеграция Битрикс24 с 1С", "Интеграция CRM и 1С", "CRM для продаж"],
                          tp.Lexicon().stopwords)
        self.assertTrue(any("интеграц" in t for t in terms), terms)


class DiscoveryUnitTests(unittest.TestCase):
    def test_build_queries_short_then_words_then_long(self):
        from parsing.smart.discovery import build_queries
        prof = Profile(channel="x", community_queries=["селлеры wildberries", "чат предпринимателей",
                                                       "закупщики электроники для магазинов"],
                       search_queries=["iphone оптом"])
        q = build_queries(prof, P(channel="x"))
        self.assertEqual(q[:3], ["селлеры wildberries", "чат предпринимателей", "iphone оптом"])
        self.assertIn("селлеры", q)                       # отдельные слова — Telegram ищет их лучше
        self.assertIn("закупщики", q)
        self.assertNotIn("оптом", q)                      # слишком общее слово не ищем отдельно
        self.assertLess(q.index("селлеры"), q.index("закупщики электроники для магазинов"))
        self.assertIn("селлеры wildberries чат", q)
        self.assertNotIn("чат предпринимателей чат", q)

    def test_detect_lang(self):
        from parsing.smart.probe import detect_lang
        self.assertEqual(detect_lang("Ищу поставщика айфонов оптом"), "ru")
        self.assertEqual(detect_lang("Шукаю постачальника, підкажіть"), "uk")
        self.assertEqual(detect_lang("Assalomu alaykum, narxi qancha?"), "other")
        self.assertEqual(detect_lang("Looking for a supplier of iPhones"), "en")
        self.assertEqual(detect_lang("Нархи қанча, оптом борми?"), "uz")


class JunkTests(unittest.TestCase):
    def test_verdict_thresholds(self):
        from parsing.smart.junk import verdict
        p = P(channel="x")
        clean = {"n": 100, "bots": 0.1, "links": 0.2, "dups": 0.05, "topic": 0.0, "ads": 0.1}
        self.assertEqual(verdict(clean, p), "")
        self.assertIn("копипаст 40%", verdict(dict(clean, dups=0.4), p))
        self.assertIn("заработки", verdict(dict(clean, topic=0.2), p))
        self.assertEqual(verdict(dict(clean, n=5, bots=1.0), p), "", "по 5 сообщениям не судим")

    def test_junk_message_dropped(self):
        f = ft.extract("Заработок от 5000 в день без вложений, пиши", tp.Lexicon())
        self.assertTrue(tp.Lexicon().junk.find(f.lemmas))


class FeatureTests(unittest.TestCase):
    lex = tp.Lexicon()

    def score(self, text: str) -> float:
        return ft.intent_rule(ft.extract(text, self.lex))

    def test_request_beats_ad(self):
        req = self.score("Ищем подрядчика на интеграцию Битрикс24 с 1С, бюджет до 150к. Кто делал?")
        ad = self.score("Делаем интеграции под ключ! Портфолио https://example.com, скидка 20%")
        self.assertGreater(req, 0.6)
        self.assertLess(ad, 0.1)

    def test_invite_depends_on_request(self):
        buyer = self.score("Ищу дизайнера для лендинга, пишите в лс")
        seller = self.score("Рисую лендинги быстро, пишите в лс")
        self.assertGreater(buyer, 0.45)
        self.assertLess(seller, ft.W["base"] + 0.01)

    def test_looking_for_clients_is_seller(self):
        f = ft.extract("Ищу клиентов на настройку CRM", self.lex)
        self.assertTrue(f.seller_hits)
        self.assertFalse(f.intent_hits)

    def test_duplicates(self):
        class C:
            def __init__(self, sid, sender, text):
                self.msg = Message(sid, 1, NOW, sender, "user", text)
                self.f = ft.extract(text, FeatureTests.lex)
        ad = "Делаем интеграции Битрикс24 и 1С под ключ, портфолио https://example.com пишите"
        req = "Ищу подрядчика на интеграцию Битрикс24 с 1С для интернет-магазина, кто делал?"
        cands = [C(1, 7, ad), C(2, 7, ad), C(1, 8, req), C(2, 8, req)]
        ft.mark_duplicates(cands)
        self.assertTrue(cands[0].f.ad_dup)
        self.assertFalse(cands[2].f.ad_dup)
        self.assertEqual(cands[2].f.dup_sources, 2)


def build_fixture(tmp: str) -> Params:
    """Синтетика: клиент делает интеграции Битрикс24/CRM. 2 группы + канал с комментариями."""
    params = P(channel="@client_crm", days=14, offline=True, no_llm=True, check_contacted=False,
                    db_path=os.path.join(tmp, "smart.db"), profiles_dir=os.path.join(tmp, "profiles"),
                    out=os.path.join(tmp, "out", "leads.xlsx"))
    Profile(channel="client_crm", title="CRM-интегратор", offer="Внедрение CRM и интеграция Битрикс24 с 1С",
            audience="Владельцы и руководители малого и среднего бизнеса",
            topic_terms=["битрикс24", "crm", "интеграция", "1с", "внедрение crm", "amocrm",
                         "воронка продаж", "автоматизация продаж", "склад"],
            posts_sample=["Интегрировали Битрикс24 с 1С для сети магазинов",
                          "Как настроить воронку продаж в CRM"]).save(params.profile_path())

    st = Store(params.db_path)
    srcs = [Source(1001, "CRM и автоматизация бизнеса", "crm_chat", "group", members=4000),
            Source(1002, "Предприниматели Москвы", "biz_msk", "group", members=12000),
            Source(1003, "CRM новости", "crm_news", "channel", members=9000, linked_chat_id=5003)]
    for s in srcs:
        s.status, s.reason, s.found_via = "selected", "тест", "поиск: crm"
        st.upsert_source(s)
        st.save_decision(params.channel, s)

    A = Author
    authors = [
        A(1, "ivan_owner", "Иван", status="recently"),
        A(2, "crm_studio", "CRM Studio", status="recently"),
        A(3, "petr", "Пётр", status="last_week"),
        A(4, "maria", "Мария", status="recently"),
        A(5, "closed_guy", "Глеб", status="recently"),
        A(6, "helper1", "Хелпер", status="recently"),
        A(7, "", "Олег", status="recently"),
        A(8, "optout_user", "Ольга", status="recently"),
        A(9, "lead_bot", "Bot", is_bot=True),
        A(10, "old_req", "Старый", status="last_month"),
        A(11, "anna_ceo", "Анна", status="online"),
        A(12, "formula", "Фёдор", status="recently"),
        A(13, "q_user", "Кирилл", status="recently"),
    ]
    st.upsert_authors(authors)
    st.set_about(1, "Основатель интернет-магазина")
    st.set_about(11, "CEO логистической компании")

    U = "user"
    ad = "Делаем интеграции Битрикс24 и 1С под ключ! Портфолио https://example.com, скидка 20%"
    msgs = [
        Message(1001, 1, ago(5), 1, U, "Ищем подрядчика на интеграцию Битрикс24 с 1С, бюджет до 150к, "
                                      "нужно до конца месяца. Кто делал?"),
        Message(1001, 2, ago(4), 2, U, ad),
        Message(1001, 3, ago(30), 3, U, "Подскажите, кто пользовался amoCRM? Хотим перейти с Битрикс24, "
                                        "стоит ли?"),
        Message(1001, 4, ago(2), 4, U, "всем привет, как прошли выходные, кто куда ездил"),
        Message(1001, 5, ago(50), 5, U, "Нужен специалист по настройке воронки продаж в Битрикс24"),
        Message(1001, 6, ago(40), 5, U, "Спасибо, уже нашли исполнителя", reply_to=5),
        Message(1001, 7, ago(4), 6, U, "Могу помочь, написал в лс", reply_to=1),
        Message(1001, 8, ago(10), 7, U, "Ищу специалиста по внедрению CRM для отдела продаж, посоветуете?"),
        Message(1001, 9, ago(6), 8, U, "Ищу интегратора для 1С и Битрикс24. В лс не писать, отвечайте в чате"),
        Message(1001, 10, ago(3), 9, U, "Ищу CRM интеграцию битрикс24 срочно кто делает"),
        Message(1001, 11, ago(13 * 24), 10, U, "Ищу подрядчика на внедрение CRM и интеграцию с 1С"),
        Message(1002, 1, ago(4), 2, U, ad),
        Message(1002, 2, ago(3), 11, U, "Порекомендуйте, пожалуйста, интегратора Битрикс24 — нужно "
                                        "связать склад и 1С с CRM. Пишите в лс"),
        Message(1002, 3, ago(8), 12, U, "=) Ищем CRM для автоматизации продаж, что посоветуете?"),
        Message(1003, 100, ago(20), 0, "none", "Как выбрать CRM для отдела продаж: Битрикс24 или amoCRM",
                is_post=True),
        Message(1003, 7001, ago(18), 13, U, "А сколько стоит такое внедрение под ключ?", post_id=100),
    ]
    st.add_messages(msgs)
    st.close()
    return params


class EndToEndTests(unittest.TestCase):
    def test_offline_run_builds_sorted_excel(self):
        from openpyxl import load_workbook

        from parsing.smart.evaluate import report
        from parsing.smart.run import run_smart_parse

        with tempfile.TemporaryDirectory() as tmp:
            params = build_fixture(tmp)
            path = asyncio.run(run_smart_parse(params, echo=False))
            self.assertTrue(os.path.exists(path))
            wb = load_workbook(path)
            self.assertEqual(wb.sheetnames, ["Люди", "Запас", "Без username", "Источники",
                                             "Пересечения чатов", "Отсеянные", "Профиль", "Лог"])
            ws = wb["Люди"]
            header = [c.value for c in ws[1]]
            rows = [dict(zip(header, [c.value for c in r])) for r in ws.iter_rows(min_row=2)]
            names = [r["Username"] for r in rows]
            score = [r["Интерес"] for r in rows]

            self.assertEqual(score, sorted(score, reverse=True), "люди не отсортированы по интересу")
            # ЛПР + приглашение в ЛС + свежо + без конкурентов — первая
            self.assertEqual(names[0], "@anna_ceo", names)
            self.assertEqual(set(names[:3]), {"@anna_ceo", "@formula", "@ivan_owner"}, names)
            # вопрос в комментарии под профильным постом находится через контекст поста
            for must in ("@petr", "@q_user"):
                self.assertIn(must, names)
            self.assertNotIn("@old_req", names, "запрос 13-дневной давности ниже порога PQI")
            reserve = col_values(wb["Запас"], "Username")
            self.assertEqual(reserve, ["@old_req"], "ниже порога — в листе «Запас»")
            self.assertEqual(rows[0]["Уровень"], "A")
            self.assertTrue(all(r["PQI запроса"] >= params.min_pqi for r in rows if r["Тип"] == "горячий"))
            for bad in ("@crm_studio", "@maria", "@closed_guy", "@optout_user", "@lead_bot",
                        "@helper1"):
                self.assertNotIn(bad, names)
            self.assertTrue(all(r["Почему в списке"] for r in rows))

            anna = next(r for r in rows if r["Username"] == "@anna_ceo")
            self.assertIn("ЛПР", anna["Почему в списке"])
            self.assertIn("ждёт предложений в ЛС", anna["Почему в списке"])
            ivan = next(r for r in rows if r["Username"] == "@ivan_owner")
            self.assertIn("конкурентов в ветке: 1", ivan["Почему в списке"])

            formula_row = next(i for i, r in enumerate(rows, 2) if r["Username"] == "@formula")
            cell = ws.cell(row=formula_row, column=header.index("Цитата") + 1)
            self.assertEqual(cell.data_type, "s")
            self.assertTrue(str(cell.value).startswith("=)"))

            no_u = col_values(wb["Без username"], "Имя")
            self.assertIn("Олег", no_u)

            reasons = " ".join(str(c.value) for c in wb["Отсеянные"]["A"][1:])
            for r in ("реклама", "закрыл запрос", "не писать", "бот"):
                self.assertIn(r, reasons)
            self.assertIn("ВОРОНКА", [c.value for c in wb["Лог"]["A"]])

            # разметка → evaluate
            ws.cell(row=2, column=header.index("Разметка (да/нет)") + 1, value="да")
            ws.cell(row=3, column=header.index("Разметка (да/нет)") + 1, value="нет")
            wb.save(path)
            text = report(path)
            self.assertIn("размечено: 2", text)
            self.assertIn("@10", text)


class FakeClient:
    """Минимальная подмена TelegramClient: ровно те вызовы, что делает умный парсинг."""

    def __init__(self):
        from types import SimpleNamespace as NS

        from telethon.tl.types import Channel, ChatPhotoEmpty, PeerUser, User, UserStatusRecently

        self.NS = NS

        def ch(cid, title, username, *, group=False, members=5000, left=True):
            return Channel(id=cid, title=title, photo=ChatPhotoEmpty(), date=NOW, megagroup=group,
                           broadcast=not group, username=username, participants_count=members,
                           left=left, access_hash=cid)

        def user(uid, username, name):
            return User(id=uid, username=username, first_name=name, status=UserStatusRecently())

        def msg(mid, hours, text, u=None, reply_to=0, replies=0, forward=None):
            return NS(id=mid, date=ago(hours), message=text, sender=u, forward=forward,
                      from_id=PeerUser(u.id) if u else None, fwd_from=None, chat=None,
                      reply_to=NS(reply_to_msg_id=reply_to) if reply_to else None,
                      replies=NS(replies=replies, max_id=9000 + replies) if replies else None)

        self.me = User(id=999, first_name="reader")
        self.client_ch = ch(500, "CRM-интегратор", "client_crm")
        self.client_group = ch(600, "Обсуждение CRM-интегратор", "", group=True, members=800)
        self.g1 = ch(1001, "CRM и автоматизация бизнеса", "crm_chat", group=True, members=4000, left=False)
        self.g2 = ch(1002, "Котики и мемы", "cats_chat", group=True, members=8000)
        self.c3 = ch(1003, "CRM новости", "crm_news", members=9000)
        self.c4 = ch(1004, "Блог про CRM", "crm_blog", members=3000)
        self.g5 = ch(1005, "CRM чат Москва", "crm_msk", group=True, members=6000)    # заспамлен ботами
        self.g6 = ch(1006, "Заработок онлайн без вложений", "easy_money", group=True)  # мусор по названию
        self.g7 = ch(1007, "Клуб владельцев магазинов", "shop_owners", group=True, members=900)  # только по ссылке
        self.c8 = ch(1008, "Автоматизация продаж", "sales_auto", members=2000)                  # только по пересылке
        self.g9 = ch(1009, "Optom savdo CRM", "optom_uz", group=True, members=3000)              # чужой язык
        self.g10 = ch(1010, "Чат владельцев складов", "hop_two", group=True, members=700)       # 2-я ступень графа
        self.g11 = ch(1011, "Закупщики техники", "old_buyers", group=True, members=500)         # писали давно
        self.channels = [self.client_ch, self.client_group, self.g1, self.g2, self.c3, self.c4,
                         self.g5, self.g6, self.g7, self.c8, self.g9, self.g10, self.g11]
        self.search_enabled = True
        self.search_map = None      # {подстрока запроса: [чаты]} — если задан, поиск зависит от запроса
        self.participants = {}      # chat_id → [User]: открытые списки участников групп
        self.search_log = []
        self.full = {500: ("Внедряем CRM и интегрируем Битрикс24 с 1С", 600),
                     600: ("Обсуждение", 500),
                     1001: ("Чат про CRM, Битрикс24 и автоматизацию продаж", 0),
                     1002: ("Котики", 0), 1003: ("Новости CRM", 5003), 1004: ("Блог", 0),
                     1005: ("Чат про CRM и Битрикс24", 0), 1006: ("Деньги", 0),
                     1007: ("Владельцы интернет-магазинов", 0), 1008: ("Канал про CRM", 5008),
                     1009: ("Optom savdo", 0), 1010: ("Склады и CRM", 0), 1011: ("Закупщики", 0)}
        spam_bot = User(id=50, username="spam_bot", first_name="Promo", bot=True)
        self.spam_reads = 0
        buyer, ad, cat, commenter = (user(1, "buyer_ivan", "Иван"), user(2, "studio", "Студия"),
                                     user(3, "cat_lover", "Кот"), user(4, "q_user", "Кирилл"))
        client_fan, hidden_fan = user(5, "client_fan", "Фан"), user(6, "hidden_fan", "Скрытый")
        # аудитория клиента: client_fan виден в списке участников, hidden_fan — только точечно
        self.group_visible = [client_fan, self.me]
        self.group_hidden = {6}
        self.about = {1: "Основатель сети магазинов"}
        self.history = {
            500: [msg(1, 30, "Интегрировали Битрикс24 с 1С для сети магазинов. Наш чат: t.me/crm_chat"),
                  msg(2, 60, "Как внедрение CRM ускоряет отдел продаж: воронка продаж в Битрикс24"),
                  msg(3, 90, "Интеграция 1С и CRM: кейс автоматизации продаж")],
            1001: [msg(10, 5, "Ищем подрядчика на интеграцию Битрикс24 с 1С, бюджет 150к. Кто делал?", buyer),
                   msg(14, 8, "Полезный клуб для владельцев магазинов: t.me/shop_owners", user(7, "linker", "Л")),
                   msg(15, 9, "Интересный разбор про CRM", user(8, "fwd_user", "Ф"), forward=NS(chat=self.c8)),
                   msg(12, 6, "Посоветуйте интегратора Битрикс24 и 1С для склада?", client_fan),
                   msg(13, 7, "Нужен специалист по внедрению CRM и Битрикс24, кто делает?", hidden_fan),
                   msg(11, 4, "Делаем интеграции Битрикс24 под ключ, портфолио https://x.ru", ad)],
            1002: [msg(20, 3, "Посоветуйте корм для кота, мой не ест", cat)],
            1003: [msg(100, 20, "Битрикс24 или amoCRM: что выбрать для отдела продаж", replies=6)],
            1005: [msg(200 + i, 1 + i * 0.1, f"Лучшие цены на CRM, переходи https://spam{i}.ru", spam_bot)
                   for i in range(25)],
            1007: [msg(300, 4, "Посоветуйте CRM с интеграцией Битрикс24 для интернет-магазина?",
                       user(9, "graph_buyer", "Гриша")),
                   msg(301, 5, "Коллеги со складами тут: t.me/hop_two", user(40, "linker2", "Л2"))],
            1011: [msg(800, 24 * 60, "Ищу поставщика битрикс24 для магазина, кто делал?",
                       user(50, "old_asker", "Старый"))],
            1010: [msg(600, 6, "Ищем интеграцию Битрикс24 с 1С для склада, кто делал CRM?",
                       user(41, "hop2_buyer", "Хоп"))],
            1008: [msg(400, 10, "Как выбрать CRM для магазина", replies=6)],
            1009: [msg(500 + i, 2 + i, f"Assalomu alaykum, narxi qancha? optom bor {i}", user(10 + i, f"uz{i}", "U"))
                   for i in range(15)],
        }
        self.comments = {(1003, 100): [msg(9001, 18, "Сколько стоит такое внедрение CRM?", commenter,
                                           reply_to=100)],
                         (1008, 400): [msg(9101, 9, "Ищем внедрение CRM и Битрикс24 для склада, кто делает?",
                                           user(30, "graph_fwd_buyer", "Вова"), reply_to=400)]}
        self.full_user_calls = 0

    async def connect(self):
        return None

    async def get_me(self):
        return self.me

    async def get_entity(self, x):
        key = str(x).lstrip("@").lower()
        for c in self.channels:
            if c.username == key or x == c.id:
                return c
        raise ValueError(f"Cannot find any entity corresponding to {x}")

    async def __call__(self, req):
        name = type(req).__name__
        if name == "GetFullChannelRequest":
            about, linked = self.full[req.channel.id]
            return self.NS(full_chat=self.NS(about=about, participants_count=req.channel.participants_count,
                                             linked_chat_id=linked),
                           chats=[c for c in self.channels if c.id in (req.channel.id, linked)])
        if name == "GetParticipantRequest":
            from telethon.errors import UserNotParticipantError
            assert req.channel.id == 600, "проверять надо по группе клиента"
            visible = {u.id for u in self.group_visible}
            if req.participant in visible | self.group_hidden:
                return self.NS(participant=None)
            raise UserNotParticipantError(request=None)
        if name == "SearchRequest":
            self.search_log.append(req.q)
            if not self.search_enabled:
                return self.NS(chats=[])
            if self.search_map is not None:
                return self.NS(chats=[c for key, chats in self.search_map.items()
                                      if key in req.q.lower() for c in chats])
            return self.NS(chats=[self.g1, self.g2, self.c3, self.c4, self.g5, self.g6, self.g9])
        if name == "GetChannelRecommendationsRequest":
            return self.NS(chats=[self.c3] if self.search_enabled else [])
        if name == "GetFullUserRequest":
            self.full_user_calls += 1
            return self.NS(full_user=self.NS(about=self.about.get(req.id, "")))
        raise AssertionError(f"неожиданный запрос {name}")

    def iter_messages(self, entity, limit=None, min_id=0, wait_time=None, search=None,
                      reply_to=None, **_):
        async def gen():
            if entity is None:
                key = (search or "").split()[-1][:6].lower() if search else ""
                by_id = {c.id: c for c in self.channels}
                items = []
                for cid, msgs in self.history.items():
                    for m in msgs:
                        if key and key in (m.message or "").lower() and cid in by_id:
                            m.chat = by_id[cid]
                            items.append(m)
            elif reply_to:
                items = self.comments.get((entity.id, reply_to), [])
            else:
                items = self.history.get(entity.id, [])
                if entity.id == 1005:
                    self.spam_reads += 1
            items = sorted((m for m in items if m.id > (min_id or 0)), key=lambda m: -m.id)
            for m in items[:limit]:
                yield m
        return gen()

    def iter_participants(self, entity, limit=None, **_):
        async def gen():
            if entity.id == 600:
                for u in self.group_visible:
                    yield u
                return
            if entity.id in self.participants:
                for u in self.participants[entity.id][:limit]:
                    yield u
                return
            raise PermissionError("CHAT_ADMIN_REQUIRED")  # подписчиков канала не-админ не видит
        return gen()


class OnlineFakeClientTests(unittest.TestCase):
    def setUp(self):
        try:
            import telethon  # noqa: F401
        except ImportError:
            self.skipTest("telethon не установлен")

    def test_full_online_pipeline(self):
        from unittest import mock

        from openpyxl import load_workbook

        from parsing.smart.run import run_smart_parse

        real_sleep = asyncio.sleep

        async def fast_sleep(*_a, **_k):
            await real_sleep(0)

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(asyncio, "sleep", fast_sleep):
            client = FakeClient()
            params = P(channel="@client_crm", no_llm=True, check_contacted=False, people=10,
                            search_messages=False,   # здесь проверяем граф, а не поиск по сообщениям
                            db_path=os.path.join(tmp, "s.db"), profiles_dir=os.path.join(tmp, "p"),
                            out=os.path.join(tmp, "o", "a.xlsx"))
            path = asyncio.run(run_smart_parse(params, client=client, echo=False))
            wb = load_workbook(path)

            src = {r[0]: r for r in wb["Источники"].iter_rows(min_row=2, values_only=True)}
            self.assertEqual(src["CRM и автоматизация бизнеса"][5], "взят")
            self.assertIn("поиск:", src["CRM и автоматизация бизнеса"][4])
            self.assertNotIn("Обсуждение CRM-интегратор", src, "обсуждение клиента — не источник")
            # мусор: по названию — до чтения; заспамленный ботами — по содержимому
            self.assertTrue(src["Заработок онлайн без вложений"][6].startswith("мусорная тематика: «"))
            self.assertIn("мусорный чат: боты", src["CRM чат Москва"][6])
            self.assertIn("ссылки и контакты", src["CRM чат Москва"][6])
            self.assertIn("пересылки 100%", src["CRM чат Москва"][6])  # отсеян ещё на пробе
            self.assertEqual(client.spam_reads, 1)
            # чужой язык отсеян пробой
            self.assertTrue(src["Optom savdo CRM"][6].startswith("язык:"), src["Optom savdo CRM"][6])
            # граф: чат по ссылке и канал по пересылке найдены и прочитаны
            self.assertIn("граф: ссылка в @crm_chat", src["Клуб владельцев магазинов"][4])
            self.assertIn("граф: пересылка в @crm_chat", src["Автоматизация продаж"][4])
            self.assertEqual(src["Блог про CRM"][6], "у канала нет комментариев")
            self.assertNotIn("CRM-интегратор", src, "свой канал клиента не должен быть источником")

            ws = wb["Люди"]
            header = [c.value for c in ws[1]]
            rows = [dict(zip(header, r)) for r in ws.iter_rows(min_row=2, values_only=True)]
            names = [r["Username"] for r in rows]
            self.assertIn("@buyer_ivan", names, names)
            self.assertIn("@q_user", names)
            self.assertNotIn("@studio", names)
            self.assertNotIn("@cat_lover", names)
            # аудитория клиента: и видимый в списке, и найденный точечной проверкой
            self.assertNotIn("@client_fan", names)
            self.assertNotIn("@hidden_fan", names)
            reasons = [r[0] for r in wb["Отсеянные"].iter_rows(min_row=2, values_only=True)]
            self.assertTrue(any(str(x).startswith("уже в аудитории клиента (всего 2)") for x in reasons),
                            reasons)
            self.assertIn("ЛПР (био: «основатель»)", next(r for r in rows if r["Username"] == "@buyer_ivan")
                          ["Почему в списке"])
            self.assertIn("@graph_buyer", names)
            self.assertIn("@graph_fwd_buyer", names)
            self.assertTrue(rows[0]["Сообщение"])
            link = ws.cell(row=2, column=header.index("Сообщение") + 1).hyperlink.target
            ivan_row = names.index("@buyer_ivan") + 2
            link = ws.cell(row=ivan_row, column=header.index("Сообщение") + 1).hyperlink.target
            self.assertEqual(link, "https://t.me/crm_chat/10")
            q_row = names.index("@q_user") + 2
            self.assertEqual(ws.cell(row=q_row, column=header.index("Сообщение") + 1).hyperlink.target,
                             "https://t.me/crm_news/100?comment=9001")

            # повторный запуск: профиль из файла, новые сообщения не дочитываются, био из кеша
            calls_before = client.full_user_calls
            params.out = os.path.join(tmp, "o", "b.xlsx")
            path2 = asyncio.run(run_smart_parse(params, client=client, echo=False))
            self.assertEqual(client.full_user_calls, calls_before, "био должно браться из кеша")
            log2 = " ".join(str(r[3]) for r in load_workbook(path2)["Лог"].iter_rows(values_only=True))
            self.assertIn("взят сохранённый профиль", log2)
            self.assertIn("новых сообщений 0", log2)
            self.assertEqual(client.spam_reads, 1, "мусорный чат не должен читаться повторно")
            self.assertIn("ранее признан мусорным", " ".join(
                str(r[6]) for r in load_workbook(path2)["Источники"].iter_rows(values_only=True)))
            self.assertIn("@buyer_ivan", col_values(load_workbook(path2)["Люди"], "Username"))

            # каталог: другой клиент той же темы получает чаты без поиска Telegram
            client.search_enabled = False
            client.channels.append(client.client_ch.__class__(
                id=700, title="CRM для магазинов", photo=client.client_ch.photo, date=NOW,
                broadcast=True, username="client_two", access_hash=700))
            client.full[700] = ("Внедряем CRM и Битрикс24 для интернет-магазинов", 0)
            client.history[700] = [client.history[500][0]]
            p2 = P(channel="@client_two", no_llm=True, check_contacted=False, people=5,
                        db_path=params.db_path, profiles_dir=params.profiles_dir,
                        out=os.path.join(tmp, "o", "c.xlsx"))
            path3 = asyncio.run(run_smart_parse(p2, client=client, echo=False))
            src3 = {r[0]: r for r in load_workbook(path3)["Источники"].iter_rows(min_row=2, values_only=True)}
            self.assertIn("каталог", src3["CRM и автоматизация бизнеса"][4])


class PeopleTargetTests(unittest.TestCase):
    def test_stops_when_enough_and_trims_to_n(self):
        from unittest import mock

        real_sleep = asyncio.sleep

        async def fast_sleep(*_a, **_k):
            await real_sleep(0)

        with mock.patch.object(asyncio, "sleep", fast_sleep):
            client = FakeClient()
            with tempfile.TemporaryDirectory() as tmp:
                from openpyxl import load_workbook

                from parsing.smart.run import run_smart_parse
                params = P(channel="@client_crm", no_llm=True, check_contacted=False, people=2,
                                wave=1, overshoot=1.0, db_path=os.path.join(tmp, "s.db"),
                                profiles_dir=os.path.join(tmp, "p"), out=os.path.join(tmp, "o.xlsx"))
                path = asyncio.run(run_smart_parse(params, client=client, echo=False))
                wb = load_workbook(path)
                names = col_values(wb["Люди"], "Username")
                self.assertEqual(len(names), 2, names)           # ровно столько, сколько просили
                src = {r[0]: r for r in wb["Источники"].iter_rows(min_row=2, values_only=True)}
                skipped = [n for n, r in src.items() if r[6] == "не понадобился: нужное число людей уже найдено"]
                self.assertTrue(skipped, "после набора нужного числа остальные источники не читаются")
                reserve = col_values(wb["Запас"], "Username") if "Запас" in wb.sheetnames else []
                self.assertTrue(set(names).isdisjoint(reserve))

    def test_message_search_skips_account_own_chats(self):
        from unittest import mock

        real_sleep = asyncio.sleep

        async def fast_sleep(*_a, **_k):
            await real_sleep(0)

        class Spy(FakeClient):
            def __init__(self):
                super().__init__()
                self.global_searches = 0
                self.dialog_calls = 0

            def iter_messages(self, entity, *a, **k):
                if entity is None:
                    self.global_searches += 1
                return super().iter_messages(entity, *a, **k)

            def iter_dialogs(self, *a, **k):
                self.dialog_calls += 1
                raise AssertionError("личные диалоги аккаунта читать нельзя")

        from parsing.smart.run import run_smart_parse
        with mock.patch.object(asyncio, "sleep", fast_sleep), tempfile.TemporaryDirectory() as tmp:
            client = Spy()
            params = P(channel="@client_crm", no_llm=True, check_contacted=False, people=5,
                            db_path=os.path.join(tmp, "s.db"), profiles_dir=os.path.join(tmp, "p"),
                            out=os.path.join(tmp, "o.xlsx"))
            asyncio.run(run_smart_parse(params, client=client, echo=False))
            self.assertGreater(client.global_searches, 0, "поиск по сообщениям включён по умолчанию")
            st = Store(params.db_path)
            via = {s.username: s.found_via for s in st.load_decisions(params.channel)}
            st.close()
            self.assertIn("фраза:", via.get("shop_owners", ""), "публичный чужой чат найден по фразе")
            self.assertNotIn("фраза:", via.get("crm_chat", ""), "чат, где аккаунт состоит, не берём")


class SearchRoundsTests(unittest.TestCase):
    def _env(self):
        from unittest import mock
        real_sleep = asyncio.sleep

        async def fast_sleep(*_a, **_k):
            await real_sleep(0)
        return mock.patch.object(asyncio, "sleep", fast_sleep)

    def _profile(self, tmp, queries, **extra):
        p = Profile(channel="client_crm", title="CRM", offer="Внедрение CRM и интеграция Битрикс24",
                    audience="Владельцы бизнеса", community_queries=queries, queries_version=3,
                    topic_terms=["битрикс24", "crm", "интеграция", "1с", "склад", "автоматизация продаж"],
                    **extra)
        os.makedirs(os.path.join(tmp, "p"), exist_ok=True)
        p.save(os.path.join(tmp, "p", "client_crm.json"))

    def test_next_rounds_use_next_queries_until_enough_people(self):
        from parsing.smart.run import run_smart_parse
        with self._env(), tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            # запросы 1-3 находят один чат, 4-6 другой, 7-9 третий: людей хватит только к концу
            client.search_map = {"alpha": [client.g1], "beta": [client.g7], "gamma": [client.c8]}
            qs = [f"{w} {i}" for w in ("alpha", "beta", "gamma") for i in range(3)]
            self._profile(tmp, qs)
            params = P(channel="@client_crm", no_llm=True, check_contacted=False, people=4, wave=1,
                            overshoot=1.0, queries_per_round=3, queries_target=9, min_pqi=5.0,
                            graph=False, search_messages=False, db_path=os.path.join(tmp, "s.db"),
                            profiles_dir=os.path.join(tmp, "p"), out=os.path.join(tmp, "o.xlsx"))
            from openpyxl import load_workbook
            path = asyncio.run(run_smart_parse(params, client=client, echo=False))
            self.assertEqual(client.search_log[:3], qs[:3], "первый раунд — первые 3 запроса")
            self.assertEqual(len(set(client.search_log)), len(client.search_log), "запросы не повторяются")
            wb = load_workbook(path)
            log = " ".join(str(r[3]) for r in wb["Лог"].iter_rows(values_only=True))
            self.assertIn("раунд 1:", log)
            self.assertIn("раунд 2:", log, "людей не хватило — должен быть второй раунд")
            names = col_values(wb["Люди"], "Username")
            self.assertIn("@graph_buyer", names)   # из чата, найденного только во втором раунде

    def test_refill_when_pool_exhausted_calls_llm_for_new_queries(self):
        from unittest import mock

        from parsing.smart import profile as profmod
        from parsing.smart.run import run_smart_parse

        prompts = []

        async def fake_llm(prompt, max_tokens):
            prompts.append(prompt)
            return json.dumps(["sales club", "магазины клуб", "new chat idea"], ensure_ascii=False)

        with self._env(), tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(profmod, "llm_complete", fake_llm), \
                mock.patch("parsing.smart.run.llm_available", return_value=(True, "fake")):
            client = FakeClient()
            client.search_map = {"alpha": [client.g1], "sales club": [client.g7]}
            self._profile(tmp, ["alpha one", "alpha two"])
            params = P(channel="@client_crm", check_contacted=False, people=4, wave=1, overshoot=1.0,
                            queries_per_round=2, queries_target=2, min_pqi=5.0, graph=False,
                            llm_budget=0, search_messages=False, db_path=os.path.join(tmp, "s.db"),
                            profiles_dir=os.path.join(tmp, "p"), out=os.path.join(tmp, "o.xlsx"))
            asyncio.run(run_smart_parse(params, client=client, echo=False))
            self.assertTrue(prompts, "когда пул запросов кончился, нейросеть должна дать новые")
            self.assertIn("Уже использованы", prompts[-1])
            self.assertIn("alpha one", prompts[-1])
            self.assertIn("sales club", client.search_log)

    def test_generate_queries_dedup_and_fallback(self):
        from unittest import mock

        from parsing.smart import profile as profmod

        async def fake_llm(prompt, max_tokens):
            return json.dumps(["Чат ИТ", "чат ит", "alpha one", "клуб владельцев"], ensure_ascii=False)

        prof = Profile(channel="x", offer="o", topic_terms=["crm", "битрикс24"])
        with mock.patch.object(profmod, "llm_complete", fake_llm):
            from parsing.smart.runlog import RunLog
            out = asyncio.run(profmod.generate_queries(prof, True, RunLog(echo=False), {"alpha one"}, [], 8))
        self.assertEqual(len(out), 8)
        self.assertEqual(len({q.lower() for q in out}), 8, "без повторов")
        self.assertNotIn("alpha one", [q.lower() for q in out])
        self.assertEqual(out[:2], ["Чат ИТ", "клуб владельцев"])      # сначала нейросеть
        self.assertTrue(any("crm" in q for q in out[2:]))              # остальное — шаблоны

    def test_stops_after_empty_rounds(self):
        from parsing.smart.run import run_smart_parse
        with self._env(), tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            client.search_map = {}                     # поиск ничего не находит
            self._profile(tmp, [f"q{i}" for i in range(30)])
            params = P(channel="@client_crm", no_llm=True, check_contacted=False, people=50,
                            queries_per_round=2, queries_target=30, graph=False, max_rounds=10,
                            search_messages=False,
                            db_path=os.path.join(tmp, "s.db"), profiles_dir=os.path.join(tmp, "p"),
                            out=os.path.join(tmp, "o.xlsx"))
            asyncio.run(run_smart_parse(params, client=client, echo=False))
            # раунд 1 непустой (похожий канал клиента), раунды 2–4 пустые — и стоп
            self.assertEqual(len(client.search_log), 8)


class IntersectionsIntegrationTests(unittest.TestCase):
    def test_warm_people_and_chat_pairs_in_excel(self):
        from unittest import mock

        from openpyxl import load_workbook
        from telethon.tl.types import User, UserStatusRecently

        from parsing.smart.run import run_smart_parse
        real_sleep = asyncio.sleep

        async def fast_sleep(*_a, **_k):
            await real_sleep(0)

        with mock.patch.object(asyncio, "sleep", fast_sleep), tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            client.search_map = {"битрикс24": [client.g1]}
            NS = client.NS
            writer = User(id=72, username="warm_writer", first_name="Тёма", status=UserStatusRecently())
            silent = User(id=70, username="silent_fan", first_name="Тихий", status=UserStatusRecently())
            topic = "У нас склад и Битрикс24, интеграция с 1С — наша вечная боль"
            from telethon.tl.types import PeerUser

            def m(mid, hours):
                return NS(id=mid, date=ago(hours), message=topic, sender=writer, forward=None,
                          from_id=PeerUser(72), fwd_from=None, chat=None, reply_to=None, replies=None)
            client.history[1001].append(m(16, 7))
            client.history[1007].append(m(302, 6))
            client.participants = {1001: [writer, silent], 1007: [writer, silent], 1010: [silent]}
            params = P(channel="@client_crm", no_llm=True, check_contacted=False, people=20,
                            min_seeds=1, search_messages=False, min_interest=0.0, db_path=os.path.join(tmp, "s.db"),
                            profiles_dir=os.path.join(tmp, "p"), out=os.path.join(tmp, "o.xlsx"))
            path = asyncio.run(run_smart_parse(params, client=client, echo=False))
            wb = load_workbook(path)
            ws = wb["Люди"]
            header = [c.value for c in ws[1]]
            rows = [dict(zip(header, r)) for r in ws.iter_rows(min_row=2, values_only=True)]
            by_name = {r["Username"]: r for r in rows}
            self.assertIn("@warm_writer", by_name, "пишет по теме в 2 чатах ниши — тёплый")
            self.assertEqual(by_name["@warm_writer"]["Тип"], "тёплый")
            self.assertEqual(by_name["@warm_writer"]["Чатов ниши: пишет"], 2)
            self.assertNotIn("@silent_fan", by_name, "молчит и всего в 3 чатах — не берём")
            hot = [r for r in rows if r["Тип"] == "горячий"]
            self.assertTrue(hot)
            self.assertGreater(hot[0]["Интерес"], by_name["@warm_writer"]["Интерес"])
            pairs = list(wb["Пересечения чатов"].iter_rows(min_row=2, values_only=True))
            self.assertTrue(any({"@crm_chat", "@shop_owners"} == {p[0], p[1]} for p in pairs), pairs)


class ProductionDefaultsTests(unittest.TestCase):
    """Боевые настройки: PQI ниже 35 не учитываем вообще, тёплых и «Запас» по умолчанию нет."""

    def test_defaults(self):
        p = Params(channel="x")
        self.assertEqual((p.min_pqi, p.min_interest, p.warm, p.reserve), (0.0, 20.0, True, 0))

    def test_no_pqi_cutoff_sorted_by_interest_with_optional_threshold(self):
        from unittest import mock

        from openpyxl import load_workbook

        from parsing.smart.run import run_smart_parse
        real_sleep = asyncio.sleep

        async def fast_sleep(*_a, **_k):
            await real_sleep(0)

        def run(**kw):
            with mock.patch.object(asyncio, "sleep", fast_sleep), tempfile.TemporaryDirectory() as tmp:
                client = FakeClient()
                params = Params(channel="@client_crm", no_llm=True, check_contacted=False, people=20,
                                search_messages=False, db_path=os.path.join(tmp, "s.db"),
                                profiles_dir=os.path.join(tmp, "p"), out=os.path.join(tmp, "o.xlsx"), **kw)
                path = asyncio.run(run_smart_parse(params, client=client, echo=False))
                ws = load_workbook(path)["Люди"]
                header = [c.value for c in ws[1]]
                return [dict(zip(header, r)) for r in ws.iter_rows(min_row=2, values_only=True)]

        rows = run()
        self.assertTrue(rows)
        interest = [r["Интерес"] for r in rows]
        self.assertEqual(interest, sorted(interest, reverse=True), "сортировка по итоговому интересу")
        self.assertTrue(any(r["PQI запроса"] != "" and r["PQI запроса"] < 35 for r in rows),
                        "порога по PQI нет: слабее 35 тоже в списке")
        strict = run(min_interest=40.0)
        self.assertLess(len(strict), len(rows))
        self.assertTrue(all(r["Интерес"] >= 40 for r in strict))


class SeedsGraphTests(unittest.TestCase):
    def _env(self):
        from unittest import mock
        real_sleep = asyncio.sleep

        async def fast_sleep(*_a, **_k):
            await real_sleep(0)
        return mock.patch.object(asyncio, "sleep", fast_sleep)

    def _params(self, tmp, **kw):
        base = dict(channel="@client_crm", no_llm=True, check_contacted=False, people=20, min_seeds=1,
                    search_messages=False, db_path=os.path.join(tmp, "s.db"),
                    profiles_dir=os.path.join(tmp, "p"), out=os.path.join(tmp, "o.xlsx"))
        base.update(kw)
        return P(**base)

    def test_two_hops_and_saved_seeds_next_run(self):
        from openpyxl import load_workbook

        from parsing.smart.run import run_smart_parse
        from parsing.smart.store import Store
        with self._env(), tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            client.search_map = {"битрикс24": [client.g1]}      # поиск находит только одно зерно
            params = self._params(tmp)
            path = asyncio.run(run_smart_parse(params, client=client, echo=False))
            wb = load_workbook(path)
            src = {r[0]: r for r in wb["Источники"].iter_rows(min_row=2, values_only=True)}
            self.assertIn("граф: ссылка в @crm_chat", src["Клуб владельцев магазинов"][4])
            # вторая ступень: от чата, найденного графом, к следующему
            self.assertIn("граф: ссылка в @shop_owners", src["Чат владельцев складов"][4])
            names = col_values(wb["Люди"], "Username")
            self.assertIn("@hop2_buyer", names)
            st = Store(params.db_path)
            seeds = {s.username for s, _ in st.load_seeds(params.channel)}
            st.close()
            self.assertIn("crm_chat", seeds)
            self.assertIn("shop_owners", seeds)

            # следующий запуск: стартует с сохранённых зёрен, первый раунд — граф, без поиска
            client2 = FakeClient()
            client2.search_map = {}
            p2 = self._params(tmp, out=os.path.join(tmp, "o2.xlsx"), people=3, overshoot=1.0)
            path2 = asyncio.run(run_smart_parse(p2, client=client2, echo=False))
            log2 = " ".join(str(r[3]) for r in load_workbook(path2)["Лог"].iter_rows(values_only=True))
            self.assertIn("зёрна из прошлых запусков", log2)
            self.assertIn("раунд 1 (граф", log2)
            self.assertEqual(client2.search_log, [], "при достаточном числе зёрен поиск не нужен")

    def test_repeated_references_rank_first(self):
        from types import SimpleNamespace as NS

        from parsing.smart import discovery as dv
        from parsing.smart.models import Source
        from parsing.smart.probe import Probe
        from parsing.smart.runlog import RunLog

        with self._env():
            client = FakeClient()
            state = dv.DiscoveryState(set())
            seeds = []
            for cid, name in ((901, "seed_a"), (902, "seed_b")):
                seeds.append((Source(cid, name, name, "group"), NS(id=cid), Probe(hints=[])))
            # оба зерна пересылают из c8, только одно — из g7
            seeds[0][2].fwd_ents = [client.c8, client.g7]
            seeds[1][2].fwd_ents = [client.c8]
            params = P(channel="x", graph_resolves=0)
            asyncio.run(dv._expand_graph(client, seeds, state.pool, params, RunLog(echo=False), state))
            self.assertEqual(state.graph_score[client.c8.id], 2.0)   # две пересылки по 1.0
            self.assertEqual(state.graph_score[client.g7.id], 1.0)
            # повторная связь от того же зерна не считается
            asyncio.run(dv._expand_graph(client, seeds[:1], state.pool, params, RunLog(echo=False), state))
            self.assertEqual(state.graph_score[client.c8.id], 2.0)


class InterestModelTests(unittest.TestCase):
    """Интерес = текст (запрос) + пересечения (в скольких чатах ниши человек есть)."""

    def _setup(self):
        from parsing.smart import scoring
        lex = tp.Lexicon()
        sources = {i: Source(i, f"Чат {i}", f"chat{i}", "group", y_est=5.0) for i in (1, 2, 3, 4)}
        authors = {u: Author(u, f"user{u}", f"Имя{u}", status="recently") for u in range(1, 8)}
        mid = iter(range(1, 1000))

        def cand(uid, chat, text, r, hours=5, **kw):
            m = Message(chat, next(mid), ago(hours), uid, "user", text)
            c = scoring.Cand(msg=m, source=sources[chat], author=authors[uid], f=ft.extract(text, lex))
            c.r = c.rel = r
            c.age_h = hours
            for k, v in kw.items():
                setattr(c, k, v)
            return c

        req = "Ищем поставщика айфонов оптом, кто возит из Дубая?"
        topic = "У нас магазин техники, айфоны берём оптом партиями из Дубая"
        ad = "Продаём айфоны оптом, прайс по ссылке https://x.ru, пишите в лс"
        hot1 = cand(1, 1, req, 0.8, pqi=30.0, kind="hot", a=0.3, e=1.0)
        hot2 = cand(2, 1, req, 0.8, pqi=30.0, kind="hot", a=0.3, e=1.0)
        hot5 = cand(5, 2, req, 0.8, pqi=30.0, kind="hot", a=0.3, e=1.0)
        cands = [hot1, hot2, hot5,
                 cand(2, 2, topic, 0.6), cand(3, 1, topic, 0.6), cand(3, 2, topic, 0.6),
                 cand(3, 3, topic, 0.6), cand(5, 1, ad, 0.6), cand(5, 3, ad, 0.6)]
        prep = scoring.Prep(cands=cands, dropped=[], src_stats={}, opt_out=set())
        members = {1: {2, 4, 6}, 2: {2, 4, 6}, 3: {2, 4}, 4: {4}}
        return lex, sources, authors, prep, members, [hot1, hot2, hot5]

    def test_ranking_text_first_intersections_boost_sellers_out(self):
        from parsing.smart import interest
        lex, sources, authors, prep, members, hot = self._setup()
        params = P(channel="x")
        stats = interest.collect_stats(prep, members, sources)
        weights, overlap, people = interest.chat_weights(sources, {}, stats, set())
        warm = interest.build_warm(stats, {1, 2, 5}, set(), authors, sources, weights, params, NOW, set())
        warm_ids = {c.msg.sender_id for c in warm}
        self.assertIn(3, warm_ids, "пишет по теме в 3 чатах ниши — тёплый")
        self.assertNotIn(6, warm_ids, "только состоит в 2 чатах — слишком слабо")
        self.assertNotIn(4, warm_ids, "только состоит в 4 чатах — без сообщений не дотягивает")

        ranked, sellers = interest.score_all(hot + warm, stats, weights, sources, lex, params, NOW)
        order = [c.msg.sender_id for c in ranked]
        self.assertEqual(order[:3], [2, 1, 3], "запрос + пересечения > только запрос > тёплый")
        self.assertEqual([c.msg.sender_id for c in sellers], [5], "реклама в 2 из 3 сообщений — продавец")
        self.assertIn("похож на продавца", sellers[0].drop)
        top, warm3 = ranked[0], next(c for c in ranked if c.msg.sender_id == 3)
        self.assertEqual((top.chats_active, top.chats_member), (2, 1))
        self.assertEqual(warm3.kind, "warm")
        self.assertIn("тёплый: пишет в 3 чатах ниши", warm3.why)
        self.assertGreater(top.interest, warm3.interest)

    def test_chat_pairs(self):
        from parsing.smart import interest
        sources = {i: Source(i, f"Чат {i}", f"chat{i}", "group") for i in (1, 2, 3)}
        pairs = interest.chat_pairs({1: {1, 2, 3}, 2: {2, 3, 4}, 3: {9}}, sources)
        self.assertEqual(pairs[0][:3], ("@chat1", "@chat2", 2))
        self.assertEqual(len(pairs), 1)


class DiscoveryQualityTests(unittest.TestCase):
    def _env(self):
        from unittest import mock
        real_sleep = asyncio.sleep

        async def fast_sleep(*_a, **_k):
            await real_sleep(0)
        return mock.patch.object(asyncio, "sleep", fast_sleep)

    def test_message_search_uses_old_hits_to_find_chats(self):
        from parsing.smart.run import run_smart_parse
        from parsing.smart.store import Store
        with self._env(), tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            client.search_map = {}
            params = P(channel="@client_crm", no_llm=True, check_contacted=False, people=5, days=14,
                       db_path=os.path.join(tmp, "s.db"), profiles_dir=os.path.join(tmp, "p"),
                       out=os.path.join(tmp, "o.xlsx"))
            asyncio.run(run_smart_parse(params, client=client, echo=False))
            st = Store(params.db_path)
            via = {s.username: (s.found_via, s.reason) for s in st.load_decisions(params.channel)}
            st.close()
            self.assertIn("old_buyers", via, "чат найден по сообщению 60-дневной давности")
            self.assertIn("фраза:", via["old_buyers"][0])
            self.assertIn("мёртвый чат", via["old_buyers"][1], "а мёртвым его признаёт уже проба")

    def test_chat_without_product_mentions_is_rejected_by_probe(self):
        from parsing.smart.probe import Probe, evaluate
        from parsing.smart.store import Store
        lex = tp.Lexicon()
        with tempfile.TemporaryDirectory() as tmp:
            st = Store(os.path.join(tmp, "s.db"))
            prof = Profile(channel="x", offer="Оптовая продажа техники Apple: iPhone, MacBook",
                           topic_terms=["iphone", "macbook", "apple", "техника оптом"],
                           product_keywords=["айфон", "iphone", "макбук"])
            mk = lambda sid, text, i: Message(sid, i, ago(i), 100 + i, "user", text)  # noqa: E731
            logistics = [mk(1, "Подскажите по логистике Ozon, когда приёмка на складе?", i) for i in range(1, 15)]
            on_topic = [mk(2, "Куплю iPhone 14 Pro оптом, сколько стоит партия?" if i % 3 == 0
                           else "Привет всем, как дела в этом чате сегодня", i) for i in range(1, 15)]
            probes = {1: Probe(msgs=logistics, activity=5, newest_age_days=0.1),
                      2: Probe(msgs=on_topic, activity=5, newest_age_days=0.1)}
            evaluate(probes, prof, lex, P(channel="x"), st)
            st.close()
            self.assertIn("не упоминается товар клиента", probes[1].reason)
            self.assertEqual(probes[1].requests, 0)
            self.assertEqual(probes[2].reason, "")
            self.assertGreater(probes[2].product_msgs, 0)
            self.assertGreater(probes[2].requests, 0)

    def test_catalog_contains_only_productive_chats(self):
        from parsing.smart.store import Store
        with tempfile.TemporaryDirectory() as tmp:
            st = Store(os.path.join(tmp, "s.db"))
            for cid, name in ((1, "good"), (2, "weak")):
                src = Source(cid, name, name, "group")
                st.upsert_source(src)
                st.save_probe(cid, "ru", 1.0, 5.0, ["айфон", "оптом"], [])
            self.assertEqual(st.catalog(30), [], "ни один чат пока не давал людей")
            st.mark_productive(1)
            self.assertEqual([s.username for s, _ in st.catalog(30)], ["good"])
            st.close()

    def test_time_limit_stops_and_still_builds_report(self):
        from openpyxl import load_workbook

        from parsing.smart.run import run_smart_parse
        with self._env(), tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            params = P(channel="@client_crm", no_llm=True, check_contacted=False, people=500,
                       max_minutes=0.0001, db_path=os.path.join(tmp, "s.db"),
                       profiles_dir=os.path.join(tmp, "p"), out=os.path.join(tmp, "o.xlsx"))
            path = asyncio.run(run_smart_parse(params, client=client, echo=False))
            log = " ".join(str(r[3]) for r in load_workbook(path)["Лог"].iter_rows(values_only=True))
            self.assertIn("лимит времени", log)
            self.assertTrue(os.path.exists(path))


class LLMChainTests(unittest.TestCase):
    def test_failover_and_disable(self):
        from unittest import mock

        from parsing.smart import llm_chain
        from parsing.smart.runlog import RunLog

        calls = []

        async def fake_call(spec, prompt, max_tokens):
            calls.append(spec)
            if spec.startswith("openrouter"):
                raise RuntimeError("429 rate limit")
            return f"ответ от {spec}"

        log = RunLog(echo=False)
        chain = llm_chain.Chain(["openrouter:free", "groq:llama"], log)
        with mock.patch.object(llm_chain, "_call", fake_call):
            for _ in range(3):
                self.assertEqual(asyncio.run(chain.complete("q", 10)), "ответ от groq:llama")
            # openrouter два раза подряд упал и отключён — третий запрос идёт сразу в groq
            self.assertEqual(calls, ["openrouter:free", "groq:llama", "openrouter:free", "groq:llama",
                                     "groq:llama"])
            self.assertIn("отключён до конца запуска", " ".join(r[3] for r in log.rows))
            self.assertEqual(chain.usage(), "groq 3")

            dead = llm_chain.Chain(["openrouter:free"], None)
            with self.assertRaisesRegex(RuntimeError, "все LLM-провайдеры недоступны"):
                asyncio.run(dead.complete("q", 10))

    def test_model_404_picks_another_model(self):
        from unittest import mock

        from parsing.smart import llm_chain
        from parsing.smart.runlog import RunLog

        async def fake_call(spec, prompt, max_tokens):
            if spec == "groq:old-model":
                raise RuntimeError("groq 404: The model `old-model` does not exist")
            return f"ответ от {spec}"

        async def fake_models(prov):
            return ["whisper-large-v3", "llama-guard-4", "llama-3.1-8b-instant", "openai/gpt-oss-120b"]

        log = RunLog(echo=False)
        chain = llm_chain.Chain(["groq:old-model"], log)
        with mock.patch.object(llm_chain, "_call", fake_call), \
                mock.patch.object(llm_chain, "list_models", fake_models):
            self.assertEqual(asyncio.run(chain.complete("q", 10)), "ответ от groq:openai/gpt-oss-120b")
            self.assertEqual(chain.specs, ["groq:openai/gpt-oss-120b"])
            self.assertIn("LLM_FAST=groq:openai/gpt-oss-120b", " ".join(r[3] for r in log.rows))
            # дальше работает уже новая модель, без повторного поиска
            self.assertEqual(asyncio.run(chain.complete("q", 10)), "ответ от groq:openai/gpt-oss-120b")

    def test_rate_limit_wait_and_retry_same_provider(self):
        from unittest import mock

        from parsing.smart import llm_chain

        self.assertAlmostEqual(llm_chain.rate_limit_wait(
            "groq 429: Rate limit reached ... Please try again in 3.4s."), 3.4)
        self.assertAlmostEqual(llm_chain.rate_limit_wait("429 ... try again in 1m2.5s"), 62.5)
        self.assertAlmostEqual(llm_chain.rate_limit_wait("429 try again in 250ms"), 0.25)
        self.assertIsNone(llm_chain.rate_limit_wait("500 overloaded"))
        self.assertAlmostEqual(llm_chain.rate_limit_wait("429 Rate limit. Please try again in 3h20m5.5s."),
                               3 * 3600 + 20 * 60 + 5.5)
        self.assertAlmostEqual(llm_chain.rate_limit_wait("429 try again in 6m30s"), 390.0)

        calls = []

        async def flaky(spec, prompt, max_tokens):
            calls.append(spec)
            if len(calls) == 1:
                raise RuntimeError("groq 429: Rate limit reached. Please try again in 0.01s.")
            return "ok"

        real_sleep = asyncio.sleep

        async def fast_sleep(*_a, **_k):
            await real_sleep(0)
        chain = llm_chain.Chain(["groq:m"], None)
        with mock.patch.object(llm_chain, "_call", flaky), mock.patch.object(asyncio, "sleep", fast_sleep):
            self.assertEqual(asyncio.run(chain.complete("q", 10)), "ok")
        self.assertEqual(calls, ["groq:m", "groq:m"])
        self.assertEqual(chain.fails["groq:m"], 0, "ожидание по лимиту — не ошибка провайдера")

    def test_daily_limit_switches_to_next_model_at_once(self):
        from unittest import mock

        from parsing.smart import llm_chain
        from parsing.smart.runlog import RunLog

        calls = []

        async def fake(spec, prompt, max_tokens):
            calls.append(spec)
            if spec == "groq:big":
                raise RuntimeError("groq 429: Rate limit reached for tokens per day (TPD). "
                                   "Please try again in 4h12m3s.")
            return "ответ " + spec

        log = RunLog(echo=False)
        chain = llm_chain.Chain(["groq:big", "groq:small"], log)
        with mock.patch.object(llm_chain, "_call", fake):
            self.assertEqual(asyncio.run(chain.complete("q", 10)), "ответ groq:small")
            self.assertEqual(asyncio.run(chain.complete("q", 10)), "ответ groq:small")
        self.assertEqual(calls, ["groq:big", "groq:small", "groq:small"],
                         "суточный лимит: big не ждём и больше не пробуем")
        self.assertIn("отключён до конца запуска", " ".join(r[3] for r in log.rows))

    def test_think_block_is_stripped_from_json(self):
        from parsing.smart.llm_judge import parse_json_loose
        self.assertEqual(parse_json_loose("<think>сначала [подумаю] {так}</think>\n[\"a\", \"b\"]"), ["a", "b"])

    def test_build_chain_free_only_by_default(self):
        from unittest import mock

        try:
            with mock.patch.dict(os.environ, {"TELEGRAM_API_ID": os.environ.get("TELEGRAM_API_ID") or "1",
                                              "TELEGRAM_API_HASH": os.environ.get("TELEGRAM_API_HASH") or "x"}):
                from api import llm  # noqa: F401
        except Exception as e:  # noqa: BLE001
            self.skipTest(f"нет зависимостей проекта: {e}")
        from parsing.smart import llm_chain

        env = {"OPENROUTER_API_KEY": "k1", "GROQ_API_KEY": "k2", "XAI_API_KEY": "k3",
               "DEEPSEEK_API_KEY": "k4", "ANTHROPIC_API_KEY": "k5",
               "LLM_FAST": "openrouter:nvidia/x:free", "SMART_LLM_CHAIN": ""}
        with mock.patch.dict(os.environ, env):
            chain, info = llm_chain.build_chain()
            self.assertEqual(chain.specs, ["openrouter:nvidia/x:free", "groq:openai/gpt-oss-120b",
                                           "groq:qwen/qwen3.8-27b", "groq:openai/gpt-oss-20b"])
            custom, _ = llm_chain.build_chain("deepseek:deepseek-chat, groq:m")
            self.assertEqual(custom.specs, ["deepseek:deepseek-chat", "groq:m"])
        with mock.patch.dict(os.environ, {**env, "GROQ_API_KEY": "", "OPENROUTER_API_KEY": ""}):
            chain, info = llm_chain.build_chain()
            self.assertIsNone(chain)
            self.assertIn("нет ни одного ключа", info)


class AccountFallbackTests(unittest.TestCase):
    def test_skips_dead_accounts(self):
        from unittest import mock

        try:
            from telethon.errors import AuthKeyDuplicatedError
            # config.py падает на пустых ключах в .env — тесту реальные ключи не нужны
            with mock.patch.dict(os.environ, {"TELEGRAM_API_ID": os.environ.get("TELEGRAM_API_ID") or "1",
                                              "TELEGRAM_API_HASH": os.environ.get("TELEGRAM_API_HASH") or "x"}):
                import accounts.manager  # noqa: F401
        except Exception as e:  # noqa: BLE001
            self.skipTest(f"нет зависимостей проекта: {e}")
        from parsing.smart import run as runmod
        from parsing.smart.runlog import RunLog

        class Fake:
            def __init__(self, name, fail=None, authorized=True):
                self.name, self.fail, self.authorized, self.closed = name, fail, authorized, False

            async def connect(self):
                if self.fail:
                    raise self.fail

            async def is_user_authorized(self):
                return self.authorized

            async def get_me(self):
                return object()

            async def disconnect(self):
                self.closed = True

        clients = {
            "sessions/+1.session": Fake("+1", fail=AuthKeyDuplicatedError(request=None)),
            "sessions/+2.session": Fake("+2", authorized=False),
            "sessions/+3.session": Fake("+3"),
        }
        log = RunLog(echo=False)
        with mock.patch("accounts.manager.get_session_files", return_value=list(clients)), \
                mock.patch("accounts.manager.create_client", side_effect=lambda p: clients[p]):
            got = asyncio.run(runmod._open_client(P(channel="x", session="+2"), log))
            self.assertIs(got, clients["sessions/+3.session"])
            self.assertTrue(clients["sessions/+1.session"].closed)
            self.assertTrue(clients["sessions/+2.session"].closed)
            text = " ".join(r[3] for r in log.rows)
            self.assertIn("аккаунт +2 не работает", text)   # --session пробуется первым
            self.assertIn("аннулирован", text)
            self.assertIn("читающий аккаунт: +3", text)

            for c in clients.values():
                c.fail = AuthKeyDuplicatedError(request=None)
            with self.assertRaisesRegex(RuntimeError, "ни один аккаунт не подключился"):
                asyncio.run(runmod._open_client(P(channel="x"), RunLog(echo=False)))


class LLMJudgeTests(unittest.TestCase):
    def test_batch_cache_and_seller_drop(self):
        from parsing.smart import llm_judge, scoring
        from parsing.smart.runlog import RunLog

        calls = []

        async def fake_complete(prompt, max_tokens):
            calls.append(prompt)
            return "```json\n" + json.dumps([
                {"id": 1, "role": "buyer", "is_request": True, "topic": 0.9, "specific": 0.8,
                 "intent": "vendor", "dm": 0.7, "why": "ищет интегратора"},
                {"id": 2, "role": "seller", "is_request": False, "topic": 0.8, "specific": 0.1,
                 "intent": "none", "dm": 0.0, "why": "реклама услуг"},
            ], ensure_ascii=False) + "\n```"

        saved = llm_judge.llm_complete
        llm_judge.llm_complete = fake_complete
        try:
            with tempfile.TemporaryDirectory() as tmp:
                st = Store(os.path.join(tmp, "s.db"))
                prof = Profile(channel="x", offer="интеграция CRM", topic_terms=["crm"])
                params = P(channel="x", llm_delay=0, llm_budget=5)
                log = RunLog(echo=False)
                judge = llm_judge.LLMJudge(st, prof, params, log)
                items = [("k1", "Ищу интегратора CRM", ""), ("k2", "Внедряем CRM недорого", "")]
                v1 = asyncio.run(judge.judge(items))
                v2 = asyncio.run(llm_judge.LLMJudge(st, prof, params, log).judge(items))
                self.assertEqual(len(calls), 1, "второй прогон должен взять ответы из кеша")
                self.assertEqual(v1["k1"].role, "buyer")
                self.assertEqual(v2["k2"].role, "seller")

                lex = tp.Lexicon()
                src = Source(1, "t", "t")

                def cand(key_id, text):
                    m = Message(1, key_id, NOW, key_id, "user", text)
                    c = scoring.Cand(msg=m, source=src, author=None, f=ft.extract(text, lex))
                    c.f.intent_rule, c.r, c.fresh, c.t = 0.6, 0.6, 1.0, 1.0
                    return c

                c1, c2 = cand(1, "Ищу интегратора CRM"), cand(2, "Внедряем CRM недорого")
                prep = scoring.Prep(cands=[c1, c2], dropped=[], src_stats={}, opt_out=set())
                scoring.apply_verdicts(prep, {c1.msg.key: v1["k1"], c2.msg.key: v1["k2"]}, params, lex)
                self.assertEqual(prep.cands, [c1])
                self.assertAlmostEqual(c1.i, 0.6 + 0.4 * 0.8)
                self.assertEqual(c2.drop, "продавец (LLM)")
                st.close()
        finally:
            llm_judge.llm_complete = saved


class KeywordQualityTests(unittest.TestCase):
    """Слова товара: без выдумок, целым словом, с проверкой в Telegram; ранжирование без
    «вытягивания» слабых запросов одними чатами."""

    def test_clean_product_keywords_drops_generic_and_hyphenated(self):
        from parsing.smart.profile import clean_product_keywords
        got = clean_product_keywords(["AI-онбординг", "ai", "курсы", "нейросети", "Нейросети", "chatgpt"])
        self.assertEqual(got, ["нейросети", "chatgpt"])

    def test_product_match_is_whole_phrase(self):
        from parsing.smart.keywords import product_matcher
        prof = Profile(channel="x", product_keywords=["ai", "промпт инженер", "нейросети"])
        m = product_matcher(prof)
        self.assertFalse(m.find(tp.lemmas("Продаю AI-аккаунты для WB")), "общее «ai» не в счёт")
        self.assertFalse(m.find(tp.lemmas("нужен промпт для карточки")), "фраза — только целиком")
        self.assertTrue(m.find(tp.lemmas("Какие нейросети посоветуете для текстов?")))

    def test_ground_profile_drops_terms_without_hits(self):
        from types import SimpleNamespace as NS

        from telethon.tl.types import Channel

        from parsing.smart.keywords import ground_profile

        def chan(left):
            return Channel(id=5, title="t", photo=None, date=NOW, username="pub", left=left)

        class Client:
            async def _gen(self, term):
                rows = {"нейросети": 3, "посоветуйте нейросеть": 2}.get(term, 0)
                for i in range(rows):
                    yield NS(chat=chan(True), date=ago(5))
                yield NS(chat=chan(False), date=ago(5))       # свой чат — не считается

            def iter_messages(self, entity, search="", limit=0):
                return self._gen(search)

        class Log:
            def info(self, *a): pass
            def warn(self, *a): pass
            def step(self, *a): pass

        from unittest import mock
        real_sleep = asyncio.sleep

        async def fast_sleep(*_a, **_k):
            await real_sleep(0)
        prof = Profile(channel="x", product_keywords=["нейрокурсы", "нейросети", "chatgpt", "промпты"],
                       buyer_phrases=["хочу нейрообучение", "посоветуйте нейросеть"])
        with mock.patch.object(asyncio, "sleep", fast_sleep):
            changed = asyncio.run(ground_profile(Client(), prof, P(channel="@x"), Log()))
        self.assertTrue(changed)
        self.assertEqual(prof.product_keywords[0], "нейросети", "слово с совпадениями — первым")
        self.assertEqual(prof.buyer_phrases[0], "посоветуйте нейросеть")
        self.assertEqual(prof.keyword_hits["нейрокурсы"], 0)
        self.assertTrue(prof.keywords_checked)
        self.assertFalse(asyncio.run(ground_profile(Client(), prof, P(channel="@x"), Log())),
                         "проверка — один раз на профиль")

    def test_hot_gate_weak_request_not_lifted_by_affinity(self):
        from parsing.smart import interest, scoring
        lex = tp.Lexicon()
        src = Source(1, "t", "t")
        params = P(channel="@x")

        def hot(uid, pqi):
            m = Message(1, uid, NOW, uid, "user", "вопрос")
            c = scoring.Cand(msg=m, source=src, author=Author(user_id=uid, username=f"u{uid}"),
                             f=ft.Features())
            c.kind, c.pqi, c.a = "hot", pqi, 0.3
            return c
        wide = interest.PersonStats(active={1, 2, 3, 4, 5}, total=5, topic_msgs=5)
        stats = {1: wide, 2: wide}
        weights = {i: 1.0 for i in range(1, 6)}
        ranked, _ = interest.score_all([hot(1, 2.7), hot(2, 30.0)], stats, weights,
                                       {1: src}, lex, params, NOW)
        weak = next(c for c in ranked if c.msg.sender_id == 1)
        self.assertLess(weak.interest, 10, "PQI 2.7 не вытягивается широким охватом")
        self.assertEqual(ranked[0].msg.sender_id, 2)

    def test_chat_weight_penalised_when_product_not_discussed(self):
        from parsing.smart import interest
        srcs = {1: Source(1, "a", "a"), 2: Source(2, "b", "b")}
        w, _, _ = interest.chat_weights(srcs, {1: {"cqi_n": 1.0}, 2: {"cqi_n": 1.0}}, {}, set(),
                                        {1: 0.0, 2: 0.1})
        self.assertAlmostEqual(w[1], 0.2)
        self.assertAlmostEqual(w[2], 1.0)

    def test_harvest_terms(self):
        from parsing.smart.keywords import harvest_terms
        prof = Profile(channel="x", product_keywords=["ai", "нейросети", "chatgpt"],
                       buyer_phrases=["посоветуйте нейросеть", "хочу нейрообучение"],
                       keyword_hits={"посоветуйте нейросеть": 4, "хочу нейрообучение": 0})
        self.assertEqual(harvest_terms(prof, P(channel="@x")),
                         ["нейросети", "chatgpt", "посоветуйте нейросеть"])
        self.assertEqual(harvest_terms(prof, P(channel="@x", targeted=False)), [])


if __name__ == "__main__":
    unittest.main()
