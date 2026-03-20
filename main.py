import asyncio
import os

from config import (
    load_proxy, save_proxy, SESSIONS_DIR, PARSED_EXCEL_FILE,
    PARSE_DELAY_MIN, PARSE_DELAY_MAX, FOLDER_LINKS,
)
from accounts.manager import (
    list_accounts, check_account, get_session_files, create_client, migrate_all_sessions,
)
from accounts.lzt_buyer import buy_accounts_interactive
from accounts.tdata_importer import import_tdata_interactive
from messaging.dm_sender import run_dm_sending
from messaging.chat_sender import run_chat_sending
from messaging.inviter import run_inviting
from parsing.excel_writer import ParsingExcelWriter
from parsing.folder_parser import parse_all_folders
from parsing.zip_parser import parse_zip_file


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
    print("  0. Выход")
    print("-" * 50)


def show_accounts_menu():
    print("\n--- Управление аккаунтами ---")
    print("  1. Купить аккаунты (LZT.market)")
    print("  2. Импортировать tdata")
    print("  3. Список аккаунтов")
    print("  4. Проверить аккаунты")
    print("  0. Назад")


def handle_accounts():
    while True:
        show_accounts_menu()
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
        elif choice == "0":
            break


def handle_dm_sending():
    print("\n--- Рассылка в ЛС ---")
    print("Юзеры загружаются из data/targets.xlsx (лист 'Users')")
    message = input("\nВведите текст сообщения:\n> ").strip()
    if not message:
        print("Сообщение пустое. Отмена.")
        return
    print(f"\nПодтвердите отправку? Текст: '{message[:50]}...' (y/n): ", end="")
    if input().strip().lower() != "y":
        print("Отменено.")
        return
    asyncio.run(run_dm_sending(message))


def handle_chat_sending():
    print("\n--- Рассылка по чатам ---")
    print("Чаты загружаются из data/targets.xlsx (лист 'Chats')")
    message = input("\nВведите текст сообщения:\n> ").strip()
    if not message:
        print("Сообщение пустое. Отмена.")
        return
    print(f"\nПодтвердите отправку? Текст: '{message[:50]}...' (y/n): ", end="")
    if input().strip().lower() != "y":
        print("Отменено.")
        return
    asyncio.run(run_chat_sending(message))


def handle_inviting():
    print("\n--- Инвайтинг в группу ---")
    print("Юзеры загружаются из data/targets.xlsx (лист 'Users')")
    target = input("ID или @username целевой группы: ").strip()
    if not target:
        print("Группа не указана. Отмена.")
        return

    # Парсим числовой ID или оставляем как username
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


def handle_parsing():
    while True:
        print("\n--- Парсинг чатов ---")
        print(f"  Результаты: {PARSED_EXCEL_FILE}")
        print("  1. Парсинг папок (addlist ссылки)")
        print("  2. Парсинг ZIP-архива")
        print("  3. Всё сразу (папки + ZIP)")
        print("  0. Назад")

        choice = input("\nВыбор: ").strip()

        if choice == "1":
            handle_parse_folders()
        elif choice == "2":
            handle_parse_zip()
        elif choice == "3":
            handle_parse_folders()
            handle_parse_zip()
        elif choice == "0":
            break


def handle_parse_folders():
    """Парсинг папок Telegram по ссылкам addlist."""
    sessions = get_session_files()
    if not sessions:
        print("Нет аккаунтов. Добавьте хотя бы один в 'Управление аккаунтами'.")
        return

    print("\n--- Парсинг папок ---")
    print(f"  Сохраненных ссылок в config.py: {len(FOLDER_LINKS)}")
    print("  1. Парсить сохраненные ссылки из config.py")
    print("  2. Ввести ссылки вручную")
    print("  3. Сохраненные + ввести дополнительные")
    print("  0. Назад")

    choice = input("\nВыбор: ").strip()
    if choice == "0":
        return

    folder_links = []

    if choice in ("1", "3"):
        folder_links.extend(FOLDER_LINKS)
        print(f"  Загружено из config: {len(FOLDER_LINKS)} ссылок")

    if choice in ("2", "3"):
        print("Введите ссылки (по одной на строку, пустая строка = конец):")
        while True:
            line = input("> ").strip()
            if not line:
                break
            if "t.me/addlist/" in line:
                folder_links.append(line)
            else:
                print(f"  Пропущено (не addlist ссылка): {line}")

    if not folder_links:
        print("Нет ссылок для парсинга.")
        return

    # Убираем дубли, сохраняя порядок
    seen = set()
    unique = []
    for link in folder_links:
        if link not in seen:
            seen.add(link)
            unique.append(link)
    folder_links = unique

    print(f"\nСсылок для парсинга: {len(folder_links)}")
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

        writer = ParsingExcelWriter(PARSED_EXCEL_FILE)
        print("\nПарсинг папок...")
        stats = await parse_all_folders(
            client, folder_links, writer,
            delay_min=PARSE_DELAY_MIN, delay_max=PARSE_DELAY_MAX,
        )
        writer.save()
        await client.disconnect()

        print(f"\nГотово! Спарсено: {stats['total_parsed']}, добавлено новых: {stats['total_added']}")
        print(f"Файл: {PARSED_EXCEL_FILE}")

    asyncio.run(_run())


def handle_parse_zip():
    """Парсинг ZIP-архива с чатами."""
    print("\n--- Парсинг ZIP-архива ---")
    zip_path = input("Путь к ZIP-файлу: ").strip().strip('"')

    if not zip_path or not os.path.exists(zip_path):
        print(f"Файл не найден: {zip_path}")
        return

    print(f"Парсинг: {zip_path}")
    writer = ParsingExcelWriter(PARSED_EXCEL_FILE)

    try:
        stats = parse_zip_file(zip_path, writer)
        writer.save()

        print(f"\nГотово!")
        print(f"  Файлов обработано: {stats['total_files']}")
        print(f"  Ссылок найдено: {stats['total_links']}")
        print(f"  Добавлено новых: {stats['total_added']}")
        print(f"  Файл: {PARSED_EXCEL_FILE}")

        if stats['by_category']:
            print("\nПо категориям:")
            for cat, count in sorted(stats['by_category'].items(), key=lambda x: -x[1]):
                print(f"  {cat}: +{count}")
    except Exception as e:
        print(f"Ошибка: {e}")


def handle_proxy():
    print("\n--- Настройки прокси ---")
    current = load_proxy()
    if current:
        print(f"Текущий прокси: {current}")
    else:
        print("Прокси не установлен.")

    print("\n  1. Установить прокси")
    print("  2. Удалить прокси")
    print("  0. Назад")

    choice = input("\nВыбор: ").strip()

    if choice == "1":
        proxy = input("SOCKS5 прокси (socks5://user:pass@host:port): ").strip()
        if not proxy:
            print("Отмена.")
            return
        if not proxy.startswith("socks5://"):
            print("Формат: socks5://user:pass@host:port")
            return
        save_proxy(proxy)
        print(f"Прокси сохранен: {proxy}")
    elif choice == "2":
        if os.path.exists("proxy.txt"):
            os.remove("proxy.txt")
            print("Прокси удален.")
        else:
            print("Прокси не был установлен.")


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
        elif choice == "0":
            print("Выход.")
            break
        else:
            print("Неверный выбор.")


if __name__ == "__main__":
    migrate_all_sessions()  # автоматически фиксим старые сессии при старте
    main()
