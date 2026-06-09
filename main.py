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
    print("  2. Нейрокомментинг (Gemini)")
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
    """Ввод многострочного текста. Пустая строка = конец ввода."""
    print(prompt)
    print("(пустая строка = завершить ввод)")
    lines = []
    while True:
        line = input()
        if line == "" and lines:
            break
        lines.append(line)
    return "\n".join(lines)


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
        print("  8. Сгенерировать имена/фамилии (Gemini)")
        print("  9. Привязать личный канал к профилю")
        print(" 10. Конвертировать accounts → sessions")
        print(" 11. Скачать сессии с LZT (для уже купленных)")
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
        elif choice == "0":
            break


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


def _handle_generate_names():
    """Сгенерировать имена/фамилии через Gemini и применить ко всем аккаунтам."""
    from api.gemini import is_configured, generate_names

    sessions = get_session_files()
    if not sessions:
        print("Нет аккаунтов.")
        return

    if not is_configured():
        print("\n⚠  GEMINI_API_KEY не задан в .env")
        return

    print("\n--- Сгенерировать имена/фамилии (Gemini) ---")
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

    print("\nПромт для Gemini (описание стиля имён).")
    print("Например: «русские мужские», «западные женские, 25-35 лет», «нейтральные»")
    prompt = input("Промт (Enter = без уточнений): ").strip()

    print(f"\nГенерирую {len(sessions)} вариантов... ", end="", flush=True)

    async def _run():
        from api.client_pool import pool
        from api.routes_accounts import ProfileIn, _apply_profile

        # Генерируем имена (не нужен пул — только Gemini)
        try:
            generated = await generate_names(prompt, count=len(sessions), fields=fields)
        except Exception as e:
            print(f"✗\nОшибка Gemini: {e}")
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

        # Резолвим entity канала один раз через первый аккаунт
        channel_entity = None
        if not clear_mode:
            first_client = next(iter(pool.clients.values()))
            try:
                channel_entity = await first_client.get_input_entity(channel_input)
                print(f"Канал найден: @{channel_input}\n")
            except Exception as e:
                print(f"Не удалось найти канал @{channel_input}: {e}")
                await pool.shutdown()
                return

        ok = fail = 0
        for acc_name, client in pool.clients.items():
            try:
                if clear_mode:
                    await client(UpdatePersonalChannelRequest(channel=InputChannelEmpty()))
                    print(f"  ✓ {acc_name} — канал отвязан")
                else:
                    await client(UpdatePersonalChannelRequest(channel=channel_entity))
                    print(f"  ✓ {acc_name} → @{channel_input}")
                ok += 1
            except Exception as e:
                print(f"  ✗ {acc_name}: {e}")
                fail += 1

        await pool.shutdown()
        print(f"\nГотово: {ok} успешно, {fail} ошибок")

    asyncio.run(_run())


def _handle_download_sessions_lzt():
    """Скачивает .session с LZT для аккаунтов из accounts/ у которых ещё нет сессии."""
    from accounts.lzt_buyer import LZTMarketAPI, login_via_code

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
        session_path = asyncio.run(login_via_code(item, api))
        if session_path:
            print(f"  [{item_id}] ✓ Сессия готова")
            ok += 1
        else:
            print(f"  [{item_id}] ✗ Не удалось создать сессию")
            fail += 1

    print(f"\nГотово: {ok} успешно, {fail} ошибок")


# ========================================================================
#  Нейрокомментинг
# ========================================================================

def handle_neuro_commenting():
    sessions = get_session_files()
    if not sessions:
        print("Нет аккаунтов.")
        return

    from api.gemini import is_configured

    if not is_configured():
        print("\n⚠  GEMINI_API_KEY не задан в .env")
        print("   Получи ключ на https://aistudio.google.com/app/apikey")
        print("   и добавь в .env: GEMINI_API_KEY=AIzaSy...")
        return

    print("\n--- Нейрокомментинг ---")
    print("Gemini читает пост и пишет живой осмысленный комментарий.\n")

    # Стиль
    print("Стиль комментирования (промт для Gemini).")
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
                elif ev in ("gemini_error", "comment_error", "flood_wait"):
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


def _run_job_console(job, runner_coro):
    """Запускает job через пул клиентов. Выводит итог."""
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
        print(f"\n{'='*40}")
        print(f"Статус:      {job.status}")
        print(f"Отправлено:  {job.sent}")
        print(f"Пропущено:   {job.skipped}")
        print(f"Ошибок:      {job.failed}")
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
    message = input_multiline("\nТекст сообщения:").strip()
    if not message:
        print("Пустое сообщение. Отмена.")
        return

    print(f"\nСообщение:\n---\n{message}\n---")
    print(f"Отправить {len(users)} контактам? (y/n): ", end="")
    if input().strip().lower() != "y":
        print("Отменено.")
        return

    from api.dm_runner import run_dm_job
    from api.jobs import jobs as job_manager

    # Конвертируем в строки для job.targets
    targets = [f"@{u['username']}" if u.get("username") else str(u["user_id"]) for u in users]
    job = job_manager.create(kind="dm", message=message, targets=targets, parallel=1)
    _run_job_console(job, run_dm_job(job))


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
    message = input_multiline("\nТекст сообщения:").strip()
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
            print(f"Готово! Спарсено: {result['parsed']}, добавлено: {result['added']}, дублей: {result['skipped']}")

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
                print(f"✓ {result['parsed']} участников, +{result['added']} новых")

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
    print("\n--- Настройки прокси ---")
    current = load_proxy()
    if current:
        print(f"Текущий: {current}")
    else:
        print("Не установлен.")

    print("\n  1. Установить прокси")
    print("  2. Удалить прокси")
    print("  0. Назад")

    choice = input("\nВыбор: ").strip()

    if choice == "1":
        proxy = input("SOCKS5 (socks5://user:pass@host:port): ").strip()
        if not proxy:
            print("Отмена.")
            return
        if not proxy.startswith("socks5://"):
            print("Формат: socks5://user:pass@host:port")
            return
        save_proxy(proxy)
        print(f"Сохранен: {proxy}")
    elif choice == "2":
        from data.db import clear_proxy
        clear_proxy()
        print("Удален.")


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
    from data.db import init_db, migrate_from_files, sync_all_to_db
    # Инициализируем БД; при первом запуске переносим sessions/, proxy.txt → БД
    init_db()
    migrate_from_files(SESSIONS_DIR, DATA_DIR)
    # При выходе синхронизируем обновлённые сессии обратно в БД
    atexit.register(lambda: sync_all_to_db(SESSIONS_DIR))
    migrate_all_sessions()
    main()
