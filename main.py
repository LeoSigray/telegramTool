import asyncio
import os

# Загружаем .env до всех остальных импортов
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from config import (
    load_proxy, save_proxy, SESSIONS_DIR, DATA_DIR, EXCEL_FILE,
    PARSE_DELAY_MIN, PARSE_DELAY_MAX,
    load_folder_links, save_folder_links,
)
from accounts.manager import (
    list_accounts, check_account, get_session_files, create_client, migrate_all_sessions,
    update_bio,
)
from accounts.lzt_buyer import buy_accounts_interactive
from accounts.convert_accounts import convert_accounts_interactive
from accounts.tdata_importer import import_tdata_interactive
from data.users_manager import add_users_manually, parse_members_from_chat
from parsing.folder_parser import parse_all_folders
from parsing.zip_parser import parse_zip_file
from data.excel_manager import (
    get_category_stats, get_category_names,
    export_categories_to_chats, import_from_parsed_excel,
    load_users, load_chats, load_chats_by_category,
)


# ========================================================================
#  Утилиты
# ========================================================================

# Папка с фотографиями для случайных аватарок ("14. Случайные аватарки")
AVATARS_DIR = "foricons"


def _ensure_min_photo_size(img_path: str, min_side: int = 800) -> str:
    """
    Гарантирует минимум min_side×min_side и конвертирует в JPEG.
    Telegram принимает аватарки от 800×800 (меньше — 'Photo is too small').
    Всегда возвращает путь к JPEG-файлу (tmp или оригинал если уже подходит).
    """
    from PIL import Image
    import tempfile

    with Image.open(img_path) as img:
        # Конвертируем в RGB: убирает alpha-канал, поддерживает JPEG
        if img.mode != "RGB":
            img = img.convert("RGB")

        w, h = img.size
        if w < min_side or h < min_side:
            scale = min_side / min(w, h)
            new_w, new_h = int(w * scale), int(h * scale)
            img = img.resize((new_w, new_h), Image.LANCZOS)
            print(f"    [resize] {w}×{h} → {new_w}×{new_h}", flush=True)

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
        img.save(tmp.name, "JPEG", quality=95)
        return tmp.name


# ========================================================================
#  Меню
# ========================================================================

def show_menu():
    proxy = load_proxy()
    proxy_status = f"[{proxy}]" if proxy else "[не установлен]"

    print("\n" + "=" * 50)
    print("       TELEGRAM AUTOMATOR")
    print("=" * 50)
    print(f"  Прокси: {proxy_status}")
    print(f"  Аккаунтов: {len(get_session_files())}")
    print("-" * 50)
    print("  1. Управление аккаунтами")
    print("  2. Нейрокомментинг (Grok)")
    print("  3. Рассылка")
    print("  4. Парсинг чатов")
    print("  5. База людей (Users)")
    print("  6. Настройки прокси")
    print("  0. Выход")
    print("-" * 50)


# ========================================================================
#  Хелперы
# ========================================================================

def input_multiline(prompt: str) -> str:
    """
    Ввод многострочного текста — поддерживает вставку готового текста с
    пустыми строками между абзацами (раньше пустая строка обрывала ввод,
    и вставленный текст с абзацами резался на первом переносе).
    Завершение — отдельная строка "END".
    """
    print(prompt)
    print('Вставь или напиши текст. Когда закончишь — с новой строки "END" и Enter.')
    lines = []
    while True:
        line = input()
        if line.strip().upper() == "END":
            break
        lines.append(line.rstrip("\r"))
    return "\n".join(lines)


def pick_message_text() -> str | None:
    """
    Выбор текста рассылки:
      1. Статичный — текст из переменной STATIC_MESSAGE в data/static_message.py.
         Чтобы поменять текст — открой этот файл и отредактируй переменную.
      2. Динамичный — свой текст (ввод/вставка через input_multiline).

    Возвращает готовый текст или None, если ввод пуст/отменён.
    """
    from data.static_message import STATIC_MESSAGE

    print("\n--- Текст сообщения ---")
    print("  1. Статичный (текст из data/static_message.py)")
    print("  2. Динамичный (написать/вставить новый)")
    choice = input("Выбор (Enter = динамичный): ").strip()

    if choice == "1":
        if not STATIC_MESSAGE.strip():
            print("STATIC_MESSAGE пустой — впиши текст в data/static_message.py.")
        else:
            return STATIC_MESSAGE

    return input_multiline("Текст сообщения:").strip() or None


# ========================================================================
#  Аккаунты
# ========================================================================

def handle_accounts():
    while True:
        print("\n--- Управление аккаунтами ---")
        print("  1. Купить аккаунты (LZT.market)")
        print("  2. Импортировать tdata")
        print("  3. Импортировать .session файлы")
        print("  4. Список аккаунтов")
        print("  5. Проверить аккаунты")
        print("  6. Изменить описание профиля (bio)")
        print("  7. Сменить аватарки (ZIP)")
        print("  8. Сгенерировать имена/фамилии (Grok)")
        print("  9. Привязать личный канал к профилю")
        print(" 10. Конвертировать accounts → sessions")
        print(" 11. Скачать сессии с LZT (для уже купленных)")
        print(" 12. Бэкофилл аналитики закупки (цена/продавец/дата из файлов)")
        print(" 13. Подтянуть ВСЕ купленные с LZT (по истории заказов)")
        print(f" 14. Случайные аватарки из {AVATARS_DIR}/ (2 шт. на аккаунт)")
        print(" 15. Имена LINKTECH (фикс. имя/фамилия всем аккаунтам)")
        print(" 16. Очистить имя и фамилию (всем аккаунтам)")
        print(" 17. Очистить аватарки (всем аккаунтам)")
        print(" 18. Авторизация в аккаунт (повторный вход для одного)")
        print(" 19. Сбросить здоровье аккаунтов (снять отдых и штрафы)")
        print("  0. Назад")

        choice = input("\nВыбор: ").strip()

        if choice == "1":
            buy_accounts_interactive()
        elif choice == "2":
            import_tdata_interactive()
        elif choice == "3":
            _handle_import_sessions()
        elif choice == "4":
            list_accounts()
        elif choice == "5":
            sessions = get_session_files()
            if not sessions:
                print("Нет аккаунтов.")
                continue
            print("\nПроверка аккаунтов...")
            for s in sessions:
                name = os.path.splitext(os.path.basename(s))[0]
                ok, info = asyncio.run(check_account(s))
                status = "OK" if ok else "FAIL"
                print(f"  [{status}] {name}: {info}")
        elif choice == "6":
            _handle_update_bio()
        elif choice == "7":
            _handle_bulk_avatars_zip()
        elif choice == "8":
            _handle_generate_names()
        elif choice == "9":
            _handle_set_personal_channel()
        elif choice == "10":
            convert_accounts_interactive()
        elif choice == "11":
            _handle_download_sessions_lzt()
        elif choice == "12":
            _handle_backfill_account_analytics()
        elif choice == "13":
            _handle_pull_all_lzt()
        elif choice == "14":
            _handle_random_avatars_folder()
        elif choice == "15":
            _handle_set_linktech_names()
        elif choice == "16":
            _handle_clear_names()
        elif choice == "17":
            _handle_clear_avatars()
        elif choice == "18":
            _handle_authorize_one_account()
        elif choice == "19":
            _handle_reset_health()
        elif choice == "0":
            break
        else:
            print("Неверный выбор.")


def _handle_import_sessions():
    """Импортировать .session файлы — из файла, папки или ZIP."""
    import shutil
    import zipfile
    import tempfile

    print("\n--- Импортировать .session файлы ---")
    print("Можно указать:")
    print("  • путь к одному .session файлу")
    print("  • путь к папке с .session файлами")
    print("  • путь к ZIP-архиву с .session файлами")
    print("  • несколько путей через Enter (пустая строка = конец)\n")

    # Собираем пути
    raw_paths = []
    while True:
        p = input("Путь: ").strip().strip('"').strip("'")
        if not p:
            if raw_paths:
                break
            continue
        raw_paths.append(p)

    if not raw_paths:
        return

    os.makedirs(SESSIONS_DIR, exist_ok=True)
    found: list[str] = []   # все .session файлы из всех источников
    tmp_dirs: list[str] = []

    for raw in raw_paths:
        if not os.path.exists(raw):
            print(f"  ✗ Не найден: {raw}")
            continue

        # ZIP-архив
        if raw.lower().endswith(".zip") and zipfile.is_zipfile(raw):
            tmp = tempfile.mkdtemp(prefix="sess_import_")
            tmp_dirs.append(tmp)
            with zipfile.ZipFile(raw) as zf:
                for member in zf.namelist():
                    if member.lower().endswith(".session") and not os.path.basename(member).startswith("__"):
                        zf.extract(member, tmp)
                        found.append(os.path.join(tmp, member))
            continue

        # Папка
        if os.path.isdir(raw):
            for fn in os.listdir(raw):
                if fn.lower().endswith(".session"):
                    found.append(os.path.join(raw, fn))
            continue

        # Одиночный файл
        if raw.lower().endswith(".session"):
            found.append(raw)
        else:
            print(f"  ✗ Не .session файл: {os.path.basename(raw)}")

    if not found:
        print("Не найдено ни одного .session файла.")
        for t in tmp_dirs:
            shutil.rmtree(t, ignore_errors=True)
        return

    # Показываем что нашли
    print(f"\nНайдено .session файлов: {len(found)}")
    for f in found:
        print(f"  • {os.path.basename(f)}")

    print(f"\nСкопировать в {SESSIONS_DIR}/? (y/n): ", end="")
    if input().strip().lower() != "y":
        print("Отменено.")
        for t in tmp_dirs:
            shutil.rmtree(t, ignore_errors=True)
        return

    from data.db import save_session_from_file as _db_save
    try:
        from data import analytics as _an
    except Exception:  # noqa: BLE001
        _an = None
    copied = skipped = db_saved = 0
    for src in found:
        dst = os.path.join(SESSIONS_DIR, os.path.basename(src))
        if os.path.exists(dst):
            print(f"  ~ {os.path.basename(src)} — уже существует, пропущен")
            skipped += 1
        else:
            shutil.copy2(src, dst)
            print(f"  ✓ {os.path.basename(src)}")
            copied += 1
        # Сохраняем / обновляем в БД в любом случае
        if _db_save(dst):
            db_saved += 1
        # Учёт появления аккаунта для аналитики закупки
        if _an is not None:
            try:
                _an.register_purchase(os.path.splitext(os.path.basename(dst))[0],
                                      source="session", cost=0.0)
            except Exception:  # noqa: BLE001
                pass

    for t in tmp_dirs:
        shutil.rmtree(t, ignore_errors=True)

    print(f"\nГотово: {copied} скопировано, {skipped} пропущено")
    print(f"Сохранено в БД: {db_saved}")
    print(f"Итого аккаунтов в sessions/: {len(get_session_files())}")


def _handle_update_bio():
    sessions = get_session_files()
    if not sessions:
        print("Нет аккаунтов.")
        return

    print(f"\n--- Изменить описание профиля ---")
    print(f"Аккаунтов: {len(sessions)}")
    bio = input("Новое описание (пустая строка = очистить): ")

    print(f"\nУстановить bio '{bio[:60]}' на {len(sessions)} аккаунтах? (y/n): ", end="")
    if input().strip().lower() != "y":
        print("Отменено.")
        return

    results = asyncio.run(update_bio(bio))
    ok = sum(1 for v in results.values() if v == "ok")
    fail = len(results) - ok
    print(f"\nГотово: {ok} успешно, {fail} ошибок")
    for name, status in results.items():
        if status != "ok":
            print(f"  [{name}] {status}")


def _handle_bulk_avatars_zip():
    """Сменить аватарки всем аккаунтам из ZIP-архива с картинками."""
    import random
    import shutil
    import tempfile
    import zipfile
    from telethon.tl.functions.photos import UploadProfilePhotoRequest

    sessions = get_session_files()
    if not sessions:
        print("Нет аккаунтов.")
        return

    print("\n--- Сменить аватарки (ZIP) ---")
    print("ZIP должен содержать .jpg/.jpeg/.png файлы.")
    zip_path = input("Путь к ZIP-файлу: ").strip().strip('"')

    if not zip_path or not os.path.exists(zip_path):
        print(f"Файл не найден: {zip_path}")
        return
    if not zipfile.is_zipfile(zip_path):
        print("Это не ZIP-архив.")
        return

    # Распаковываем во временную папку
    tmp_dir = tempfile.mkdtemp(prefix="avatars_")
    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(tmp_dir)

        images = []
        for root, _, files in os.walk(tmp_dir):
            # Пропускаем служебные папки macOS (__MACOSX) и скрытые файлы (._*)
            if "__MACOSX" in root:
                continue
            for fn in files:
                if fn.startswith("._"):
                    continue  # resource fork macOS
                if fn.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                    images.append(os.path.join(root, fn))

        if not images:
            print("В архиве нет .jpg/.png файлов.")
            return

        print(f"\nКартинок в архиве: {len(images)}")
        print(f"Аккаунтов:         {len(sessions)}")
        if len(images) < len(sessions):
            print(f"  (картинок меньше — некоторые аккаунты получат одинаковые)")
        print("Начать? (y/n): ", end="")
        if input().strip().lower() != "y":
            return

        async def _run():
            from api.client_pool import pool
            print("\nПодключаем аккаунты...")
            await pool.start_all()
            active = pool.list_active()
            if not active:
                print("Ни один аккаунт не авторизован.")
                await pool.shutdown()
                return
            print(f"Активных: {len(active)}\n")

            random.shuffle(images)
            ok = fail = 0
            for acc_name, client in pool.clients.items():
                img_path = random.choice(images)
                try:
                    upload_path = _ensure_min_photo_size(img_path)
                    uploaded = await client.upload_file(upload_path)
                    await client(UploadProfilePhotoRequest(file=uploaded))
                    print(f"  ✓ {acc_name} ← {os.path.basename(img_path)}")
                    ok += 1
                    # удаляем временный ресайзнутый файл если он создавался
                    if upload_path != img_path and os.path.exists(upload_path):
                        os.unlink(upload_path)
                except Exception as e:
                    print(f"  ✗ {acc_name}: {e}")
                    fail += 1

            await pool.shutdown()
            print(f"\nГотово: {ok} успешно, {fail} ошибок")

        asyncio.run(_run())
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _handle_random_avatars_folder():
    """
    Ставит каждому аккаунту 2 случайные разные фотографии из AVATARS_DIR
    как аватарки профиля (доп. фото профиля, не замена). Выбор пары —
    отдельно и заново для каждого аккаунта.
    """
    import random
    from telethon.tl.functions.photos import UploadProfilePhotoRequest

    sessions = get_session_files()
    if not sessions:
        print("Нет аккаунтов.")
        return

    print(f"\n--- Случайные аватарки из {AVATARS_DIR}/ ---")

    if not os.path.isdir(AVATARS_DIR):
        print(f"Папка не найдена: {AVATARS_DIR}")
        return

    images = [
        os.path.join(AVATARS_DIR, fn)
        for fn in os.listdir(AVATARS_DIR)
        if fn.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
    ]
    if len(images) < 2:
        print(f"В папке {AVATARS_DIR}/ меньше 2 фото ({len(images)}) — нечего случайно выбирать.")
        return

    print(f"Фото в папке: {len(images)}")
    print(f"Аккаунтов:    {len(sessions)}")
    print("Каждому аккаунту — 2 случайные разные фотографии из папки.")
    print("Начать? (y/n): ", end="")
    if input().strip().lower() != "y":
        return

    async def _run():
        from api.client_pool import pool
        print("\nПодключаем аккаунты...")
        await pool.start_all()
        active = pool.list_active()
        if not active:
            print("Ни один аккаунт не авторизован.")
            await pool.shutdown()
            return
        print(f"Активных: {len(active)}\n")

        ok = fail = 0
        for acc_name, client in pool.clients.items():
            for img_path in random.sample(images, 2):
                upload_path = img_path
                try:
                    upload_path = _ensure_min_photo_size(img_path)
                    uploaded = await client.upload_file(upload_path)
                    await client(UploadProfilePhotoRequest(file=uploaded))
                    print(f"  ✓ {acc_name} ← {os.path.basename(img_path)}")
                    ok += 1
                except Exception as e:
                    print(f"  ✗ {acc_name} ({os.path.basename(img_path)}): {e}")
                    fail += 1
                finally:
                    if upload_path != img_path and os.path.exists(upload_path):
                        os.unlink(upload_path)

        await pool.shutdown()
        print(f"\nГотово: {ok} успешно, {fail} ошибок")

    asyncio.run(_run())


def _handle_generate_names():
    """Сгенерировать имена/фамилии нейросетью и применить ко всем аккаунтам."""
    from api.copywriter import is_configured, generate_names, config_hint

    sessions = get_session_files()
    if not sessions:
        print("Нет аккаунтов.")
        return

    if not is_configured():
        print(f"\n⚠  {config_hint()}")
        return

    print("\n--- Сгенерировать имена/фамилии ---")
    print(f"Аккаунтов: {len(sessions)}\n")

    print("Какие поля генерировать?")
    print("  1. Имя и фамилия")
    print("  2. Только имя")
    print("  3. Только фамилия")
    field_choice = input("Выбор (Enter = 1): ").strip() or "1"
    if field_choice == "2":
        fields = ["first_name"]
    elif field_choice == "3":
        fields = ["last_name"]
    else:
        fields = ["first_name", "last_name"]

    print("\nПромт для Grok (описание стиля имён).")
    print("Например: «русские мужские», «западные женские, 25-35 лет», «нейтральные»")
    prompt = input("Промт (Enter = без уточнений): ").strip()

    print(f"\nГенерирую {len(sessions)} вариантов... ", end="", flush=True)

    async def _run():
        from api.client_pool import pool
        from api.routes_accounts import ProfileIn, _apply_profile

        # Генерируем имена (не нужен пул — только Grok)
        try:
            generated = await generate_names(prompt, count=len(sessions), fields=fields)
        except Exception as e:
            print(f"✗\nОшибка Grok: {e}")
            return

        print(f"✓ ({len(generated)} вариантов)\n")

        # Показываем что получилось
        for i, names in enumerate(generated, 1):
            parts = [names.get("first_name", ""), names.get("last_name", "")]
            print(f"  {i:>2}. {' '.join(p for p in parts if p)}")

        print(f"\nПрименить ко всем {len(sessions)} аккаунтам? (y/n): ", end="")
        if input().strip().lower() != "y":
            print("Отменено.")
            return

        print("\nПодключаем аккаунты...")
        await pool.start_all()
        active = pool.list_active()
        if not active:
            print("Ни один аккаунт не авторизован.")
            await pool.shutdown()
            return
        print(f"Активных: {len(active)}\n")

        ok = fail = 0
        for (acc_name, client), names in zip(pool.clients.items(), generated):
            fn = names.get("first_name")
            ln = names.get("last_name")
            profile = ProfileIn(first_name=fn, last_name=ln)
            try:
                await _apply_profile(client, profile)
                display = " ".join(p for p in [fn or "", ln or ""] if p)
                print(f"  ✓ {acc_name} → {display}")
                ok += 1
            except Exception as e:
                print(f"  ✗ {acc_name}: {e}")
                fail += 1

        await pool.shutdown()
        print(f"\nГотово: {ok} успешно, {fail} ошибок")

    asyncio.run(_run())


# Фиксированные имя/фамилия для брендинга LINKTECH — без нейросети,
# одинаковые у всех аккаунтов (в отличие от "8", где имена генерируются).
LINKTECH_FIRST_NAME = "LINKTECH | WORK"
LINKTECH_LAST_NAME = "Михаил Кадяев"


def _handle_set_linktech_names():
    """Ставит фиксированные имя+фамилию LINKTECH всем аккаунтам."""
    sessions = get_session_files()
    if not sessions:
        print("Нет аккаунтов.")
        return

    print("\n--- Имена LINKTECH ---")
    print(f"Аккаунтов: {len(sessions)}")
    print(f"Имя:     {LINKTECH_FIRST_NAME}")
    print(f"Фамилия: {LINKTECH_LAST_NAME}")
    print(f"\nПрименить ко всем {len(sessions)} аккаунтам? (y/n): ", end="")
    if input().strip().lower() != "y":
        print("Отменено.")
        return

    async def _run():
        from api.client_pool import pool
        from api.routes_accounts import ProfileIn, _apply_profile

        print("\nПодключаем аккаунты...")
        await pool.start_all()
        active = pool.list_active()
        if not active:
            print("Ни один аккаунт не авторизован.")
            await pool.shutdown()
            return
        print(f"Активных: {len(active)}\n")

        profile = ProfileIn(first_name=LINKTECH_FIRST_NAME, last_name=LINKTECH_LAST_NAME)
        ok = fail = 0
        for acc_name, client in pool.clients.items():
            try:
                await _apply_profile(client, profile)
                print(f"  ✓ {acc_name}")
                ok += 1
            except Exception as e:
                print(f"  ✗ {acc_name}: {e}")
                fail += 1

        await pool.shutdown()
        print(f"\nГотово: {ok} успешно, {fail} ошибок")

    asyncio.run(_run())


# Telegram не даёт первому имени быть по-настоящему пустой строкой
# ("The first name is invalid" от UpdateProfileRequest — проверено вживую,
# даже " " (пробел) он режет). ⁣ (INVISIBLE SEPARATOR) — единственный
# рабочий способ получить визуально пустое имя: Telegram его принимает,
# а показывается оно как пусто. Фамилию можно оставлять реально пустой.
_EMPTY_FIRST_NAME = "⁣"


def _handle_clear_names():
    """Ставит визуально пустые имя+фамилию всем аккаунтам."""
    sessions = get_session_files()
    if not sessions:
        print("Нет аккаунтов.")
        return

    print("\n--- Очистить имя и фамилию ---")
    print(f"Аккаунтов: {len(sessions)}")
    print(f"\nОчистить имя и фамилию у всех {len(sessions)} аккаунтов? (y/n): ", end="")
    if input().strip().lower() != "y":
        print("Отменено.")
        return

    async def _run():
        from api.client_pool import pool
        from api.routes_accounts import ProfileIn, _apply_profile

        print("\nПодключаем аккаунты...")
        await pool.start_all()
        active = pool.list_active()
        if not active:
            print("Ни один аккаунт не авторизован.")
            await pool.shutdown()
            return
        print(f"Активных: {len(active)}\n")

        profile = ProfileIn(first_name=_EMPTY_FIRST_NAME, last_name="")
        ok = fail = 0
        for acc_name, client in pool.clients.items():
            try:
                await _apply_profile(client, profile)
                print(f"  ✓ {acc_name}")
                ok += 1
            except Exception as e:
                print(f"  ✗ {acc_name}: {e}")
                fail += 1

        await pool.shutdown()
        print(f"\nГотово: {ok} успешно, {fail} ошибок")

    asyncio.run(_run())


def _handle_clear_avatars():
    """Удаляет все фото профиля у всех аккаунтов."""
    sessions = get_session_files()
    if not sessions:
        print("Нет аккаунтов.")
        return

    print("\n--- Очистить аватарки ---")
    print(f"Аккаунтов: {len(sessions)}")
    print(f"\nУдалить ВСЕ фото профиля у всех {len(sessions)} аккаунтов? (y/n): ", end="")
    if input().strip().lower() != "y":
        print("Отменено.")
        return

    async def _run():
        from api.client_pool import pool
        from telethon import utils
        from telethon.tl.functions.photos import DeletePhotosRequest

        print("\nПодключаем аккаунты...")
        await pool.start_all()
        active = pool.list_active()
        if not active:
            print("Ни один аккаунт не авторизован.")
            await pool.shutdown()
            return
        print(f"Активных: {len(active)}\n")

        ok = fail = 0
        for acc_name, client in pool.clients.items():
            try:
                photos = await client.get_profile_photos("me")
                if not photos:
                    print(f"  ─ {acc_name}: аватарок и так нет")
                    ok += 1
                    continue
                input_photos = [utils.get_input_photo(p) for p in photos]
                await client(DeletePhotosRequest(id=input_photos))
                print(f"  ✓ {acc_name}: удалено {len(input_photos)}")
                ok += 1
            except Exception as e:
                print(f"  ✗ {acc_name}: {e}")
                fail += 1

        await pool.shutdown()
        print(f"\nГотово: {ok} успешно, {fail} ошибок")

    asyncio.run(_run())


def _handle_authorize_one_account():
    """
    Повторный автовход для ОДНОГО конкретного аккаунта (по item_id LZT) —
    когда он не авторизован, а гонять весь пункт 13 (все купленные) не нужно.
    """
    from accounts.lzt_buyer import LZTMarketAPI, create_session_for_item
    from accounts.lzt_buyer import _session_is_authorized

    item_id = input("\nitem_id аккаунта (число, как в sessions/<item_id>.session): ").strip()
    if not item_id or not item_id.isdigit():
        print("Нужен числовой item_id.")
        return

    sess = os.path.join(SESSIONS_DIR, f"{item_id}.session")
    if os.path.exists(sess) and _session_is_authorized(sess):
        print(f"[{item_id}] уже авторизован — делать нечего.")
        return

    api = LZTMarketAPI()
    item = api.get_item(int(item_id))
    if not item:
        print(f"[{item_id}] LZT не отдал данные по этому item_id "
              f"(не ваш аккаунт / не найден / нет доступа).")
        return

    print(f"[{item_id}] пробую автовход...")
    path = create_session_for_item(item, api, attempts=4)
    if path and _session_is_authorized(path):
        print(f"[{item_id}] ✓ авторизован, сессия готова: {path}")
    else:
        print(f"[{item_id}] ✗ автовход не удался — см. причину выше "
              f"(код не пришёл / протух / аккаунт мёртв).")


def _handle_reset_health():
    """
    Снимает «отдых» и восстанавливает штрафной множитель (cap_multiplier → 1.0).

    Нужно, когда аккаунты получили серию PeerFlood не по своей вине (например,
    из-за бага в рассылке — см. историю с проваленными резолвами без пауз):
    каждый флуд режет множитель вдвое, до пола 0.15, и тогда дневной лимит
    падает до нуля — аккаунт живой, но тул его не берёт. Сброс возвращает
    аккаунт в строй. На реальные ограничения Telegram это не влияет никак:
    если он всё ещё ограничивает аккаунт, тот просто снова словит PeerFlood.
    """
    from data import analytics as an
    import optimizer.health as health

    rows = [m for m in an.list_accounts()
            if not (m["name"].startswith("демо") or "demo" in m["name"])]
    if not rows:
        print("Нет аккаунтов в аналитике.")
        return

    print("\n--- Здоровье аккаунтов ---")
    problem = []
    for m in rows:
        name = m["name"]
        resting = health.is_resting(m)
        mult = float(m.get("cap_multiplier") or 1.0)
        cap = health.daily_cap(name)
        mark = ""
        if m["status"] == "dead":
            mark = "МЁРТВ (бан)"
        elif resting or mult < 1.0:
            mark = "под штрафом"
            problem.append(name)
        print(f"  {name:<12} статус={m['status']:<8} множитель={mult:<5} "
              f"лимит_сегодня={cap:<3} {mark}")

    if not problem:
        print("\nШтрафов нет — сбрасывать нечего.")
        return

    print(f"\nПод штрафом: {len(problem)} аккаунт(ов).")
    print("Сброс снимет отдых и вернёт множитель 1.0 (мёртвые не трогаем).")
    if input("Сбросить? (y/n): ").strip().lower() != "y":
        print("Отменено.")
        return

    done = 0
    for name in problem:
        an.update_account(name, status="active", rest_until=None, cap_multiplier=1.0)
        done += 1
    print(f"Готово: сброшено {done}.")

    print("\nПосле сброса:")
    for name in problem:
        print(f"  {name:<12} лимит_сегодня={health.daily_cap(name)}")


def _handle_set_personal_channel():
    """Привязать личный канал к профилям всех аккаунтов."""
    sessions = get_session_files()
    if not sessions:
        print("Нет аккаунтов.")
        return

    print("\n--- Привязать личный канал к профилю ---")
    print("Канал будет отображаться в профиле как «Личный канал».")
    print("Введите 0 чтобы отвязать канал от всех аккаунтов.\n")

    channel_input = input("@username или ссылка на канал (0 = отвязать): ").strip()
    if not channel_input:
        return

    clear_mode = channel_input == "0"

    if not clear_mode:
        # Нормализуем: убираем https://t.me/, @
        if channel_input.startswith("https://t.me/"):
            channel_input = channel_input.split("t.me/")[-1].split("/")[0]
        channel_input = channel_input.lstrip("@")

    action = "отвязать канал" if clear_mode else f"привязать @{channel_input}"
    print(f"\nДействие: {action}")
    print(f"Аккаунтов: {len(sessions)}")
    print("Применить ко всем? (y/n): ", end="")
    if input().strip().lower() != "y":
        print("Отменено.")
        return

    async def _run():
        from telethon.tl.functions.account import UpdatePersonalChannelRequest
        from telethon.tl.types import InputChannelEmpty
        from api.client_pool import pool

        print("\nПодключаем аккаунты...")
        await pool.start_all()
        if not pool.list_active():
            print("Ни один аккаунт не авторизован.")
            await pool.shutdown()
            return

        ok = fail = 0
        for acc_name, client in pool.clients.items():
            try:
                if clear_mode:
                    await client(UpdatePersonalChannelRequest(channel=InputChannelEmpty()))
                    print(f"  ✓ {acc_name} — канал отвязан")
                else:
                    # Каждый аккаунт резолвит entity самостоятельно —
                    # access_hash у каждого свой, нельзя использовать чужой.
                    # Важно: аккаунт должен быть владельцем/администратором канала.
                    ch_entity = await client.get_input_entity(channel_input)
                    await client(UpdatePersonalChannelRequest(channel=ch_entity))
                    print(f"  ✓ {acc_name} → @{channel_input}")
                ok += 1
            except Exception as e:
                err = str(e)
                if "CHAT_WRITE_FORBIDDEN" in err or "can't write" in err.lower():
                    print(f"  ✗ {acc_name}: аккаунт не является администратором @{channel_input}")
                elif "CHANNEL_INVALID" in err or "Invalid channel" in err:
                    print(f"  ✗ {acc_name}: канал не найден или аккаунт не подписан")
                else:
                    print(f"  ✗ {acc_name}: {e}")
                fail += 1

        await pool.shutdown()
        print(f"\nГотово: {ok} успешно, {fail} ошибок")

    asyncio.run(_run())


def _handle_pull_all_lzt():
    """Подтягивает ВСЕ купленные на LZT Telegram-аккаунты по истории заказов:
    сохраняет accounts/*.txt, пишет аналитику покупки и (опц.) делает автовход."""
    from accounts.lzt_buyer import pull_purchased_accounts
    from data.db import sync_all_to_db
    from config import SESSIONS_DIR

    print("\n--- Подтянуть ВСЕ купленные с LZT ---")
    print("Возьму список из истории заказов LZT (не из папки accounts/).")
    print("Сессии собираются из auth-ключа аккаунта (быстро, без прокси);")
    print("для мёртвых ключей — фолбэк на вход по SMS-коду от LZT.")
    ans = input("\nСобирать сессии сейчас? (Y/n): ").strip().lower()
    do_login = ans != "n"

    res = pull_purchased_accounts(login=do_login)

    print(f"\nСохранено данных аккаунтов: {len(res['saved'])}")
    if res.get("live"):
        print(f"  Рабочих ({len(res['live'])}): {', '.join(res['live'])}")
    if res.get("skipped"):
        print(f"  Уже были ({len(res['skipped'])}): {', '.join(res['skipped'])}")
    if res.get("dead"):
        print(f"  Нерабочих ({len(res['dead'])}): {', '.join(res['dead'])}")
        print("  → у этих аккаунтов на стороне LZT сброшен ключ. Зайди на lzt.market,")
        print("    открой аккаунт и залогинься там заново, либо запроси возврат/замену.")

    if do_login:
        n = sync_all_to_db(SESSIONS_DIR)
        print(f"Синхронизировано сессий в БД: {n}")


def _handle_download_sessions_lzt():
    """Скачивает .session с LZT для аккаунтов из accounts/ у которых ещё нет сессии."""
    from accounts.lzt_buyer import LZTMarketAPI, create_session_for_item, register_account_purchase

    print("\n--- Скачать сессии с LZT ---")

    accounts_dir = "accounts"
    if not os.path.exists(accounts_dir):
        print("Папка accounts/ не найдена.")
        return

    txt_files = sorted([f for f in os.listdir(accounts_dir) if f.endswith(".txt")])
    if not txt_files:
        print("Нет .txt файлов в accounts/.")
        return

    print(f"Найдено аккаунтов: {len(txt_files)}")
    for i, f in enumerate(txt_files, 1):
        item_id = f.replace(".txt", "")
        has_session = os.path.exists(os.path.join("sessions", f"{item_id}.session"))
        status = "✓ сессия есть" if has_session else "✗ нет сессии"
        print(f"  {i}. {item_id}  [{status}]")

    print("\n  Enter = скачать все без сессии  |  0 = отмена")
    sel = input("\nВыбор: ").strip()
    if sel == "0":
        return

    if sel == "":
        targets = [
            f.replace(".txt", "") for f in txt_files
            if not os.path.exists(os.path.join("sessions", f.replace(".txt", "") + ".session"))
        ]
        if not targets:
            print("У всех аккаунтов уже есть сессии.")
            return
    else:
        targets = []
        for part in sel.split(","):
            try:
                idx = int(part.strip()) - 1
                if 0 <= idx < len(txt_files):
                    targets.append(txt_files[idx].replace(".txt", ""))
            except ValueError:
                pass
        if not targets:
            print("Ничего не выбрано.")
            return

    print(f"\nБудет обработано: {len(targets)} аккаунт(ов). Начать? (y/n): ", end="")
    if input().strip().lower() != "y":
        print("Отменено.")
        return

    api = LZTMarketAPI()
    ok = fail = 0
    for item_id in targets:
        print(f"\n  [{item_id}] Получаю данные с LZT...")
        item = api.get_item(int(item_id))
        if not item:
            print(f"  [{item_id}] ✗ Не удалось получить данные")
            fail += 1
            continue
        session_path = create_session_for_item({**item, "item_id": item_id}, api, attempts=4)
        if session_path:
            print(f"  [{item_id}] ✓ Сессия готова")
            register_account_purchase({**item, "item_id": item_id})
            ok += 1
        else:
            print(f"  [{item_id}] ✗ Не удалось создать сессию")
            fail += 1

    print(f"\nГотово: {ok} успешно, {fail} ошибок")


def _handle_backfill_account_analytics():
    """Восстанавливает source/cost/seller/acquired_at для аккаунтов, заведённых
    до появления учёта покупки (из accounts/*.txt и mtime .session)."""
    from accounts.backfill import backfill_account_meta

    print("\n--- Бэкофилл аналитики закупки аккаунтов ---")
    force = input("Перезаписать даже уже заполненные записи? (y/N): ").strip().lower() == "y"
    res = backfill_account_meta(force=force)
    print(f"\nОбновлено: {res['changed_count']}, пропущено: {res['skipped']}")
    for row in res["changed"]:
        est = " (дата оценочная)" if row.get("estimated_date") else ""
        print(f"  {row['account']}: "
              f"source={row.get('source', '—')} cost={row.get('cost', '—')} "
              f"seller={row.get('seller', '—')} acquired_at={row.get('acquired_at', '—')}{est}")


# ========================================================================
#  Нейрокомментинг
# ========================================================================

def handle_neuro_commenting():
    sessions = get_session_files()
    if not sessions:
        print("Нет аккаунтов.")
        return

    from api.copywriter import is_configured, config_hint

    if not is_configured():
        print(f"\n⚠  {config_hint()}")
        return

    print("\n--- Нейрокомментинг ---")
    print("Нейросеть читает пост и пишет живой осмысленный комментарий.\n")

    # Стиль
    print("Стиль комментирования (промт для Grok).")
    print("Например: «Ты эксперт по инвестициям. Пиши коротко, задавай вопросы, делись мнением.»")
    style_prompt = input_multiline("Промт:").strip()
    if not style_prompt:
        print("Пустой промт. Отмена.")
        return

    # Ключевые слова
    print("\nКлючевые слова для поиска каналов (по одному на строку, пустая строка = конец):")
    keywords = []
    while True:
        kw = input("> ").strip()
        if not kw:
            if keywords:
                break
            continue
        keywords.append(kw)
        print(f"  + {kw}")

    if not keywords:
        print("Нет ключевых слов. Отмена.")
        return

    # Настройки
    try:
        min_subs = int(input("\nМинимум подписчиков у канала (Enter = 1000): ").strip() or "1000")
    except ValueError:
        min_subs = 1000

    try:
        posts_per_channel = int(input("Постов комментировать на канал (Enter = 2): ").strip() or "2")
        posts_per_channel = max(1, min(posts_per_channel, 5))
    except ValueError:
        posts_per_channel = 2

    # Режим: одноразовый или непрерывный
    cont_answer = input("\nНепрерывный режим? (каждые 30 мин ищет новые каналы, пока не стопнуть) (y/n): ").strip().lower()
    continuous = cont_answer == "y"

    print(f"\nНастройки:")
    print(f"  Ключевых слов:   {len(keywords)}: {', '.join(keywords)}")
    print(f"  Мин. подписчики: {min_subs}")
    print(f"  Постов на канал: {posts_per_channel}")
    print(f"  Аккаунтов:       {len(sessions)}")
    print(f"  Режим:           {'♾  Непрерывный (Ctrl+C для остановки)' if continuous else '1 раз'}")
    print("\nЗапустить? (y/n): ", end="")
    if input().strip().lower() != "y":
        print("Отменено.")
        return

    async def _run():
        from api.comment_runner import run_comment_job
        from api.jobs import jobs as job_manager
        from api.client_pool import pool

        print("\nПодключаем аккаунты...")
        await pool.start_all()
        active = pool.list_active()
        if not active:
            print("Ни один аккаунт не авторизован.")
            return
        print(f"Активных: {len(active)}")

        job = job_manager.create(
            kind="comment",
            message=style_prompt,
            targets=[],
            parallel=1,
        )
        job.continuous = continuous

        print(f"\n[job {job.id}] Ищем каналы по: {', '.join(keywords)}...")
        if continuous:
            print("♾  Непрерывный режим — каждые 30 мин новый раунд. Ctrl+C для остановки.\n")
        else:
            print("(Ctrl+C для остановки)\n")

        try:
            await run_comment_job(job, keywords, min_subs, posts_per_channel,
                                  continuous=continuous)
        except KeyboardInterrupt:
            job.cancel.set()
            print("\nОстановка...")
            await asyncio.sleep(1)

        print(f"\n{'='*40}")
        print(f"Статус:          {job.status}")
        print(f"Раундов:         {job.current_round}")
        print(f"Каналов найдено: {job.total}")
        print(f"Комментариев:    {job.sent}")
        print(f"Пропущено:       {job.skipped}")

        if job.log:
            print("\nПоследние события:")
            for entry in list(job.log)[-10:]:
                ev = entry.get("event", "")
                if ev == "commented":
                    print(f"  ✓ @{entry.get('channel')} — «{entry.get('comment_preview', '')[:60]}»")
                elif ev == "channels_found":
                    r = entry.get("round", "")
                    print(f"  🔍 [{r}] Найдено: {entry.get('total')}, новых: {entry.get('new')}")
                elif ev == "round_complete":
                    print(f"  ✓ Раунд {entry.get('round')} завершён, отправлено: {entry.get('sent')}")
                elif ev in ("llm_error", "comment_error", "flood_wait"):
                    print(f"  ✗ {ev}: {entry.get('error') or entry.get('channel')}")

        await pool.shutdown()

    asyncio.run(_run())


# ========================================================================
#  Рассылка
# ========================================================================

def handle_broadcasting():
    while True:
        sessions = get_session_files()
        print("\n--- Рассылка ---")
        print(f"  Аккаунтов: {len(sessions)}")
        print("  1. Рассылка в личные сообщения (ЛС)")
        print("  2. Рассылка по чатам")
        print("  3. Инвайтинг в группу")
        print("  0. Назад")

        choice = input("\nВыбор: ").strip()
        if choice == "1":
            handle_dm_sending()
        elif choice == "2":
            handle_chat_sending()
        elif choice == "3":
            handle_inviting()
        elif choice == "0":
            break
        else:
            print("Неверный выбор.")


def _run_job_console(job, runner_coro):
    """
    Запускает job через пул клиентов. Выводит итог — в т.ч. сколько целей
    НЕ обработано (застряло в очереди, если все аккаунты остановились раньше
    времени) и почему аккаунты останавливались (FloodWait/PeerFlood/бан) —
    раньше это молча терялось: печатались только sent/skipped/failed, и было
    непонятно, почему из 50 адресатов ушло сообщение только одному.
    """
    async def _run():
        from api.client_pool import pool
        print("\nПодключаем аккаунты...")
        await pool.start_all()
        active = pool.list_active()
        if not active:
            print("Ни один аккаунт не авторизован.")
            await pool.shutdown()
            return
        print(f"Активных: {len(active)}")
        print("(Ctrl+C для остановки)\n")
        try:
            await runner_coro
        except KeyboardInterrupt:
            job.cancel.set()
            print("\nОстановка...")
            await asyncio.sleep(1)

        attempted = job.sent + job.failed + job.skipped
        backlog = job.total - attempted

        print(f"\n{'='*40}")
        print(f"Статус:         {job.status}")
        print(f"Всего в списке: {job.total}")
        print(f"Отправлено:     {job.sent}")
        print(f"Пропущено:      {job.skipped}")
        print(f"Ошибок:         {job.failed}")
        if backlog > 0:
            print(f"⚠ НЕ обработано: {backlog} — не дошла очередь "
                  f"(все аккаунты остановились раньше, см. причины ниже)")

        reasons = [
            f"  [{e.get('account')}] {e.get('event')}: {e.get('reason') or e.get('error') or ''}"
            for e in job.log
            if e.get("event") in ("account_paused", "account_dead", "account_skipped", "account_failed")
        ]
        if reasons:
            print("\nПричины остановки/пропуска аккаунтов:")
            for r in reasons:
                print(r)

        await pool.shutdown()

    asyncio.run(_run())


def handle_dm_sending():
    print("\n--- Рассылка в ЛС ---")
    print(f"Юзеры из {EXCEL_FILE} (лист 'Users')")

    users = load_users()
    if not users:
        print("Список Users пуст. Добавь людей через пункт 5.")
        return

    print(f"Найдено: {len(users)} контактов")

    # Контакты без @username (только числовой user_id) отправить НЕЛЬЗЯ: Telegram
    # не даёт написать по голому id, если этот аккаунт раньше с человеком не
    # пересекался (нет access_hash). Парсил чаты один аккаунт, рассылают другие —
    # у них таких прав нет, и каждый такой резолв гарантированно падает.
    # А пачка проваленных резолвов подряд — главный триггер PeerFlood.
    # Поэтому отсеиваем их ДО рассылки, а не жжём на них аккаунты.
    sendable = [u for u in users if u.get("username")]
    dropped = len(users) - len(sendable)
    if dropped:
        print(f"  ⚠ Пропущено {dropped} контактов без @username "
              f"(по голому id Telegram написать не даёт — они бы всё равно упали)")
    if not sendable:
        print("Не осталось контактов с @username — рассылать некому.")
        return
    users = sendable
    print(f"Реально доступно к отправке: {len(users)}")

    limit_raw = input(f"Скольким отправить? (Enter = всем {len(users)}): ").strip()
    if limit_raw:
        try:
            limit = int(limit_raw)
        except ValueError:
            print("Неверное число.")
            return
        if limit <= 0:
            print("Число должно быть больше 0.")
            return
        # Случайная выборка, а не "первые N" — контакты из парсинга идут по
        # чатам подряд, и в начале списка может оказаться сплошь один чат
        # (в т.ч. ботный/накрученный). Рандом размазывает выборку по всей базе.
        import random
        limit = min(limit, len(users))
        users = random.sample(users, limit)
        print(f"К отправке: {len(users)} контактов (случайная выборка)")

    targets = [f"@{u['username']}" for u in users]

    niche = input("\nНиша (Enter — без оптимизатора, разовое сообщение вручную): ").strip()
    target_channel = None

    if niche:
        target_channel = input("Канал, на подписку в который зовём (Enter — пропустить): ").strip() or None
        message, plan = _prepare_niche_dm(niche, targets)
        if plan is None:
            return
        optimize = True
        targets = plan["targets"]["targets"]
        if not targets:
            print("После чистки списка (дубли/стоп-лист/уже писали) отправлять некому.")
            return
    else:
        message = pick_message_text()
        if not message:
            print("Пустое сообщение. Отмена.")
            return
        print(f"\nСообщение:\n---\n{message}\n---")
        print(f"Отправить {len(targets)} контактам? (y/n): ", end="")
        if input().strip().lower() != "y":
            print("Отменено.")
            return
        optimize = False
        plan = None

    from api.dm_runner import run_dm_job
    from api.jobs import jobs as job_manager

    job = job_manager.create(kind="dm", message=message, targets=targets, parallel=1,
                             niche=niche or None, optimize=optimize, plan=plan,
                             target_channel=target_channel)
    _run_job_console(job, run_dm_job(job))


def _prepare_niche_dm(niche: str, raw_targets: list[str]):
    """
    Ниша → существующие шаблоны или генерация через Grok → план (чистка списка,
    ёмкость аккаунтов на сегодня). Возвращает (message_fallback, plan) или (None, None)
    при отмене. message_fallback используется только как job.message для старых
    kind, сама рассылка идёт вариантами из БД через optimizer.bandit.
    """
    from data import analytics as an
    from optimizer import planner

    existing = an.get_templates(niche)
    if existing:
        print(f"\nДля ниши '{niche}' уже есть {len(existing)} шаблон(ов):")
        for t in existing:
            print(f"  [{t['variant']}] {t['text'][:80]}")
        if input("Сгенерировать ещё варианты? (y/n): ").strip().lower() == "y":
            existing = existing + _generate_dm_templates_console(niche)
    else:
        print(f"\nДля ниши '{niche}' шаблонов ещё нет.")
        existing = _generate_dm_templates_console(niche)

    if not existing:
        print("Нет ни одного шаблона — отправлять нечем. Отмена.")
        return None, None

    plan = planner.build_plan(raw_targets, niche=niche)
    print(f"\n--- План ---")
    print(f"  Контактов на входе: {plan['targets']['input']}")
    r = plan["targets"]["removed"]
    if any(r.values()):
        print(f"  Отсеяно: дублей={r['duplicates']}, стоп-лист={r['suppressed']}, "
              f"уже писали={r['already_contacted']}")
    print(f"  К отправке сегодня: {plan['wave']['size']} ({plan['wave']['reason']})")
    for w in plan["warnings"]:
        print(f"  ⚠ {w}")

    if input(f"\nЗапустить рассылку? (y/n): ").strip().lower() != "y":
        print("Отменено.")
        return None, None

    return existing[0]["text"], plan


def _generate_dm_templates_console(niche: str) -> list[dict]:
    """Генерирует варианты нейросетью, даёт посмотреть и подтвердить перед сохранением."""
    from data import analytics as an
    from api import copywriter

    if not copywriter.is_configured():
        print(f"{copywriter.config_hint()} — нейрогенерация недоступна.")
        text = input_multiline("Введите текст шаблона вручную:").strip()
        if not text:
            return []
        return [an.get_template(an.add_template(niche, text))]

    info = input("Доп. инфо про нишу/оффер (Enter — пропустить): ").strip()
    count_raw = input("Сколько вариантов сгенерировать (по умолчанию 2): ").strip()
    count = int(count_raw) if count_raw.isdigit() else 2

    print("Генерирую...")
    try:
        variants = asyncio.run(copywriter.generate_dm_variants(niche, info, count))
    except Exception as e:  # noqa: BLE001
        print(f"Ошибка генерации: {e}")
        return []

    print(f"\nСгенерировано {len(variants)} вариантов:")
    for i, v in enumerate(variants, 1):
        print(f"\n  [{i}] {v}")

    if input("\nСохранить все и использовать в рассылке? (y/n): ").strip().lower() != "y":
        print("Отменено — шаблоны не сохранены.")
        return []

    return [an.get_template(an.add_template(niche, v)) for v in variants]


def handle_chat_sending():
    print("\n--- Рассылка по чатам ---")
    print("  Источник чатов:")
    print("  1. Категории (парсинг)")
    print("  2. Лист 'Chats'")
    print("  0. Назад")

    choice = input("\nВыбор: ").strip()
    if choice == "0":
        return

    chats = []

    if choice == "1":
        categories = get_category_names()
        if not categories:
            print("Нет категорий. Сначала запусти парсинг (пункт 4).")
            return

        stats = get_category_stats()
        print("\nКатегории:")
        for i, cat in enumerate(categories, 1):
            print(f"  {i}. {cat} ({stats.get(cat, 0)})")
        print("\n  0 = все категории")
        sel = input("\nНомера через запятую (или 0): ").strip()
        if not sel:
            return

        selected = categories if sel == "0" else [
            categories[int(p.strip()) - 1]
            for p in sel.split(",")
            if p.strip().isdigit() and 0 <= int(p.strip()) - 1 < len(categories)
        ]
        if not selected:
            print("Ничего не выбрано.")
            return

        chats = load_chats_by_category(selected)
        if not chats:
            print("В выбранных категориях нет чатов.")
            return
        print(f"Категории: {', '.join(selected)}")

    elif choice == "2":
        chats = load_chats()
        if not chats:
            print(f"Лист 'Chats' в {EXCEL_FILE} пуст.")
            return
    else:
        return

    print(f"Чатов: {len(chats)}")
    message = pick_message_text()
    if not message:
        print("Пустое сообщение. Отмена.")
        return

    sessions = get_session_files()
    parallel = 1
    if len(sessions) > 1:
        try:
            p = input(f"Сколько аккаунтов параллельно? (1-{len(sessions)}, Enter=1): ").strip()
            parallel = max(1, min(int(p), len(sessions))) if p else 1
        except ValueError:
            parallel = 1

    print(f"\nСообщение:\n---\n{message}\n---")
    print(f"Параллельно: {parallel} аккаунт(ов)")
    print("Начать? (y/n): ", end="")
    if input().strip().lower() != "y":
        print("Отменено.")
        return

    from api.chat_runner import run_chat_job
    from api.jobs import jobs as job_manager

    targets = [f"@{c['username']}" if c.get("username") else str(c["chat_id"]) for c in chats]
    job = job_manager.create(kind="chat", message=message, targets=targets, parallel=parallel)
    _run_job_console(job, run_chat_job(job))


def handle_inviting():
    print("\n--- Инвайтинг в группу ---")
    print(f"Юзеры из {EXCEL_FILE} (лист 'Users')")

    users = load_users()
    if not users:
        print("Список Users пуст. Добавь людей через пункт 5.")
        return

    target_group = input("ID или @username группы: ").strip()
    if not target_group:
        print("Не указана группа. Отмена.")
        return
    if not target_group.startswith("@") and not target_group.lstrip("-").isdigit():
        target_group = f"@{target_group}"

    print(f"\nИнвайт {len(users)} контактов в {target_group}. Подтвердить? (y/n): ", end="")
    if input().strip().lower() != "y":
        print("Отменено.")
        return

    from api.invite_runner import run_invite_job
    from api.jobs import jobs as job_manager

    targets = [f"@{u['username']}" if u.get("username") else str(u["user_id"]) for u in users]
    job = job_manager.create(kind="invite", message=target_group, targets=targets, parallel=1)
    _run_job_console(job, run_invite_job(job, target_group))


# ========================================================================
#  Парсинг чатов
# ========================================================================

def handle_parsing():
    while True:
        stats = get_category_stats()
        total_chats = sum(stats.values())

        print("\n--- Парсинг чатов ---")
        print(f"  Файл: {EXCEL_FILE}")
        if stats:
            print(f"  Категорий: {len(stats)}, чатов: {total_chats}")
        print("  1. Парсинг папок (addlist)")
        print("  2. Парсинг ZIP-архива")
        print("  3. Всё сразу (папки + ZIP)")
        print("  4. Импорт из parsed_chats.xlsx")
        print("  5. Статистика по категориям")
        print("  0. Назад")

        choice = input("\nВыбор: ").strip()

        if choice == "1":
            handle_parse_folders()
        elif choice == "2":
            handle_parse_zip()
        elif choice == "3":
            handle_parse_folders()
            handle_parse_zip()
        elif choice == "4":
            handle_import_parsed()
        elif choice == "5":
            _show_category_stats()
        elif choice == "0":
            break
        else:
            print("Неверный выбор.")


def handle_parse_folders():
    sessions = get_session_files()
    if not sessions:
        print("Нет аккаунтов. Добавьте через 'Управление аккаунтами'.")
        return

    saved = load_folder_links()

    print("\n--- Парсинг папок ---")
    if saved:
        print(f"  Сохранено ссылок: {len(saved)} (в data/folder_links.txt)")
    print("  1. Парсить сохраненные ссылки")
    print("  2. Ввести новые ссылки")
    print("  3. Сохраненные + новые")
    print("  0. Назад")

    choice = input("\nВыбор: ").strip()
    if choice == "0":
        return

    folder_links = []

    if choice in ("1", "3"):
        if not saved:
            print("  Нет сохраненных ссылок.")
            if choice == "1":
                return
        else:
            folder_links.extend(saved)
            print(f"  Загружено: {len(saved)}")

    if choice in ("2", "3"):
        print("Ссылки (по одной, пустая строка = конец):")
        new_links = []
        while True:
            line = input("> ").strip()
            if not line:
                break
            if "t.me/addlist/" in line:
                new_links.append(line)
            else:
                print(f"  Пропущено: {line}")
        folder_links.extend(new_links)

        if new_links:
            all_saved = list(dict.fromkeys(saved + new_links))
            save_folder_links(all_saved)
            print(f"  Сохранено в folder_links.txt: {len(all_saved)} ссылок")

    folder_links = list(dict.fromkeys(folder_links))

    if not folder_links:
        print("Нет ссылок.")
        return

    print(f"\nСсылок: {len(folder_links)}")
    print(f"Аккаунт: {os.path.basename(sessions[0])}")
    print("Начать? (y/n): ", end="")
    if input().strip().lower() != "y":
        print("Отменено.")
        return

    async def _run():
        client = create_client(sessions[0])
        await client.connect()
        if not await client.is_user_authorized():
            print("Аккаунт не авторизован!")
            await client.disconnect()
            return
        print("\nПарсинг папок...")
        stats = await parse_all_folders(
            client, folder_links,
            delay_min=PARSE_DELAY_MIN, delay_max=PARSE_DELAY_MAX,
        )
        await client.disconnect()
        print(f"\nГотово! Спарсено: {stats['total_parsed']}, добавлено: {stats['total_added']}")

    asyncio.run(_run())


def handle_parse_zip():
    print("\n--- Парсинг ZIP ---")
    zip_path = input("Путь к ZIP-файлу: ").strip().strip('"')

    if not zip_path or not os.path.exists(zip_path):
        print(f"Файл не найден: {zip_path}")
        return

    try:
        print(f"Парсинг: {zip_path}")
        stats = parse_zip_file(zip_path)
        print(f"\nГотово!")
        print(f"  Файлов:     {stats['total_files']}")
        print(f"  Ссылок:     {stats['total_links']}")
        print(f"  Добавлено:  {stats['total_added']}")
        if stats.get('by_category'):
            print("\nПо категориям:")
            for cat, count in sorted(stats['by_category'].items(), key=lambda x: -x[1]):
                print(f"  {cat}: +{count}")
    except Exception as e:
        print(f"Ошибка: {e}")


def handle_import_parsed():
    parsed_path = os.path.join("data", "parsed_chats.xlsx")
    if not os.path.exists(parsed_path):
        parsed_path = input("Путь к parsed_chats.xlsx: ").strip().strip('"')
        if not parsed_path or not os.path.exists(parsed_path):
            print(f"Файл не найден: {parsed_path}")
            return

    print(f"Импорт из: {parsed_path}")
    try:
        stats = import_from_parsed_excel(parsed_path)
        if stats:
            total = sum(stats.values())
            print(f"\nИмпортировано: {total} чатов")
            for cat, count in sorted(stats.items(), key=lambda x: -x[1]):
                print(f"  {cat}: +{count}")
        else:
            print("Новых чатов не найдено (всё уже импортировано).")
    except Exception as e:
        print(f"Ошибка: {e}")


def _show_category_stats():
    stats = get_category_stats()
    if not stats:
        print("Категорий пока нет. Запусти парсинг.")
        return

    print(f"\n{'Категория':<30} {'Чатов':>8}")
    print("-" * 40)
    for cat, count in sorted(stats.items(), key=lambda x: -x[1]):
        print(f"  {cat:<28} {count:>8}")
    print("-" * 40)
    print(f"  {'ВСЕГО':<28} {sum(stats.values()):>8}")


# ========================================================================
#  База людей (Users)
# ========================================================================

def handle_users_base():
    while True:
        users = load_users()
        print("\n--- База людей (Users) ---")
        print(f"  Сейчас в базе: {len(users)} контактов")
        print("  1. Добавить вручную")
        print("  2. Спарсить из одного чата")
        print("  3. Авто-парсинг по категории")
        print("  0. Назад")

        choice = input("\nВыбор: ").strip()
        if choice == "1":
            _handle_add_users_manual()
        elif choice == "2":
            _handle_parse_single_chat()
        elif choice == "3":
            _handle_parse_by_category()
        elif choice == "0":
            break
        else:
            print("Неверный выбор.")


def _handle_add_users_manual():
    print("\n--- Добавить людей вручную ---")
    print(f"Форматы: @username, 123456789")
    print("Пустая строка — завершить ввод.\n")

    comment_default = input("Метка/комментарий (Enter = пусто): ").strip()

    entries = []
    print("Вводи по одному (пустая строка = конец):")
    while True:
        line = input("> ").strip()
        if not line:
            if entries:
                break
            continue
        parts = line.split(None, 1)
        identifier = parts[0]
        comment = parts[1] if len(parts) > 1 else comment_default
        entry = {"user_id": "", "username": "", "comment": comment}
        if identifier.lstrip("@").lstrip("-").isdigit():
            entry["user_id"] = identifier.lstrip("@")
        else:
            entry["username"] = identifier.lstrip("@")
        entries.append(entry)
        print(f"  + {identifier}  [{comment or '—'}]")

    if not entries:
        print("Ничего не введено.")
        return

    print(f"\nДобавить {len(entries)} записей? (y/n): ", end="")
    if input().strip().lower() != "y":
        print("Отменено.")
        return

    stats = add_users_manually(entries)
    print(f"Готово! Добавлено: {stats['added']}, пропущено дублей: {stats['skipped']}")


def _handle_parse_single_chat():
    sessions = get_session_files()
    if not sessions:
        print("Нет аккаунтов.")
        return

    print("\n--- Парсинг одного чата ---")
    chat_link = input("Ссылка или @username чата: ").strip()
    if not chat_link:
        return

    comment = input("Метка для записей (Enter = пусто): ").strip()

    print(f"Аккаунт: {os.path.basename(sessions[0])}")
    print("Начать? (y/n): ", end="")
    if input().strip().lower() != "y":
        return

    async def _run():
        client = create_client(sessions[0])
        await client.connect()
        if not await client.is_user_authorized():
            print("Аккаунт не авторизован!")
            await client.disconnect()
            return
        print("Парсинг...")
        last_pct = [-1]
        def progress(cur, total):
            pct = int(cur / total * 100) if total else 0
            if pct != last_pct[0] and pct % 10 == 0:
                print(f"  {pct}% ({cur}/{total})")
                last_pct[0] = pct
        result = await parse_members_from_chat(client, chat_link, comment=comment, progress_cb=progress)
        await client.disconnect()
        if result["error"]:
            print(f"Ошибка: {result['error']}")
        else:
            print(f"Готово! Спарсено: {result['parsed']}, добавлено: {result['added']}, "
                  f"дублей: {result['skipped']}, без @username (пропущены): "
                  f"{result.get('no_username', 0)}")

    asyncio.run(_run())


def _handle_parse_by_category():
    """Авто-парсинг участников из всех чатов выбранной категории."""
    sessions = get_session_files()
    if not sessions:
        print("Нет аккаунтов.")
        return

    categories = get_category_names()
    if not categories:
        print("Нет категорий. Сначала запусти парсинг чатов (пункт 4).")
        return

    stats = get_category_stats()
    print("\n--- Авто-парсинг по категории ---")
    print("Участники каждого чата из категории будут добавлены в Users.\n")
    print("Доступные категории:")
    for i, cat in enumerate(categories, 1):
        print(f"  {i}. {cat}  ({stats.get(cat, 0)} чатов)")

    sel = input("\nНомер категории (или 0 = все): ").strip()
    if not sel:
        return

    if sel == "0":
        selected_cats = categories
    else:
        try:
            idx = int(sel) - 1
            if not (0 <= idx < len(categories)):
                print("Неверный номер.")
                return
            selected_cats = [categories[idx]]
        except ValueError:
            print("Неверный ввод.")
            return

    chats = load_chats_by_category(selected_cats)
    if not chats:
        print("В выбранных категориях нет чатов.")
        return

    # Лимит чатов на случай если категория огромная
    print(f"\nЧатов в выборке: {len(chats)}")
    try:
        limit_input = input(f"Максимум чатов обработать (Enter = все): ").strip()
        max_chats = int(limit_input) if limit_input else len(chats)
    except ValueError:
        max_chats = len(chats)
    chats = chats[:max_chats]

    comment = input("Метка для записей (Enter = категория): ").strip()
    if not comment:
        comment = ", ".join(selected_cats)

    print(f"\nБудет обработано: {len(chats)} чатов")
    print(f"Аккаунт: {os.path.basename(sessions[0])}")
    print("Запустить? (y/n): ", end="")
    if input().strip().lower() != "y":
        return

    async def _run():
        client = create_client(sessions[0])
        await client.connect()
        if not await client.is_user_authorized():
            print("Аккаунт не авторизован!")
            await client.disconnect()
            return

        total_parsed = total_added = total_skipped = total_errors = 0

        for i, chat in enumerate(chats, 1):
            chat_id = chat.get("username") or str(chat.get("chat_id", ""))
            if not chat_id:
                continue

            print(f"\n[{i}/{len(chats)}] @{chat_id}  ", end="", flush=True)

            result = await parse_members_from_chat(client, chat_id, comment=comment)

            if result["error"]:
                print(f"✗ {result['error'][:60]}")
                total_errors += 1
            else:
                total_parsed += result["parsed"]
                total_added += result["added"]
                total_skipped += result["skipped"]
                print(f"✓ {result['parsed']} участников, +{result['added']} новых "
                      f"(без @username пропущено: {result.get('no_username', 0)})")

            # Пауза между чатами чтобы не получить FloodWait
            if i < len(chats):
                await asyncio.sleep(3)

        await client.disconnect()

        print(f"\n{'='*40}")
        print(f"Обработано чатов: {len(chats)}  (ошибок: {total_errors})")
        print(f"Всего участников: {total_parsed}")
        print(f"Добавлено новых:  {total_added}")
        print(f"Дублей пропущено: {total_skipped}")

    asyncio.run(_run())


# ========================================================================
#  Прокси
# ========================================================================

def handle_proxy():
    while True:
        current = load_proxy()
        print("\n--- Настройки прокси ---")
        print(f"Общий (фолбэк): {current or 'не установлен'}")
        print("\n  1. Установить общий прокси")
        print("  2. Удалить общий прокси")
        print("  3. Прокси по аккаунтам — список")
        print("  4. Назначить прокси одному аккаунту")
        print("  5. Массово назначить (список прокси → по одному на аккаунт)")
        print("  6. Убрать персональный прокси у аккаунта")
        print("  0. Назад")

        choice = input("\nВыбор: ").strip()

        if choice == "1":
            proxy = input("SOCKS5 (socks5://...) или MTProxy (tg://proxy?...): ").strip()
            proxy = _valid_proxy_or_none(proxy)
            if not proxy:
                print("Отмена.")
                continue
            save_proxy(proxy)
            print(f"Сохранен: {proxy}")
        elif choice == "2":
            from data.db import clear_proxy
            clear_proxy()
            print("Удален.")
        elif choice == "3":
            _show_account_proxies()
        elif choice == "4":
            _handle_set_account_proxy()
        elif choice == "5":
            _handle_bulk_assign_proxies()
        elif choice == "6":
            _handle_clear_account_proxy()
        elif choice == "0":
            break
        else:
            print("Неверный выбор.")


def _valid_proxy_or_none(s: str) -> str | None:
    """Принимает socks5://... или tg://proxy?server=...&port=...&secret=... (MTProxy)."""
    from proxy_manager import parse_mtproxy_link

    s = s.strip()
    if not s:
        return None
    if s.startswith("socks5://"):
        return s
    if parse_mtproxy_link(s):
        return s
    print(f"  Пропущено (не socks5:// и не tg://proxy?...): {s}")
    return None


def _show_account_proxies():
    """Показывает, у какого аккаунта какой прокси (персональный / общий фолбэк / нет)."""
    from data.db import all_account_proxies

    sessions = get_session_files()
    if not sessions:
        print("Нет аккаунтов.")
        return

    assigned = all_account_proxies()
    common = load_proxy()

    print(f"\n--- Прокси по аккаунтам ({len(sessions)}) ---")
    for s in sessions:
        name = os.path.splitext(os.path.basename(s))[0]
        if name in assigned:
            print(f"  {name:<15} персональный: {assigned[name]}")
        elif common:
            print(f"  {name:<15} общий (фолбэк): {common}")
        else:
            print(f"  {name:<15} нет прокси (прямое соединение)")

    without_any = [os.path.splitext(os.path.basename(s))[0] for s in sessions
                   if os.path.splitext(os.path.basename(s))[0] not in assigned and not common]
    if without_any:
        print(f"\n⚠ Без прокси вообще: {len(without_any)} — все ходят с твоего реального IP.")


def _handle_set_account_proxy():
    sessions = get_session_files()
    if not sessions:
        print("Нет аккаунтов.")
        return

    names = [os.path.splitext(os.path.basename(s))[0] for s in sessions]
    print("\nАккаунты:")
    for i, n in enumerate(names, 1):
        print(f"  {i}. {n}")

    sel = input("\nНомер аккаунта: ").strip()
    try:
        idx = int(sel) - 1
        if not (0 <= idx < len(names)):
            print("Неверный номер.")
            return
    except ValueError:
        print("Неверный ввод.")
        return

    proxy = input("SOCKS5 (socks5://...) или MTProxy (tg://proxy?...): ").strip()
    proxy = _valid_proxy_or_none(proxy)
    if not proxy:
        print("Отмена.")
        return

    from data.db import set_account_proxy
    set_account_proxy(names[idx], proxy)
    print(f"Назначено: {names[idx]} → {proxy}")


def _handle_bulk_assign_proxies():
    """
    Вставляешь список прокси (по одному на строку) — раздаются по одному
    на аккаунт, в том же порядке, что в списке аккаунтов. Прокси меньше,
    чем аккаунтов — хвост останется без персонального (упадёт на общий).
    """
    sessions = get_session_files()
    if not sessions:
        print("Нет аккаунтов.")
        return

    names = [os.path.splitext(os.path.basename(s))[0] for s in sessions]
    print(f"\n--- Массовое назначение прокси ({len(names)} аккаунтов) ---")
    print("Вставь список прокси, по одному на строку (socks5://... или tg://proxy?...).")
    text = input_multiline("Список прокси:")
    proxies = [p for p in (_valid_proxy_or_none(line) for line in text.split("\n")) if p]

    if not proxies:
        print("Ни одного валидного прокси не найдено. Отмена.")
        return

    print(f"\nПрокси: {len(proxies)}, аккаунтов: {len(names)}")
    if len(proxies) < len(names):
        print(f"  ⚠ Прокси меньше, чем аккаунтов — последним {len(names) - len(proxies)} "
              f"персональный не достанется (упадут на общий фолбэк).")
    elif len(proxies) > len(names):
        print(f"  Лишние {len(proxies) - len(names)} прокси не используются.")

    print("\nПревью назначений:")
    for name, proxy in zip(names, proxies):
        print(f"  {name} → {proxy}")

    if input("\nПрименить? (y/n): ").strip().lower() != "y":
        print("Отменено.")
        return

    from data.db import set_account_proxy
    for name, proxy in zip(names, proxies):
        set_account_proxy(name, proxy)
    print(f"Готово: назначено {min(len(names), len(proxies))} аккаунтам.")


def _handle_clear_account_proxy():
    from data.db import all_account_proxies, clear_account_proxy

    assigned = all_account_proxies()
    if not assigned:
        print("Ни у одного аккаунта нет персонального прокси.")
        return

    names = list(assigned.keys())
    print("\nС персональным прокси:")
    for i, n in enumerate(names, 1):
        print(f"  {i}. {n}  ({assigned[n]})")

    sel = input("\nНомер (0 = все сразу): ").strip()
    if sel == "0":
        if input(f"Убрать персональный прокси у ВСЕХ {len(names)}? (y/n): ").strip().lower() != "y":
            print("Отменено.")
            return
        for n in names:
            clear_account_proxy(n)
        print(f"Убрано у {len(names)} аккаунтов.")
        return

    try:
        idx = int(sel) - 1
        if not (0 <= idx < len(names)):
            print("Неверный номер.")
            return
    except ValueError:
        print("Неверный ввод.")
        return

    clear_account_proxy(names[idx])
    print(f"Убрано у {names[idx]}.")


# ========================================================================
#  Main
# ========================================================================

def main():
    os.makedirs(SESSIONS_DIR, exist_ok=True)

    while True:
        show_menu()
        choice = input("\nВыбор: ").strip()

        if choice == "1":
            handle_accounts()
        elif choice == "2":
            handle_neuro_commenting()
        elif choice == "3":
            handle_broadcasting()
        elif choice == "4":
            handle_parsing()
        elif choice == "5":
            handle_users_base()
        elif choice == "6":
            handle_proxy()
        elif choice == "0":
            print("Выход.")
            break
        else:
            print("Неверный выбор.")


if __name__ == "__main__":
    import atexit
    from data.analytics import init_analytics
    from data.db import init_db, migrate_from_files, sync_all_to_db
    # Инициализируем БД; при первом запуске переносим sessions/, proxy.txt → БД
    init_db()
    init_analytics()  # таблицы sends/templates/account_meta и т.д. — без них оптимизатор не работает
    migrate_from_files(SESSIONS_DIR, DATA_DIR)
    # При выходе синхронизируем обновлённые сессии обратно в БД
    atexit.register(lambda: sync_all_to_db(SESSIONS_DIR))
    migrate_all_sessions()
    main()
