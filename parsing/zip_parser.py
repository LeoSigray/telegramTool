"""
Парсинг ZIP-архива с текстовыми файлами чатов.

Структура ZIP: Чаты/Регион/Чаты_Категория.txt
Каждый txt-файл содержит ссылки (https://t.me/...) по одной на строку.
Категория определяется по названию файла.
"""

import os
import re
import zipfile

from .categories import map_zip_category
from .excel_writer import ParsingExcelWriter


def _extract_links(content: str) -> list[str]:
    """Извлекает ссылки t.me из текста, очищает от мусора."""
    raw_links = re.findall(r"https://t\.me/\S+", content)
    clean = []
    for link in raw_links:
        link = link.strip().rstrip(".,;)]!\"'")
        link = link.split("?")[0]  # убираем query params
        if link and len(link) > len("https://t.me/"):
            clean.append(link)
    return clean


def parse_zip_file(
    zip_path: str,
    writer: ParsingExcelWriter,
) -> dict:
    """
    Парсит ZIP-архив и записывает ссылки в Excel по категориям.

    Возвращает статистику.
    """
    if not os.path.exists(zip_path):
        raise FileNotFoundError(f"ZIP не найден: {zip_path}")

    stats = {}  # {категория: кол-во добавленных}
    total_links = 0
    total_added = 0

    with zipfile.ZipFile(zip_path, "r") as zf:
        txt_files = [f for f in zf.infolist() if f.filename.endswith(".txt") and not f.is_dir()]

        print(f"  Найдено {len(txt_files)} текстовых файлов в архиве")

        for fi in txt_files:
            # Парсим путь: Чаты/Регион/Чаты_Категория.txt
            parts = fi.filename.split("/")
            basename = os.path.splitext(parts[-1])[0]

            # Извлекаем категорию из имени файла (Чаты_КАТЕГОРИЯ -> КАТЕГОРИЯ)
            if "_" in basename:
                zip_category = basename.split("_", 1)[1]
            else:
                zip_category = basename

            # Извлекаем регион (второй уровень пути)
            region = parts[1] if len(parts) >= 3 else ""

            # Маппим категорию из ZIP в нашу систему
            category = map_zip_category(zip_category)
            source = f"ZIP / {region}" if region else "ZIP архив"

            # Читаем содержимое
            content = zf.read(fi.filename).decode("utf-8", errors="ignore")
            links = _extract_links(content)
            total_links += len(links)

            if links:
                added = writer.add_zip_chats(category, links, source=source)
                total_added += added
                stats[category] = stats.get(category, 0) + added

                if added > 0:
                    print(f"  -> [{region}] {zip_category} -> {category}: +{added} новых (из {len(links)})")

        total_files = len(txt_files)

    return {
        "total_files": total_files,
        "total_links": total_links,
        "total_added": total_added,
        "by_category": stats,
    }
