# -*- coding: utf-8 -*-
"""Запуск: python -m vkpanel (служба telegramtool-vkpanel). Ждёт токен бота и список админов в .env."""
import asyncio
import logging
import os
import sys
import time

from dotenv import load_dotenv

import config  # noqa: F401  — .env
from .panel import Panel, parse_admins
from .tg import Tg

log = logging.getLogger("vkpanel")


async def main() -> None:
    while True:
        load_dotenv(override=True)                  # токен можно дописать в .env, не перезапуская службу
        token, admins = os.getenv("VKPANEL_BOT_TOKEN", "").strip(), parse_admins()
        if token and admins:
            break
        log.warning("[vkpanel] жду VKPANEL_BOT_TOKEN и VKPANEL_ADMINS в .env (бот: %s, админы: %s)",
                    "есть" if token else "нет", len(admins))
        await asyncio.sleep(30)
    tg = Tg(token)
    me = await tg.call("getMe")
    log.info("[vkpanel] запущен как @%s, админов: %d", me.get("username"), len(admins))
    try:
        await Panel(tg, admins).run()
    finally:
        await tg.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
