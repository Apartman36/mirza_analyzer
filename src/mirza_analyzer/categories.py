from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CategoryConfig:
    category_id: str
    display_name: str
    description: str
    strong_keywords: list[str]
    weak_keywords: list[str]
    regex_patterns: list[str]
    negative_patterns: list[str]
    scoring_hints: dict[str, Any]


DEFAULT_SCORING_HINTS: dict[str, Any] = {
    "strong_keyword": 3,
    "regex": 4,
    "weak_keyword": 1,
    "project_article_bonus": 2,
    "photo_bonus": 1,
    "negative_pattern": -2,
}


CATEGORY_CONFIGS: list[dict[str, Any]] = [
    {
        "category_id": "flooring",
        "display_name": "Полы",
        "description": (
            "Кандидаты про напольные покрытия: ламинат, кварцвинил, плитка, "
            "паркет, инженерная доска, бренды и места покупки, если они явно указаны."
        ),
        "strong_keywords": [
            "ламинат",
            "кварцвинил",
            "кварц винил",
            "кварц-винил",
            "керамогранит",
            "паркет",
            "инженерная доска",
            "напольное покрытие",
            "плитка на пол",
            "плитка на полу",
            "плитка для пола",
            "пол покрасили",
            "покрытие пола",
        ],
        "weak_keywords": [
            "пол",
            "плитка",
        ],
        "regex_patterns": [
            r"плитк[а-яе\s-]{0,30}(пол|полу|пола|наполь)",
            r"(пол|полу|пола)[а-яе\s-]{0,30}плитк",
            r"напольн[а-яе\s-]{0,30}(покрыт|материал)",
            r"\bкварц[\s-]?винил[а-яе]*\b",
        ],
        "negative_patterns": [
            r"\bполк[а-яе]*\b",
            r"\bполоч[а-яе]*\b",
            r"\bполотенц[а-яе]*\b",
            r"\bполезн[а-яе]*\b",
            r"\bполовин[а-яе]*\b",
            r"\bполу\b",
        ],
        "scoring_hints": {
            **DEFAULT_SCORING_HINTS,
            "category_note": "Weak 'пол' alone is suppressed unless supported by stronger flooring context.",
        },
    },
    {
        "category_id": "wall_colors",
        "display_name": "Цвета стен",
        "description": (
            "Кандидаты про цвета стен, краски, выкрасы и явные цветовые коды "
            "RAL/NCS/Tikkurila/Dulux/V33."
        ),
        "strong_keywords": [
            "цвет стен",
            "стены цвет",
            "краска",
            "оттенок краски",
            "выкрас",
            "ral",
            "ncs",
            "tikkurila",
            "dulux",
            "v33",
            "g482",
            "g486",
            "ral 7047",
            "ral 9016",
            "30yy",
            "01yr",
            "12 gy",
        ],
        "weak_keywords": [
            "стены",
            "бежевые стены",
            "серые стены",
            "зеленый оттенок",
        ],
        "regex_patterns": [
            r"\bral\s*\d{3,4}\b",
            r"\bncs\s*[a-z]?\s*\d{4}[a-z\s-]*\b",
            r"\b\d{2}\s*(yy|yr|gy)\b",
            r"\bg\d{3}\b",
        ],
        "negative_patterns": [],
        "scoring_hints": DEFAULT_SCORING_HINTS,
    },
    {
        "category_id": "kitchens",
        "display_name": "Кухни",
        "description": (
            "Кандидаты про кухни: производители, кухонные гарнитуры, фасады, "
            "сочетания фасадов, цвета и декоры."
        ),
        "strong_keywords": [
            "кухня",
            "кухни",
            "кухонный гарнитур",
            "фасад",
            "фасады",
            "столешница",
            "фартук",
            "мебель ин",
            "mebel.in",
            "mebel in",
            "мебель inn",
            "кухни москвы",
            "леруа",
            "леруа мерлен",
            "лемана",
            "hoff",
            "obi",
            "оби",
            "стильные кухни",
            "дуб каселла",
            "дуб мадуро",
            "сантьяго",
            "ньюпорт",
        ],
        "weak_keywords": [
            "кухн",
        ],
        "regex_patterns": [
            r"\bкухн[а-яе]*\b",
            r"\bфасад[а-яе]*\b",
        ],
        "negative_patterns": [],
        "scoring_hints": DEFAULT_SCORING_HINTS,
    },
    {
        "category_id": "chairs",
        "display_name": "Стулья",
        "description": (
            "Кандидаты про стулья: источники, продавцы, цвет и материал, если они явно указаны."
        ),
        "strong_keywords": [
            "стулья",
            "стул",
            "обеденные стулья",
            "кухонные стулья",
            "stoolgroup",
            "lifemebel",
            "лайфмебель",
            "annihaus",
            "ogogo",
            "velosso",
            "sk design",
        ],
        "weak_keywords": [
            "кресло",
            "кресла",
        ],
        "regex_patterns": [
            r"\bстул[а-яе]*\b",
            r"\bстуль[а-яе]*\b",
        ],
        "negative_patterns": [],
        "scoring_hints": DEFAULT_SCORING_HINTS,
    },
    {
        "category_id": "tables",
        "display_name": "Столы",
        "description": (
            "Кандидаты про столы: источники, форма, цвет, материал, подстолье и столешницы."
        ),
        "strong_keywords": [
            "стол",
            "столы",
            "обеденный стол",
            "кухонный стол",
            "круглый стол",
            "журнальный столик",
            "рабочий стол",
            "подстолье",
            "столешница",
        ],
        "weak_keywords": [
            "столик",
        ],
        "regex_patterns": [
            r"\bстол(ы|а|у|ом|е)?\b",
            r"\bстолик[а-яе]*\b",
            r"\bподстоль[а-яе]*\b",
        ],
        "negative_patterns": [],
        "scoring_hints": {
            **DEFAULT_SCORING_HINTS,
            "category_note": (
                "A lone 'столешница' inside a kitchen context is kept for review "
                "but capped to low confidence."
            ),
        },
    },
    {
        "category_id": "sofas",
        "display_name": "Диваны",
        "description": (
            "Кандидаты про диваны: источники, цвет, материал и ткани вроде рогожки, велюра, букле."
        ),
        "strong_keywords": [
            "диван",
            "диваны",
            "divan.ru",
            "диван.ру",
            "moon",
            "муун",
            "8 марта",
            "рогожка",
            "велюр",
            "букле",
            "velvet",
            "bucle",
            "soft grey",
            "velvet emerald",
            "velvet olive",
        ],
        "weak_keywords": [
            "софа",
        ],
        "regex_patterns": [
            r"\bдиван[а-яе]*\b",
            r"\bdivan\.ru\b",
            r"\bvelvet\s+(emerald|olive)\b",
        ],
        "negative_patterns": [],
        "scoring_hints": DEFAULT_SCORING_HINTS,
    },
    {
        "category_id": "hallway",
        "display_name": "Прихожая",
        "description": (
            "Кандидаты про прихожие: мебель, цвета, материалы, зеркала, обувницы, пуфы, "
            "банкетки и продавцы."
        ),
        "strong_keywords": [
            "прихожая",
            "в прихожей",
            "коридор",
            "шкаф в прихожей",
            "обувница",
            "пуф",
            "банкетка",
            "зеркало в прихожей",
            "консоль в прихожей",
            "входная группа",
        ],
        "weak_keywords": [
            "шкаф",
            "зеркало",
        ],
        "regex_patterns": [
            r"\bприхож[а-яе]*\b",
            r"\bкоридор[а-яе]*\b",
            r"\bвходн[а-яе\s-]{0,20}групп[а-яе]*\b",
        ],
        "negative_patterns": [],
        "scoring_hints": {
            **DEFAULT_SCORING_HINTS,
            "category_note": "Weak 'шкаф' or 'зеркало' alone is broad and capped to low confidence.",
        },
    },
    {
        "category_id": "living_room_furniture",
        "display_name": "Мебель для гостиной",
        "description": (
            "Кандидаты про мебель для гостиной: ТВ-тумбы, шкафы, консоли, полки, "
            "стеллажи, комоды и стенки."
        ),
        "strong_keywords": [
            "гостиная",
            "тв тумба",
            "тв-тумба",
            "тумба под тв",
            "телевизор",
            "комод",
            "стеллаж",
            "полки",
            "гостиная мебель",
            "стенка гостиная",
            "журнальный столик",
        ],
        "weak_keywords": [
            "тумба",
            "шкаф",
            "полка",
        ],
        "regex_patterns": [
            r"\bтв[\s-]*тумб[а-яе]*\b",
            r"\bтумб[а-яе\s-]{0,20}(тв|телевизор)",
            r"\bгостин[а-яе]*\b",
        ],
        "negative_patterns": [],
        "scoring_hints": {
            **DEFAULT_SCORING_HINTS,
            "category_note": "Generic weak furniture words alone remain low confidence.",
        },
    },
]


PROJECT_ARTICLE_TERMS: list[str] = [
    "Артикулы проекта",
    "Покупки:",
    "Основные покупки",
    "Купить все артикулы",
    "Проект со всеми ссылками",
    "Проект с большинством ссылок",
    "промокод MIRZABAEVA",
    "OZON Арт.",
    "WB Арт.",
    "ВБ арт.",
    "ЯМ Арт.",
    "Divan.ru",
    "Диван.ру",
    "mebel.in",
    "мебель ин",
    "VERESK",
    "Леруа Мерлен",
    "Лемана Про",
    "Сантехника онлайн",
]


PROJECT_ARTICLE_REGEXES: list[str] = [
    r"\bozon\s*арт\.?",
    r"\bwb\s*арт\.?",
    r"\bвб\s*арт\.?",
    r"\bям\s*арт\.?",
]


def load_category_configs() -> list[CategoryConfig]:
    return [CategoryConfig(**config) for config in CATEGORY_CONFIGS]

