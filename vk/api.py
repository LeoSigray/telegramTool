# -*- coding: utf-8 -*-
"""Тонкий клиент VK API (user access token). Лимит VK — 3 запроса/с на токен.

Прокси: VK_PROXY (socks5://логин:пароль@хост:порт или http://…) — запросы к VK идут через него, чтобы аккаунты
не светились с адреса сервера. Без VK_PROXY запросы идут напрямую."""
import os
import time

import requests

from . import config as cfg

# Коды ошибок VK, которые нам важно различать
FLOOD, CAPTCHA, AUTH_FAILED, TOO_FAST = 9, 14, 5, 6
VALIDATION = 17
NO_RIGHTS = 7
CANT_WRITE = (900, 901, 902)          # чёрный список / закрытые ЛС получателя


class VKError(Exception):
    def __init__(self, code: int, message: str, extra: dict | None = None):
        super().__init__(f"VK {code}: {message}")
        self.code, self.message, self.extra = code, message, extra or {}


class VKApi:
    def __init__(self, token: str, *, session=None, sleep=time.sleep, clock=time.monotonic, proxy: str | None = None):
        self.token = token
        px = (proxy if proxy is not None else os.getenv("VK_PROXY", "")).strip()
        if session is None and px:
            px = px.replace("socks5://", "socks5h://", 1)        # DNS тоже через прокси
            session = requests.Session()
            session.proxies = {"http": px, "https": px}
        self._http = session or requests
        self._sleep, self._clock = sleep, clock
        self._last = 0.0

    def _throttle(self) -> None:
        wait = cfg.API_MIN_INTERVAL_S - (self._clock() - self._last)
        if wait > 0:
            self._sleep(wait)
        self._last = self._clock()

    def call(self, method: str, **params):
        """Вызов метода. Лимит 3/с держим сами; 'Too many requests' и временные сбои повторяем."""
        data = {k: v for k, v in params.items() if v is not None}
        data.update(access_token=self.token, v=cfg.API_VERSION)
        net_fail = 0
        for attempt in range(6):
            self._throttle()
            try:
                r = self._http.post(cfg.API_URL + method, data=data, timeout=30)
                body = r.json()
            except (requests.RequestException, ValueError) as e:
                net_fail += 1
                if net_fail >= 3:
                    raise ConnectionError(f"VK недоступен: {type(e).__name__}") from e
                self._sleep(2 * net_fail)
                continue
            if "error" not in body:
                return body.get("response")
            err = body["error"]
            code = int(err.get("error_code", 0))
            if code in (TOO_FAST, 10) and attempt < 5:      # слишком часто / внутренняя ошибка
                self._sleep(1.0 + attempt)
                continue
            raise VKError(code, err.get("error_msg", ""), err)
        raise VKError(TOO_FAST, "превышен лимит повторов")
