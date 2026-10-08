# -*- coding: utf-8 -*-
"""
Рассылка в личные сообщения VK по подобранной ЦА (сначала самые высокие по score).

Защита аккаунтов:
  • лимит новых диалогов на аккаунт за скользящие 24 часа (DM_LIMIT_PER_ACCOUNT);
  • случайная пауза между сообщениями;
  • флуд-контроль / капча / «нужна проверка» -> аккаунт отдыхает сутки, цель остаётся в очереди;
  • невалидный токен -> аккаунт помечается мёртвым.
"""
import random
import time
from datetime import datetime, timezone

from . import config as cfg
from . import store
from .api import AUTH_FAILED, CAPTCHA, CANT_WRITE, FLOOD, NO_RIGHTS, VALIDATION, VKApi, VKError

MAX_BAD_IN_ROW = 10   # столько неудач подряд без единой отправки — значит, что-то не так с токеном/правами


def render(text: str, target: dict) -> str:
    """{name} -> имя получателя (если имени нет — подстановка убирается вместе с пробелом)."""
    name = (target.get("first_name") or "").strip()
    out = text.replace("{name}", name)
    return out.replace("  ", " ").strip() if not name else out


def _usable(acc: dict, now: datetime) -> bool:
    if acc["status"] == "dead":
        return False
    if acc["rest_until"]:
        until = datetime.strptime(acc["rest_until"], store._FMT).replace(tzinfo=timezone.utc)
        if until > now:
            return False
    return store.sent_last_24h(acc["id"], now) < cfg.DM_LIMIT_PER_ACCOUNT


def run_broadcast(text: str, limit: int, *, api_factory=VKApi, sleep=time.sleep,
                  rng: random.Random | None = None, now=None, on_event=print) -> dict:
    """
    Отправляет текст первым `limit` целям со статусом new (по убыванию score).
    Возвращает {'sent', 'closed', 'failed', 'left'} и причину остановки в 'stop'.
    """
    rng = rng or random.Random()
    now = now or (lambda: datetime.now(timezone.utc))
    stats = {"sent": 0, "closed": 0, "failed": 0, "stop": None}
    targets = store.list_targets("new", limit)
    apis: dict[int, VKApi] = {}
    rr = 0  # round-robin: по очереди, а не «пока не выжмем один аккаунт»
    bad = 0

    for t in targets:
        acc = None
        while True:
            accounts = [a for a in store.list_accounts() if _usable(a, now())]
            if not accounts:
                stats["stop"] = "нет доступных аккаунтов (лимит на сегодня / отдых / нет токенов)"
                break
            acc = accounts[rr % len(accounts)]
            rr += 1
            api = apis.setdefault(acc["id"], api_factory(acc["token"]))
            label = acc["name"] or acc["id"]
            try:
                api.call("messages.send", user_id=t["user_id"], random_id=rng.randint(1, 2 ** 31 - 1),
                         message=render(text, t))
            except VKError as e:
                if e.code in (FLOOD, CAPTCHA, VALIDATION):
                    store.set_account_status(acc["id"], "resting", cfg.REST_AFTER_FLOOD_H)
                    on_event(f"  [{label}] {e.message} -> аккаунт отдыхает {cfg.REST_AFTER_FLOOD_H} ч; "
                             f"цель {t['user_id']} уйдёт другому аккаунту")
                    continue                                   # та же цель, следующий аккаунт
                if e.code in (AUTH_FAILED, NO_RIGHTS):
                    store.set_account_status(acc["id"], "dead")
                    why = "токен недействителен" if e.code == AUTH_FAILED else "у токена нет права messages"
                    on_event(f"  [{label}] {why} -> аккаунт отключён")
                    continue
                if e.code in CANT_WRITE:
                    store.mark_target(t["user_id"], "closed", label, e.message)
                    store.log_send(acc["id"], t["user_id"], "closed", e.message)
                    stats["closed"] += 1
                    bad += 1
                    on_event(f"  ✗ {t['user_id']}: написать нельзя ({e.message})")
                    sleep(rng.uniform(*cfg.SKIP_DELAY))
                    break
                store.mark_target(t["user_id"], "failed", label, f"{e.code}: {e.message}")
                store.log_send(acc["id"], t["user_id"], "failed", f"{e.code}: {e.message}")
                stats["failed"] += 1
                bad += 1
                on_event(f"  ✗ {t['user_id']}: ошибка VK {e.code} — {e.message}")
                sleep(rng.uniform(*cfg.SKIP_DELAY))
                break
            except ConnectionError as e:
                stats["stop"] = f"нет связи с VK: {e}"
                break
            else:
                store.mark_target(t["user_id"], "sent", label)
                store.log_send(acc["id"], t["user_id"], "sent")
                stats["sent"] += 1
                bad = 0
                on_event(f"  ✓ [{label}] -> id{t['user_id']} {t['first_name']} "
                         f"(score {t['score']}, сообществ {t['communities']})")
                sleep(rng.uniform(cfg.DM_DELAY_MIN, cfg.DM_DELAY_MAX))
                break
        if not stats["stop"] and bad >= MAX_BAD_IN_ROW:
            stats["stop"] = (f"{bad} неудач подряд без отправок — проверь токены и права "
                             f"(нужен scope messages) и текст сообщения")
        if stats["stop"]:
            break

    stats["left"] = len(store.list_targets("new"))
    return stats
