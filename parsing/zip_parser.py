"""
Парсинг ZIP-архива с текстовыми файлами чатов.

Структура ZIP: Чаты/Регион/Чаты_Категория.txt
Каждый txt содержит ссылки (https://t.me/...) по одной на строку.
Категория определяется по имени файла, записывается в targets.xlsx.
"""

import os
import re
import zipfile

from .categories import map_zip_category
from data.excel_manager import add_zip_chats


def _extract_links(content: str) -> list[str]:
    """Извлекает ссылки t.me из текста."""
    raw = re.findall(r"https://t\.me/\S+", content)
    clean = []
    for link in raw:
        link = link.strip().rstrip(".,;)]!\"'").split("?")[0]
        if link and len(link) > len("https://t.me/"):
            clean.append(link)
    return clean


def parse_zip_file(zip_path: str) -> dict:
    """
    Парсит ZIP-архив, записывает ссылки в targets.xlsx по категориям.
    Возвращает статистику.
    """
    if not os.path.exists(zip_path):
        raise FileNotFoundError(f"ZIP не найден: {zip_path}")

    stats = {}
    total_links = 0
    total_added = 0

    with zipfile.ZipFile(zip_path, "r") as zf:
        txt_files = [f for f in zf.infolist() if f.filename.endswith(".txt") and not f.is_dir()]
        print(f"  Найдено {len(txt_files)} текстовых файлов")

        for fi in txt_files:
            parts = fi.filename.split("/")
            basename = os.path.splitext(parts[-1])[0]

            # Чаты_КАТЕГОРИЯ -> КАТЕГОРИЯ
            zip_category = basename.split("_", 1)[1] if "_" in basename else basename
            region = parts[1] if len(parts) >= 3 else ""
            category = map_zip_category(zip_category)
            source = f"ZIP / {region}" if region else "ZIP"

            content = zf.read(fi.filename).decode("utf-8", errors="ignore")
            links = _extract_links(content)
            total_links += len(links)

            if links:
                added = add_zip_chats(category, links, source=source)
                total_added += added
                stats[category] = stats.get(category, 0) + added
                if added > 0:
                    print(f"  -> [{region}] {zip_category} -> {category}: +{added}")

        total_files = len(txt_files)

    return {
        "total_files": total_files,
        "total_links": total_links,
        "total_added": total_added,
        "by_category": stats,
    }
