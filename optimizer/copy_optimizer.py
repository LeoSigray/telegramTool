"""
optimizer/copy_optimizer.py — «AI-редактор текстов рассылки».

Замыкает петлю обучения: бандит (optimizer/bandit.py) статистически находит
лидера и аутсайдеров среди вариантов текста; здесь Claude получает В КОНТЕКСТ
эту статистику + РЕАЛЬНЫЕ ответы людей + фразы отказов и:
  • объясняет, ПОЧЕМУ варианты работают по-разному (только по цифрам/ответам);
  • предлагает 2-3 НОВЫХ варианта, каждый с обоснованием от данных;
  • переписывает текущий лучший оффер в 3 точках шкалы «человечно ↔ официально».

Ничего не сохраняет.새 варианты добавляет человек кнопкой в дашборде
(POST /analytics/copy/apply) — это и есть следующий виток петли.
"""
from api.copywriter import _parse_json
from api.llm import complete
from data import analytics as an
from optimizer import bandit

# сколько последних ответов показывать модели
REPLIES_SAMPLE = 40


def _gather(niche: str) -> dict:
    stats = an.template_stats(niche=niche)
    active = [s for s in stats if s["active"]]
    wp = bandit.win_probability(niche) if len(active) >= 2 else {}
    prune = bandit.prune_suggestions(niche)
    replies = an.recent_replies(niche=niche, limit=REPLIES_SAMPLE)
    return {"stats": stats, "win_probability": wp,
            "prune_suggestions": prune, "replies": replies}


def _build_prompt(niche: str, data: dict) -> str:
    lines: list[str] = []
    lines.append(f"НИША: {niche}\n")
    lines.append("ВАРИАНТЫ ТЕКСТА И ИХ СТАТИСТИКА "
                 "(decided = получили исход; success = ответил по делу; "
                 "rejected = отказ; no_reply = молчание; blocked = не дошло):")
    for s in data["stats"]:
        wp = data["win_probability"].get(s["variant"])
        lines.append(
            f"— Вариант {s['variant']} "
            f"[{'активен' if s['active'] else 'выключен'}]"
            f" P(лучший)={'%.0f%%' % (wp * 100) if wp is not None else '—'}\n"
            f"  отправлено {s['sent']}, decided {s['decided']}, "
            f"success {s['replies']}, rejected {s['rejected']}, "
            f"no_reply {s['no_reply']}, blocked {s['blocked']}, "
            f"reply_rate {s['reply_rate'] if s['reply_rate'] is not None else '—'}\n"
            f"  ТЕКСТ: {s['text']}"
        )
    if data["prune_suggestions"]:
        ps = ", ".join(f"{p['variant']} (хуже лидера {p['leader_variant']})"
                       for p in data["prune_suggestions"])
        lines.append(f"\nБандит предлагает выключить как стабильно слабые: {ps}")

    if data["replies"]:
        lines.append("\nРЕАЛЬНЫЕ ОТВЕТЫ ЛЮДЕЙ на эти рассылки "
                     "(outcome: success/rejected/…):")
        for r in data["replies"][:REPLIES_SAMPLE]:
            txt = (r.get("reply_text") or "").replace("\n", " ")[:200]
            lines.append(f"— [{r.get('outcome')}] вар.{r.get('variant') or '—'}: {txt}")

    lines.append(
        "\n\nПРОАНАЛИЗИРУЙ это и верни СТРОГО JSON без markdown:\n"
        "{\n"
        '  "insights": ["наблюдение 1 со ссылкой на цифры/ответы", "наблюдение 2", ...],\n'
        '  "new_variants": [\n'
        '    {"text": "новый первый месседж 2-4 предложения",\n'
        '     "rationale": "почему он должен зайти — что берём у сильного варианта, '
        'что убираем из-за отказов"}\n'
        "  ],\n"
        '  "tone_dial": {\n'
        '    "human": "тот же оффер, максимально по-человечески/разговорно",\n'
        '    "neutral": "нейтрально",\n'
        '    "official": "официально-деловым тоном"\n'
        "  }\n"
        "}\n"
        "Требования к текстам: живой язык, без канцелярита, без хэштегов/ссылок, "
        "не звучать как массовая рассылка, не раскрывать ИИ. "
        "insights — только выводы из приведённых данных, без общих слов. "
        "new_variants — 2-3 штуки, реально разные по заходу."
    )
    return "\n".join(lines)


async def analyze_niche(niche: str) -> dict:
    """Разбор ниши + новые варианты. Ничего не сохраняет."""
    if not niche or not niche.strip():
        raise RuntimeError("не задана ниша")

    data = _gather(niche)
    if not data["stats"]:
        raise RuntimeError(f"для ниши «{niche}» нет вариантов текста — "
                           "сначала заведите хотя бы один шаблон")

    raw = await complete(_build_prompt(niche, data), task="smart", max_tokens=4000)
    parsed = _parse_json(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError("модель вернула не объект")

    new_variants = []
    for v in (parsed.get("new_variants") or []):
        if isinstance(v, dict) and isinstance(v.get("text"), str) and v["text"].strip():
            new_variants.append({"text": v["text"].strip(),
                                 "rationale": (v.get("rationale") or "").strip()})

    tone = parsed.get("tone_dial") or {}
    tone_dial = {k: (tone.get(k) or "").strip() for k in ("human", "neutral", "official")}

    return {
        "niche": niche,
        "insights": [s.strip() for s in (parsed.get("insights") or []) if isinstance(s, str) and s.strip()],
        "new_variants": new_variants,
        "tone_dial": tone_dial,
        "model_input": {
            "variants": len(data["stats"]),
            "replies_seen": len(data["replies"]),
            "win_probability": data["win_probability"],
            "prune_suggestions": data["prune_suggestions"],
        },
    }
