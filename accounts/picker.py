# -*- coding: utf-8 -*-
"""Интерактивный выбор аккаунтов (общий для рассылки, профиля, прогрева)."""
import os

from accounts.manager import get_session_files


def warmup_marks(names) -> dict[str, str]:
    """
    Пометки о прогреве для списка аккаунтов (для рассылки):
      идёт  -> «НА ПРОГРЕВЕ — нельзя»  (им занят прогрев, трогать нельзя)
      пауза -> «пауза — можно»         (прогрев отпустил аккаунт)
      готов -> «прогрев завершён»
    """
    labels = {"active": "НА ПРОГРЕВЕ — нельзя", "paused": "пауза — можно", "done": "прогрев завершён",
              "dead": "прогрев: мёртв"}
    try:
        from warmup import store
        states = {r["account"]: r["state"] for r in store.list_accounts()}
    except Exception:  # noqa: BLE001
        return {}
    return {n: labels[states[n]] for n in names if states.get(n) in labels}


def warmup_busy(names) -> set[str]:
    """Аккаунты, которыми сейчас занят прогрев (статус «идёт») — для рассылки их трогать нельзя."""
    try:
        from warmup import store
        active = {r["account"] for r in store.list_accounts(("active",))}
    except Exception:  # noqa: BLE001
        return set()
    return {n for n in names if n in active}


def pick_accounts(title: str, show_limits: bool = False,
                  names: list[str] | None = None, show_warmup: bool = False) -> set[str] | None:
    """
    Пронумерованный список аккаунтов + выбор номеров через пробел.

    Enter (пусто) = все аккаунты. None = отмена / аккаунтов нет.
    Используется везде, где операция может идти либо по всем аккаунтам,
    либо по конкретным: тестовая рассылка, смена имени, аватарки и т.п.

    names — выбирать не из всех сессий, а из этого списка (напр. только аккаунты
    на прогреве).

    show_limits — показывать дневной лимит (важно для рассылки, не важно
    для операций с профилем, которые лимитов не касаются).
    """
    from data import analytics as an
    import optimizer.health as health

    if names is None:
        sessions = get_session_files()
        names = [os.path.splitext(os.path.basename(s))[0] for s in sessions]
    if not names:
        print("Нет аккаунтов.")
        return None

    marks = warmup_marks(names) if show_warmup else {}
    print(f"\n--- {title} ---")
    header = f"{'№':<4} {'аккаунт':<14} {'статус':<10}"
    import config as _cfg
    daily = _cfg.DM_DAILY_LIMIT_PER_ACCOUNT
    print(header + (f"{'лимит':<7}{'за 24ч':<8}" if show_limits else "") + ("прогрев" if show_warmup else ""))
    for i, name in enumerate(names, 1):
        meta = an.get_account(name) or {}
        status = meta.get("status") or "—"
        if health.is_resting(meta):
            status = "отдыхает"
        line = f"{i:<4} {name:<14} {status:<10}"
        cap = health.daily_cap(name) if show_limits else None
        used, wait = an.daily_window(name, daily) if show_limits else (0, 0.0)
        if show_limits:
            line += f"{cap:<7}{f'{used}/{daily}':<8}"
        notes = []
        if show_warmup and name in marks:
            notes.append(marks[name])
        if show_limits and wait > 0:
            notes.append(f"суточный лимит исчерпан, снова сможет через {an.fmt_wait(wait)}")
        elif show_limits and cap is not None and cap <= 0:
            notes.append("не сможет отправить")
        if notes:
            line += ("← " if not (show_warmup and name in marks) else "") + "; ".join(notes)
        print(line)

    sel = input(f"\nНомера через пробел (например: 1 3 5), Enter = все {len(names)}: ").strip()
    if not sel:
        print(f"Выбраны все ({len(names)}).")
        return set(names)

    chosen: list[str] = []
    bad: list[str] = []
    for part in sel.split():
        if not part.isdigit():
            bad.append(part)
            continue
        idx = int(part) - 1
        if 0 <= idx < len(names):
            if names[idx] not in chosen:
                chosen.append(names[idx])
        else:
            bad.append(part)

    if bad:
        print(f"Пропущено (не номера из списка): {', '.join(bad)}")
    if not chosen:
        print("Ни один аккаунт не выбран.")
        return None

    print(f"Выбрано ({len(chosen)}): {', '.join(chosen)}")
    return set(chosen)
