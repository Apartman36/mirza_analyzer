from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import pytest

import mirza_analyzer.father_queries as father_queries
from mirza_analyzer.father_queries import (
    FurnitureMakerMention,
    ProjectLink,
    SourceEntity,
    WallPaintMention,
    attribute_nearby_urls,
    extract_appliance_mentions,
    extract_furniture_maker_mentions,
    extract_message_entities,
    extract_wall_paint_mentions,
    link_message_to_project,
    normalize_shade_family,
)
from mirza_analyzer.kitchen_palette_report import CanonicalMessage


CHANNEL = "olya_homestaging"


FACTS_SCHEMA = """
CREATE TABLE extracted_facts (
    id INTEGER PRIMARY KEY,
    source_message_id INTEGER NOT NULL,
    date TEXT,
    source_scope TEXT,
    project_name TEXT,
    category TEXT NOT NULL,
    item_type TEXT NOT NULL,
    item_name TEXT,
    vendor_raw TEXT,
    vendor_normalized TEXT,
    brand_raw TEXT,
    brand_normalized TEXT,
    model TEXT,
    material TEXT,
    finish TEXT,
    color TEXT,
    color_code TEXT,
    article_id TEXT,
    marketplace TEXT,
    price_value REAL,
    price_currency TEXT,
    price_unit TEXT,
    promo_code TEXT,
    room_context TEXT,
    evidence_quote TEXT NOT NULL,
    extraction_method TEXT,
    confidence TEXT NOT NULL,
    needs_review INTEGER NOT NULL,
    notes TEXT,
    source_text_hash TEXT,
    created_at TEXT,
    first_photo_path TEXT
);
"""


CANONICAL_SCHEMA = """
CREATE TABLE canonical_messages (
    telegram_message_id INTEGER PRIMARY KEY,
    date TEXT,
    text_plain TEXT NOT NULL,
    text_entities_json TEXT,
    raw_best_json TEXT NOT NULL,
    has_real_photo INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE message_variants (
    id INTEGER PRIMARY KEY,
    telegram_message_id INTEGER NOT NULL,
    raw_json TEXT NOT NULL
);

CREATE TABLE media (
    id INTEGER PRIMARY KEY,
    telegram_message_id INTEGER NOT NULL,
    media_kind TEXT NOT NULL,
    absolute_path TEXT NOT NULL
);
"""


def _message(
    message_id: int,
    text: str,
    *,
    entities: list[dict] | None = None,
    raw_best: dict | None = None,
) -> CanonicalMessage:
    return CanonicalMessage(
        message_id=message_id,
        date="2026-05-01T12:00:00",
        text_plain=text,
        text_entities_json=(
            json.dumps(entities, ensure_ascii=False) if entities is not None else None
        ),
        raw_best_json=json.dumps(raw_best or {}, ensure_ascii=False),
    )


def _entity(text: str, visible_text: str, href: str, message_id: int = 1) -> SourceEntity:
    start = text.index(visible_text)
    return SourceEntity(
        source_message_id=message_id,
        visible_text=visible_text,
        href=href,
        entity_type="text_link",
        start=start,
        end=start + len(visible_text),
        context=text,
        source_priority="synthetic_fixture",
    )


def _maker_mention(
    message_id: int,
    *,
    name: str = "Mebel.in",
    classification: str = "confirmed_maker",
    person_name: str | None = None,
    phone: str | None = None,
    telegram: str | None = None,
    whatsapp: str | None = None,
    instagram: str | None = None,
    website: str | None = None,
    what_was_made: str | None = None,
    evidence_quote: str = "Мебель на заказ",
) -> FurnitureMakerMention:
    return FurnitureMakerMention(
        source_message_id=message_id,
        maker_name_raw=name,
        maker_name_normalized=name,
        classification=classification,
        person_name=person_name,
        phone=phone,
        telegram=telegram,
        whatsapp=whatsapp,
        instagram=instagram,
        website=website,
        what_was_made=what_was_made,
        telegram_post_url=f"https://t.me/{CHANNEL}/{message_id}",
        evidence_quote=evidence_quote,
        confidence="high",
    )


def _insert_fact(conn: sqlite3.Connection, **overrides: object) -> None:
    base: dict[str, object] = {
        "source_message_id": 10,
        "date": "2026-05-01T12:00:00",
        "source_scope": "project_articles",
        "project_name": "ЖК Тестовый",
        "category": "kitchens",
        "item_type": "kitchen_facades",
        "item_name": None,
        "vendor_raw": None,
        "vendor_normalized": "Mebel.in",
        "brand_raw": None,
        "brand_normalized": None,
        "model": None,
        "material": None,
        "finish": "Дуб каселла + капучино",
        "color": None,
        "color_code": None,
        "article_id": None,
        "marketplace": None,
        "price_value": None,
        "price_currency": "₽",
        "price_unit": None,
        "promo_code": None,
        "room_context": "кухня-гостиная",
        "evidence_quote": "Кухня: дуб каселла + капучино, Mebel.in",
        "extraction_method": "regex",
        "confidence": "high",
        "needs_review": 0,
        "notes": None,
        "source_text_hash": "fixture",
        "created_at": "2026-05-01T12:00:00",
        "first_photo_path": None,
    }
    base.update(overrides)
    columns = ", ".join(base)
    placeholders = ", ".join("?" for _ in base)
    conn.execute(
        f"INSERT INTO extracted_facts ({columns}) VALUES ({placeholders})",
        tuple(base.values()),
    )


def _stage6_fixture(tmp_path: Path) -> tuple[Path, Path]:
    facts_db = tmp_path / "facts.sqlite"
    canonical_db = tmp_path / "canonical.sqlite"

    with sqlite3.connect(facts_db) as conn:
        conn.executescript(FACTS_SCHEMA)
        _insert_fact(conn, id=1)
        _insert_fact(
            conn,
            id=2,
            category="wall_colors",
            item_type="wall_color",
            vendor_raw=None,
            vendor_normalized=None,
            finish=None,
            color_code="G482",
            evidence_quote="Цвет стен — теплый бежевый G482",
        )
        conn.commit()

    project_href = f"https://t.me/{CHANNEL}/20"
    oven_href = "https://www.dns-shop.ru/product/9110581/dexp-1ylo45sb/"
    maker_href = "https://t.me/mebel_inn"
    messages = [
        {
            "id": 10,
            "text": (
                "Артикулы проекта\nЖК Тестовый\n"
                "Кухня: дуб каселла + капучино. Цвет стен — теплый бежевый G482"
            ),
            "entities": [
                {"type": "text_link", "text": "Артикулы проекта", "href": project_href}
            ],
        },
        {
            "id": 20,
            "text": "ЖК Тестовый\nДизайнер Анна\nСтены: теплый бежевый G482",
            "entities": [],
        },
        {
            "id": 2778,
            "text": "Духовой шкаф DEXP 1YLO45SB, артикул 9110581 — DNS",
            "entities": [
                {"type": "text_link", "text": "DNS", "href": oven_href}
            ],
        },
        {
            "id": 3000,
            "text": "Кухню и встроенные шкафы на заказ изготовила Mebel.in — Telegram",
            "entities": [
                {"type": "text_link", "text": "Telegram", "href": maker_href}
            ],
        },
    ]
    with sqlite3.connect(canonical_db) as conn:
        conn.executescript(CANONICAL_SCHEMA)
        for row in messages:
            conn.execute(
                """
                INSERT INTO canonical_messages (
                    telegram_message_id, date, text_plain, text_entities_json,
                    raw_best_json, has_real_photo
                ) VALUES (?, ?, ?, ?, ?, 0)
                """,
                (
                    row["id"],
                    "2026-05-01T12:00:00",
                    row["text"],
                    json.dumps(row["entities"], ensure_ascii=False),
                    "{}",
                ),
            )
        conn.commit()
    return facts_db, canonical_db


def test_entity_reader_uses_canonical_then_raw_best_then_variant() -> None:
    text = "Духовой шкаф — DNS"
    canonical = _message(
        1,
        text,
        entities=[
            {
                "type": "text_link",
                "text": "DNS",
                "href": "https://dns-shop.ru/product/canonical-product",
            }
        ],
        raw_best={
            "text_entities": [
                {
                    "type": "text_link",
                    "text": "DNS",
                    "href": "https://example.test/raw-best-must-not-win",
                }
            ]
        },
    )
    result = extract_message_entities(
        canonical,
        variant_raw_jsons=(
            json.dumps(
                {
                    "text_entities": [
                        {
                            "type": "text_link",
                            "text": "DNS",
                            "href": "https://example.test/variant-must-not-win",
                        }
                    ]
                },
                ensure_ascii=False,
            ),
        ),
    )
    assert len(result) == 1
    assert result[0].href.endswith("canonical-product")
    assert result[0].visible_text == "DNS"
    assert result[0].entity_type == "text_link"
    assert result[0].source_message_id == 1
    assert "Духовой шкаф" in result[0].context
    assert result[0].source_priority == "canonical_messages.text_entities_json"

    raw_best = _message(
        2,
        text,
        raw_best={
            "text_entities": [
                {
                    "type": "text_link",
                    "text": "DNS",
                    "href": "https://dns-shop.ru/product/raw-best-product",
                }
            ]
        },
    )
    assert extract_message_entities(raw_best)[0].source_priority == "canonical_messages.raw_best_json"

    variant_only = _message(3, text)
    variants = (
        json.dumps(
            {
                "text_entities": [
                    {
                        "type": "text_link",
                        "text": "DNS",
                        "href": "https://dns-shop.ru/product/variant-product",
                    }
                ]
            },
            ensure_ascii=False,
        ),
    )
    assert extract_message_entities(variant_only, variants)[0].source_priority == "message_variants.raw_json"


def test_url_attribution_is_limited_to_nearby_product_block() -> None:
    text = (
        "Духовой шкаф DEXP 1YLO45SB — DNS\n\n"
        "Диван для гостиной — Divan.ru"
    )
    dns = _entity(text, "DNS", "https://dns-shop.ru/product/dexp-1ylo45sb")
    divan = _entity(text, "Divan.ru", "https://divan.ru/product/sofa")
    start = text.index("DEXP")
    attributed = attribute_nearby_urls(text, [dns, divan], start, start + len("DEXP 1YLO45SB"))

    assert [item.href for item in attributed] == [dns.href]


@pytest.mark.parametrize(
    ("text", "expected_code", "expected_manufacturer"),
    [
        ("Цвет стен G482", "G482", None),
        ("Краска для стен K493", "K493", None),
        ("Цвет стен L490", "L490", None),
        ("Краска на стенах F497", "F497", None),
        ("Стены H486", "H486", None),
        ("Оттенок стен S431", "S431", None),
        ("Цвет стен NCS S 5030-Y60R", "S 5030-Y60R", None),
        ("Краска для стен RAL 9001", "RAL 9001", None),
        ("Цвет стен Dulux 30YY 69/048", "30YY 69/048", "Dulux"),
        ("Краска для стен Little Greene 236", "236", "Little Greene"),
    ],
)
def test_paint_code_parsing(
    text: str,
    expected_code: str,
    expected_manufacturer: str | None,
) -> None:
    mentions = extract_wall_paint_mentions(_message(100, text))

    assert len(mentions) == 1
    assert isinstance(mentions[0], WallPaintMention)
    assert mentions[0].color_code_normalized == expected_code
    assert mentions[0].manufacturer == expected_manufacturer
    assert mentions[0].source_message_id == 100
    assert text in mentions[0].evidence_quote


def test_paint_manufacturer_and_product_are_scoped_to_local_sentence() -> None:
    message = _message(
        101,
        (
            "Стены: Tikkurila Luja Extra, цвет G482. "
            "Терракот — 50YR 21/318 Dulux. "
            "Серо-зеленый — 45 GY 55/052 Dulux."
        ),
    )
    mentions = extract_wall_paint_mentions(message)
    by_code = {mention.color_code_normalized: mention for mention in mentions}

    assert by_code["G482"].manufacturer == "Tikkurila"
    assert by_code["G482"].product_line == "Luja Extra"
    assert by_code["50YR 21/318"].manufacturer == "Dulux"
    assert by_code["50YR 21/318"].product_line is None
    assert by_code["45GY 55/052"].manufacturer == "Dulux"
    assert all(
        mention.manufacturer != "Tikkurila"
        for code, mention in by_code.items()
        if code in {"50YR 21/318", "45GY 55/052"}
    )


def test_paint_codes_on_telegram_lines_without_terminal_punctuation_are_kept() -> None:
    message = _message(
        102,
        "Артикулы проекта\nЖК Тестовый\n\nЦвет стен G482 и H482\n\nСледующий блок",
    )

    mentions = extract_wall_paint_mentions(message)

    assert {mention.color_code_normalized for mention in mentions} == {"G482", "H482"}


def test_each_code_uses_nearest_manufacturer_within_one_sentence() -> None:
    message = _message(
        103,
        "Стены: Tikkurila G482, терракот 50YR 21/318 Dulux.",
    )

    by_code = {
        mention.color_code_normalized: mention
        for mention in extract_wall_paint_mentions(message)
    }

    assert by_code["G482"].manufacturer == "Tikkurila"
    assert by_code["50YR 21/318"].manufacturer == "Dulux"


@pytest.mark.parametrize(
    ("text", "expected_family", "expected_method"),
    [
        ("теплый бежевый цвет стен", "warm_beige", "explicit_text"),
        ("серо-бежевый грейдж", "greige", "explicit_text"),
        ("молочный теплый белый", "warm_white_milk", "explicit_text"),
        ("холодный белый", "cool_white", "explicit_text"),
        ("светло-серый", "light_gray", "explicit_text"),
        ("серый", "gray", "explicit_text"),
        ("оливково-зеленый", "green", "explicit_text"),
        ("голубой", "blue", "explicit_text"),
        ("терракотовый", "terracotta", "explicit_text"),
        ("коричневый", "brown", "explicit_text"),
        ("G482", "other", "unknown"),
    ],
)
def test_shade_family_normalization(
    text: str,
    expected_family: str,
    expected_method: str,
) -> None:
    family, description, method = normalize_shade_family(text)

    assert family == expected_family
    assert method == expected_method
    if expected_method == "unknown":
        assert description is None
    else:
        assert description


def test_project_link_explicit_link_wins_over_stage4_mapping() -> None:
    text = "Артикулы проекта\nЖК Явный"
    message = _message(
        200,
        text,
        entities=[
            {
                "type": "text_link",
                "text": "Артикулы проекта",
                "href": f"https://t.me/{CHANNEL}/555",
            }
        ],
    )
    link = link_message_to_project(
        message,
        channel_username=CHANNEL,
        stage4_source_to_project={200: 999},
        project_name="ЖК Явный",
    )

    assert isinstance(link, ProjectLink)
    assert link.project_post_id == 555
    assert link.project_key == "telegram:555"
    assert link.link_method == "explicit_project_link"
    assert link.link_confidence == "high"


def test_stage4_collection_link_does_not_merge_distinct_project_post() -> None:
    message = _message(
        5901,
        "Артикулы проекта\nЖК Второй Нагатинский\nПосмотреть подборку других евро3",
        entities=[
            {
                "type": "text_link",
                "text": "Посмотреть подборку других евро3",
                "href": f"https://t.me/{CHANNEL}/4496",
            }
        ],
    )

    link = link_message_to_project(
        message,
        channel_username=CHANNEL,
        stage4_source_to_project={5901: 4496},
        project_name="ЖК Второй Нагатинский",
    )

    assert link.project_post_id == 5901
    assert link.project_key == "telegram:5901"
    assert link.link_method == "same_message"
    assert link.link_confidence == "high"


def test_same_residential_complex_name_alone_does_not_merge_projects() -> None:
    first = link_message_to_project(
        _message(301, "ЖК Второй Нагатинский\nПроект квартиры"),
        channel_username=CHANNEL,
        project_name="ЖК Второй Нагатинский",
    )
    second = link_message_to_project(
        _message(302, "ЖК Второй Нагатинский\nДругой проект квартиры"),
        channel_username=CHANNEL,
        project_name="ЖК Второй Нагатинский",
    )

    assert first.link_method == second.link_method == "same_message"
    assert first.project_key != second.project_key
    assert {first.project_post_id, second.project_post_id} == {301, 302}


def test_historical_project_name_alone_is_not_high_confidence_linkage() -> None:
    link = link_message_to_project(
        _message(303, "Цвет стен G482"),
        channel_username=CHANNEL,
        project_name="Панели под покраску",
    )

    assert link.project_key is None
    assert link.project_name is None
    assert link.link_method == "ambiguous_metadata_match"
    assert link.link_confidence == "low"


def test_exact_paint_ranking_counts_unique_high_confidence_projects() -> None:
    rank_exact_paints = getattr(father_queries, "rank_exact_paints")
    mentions: list[WallPaintMention] = []
    for message_id, project_post_id in ((401, 9001), (402, 9001), (403, 9002)):
        project_link = ProjectLink(
            source_message_id=message_id,
            project_post_id=project_post_id,
            project_key=f"telegram:{project_post_id}",
            project_name=f"Проект {project_post_id}",
            object_name=None,
            street=None,
            designer=None,
            link_method="explicit_project_link",
            link_confidence="high",
            evidence="synthetic explicit link",
        )
        mentions.extend(
            extract_wall_paint_mentions(
                _message(message_id, "Цвет стен G482"),
                project_link=project_link,
            )
        )

    rankings = rank_exact_paints(mentions, raw_fact_counts={"G482": 92})

    assert rankings[0].color_code == "G482"
    assert rankings[0].unique_projects == 2
    assert rankings[0].unique_messages == 3
    assert rankings[0].raw_fact_mentions == 92


def test_appliance_direct_product_link_extracts_exact_dexp_evidence() -> None:
    text = "Духовой шкаф DEXP 1YLO45SB, артикул 9110581 — DNS"
    href = "https://www.dns-shop.ru/product/9110581/dexp-1ylo45sb/"
    message = _message(
        2778,
        text,
        entities=[{"type": "text_link", "text": "DNS", "href": href}],
    )
    entities = extract_message_entities(message)
    mentions = extract_appliance_mentions(message, entities, None, CHANNEL)

    assert len(mentions) == 1
    mention = mentions[0]
    assert mention.appliance_type == "oven"
    assert mention.brand == "DEXP"
    assert mention.model == "1YLO45SB"
    assert mention.article_id == "9110581"
    assert mention.merchant == "DNS"
    assert mention.merchant_domain == "dns-shop.ru"
    assert mention.product_url == href
    assert mention.evidence_class == "direct_product_link"
    assert mention.confidence == "high"


def test_appliance_real_style_dexp_block_keeps_adjacent_article_line() -> None:
    text = "Духовка на 45 ДНС\nАрт. 9110581  23 999₽"
    href = (
        "https://www.dns-shop.ru/product/fecc1bfa325efb65/"
        "elektriceskij-duhovoj-skaf-dexp-1ylo45sb-cernyj/"
    )
    message = _message(
        2778,
        text,
        entities=[{"type": "text_link", "text": "ДНС", "href": href}],
    )

    mentions = extract_appliance_mentions(
        message,
        extract_message_entities(message),
        None,
        CHANNEL,
    )

    assert len(mentions) == 1
    assert mentions[0].brand == "DEXP"
    assert mentions[0].model == "1YLO45SB"
    assert mentions[0].article_id == "9110581"
    assert mentions[0].merchant == "DNS"
    assert mentions[0].product_url == href


def test_ordinary_refrigerator_is_explicitly_unconfirmed_not_built_in() -> None:
    message = _message(499, "Холодильник DEXP от ДНС установлен.")

    mentions = extract_appliance_mentions(message, [], None, CHANNEL)

    assert len(mentions) == 1
    assert mentions[0].appliance_type == "refrigerator_unconfirmed"
    assert mentions[0].evidence_class == "merchant_text_only"
    assert mentions[0].notes == "Обычное упоминание холодильника; встраивание не подтверждено."
    markdown = father_queries._build_appliance_markdown(mentions)
    assert "холодильник (встраивание не подтверждено)" in markdown
    assert "| встроенный холодильник |" not in markdown


@pytest.mark.parametrize(
    ("text", "expected_type", "expected_class"),
    [
        (
            "Встроенный холодильник самостоятельно куплен заказчиками, модель не указана.",
            "built_in_refrigerator",
            "customer_supplied",
        ),
        (
            "Часть техники приобретена заказчиком: варочная панель и ПММ.",
            "cooktop",
            "customer_supplied",
        ),
        (
            "Совет: встроенную СВЧ лучше размещать на удобной высоте.",
            "microwave",
            "general_advice",
        ),
    ],
)
def test_appliance_non_purchase_evidence_classes(
    text: str,
    expected_type: str,
    expected_class: str,
) -> None:
    message = _message(500, text, entities=[])
    mentions = extract_appliance_mentions(message, [], None, CHANNEL)

    assert len(mentions) == 1
    assert mentions[0].appliance_type == expected_type
    assert mentions[0].evidence_class == expected_class
    assert mentions[0].product_url is None


def test_appliance_does_not_attach_irrelevant_furniture_url() -> None:
    text = "Варочная панель установлена.\n\nДиван для гостиной — Divan.ru"
    message = _message(
        501,
        text,
        entities=[
            {
                "type": "text_link",
                "text": "Divan.ru",
                "href": "https://divan.ru/product/sofa-not-an-appliance",
            }
        ],
    )
    mentions = extract_appliance_mentions(
        message,
        extract_message_entities(message),
        None,
        CHANNEL,
    )

    assert len(mentions) == 1
    assert mentions[0].appliance_type == "cooktop"
    assert mentions[0].evidence_class == "mentioned_no_source"
    assert mentions[0].product_url is None
    assert mentions[0].merchant is None


def test_appliance_attribution_does_not_cross_sentence_in_same_paragraph() -> None:
    text = "Варочная панель установлена. Диван для гостиной — Divan.ru"
    message = _message(
        502,
        text,
        entities=[
            {
                "type": "text_link",
                "text": "Divan.ru",
                "href": "https://divan.ru/product/sofa-not-an-appliance",
            }
        ],
    )

    mentions = extract_appliance_mentions(
        message,
        extract_message_entities(message),
        None,
        CHANNEL,
    )

    assert len(mentions) == 1
    assert mentions[0].product_url is None
    assert mentions[0].merchant is None
    assert mentions[0].evidence_quote == "Варочная панель установлена."


def test_appliance_report_renders_unidentified_direct_links_and_dedupes_merchant_messages() -> None:
    text = "Духовой шкаф — DNS.\nВарочная панель — DNS."
    first_href = "https://www.dns-shop.ru/product/aaaaaaaaaaaa/generic-item-one/"
    second_href = "https://www.dns-shop.ru/product/bbbbbbbbbbbb/generic-item-two/"
    message = _message(
        503,
        text,
        entities=[
            {"type": "text_link", "text": "DNS", "href": first_href},
            {"type": "text_link", "text": "DNS", "href": second_href},
        ],
    )
    mentions = extract_appliance_mentions(
        message,
        extract_message_entities(message),
        None,
        CHANNEL,
    )

    assert len(mentions) == 2
    assert all(item.evidence_class == "direct_product_link" for item in mentions)
    assert all(item.model is None and item.article_id is None for item in mentions)
    markdown = father_queries._build_appliance_markdown(mentions)
    assert first_href in markdown
    assert second_href in markdown
    assert "в 0 из них явно определена модель или артикул" in markdown
    assert "DNS: 1 сообщение" in markdown


def test_appliance_weak_table_shows_fields_notes_and_centered_quote() -> None:
    prefix = "Предыстория " * 40
    message = _message(504, f"{prefix}холодильник DEXP от ДНС установлен.")
    mentions = extract_appliance_mentions(message, [], None, CHANNEL)

    markdown = father_queries._build_appliance_markdown(mentions)

    assert "Бренд / модель" in markdown
    assert "Артикул" in markdown
    assert "Магазин" in markdown
    assert "Оговорка" in markdown
    assert "холодильник DEXP от ДНС" in markdown
    assert "Обычное упоминание холодильника; встраивание не подтверждено." in markdown
    assert ("Предыстория " * 15).strip() not in markdown


def test_confirmed_maker_extracts_local_contacts_and_work() -> None:
    text = (
        "Кухню и встроенные шкафы на заказ изготовила Mebel.in. "
        "Контакт Анна: Telegram, WhatsApp, Instagram, сайт"
    )
    links = {
        "Telegram": "https://t.me/mebel_inn",
        "WhatsApp": "https://wa.me/79991234567",
        "Instagram": "https://instagram.com/mebel.in",
        "сайт": "https://mebel.in/custom-kitchens",
    }
    message = _message(
        600,
        text,
        entities=[
            {"type": "text_link", "text": anchor, "href": href}
            for anchor, href in links.items()
        ],
    )
    mentions = extract_furniture_maker_mentions(
        message,
        extract_message_entities(message),
        None,
        CHANNEL,
    )

    mebel = next(item for item in mentions if item.maker_name_normalized == "Mebel.in")
    assert mebel.classification == "confirmed_maker"
    assert mebel.person_name == "Анна"
    assert mebel.phone == "+79991234567"
    assert mebel.telegram == links["Telegram"]
    assert mebel.whatsapp == links["WhatsApp"]
    assert mebel.instagram == links["Instagram"]
    assert mebel.website == links["сайт"]
    assert "кухня" in (mebel.what_was_made or "")
    assert "шкафы" in (mebel.what_was_made or "")


def test_maker_does_not_inherit_unrelated_specialist_contact_on_next_line() -> None:
    text = "Кухня Mebel.in\nШторы Textelli — WhatsApp"
    message = _message(
        602,
        text,
        entities=[
            {"type": "text_link", "text": "Mebel.in", "href": "https://t.me/+79990000001"},
            {"type": "text_link", "text": "WhatsApp", "href": "https://wa.me/79990000002"},
        ],
    )

    mentions = extract_furniture_maker_mentions(
        message,
        extract_message_entities(message),
        None,
        CHANNEL,
    )

    mebel = next(item for item in mentions if item.maker_name_normalized == "Mebel.in")
    assert mebel.telegram == "https://t.me/+79990000001"
    assert mebel.phone == "+79990000001"
    assert mebel.whatsapp is None


@pytest.mark.parametrize(
    ("text", "expected_name", "expected_class"),
    [
        ("Встроенные шкафы на заказ изготовил VERESK.", "VERESK", "confirmed_maker"),
        ("Диван для гостиной купили в Divan.ru.", "Divan.ru", "retailer"),
        ("Шкаф для прихожей купили в HOFF.", "HOFF", "retailer"),
        ("Полки и мебель купили в Лемана Про.", "Лемана Про", "retailer"),
        (
            "Мебель на заказ изготовлена, но производитель в посте не указан.",
            "unnamed_custom_maker",
            "ambiguous",
        ),
    ],
)
def test_furniture_maker_classification(
    text: str,
    expected_name: str,
    expected_class: str,
) -> None:
    message = _message(601, text, entities=[])
    mentions = extract_furniture_maker_mentions(message, [], None, CHANNEL)

    assert any(
        item.maker_name_normalized == expected_name and item.classification == expected_class
        for item in mentions
    )
    assert not any(
        item.classification == "confirmed_maker"
        for item in mentions
        if item.maker_name_normalized in {"Divan.ru", "HOFF", "Лемана Про"}
    )


def test_maker_markdown_keeps_claims_with_their_own_quote_and_post() -> None:
    first = _maker_mention(
        700,
        person_name="Анна",
        phone="+79991234567",
        telegram="https://t.me/mebel_inn",
        whatsapp="https://wa.me/79991234567",
        what_was_made="кухня и встроенные шкафы",
        evidence_quote="Кухню и встроенные шкафы изготовила Mebel.in, контакт Анна.",
    )
    second = _maker_mention(
        701,
        person_name="Борис",
        website="https://mebel.in/wardrobes",
        what_was_made="гардеробная",
        evidence_quote="Гардеробную изготовила Mebel.in, контакт Борис.",
    )

    report = father_queries._build_maker_markdown([first, second], {})
    first_start = report.index("[пост 700]")
    second_start = report.index("[пост 701]")
    first_block = report[first_start:second_start]
    second_block = report[second_start:]

    assert "кухня и встроенные шкафы" in first_block
    assert "Контактное лицо: Анна" in first_block
    assert "https://t.me/mebel_inn" in first_block
    assert "https://wa.me/79991234567" in first_block
    assert first.evidence_quote in first_block
    assert first.telegram_post_url in first_block
    assert "Борис" not in first_block

    assert "гардеробная" in second_block
    assert "Контактное лицо: Борис" in second_block
    assert "https://mebel.in/wardrobes" in second_block
    assert second.evidence_quote in second_block
    assert second.telegram_post_url in second_block


def test_maker_markdown_cites_retailer_evidence_and_keeps_limit_notes() -> None:
    maker = _maker_mention(
        710,
        what_was_made="кухня",
        evidence_quote="Кухню на заказ изготовила Mebel.in.",
    )
    retailer = _maker_mention(
        900,
        name="Divan.ru",
        classification="retailer",
        instagram="https://instagram.com/unrelated_designer",
        what_was_made="диван для гостиной",
        evidence_quote="Диван для гостиной купили в Divan.ru.",
    )

    report = father_queries._build_maker_markdown([maker, retailer], {})

    assert "## Магазины — не производители на заказ" in report
    assert "### Divan.ru" in report
    assert "магазин/ритейлер, не производитель мебели на заказ" in report
    assert retailer.evidence_quote in report
    assert retailer.telegram_post_url in report
    assert "unrelated_designer" not in report
    assert "Комментарии Telegram в замороженном корпусе отсутствуют" in report
    assert "не является исчерпывающим" in report


def test_candidate_telegram_url_and_initial_validation_status(tmp_path: Path) -> None:
    generate_father_query_index = getattr(father_queries, "generate_father_query_index")
    facts_db, canonical_db = _stage6_fixture(tmp_path)
    out_dir = tmp_path / "father_queries"

    generate_father_query_index(
        facts_db=facts_db,
        canonical_db=canonical_db,
        out_dir=out_dir,
        channel_username=CHANNEL,
    )

    rows = list(
        csv.DictReader(
            (out_dir / "source_link_validation.csv").open(
                encoding="utf-8-sig",
                newline="",
            )
        )
    )
    assert rows
    assert all(row["status"] == "unverified_requires_manual_verification" for row in rows)
    assert any(
        row["candidate_telegram_url"] == f"https://t.me/{CHANNEL}/2778"
        for row in rows
    )


def test_build_index_uses_synthetic_databases_and_writes_provenance(tmp_path: Path) -> None:
    build_father_query_index = getattr(father_queries, "build_father_query_index")
    generate_father_query_index = getattr(father_queries, "generate_father_query_index")
    facts_db, canonical_db = _stage6_fixture(tmp_path)
    out_dir = tmp_path / "father_queries"

    index = build_father_query_index(
        facts_db=facts_db,
        canonical_db=canonical_db,
        out_dir=out_dir,
        channel_username=CHANNEL,
    )

    assert index.wall_paint_mentions
    assert index.appliance_mentions
    assert index.furniture_maker_mentions

    result = generate_father_query_index(
        facts_db=facts_db,
        canonical_db=canonical_db,
        out_dir=out_dir,
        channel_username=CHANNEL,
    )
    assert result.index.wall_paint_mentions
    assert (out_dir / "father_queries.sqlite").exists()
    assert (out_dir / "project_linkage_review.csv").exists()
    assert (out_dir / "source_link_validation.csv").exists()

    with sqlite3.connect(out_dir / "father_queries.sqlite") as conn:
        table_names = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    conn.close()
    assert {
        "project_links",
        "wall_paint_mentions",
        "appliance_mentions",
        "furniture_maker_mentions",
    } <= table_names

    first_db_bytes = (out_dir / "father_queries.sqlite").read_bytes()
    second_out_dir = tmp_path / "father_queries_second"
    generate_father_query_index(
        facts_db=facts_db,
        canonical_db=canonical_db,
        out_dir=second_out_dir,
        channel_username=CHANNEL,
    )
    assert (second_out_dir / "father_queries.sqlite").read_bytes() == first_db_bytes


def test_wall_reports_keep_claim_level_paint_provenance(tmp_path: Path) -> None:
    generate_father_wall_paints = getattr(father_queries, "generate_father_wall_paints")
    facts_db, canonical_db = _stage6_fixture(tmp_path)
    out_dir = tmp_path / "father_queries"

    generate_father_wall_paints(
        facts_db=facts_db,
        canonical_db=canonical_db,
        out_dir=out_dir,
        channel_username=CHANNEL,
        kitchen_palette_dir=tmp_path / "missing_stage4_outputs",
    )

    rows = list(
        csv.DictReader(
            (out_dir / "kitchen_wall_color_projects.csv").open(
                encoding="utf-8-sig",
                newline="",
            )
        )
    )
    assert rows
    claims = json.loads(rows[0]["wall_paint_evidence_json"])
    assert claims
    assert all(
        {
            "source_message_id",
            "manufacturer",
            "product_line",
            "color_code",
            "evidence_quote",
            "telegram_post_url",
            "link_method",
            "link_confidence",
        }
        <= claim.keys()
        for claim in claims
    )
    assert any(
        claim["color_code"] == "G482"
        and claim["source_message_id"] == 10
        and "G482" in claim["evidence_quote"]
        for claim in claims
    )

    kitchen_report = (out_dir / "kitchen_wall_colors.md").read_text(encoding="utf-8")
    ranking_report = (out_dir / "wall_paints_top3.md").read_text(encoding="utf-8")
    assert "каждый код связан со своим локальным источником" in kitchen_report
    assert f"https://t.me/{CHANNEL}/10" in kitchen_report
    assert "ключей проекта/проектного поста" in ranking_report
    assert "Примеры доказательств для точных кодов" in ranking_report
    assert f"https://t.me/{CHANNEL}/10" in ranking_report


@pytest.mark.parametrize(
    ("command", "expected_files"),
    [
        (
            "father-query-index",
            {
                "father_queries.sqlite",
                "project_linkage_review.csv",
                "source_link_validation.csv",
            },
        ),
        (
            "father-wall-paints",
            {
                "kitchen_wall_colors.md",
                "kitchen_wall_color_projects.csv",
                "wall_paints_top3.md",
                "wall_paints_all.csv",
            },
        ),
        (
            "father-appliances",
            {"built_in_appliances.md", "built_in_appliances.csv"},
        ),
        (
            "father-furniture-makers",
            {"furniture_makers.md", "furniture_makers.csv"},
        ),
    ],
)
def test_independent_father_query_cli_commands(
    tmp_path: Path,
    command: str,
    expected_files: set[str],
) -> None:
    # Import lazily so missing CLI wiring does not hide focused extractor failures.
    from mirza_analyzer.cli import main

    facts_db, canonical_db = _stage6_fixture(tmp_path)
    out_dir = tmp_path / command

    code = main(
        [
            command,
            "--facts-db",
            str(facts_db),
            "--canonical-db",
            str(canonical_db),
            "--out-dir",
            str(out_dir),
            "--channel-username",
            CHANNEL,
        ]
    )

    assert code == 0
    assert expected_files <= {path.name for path in out_dir.iterdir()}
