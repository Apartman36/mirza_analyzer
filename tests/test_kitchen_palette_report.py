from __future__ import annotations

import csv
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from mirza_analyzer.cli import main
from mirza_analyzer.kitchen_palette_report import (
    DEFAULT_DESIGNER,
    build_kitchen_palette_report,
    classify_palette,
    create_contact_sheet,
    extract_designer,
    extract_object_name,
    is_clean_kitchen_palette_value,
    load_kitchen_facts,
    resolve_project_post_id,
    KitchenProject,
    CanonicalMessage,
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


CANONICAL_SCHEMA = """
CREATE TABLE canonical_messages (
    telegram_message_id INTEGER PRIMARY KEY,
    date TEXT,
    text_plain TEXT NOT NULL,
    text_entities_json TEXT,
    raw_best_json TEXT,
    has_real_photo INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE media (
    id INTEGER PRIMARY KEY,
    telegram_message_id INTEGER NOT NULL,
    media_kind TEXT NOT NULL,
    absolute_path TEXT NOT NULL
);
"""


def _insert_fact(conn: sqlite3.Connection, **overrides) -> int:
    base = {
        "source_message_id": 100,
        "date": "2025-01-01T10:00:00",
        "source_scope": "project_articles",
        "project_name": None,
        "category": "kitchens",
        "item_type": "kitchen_facades",
        "item_name": None,
        "vendor_raw": None,
        "vendor_normalized": "Mebel.in",
        "brand_raw": None,
        "brand_normalized": None,
        "model": None,
        "material": None,
        "finish": "Дуб Каселла + Капучино",
        "color": None,
        "color_code": None,
        "article_id": None,
        "marketplace": None,
        "price_value": None,
        "price_currency": "₽",
        "price_unit": None,
        "promo_code": None,
        "room_context": None,
        "evidence_quote": "Кухня фасады Дуб Каселла + Капучино Mebel.in",
        "extraction_method": "regex",
        "confidence": "high",
        "needs_review": 0,
        "notes": None,
        "source_text_hash": "hash",
        "created_at": "2025-01-01T10:00:00",
        "first_photo_path": None,
    }
    base.update(overrides)
    columns = ", ".join(base)
    placeholders = ", ".join("?" for _ in base)
    cur = conn.execute(
        f"INSERT INTO extracted_facts ({columns}) VALUES ({placeholders})",
        tuple(base.values()),
    )
    return int(cur.lastrowid)


def _make_facts_db(path: Path, rows: list[dict]) -> Path:
    with sqlite3.connect(path) as conn:
        conn.executescript(FACTS_SCHEMA)
        for row in rows:
            _insert_fact(conn, **row)
        conn.commit()
    return path


def _make_canonical_db(path: Path, messages: list[dict], media: list[dict]) -> Path:
    with sqlite3.connect(path) as conn:
        conn.executescript(CANONICAL_SCHEMA)
        for message in messages:
            conn.execute(
                """
                INSERT INTO canonical_messages (
                    telegram_message_id, date, text_plain, text_entities_json,
                    raw_best_json, has_real_photo
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    message["telegram_message_id"],
                    message.get("date", "2025-01-01T10:00:00"),
                    message.get("text_plain", ""),
                    json.dumps(message.get("entities", []), ensure_ascii=False),
                    "{}",
                    int(message.get("has_real_photo", False)),
                ),
            )
        for item in media:
            conn.execute(
                """
                INSERT INTO media (telegram_message_id, media_kind, absolute_path)
                VALUES (?, 'photo', ?)
                """,
                (item["telegram_message_id"], str(item["absolute_path"])),
            )
        conn.commit()
    return path


def _tiny_jpeg(path: Path) -> Path:
    if importlib.util.find_spec("PIL") is not None:
        from PIL import Image

        Image.new("RGB", (12, 10), color=(120, 140, 160)).save(path)
        return path
    if importlib.util.find_spec("cv2") is not None:
        import cv2
        import numpy as np

        image = np.full((10, 12, 3), (120, 140, 160), dtype=np.uint8)
        ok, encoded = cv2.imencode(".jpg", image)
        assert ok
        encoded.tofile(str(path))
        return path
    path.write_bytes(b"not-a-real-image")
    return path


def _stage4_fixture(tmp_path: Path) -> tuple[Path, Path]:
    img1 = _tiny_jpeg(tmp_path / "p1.jpg")
    img2 = _tiny_jpeg(tmp_path / "p2.jpg")
    img3 = _tiny_jpeg(tmp_path / "p3.jpg")
    img4 = _tiny_jpeg(tmp_path / "p4.jpg")

    facts_db = _make_facts_db(
        tmp_path / "facts.sqlite",
        [
            {
                "id": 1,
                "source_message_id": 100,
                "item_type": "kitchen_facades",
                "finish": "Дуб Каселла + Капучино",
                "evidence_quote": "Кухня фасады Дуб Каселла + Капучино Mebel.in",
            },
            {
                "id": 2,
                "source_message_id": 100,
                "item_type": "countertop",
                "finish": None,
                "material": "столешница Grandex камень",
                "evidence_quote": "Столешница Grandex камень",
            },
            {
                "id": 3,
                "source_message_id": 101,
                "item_type": "backsplash",
                "finish": None,
                "material": "фартук Laparet",
                "evidence_quote": "Фартук Laparet",
            },
            {
                "id": 4,
                "source_message_id": 300,
                "item_type": "kitchen_facades",
                "finish": "Дуб Чарльстон темно-коричневый + Оливковый",
                "evidence_quote": "Кухня фасады Дуб Чарльстон темно-коричневый + Оливковый",
            },
            {
                "id": 5,
                "source_message_id": 300,
                "item_type": "backsplash",
                "finish": None,
                "material": "плитка фартук Лемана Про",
                "evidence_quote": "Плитка на фартуке Лемана Про",
            },
            {
                "id": 6,
                "source_message_id": 500,
                "item_type": "kitchen_facades",
                "finish": "Premium White",
                "evidence_quote": "Фасады кухни Premium White",
            },
            {
                "id": 7,
                "source_message_id": 500,
                "item_type": "countertop",
                "finish": None,
                "material": "столешница Calacatta камень",
                "evidence_quote": "Столешница Calacatta камень",
            },
            {
                "id": 8,
                "source_message_id": 700,
                "item_type": "bundle_purchase",
                "finish": None,
                "material": None,
                "evidence_quote": "Кухня, диван, стол комплектом 100000 ₽",
                "confidence": "low",
            },
        ],
    )

    link_entities = [
        {
            "type": "text_link",
            "text": "проекта",
            "href": "https://t.me/olya_homestaging/200",
        }
    ]
    canonical_db = _make_canonical_db(
        tmp_path / "canonical.sqlite",
        [
            {
                "telegram_message_id": 100,
                "text_plain": "Артикулы проекта\nКухня фасады Дуб Каселла + Капучино",
                "entities": link_entities,
            },
            {
                "telegram_message_id": 101,
                "text_plain": "Артикулы проекта\nФартук Laparet",
                "entities": link_entities,
            },
            {
                "telegram_message_id": 200,
                "text_plain": "Евро2 45 м2\nЖК Митинский лес\nДизайнер Алена\nЗадача: светлое дерево и нейтральный интерьер",
                "has_real_photo": True,
            },
            {
                "telegram_message_id": 201,
                "text_plain": "",
                "has_real_photo": True,
            },
            {
                "telegram_message_id": 300,
                "text_plain": "Евро2 44 м2\nЖК Матвеевский парк\nХоумстейджер Наталья\nТемное дерево + зеленый",
                "has_real_photo": True,
            },
            {
                "telegram_message_id": 500,
                "text_plain": "Студия 25 м2\nЖК Лосиноостровский парк\nСветлый фасад и каменная столешница",
                "has_real_photo": True,
            },
            {
                "telegram_message_id": 700,
                "text_plain": "ЖК Без деталей\nКухня в составе общего комплекта",
                "has_real_photo": True,
            },
        ],
        [
            {"telegram_message_id": 200, "absolute_path": img1},
            {"telegram_message_id": 201, "absolute_path": img2},
            {"telegram_message_id": 201, "absolute_path": img3},
            {"telegram_message_id": 300, "absolute_path": img4},
            {"telegram_message_id": 500, "absolute_path": img1},
            {"telegram_message_id": 700, "absolute_path": img2},
        ],
    )
    return facts_db, canonical_db


def test_uses_extracted_facts_table_not_facts(tmp_path: Path) -> None:
    facts_db = _make_facts_db(tmp_path / "facts.sqlite", [])
    assert load_kitchen_facts(facts_db) == []


def test_resolves_project_post_id_from_article_project_link() -> None:
    message = CanonicalMessage(
        message_id=100,
        date="2025-01-01T10:00:00",
        text_plain="Артикулы проекта",
        text_entities_json=json.dumps(
            [
                {
                    "type": "text_link",
                    "text": "Артикулы проекта",
                    "href": "https://t.me/olya_homestaging/200",
                }
            ],
            ensure_ascii=False,
        ),
        raw_best_json="{}",
    )
    assert resolve_project_post_id(message, channel_username="olya_homestaging", fallback_message_id=100) == 200


def test_extracts_object_and_designer_or_default() -> None:
    text = "Евро2 45 м2\nЖК Митинский лес\nДизайнер Алена\nХоумстейджер Наталья"
    assert extract_object_name(text, date="2025-01-01T10:00:00", project_post_id=200)[0] == "ЖК Митинский лес"
    designer, source = extract_designer(text)
    assert "Дизайнер Алена" in designer
    assert "Хоумстейджер Наталья" in designer
    assert source == "credited_in_post"

    default_designer, default_source = extract_designer("ЖК Митинский лес\nЗадача: квартира под аренду")
    assert default_designer == DEFAULT_DESIGNER
    assert default_source == "default_channel_author"


def test_clean_kitchen_palette_value_filters_noise() -> None:
    rejected = [
        "Задача",
        "Алюмика",
        "на кухне из акрила. Функциональность и максимальное количество мест хранения",
        "Задача: сделать квартиру под сдачу, чтобы заказчики были на полном доверии и все было рабочее",
        "Арт",
    ]
    for value in rejected:
        assert not is_clean_kitchen_palette_value(value)

    preserved = [
        "Дуб каселла + Капучино",
        "Дуб каселла натуральный + Сапфир",
        "орех Карини и МДФ белый нубук",
        "5023 Доминикана",
        "пластик Форст итальянский камень",
    ]
    for value in preserved:
        assert is_clean_kitchen_palette_value(value)


def test_palette_classification_rules() -> None:
    base = {
        "project_post_id": 1,
        "source_message_ids": [1],
        "date": "2025-01-01",
        "object_name": "ЖК Test",
        "object_source": "project_text_jk",
        "area_type": None,
        "city": None,
        "designer": DEFAULT_DESIGNER,
        "designer_source": "default_channel_author",
        "candidate_project_url": "https://t.me/olya_homestaging/1",
        "candidate_article_urls": [],
        "facade_parts": [],
        "wall_color": None,
        "flooring": None,
        "vendors": [],
        "prices": [],
        "evidence_quotes": [],
        "photo_paths": [],
    }
    p1 = KitchenProject(**base, facade_finish_raw="Дуб Каселла + Капучино", countertop_raw=None, backsplash_raw=None)
    p2 = KitchenProject(**base, facade_finish_raw="Дуб + Зеленый сапфир", countertop_raw=None, backsplash_raw=None)
    p3 = KitchenProject(**base, facade_finish_raw="Premium White", countertop_raw="Calacatta камень", backsplash_raw=None)

    assert classify_palette(p1)[0] == "wood_neutral"
    assert classify_palette(p2)[0] == "wood_nature_accent"
    assert classify_palette(p3)[0] == "light_facade_stone_accent"


def test_build_report_groups_by_project_and_writes_outputs(tmp_path: Path) -> None:
    facts_db, canonical_db = _stage4_fixture(tmp_path)
    out_dir = tmp_path / "out"
    result = build_kitchen_palette_report(
        facts_db=facts_db,
        canonical_db=canonical_db,
        out_dir=out_dir,
        channel_username="olya_homestaging",
        examples_per_category=6,
        photos_per_example=2,
    )

    assert result.kitchen_fact_count == 8
    assert result.project_candidate_count == 4
    assert result.selected_by_category["wood_neutral"] == 1
    assert result.selected_by_category["wood_nature_accent"] == 1
    assert result.selected_by_category["light_facade_stone_accent"] == 1

    assert (out_dir / "kitchen_examples.csv").exists()
    assert (out_dir / "kitchen_examples.jsonl").exists()
    assert (out_dir / "kitchen_palette_report.md").exists()
    assert (out_dir / "kitchen_palette_short.md").exists()
    assert (out_dir / "kitchen_palette_short_clean.md").exists()
    assert (out_dir / "kitchen_palette_quality_notes.md").exists()
    assert (out_dir / "kitchen_examples_selected_clean.csv").exists()
    assert (out_dir / "link_validation_todo.csv").exists()

    report = (out_dir / "kitchen_palette_report.md").read_text(encoding="utf-8")
    assert "Категория 1 — Светлое дерево + тёплый нейтральный фасад" in report
    assert "Категория 2 — Дерево + цветной/природный акцент" in report
    assert "Категория 3 — Светлый фасад + камень/фартук/столешница как акцент" in report
    assert "не анализировались через OCR, VLM" in report
    assert "визуальной классификацией" in report
    assert "ЖК Митинский лес" in report
    assert "https://t.me/olya_homestaging/200" in report

    rows = list(csv.DictReader((out_dir / "kitchen_examples.csv").open(encoding="utf-8-sig")))
    selected = [row for row in rows if row["selected_for_report"] == "1"]
    excluded = [row for row in rows if row["exclusion_reason"] == "bundle_only"]
    assert len(selected) == 3
    assert len(excluded) == 1
    assert any(row["project_post_id"] == "200" and row["source_message_ids"] == "100;101" for row in rows)
    for column in [
        "quality_score",
        "quality_tier",
        "selected_for_clean_report",
        "palette_summary_clean",
        "has_clean_facade",
        "has_countertop",
        "has_backsplash",
        "has_photo",
        "object_name",
        "designer",
        "project_post_id",
        "candidate_project_url",
        "contact_sheet_path",
    ]:
        assert column in rows[0]

    clean_rows = list(csv.DictReader((out_dir / "kitchen_examples_selected_clean.csv").open(encoding="utf-8-sig")))
    assert len(clean_rows) == 3
    assert all(row["selected_for_clean_report"] == "1" for row in clean_rows)

    clean_short = (out_dir / "kitchen_palette_short_clean.md").read_text(encoding="utf-8")
    for heading in [
        "## 1. Светлое дерево + тёплый нейтральный фасад",
        "## 2. Дерево + цветной/природный акцент",
        "## 3. Светлый фасад + камень/фартук/столешница как акцент",
    ]:
        assert heading in clean_short
    assert "Задача" not in clean_short
    assert "на кухне из акрила" not in clean_short
    assert "Алюмика" not in clean_short
    assert "Фото приложены механически" in clean_short
    assert "https://t.me/olya_homestaging/" in clean_short
    assert "🟢 сильный пример" in clean_short

    quality_notes = (out_dir / "kitchen_palette_quality_notes.md").read_text(encoding="utf-8")
    assert "Suppression is display/report-layer only; source rows are preserved in CSV/JSONL." in quality_notes

    todo = (out_dir / "link_validation_todo.csv").read_text(encoding="utf-8-sig")
    assert "requires manual verification" in todo


def test_bundle_only_and_no_clean_facade_are_not_selected_for_clean_report(tmp_path: Path) -> None:
    img = _tiny_jpeg(tmp_path / "p.jpg")
    facts_db = _make_facts_db(
        tmp_path / "facts.sqlite",
        [
            {
                "id": 1,
                "source_message_id": 10,
                "item_type": "bundle_purchase",
                "finish": None,
                "material": None,
                "evidence_quote": "Кухня и шкафы комплектом",
                "confidence": "low",
            },
            {
                "id": 2,
                "source_message_id": 20,
                "item_type": "countertop",
                "finish": None,
                "material": "столешница Grandex камень",
                "evidence_quote": "Столешница Grandex камень",
            },
        ],
    )
    canonical_db = _make_canonical_db(
        tmp_path / "canonical.sqlite",
        [
            {"telegram_message_id": 10, "text_plain": "ЖК Bundle", "has_real_photo": True},
            {"telegram_message_id": 20, "text_plain": "ЖК No facade\nсветлая кухня и каменная столешница", "has_real_photo": True},
        ],
        [
            {"telegram_message_id": 10, "absolute_path": img},
            {"telegram_message_id": 20, "absolute_path": img},
        ],
    )
    out_dir = tmp_path / "out"
    build_kitchen_palette_report(
        facts_db=facts_db,
        canonical_db=canonical_db,
        out_dir=out_dir,
        channel_username="olya_homestaging",
        examples_per_category=6,
        photos_per_example=1,
    )

    rows = list(csv.DictReader((out_dir / "kitchen_examples.csv").open(encoding="utf-8-sig")))
    assert all(row["selected_for_clean_report"] == "0" for row in rows)
    assert {row["exclusion_reason"] for row in rows} >= {"bundle_only", "no_clean_facade"}


def test_category_does_not_force_six_examples(tmp_path: Path) -> None:
    facts_db, canonical_db = _stage4_fixture(tmp_path)
    out_dir = tmp_path / "out"
    result = build_kitchen_palette_report(
        facts_db=facts_db,
        canonical_db=canonical_db,
        out_dir=out_dir,
        channel_username="olya_homestaging",
        examples_per_category=6,
        photos_per_example=2,
    )
    assert result.selected_by_category == {
        "wood_neutral": 1,
        "wood_nature_accent": 1,
        "light_facade_stone_accent": 1,
    }


def test_cli_kitchen_palette_report_command(tmp_path: Path) -> None:
    facts_db, canonical_db = _stage4_fixture(tmp_path)
    out_dir = tmp_path / "cli_out"
    code = main(
        [
            "kitchen-palette-report",
            "--facts-db",
            str(facts_db),
            "--canonical-db",
            str(canonical_db),
            "--out-dir",
            str(out_dir),
            "--channel-username",
            "olya_homestaging",
            "--examples-per-category",
            "6",
            "--photos-per-example",
            "2",
            "--format",
            "markdown",
        ]
    )
    assert code == 0
    assert (out_dir / "kitchen_palette_report.md").exists()
    assert (out_dir / "kitchen_palette_short.md").exists()
    assert (out_dir / "kitchen_palette_short_clean.md").exists()
    assert (out_dir / "kitchen_palette_quality_notes.md").exists()


def test_contact_sheet_tiny_images_or_graceful_fallback(tmp_path: Path) -> None:
    paths = [_tiny_jpeg(tmp_path / f"img_{index}.jpg") for index in range(4)]
    out_path = tmp_path / "sheet.jpg"
    ok = create_contact_sheet([str(path) for path in paths], out_path)
    if importlib.util.find_spec("PIL") is None and importlib.util.find_spec("cv2") is None:
        assert ok is False
        assert not out_path.exists()
    else:
        assert ok is True
        assert out_path.exists()
