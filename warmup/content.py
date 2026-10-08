# -*- coding: utf-8 -*-
"""
Тексты для прогрева. Нейросеть — через общий слой api/llm.py (тот же провайдер,
что и у остального тула). Каждый метод имеет ЗАПАСНОЙ путь без нейросети
(кроме диалога — реплики без неё не сделать), чтобы профиль заполнялся даже
если LLM недоступна.

Класс Content легко подменить в тестах: исполнитель принимает любой объект
с теми же методами.
"""
import random
import re

_FALLBACK_FIRST = ["Алексей", "Дмитрий", "Максим", "Артём", "Никита", "Иван", "Егор", "Кирилл",
                   "Анна", "Мария", "Елена", "Ольга", "Дарья", "Полина", "Софья", "Ксения"]
_FALLBACK_LAST = ["Смирнов", "Кузнецов", "Попов", "Васильев", "Петров", "Соколов", "Михайлов",
                  "Новиков", "Фёдоров", "Морозов", "Волков", "Зайцев", "Павлов", "Семёнов"]
_FALLBACK_BIO = ["", "живу как живу", "всё потом", "кофе и музыка", "на связи", "работаю",
                 "люблю путешествия", "просто тут", "спорт и кино", "без драмы"]

_TRANSLIT = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                     ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o",
                      "p", "r", "s", "t", "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e",
                      "yu", "ya"]))
_USERNAME_RE = re.compile(r"^[a-z][a-z0-9_]{4,31}$")


def translit(s: str) -> str:
    return "".join(_TRANSLIT.get(ch, ch) for ch in s.lower())


def clean_username(s: str) -> str | None:
    """Привести кандидата к виду, который примет Telegram (a-z0-9_, 5-32, не с цифры)."""
    s = re.sub(r"[^a-z0-9_]", "", translit(s.strip().lstrip("@")))
    s = re.sub(r"_+", "_", s).strip("_")
    return s if _USERNAME_RE.match(s) else None


def fallback_usernames(first: str, last: str, rng: random.Random) -> list[str]:
    f, l = translit(first) or "user", translit(last) or "x"
    f, l = re.sub(r"[^a-z]", "", f), re.sub(r"[^a-z]", "", l)
    cands = []
    for _ in range(10):
        n = rng.choice([rng.randint(10, 99), rng.randint(1970, 2004), rng.randint(100, 999)])
        pat = rng.choice([f"{f}_{l}{n}", f"{f}{l[:1]}{n}", f"{f}.{l}".replace(".", "_") + str(n),
                          f"{f[:1]}{l}{n}", f"{f}{n}"])
        c = clean_username(pat)
        if c and c not in cands:
            cands.append(c)
    return cands


def _clean_line(text: str, limit: int) -> str:
    text = (text or "").strip().strip('"«»“”\'').strip()
    text = text.splitlines()[0].strip() if text else ""
    return text[:limit].strip()


class LLMUnavailable(RuntimeError):
    """Нейросеть не настроена — это не сбой, а условие: задачу надо пропустить, не повторять."""


class Content:
    def __init__(self, rng: random.Random | None = None):
        self.rng = rng or random.Random()

    @staticmethod
    def llm_ready() -> bool:
        try:
            from api import llm
            return llm.is_configured()
        except Exception:  # noqa: BLE001
            return False

    async def names(self, style: str = "") -> dict:
        """{first_name, last_name}. Без нейросети — из запасных списков."""
        if self.llm_ready():
            try:
                from api.copywriter import generate_names
                res = await generate_names(style or "обычные русские имена, реалистичные",
                                           count=1, fields=["first_name", "last_name"])
                if res and res[0].get("first_name"):
                    return {"first_name": res[0]["first_name"],
                            "last_name": res[0].get("last_name", "")}
            except Exception:  # noqa: BLE001
                pass
        return {"first_name": self.rng.choice(_FALLBACK_FIRST),
                "last_name": self.rng.choice(_FALLBACK_LAST)}

    async def bio(self, first_name: str, bio_max: int = 70) -> str:
        if self.llm_ready():
            try:
                from api import llm
                raw = await llm.complete(
                    f"Придумай короткое «о себе» для Telegram-профиля человека по имени {first_name}. "
                    f"Не больше {bio_max} символов, по-русски, как пишут обычные люди: без рекламы, "
                    "без ссылок, без хештегов, без кавычек. Верни только сам текст.",
                    task="fast", max_tokens=60)
                bio = _clean_line(raw, bio_max)
                if bio:
                    return bio
            except Exception:  # noqa: BLE001
                pass
        return self.rng.choice(_FALLBACK_BIO)[:bio_max]

    async def usernames(self, first: str, last: str) -> list[str]:
        """Кандидаты в юзернейм (уже приведённые к допустимому виду)."""
        cands: list[str] = []
        if self.llm_ready():
            try:
                from api import llm
                raw = await llm.complete(
                    f"Предложи 6 вариантов Telegram-юзернейма для человека: {first} {last}. "
                    "Латиницей, строчные, цифры и _ допустимы, 5–20 символов, должны выглядеть "
                    "как реальный ник (имя+фамилия/год/цифры), без слов «user», «account». "
                    "Верни ТОЛЬКО список через запятую.", task="fast", max_tokens=80)
                for part in re.split(r"[,\n;]", raw or ""):
                    c = clean_username(part)
                    if c and c not in cands:
                        cands.append(c)
            except Exception:  # noqa: BLE001
                pass
        for c in fallback_usernames(first, last, self.rng):
            if c not in cands:
                cands.append(c)
        return cands

    async def dialog_message(self, me: str, other: str, topic: str, history: list[dict],
                             me_account: str) -> str:
        """
        Следующая реплика. history: [{sender, text}], sender — номер аккаунта.
        Нейросети нужна обязательно (шаблонами живой диалог не сделать) —
        без неё бросаем RuntimeError, исполнитель пропустит задачу с понятной причиной.
        """
        if not self.llm_ready():
            raise LLMUnavailable("нейросеть не настроена — диалоги между аккаунтами невозможны")
        from api import llm
        lines = [("Ты" if h["sender"] == me_account else other) + f": {h['text']}" for h in history]
        task = ("Начни разговор." if not lines else
                "Напиши следующую реплику от своего лица (ты — «Ты»).")
        raw = await llm.complete(
            ("История переписки:\n" + "\n".join(lines) + "\n\n" if lines else "") + task,
            task="fast", max_tokens=80,
            system=(f"Ты — {me}. Ты переписываешься в Telegram со знакомым по имени {other}, "
                    f"вы приятели. Тема разговора: {topic}. Пиши как живой человек: коротко "
                    "(обычно 2–12 слов), разговорно, иногда с опечаткой или эмодзи, но не в каждой "
                    "реплике. Не повторяй фразы из истории, не здоровайся каждый раз, не задавай "
                    "больше одного вопроса. Верни ТОЛЬКО текст одного сообщения — без кавычек, "
                    "без пояснений, без имени отправителя."))
        text = _clean_line(raw, 220)
        if not text:
            raise RuntimeError("нейросеть вернула пустую реплику")
        return text
