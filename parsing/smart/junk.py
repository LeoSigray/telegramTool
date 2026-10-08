"""Мусорные чаты: где спамят боты, копипаст, реклама, заработки, 18+ и т.п.

Два этапа:
  1. до чтения — по названию, @username и описанию (словарь lexicon/junk_chat.txt);
  2. после чтения — по сообщениям окна: доля ботов и пересылок, ссылок, копипаста,
     рекламы и мусорной тематики. Если сработал хоть один порог из settings.py,
     чат выбрасывается целиком.

Вердикт запоминается в SQLite: следующие запуски такой чат не читают
(перепроверка через junk_recheck_days).
"""
from __future__ import annotations

from collections import Counter, defaultdict

from . import features as ft
from . import textproc as tp


def junk_by_title(src, lex: tp.Lexicon) -> str:
    """Причина отсева по названию/описанию или пустая строка."""
    text = f"{src.title} {src.username.replace('_', ' ')} {src.about}"
    hits = lex.junk.find(tp.lemmas(text))
    return f"мусорная тематика: «{hits[0]}»" if hits else ""


def content_metrics(messages: list, authors: dict, lex: tp.Lexicon) -> dict:
    """chat_id → доли признаков мусора среди сообщений окна."""
    by_src: dict = defaultdict(list)
    for m in messages:
        by_src[m.source_id].append(m)
    out: dict = {}
    for sid, msgs in by_src.items():
        n = len(msgs)
        fps = Counter()
        bots = links = junk = ads = 0
        fp_list = []
        for m in msgs:
            a = authors.get(m.sender_id)
            if m.sender_kind != "user" or m.is_fwd or (a is not None and a.is_bot):
                bots += 1
            f = ft.extract(m.text, lex)
            if f.n_links or f.n_mentions or f.n_phones:
                links += 1
            if lex.junk.find(f.lemmas):
                junk += 1
            if ft.is_ad(f):
                ads += 1
            fp_list.append(f.fp)
            if f.fp:
                fps[f.fp] += 1
        with_fp = sum(1 for fp in fp_list if fp)
        dups = sum(1 for fp in fp_list if fp and fps[fp] >= 2)
        out[sid] = {
            "n": n,
            "bots": bots / n,
            "links": links / n,
            "dups": dups / with_fp if with_fp else 0.0,
            "topic": junk / n,
            "ads": ads / n,
        }
    return out


def verdict(m: dict, params) -> str:
    """Причина «мусорный чат» по метрикам или пустая строка."""
    if m["n"] < params.junk_min_msgs:
        # мало сообщений: судим только по явной мусорной тематике
        return f"мусорная тематика в {m['topic']:.0%} сообщений" if m["topic"] >= 0.5 else ""
    rules = [
        ("bots", params.junk_bots, "боты, анонимы и пересылки"),
        ("links", params.junk_links, "ссылки и контакты"),
        ("dups", params.junk_dups, "копипаст"),
        ("topic", params.junk_topic, "заработки, 18+, ставки и т.п."),
        ("ads", params.junk_ads, "реклама исполнителей"),
    ]
    hits = [f"{label} {m[key]:.0%}" for key, thr, label in rules if m[key] >= thr]
    return "мусорный чат: " + ", ".join(hits) if hits else ""


def summary(m: dict | None) -> str:
    if not m:
        return ""
    return (f"боты {m['bots']:.0%}, ссылки {m['links']:.0%}, копипаст {m['dups']:.0%}, "
            f"мусор-тема {m['topic']:.0%}, реклама {m['ads']:.0%}")


def filter_sources(sources: dict, messages: list, authors: dict, lex, params, store, log,
                   quiet: bool = False):
    """Выкидывает мусорные чаты. Возвращает (оставшиеся источники, сообщения, метрики).

    quiet=True — промежуточный пересчёт между волнами: ничего не пишет в базу и лог."""
    metrics = content_metrics(messages, authors, lex)
    junk_ids = set()
    for sid, src in sources.items():
        m = metrics.get(sid)
        reason = verdict(m, params) if m else ""
        if reason:
            junk_ids.add(sid)
            if not quiet:
                src.status, src.reason = "rejected", reason
                store.mark_junk(sid, reason)
                store.save_decision(params.channel, src)
                log.info("мусор", f"{src.label()}: {reason}")
    if not quiet:
        if junk_ids:
            log.info("мусор", f"выброшено мусорных чатов: {len(junk_ids)} из {len(sources)}")
        log.step("Мусорных чатов (по содержимому)", len(junk_ids))
    kept = {sid: s for sid, s in sources.items() if sid not in junk_ids}
    msgs = [m for m in messages if m.source_id not in junk_ids]
    return kept, msgs, metrics
