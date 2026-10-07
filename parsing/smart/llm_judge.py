"""Проверка сообщений бесплатным облачным LLM (через api/llm.py).

Экономия бесплатного лимита:
  • проверяем только верхушку списка, отобранную правилами;
  • 20 сообщений в одном запросе, ответ в JSON;
  • результат кешируется по тексту и профилю клиента, повторно не платим;
  • --llm-budget задаёт потолок вызовов на запуск;
  • при ошибках/лимите LLM отключается, парсинг продолжается на правилах.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re

from .models import Verdict

ROLE_RU = {"buyer": "покупатель", "seller": "продавец", "other": "другое"}
INTENT_RU = {"vendor": "ищет исполнителя", "problem": "описывает проблему",
             "advice": "просит совета", "none": "—"}


def llm_available() -> tuple[bool, str]:
    try:
        from api import llm  # noqa: WPS433 — ленивый импорт: без него модуль работает
    except Exception as e:  # noqa: BLE001
        return False, f"api.llm не загрузился: {e}"
    try:
        if not llm.is_configured():
            return False, llm.config_hint()
        return True, f"{llm.provider('fast')}:{llm.model_for('fast')}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


async def llm_complete(prompt: str, max_tokens: int) -> str:
    from api.llm import complete
    return await complete(prompt, task="fast", max_tokens=max_tokens)


def parse_json_loose(text: str):
    """JSON из ответа модели: снимаем ```-обёртку и берём от первой скобки до последней."""
    s = (text or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s)
    except ValueError:
        pass
    for open_ch, close_ch in (("[", "]"), ("{", "}")):
        i, j = s.find(open_ch), s.rfind(close_ch)
        if i != -1 and j > i:
            try:
                return json.loads(s[i:j + 1])
            except ValueError:
                continue
    raise ValueError(f"не JSON: {s[:200]!r}")


def _f01(x) -> float:
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return 0.0


def to_verdict(d: dict) -> Verdict:
    role = str(d.get("role", "other")).lower().strip()
    intent = str(d.get("intent", "none")).lower().strip()
    req = d.get("is_request", False)
    if isinstance(req, str):
        req = req.strip().lower() in ("true", "1", "да", "yes")
    return Verdict(
        role=role if role in ROLE_RU else "other",
        is_request=bool(req),
        topic=_f01(d.get("topic")),
        specific=_f01(d.get("specific")),
        intent=intent if intent in INTENT_RU else "none",
        dm=_f01(d.get("dm")),
        why=str(d.get("why", "") or "")[:160],
    )


def _verdict_dict(v: Verdict) -> dict:
    return {"role": v.role, "is_request": v.is_request, "topic": v.topic, "specific": v.specific,
            "intent": v.intent, "dm": v.dm, "why": v.why}


class LLMJudge:
    def __init__(self, store, profile, params, log) -> None:
        self.store = store
        self.profile = profile
        self.params = params
        self.log = log
        self.calls = 0
        self.failures = 0
        self.disabled = False
        self.cache_hits = 0
        self.judged = 0
        self._phash = profile.hash()

    def _key(self, text: str) -> str:
        norm = re.sub(r"\s+", " ", (text or "").strip().lower())[:600]
        return hashlib.sha1(f"{self._phash}|{norm}".encode("utf-8")).hexdigest()[:24]

    def _prompt(self, batch: list[tuple[int, str, str]]) -> str:
        p = self.profile
        anti = "; ".join(p.anti[:8]) or "—"
        lines = [
            "Ты помогаешь найти потенциальных клиентов в сообщениях из Telegram-чатов.",
            "",
            "Кому ищем клиентов:",
            f"- Что продаёт компания: {p.offer or ', '.join(p.topic_terms[:10])}",
            f"- Кто покупатель: {p.audience or '—'}",
            f"- Не подходят: {anti}",
            "",
            "Для каждого сообщения определи:",
            '- role: "buyer" — автор сам ищет, хочет купить, заказать или получить помощь;'
            ' "seller" — автор предлагает свои услуги или товар, рекламирует; "other" — всё остальное.',
            "- is_request: true, если автору можно предложить решение (это запрос или вопрос).",
            "- topic: от 0 до 1 — насколько запрос относится к тому, что продаёт компания.",
            "- specific: от 0 до 1 — есть ли конкретика (задача, бюджет, сроки, объём).",
            '- intent: "vendor" (ищет исполнителя/поставщика), "problem" (описывает проблему),'
            ' "advice" (просит совета), "none".',
            "- dm: от 0 до 1 — похоже ли, что автор принимает решение о покупке"
            " (владелец, руководитель, говорит от компании).",
            "- why: до 12 слов по-русски, почему так.",
            "",
            "Сообщения:",
        ]
        for idx, text, ctx in batch:
            t = re.sub(r"\s+", " ", text).strip()[:500]
            ctx_clean = re.sub(r"\s+", " ", ctx or "").strip()[:160]
            c = f" (комментарий к посту: «{ctx_clean}»)" if ctx_clean else ""
            lines.append(f"[{idx}]{c} {t}")
        lines += [
            "",
            "Верни СТРОГО JSON-массив без markdown: по одному объекту на каждое сообщение,"
            ' с полями id, role, is_request, topic, specific, intent, dm, why.',
        ]
        return "\n".join(lines)

    async def judge(self, items: list[tuple[object, str, str]]) -> dict:
        """items: (ключ, текст, контекст поста). Возвращает {ключ: Verdict}."""
        out: dict = {}
        pending: list[tuple[object, str, str, str]] = []
        for key, text, ctx in items:
            ck = self._key(text)
            cached = self.store.llm_get(ck)
            if cached is not None:
                out[key] = to_verdict(cached)
                self.cache_hits += 1
            else:
                pending.append((key, text, ctx, ck))

        size = max(1, self.params.llm_batch)
        for start in range(0, len(pending), size):
            if self.disabled or self.calls >= self.params.llm_budget:
                break
            chunk = pending[start:start + size]
            batch = [(i + 1, text, ctx) for i, (_, text, ctx, _) in enumerate(chunk)]
            self.calls += 1
            try:
                raw = await llm_complete(self._prompt(batch), max_tokens=200 + 120 * len(batch))
                data = parse_json_loose(raw)
                if isinstance(data, dict):
                    data = data.get("items") or data.get("results") or [data]
                by_id = {}
                for d in data:
                    if isinstance(d, dict):
                        try:
                            by_id[int(d.get("id"))] = d
                        except (TypeError, ValueError):
                            continue
                got = 0
                for i, (key, _, _, ck) in enumerate(chunk):
                    d = by_id.get(i + 1)
                    if d is None:
                        continue
                    v = to_verdict(d)
                    out[key] = v
                    self.store.llm_put(ck, _verdict_dict(v))
                    got += 1
                self.judged += got
                self.failures = 0
                self.log.info("LLM", f"вызов {self.calls}/{self.params.llm_budget}: "
                                     f"размечено {got} из {len(chunk)}")
            except Exception as e:  # noqa: BLE001 — LLM не должен ронять парсинг
                self.failures += 1
                msg = str(e)
                self.log.warn("LLM", f"вызов {self.calls} не удался: {msg[:200]}")
                if "429" in msg or "rate" in msg.lower():
                    self.log.info("LLM", "похоже на лимит бесплатного тарифа — пауза 30 с")
                    await asyncio.sleep(30)
                if self.failures >= 2:
                    self.disabled = True
                    self.log.warn("LLM", "две ошибки подряд — LLM отключён до конца запуска, "
                                         "остальные сообщения оцениваются правилами")
            await asyncio.sleep(self.params.llm_delay)
        return out
