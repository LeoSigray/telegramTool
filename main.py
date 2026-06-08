import asyncio
import os

# Загружаем .env до всех остальных импортов
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from config import (
    load_proxy, save_proxy, SESSIONS_DIR, EXCEL_FILE,
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
from messaging.dm_sender import run_dm_sending
from messaging.chat_sender import run_chat_sending
from messaging.inviter import run_inviting
from parsing.folder_parser import parse_all_folders
from parsing.zip_parser import parse_zip_file
from data.excel_manager import (
    get_category_stats, get_category_names, load_chats_by_category,
    export_categories_to_chats, import_from_parsed_excel,
)


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
    print("  2. Рассылка в ЛС")
    print("  3. Рассылка по чатам")
    print("  4. Инвайтинг в группу")
    print("  5. Парсинг чатов")
    print("  6. Настройки прокси")
    print("  7. Добавить людей для рассылки")
    print("  8. Добавить чат → спарсить участников")
    print("  9. Нейрокомментинг (Gemini)")
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
        print("  3. Список аккаунтов")
        print("  4. Проверить аккаунты")
        print("  5. Изменить описание профиля (bio)")
        print("  6. Конвертировать accounts → sessions")
        print("  7. Скачать сессии с LZT (для уже купленных)")
        print("  0. Назад")

        choice = input("\nВыбор: ").strip()

        if choice == "1":
            buy_accounts_interactive()
        elif choice == "2":
            import_tdata_interactive()
        elif choice == "3":
            list_accounts()
        elif choice == "4":
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
        elif choice == "5":
            _handle_update_bio()
        elif choice == "6":
            convert_accounts_interactive()
        elif choice == "7":
            _handle_download_sessions_lzt()
        elif choice == "0":
            break


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
#  Добавить людей / парсинг участников
# ========================================================================

def handle_add_users():
    """Пункт 7 — ручное добавление пользователей в лист 'Users'."""
    print("\n--- Добавить людей для рассылки ---")
    print(f"Файл: {EXCEL_FILE}  |  Лист: Users")
    print("\nФорматы: @username, 123456789, +79991234567")
    print("Пустая строка — завершить ввод.\n")

    comment_default = input("Комментарий по умолчанию (Enter = пусто): ").strip()

    entries = []
    print("\nВводи по одному (пустая строка = конец):")
    while True:
        line = input("> ").strip()
        if not line:
            if entries:
                break
            continue

        parts = line.split(None, 1)
        identifier = parts[0]
        inline_comment = parts[1] if len(parts) > 1 else ""
        comment = inline_comment or comment_default

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
    print(f"\nГотово! Добавлено: {stats['added']}, пропущено дублей: {stats['skipped']}")


def handle_parse_members():
    """Пункт 8 — спарсить участников чата и добавить в Users."""
    sessions = get_session_files()
    if not sessions:
        print("Нет аккаунтов.")
        return

    print("\n--- Парсинг участников чата ---")
    chat_link = input("Ссылка или @username чата: ").strip()
    if not chat_link:
        return

    comment = input("Метка/комментарий для записей (Enter = пусто): ").strip()

    print(f"\nАккаунт для парсинга: {os.path.basename(sessions[0])}")
    print(f"Начать? (y/n): ", end="")
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

        print("Парсинг участников...")
        last_pct = [-1]

        def progress(current, total):
            pct = int(current / total * 100) if total else 0
            if pct != last_pct[0] and pct % 10 == 0:
                print(f"  {pct}% ({current}/{total})")
                last_pct[0] = pct

        result = await parse_members_from_chat(client, chat_link, comment=comment, progress_cb=progress)
        await client.disconnect()

        if result["error"]:
            print(f"Ошибка: {result['error']}")
        else:
            print(f"\nГотово!")
            print(f"  Спарсено участников:  {result['parsed']}")
            print(f"  Добавлено в Users:    {result['added']}")
            print(f"  Пропущено (дубли):    {result['skipped']}")

    asyncio.run(_run())


# ========================================================================
#  Рассылка
# ========================================================================

def handle_dm_sending():
    print("\n--- Рассылка в ЛС ---")
    print(f"Юзеры из {EXCEL_FILE} (лист 'Users')")
    message = input_multiline("\nТекст сообщения:").strip()
    if not message:
        print("Пустое сообщение. Отмена.")
        return
    print(f"\nСообщение:\n---\n{message}\n---")
    print("Отправить? (y/n): ", end="")
    if input().strip().lower() != "y":
        print("Отменено.")
        return
    asyncio.run(run_dm_sending(message))


def handle_chat_sending():
    """
    Единая рассылка по чатам.
    Выбор источника: категории или лист Chats.
    Автоматически вступает в чаты если не состоит (с лимитом).
    """
    from data.excel_manager import load_chats

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
            print("Нет категорий. Сначала запусти парсинг (пункт 5).")
            return

        stats = get_category_stats()
        print("\nКатегории:")
        for i, cat in enumerate(categories, 1):
            count = stats.get(cat, 0)
            print(f"  {i}. {cat} ({count})")

        print(f"\n  0 = все категории")
        sel = input("\nНомера через запятую (или 0): ").strip()
        if not sel:
            return

        if sel == "0":
            selected = categories
        else:
            selected = []
            for part in sel.split(","):
                try:
                    idx = int(part.strip()) - 1
                    if 0 <= idx < len(categories):
                        selected.append(categories[idx])
                except ValueError:
                    pass

        if not selected:
            print("Ничего не выбрано.")
            return

        chats = load_chats_by_category(selected)
        if not chats:
            print("В выбранных категориях нет чатов с username.")
            return

        print(f"\nКатегории: {', '.join(selected)}")

    elif choice == "2":
        raw = load_chats()
        if not raw:
            print(f"Лист 'Chats' в {EXCEL_FILE} пуст.")
            return
        chats = raw

    else:
        return

    print(f"Чатов для рассылки: {len(chats)}")

    message = input_multiline("\nТекст сообщения:").strip()
    if not message:
        print("Пустое сообщение. Отмена.")
        return

    sessions = get_session_files()
    from data.daily_limits import DAILY_JOIN_LIMIT, DAILY_SEND_LIMIT

    print(f"\nАккаунтов: {len(sessions)}, чатов: {len(chats)}")
    print(f"Дневной лимит: {DAILY_JOIN_LIMIT} вступлений, {DAILY_SEND_LIMIT} отправок/аккаунт")

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

    asyncio.run(run_chat_sending(message, chats, parallel=parallel))


def handle_inviting():
    print("\n--- Инвайтинг в группу ---")
    print(f"Юзеры из {EXCEL_FILE} (лист 'Users')")
    target = input("ID или @username группы: ").strip()
    if not target:
        print("Не указана группа. Отмена.")
        return
    try:
        target = int(target)
    except ValueError:
        if not target.startswith("@"):
            target = f"@{target}"
    print(f"Инвайтинг в {target}. Подтвердить? (y/n): ", end="")
    if input().strip().lower() != "y":
        print("Отменено.")
        return
    asyncio.run(run_inviting(target))


# ========================================================================
#  Парсинг
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
        print("  4. Экспорт категорий в 'Chats' (для рассылки)")
        print("  5. Импорт из parsed_chats.xlsx")
        print("  6. Статистика по категориям")
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
            handle_export_to_chats()
        elif choice == "5":
            handle_import_parsed()
        elif choice == "6":
            _show_category_stats()
        elif choice == "0":
            break


def handle_parse_folders():
    """Парсинг папок Telegram по addlist-ссылкам."""
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
            print("  Нет сохраненных ссылок. Введите вручную или добавьте в data/folder_links.txt")
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

        # Сохраняем новые ссылки в файл
        if new_links:
            all_saved = list(dict.fromkeys(saved + new_links))  # дедуп с порядком
            save_folder_links(all_saved)
            print(f"  Сохранено в folder_links.txt: {len(all_saved)} ссылок")

    # Дедупликация
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
    """Парсинг ZIP-архива с чатами."""
    print("\n--- Парсинг ZIP ---")
    zip_path = input("Путь к ZIP-файлу: ").strip().strip('"')

    if not zip_path or not os.path.exists(zip_path):
        print(f"Файл не найден: {zip_path}")
        return

    try:
        print(f"Парсинг: {zip_path}")
        stats = parse_zip_file(zip_path)

        print(f"\nГотово!")
        print(f"  Файлов: {stats['total_files']}")
        print(f"  Ссылок найдено: {stats['total_links']}")
        print(f"  Добавлено новых: {stats['total_added']}")

        if stats['by_category']:
            print("\nПо категориям:")
            for cat, count in sorted(stats['by_category'].items(), key=lambda x: -x[1]):
                print(f"  {cat}: +{count}")
    except Exception as e:
        print(f"Ошибка: {e}")


def handle_export_to_chats():
    """Экспорт чатов из категорий в лист Chats для рассылки."""
    categories = get_category_names()
    if not categories:
        print("Нет категорий. Сначала запусти парсинг.")
        return

    stats = get_category_stats()
    print("\n--- Экспорт в лист 'Chats' ---")
    print("Доступные категории:")
    for i, cat in enumerate(categories, 1):
        count = stats.get(cat, 0)
        print(f"  {i}. {cat} ({count})")

    print(f"\n  0. Все категории")
    print(f"  q. Отмена")

    choice = input("\nВыбор (номера через запятую или 0=все): ").strip()
    if choice.lower() == "q":
        return

    if choice == "0":
        selected = None  # все
        print("Экспорт всех категорий...")
    else:
        selected = []
        for part in choice.split(","):
            part = part.strip()
            try:
                idx = int(part) - 1
                if 0 <= idx < len(categories):
                    selected.append(categories[idx])
            except ValueError:
                pass
        if not selected:
            print("Ничего не выбрано.")
            return
        print(f"Экспорт: {', '.join(selected)}")

    added = export_categories_to_chats(selected)
    print(f"Добавлено в 'Chats': {added} чатов")
    if added > 0:
        print("Теперь можно делать рассылку через пункт 3!")


def handle_import_parsed():
    """Импорт данных из отдельного parsed_chats.xlsx."""
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
            print("\nТеперь используй 'Экспорт в Chats' для рассылки.")
        else:
            print("Новых чатов не найдено (всё уже импортировано).")
    except Exception as e:
        print(f"Ошибка: {e}")


def _show_category_stats():
    """Показывает статистику по категориям."""
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
#  Нейрокомментинг
# ========================================================================

def handle_neuro_commenting():
    sessions = get_session_files()
    if not sessions:
        print("Нет аккаунтов.")
        return

    import os as _os
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

    print(f"\nНастройки:")
    print(f"  Ключевых слов:  {len(keywords)}: {', '.join(keywords)}")
    print(f"  Мин. подписчики: {min_subs}")
    print(f"  Постов на канал: {posts_per_channel}")
    print(f"  Аккаунтов:      {len(sessions)}")
    print("\nЗапустить? (y/n): ", end="")
    if input().strip().lower() != "y":
        print("Отменено.")
        return

    async def _run():
        from api.comment_runner import run_comment_job
        from api.jobs import jobs as job_manager
        from api.client_pool import pool

        # Поднимаем пул клиентов
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

        print(f"\n[job {job.id}] Ищем каналы по: {', '.join(keywords)}...")
        print("(Ctrl+C для остановки)\n")

        try:
            await run_comment_job(job, keywords, min_subs, posts_per_channel)
        except KeyboardInterrupt:
            job.cancel.set()
            print("\nОстановка...")

        print(f"\n{'='*40}")
        print(f"Статус:         {job.status}")
        print(f"Каналов найдено:{job.total}")
        print(f"Комментариев:   {job.sent}")
        print(f"Пропущено:      {job.skipped}")
        print(f"Ошибок:         {job.failed}")

        # Лог последних событий
        if job.log:
            print("\nПоследние события:")
            for entry in list(job.log)[-10:]:
                ev = entry.get("event", "")
                if ev == "commented":
                    print(f"  ✓ @{entry.get('channel')} — «{entry.get('comment_preview', '')[:60]}»")
                elif ev == "channels_found":
                    print(f"  🔍 Найдено каналов: {entry.get('count')}")
                elif ev in ("gemini_error", "comment_error", "flood_wait"):
                    print(f"  ✗ {ev}: {entry.get('error') or entry.get('channel')}")

        await pool.shutdown()

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
        if os.path.exists("proxy.txt"):
            os.remove("proxy.txt")
            print("Удален.")
        else:
            print("Не был установлен.")


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
            handle_dm_sending()
        elif choice == "3":
            handle_chat_sending()
        elif choice == "4":
            handle_inviting()
        elif choice == "5":
            handle_parsing()
        elif choice == "6":
            handle_proxy()
        elif choice == "7":
            handle_add_users()
        elif choice == "8":
            handle_parse_members()
        elif choice == "9":
            handle_neuro_commenting()
        elif choice == "0":
            print("Выход.")
            break
        else:
            print("Неверный выбор.")


if __name__ == "__main__":
    migrate_all_sessions()
    main()
