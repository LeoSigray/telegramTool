# -*- coding: utf-8 -*-
"""
Ответы на рассылку: кто из получивших написал в ответ.

Раз в несколько минут по каждому живому аккаунту смотрим непрочитанные диалоги (messages.getConversations,
filter=unread). Если последнее сообщение входящее и человек из нашей рассылки, сохраняем его как ответ.
Повторно один и тот же ответ не засчитывается (UNIQUE по аккаунту и номеру сообщения). Прочитанным диалог
мы не отмечаем: ответить человеку можно прямо из VK под этим аккаунтом.
"""
from . import store
from .api import AUTH_FAILED, VKApi, VKError


def poll_replies(api_factory=VKApi) -> list[dict]:
    """Новые ответы по всем аккаунтам. Возвращает список {account_id, user_id, text, msg_id}."""
    ours = {s["user_id"] for s in _sent_users()}
    fresh: list[dict] = []
    for acc in store.list_accounts():
        if acc["status"] == "dead":
            continue
        try:
            r = api_factory(acc["token"]).call("messages.getConversations", filter="unread", count=100)
        except VKError as e:
            if e.code == AUTH_FAILED:
                store.set_account_status(acc["id"], "dead")
            continue
        except ConnectionError:
            continue
        for it in (r or {}).get("items", []):
            peer = ((it.get("conversation") or {}).get("peer") or {})
            last = it.get("last_message") or {}
            uid = int(peer.get("id") or 0)
            if peer.get("type") != "user" or last.get("out") or uid not in ours:
                continue
            text = (last.get("text") or "").strip() or "(вложение)"
            if store.add_reply(acc["id"], uid, int(last.get("id") or 0), text):
                fresh.append({"account_id": acc["id"], "user_id": uid, "text": text, "msg_id": last.get("id")})
    return fresh


def _sent_users() -> list[dict]:
    with store._c() as c:
        return [dict(r) for r in c.execute("SELECT DISTINCT user_id FROM vk_sends WHERE status='sent'")]
