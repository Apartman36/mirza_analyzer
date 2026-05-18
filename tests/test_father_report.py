from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mirza_analyzer.cli import main
from mirza_analyzer.father_report import (
    CATEGORY_TITLES,
    DISPLAY_ITEM_TYPE_RU,
    FATHER_CATEGORIES,
    ReportOptions,
    build_father_report,
    build_report_dataset,
    clean_display_value,
    count_display_values,
    count_values,
    display_category_ru,
    display_item_type_ru,
    is_display_value_useful,
    load_llm_reviews,
    load_source_facts,
    normalize_wall_color_code,
)


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


REVIEW_SCHEMA = """
CREATE TABLE llm_review_results (
    id INTEGER PRIMARY KEY,
    fact_id INTEGER NOT NULL,
    source_message_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    original_category TEXT NOT NULL,
    original_item_type TEXT NOT NULL,
    original_needs_review INTEGER NOT NULL,
    decision TEXT NOT NULL,
    category_correct INTEGER NOT NULL,
    item_type_correct INTEGER NOT NULL,
    price_correct INTEGER,
    is_bundle INTEGER NOT NULL,
    is_context_false_positive INTEGER NOT NULL,
    is_non_target_room INTEGER NOT NULL,
    corrected_json TEXT NOT NULL,
    normalized_terms_json TEXT NOT NULL,
    rationale_short TEXT NOT NULL,
    confidence TEXT NOT NULL,
    raw_response TEXT,
    error TEXT,
    created_at TEXT NOT NULL
);
"""


def _insert_fact(conn: sqlite3.Connection, **overrides) -> int:
    base = {
        "source_message_id": 1000,
        "date": "2026-01-01T00:00:00",
        "source_scope": "project_articles",
        "project_name": None,
        "category": "kitchens",
        "item_type": "kitchen_facades",
        "item_name": None,
        "vendor_raw": None,
        "vendor_normalized": None,
        "brand_raw": None,
        "brand_normalized": None,
        "model": None,
        "material": None,
        "finish": None,
        "color": None,
        "color_code": None,
        "article_id": None,
        "marketplace": None,
        "price_value": None,
        "price_currency": "₽",
        "price_unit": None,
        "promo_code": None,
        "room_context": None,
        "evidence_quote": "evidence",
        "extraction_method": "regex",
        "confidence": "medium",
        "needs_review": 0,
        "notes": None,
        "source_text_hash": "hash",
        "created_at": "2026-01-01T00:00:00",
        "first_photo_path": None,
    }
    base.update(overrides)
    columns = ", ".join(base)
    placeholders = ", ".join("?" for _ in base)
    cursor = conn.execute(
        f"INSERT INTO extracted_facts ({columns}) VALUES ({placeholders})",
        tuple(base.values()),
    )
    return int(cursor.lastrowid)


def _insert_review(conn: sqlite3.Connection, **overrides) -> None:
    base = {
        "fact_id": 1,
        "source_message_id": 1000,
        "provider": "mock",
        "model": "mock",
        "input_hash": "input",
        "prompt_hash": "prompt",
        "original_category": "kitchens",
        "original_item_type": "kitchen_facades",
        "original_needs_review": 0,
        "decision": "keep",
        "category_correct": 1,
        "item_type_correct": 1,
        "price_correct": None,
        "is_bundle": 0,
        "is_context_false_positive": 0,
        "is_non_target_room": 0,
        "corrected_json": json.dumps({}),
        "normalized_terms_json": json.dumps({"facade_materials": []}),
        "rationale_short": "ok",
        "confidence": "high",
        "raw_response": None,
        "error": None,
        "created_at": "2026-01-01T00:00:00",
    }
    base.update(overrides)
    columns = ", ".join(base)
    placeholders = ", ".join("?" for _ in base)
    conn.execute(
        f"INSERT INTO llm_review_results ({columns}) VALUES ({placeholders})",
        tuple(base.values()),
    )


def _make_facts_db(path: Path, rows: list[dict]) -> Path:
    with sqlite3.connect(path) as conn:
        conn.executescript(FACTS_SCHEMA)
        for row in rows:
            _insert_fact(conn, **row)
        conn.commit()
    return path


def _make_review_db(path: Path, rows: list[dict]) -> Path:
    with sqlite3.connect(path) as conn:
        conn.executescript(REVIEW_SCHEMA)
        for row in rows:
            _insert_review(conn, **row)
        conn.commit()
    return path


def _merge_fixture(tmp_path: Path) -> tuple[Path, Path]:
    facts_db = tmp_path / "facts.sqlite"
    review_db = tmp_path / "llm_review.sqlite"
    _make_facts_db(
        facts_db,
        [
            {
                "id": 1,
                "source_message_id": 101,
                "category": "kitchens",
                "item_type": "kitchen_facades",
                "vendor_normalized": "Mebel.in",
                "finish": "Дуб каселла+Эбони СС1106",
                "evidence_quote": "Кухня фасады Дуб каселла+Эбони СС1106 Mebel.in 330 000₽",
                "confidence": "high",
            },
            {
                "id": 2,
                "source_message_id": 102,
                "category": "sofas",
                "item_type": "sofa",
                "evidence_quote": "Диван без модели",
                "needs_review": 1,
                "confidence": "low",
            },
            {
                "id": 3,
                "source_message_id": 103,
                "category": "chairs",
                "item_type": "chair",
                "evidence_quote": "Стул OZON Арт. 123 4990₽",
                "needs_review": 1,
                "confidence": "low",
            },
            {
                "id": 4,
                "source_message_id": 104,
                "category": "living_room_furniture",
                "item_type": "coffee_table",
                "vendor_normalized": "OZON",
                "evidence_quote": "журнальный столик OZON Арт. 1876847138 5768₽",
                "confidence": "medium",
            },
            {
                "id": 5,
                "source_message_id": 105,
                "category": "sofas",
                "item_type": "sofa",
                "evidence_quote": "Недавно мне на премии Диван.ру подарили сертификат на 50 000 ₽",
                "confidence": "medium",
            },
            {
                "id": 6,
                "source_message_id": 106,
                "category": "hallway",
                "item_type": "mirror",
                "evidence_quote": "Зеркало в прихожую без артикула",
                "needs_review": 1,
                "confidence": "medium",
            },
            {
                "id": 7,
                "source_message_id": 107,
                "category": "flooring",
                "item_type": "flooring",
                "evidence_quote": "Бра на стене за диваном",
                "confidence": "medium",
            },
            {
                "id": 8,
                "source_message_id": 108,
                "category": "living_room_furniture",
                "item_type": "cabinet",
                "evidence_quote": "Люстра в гостиной",
                "confidence": "medium",
            },
            {
                "id": 9,
                "source_message_id": 109,
                "category": "tables",
                "item_type": "table",
                "vendor_normalized": "OZON",
                "price_value": 9999,
                "evidence_quote": "Стол OZON цена общая для набора 9999₽",
                "confidence": "medium",
            },
        ],
    )
    _make_review_db(
        review_db,
        [
            {
                "fact_id": 3,
                "source_message_id": 103,
                "original_category": "chairs",
                "original_item_type": "chair",
                "original_needs_review": 1,
                "decision": "keep",
                "price_correct": 1,
                "corrected_json": json.dumps({"vendor_normalized": "OZON", "article_id": "123", "price_value": 4990}),
            },
            {
                "fact_id": 4,
                "source_message_id": 104,
                "original_category": "living_room_furniture",
                "original_item_type": "coffee_table",
                "decision": "fix",
                "category_correct": 0,
                "corrected_json": json.dumps({"category": "tables", "item_type": "coffee_table"}),
                "rationale_short": "coffee table belongs to tables",
            },
            {
                "fact_id": 5,
                "source_message_id": 105,
                "original_category": "sofas",
                "original_item_type": "sofa",
                "decision": "discard",
                "category_correct": 0,
                "rationale_short": "certificate, not a sofa",
            },
            {
                "fact_id": 6,
                "source_message_id": 106,
                "original_category": "hallway",
                "original_item_type": "mirror",
                "original_needs_review": 1,
                "decision": "needs_human",
            },
            {
                "fact_id": 7,
                "source_message_id": 107,
                "original_category": "flooring",
                "original_item_type": "flooring",
                "decision": "keep",
                "category_correct": 0,
                "rationale_short": "category mismatch",
            },
            {
                "fact_id": 8,
                "source_message_id": 108,
                "original_category": "living_room_furniture",
                "original_item_type": "cabinet",
                "decision": "keep",
                "is_context_false_positive": 1,
                "rationale_short": "light fixture, not furniture",
            },
            {
                "fact_id": 9,
                "source_message_id": 109,
                "original_category": "tables",
                "original_item_type": "table",
                "decision": "keep",
                "price_correct": 0,
                "rationale_short": "price is for a bundle",
            },
        ],
    )
    return facts_db, review_db


def test_effective_fact_merging_and_llm_decisions(tmp_path: Path) -> None:
    facts_db, review_db = _merge_fixture(tmp_path)
    facts = load_source_facts(facts_db)
    reviews = load_llm_reviews([review_db])
    dataset = build_report_dataset(facts, reviews, ReportOptions(min_confidence="medium"))

    used_ids = {fact.fact_id for fact in dataset.facts}
    excluded_reasons = {item.fact_id: item.reason for item in dataset.excluded}

    assert 1 in used_ids
    assert excluded_reasons[2] == "deterministic_needs_review_without_llm"
    assert 3 in used_ids
    assert excluded_reasons[5] == "llm_discard"
    assert excluded_reasons[6] == "llm_needs_human"

    fixed = next(fact for fact in dataset.facts if fact.fact_id == 4)
    assert fixed.category == "tables"
    assert fixed.item_type == "coffee_table"
    assert dataset.applied_fix_count == 1

    price_conflict = next(fact for fact in dataset.facts if fact.fact_id == 9)
    assert price_conflict.price_value is None
    assert price_conflict.price_reliable is False


def test_consistency_override_excludes_conflicting_keep(tmp_path: Path) -> None:
    facts_db, review_db = _merge_fixture(tmp_path)
    dataset = build_report_dataset(
        load_source_facts(facts_db),
        load_llm_reviews([review_db]),
        ReportOptions(),
    )
    reasons = {item.fact_id: item.reason for item in dataset.excluded}

    assert reasons[7] in {"llm_category_conflict", "llm_rationale_conflict"}
    assert reasons[8] == "llm_context_false_positive"


def test_wall_color_aggregation_normalizes_codes(tmp_path: Path) -> None:
    facts_db = _make_facts_db(
        tmp_path / "facts.sqlite",
        [
            {
                "id": 1,
                "source_message_id": 201,
                "category": "wall_colors",
                "item_type": "wall_color",
                "color_code": "g482",
                "evidence_quote": "Цвет стен G482",
                "confidence": "high",
            },
            {
                "id": 2,
                "source_message_id": 202,
                "category": "wall_colors",
                "item_type": "wall_color",
                "color_code": "12GY 39/101",
                "evidence_quote": "Цвет стен 12GY 39/101",
                "confidence": "high",
            },
            {
                "id": 3,
                "source_message_id": 203,
                "category": "wall_colors",
                "item_type": "wall_color",
                "color_code": "12 GY 39/101",
                "evidence_quote": "Цвет стен 12 GY 39/101",
                "confidence": "high",
            },
        ],
    )
    dataset = build_report_dataset(load_source_facts(facts_db), {}, ReportOptions())
    counts = count_values(dataset.facts, lambda fact: normalize_wall_color_code(fact.color_code))

    assert counts["G482"] == 1
    assert counts["12 GY 39/101"] == 2


def test_report_generation_writes_markdown_and_excludes_discards(tmp_path: Path) -> None:
    rows = []
    for index, category in enumerate(FATHER_CATEGORIES, start=1):
        rows.append(
            {
                "id": index,
                "source_message_id": 300 + index,
                "category": category,
                "item_type": "wall_color" if category == "wall_colors" else "item",
                "vendor_normalized": "OZON" if category != "wall_colors" else None,
                "color_code": "G482" if category == "wall_colors" else None,
                "evidence_quote": f"Evidence quote for {category}",
                "confidence": "high",
            }
        )
    rows.append(
        {
            "id": 99,
            "source_message_id": 399,
            "category": "sofas",
            "item_type": "sofa",
            "evidence_quote": "DISCARD_THIS_CERTIFICATE",
            "confidence": "high",
        }
    )
    facts_db = _make_facts_db(tmp_path / "facts.sqlite", rows)
    review_db = _make_review_db(
        tmp_path / "review.sqlite",
        [
            {
                "fact_id": 99,
                "source_message_id": 399,
                "original_category": "sofas",
                "original_item_type": "sofa",
                "decision": "discard",
                "category_correct": 0,
                "rationale_short": "certificate, not sofa",
            }
        ],
    )
    out_dir = tmp_path / "father_report"

    result = build_father_report(
        facts_db=facts_db,
        llm_review_dbs=[review_db],
        out_dir=out_dir,
        canonical_db=None,
    )

    report_path = out_dir / "father_report.md"
    assert report_path.exists()
    assert (out_dir / "father_report_summary.md").exists()
    assert (out_dir / "data_quality_notes.md").exists()
    assert (out_dir / "source_facts_used.csv").exists()
    assert (out_dir / "source_facts_excluded.csv").exists()

    report = report_path.read_text(encoding="utf-8")
    assert "Шпаргалка по повторяющимся решениям" in report
    for title in (
        "Цвета стен",
        "Напольные покрытия",
        "Кухни и фасады",
        "Стулья",
        "Столы",
        "Диваны",
        "Прихожая",
        "Мебель гостиной",
    ):
        assert title in report
    assert "Evidence quote for kitchens" in report
    assert "DISCARD_THIS_CERTIFICATE" not in report
    assert len(result.dataset.facts) == len(FATHER_CATEGORIES)

    for category in FATHER_CATEGORIES:
        section_path = out_dir / "category_sections" / f"{category}.md"
        assert section_path.exists()
        section = section_path.read_text(encoding="utf-8")
        assert "Фактов в разделе" in section
        assert section.strip()


def test_cli_father_report_command_and_unsupported_format(tmp_path: Path) -> None:
    facts_db = _make_facts_db(
        tmp_path / "facts.sqlite",
        [
            {
                "id": 1,
                "source_message_id": 501,
                "category": "wall_colors",
                "item_type": "wall_color",
                "color_code": "G482",
                "evidence_quote": "Цвет стен G482",
                "confidence": "high",
            }
        ],
    )
    out_dir = tmp_path / "out"
    code = main(
        [
            "father-report",
            "--facts-db",
            str(facts_db),
            "--out-dir",
            str(out_dir),
            "--format",
            "markdown",
        ]
    )
    assert code == 0
    assert (out_dir / "father_report.md").exists()
    assert (out_dir / "data_quality_notes.md").exists()

    with pytest.raises(SystemExit):
        main(
            [
                "father-report",
                "--facts-db",
                str(facts_db),
                "--out-dir",
                str(out_dir),
                "--format",
                "html",
            ]
        )


# ---------------------------------------------------------------------------
# Stage 3.1: report-level cleanup, item-type labels, short report
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "Арт",
        "Арт.",
        "арт",
        "Артикул",
        "на пол",
        "в кабинете",
        "в гостиной",
        "на кухне",
        "в прихожей",
        "в ванной",
        "в спальне",
        "в детской",
        "для ключей",
        "рабочее",
        "кухонный",
        "2шт",
        "2 шт",
        "1876847138",
        "1 876 847 138",
        "Арт. 1876847138",
        "5768₽",
        "5768 руб",
        "5768 руб/шт",
        "Задача: продумать прихожую целиком и подобрать все элементы единым стилем.",
        "Это практически, как на заказ — выбрав нужную модель и ткань, можно сэкономить",
    ],
)
def test_is_display_value_useful_suppresses_bad_values(value: str) -> None:
    assert not is_display_value_useful(value)


@pytest.mark.parametrize(
    "value",
    [
        "G482",
        "RAL 7047",
        "30YY 72/018",
        "Дуб каселла + Капучино",
        "Velvet Light",
        "Bucle White",
        "Спейс-1",
        "Слипсон",
        "Лилле",
        "Ларсон",
        "Alpine Floor ECO 13-16 Дуб Фафнир",
    ],
)
def test_is_display_value_useful_keeps_legit_values(value: str) -> None:
    assert is_display_value_useful(value)


def test_is_display_value_useful_allows_long_facade_combination() -> None:
    long_facade = "Дуб каселла + Капучино + Эбони СС1106 + Велюр матовый + Тальк + Greige"
    assert len(long_facade) > 60
    assert is_display_value_useful(long_facade, item_type="kitchen_facades")


def test_is_display_value_useful_rejects_long_essay_even_for_facades() -> None:
    essay = (
        "Задача: продумать кухню целиком, обратите внимание на сочетание древесного декора и "
        "матового нейтрального тона; модель и ткань подбираются вместе"
    )
    assert not is_display_value_useful(essay, item_type="kitchen_facades")


def test_clean_display_value_strips_punctuation_and_whitespace() -> None:
    assert clean_display_value("  Velvet Light .  ") == "Velvet Light"
    assert clean_display_value(None) is None
    assert clean_display_value("   ") is None


def test_display_item_type_ru_maps_internal_labels() -> None:
    assert display_item_type_ru("flooring_laminate") == "ламинат / SPC / кварцвинил"
    assert display_item_type_ru("kitchen_facades") == "фасады кухни"
    assert display_item_type_ru("coffee_table") == "журнальный столик"
    assert display_item_type_ru("hallway_wardrobe") == "шкаф в прихожей"
    assert display_item_type_ru("entry_group") == "входная зона"
    assert display_item_type_ru("living_room_context_match") == "контекст гостиной"


def test_display_item_type_ru_cleans_unknown_snake_case() -> None:
    assert display_item_type_ru("some_unknown_thing") == "some unknown thing"
    assert display_item_type_ru(None) is None


def test_display_category_ru_uses_known_titles() -> None:
    assert display_category_ru("kitchens") == CATEGORY_TITLES["kitchens"]
    assert display_category_ru("flooring") == CATEGORY_TITLES["flooring"]
    assert display_category_ru(None) is None


def test_count_display_values_filters_noisy_values(tmp_path: Path) -> None:
    facts_db = _make_facts_db(
        tmp_path / "facts.sqlite",
        [
            {
                "id": 1,
                "source_message_id": 901,
                "category": "sofas",
                "item_type": "sofa",
                "model": "Спейс-1",
                "evidence_quote": "Диван Спейс-1 Divan.ru",
                "confidence": "high",
            },
            {
                "id": 2,
                "source_message_id": 902,
                "category": "sofas",
                "item_type": "sofa",
                "model": "в кабинете",
                "evidence_quote": "Диван в кабинете",
                "confidence": "high",
            },
            {
                "id": 3,
                "source_message_id": 903,
                "category": "sofas",
                "item_type": "sofa",
                "model": "Арт",
                "evidence_quote": "Арт что-то Divan.ru",
                "confidence": "high",
            },
        ],
    )
    dataset = build_report_dataset(load_source_facts(facts_db), {}, ReportOptions())
    counts = count_display_values(dataset.facts, lambda fact: fact.model)
    assert counts["Спейс-1"] == 1
    assert "в кабинете" not in counts
    assert "Арт" not in counts


def _stage_31_fixture(tmp_path: Path) -> tuple[Path, Path]:
    facts_db = tmp_path / "facts.sqlite"
    review_db = tmp_path / "llm_review.sqlite"
    _make_facts_db(
        facts_db,
        [
            {
                "id": 10,
                "source_message_id": 1010,
                "category": "wall_colors",
                "item_type": "wall_color",
                "color_code": "G482",
                "evidence_quote": "Цвет стен G482",
                "confidence": "high",
            },
            {
                "id": 11,
                "source_message_id": 1011,
                "category": "kitchens",
                "item_type": "kitchen_facades",
                "vendor_normalized": "Mebel.in",
                "finish": "Дуб каселла+Эбони СС1106",
                "evidence_quote": "Кухня фасады Дуб каселла+Эбони СС1106 Mebel.in 330 000₽",
                "confidence": "high",
            },
            {
                "id": 12,
                "source_message_id": 1012,
                "category": "kitchens",
                "item_type": "kitchen_facades",
                "finish": "Арт",
                "evidence_quote": "Кухня Арт",
                "confidence": "medium",
            },
            {
                "id": 13,
                "source_message_id": 1013,
                "category": "sofas",
                "item_type": "sofa",
                "vendor_normalized": "Divan.ru",
                "model": "Слипсон",
                "material": "Velvet Light",
                "evidence_quote": "Диван Слипсон Velvet Light Divan.ru",
                "confidence": "high",
            },
            {
                "id": 14,
                "source_message_id": 1014,
                "category": "sofas",
                "item_type": "sofa",
                "model": "в кабинете",
                "evidence_quote": "Диван в кабинете",
                "confidence": "medium",
            },
            {
                "id": 15,
                "source_message_id": 1015,
                "category": "chairs",
                "item_type": "chair",
                "vendor_normalized": "OZON",
                "model": "Арт. 123",
                "article_id": "123",
                "evidence_quote": "Стул OZON Арт. 123 4990₽",
                "confidence": "medium",
            },
            {
                "id": 16,
                "source_message_id": 1016,
                "category": "flooring",
                "item_type": "flooring_laminate",
                "brand_normalized": "Alpine Floor",
                "material": "Alpine Floor ECO 13-16 Дуб Фафнир",
                "vendor_normalized": "Лемана Про",
                "evidence_quote": "Пол Alpine Floor ECO 13-16 Дуб Фафнир Лемана Про",
                "confidence": "high",
            },
            {
                "id": 17,
                "source_message_id": 1017,
                "category": "living_room_furniture",
                "item_type": "shelves",
                "evidence_quote": "Полки в прихожей для ключей",
                "confidence": "medium",
            },
            {
                "id": 18,
                "source_message_id": 1018,
                "category": "living_room_furniture",
                "item_type": "coffee_table",
                "vendor_normalized": "OZON",
                "evidence_quote": "Журнальный столик OZON",
                "confidence": "medium",
            },
            {
                "id": 19,
                "source_message_id": 1019,
                "category": "living_room_furniture",
                "item_type": "tv_unit",
                "vendor_normalized": "Mebel.in",
                "model": "Ларсон",
                "evidence_quote": "ТВ-тумба Ларсон Mebel.in",
                "confidence": "high",
            },
            {
                "id": 20,
                "source_message_id": 1020,
                "category": "tables",
                "item_type": "coffee_table",
                "vendor_normalized": "Divan.ru",
                "model": "Лилле",
                "evidence_quote": "Журнальный столик Лилле Divan.ru",
                "confidence": "high",
            },
            {
                "id": 21,
                "source_message_id": 1021,
                "category": "hallway",
                "item_type": "mirror",
                "vendor_normalized": "VERESK",
                "evidence_quote": "Зеркало VERESK в прихожую",
                "confidence": "high",
            },
            {
                "id": 22,
                "source_message_id": 1022,
                "category": "hallway",
                "item_type": "shoe_cabinet",
                "vendor_normalized": "OZON",
                "evidence_quote": "Обувница OZON",
                "confidence": "high",
            },
        ],
    )
    _make_review_db(
        review_db,
        [
            {
                "fact_id": 18,
                "source_message_id": 1018,
                "original_category": "living_room_furniture",
                "original_item_type": "coffee_table",
                "decision": "fix",
                "category_correct": 0,
                "corrected_json": json.dumps({"category": "tables", "item_type": "coffee_table"}),
                "rationale_short": "coffee table belongs to tables",
            },
        ],
    )
    return facts_db, review_db


def test_kitchens_section_has_no_duplicate_evidence_heading(tmp_path: Path) -> None:
    facts_db, review_db = _stage_31_fixture(tmp_path)
    out_dir = tmp_path / "father_report"
    build_father_report(
        facts_db=facts_db,
        llm_review_dbs=[review_db],
        out_dir=out_dir,
        canonical_db=None,
    )

    section = (out_dir / "category_sections" / "kitchens.md").read_text(encoding="utf-8")
    assert section.count("### Примеры доказательств") == 1

    full_report = (out_dir / "father_report.md").read_text(encoding="utf-8")
    kitchens_heading = "## 3. Кухни и фасады"
    assert kitchens_heading in full_report
    kitchens_index = full_report.index(kitchens_heading)
    next_heading = "## 4."
    end_index = full_report.index(next_heading, kitchens_index)
    kitchens_block = full_report[kitchens_index:end_index]
    assert kitchens_block.count("### Примеры доказательств") == 1


def test_living_room_section_excludes_hallway_bathroom_and_coffee_tables(tmp_path: Path) -> None:
    facts_db, review_db = _stage_31_fixture(tmp_path)
    out_dir = tmp_path / "father_report"
    build_father_report(
        facts_db=facts_db,
        llm_review_dbs=[review_db],
        out_dir=out_dir,
        canonical_db=None,
    )
    section = (out_dir / "category_sections" / "living_room_furniture.md").read_text(encoding="utf-8")
    assert "в прихожей для ключей" not in section
    assert "Журнальный столик" not in section
    assert "Ларсон" in section


def test_tables_section_contains_corrected_coffee_table(tmp_path: Path) -> None:
    facts_db, review_db = _stage_31_fixture(tmp_path)
    out_dir = tmp_path / "father_report"
    build_father_report(
        facts_db=facts_db,
        llm_review_dbs=[review_db],
        out_dir=out_dir,
        canonical_db=None,
    )
    tables_section = (out_dir / "category_sections" / "tables.md").read_text(encoding="utf-8")
    assert "Лилле" in tables_section
    assert "журнальный столик" in tables_section.lower()


def test_report_uses_russian_item_type_labels(tmp_path: Path) -> None:
    facts_db, review_db = _stage_31_fixture(tmp_path)
    out_dir = tmp_path / "father_report"
    build_father_report(
        facts_db=facts_db,
        llm_review_dbs=[review_db],
        out_dir=out_dir,
        canonical_db=None,
    )
    flooring_section = (out_dir / "category_sections" / "flooring.md").read_text(encoding="utf-8")
    assert "ламинат / SPC / кварцвинил" in flooring_section
    assert "flooring_laminate" not in flooring_section


def test_short_report_is_generated_and_clean(tmp_path: Path) -> None:
    facts_db, review_db = _stage_31_fixture(tmp_path)
    out_dir = tmp_path / "father_report"
    result = build_father_report(
        facts_db=facts_db,
        llm_review_dbs=[review_db],
        out_dir=out_dir,
        canonical_db=None,
    )
    short_path = out_dir / "father_report_short.md"
    assert short_path.exists()
    assert short_path in result.output_files

    content = short_path.read_text(encoding="utf-8")
    assert "Короткая шпаргалка" in content
    for title in CATEGORY_TITLES.values():
        assert title in content, f"missing father category title in short report: {title}"
    for snake in (
        "flooring_laminate",
        "kitchen_facades",
        "coffee_table",
        "hallway_wardrobe",
        "living_room_context_match",
    ):
        assert snake not in content, f"raw snake_case leaked into short report: {snake}"
    # "в прихожей для ключей" must be fully suppressed (it was a shelves-in-hallway leak
    # into living_room examples).
    assert "в прихожей для ключей" not in content
    # "Арт. 123" must not appear as a bullet "Модели" value. It is allowed inside
    # an evidence quote per spec, so check only non-quote bullet lines starting with `- `
    # or `  - `.
    bullet_lines = [
        line
        for line in content.splitlines()
        if line.lstrip().startswith("- ") and "Модели" in line
    ]
    for line in bullet_lines:
        assert "Арт. 123" not in line, f"article id leaked into models bullet: {line}"


def test_source_facts_used_csv_preserves_underlying_rows(tmp_path: Path) -> None:
    facts_db, review_db = _stage_31_fixture(tmp_path)
    out_dir = tmp_path / "father_report"
    build_father_report(
        facts_db=facts_db,
        llm_review_dbs=[review_db],
        out_dir=out_dir,
        canonical_db=None,
    )
    csv_text = (out_dir / "source_facts_used.csv").read_text(encoding="utf-8")
    csv_rows = [line for line in csv_text.splitlines() if line and not line.startswith("fact_id,")]
    csv_fact_ids = {line.split(",", 1)[0] for line in csv_rows}
    # Fact 15 (chairs with article-only model) is suppressed in aggregates
    # but the underlying source fact must still be in the CSV.
    assert "15" in csv_fact_ids
    # Fact 14 (sofa with "в кабинете") similarly preserved.
    assert "14" in csv_fact_ids


def test_discard_and_needs_human_still_excluded_from_main_report(tmp_path: Path) -> None:
    facts_db = tmp_path / "facts.sqlite"
    review_db = tmp_path / "review.sqlite"
    _make_facts_db(
        facts_db,
        [
            {
                "id": 30,
                "source_message_id": 3000,
                "category": "sofas",
                "item_type": "sofa",
                "evidence_quote": "DISCARDED_SOFA",
                "confidence": "high",
            },
            {
                "id": 31,
                "source_message_id": 3001,
                "category": "hallway",
                "item_type": "mirror",
                "evidence_quote": "NEEDS_HUMAN_MIRROR",
                "confidence": "high",
            },
        ],
    )
    _make_review_db(
        review_db,
        [
            {
                "fact_id": 30,
                "source_message_id": 3000,
                "original_category": "sofas",
                "original_item_type": "sofa",
                "decision": "discard",
                "category_correct": 0,
                "rationale_short": "certificate",
            },
            {
                "fact_id": 31,
                "source_message_id": 3001,
                "original_category": "hallway",
                "original_item_type": "mirror",
                "decision": "needs_human",
            },
        ],
    )
    out_dir = tmp_path / "father_report"
    build_father_report(
        facts_db=facts_db,
        llm_review_dbs=[review_db],
        out_dir=out_dir,
        canonical_db=None,
    )
    full_report = (out_dir / "father_report.md").read_text(encoding="utf-8")
    short_report = (out_dir / "father_report_short.md").read_text(encoding="utf-8")
    assert "DISCARDED_SOFA" not in full_report
    assert "DISCARDED_SOFA" not in short_report
    assert "NEEDS_HUMAN_MIRROR" not in full_report
    assert "NEEDS_HUMAN_MIRROR" not in short_report


def test_summary_and_quality_notes_mention_stage_31(tmp_path: Path) -> None:
    facts_db, review_db = _stage_31_fixture(tmp_path)
    out_dir = tmp_path / "father_report"
    build_father_report(
        facts_db=facts_db,
        llm_review_dbs=[review_db],
        out_dir=out_dir,
        canonical_db=None,
    )
    summary = (out_dir / "father_report_summary.md").read_text(encoding="utf-8")
    quality = (out_dir / "data_quality_notes.md").read_text(encoding="utf-8")
    assert "father_report_short.md" in summary
    assert "Display values suppressed" in summary
    assert "Stage 3.1" in quality
    assert "source_facts_used.csv" in quality


def test_display_item_type_table_is_exposed() -> None:
    # Sanity: the mapping itself is the public contract for Russian display labels.
    assert "flooring_laminate" in DISPLAY_ITEM_TYPE_RU
    assert DISPLAY_ITEM_TYPE_RU["coffee_table"] == "журнальный столик"
