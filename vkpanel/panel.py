# -*- coding: utf-8 -*-
"""
Панель управления VK-рассылкой в Telegram. Доступ: только VKPANEL_ADMINS (имена без @ или числовые id).

Что умеет: подобрать ЦА (студенты СПб, мужчины, алгоритм из «Кто ЦА»), показать топ, запустить рассылку
с реферальной ссылкой Яндекс Еды, показать ответы, вести токены аккаунтов VK, покупать аккаунты на DarkStore,
менять ссылку, лимиты и тексты. Долгие дела идут в фоне, ход виден в сообщении, остановить можно кнопкой.
"""
import asyncio
import logging
import os
import re
import tempfile
import time
from datetime import datetime

from vk import audience, messages, replies, store
from vk import config as cfg
from vk.api import VKApi, VKError
from vk.sender import daily_limit, render, run_broadcast, _usable

from . import shop
from .jobs import Jobs
from .tg import Tg

log = logging.getLogger(__name__)

REPLY_POLL_SEC = 300
SLEEP = time.sleep                   # паузы рассылки; в тестах подменяется
TOKEN_RE = re.compile(r"[A-Za-z0-9_.\-]{50,}")
SECRET_TTL_SEC = 90                 # логины и пароли в чате удаляются через столько секунд

MAIN_KB = [
    [("📊 Статус", "st")],
    [("🎯 Подобрать ЦА", "au"), ("📋 Топ ЦА", "tp:0")],
    [("✉️ Рассылка", "bc"), ("💬 Ответы", "rp")],
    [("🔑 Аккаунты VK", "ac"), ("🛒 DarkStore", "ds")],
    [("⚙️ Настройки", "se")],
]


def kb(rows: list[list[tuple[str, str]]]) -> list:
    return [[{"text": t, "callback_data": d} for t, d in row] for row in rows]


def back(*extra: tuple[str, str]) -> list:
    return kb([list(extra), [("« Меню", "m")]] if extra else [[("« Меню", "m")]])


def parse_admins() -> list[str]:
    return [x.strip().lstrip("@").lower() for x in os.getenv("VKPANEL_ADMINS", "").split(",") if x.strip()]


def person(r: dict) -> str:
    return " ".join(x for x in (r.get("first_name"), r.get("last_name")) if x) or f"id{r['user_id']}"


def fmt_target(i: int, r: dict) -> str:
    uni = f" · {(r.get('university') or '')[:28]} ({r.get('start_year')})" if r.get("university") else ""
    return (f"{i}. {person(r)}\n   vk.com/id{r['user_id']} · score {r['score']:.2f} · сообществ {r['communities']}"
            f" · a {r['a']:.2f} b {r['b']:.2f}{uni}")


def read_token() -> str | None:
    """Токен для чтения (поиск сообществ, участники): VK_TOKEN или первый живой аккаунт."""
    token = os.getenv("VK_TOKEN", "").strip()
    if token:
        return token
    live = [a for a in store.list_accounts() if a["status"] != "dead"]
    return live[0]["token"] if live else None


class Panel:
    def __init__(self, tg: Tg, admins: list[str]) -> None:
        self.tg, self.admins = tg, admins
        self.jobs = Jobs()
        self.awaiting: dict[int, str] = {}          # chat_id -> что ждём от человека (токен, ссылка…)
        self.pending_refs: dict[int, list[str]] = {}

    # ── доступ ────────────────────────────────────────────────────────────
    def admin_ids(self) -> list[int]:
        ids = [int(x) for x in self.admins if x.isdigit()]
        for a in self.admins:
            raw = store.get_setting(f"panel_admin:{a}")
            if raw and raw.isdigit():
                ids.append(int(raw))
        return sorted(set(ids))

    def is_admin(self, frm: dict) -> bool:
        uid, uname = int(frm.get("id") or 0), (frm.get("username") or "").lower()
        if str(uid) in self.admins:
            return True
        if uname and uname in self.admins:
            known = store.get_setting(f"panel_admin:{uname}")
            if known and known != str(uid):
                return False                       # имя в Telegram перешло к другому человеку
            if not known:
                store.set_setting(f"panel_admin:{uname}", str(uid))
            return True
        return False

    # ── цикл ──────────────────────────────────────────────────────────────
    async def run(self) -> None:
        asyncio.create_task(self.replies_loop())
        offset = int(store.get_setting("panel_offset") or 0)
        while True:
            try:
                for u in await self.tg.updates(offset):
                    offset = u["update_id"] + 1
                    store.set_setting("panel_offset", str(offset))
                    try:
                        if "callback_query" in u:
                            await self.on_callback(u["callback_query"])
                        elif "message" in u:
                            await self.on_message(u["message"])
                    except Exception:  # noqa: BLE001 — одна ошибка не должна останавливать панель
                        log.exception("[vkpanel] обработка апдейта")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — сеть, 502 Telegram: пробуем снова
                log.warning("[vkpanel] getUpdates: %s", str(e)[:120])
                await asyncio.sleep(5)

    async def replies_loop(self) -> None:
        """Раз в несколько минут смотрим ответы людей и пишем админам."""
        while True:
            await asyncio.sleep(REPLY_POLL_SEC)
            try:
                if not store.list_accounts():
                    continue
                fresh = await asyncio.to_thread(replies.poll_replies, VKApi)
                for r in fresh:
                    await self.notify(f"💬 Ответ из VK\n{await asyncio.to_thread(self.who, r['user_id'])}: {r['text'][:500]}\n"
                                      f"открыть диалог: https://vk.com/im?sel={r['user_id']}")
            except Exception:  # noqa: BLE001
                log.exception("[vkpanel] проверка ответов")

    def who(self, user_id: int) -> str:
        t = next((x for x in store.list_targets(None) if x["user_id"] == user_id), None)
        return f"{person(t)} (vk.com/id{user_id})" if t else f"vk.com/id{user_id}"

    async def notify(self, text: str) -> None:
        for chat in self.admin_ids():
            await self.tg.send(chat, text)

    # ── сообщения ─────────────────────────────────────────────────────────
    async def on_message(self, msg: dict) -> None:
        frm, chat = msg.get("from") or {}, (msg.get("chat") or {}).get("id")
        if not chat or (msg.get("chat") or {}).get("type") != "private" or not self.is_admin(frm):
            return                                   # чужим не отвечаем вовсе
        text = (msg.get("text") or "").strip()
        wait = self.awaiting.pop(chat, None)
        if text.startswith("/"):
            cmd, _, arg = text.partition(" ")
            return await self.command(chat, cmd.split("@")[0].lower(), arg.strip(), msg)
        if wait:
            return await self.answer_wait(chat, wait, text, msg)
        if TOKEN_RE.search(text):
            return await self.add_tokens(chat, text, msg)       # просто вставили токен
        await self.menu(chat)

    async def command(self, chat: int, cmd: str, arg: str, msg: dict) -> None:
        if cmd in ("/start", "/menu"):
            return await self.menu(chat)
        if cmd == "/status":
            return await self.status(chat)
        if cmd == "/link":
            return await self.set_link(chat, arg)
        if cmd == "/limit":
            return await self.set_limit(chat, arg)
        if cmd == "/token":
            return await self.add_tokens(chat, arg, msg)
        await self.menu(chat)

    async def answer_wait(self, chat: int, wait: str, text: str, msg: dict) -> None:
        if wait == "token":
            await self.add_tokens(chat, text, msg)
        elif wait == "link":
            await self.set_link(chat, text)
        elif wait == "limit":
            await self.set_limit(chat, text)
        elif wait == "app":
            if text.isdigit():
                store.set_setting("app_id", text)
                await self.tg.send(chat, f"✓ ID приложения VK {text} сохранён.", back(("🔑 Аккаунты", "ac")))
            else:
                await self.tg.send(chat, "Нужны только цифры (ID приложения).", back())
        elif wait == "refs":
            refs = [r for r in re.split(r"[\s,;]+", text) if r]
            await self.start_collect(chat, refs or None)
        elif wait == "varadd":
            if "{link}" not in text:
                return await self.tg.send(chat, "В тексте нужна вставка {link}: туда подставится ссылка. Пришлите текст ещё раз.", back())
            items = messages.variants() if store.get_setting("variants") else []
            items.append((f"s{len(items) + 1}", text))
            messages.set_variants(items)
            await self.tg.send(chat, f"✓ Вариант добавлен. Теперь своих вариантов: {len(items)} (встроенные отключены).", back(("⚙️ Настройки", "se")))

    # ── меню и статус ─────────────────────────────────────────────────────
    async def menu(self, chat: int) -> None:
        await self.tg.send(chat, "VK-панель: привлечение курьеров Яндекс Еды.\nЦА: студенты СПб, мужчины.", kb(MAIN_KB))

    async def status(self, chat: int) -> None:
        accs, c, today = store.list_accounts(), store.counts(), store.sends_today()
        now = datetime.now().astimezone()
        by = {"active": 0, "resting": 0, "dead": 0}
        for a in accs:
            by[a["status"]] = by.get(a["status"], 0) + 1
        usable = [a for a in accs if _usable(a, now)]
        capacity = sum(daily_limit() - store.sent_last_24h(a["id"]) for a in usable)
        shop_line = ""
        try:
            bal, cur = await asyncio.to_thread(shop.balance)
            shop_line = f"\nDarkStore: баланс {bal:g} {cur}"
        except shop.ShopError as e:
            shop_line = f"\nDarkStore: {str(e)[:80]}"
        link = messages.ref_link()
        text = (f"📊 Статус\n"
                f"Аккаунтов VK: {len(accs)} (активны {by['active']}, отдыхают {by['resting']}, мертвы {by['dead']})\n"
                f"Сейчас можно отправить: ≈{capacity} (лимит {daily_limit()} на аккаунт в сутки)\n"
                f"ЦА в базе: новых {c.get('new', 0)}, отправлено {c.get('sent', 0)}, нельзя написать {c.get('closed', 0)}, ошибок {c.get('failed', 0)}\n"
                f"За 24 ч: отправлено {today.get('sent', 0)}\n"
                f"Реферальная ссылка: {'задана' if link else 'НЕ ЗАДАНА (кнопка «Настройки»)'}\n"
                f"Вариантов текста: {len(messages.variants())}{shop_line}")
        for j in self.jobs.active.values():
            if not j.done:
                text += f"\n⏳ Идёт: {j.title} ({j.elapsed()})"
        stats = store.variant_stats()
        if stats:
            text += "\n\nВарианты текста (отправлено / ответили):\n" + "\n".join(f"  {s['variant']}: {s['sent']} / {s['replied']}" for s in stats)
        await self.tg.send(chat, text, back(("🔄 Обновить", "st")))

    # ── кнопки ────────────────────────────────────────────────────────────
    async def on_callback(self, cb: dict) -> None:
        frm, msg = cb.get("from") or {}, cb.get("message") or {}
        chat, data = (msg.get("chat") or {}).get("id"), cb.get("data") or ""
        if not chat or not self.is_admin(frm):
            return
        await self.tg.answer(cb["id"])
        part = data.split(":")
        h = {"m": self.menu, "st": self.status}.get(part[0])
        if h:
            return await h(chat)
        fn = getattr(self, "cb_" + part[0], None)
        if fn:
            await fn(chat, part[1:], msg)

    # подбор ЦА
    async def cb_au(self, chat: int, a: list, msg: dict) -> None:
        if not a:
            saved = store.get_setting("communities")
            rows = [[("🤖 Автоподбор топ-10 сообществ СПб", "au:auto")]]
            if saved:
                rows.append([("♻️ Взять сохранённый список", "au:saved")])
            rows += [[("✍️ Вписать свои сообщества", "au:own")], [("« Меню", "m")]]
            return await self.tg.send(chat, "🎯 Подбор ЦА: студенты СПб, мужчины, состоят минимум в 3 из 10 сообществ.\n"
                                            "Читает только публичные списки, ничего не пишет людям.", kb(rows))
        if a[0] == "own":
            self.awaiting[chat] = "refs"
            return await self.tg.send(chat, "Пришлите ссылки или короткие имена сообществ СПб (пробел, запятая или с новой строки). "
                                            "Лучше 10 штук.", back())
        refs = None
        if a[0] == "saved":
            import json
            refs = json.loads(store.get_setting("communities") or "[]") or None
        await self.start_collect(chat, refs)

    async def start_collect(self, chat: int, refs: list[str] | None) -> None:
        if self.jobs.running("collect"):
            return await self.tg.send(chat, "Подбор уже идёт, подождите.", back())
        if not read_token():
            return await self.tg.send(chat, "Нет токена VK для чтения. Добавьте хотя бы один аккаунт: «Аккаунты VK».", back(("🔑 Аккаунты", "ac")))

        def work(job):
            import json
            api = VKApi(read_token())
            job.log("Ищу сообщества…")
            groups = audience.resolve_communities(api, refs) if refs else audience.find_top_communities(api, log=job.log)
            if not groups:
                return "Сообщества не найдены: проверьте ссылки."
            store.set_setting("communities", json.dumps([g["screen_name"] for g in groups]))
            job.log(f"Сообществ: {len(groups)}. Читаю участников (мужчины, город — СПб)…")
            found, scanned = audience.collect(api, groups, log=job.log)
            if not scanned:
                return "Ни одно сообщество не удалось прочитать."
            rows = audience.rank(found, scanned)
            new, upd = store.save_targets(rows)
            fit = sum(1 for r in rows if r["b"] > 0)
            top = store.list_targets("new", 10)
            return (f"Готово за {job.elapsed()}.\nСообществ прочитано: {scanned} из {len(groups)} "
                    f"(целиком {found.stats['read_full']}, по списку {found.stats['checked']}).\n"
                    f"Мужчин из СПб среди участников: {len(found)}; в ≥{cfg.MIN_COMMUNITIES} сообществах: {len(rows)}; "
                    f"со ВУЗом СПб и подходящим годом: {fit}.\nВ базу: новых {new}, обновлено {upd}.\n\nТоп-10:\n"
                    + "\n".join(fmt_target(i, r) for i, r in enumerate(top, 1)))
        await self.run_job(chat, "collect", "Подбор ЦА", work, back(("📋 Топ ЦА", "tp:0")), stoppable=False)

    async def cb_tp(self, chat: int, a: list, msg: dict) -> None:
        off = int(a[0]) if a and a[0].isdigit() else 0
        rows = store.list_targets("new", off + 10)[off:]
        if not rows:
            return await self.tg.send(chat, "Новых целей нет. Сначала «Подобрать ЦА».", back(("🎯 Подобрать", "au")))
        text = f"📋 Топ ЦА (новые), {off + 1}–{off + len(rows)}:\n\n" + "\n".join(fmt_target(off + i, r) for i, r in enumerate(rows, 1))
        more = [("➡️ Ещё 10", f"tp:{off + 10}")] if len(rows) == 10 else []
        await self.tg.send(chat, text, kb([more + [("📥 Excel", "xl")], [("« Меню", "m")]]))

    async def cb_xl(self, chat: int, a: list, msg: dict) -> None:
        from openpyxl import Workbook
        rows = store.list_targets(None)
        if not rows:
            return await self.tg.send(chat, "База пуста.", back())
        wb = Workbook()
        ws = wb.active
        ws.append(["id", "ссылка", "имя", "фамилия", "сообществ", "a", "b", "score", "ВУЗ", "год начала", "возраст", "статус"])
        for r in rows:
            ws.append([r["user_id"], f"https://vk.com/id{r['user_id']}", r["first_name"], r["last_name"], r["communities"],
                       r["a"], r["b"], r["score"], r["university"], r["start_year"], r.get("age"), r["status"]])
        path = os.path.join(tempfile.gettempdir(), "vk_targets.xlsx")
        wb.save(path)
        await self.tg.send_file(chat, path, f"ЦА: {len(rows)} человек")

    # рассылка
    async def cb_bc(self, chat: int, a: list, msg: dict) -> None:
        if a and a[0] == "stop":
            j = self.jobs.running("broadcast")
            if j:
                j.stop()
            return await self.tg.send(chat, "Останавливаю после текущего сообщения…", back())
        accs = store.list_accounts()
        now = datetime.now().astimezone()
        usable = [x for x in accs if _usable(x, now)]
        capacity = sum(daily_limit() - store.sent_last_24h(x["id"]) for x in usable)
        targets = store.list_targets("new")
        link = messages.ref_link()
        if a and a[0] == "go":
            return await self.start_broadcast(chat, int(a[1]), min(capacity, len(targets)))
        if a and a[0].isdigit():
            return await self.broadcast_confirm(chat, int(a[0]), targets, link)
        problems = []
        if not link:
            problems.append("• не задана реферальная ссылка (Настройки → Ссылка)")
        if not usable:
            problems.append("• нет доступных аккаунтов (нет токенов, лимит на сегодня или отдых)")
        if not targets:
            problems.append("• нет целей: сначала «Подобрать ЦА»")
        text = (f"✉️ Рассылка в личные сообщения VK\nАккаунтов доступно: {len(usable)} из {len(accs)}, лимит на сейчас ≈{capacity}\n"
                f"Целей (новые): {len(targets)}; текстов: {len(messages.variants())}; пауза {cfg.DM_DELAY_MIN}–{cfg.DM_DELAY_MAX} с")
        if problems:
            return await self.tg.send(chat, text + "\n\nНельзя запустить:\n" + "\n".join(problems), back())
        can = min(capacity, len(targets))
        opts = sorted({n for n in (5, 10, 25, can) if 0 < n <= can})
        await self.tg.send(chat, text + "\n\nСкольким отправить? Сверху идут самые подходящие.",
                           kb([[(f"{n}", f"bc:{n}") for n in opts], [("« Меню", "m")]]))

    async def broadcast_confirm(self, chat: int, n: int, targets: list, link: str) -> None:
        name, body = messages.variants()[0]
        sample = render(body, targets[0], link)
        await self.tg.send(chat, f"Отправить {n} человек(у)?\nПример текста (вариант {name}, остальные варианты случайно):\n---\n{sample}\n---\n"
                                 f"≈{n * (cfg.DM_DELAY_MIN + cfg.DM_DELAY_MAX) // 120} мин. Остановить можно в любой момент.",
                           kb([[("✅ Запустить", f"bc:go:{n}")], [("« Назад", "bc")]]))

    async def start_broadcast(self, chat: int, n: int, cap: int) -> None:
        if self.jobs.running("broadcast"):
            return await self.tg.send(chat, "Рассылка уже идёт.", back())
        link = messages.ref_link()
        if not link:
            return await self.tg.send(chat, "Сначала задайте реферальную ссылку.", back(("⚙️ Настройки", "se")))
        n = min(n, cap)
        variants = messages.variants()

        def work(job):
            st = run_broadcast(variants, n, link=link, on_event=job.log, should_stop=job.should_stop, api_factory=VKApi, sleep=SLEEP)
            out = (f"Итог: отправлено {st['sent']}, нельзя написать {st['closed']}, ошибок {st['failed']}, в очереди осталось {st['left']}.")
            return out + (f"\nОстановлено: {st['stop']}" if st["stop"] else "")
        await self.run_job(chat, "broadcast", f"Рассылка на {n}", work, back(("💬 Ответы", "rp")), stoppable=True)

    async def cb_rp(self, chat: int, a: list, msg: dict) -> None:
        if not store.list_accounts():
            return await self.tg.send(chat, "Нет аккаунтов VK.", back())
        fresh = await asyncio.to_thread(replies.poll_replies, VKApi)
        old = store.unnotified_replies()
        if not fresh and not old:
            return await self.tg.send(chat, "Новых ответов нет.", back(("🔄 Проверить", "rp")))
        lines = [f"{await asyncio.to_thread(self.who, r['user_id'])}: {r['text'][:300]}\n  https://vk.com/im?sel={r['user_id']}" for r in (old or [])[:15]]
        store.mark_replies_notified([r["id"] for r in old])
        await self.tg.send(chat, "💬 Ответы:\n\n" + "\n\n".join(lines or [f"{r['user_id']}: {r['text'][:200]}" for r in fresh]), back())

    # аккаунты VK
    async def cb_ac(self, chat: int, a: list, msg: dict) -> None:
        if a and a[0] == "add":
            self.awaiting[chat] = "token"
            return await self.tg.send(chat, "Пришлите токен(ы) VK, по одному в строке. Сообщение с токеном я удалю.\n"
                                            "Нужен user access token с правами messages, groups, offline.", back(("ℹ️ Как получить", "ac:help")))
        if a and a[0] == "help":
            app = store.get_setting("app_id")
            link = (f"\nСсылка для входа (откройте под нужным аккаунтом):\nhttps://oauth.vk.com/authorize?client_id={app}&scope=messages,groups,offline"
                    f"&response_type=token&v=5.199&redirect_uri=https://oauth.vk.com/blank.html" if app else
                    "\nСначала создайте Standalone-приложение на vk.com/apps?act=manage и пришлите его ID (Настройки → ID приложения VK).")
            return await self.tg.send(chat, "Как получить токен аккаунта:\n1) войдите в аккаунт VK в браузере (лучше в отдельном профиле);\n"
                                            "2) откройте ссылку ниже и нажмите «Разрешить»;\n3) из адресной строки скопируйте значение access_token= (до &) и пришлите сюда." + link,
                               back(("➕ Добавить токен", "ac:add")))
        if a and a[0] == "chk":
            return await self.check_accounts(chat)
        if a and a[0] == "del" and len(a) > 1 and a[1].isdigit():
            store.delete_account(int(a[1]))
        if a and a[0] == "buyer":
            return await self.show_purchased(chat)
        accs = store.list_accounts()
        lines = [f"{x['id']}. {x['name'] or '?'} (id{x['user_id']}) — {x['status']}, за 24 ч {store.sent_last_24h(x['id'])}/{daily_limit()}" for x in accs]
        pur = len(store.list_purchased(only_without_token=True))
        rows = [[("➕ Добавить токен", "ac:add"), ("🔄 Проверить все", "ac:chk")]]
        rows += [[(f"🗑 {x['id']}", f"ac:del:{x['id']}") for x in accs[i:i + 4]] for i in range(0, len(accs), 4)]
        rows.append([(f"🧾 Купленные, ждут токен: {pur}", "ac:buyer")] if pur else [("ℹ️ Как получить токен", "ac:help")])
        rows.append([("« Меню", "m")])
        await self.tg.send(chat, "🔑 Аккаунты VK\n" + ("\n".join(lines) or "(пусто)"), kb(rows))

    async def add_tokens(self, chat: int, text: str, msg: dict) -> None:
        tokens = list(dict.fromkeys(TOKEN_RE.findall(text)))
        if msg.get("message_id"):
            await self.tg.delete(chat, msg["message_id"])
        if not tokens:
            return await self.tg.send(chat, "Токен не нашёл. Он выглядит как длинная строка из букв, цифр и точек.", back())
        out = []
        for t in tokens:
            try:
                u = (await asyncio.to_thread(VKApi(t).call, "users.get"))[0]
                name = f"{u.get('first_name', '')} {u.get('last_name', '')}".strip()
                store.add_account(t, int(u["id"]), name)
                out.append(f"✓ {name} (id{u['id']})")
            except (VKError, ConnectionError) as e:
                out.append(f"✗ токен …{t[-4:]}: {getattr(e, 'message', e)}")
        await self.tg.send(chat, "Токены:\n" + "\n".join(out), back(("🔑 Аккаунты", "ac")))

    async def check_accounts(self, chat: int) -> None:
        out = []
        for a in store.list_accounts():
            try:
                r = await asyncio.to_thread(VKApi(a["token"]).call, "users.get")
                store.set_account_status(a["id"], "active")
                out.append(f"{a['id']}: ок — {r[0].get('first_name')}")
            except (VKError, ConnectionError):
                store.set_account_status(a["id"], "dead")
                out.append(f"{a['id']}: не работает")
        await self.tg.send(chat, "Проверка:\n" + ("\n".join(out) or "аккаунтов нет"), back(("🔑 Аккаунты", "ac")))

    async def show_purchased(self, chat: int) -> None:
        rows = store.list_purchased(only_without_token=True)
        lines = [f"{r['id']}. {r['login']}:{r['password']}" + (f":{r['extra']}" if r["extra"] else "") for r in rows]
        sent = await self.tg.send(chat, f"🧾 Логины и пароли (сообщение удалится через {SECRET_TTL_SEC} с):\n" + "\n".join(lines), back())
        if sent:
            asyncio.create_task(self.delete_later(chat, sent["message_id"]))

    async def delete_later(self, chat: int, message_id: int) -> None:
        await asyncio.sleep(SECRET_TTL_SEC)
        await self.tg.delete(chat, message_id)

    # DarkStore
    async def cb_ds(self, chat: int, a: list, msg: dict) -> None:
        try:
            if a and a[0] == "ok":
                return await self.buy(chat, int(a[1]))
            if a and a[0] == "buy":
                p = next((x for x in await asyncio.to_thread(shop.offers, 40) if x["id"] == int(a[1])), None)
                bal, cur = await asyncio.to_thread(shop.balance)
                if not p:
                    return await self.tg.send(chat, "Товара уже нет в наличии.", back(("🛒", "ds")))
                return await self.tg.send(chat, f"Купить 1 шт.?\n{p['name']}\nЦена {p['price']:g} {cur}, на счёте {bal:g} {cur}.\n"
                                                f"Потолок покупки {shop.MAX_SPEND:g}. Это реальные деньги.",
                                          kb([[("✅ Купить", f"ds:ok:{p['id']}")], [("« Назад", "ds")]]))
            bal, cur = await asyncio.to_thread(shop.balance)
            items = await asyncio.to_thread(shop.offers, 8)
        except shop.ShopError as e:
            return await self.tg.send(chat, f"DarkStore: {e}", back())
        lines = [f"#{p['id']} · {p['price']:g} {cur} · в наличии {p['qty']} · {p['country'] or '—'}\n   {p['name']}" for p in items]
        rows = [[(f"Купить #{p['id']} ({p['price']:g})", f"ds:buy:{p['id']}")] for p in items]
        await self.tg.send(chat, f"🛒 DarkStore. Баланс {bal:g} {cur}\nДешёвые аккаунты VK без ретрива и спамблока:\n\n" + ("\n".join(lines) or "сейчас ничего нет")
                           + "\n\nАккаунты приходят логином и паролем, для работы нужен ещё токен (Аккаунты VK → как получить).",
                           kb(rows + [[("« Меню", "m")]]))

    async def buy(self, chat: int, product_id: int) -> None:
        await self.tg.send(chat, "Покупаю…")
        try:
            r = await asyncio.to_thread(shop.buy, product_id, 1)
        except shop.ShopError as e:
            return await self.tg.send(chat, f"Не купил: {e}", back(("🛒", "ds")))
        await self.tg.send(chat, f"Заказ {r['order']}: списано {r['total']:g} {r['currency']}, сохранено аккаунтов {r['saved']}."
                                 + (f"\n{r['note']}" if r["note"] else "\nДальше: «Аккаунты VK» → «Купленные» и получить токен."),
                           back(("🔑 Аккаунты", "ac")))

    # настройки
    async def cb_se(self, chat: int, a: list, msg: dict) -> None:
        if a and a[0] == "link":
            self.awaiting[chat] = "link"
            return await self.tg.send(chat, "Пришлите вашу реферальную ссылку Яндекс Еды (начинается с https://).", back())
        if a and a[0] == "limit":
            self.awaiting[chat] = "limit"
            return await self.tg.send(chat, f"Сколько новых диалогов с одного аккаунта в сутки? Сейчас {daily_limit()}. Безопаснее 10–20.", back())
        if a and a[0] == "app":
            self.awaiting[chat] = "app"
            return await self.tg.send(chat, "Пришлите ID вашего Standalone-приложения VK (цифры).", back())
        if a and a[0] == "varadd":
            self.awaiting[chat] = "varadd"
            return await self.tg.send(chat, "Пришлите текст варианта. {name} подставит имя, {link} обязательна. Лучше без обещаний заработка.", back())
        if a and a[0] == "varreset":
            messages.reset_variants()
        if a and a[0] == "var":
            text = "\n\n".join(f"[{k}] {t}" for k, t in messages.variants())
            return await self.tg.send(chat, "Тексты рассылки:\n\n" + text, back(("➕ Добавить свой", "se:varadd"), ("♻️ Встроенные", "se:varreset")))
        link = messages.ref_link()
        await self.tg.send(chat, f"⚙️ Настройки\nСсылка: {link or 'не задана'}\nЛимит на аккаунт: {daily_limit()} в сутки\n"
                                 f"ID приложения VK: {store.get_setting('app_id') or 'не задан'}\nВариантов текста: {len(messages.variants())}",
                           kb([[("🔗 Ссылка", "se:link"), ("📉 Лимит", "se:limit")], [("📝 Тексты", "se:var"), ("🆔 ID приложения", "se:app")], [("« Меню", "m")]]))

    async def set_link(self, chat: int, url: str) -> None:
        if not re.match(r"^https?://\S+$", url.strip()):
            return await self.tg.send(chat, "Это не похоже на ссылку. Нужна целиком, с https://.", back(("⚙️", "se")))
        messages.set_ref_link(url)
        await self.tg.send(chat, "✓ Ссылка сохранена.", back(("✉️ Рассылка", "bc")))

    async def set_limit(self, chat: int, raw: str) -> None:
        if not raw.strip().isdigit() or not 1 <= int(raw) <= 100:
            return await self.tg.send(chat, "Число от 1 до 100.", back(("⚙️", "se")))
        store.set_setting("dm_limit", raw.strip())
        await self.tg.send(chat, f"✓ Лимит: {raw.strip()} новых диалогов с аккаунта в сутки.", back(("⚙️", "se")))

    # ── фоновые дела ──────────────────────────────────────────────────────
    async def run_job(self, chat: int, kind: str, title: str, fn, final_kb: list, stoppable: bool) -> None:
        job = self.jobs.start(kind, title, fn)
        stop_kb = kb([[("⏹ Остановить", "bc:stop")]]) if stoppable else None
        sent = await self.tg.send(chat, f"⏳ {title}…", stop_kb)
        asyncio.create_task(self.watch(job, chat, sent["message_id"] if sent else None, final_kb, stop_kb))

    async def watch(self, job, chat: int, mid: int | None, final_kb: list, stop_kb) -> None:
        last = ""
        while not job.done:
            await asyncio.sleep(5)
            text = f"⏳ {job.title} ({job.elapsed()})\n\n{job.tail()}"
            if mid and text != last:
                await self.tg.edit(chat, mid, text, stop_kb)
                last = text
        text = f"❌ {job.title}: {job.error}" if job.error else f"✅ {job.title}\n\n{job.result}"
        if mid:
            await self.tg.edit(chat, mid, text, final_kb)
        else:
            await self.tg.send(chat, text, final_kb)
        if job.kind == "broadcast":
            for other in self.admin_ids():
                if other != chat:                                  # второй админ тоже должен знать, чем кончилось
                    await self.tg.send(other, f"Рассылка закончилась: {job.result or job.error}")
