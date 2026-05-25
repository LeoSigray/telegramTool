# ============================================================
# ПАТЧ для main.py
# ============================================================
# 1. Добавь в блок импортов (после строки с excel_manager):
#
#    from data.gsheets_manager import export_to_gsheets_interactive
#
# 2. Замени функцию handle_parsing() на версию ниже:
# ============================================================

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
        print("  7. Экспорт в Google Sheets")   # ← НОВЫЙ ПУНКТ
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
        elif choice == "7":
            export_to_gsheets_interactive()   # ← НОВЫЙ ПУНКТ
        elif choice == "0":
            break
