# -*- coding: utf-8 -*-
"""
Нормализация названия страны (введённого по-русски, в разных вариантах)
в ISO 3166-1 alpha-2 код — тот же формат, что LZT.market отдаёт в поле
telegram_country ("RU", "US", "IN", ...). Используется для фильтра по
стране при покупке аккаунтов (accounts/lzt_buyer.py).

LZT.market не фильтрует по стране на своей стороне (проверено эмпирически —
параметры country[]/telegram_country[]/country игнорируются или не дают
ожидаемого результата), поэтому фильтрация — на нашей стороне, по полю
telegram_country уже полученных объявлений.
"""

# ISO-2 → варианты написания по-русски (и по-английски), без учёта регистра.
# Один код — много синонимов: официальное название, разговорное, аббревиатура.
COUNTRY_ALIASES: dict[str, list[str]] = {
    "RU": ["россия", "рф", "russia", "ru", "рос"],
    "UA": ["украина", "ukraine", "ua", "укр"],
    "BY": ["беларусь", "белоруссия", "belarus", "by"],
    "KZ": ["казахстан", "kazakhstan", "kz"],
    "UZ": ["узбекистан", "uzbekistan", "uz"],
    "KG": ["киргизия", "кыргызстан", "kyrgyzstan", "kg"],
    "TJ": ["таджикистан", "tajikistan", "tj"],
    "TM": ["туркменистан", "turkmenistan", "tm"],
    "AZ": ["азербайджан", "azerbaijan", "az"],
    "AM": ["армения", "armenia", "am"],
    "GE": ["грузия", "georgia", "ge"],
    "MD": ["молдова", "молдавия", "moldova", "md"],
    "LV": ["латвия", "latvia", "lv"],
    "LT": ["литва", "lithuania", "lt"],
    "EE": ["эстония", "estonia", "ee"],
    "US": ["сша", "usa", "us", "америка", "штаты",
           "соединенные штаты", "соединённые штаты",
           "соединенные штаты америки", "соединённые штаты америки"],
    "GB": ["великобритания", "англия", "британия", "соединенное королевство",
           "соединённое королевство", "uk", "united kingdom", "britain", "gb"],
    "DE": ["германия", "germany", "de", "фрг"],
    "FR": ["франция", "france", "fr"],
    "IT": ["италия", "italy", "it"],
    "ES": ["испания", "spain", "es"],
    "PL": ["польша", "poland", "pl"],
    "PT": ["португалия", "portugal", "pt"],
    "NL": ["нидерланды", "голландия", "netherlands", "holland", "nl"],
    "BE": ["бельгия", "belgium", "be"],
    "AT": ["австрия", "austria", "at"],
    "CH": ["швейцария", "switzerland", "ch"],
    "SE": ["швеция", "sweden", "se"],
    "NO": ["норвегия", "norway", "no"],
    "FI": ["финляндия", "finland", "fi"],
    "DK": ["дания", "denmark", "dk"],
    "IE": ["ирландия", "ireland", "ie"],
    "GR": ["греция", "greece", "gr"],
    "RO": ["румыния", "romania", "ro"],
    "BG": ["болгария", "bulgaria", "bg"],
    "CZ": ["чехия", "czech", "czechia", "cz"],
    "SK": ["словакия", "slovakia", "sk"],
    "HU": ["венгрия", "hungary", "hu"],
    "HR": ["хорватия", "croatia", "hr"],
    "RS": ["сербия", "serbia", "rs"],
    "TR": ["турция", "turkey", "turkiye", "tr"],
    "IN": ["индия", "india", "in"],
    "ID": ["индонезия", "indonesia", "id"],
    "PK": ["пакистан", "pakistan", "pk"],
    "BD": ["бангладеш", "bangladesh", "bd"],
    "VN": ["вьетнам", "vietnam", "vn"],
    "PH": ["филиппины", "philippines", "ph"],
    "MM": ["мьянма", "бирма", "myanmar", "burma", "mm"],
    "TH": ["таиланд", "thailand", "th"],
    "MY": ["малайзия", "malaysia", "my"],
    "SG": ["сингапур", "singapore", "sg"],
    "KH": ["камбоджа", "cambodia", "kh"],
    "LA": ["лаос", "laos", "la"],
    "NP": ["непал", "nepal", "np"],
    "LK": ["шри-ланка", "шриланка", "sri lanka", "lk"],
    "CN": ["китай", "china", "cn"],
    "JP": ["япония", "japan", "jp"],
    "KR": ["корея", "южная корея", "south korea", "korea", "kr"],
    "NG": ["нигерия", "nigeria", "ng"],
    "KE": ["кения", "kenya", "ke"],
    "GH": ["гана", "ghana", "gh"],
    "ZA": ["юар", "южная африка", "south africa", "za"],
    "EG": ["египет", "egypt", "eg"],
    "MA": ["марокко", "morocco", "ma"],
    "DZ": ["алжир", "algeria", "dz"],
    "TN": ["тунис", "tunisia", "tn"],
    "BR": ["бразилия", "brazil", "br"],
    "MX": ["мексика", "mexico", "mx"],
    "AR": ["аргентина", "argentina", "ar"],
    "CO": ["колумбия", "colombia", "co"],
    "PE": ["перу", "peru", "pe"],
    "CL": ["чили", "chile", "cl"],
    "VE": ["венесуэла", "venezuela", "ve"],
    "EC": ["эквадор", "ecuador", "ec"],
    "IR": ["иран", "iran", "ir"],
    "IQ": ["ирак", "iraq", "iq"],
    "SA": ["саудовская аравия", "saudi arabia", "sa"],
    "AE": ["оаэ", "эмираты", "объединенные арабские эмираты",
           "объединённые арабские эмираты", "uae", "united arab emirates", "ae"],
    "IL": ["израиль", "israel", "il"],
    "CA": ["канада", "canada", "ca"],
    "AU": ["австралия", "australia", "au"],
}

# {синоним_в_нижнем_регистре: ISO-2} — построено один раз при импорте.
_LOOKUP: dict[str, str] = {
    alias.lower(): code
    for code, aliases in COUNTRY_ALIASES.items()
    for alias in (aliases + [code])
}


def normalize_country_query(text: str) -> str | None:
    """
    "США" / "сша" / "Америка" / "us" / "USA" → "US". None, если не распознано.
    """
    key = text.strip().lower().replace("ё", "е")
    return _LOOKUP.get(key)


# Названия-аббревиатуры — .capitalize() их портит ("сша" -> "Сша"), выводим как есть.
_ACRONYM_LABELS = {"US": "США", "AE": "ОАЭ"}


def country_label(code: str) -> str:
    """ISO-2 → человекочитаемое имя для вывода."""
    if code in _ACRONYM_LABELS:
        return _ACRONYM_LABELS[code]
    aliases = COUNTRY_ALIASES.get(code)
    return aliases[0].capitalize() if aliases else code
