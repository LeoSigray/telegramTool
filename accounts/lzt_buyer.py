import os
import re
import time
import json
import asyncio
import sqlite3
from datetime import datetime, timezone

import requests

from config import CONFIG, ACCOUNTS_DIR, SESSIONS_DIR


def _strip_html(s: str) -> str:
    """LZT кладёт сообщения об ошибке в HTML (<div>, <a href=...>). Вытаскиваем
    читаемый текст: убираем теги, схлопываем пробелы, ссылки оставляем в скобках."""
    if not s:
        return ""
    s = re.sub(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', r"\2 (\1)", s, flags=re.I | re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _lzt_error(result) -> str | None:
    """Возвращает текст ошибки, если ответ LZT — это ошибка/заглушка, а не данные.

    LZT отдаёт HTTP 200 даже когда доступ к API закрыт (например, на аккаунте
    не установлен пароль безопасности) — тело при этом {status, message, system_info}
    без полезной нагрузки. Обычные ошибки приходят как {"errors": [...]}.
    """
    if not isinstance(result, dict):
        return None
    errs = result.get("errors")
    if isinstance(errs, (list, tuple)) and errs:
        return "; ".join(_strip_html(str(e)) for e in errs)
    if isinstance(errs, str) and errs.strip():
        return _strip_html(errs)
    if result.get("error"):
        return _strip_html(str(result["error"]))
    # Заглушка-гейт: только служебные ключи + message с текстом
    if result.get("message") and set(result) <= {"status", "message", "system_info"}:
        return _strip_html(str(result["message"]))
    return None

DC_IPS = {
    1: "149.154.175.53",
    2: "149.154.167.51",
    3: "149.154.175.100",
    4: "149.154.167.91",
    5: "91.108.56.130",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def extract_price(item: dict) -> float | None:
    """Цена аккаунта из ответа LZT (в рублях). Разные эндпоинты кладут её
    в разные поля — пробуем по очереди."""
    for key in ("rub_price", "price_rub", "priceWithSellerFeeLabel", "price"):
        val = item.get(key)
        if val in (None, "", 0):
            continue
        try:
            return round(float(val), 2)
        except (TypeError, ValueError):
            continue
    return None


def extract_seller(item: dict) -> str | None:
    """Идентификатор продавца из ответа LZT — для разреза эффективности по продавцам."""
    seller = item.get("seller")
    if isinstance(seller, dict):
        return (seller.get("username") or seller.get("displayed_username")
                or (str(seller["user_id"]) if seller.get("user_id") else None))
    for key in ("seller_username", "seller_id", "user_id"):
        if item.get(key):
            return str(item[key])
    return None


def _extract_2fa_password(item: dict) -> str:
    """2FA-пароль (облачный пароль Telegram) из данных LZT. После покупки лежит
    в разных местах в зависимости от типа товара — пробуем все известные."""
    cand = (item.get("loginData") or {}).get("password") \
        or item.get("telegram_password_value") \
        or item.get("telegram_2fa_password") \
        or item.get("password_2fa")
    if not cand:
        tj = item.get("telegram_json")
        if isinstance(tj, str) and tj.strip():
            try:
                cand = (json.loads(tj) or {}).get("twoFA")
            except (ValueError, TypeError):
                cand = None
        elif isinstance(tj, dict):
            cand = tj.get("twoFA")
    cand = str(cand).strip() if cand else ""
    # LZT кладёт "0"/"None" как «пароля нет»
    return "" if cand in ("0", "None", "null", "false") else cand


def register_account_purchase(item: dict) -> None:
    """Пишет метаданные покупки в аналитику (source=lzt, cost, seller, дата).
    Не должно ронять покупку, поэтому всё в try."""
    try:
        from data import analytics as an
        item_id = str(item.get("item_id", "")).strip()
        if not item_id:
            return
        an.register_purchase(item_id, source="lzt", cost=extract_price(item) or 0.0,
                             seller=extract_seller(item), acquired_at=_now_iso())
    except Exception as e:  # noqa: BLE001
        print(f"  [analytics] не удалось записать покупку {item.get('item_id')}: {e}")


class LZTMarketAPI:
    def __init__(self):
        token = CONFIG["LZT_TOKEN"]
        if not token:
            raise ValueError("LZT_TOKEN не установлен в config.py")
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        self.base_url = CONFIG["LZT_API_BASE_URL"].rstrip("/")
        self._balance_id = None
        self.last_error: str | None = None

    def _request(self, method, endpoint, data=None):
        url = f"{self.base_url}{endpoint}"
        self.last_error = None
        try:
            if method == "GET":
                r = requests.get(url, headers=self.headers, params=data, timeout=30)
            elif method == "POST":
                r = requests.post(url, headers=self.headers, data=data, timeout=30)
            else:
                raise ValueError(f"Неизвестный метод: {method}")
            # Тело парсим ДО raise_for_status — у LZT текст ошибки лежит в JSON,
            # а не в HTTP-статусе (часто это вообще 200).
            try:
                body = r.json()
            except ValueError:
                body = None

            err = _lzt_error(body)
            if err:
                self.last_error = err
                print(f"  LZT отказал: {err}")
                return None

            r.raise_for_status()
            return body
        except requests.exceptions.HTTPError as e:
            self.last_error = _lzt_error(locals().get("body")) or str(e)
            print(f"  HTTP ошибка {url}: {self.last_error}")
            return None
        except requests.exceptions.RequestException as e:
            self.last_error = str(e)
            print(f"  Ошибка запроса {url}: {e}")
            return None

    def get_item(self, item_id: int) -> dict:
        """Полные данные аккаунта. Содержит telegram_dc_id, telegram_phone и др."""
        result = self._request("GET", f"/{item_id}")
        if result:
            return result.get("item") or result
        return {}

    def _get_login_codes(self, item_id: int) -> list[dict]:
        """Сырой список кодов входа от LZT: [{"code": "12345", "date": 1788928599}, ...].
        Отсортирован LZT'ом от новых к старым, но мы не полагаемся на порядок."""
        result = self._request("GET", f"/{item_id}/telegram-login-code")
        if not result:
            return []
        codes = result.get("codes")
        if isinstance(codes, dict):          # старый формат — один код объектом
            return [codes] if codes.get("code") else []
        if isinstance(codes, list):
            return [c for c in codes if isinstance(c, dict) and c.get("code")]
        return []

    def get_telegram_login_code(self, item_id: int, after_date: float = 0) -> str | None:
        """Самый свежий код входа. Если задан after_date — только код новее него
        (чтобы не подхватить код от прошлой попытки/прежнего владельца)."""
        codes = self._get_login_codes(item_id)
        if after_date:
            codes = [c for c in codes if float(c.get("date") or 0) > after_date]
        if not codes:
            return None
        best = max(codes, key=lambda c: float(c.get("date") or 0))
        return str(best.get("code")) or None

    def get_balance_id(self):
        if self._balance_id:
            return self._balance_id
        result = self._request("GET", "/me")
        if not result:
            return None
        user = result.get("user", result)
        if float(user.get("balance", 0) or 0) > 0:
            return None
        for b in user.get("balances", []):
            if b.get("type") == "account" and float(b.get("balance", 0) or 0) > 0:
                self._balance_id = b["balance_id"]
                print(f"  Кошелёк: {b.get('title')} (баланс={b['balance']} ₽)")
                return self._balance_id
        return None

    def list_orders(self, only_category: int | None = None) -> list[dict]:
        """История покупок — все купленные тобой аккаунты (item_state='paid' и пр.).

        Каждый элемент уже содержит telegram_* поля, login, loginData и
        telegram_json — этого достаточно для save_account_txt / автовхода,
        отдельный get_item на каждый не нужен.
        """
        out: list[dict] = []
        page = 1
        while page <= 50:  # предохранитель
            r = self._request("GET", "/user/orders", {"page": page})
            if not r:
                break
            items = r.get("items") or []
            for it in items:
                if only_category is None or it.get("category_id") == only_category:
                    out.append(it)
            if not r.get("hasNextPage") or not items:
                break
            page += 1
        return out

    def search_accounts(self, min_price=0, max_price=25, page=1,
                        country: str | None = None, wanted: int = 3):
        """
        country — ISO-2 код (см. accounts/countries.py) или None = любая страна.

        LZT.market не фильтрует по стране на своей стороне — проверено
        эмпирически (country[]/telegram_country[] игнорируются сервером
        или дают 0 результатов независимо от значения). Поэтому при
        country != None листаем страницы сами и фильтруем по полю
        telegram_country в ответе, пока не наберём ~wanted*3 совпадений
        или не упрёмся в потолок страниц (антизлоупотребление — не бесконечно
        листать при редкой стране).
        """
        if not country:
            print(f"Ищу аккаунты Telegram (цена {min_price}–{max_price} руб.)...")
            params = {"pmin": min_price, "pmax": max_price, "page": page,
                      "order_by": "price_to_up", "spam": "no"}
            result = self._request("GET", "/telegram", params)
            if result and result.get("items"):
                print(f"Найдено {len(result['items'])} аккаунтов.")
                return result["items"]
            if self.last_error:
                print(f"Поиск не выполнен — LZT вернул ошибку (см. выше): {self.last_error}")
            else:
                print("Аккаунты не найдены — в этом диапазоне цен на LZT сейчас ничего нет.")
            return []

        from .countries import country_label
        label = country_label(country)
        print(f"Ищу аккаунты Telegram ({label}, цена {min_price}–{max_price} руб.)...")

        MAX_PAGES = 15  # ~ограничение по rate-limit LZT (120 запросов/окно) и по времени
        target = max(wanted * 3, 10)
        matched = []
        scanned = 0
        for p in range(1, MAX_PAGES + 1):
            params = {"pmin": min_price, "pmax": max_price, "page": p,
                      "order_by": "price_to_up", "spam": "no"}
            result = self._request("GET", "/telegram", params)
            if not result or not result.get("items"):
                break
            items = result["items"]
            scanned += len(items)
            matched.extend(it for it in items if it.get("telegram_country") == country)
            if len(matched) >= target or not result.get("hasNextPage"):
                break

        print(f"Просмотрено {scanned} объявлений на {min(p, MAX_PAGES)} стр., "
              f"из них {label}: {len(matched)}.")
        if not matched:
            print(f"Аккаунтов из {label} в этом диапазоне цен сейчас нет "
                  f"(или их слишком мало на просмотренных {MAX_PAGES} страницах).")
        return matched

    def fast_buy(self, item_id, price) -> dict:
        print(f"Покупаю item_id={item_id} за {price} руб...")
        params = {"price": int(price)}
        bid = self.get_balance_id()
        if bid:
            params["balance_id"] = bid
        result = self._request("POST", f"/{item_id}/fast-buy", params)
        if not result:
            return {}
        if result.get("status") == "ok":
            item = result["item"]
            print(f"Куплен item_id={item['item_id']} за {item.get('rub_price', price)} руб.")
            # Дозапрашиваем полные данные (telegram_dc_id, telegram_phone) — после
            # покупки они появляются не сразу, поэтому ждём телефон с ретраями.
            full = {}
            for _ in range(6):  # до ~30 сек
                time.sleep(5)
                full = self.get_item(item["item_id"]) or {}
                if str(full.get("telegram_phone", "")).strip():
                    break
                print("  Жду, пока LZT отдаст данные аккаунта...")
            if full:
                for k, v in item.items():
                    if k not in full or not full[k]:
                        full[k] = v
                return full
            return item
        print(f"Ошибка fast-buy: {result}")
        return {}

    def save_account_txt(self, item: dict):
        """Сохраняет сырые данные аккаунта в accounts/ITEM_ID.txt."""
        item_id = str(item.get("item_id", "unknown"))
        os.makedirs(ACCOUNTS_DIR, exist_ok=True)
        path = os.path.join(ACCOUNTS_DIR, f"{item_id}.txt")
        with open(path, "w", encoding="utf-8") as f:
            login = item.get("login") or ""
            f.write(f"login={login}\n")
            ld = item.get("loginData") or {}
            pwd = ld.get("password") or item.get("password") or ""
            if pwd:
                f.write(f"password={pwd}\n")
            tj = item.get("telegram_json") or ""
            if tj:
                f.write(f"telegram_json={tj}\n")
            # Сохраняем ключевые Telegram-поля явно
            for key in ("telegram_dc_id", "telegram_phone"):
                val = item.get(key)
                if val:
                    f.write(f"{key}={val}\n")
            # Метаданные покупки — для аналитики закупки/эффективности аккаунтов
            price = extract_price(item)
            if price is not None:
                f.write(f"price={price}\n")
            seller = extract_seller(item)
            if seller:
                f.write(f"seller={seller}\n")
            f.write(f"bought_at={_now_iso()}\n")
        print(f"  Данные сохранены: accounts/{item_id}.txt")


async def login_via_code(item: dict, api: "LZTMarketAPI") -> str | None:
    """
    Автоматический вход через Telethon + код с LZT API.
    Возвращает путь к .session или None.
    """
    from telethon import TelegramClient
    from telethon.errors import (
        SessionPasswordNeededError, PhoneCodeInvalidError, PhoneCodeExpiredError,
    )
    from proxy_manager import client_kwargs_for

    item_id  = str(item.get("item_id", "unknown"))
    phone    = str(item.get("telegram_phone", "")).strip()
    dc_id    = int(item.get("telegram_dc_id") or 2)
    password = _extract_2fa_password(item)

    if not phone:
        print(f"  [!] Нет номера телефона для {item_id}")
        return None

    # Нормализуем номер
    if not phone.startswith("+"):
        phone = f"+{phone}"

    session_path = os.path.join(SESSIONS_DIR, item_id)
    os.makedirs(SESSIONS_DIR, exist_ok=True)

    # Тот же прокси, что будет использоваться потом при рассылке — если вход
    # и дальнейшая работа идут с разных IP, это само по себе подозрительно
    # для Telegram (сессия "переехала").
    client = TelegramClient(
        session_path,
        CONFIG["TELEGRAM_API_ID"],
        CONFIG["TELEGRAM_API_HASH"],
        **client_kwargs_for(item_id),
    )

    try:
        await client.connect()

        if await client.is_user_authorized():
            me = await client.get_me()
            print(f"  [session] Уже авторизован: {me.first_name} (@{me.username})")
            await client.disconnect()
            return f"{session_path}.session"

        # Запоминаем время самого свежего из уже лежащих кодов — всё, что придёт
        # ПОСЛЕ него, и есть наш код (а не код от прежнего владельца / прошлой попытки).
        existing = api._get_login_codes(int(item_id))
        last_date = max((float(c.get("date") or 0) for c in existing), default=0.0)

        print(f"  Отправляю код на {phone}...")
        sent = await client.send_code_request(phone)
        phone_code_hash = getattr(sent, "phone_code_hash", None)

        # Даём LZT время получить код (обычно 5–15 сек)
        print(f"  Жду код от LZT API...")
        code = None
        for attempt in range(10):  # до ~50 секунд
            await asyncio.sleep(5)
            code = api.get_telegram_login_code(int(item_id), after_date=last_date)
            if code:
                print(f"  Получен код: {code}")
                break
            print(f"  Код ещё не пришёл, попытка {attempt+1}/10...")

        # Крайний случай: свежего кода не дождались — берём самый новый из имеющихся
        if not code:
            code = api.get_telegram_login_code(int(item_id))
            if code:
                print(f"  Использую последний доступный код: {code}")

        if not code:
            print(f"  [!] Не удалось получить код от LZT для {item_id}")
            await client.disconnect()
            return None

        # До 3 попыток: код мог протухнуть/не совпасть, пока летел через LZT —
        # перезапрашиваем и пробуем следующий, более свежий.
        signed_in = False
        for sign_attempt in range(3):
            try:
                if phone_code_hash:
                    await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
                else:
                    await client.sign_in(phone, code)
                signed_in = True
                break
            except SessionPasswordNeededError:
                if not password:
                    print(f"  [!] Требуется 2FA пароль, но его нет в данных аккаунта")
                    await client.disconnect()
                    return None
                print(f"  2FA пароль найден, ввожу...")
                await client.sign_in(password=password)
                signed_in = True
                break
            except PhoneCodeExpiredError:
                print(f"  Код протух, запрашиваю новый...")
                snap = api._get_login_codes(int(item_id))
                last_date = max((float(c.get("date") or 0) for c in snap), default=last_date)
                sent = await client.send_code_request(phone)
                phone_code_hash = getattr(sent, "phone_code_hash", None)
            except PhoneCodeInvalidError:
                print(f"  Код не подошёл, жду следующий...")

            new_code = None
            for _ in range(8):  # ~40 сек на свежий код
                await asyncio.sleep(5)
                new_code = api.get_telegram_login_code(int(item_id), after_date=last_date)
                if new_code and new_code != code:
                    break
            if not new_code:
                print(f"  [!] Свежий код от LZT не пришёл для {item_id}")
                await client.disconnect()
                return None
            code = new_code
            print(f"  Новый код: {code}")

        if not signed_in:
            print(f"  [!] Не удалось войти по коду для {item_id}")
            await client.disconnect()
            return None

        me = await client.get_me()
        print(f"  [session] Вошёл: {me.first_name} (@{me.username}, id={me.id})")

        # Выкидываем чужие сессии (панель продавца / прежний владелец часто держат
        # активный вход и убивают наш). Свежую сессию Telegram сбросить не даёт
        # (FreshResetAuthorisationForbidden) — тогда чистим по одной, кроме своей.
        await _kick_other_sessions(client)

        # Проверяем, что нас не разлогинили в процессе
        if not await client.is_user_authorized():
            print(f"  [!] Сессию {item_id} сбросили сразу после входа")
            await client.disconnect()
            return None

        await client.disconnect()
        return f"{session_path}.session"

    except Exception as e:
        print(f"  [!] Ошибка входа для {item_id}: {e}")
        try:
            await client.disconnect()
        except Exception:
            pass
        return None


async def _kick_other_sessions(client) -> None:
    """Завершает все сессии аккаунта, кроме текущей. Best-effort.

    Telegram запрещает сброс, если текущая сессия младше 24ч
    (FreshResetAuthorisationForbidden) — тогда просто выходим, панель продавца
    к этому моменту обычно уже неактивна.
    """
    try:
        from telethon.tl.functions.auth import ResetAuthorizationsRequest
        from telethon.tl.functions.account import (
            GetAuthorizationsRequest, ResetAuthorizationRequest,
        )
        try:
            await client(ResetAuthorizationsRequest())
            print("  [session] Все чужие сессии сброшены")
            return
        except Exception:  # noqa: BLE001 — не вышло скопом, пробуем поштучно
            pass
        auths = await client(GetAuthorizationsRequest())
        killed = 0
        for a in auths.authorizations:
            if getattr(a, "current", False):
                continue
            try:
                await client(ResetAuthorizationRequest(hash=a.hash))
                killed += 1
            except Exception:  # noqa: BLE001
                pass
        print(f"  [session] Завершено чужих сессий: {killed}" if killed
              else "  [session] Чужие сессии не сброшены (свежий вход — Telegram не даёт)")
    except Exception as e:  # noqa: BLE001
        print(f"  [session] Чистка чужих сессий пропущена: {e}")


def _session_is_authorized(session_file: str) -> bool:
    """Быстрая проверка .session на живой авторизованный ключ."""
    from telethon import TelegramClient
    from proxy_manager import client_kwargs_for

    account = os.path.splitext(os.path.basename(session_file))[0]

    async def _check() -> bool:
        client = TelegramClient(session_file[:-len(".session")],
                                CONFIG["TELEGRAM_API_ID"], CONFIG["TELEGRAM_API_HASH"],
                                **client_kwargs_for(account))
        try:
            await client.connect()
            return await client.is_user_authorized()
        except Exception:  # noqa: BLE001 — AuthKeyUnregistered и пр. => не живая
            return False
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    try:
        return asyncio.run(_check())
    except Exception:  # noqa: BLE001
        return False


def create_session_for_item(item: dict, api: "LZTMarketAPI", attempts: int = 3) -> str | None:
    """
    Создаёт рабочую .session для аккаунта через вход по коду от LZT.

    Сразу после покупки LZT может ещё не отдать аккаунт/телефон/код — поэтому
    несколько попыток с нарастающей паузой и переопрос данных товара.
    """
    item_id = str(item.get("item_id", "unknown"))
    session_file = os.path.join(SESSIONS_DIR, f"{item_id}.session")

    for attempt in range(1, attempts + 1):
        print(f"  Создаю сессию через вход с кодом LZT (попытка {attempt}/{attempts})...")

        # Убираем мёртвую .session от прошлой попытки (AuthKeyUnregistered и т.п.) —
        # иначе connect() будет падать на том же ключе. Живую не трогаем.
        if attempt > 1 and os.path.exists(session_file):
            if not _session_is_authorized(session_file):
                try:
                    os.remove(session_file)
                except OSError:
                    pass
            else:
                return session_file

        # Переопрашиваем товар — телефон/dc/2FA после передачи аккаунта
        # появляются не мгновенно.
        merged = dict(item)
        if item_id.isdigit():
            fresh = api.get_item(int(item_id))
            for k, v in (fresh or {}).items():
                if v not in (None, "", 0) or k not in merged:
                    merged[k] = v

        if not str(merged.get("telegram_phone", "")).strip():
            print("  Телефон ещё не отдан LZT — жду...")
        else:
            session_path = asyncio.run(login_via_code(merged, api))
            if session_path:
                return session_path

        if attempt < attempts:
            wait = 20 * attempt
            print(f"  Не вышло — жду {wait}s (LZT дозавершает передачу аккаунта) и пробую снова...")
            time.sleep(wait)

    # Последняя попытка тоже могла оставить недоавторизованный .session
    # (Telethon создаёт файл сессии уже на connect(), до входа) — если его не
    # убрать, аккаунт так и будет висеть в sessions/ и light "не авторизован,
    # пропуск" при каждом следующем запуске рассылки, вместо того чтобы явно
    # значиться "без сессии" и звать на повторный автовход.
    if os.path.exists(session_file) and not _session_is_authorized(session_file):
        try:
            os.remove(session_file)
        except OSError:
            pass

    print(f"  [!] Автовход не удался для {item_id} после {attempts} попыток")
    print(f"  [!] Позже повтори: меню → 1 → 11  (или вручную lzt.market/{item_id})")
    return None


TELEGRAM_CATEGORY_ID = 24


def pull_purchased_accounts(*, login: bool = True, only_missing: bool = True,
                            login_attempts: int = 2,
                            api: "LZTMarketAPI | None" = None) -> dict:
    """
    Подтягивает ВСЕ купленные на LZT Telegram-аккаунты:
      • сохраняет accounts/<item_id>.txt (логин-данные, телефон, dc, 2FA);
      • регистрирует покупку в аналитике (цена/продавец/дата);
      • при login=True — собирает sessions/<item_id>.session:
          1) напрямую из auth_key (loginData.raw, БЕЗ похода в Telegram) —
             быстро и без прокси; работает для большинства LZT-аккаунтов;
          2) если ключ мёртвый — фолбэк на автовход по SMS-коду от LZT.
        Каждая созданная сессия проверяется на живую авторизацию.

    only_missing=True: не трогает аккаунты, у которых уже есть живая .session.
    Возвращает {'saved', 'live', 'dead', 'skipped'} — списки item_id.
    """
    from accounts.convert_accounts import convert_account

    api = api or LZTMarketAPI()
    orders = api.list_orders(only_category=TELEGRAM_CATEGORY_ID)
    print(f"Куплено Telegram-аккаунтов на LZT: {len(orders)}")

    saved, live, dead, skipped = [], [], [], []
    for o in orders:
        item_id = str(o.get("item_id") or "").strip()
        if not item_id:
            continue
        sess = os.path.join(SESSIONS_DIR, f"{item_id}.session")

        api.save_account_txt(o)
        try:
            register_account_purchase(o)
        except Exception as e:  # noqa: BLE001 — аналитика не критична для подтяжки
            print(f"  [{item_id}] аналитика покупки не записана: {e}")
        saved.append(item_id)

        if not login:
            continue
        if only_missing and os.path.exists(sess) and _session_is_authorized(sess):
            print(f"  [{item_id}] уже авторизован — пропуск")
            skipped.append(item_id)
            continue

        # 1) быстрый путь — сессия из auth_key
        acc_data = {
            "login": (o.get("login") or "").split(":")[0],
            "telegram_json": o.get("telegram_json") or "",
            "telegram_dc_id": o.get("telegram_dc_id"),
        }
        made = convert_account(item_id, acc_data)
        if made and _session_is_authorized(made):
            print(f"  [{item_id}] ✓ сессия из ключа")
            live.append(item_id)
            continue
        if made and os.path.exists(made):
            os.remove(made)  # мёртвый ключ — не оставляем битую .session

        # 2) фолбэк — вход по коду
        path = create_session_for_item(o, api, attempts=login_attempts)
        if path and _session_is_authorized(path):
            print(f"  [{item_id}] ✓ сессия по коду")
            live.append(item_id)
        else:
            print(f"  [{item_id}] ✗ ключ мёртв и код не пришёл — аккаунт нерабочий")
            dead.append(item_id)

    return {"saved": saved, "live": live, "dead": dead, "skipped": skipped}


def buy_accounts_interactive():
    from .countries import normalize_country_query, country_label

    api = LZTMarketAPI()

    try:
        min_price = int(input("Минимальная цена (0): ") or "0")
        max_price = int(input("Максимальная цена (25): ") or "25")
        count     = int(input("Сколько купить (3): ")    or "3")
    except ValueError:
        print("Некорректный ввод.")
        return

    print("\nСтрана аккаунта:")
    print("  1. Любая страна")
    print("  2. Определённая страна")
    country_choice = input("Выбор (Enter = любая): ").strip()

    country = None
    if country_choice == "2":
        while True:
            raw = input("Введите страну (например: США, Россия, Индия): ").strip()
            if not raw:
                print("Отмена — беру любую страну.")
                break
            country = normalize_country_query(raw)
            if country:
                print(f"Понял как: {country_label(country)} ({country})")
                break
            print(f"  Не распознал страну «{raw}» — попробуй иначе "
                  f"(например, официальное название или как в паспорте на английском).")

    accounts = api.search_accounts(min_price=min_price, max_price=max_price,
                                   country=country, wanted=count)
    if not accounts:
        if api.last_error and "парол" in api.last_error.lower():
            print("\n  ┌─────────────────────────────────────────────────────────────┐")
            print("  │ LZT.market закрыл доступ к API, пока не задан пароль        │")
            print("  │ безопасности аккаунта. Это НЕ баг программы.                │")
            print("  │ Открой https://lzt.market/account/security, задай пароль,   │")
            print("  │ затем повтори покупку.                                      │")
            print("  └─────────────────────────────────────────────────────────────┘")
        return

    bought = 0
    no_session: list[str] = []
    for acc in accounts:
        if bought >= count:
            break

        item_id = acc["item_id"]
        price   = acc["price"]
        spam    = acc.get("telegram_spam_block", "unknown")
        print(f"\n  ID: {item_id} | Цена: {price} руб. | Спам-блок: {spam}")

        item = api.fast_buy(item_id, price)
        if not item:
            continue

        # search-результат (acc) часто несёт seller/price, которых нет в ответе
        # fast-buy — подмешиваем их, не перетирая более авторитетные поля item
        enriched = {**acc, **item}
        api.save_account_txt(enriched)
        register_account_purchase(enriched)
        session = create_session_for_item(enriched, api)
        bought += 1
        if not session:
            no_session.append(str(item_id))

        if bought < count:
            time.sleep(3)

    print(f"\nКуплено аккаунтов: {bought}/{count}")

    if no_session:
        print(f"\n  Без рабочей сессии: {', '.join(no_session)}")
        if (input("  Повторить автовход для них сейчас? (y/n): ").strip().lower() == "y"):
            still: list[str] = []
            for iid in no_session:
                print(f"\n  --- {iid} ---")
                it = api.get_item(int(iid)) if iid.isdigit() else {}
                if it and create_session_for_item(it, api, attempts=4):
                    print(f"  [{iid}] ✓ сессия готова")
                else:
                    still.append(iid)
            no_session = still
        if no_session:
            print(f"\n  Осталось без сессии: {', '.join(no_session)}")
            print("  Повтори позже: меню → 1 → 11 (Скачать сессии с LZT)")

    print("\nПроверь аккаунты: меню → 1 → 4")