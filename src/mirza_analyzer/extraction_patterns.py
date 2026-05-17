from __future__ import annotations

import re
from dataclasses import dataclass

from .utils import compact_whitespace


STRICT_CATEGORIES = [
    "flooring",
    "wall_colors",
    "kitchens",
    "chairs",
    "tables",
    "sofas",
    "hallway",
    "living_room_furniture",
]

CONFIDENCE_SORT_VALUE = {"low": 0, "medium": 1, "high": 2}


@dataclass(frozen=True)
class PriceParse:
    value: int
    currency: str
    unit: str | None
    raw: str


@dataclass(frozen=True)
class ArticleParse:
    article_id: str
    raw: str
    vendor_raw: str | None = None
    vendor_normalized: str | None = None
    marketplace: str | None = None


@dataclass(frozen=True)
class VendorParse:
    raw: str
    normalized: str


@dataclass(frozen=True)
class PromoParse:
    code: str
    raw: str


VENDOR_ALIASES: list[tuple[str, list[str]]] = [
    (
        "Divan.ru",
        [
            r"official_divan\.ru",
            r"divan\.ru",
            r"диван\.ру",
            r"диван\s*ру",
        ],
    ),
    (
        "Mebel.in",
        [
            r"mebel\.in",
            r"mebel\s*in",
            r"мебель\s*инн?",
        ],
    ),
    ("OZON", [r"ozon(?:\.ru)?", r"озон"]),
    (
        "Wildberries",
        [
            r"wildberries(?:\.ru)?",
            r"wb",
            r"вб",
        ],
    ),
    (
        "Yandex Market",
        [
            r"яндекс[.\s-]*маркет",
            r"yandex\s*market",
            r"ям",
        ],
    ),
    ("Лемана Про", [r"лемана\s*про"]),
    ("Леруа Мерлен", [r"леруа\s*мерлен", r"леруа(?!\s*мерлен)"]),
    ("Сантехника Онлайн", [r"сантехника\s*онлайн", r"santehnika_online"]),
    ("VERESK", [r"veresk(?:_mebel)?", r"вереск"]),
    ("HOFF", [r"hoff(?:_rus)?", r"хофф"]),
    ("Moon", [r"moon(?:\s*trade)?", r"муун"]),
]

MARKETPLACE_NORMALIZED = {"OZON", "Wildberries", "Yandex Market"}

ITEM_START_RE = re.compile(
    r"(?i)\b("
    r"кухн|фасад|фартук|столешниц|стол|диван|софа|кресл|стул|"
    r"плитк|керамогранит|кварцвинил|spc|ламинат|паркет|"
    r"прихож|обувниц|зеркал|вешал|пуф|банкетк|консол|"
    r"тв\s*тумб|тумб|комод|стеллаж|полк|цвет\s+стен"
    r")\b"
)

PRICE_RE = re.compile(
    r"(?<![\w])"
    r"(?P<amount>\d{1,3}(?:[ .]\d{3})+|\d{3,6})"
    r"\s*"
    r"(?P<currency>рублей|рубля|руб\.?|₽|р\.?)"
    r"(?:\s*/\s*(?P<unit>шт\.?|м2|м²|пара|уп\.?))?",
    re.IGNORECASE,
)

ARTICLE_RE = re.compile(
    r"(?i)"
    r"(?:(?P<vendor>ozon|озон|wb|вб|wildberries|яндекс[.\s-]*маркет|ям|"
    r"сантехника\s+онлайн)\s*)?"
    r"\b(?:арт\.?|артикул|код(?:\s+товара)?\s*:?)"
    r"\s*\.?\s*"
    r"(?P<article>[A-Za-zА-Яа-яЁё0-9_-]{3,})"
)

PROMO_RE = re.compile(r"(?i)\bпромокод\s+(?P<code>[A-Za-zА-Яа-яЁё0-9_-]+)")

COLOR_CODE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?i)\bRAL\s*\d{3,4}\b"),
    re.compile(r"(?i)\bG\d{3}\b"),
    re.compile(r"(?i)\b\d{2}\s*(?:YY|YR|GY)\s*\d{2}/\d{3}\b"),
    re.compile(r"(?i)\bNCS\s*[A-Z]?\s*\d{4}[A-Z\s-]*\b"),
]

BRAND_RE = re.compile(r"(?i)\b(Tikkurila|Dulux|V33)\b")

MATERIAL_WORDS = {
    "velvet",
    "вельвет",
    "bucle",
    "букле",
    "рогожка",
    "велюр",
    "экокожа",
    "кожа",
    "шенилл",
}

COLOR_WORDS = {
    "emerald",
    "olive",
    "white",
    "grey",
    "gray",
    "green",
    "red",
    "blue",
    "black",
    "beige",
    "зеленый",
    "зелёный",
    "оливковый",
    "белый",
    "серый",
    "синий",
    "красный",
    "черный",
    "чёрный",
    "бежевый",
    "коричневый",
    "кирпичный",
}


def normalize_for_match(value: str) -> str:
    return compact_whitespace(value).casefold().replace("ё", "е")


def normalize_vendor(raw: str | None) -> str | None:
    if raw is None:
        return None
    cleaned = compact_whitespace(raw.strip().strip("@"))
    if not cleaned:
        return None
    lowered = normalize_for_match(cleaned)
    for normalized, aliases in VENDOR_ALIASES:
        for alias in aliases:
            if re.fullmatch(alias, lowered, re.IGNORECASE):
                return normalized
    for normalized, aliases in VENDOR_ALIASES:
        for alias in aliases:
            if re.search(rf"(?<![\w.]){alias}(?![\w.])", lowered, re.IGNORECASE):
                return normalized
    return cleaned


def find_vendor(text: str) -> VendorParse | None:
    lowered = normalize_for_match(text)
    matches: list[tuple[int, str, str]] = []
    for normalized, aliases in VENDOR_ALIASES:
        for alias in aliases:
            match = re.search(rf"(?<![\w.]){alias}(?![\w.])", lowered, re.IGNORECASE)
            if match:
                raw = text[match.start() : match.end()]
                matches.append((match.start(), raw, normalized))
                break
    if not matches:
        return None
    _, raw, normalized = sorted(matches, key=lambda item: item[0])[0]
    return VendorParse(raw=compact_whitespace(raw), normalized=normalized)


def parse_price(text: str) -> PriceParse | None:
    match = PRICE_RE.search(text)
    if not match:
        return None
    amount = int(re.sub(r"[ .]", "", match.group("amount")))
    unit = normalize_price_unit(match.group("unit"))
    tail = text[match.end() : match.end() + 20]
    if unit is None and re.search(r"(?i)\bза\s+пару\b", tail):
        unit = "пара"
    return PriceParse(
        value=amount,
        currency="RUB",
        unit=unit,
        raw=compact_whitespace(match.group(0)),
    )


def normalize_price_unit(unit: str | None) -> str | None:
    if not unit:
        return None
    cleaned = unit.strip().strip(".").casefold().replace("²", "2")
    if cleaned in {"шт", "м2", "пара", "уп"}:
        return cleaned
    return cleaned or None


def parse_article_id(text: str) -> ArticleParse | None:
    match = ARTICLE_RE.search(text)
    if not match:
        return None
    vendor_raw = match.group("vendor")
    vendor = normalize_vendor(vendor_raw) if vendor_raw else find_vendor(text)
    if isinstance(vendor, VendorParse):
        vendor_raw = vendor.raw
        vendor_normalized = vendor.normalized
    else:
        vendor_normalized = vendor
    marketplace = vendor_normalized if vendor_normalized in MARKETPLACE_NORMALIZED else None
    return ArticleParse(
        article_id=match.group("article"),
        raw=compact_whitespace(match.group(0)),
        vendor_raw=compact_whitespace(vendor_raw) if vendor_raw else None,
        vendor_normalized=vendor_normalized,
        marketplace=marketplace,
    )


def parse_promo_code(text: str) -> PromoParse | None:
    match = PROMO_RE.search(text)
    if not match:
        return None
    return PromoParse(code=match.group("code"), raw=compact_whitespace(match.group(0)))


def parse_project_name(text: str) -> str | None:
    lines = [compact_whitespace(line) for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines[:8]):
        lowered = normalize_for_match(line)
        if "артикулы проекта" not in lowered:
            continue
        after = re.sub(r"(?i).*?артикулы\s+проекта", "", line).strip(" :-")
        if after:
            name = trim_project_name(after)
            if name:
                return name
        if index + 1 < len(lines):
            name = trim_project_name(lines[index + 1])
            if name:
                return name

    first_chunk = "\n".join(lines[:8])
    match = re.search(r"(?i)\bЖК\s+[A-Za-zА-Яа-яЁё0-9 .,-]{3,80}", first_chunk)
    if match:
        return trim_project_name(match.group(0))
    return None


def trim_project_name(value: str) -> str | None:
    cleaned = compact_whitespace(value.strip(" -:;,."))
    if not cleaned:
        return None
    cleaned = re.split(
        r"(?i)\b(кухня|фартук|стол|диван|цвет\s+стен|покупки|арт\.|wb|ozon)\b",
        cleaned,
        maxsplit=1,
    )[0].strip(" -:;,.")
    if not cleaned:
        return None
    if re.match(r"^\d+[.)]?\s", cleaned):
        return None
    if ITEM_START_RE.search(cleaned) and not cleaned.casefold().startswith("жк "):
        return None
    if len(cleaned) > 90:
        cleaned = cleaned[:90].rstrip(" -:;,.")
    if len(cleaned) < 3:
        return None
    return cleaned


def extract_color_codes(text: str) -> list[str]:
    codes: list[str] = []
    for pattern in COLOR_CODE_PATTERNS:
        for match in pattern.finditer(text):
            code = compact_whitespace(match.group(0).upper())
            if code not in codes:
                codes.append(code)
    return codes


def detect_brand(text: str) -> tuple[str | None, str | None]:
    match = BRAND_RE.search(text)
    if not match:
        return None, None
    raw = match.group(1)
    normalized = raw[0].upper() + raw[1:].lower()
    if normalized.casefold() == "v33":
        normalized = "V33"
    return raw, normalized


def remove_price_and_article(text: str) -> str:
    cleaned = PRICE_RE.sub(" ", text)
    cleaned = ARTICLE_RE.sub(" ", cleaned)
    cleaned = PROMO_RE.sub(" ", cleaned)
    return compact_whitespace(cleaned)


def without_promos(text: str) -> str:
    lines = [
        line
        for line in text.splitlines()
        if not re.search(r"(?i)\bпромо(?:код)?\b|mirzabaeva", line)
    ]
    return compact_whitespace(" ".join(lines))


def suspiciously_long_descriptor(value: str | None, *, max_words: int = 8) -> bool:
    if not value:
        return False
    return len(re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", value)) > max_words


def count_item_triggers(text: str) -> int:
    return len(ITEM_START_RE.findall(text))
