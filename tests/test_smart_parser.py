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
from parsing.smart.profile import Profile, extract_seeds, top_terms
from parsing.smart.settings import Params, normalize_channel
from parsing.smart.store import Store

NOW = datetime.now(timezone.utc)


def ago(hours: float) -> datetime:
    return NOW - timedelta(hours=hours)


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

    def test_seeds_and_terms(self):
        seeds = extract_seeds(["Наш чат: https://t.me/crm_talk и @bitrix_club, бот @helpbot"], own="me")
        self.assertEqual(seeds, ["crm_talk", "bitrix_club"])
        terms = top_terms(["Интеграция Битрикс24 с 1С", "Интеграция CRM и 1С", "CRM для продаж"],
                          tp.Lexicon().stopwords)
        self.assertTrue(any("интеграц" in t for t in terms), terms)


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
    params = Params(channel="@client_crm", days=14, offline=True, no_llm=True, check_contacted=False,
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
            self.assertEqual(wb.sheetnames, ["Люди", "Без username", "Источники", "Отсеянные",
                                             "Профиль", "Лог"])
            ws = wb["Люди"]
            header = [c.value for c in ws[1]]
            rows = [dict(zip(header, [c.value for c in r])) for r in ws.iter_rows(min_row=2)]
            names = [r["Username"] for r in rows]
            pqi = [r["PQI"] for r in rows]

            self.assertEqual(pqi, sorted(pqi, reverse=True), "люди не отсортированы по PQI")
            # ЛПР + приглашение в ЛС + свежо + без конкурентов — первая
            self.assertEqual(names[0], "@anna_ceo", names)
            self.assertEqual(set(names[:3]), {"@anna_ceo", "@formula", "@ivan_owner"}, names)
            # вопрос в комментарии под профильным постом находится через контекст поста
            for must in ("@petr", "@q_user"):
                self.assertIn(must, names)
            self.assertEqual(names[-1], "@old_req", "запрос 13-дневной давности — в самом низу")
            self.assertEqual(rows[0]["Уровень"], "A")
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

            no_u = [c.value for c in wb["Без username"]["E"]][1:]
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

        def msg(mid, hours, text, u=None, reply_to=0, replies=0):
            return NS(id=mid, date=ago(hours), message=text, sender=u,
                      from_id=PeerUser(u.id) if u else None, fwd_from=None, chat=None,
                      reply_to=NS(reply_to_msg_id=reply_to) if reply_to else None,
                      replies=NS(replies=replies, max_id=9000 + replies) if replies else None)

        self.me = User(id=999, first_name="reader")
        self.client_ch = ch(500, "CRM-интегратор", "client_crm")
        self.g1 = ch(1001, "CRM и автоматизация бизнеса", "crm_chat", group=True, members=4000, left=False)
        self.g2 = ch(1002, "Котики и мемы", "cats_chat", group=True, members=8000)
        self.c3 = ch(1003, "CRM новости", "crm_news", members=9000)
        self.c4 = ch(1004, "Блог про CRM", "crm_blog", members=3000)
        self.channels = [self.client_ch, self.g1, self.g2, self.c3, self.c4]
        self.full = {500: ("Внедряем CRM и интегрируем Битрикс24 с 1С", 0),
                     1001: ("Чат про CRM, Битрикс24 и автоматизацию продаж", 0),
                     1002: ("Котики", 0), 1003: ("Новости CRM", 5003), 1004: ("Блог", 0)}
        buyer, ad, cat, commenter = (user(1, "buyer_ivan", "Иван"), user(2, "studio", "Студия"),
                                     user(3, "cat_lover", "Кот"), user(4, "q_user", "Кирилл"))
        self.about = {1: "Основатель сети магазинов"}
        self.history = {
            500: [msg(1, 30, "Интегрировали Битрикс24 с 1С для сети магазинов. Наш чат: t.me/crm_chat"),
                  msg(2, 60, "Как внедрение CRM ускоряет отдел продаж: воронка продаж в Битрикс24"),
                  msg(3, 90, "Интеграция 1С и CRM: кейс автоматизации продаж")],
            1001: [msg(10, 5, "Ищем подрядчика на интеграцию Битрикс24 с 1С, бюджет 150к. Кто делал?", buyer),
                   msg(11, 4, "Делаем интеграции Битрикс24 под ключ, портфолио https://x.ru", ad)],
            1002: [msg(20, 3, "Посоветуйте корм для кота, мой не ест", cat)],
            1003: [msg(100, 20, "Битрикс24 или amoCRM: что выбрать для отдела продаж", replies=1)],
        }
        self.comments = {(1003, 100): [msg(9001, 18, "Сколько стоит такое внедрение CRM?", commenter,
                                           reply_to=100)]}
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
                                             linked_chat_id=linked))
        if name == "SearchRequest":
            return self.NS(chats=[self.g1, self.g2, self.c3, self.c4])
        if name == "GetChannelRecommendationsRequest":
            return self.NS(chats=[self.c3])
        if name == "GetFullUserRequest":
            self.full_user_calls += 1
            return self.NS(full_user=self.NS(about=self.about.get(req.id, "")))
        raise AssertionError(f"неожиданный запрос {name}")

    def iter_messages(self, entity, limit=None, min_id=0, wait_time=None, search=None,
                      reply_to=None, **_):
        async def gen():
            if entity is None:
                items = [m for m in self.history[1001] if search and search.split()[0][:3].lower()
                         in m.message.lower()]
                for m in items:
                    m.chat = self.g1
            elif reply_to:
                items = self.comments.get((entity.id, reply_to), [])
            else:
                items = self.history.get(entity.id, [])
            items = sorted((m for m in items if m.id > (min_id or 0)), key=lambda m: -m.id)
            for m in items[:limit]:
                yield m
        return gen()

    def iter_dialogs(self, limit=None):
        async def gen():
            yield self.NS(entity=self.g1)
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
            params = Params(channel="@client_crm", no_llm=True, check_contacted=False, max_sources=5,
                            db_path=os.path.join(tmp, "s.db"), profiles_dir=os.path.join(tmp, "p"),
                            out=os.path.join(tmp, "o", "a.xlsx"))
            path = asyncio.run(run_smart_parse(params, client=client, echo=False))
            wb = load_workbook(path)

            src = {r[0]: r for r in wb["Источники"].iter_rows(min_row=2, values_only=True)}
            self.assertEqual(src["CRM и автоматизация бизнеса"][5], "взят")
            self.assertIn("ссылка в постах клиента", src["CRM и автоматизация бизнеса"][4])
            self.assertEqual(src["Блог про CRM"][6], "у канала нет комментариев")
            self.assertNotIn("CRM-интегратор", src, "свой канал клиента не должен быть источником")

            ws = wb["Люди"]
            header = [c.value for c in ws[1]]
            rows = [dict(zip(header, r)) for r in ws.iter_rows(min_row=2, values_only=True)]
            names = [r["Username"] for r in rows]
            self.assertEqual(names[0], "@buyer_ivan", names)
            self.assertIn("@q_user", names)
            self.assertNotIn("@studio", names)
            self.assertNotIn("@cat_lover", names)
            self.assertIn("ЛПР (био: «основатель»)", rows[0]["Почему в списке"])
            self.assertTrue(rows[0]["Сообщение"])
            link = ws.cell(row=2, column=header.index("Сообщение") + 1).hyperlink.target
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
            rows2 = list(load_workbook(path2)["Люди"].iter_rows(min_row=2, values_only=True))
            self.assertEqual(rows2[0][3], "@buyer_ivan")


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
                params = Params(channel="x", llm_delay=0, llm_budget=5)
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


if __name__ == "__main__":
    unittest.main()
