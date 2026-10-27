# -*- coding: utf-8 -*-
"""Минимальный клиент Bot API для панели (long polling, без вебхука и без публичного адреса)."""
import logging

import httpx

log = logging.getLogger(__name__)
API = "https://api.telegram.org"


class TgError(RuntimeError):
    pass


class Tg:
    def __init__(self, token: str) -> None:
        self.token = token
        self._http = httpx.AsyncClient(timeout=70)

    async def call(self, method: str, **params):
        r = await self._http.post(f"{API}/bot{self.token}/{method}", json=params)
        data = r.json()
        if not data.get("ok"):
            raise TgError(f"{method}: {data.get('description', r.text[:150])}")
        return data["result"]

    async def updates(self, offset: int) -> list[dict]:
        return await self.call("getUpdates", offset=offset, timeout=50, allowed_updates=["message", "callback_query"])

    async def send(self, chat_id: int, text: str, kb: list | None = None) -> dict | None:
        params: dict = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}
        if kb:
            params["reply_markup"] = {"inline_keyboard": kb}
        try:
            return await self.call("sendMessage", **params)
        except TgError as e:
            log.warning("[vkpanel] не отправил %s: %s", chat_id, e)
            return None

    async def edit(self, chat_id: int, message_id: int, text: str, kb: list | None = None) -> None:
        params: dict = {"chat_id": chat_id, "message_id": message_id, "text": text[:4000], "disable_web_page_preview": True}
        if kb is not None:
            params["reply_markup"] = {"inline_keyboard": kb}
        try:
            await self.call("editMessageText", **params)
        except TgError as e:
            if "not modified" not in str(e):
                log.debug("[vkpanel] не изменил сообщение: %s", e)

    async def delete(self, chat_id: int, message_id: int) -> None:
        try:
            await self.call("deleteMessage", chat_id=chat_id, message_id=message_id)
        except TgError:
            pass

    async def answer(self, cb_id: str, text: str = "") -> None:
        try:
            await self.call("answerCallbackQuery", callback_query_id=cb_id, text=text[:190])
        except TgError:
            pass

    async def send_file(self, chat_id: int, path: str, caption: str = "") -> None:
        with open(path, "rb") as f:
            r = await self._http.post(f"{API}/bot{self.token}/sendDocument", data={"chat_id": chat_id, "caption": caption},
                                      files={"document": (path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1], f)})
        if not r.json().get("ok"):
            raise TgError(f"sendDocument: {r.text[:150]}")

    async def close(self) -> None:
        await self._http.aclose()
