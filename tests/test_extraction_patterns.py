import pytest

from mirza_analyzer.extraction import extract_facts_from_text
from mirza_analyzer.extraction_patterns import (
    normalize_vendor,
    parse_article_id,
    parse_price,
    parse_project_name,
)


def facts_by_category(text: str, category: str):
    return [
        fact
        for fact in extract_facts_from_text(
            text=text,
            source_message_id=7299,
            date="2026-05-02T11:19:01",
            source_scope="project_articles",
        )
        if fact.category == category
    ]


@pytest.mark.parametrize(
    ("raw", "value", "unit"),
    [
        ("270 000₽", 270000, None),
        ("270 000 руб.", 270000, None),
        ("14 157₽", 14157, None),
        ("6200 руб./шт.", 6200, "шт"),
        ("2590₽/м2", 2590, "м2"),
        ("33 724 ₽", 33724, None),
        ("7 431₽/шт.", 7431, "шт"),
        ("12 054 руб./пара", 12054, "пара"),
    ],
)
def test_parse_price_russian_formats(raw: str, value: int, unit: str | None) -> None:
    parsed = parse_price(raw)

    assert parsed is not None
    assert parsed.value == value
    assert parsed.currency == "RUB"
    assert parsed.unit == unit


@pytest.mark.parametrize(
    ("raw", "article_id", "vendor", "marketplace"),
    [
        ("OZON Арт. 1550292417", "1550292417", "OZON", "OZON"),
        ("WB Арт. 498029528", "498029528", "Wildberries", "Wildberries"),
        ("ЯМ Арт. 102727930434", "102727930434", "Yandex Market", "Yandex Market"),
        ("Код: 549902", "549902", None, None),
        ("Арт.1179015456", "1179015456", None, None),
        ("Сантехника онлайн Арт. 621512", "621512", "Сантехника Онлайн", None),
    ],
)
def test_parse_article_ids(
    raw: str,
    article_id: str,
    vendor: str | None,
    marketplace: str | None,
) -> None:
    parsed = parse_article_id(raw)

    assert parsed is not None
    assert parsed.article_id == article_id
    assert parsed.vendor_normalized == vendor
    assert parsed.marketplace == marketplace


def test_promo_code_is_not_article_id() -> None:
    assert parse_article_id("✨промокод MIRZABAEVA5 -2%") is None


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("Divan.ru", "Divan.ru"),
        ("Диван ру", "Divan.ru"),
        ("official_divan.ru", "Divan.ru"),
        ("Mebel in", "Mebel.in"),
        ("мебель инн", "Mebel.in"),
        ("Озон", "OZON"),
        ("ВБ", "Wildberries"),
        ("Яндекс маркет", "Yandex Market"),
        ("Лемана Про", "Лемана Про"),
        ("Леруа Мерлен", "Леруа Мерлен"),
        ("Сантехника онлайн", "Сантехника Онлайн"),
        ("veresk_mebel", "VERESK"),
        ("Хофф", "HOFF"),
    ],
)
def test_vendor_normalization(raw: str, normalized: str) -> None:
    assert normalize_vendor(raw) == normalized


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Артикулы проекта ЖК Люблинский парк Кухня фасады Дуб", "ЖК Люблинский парк"),
        ("Артикулы проекта\nЖК Кольская 8\n\nСтол OZON", "ЖК Кольская 8"),
        ("Артикулы проекта Матвеевский парк\n\nДиван Divan.ru", "Матвеевский парк"),
        ("ЖК Второй Нагатинский\n\nПокупки:", "ЖК Второй Нагатинский"),
        ("ЖК Астра Марин, Санкт-Петербург\n\nПокупки:", "ЖК Астра Марин, Санкт-Петербург"),
    ],
)
def test_project_name_parser(text: str, expected: str) -> None:
    assert parse_project_name(text) == expected


def test_wall_color_extraction_codes_and_brand() -> None:
    facts = facts_by_category(
        "Цвет стен G482\nЦвет стен акцентный 30YY 56/060\nTikkurila Symphony RAL 7047",
        "wall_colors",
    )

    assert {fact.color_code for fact in facts} >= {"G482", "30YY 56/060", "RAL 7047"}
    assert any(fact.item_type == "wall_color_accent" for fact in facts)
    assert any(fact.brand_normalized == "Tikkurila" for fact in facts)


def test_sofa_extraction_model_material_color_price() -> None:
    facts = facts_by_category("Диван Divan.ru\nСлипсон Вельвет Зеленый 64 990₽", "sofas")

    assert len(facts) == 1
    fact = facts[0]
    assert fact.item_type == "sofa"
    assert fact.vendor_normalized == "Divan.ru"
    assert fact.model == "Слипсон"
    assert fact.material == "Вельвет"
    assert fact.color == "Зеленый"
    assert fact.price_value == 64990


def test_sofa_extraction_does_not_treat_divan_ru_table_as_sofa() -> None:
    facts = facts_by_category("Журнальный столик Divan.ru Лимм Древесный 14 990₽", "sofas")

    assert facts == []


def test_kitchen_facade_extraction_finish_vendor_price() -> None:
    facts = facts_by_category(
        "Кухня фасады Дуб каселла натуральный+ Капучино гринвуд Mebel.in\n270 000₽",
        "kitchens",
    )

    assert len(facts) == 1
    fact = facts[0]
    assert fact.item_type == "kitchen_facades"
    assert fact.vendor_normalized == "Mebel.in"
    assert fact.finish == "Дуб каселла натуральный+ Капучино гринвуд"
    assert fact.price_value == 270000


def test_flooring_disambiguation_backsplash_is_not_flooring() -> None:
    assert facts_by_category("Плитка на фартук Kerama Marazzi 2590₽/м2", "flooring") == []


def test_flooring_matches_floor_tile_context() -> None:
    facts = facts_by_category("Плитка на полу Kerama Marazzi 2590₽/м2", "flooring")

    assert len(facts) == 1
    assert facts[0].item_type == "flooring_tile"


def test_flooring_bathroom_context_needs_review() -> None:
    facts = facts_by_category("Плитка в ванной на полу Kerama Marazzi 2590₽/м2", "flooring")

    assert len(facts) == 1
    assert facts[0].room_context == "bathroom"
    assert facts[0].needs_review


def test_hallway_extraction() -> None:
    facts = facts_by_category("Зеркало в прихожей WB арт. 129109108 7431₽/шт.", "hallway")

    assert len(facts) == 1
    assert facts[0].item_type == "mirror"
    assert facts[0].vendor_normalized == "Wildberries"
    assert facts[0].article_id == "129109108"


def test_living_room_tv_unit_extraction() -> None:
    facts = facts_by_category("Тв тумба OZON\nАрт. 1639148968 15 147₽", "living_room_furniture")

    assert len(facts) == 1
    assert facts[0].item_type == "tv_unit"
    assert facts[0].vendor_normalized == "OZON"
    assert facts[0].article_id == "1639148968"
