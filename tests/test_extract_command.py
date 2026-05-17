import csv
import sqlite3
from pathlib import Path

from mirza_analyzer.cli import main


def create_extract_fixture(db_path: Path, photo_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE canonical_messages (
                telegram_message_id INTEGER PRIMARY KEY,
                date TEXT,
                text_plain TEXT NOT NULL,
                text_char_count INTEGER NOT NULL,
                source_variant_count INTEGER NOT NULL,
                has_real_photo INTEGER NOT NULL DEFAULT 0,
                has_real_file INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE media (
                id INTEGER PRIMARY KEY,
                telegram_message_id INTEGER NOT NULL,
                media_kind TEXT NOT NULL,
                absolute_path TEXT NOT NULL
            );
            """
        )
        text = (
            "Артикулы проекта\n"
            "ЖК Люблинский парк\n\n"
            "Кухня фасады Дуб каселла натуральный+ Капучино гринвуд Mebel.in\n"
            "270 000₽\n\n"
            "Фартук Сантехника онлайн\n"
            "Арт. 621512 2590₽/м2\n\n"
            "Стол OZON\n"
            "Арт. 1550292417 14 157₽\n\n"
            "Диван Divan.ru\n"
            "Слипсон Вельвет Зеленый 64 990₽\n\n"
            "🌱Диваны, стулья, столик, тумба Divan.ru 242 440₽\n\n"
            "Цвет стен G482"
        )
        conn.execute(
            """
            INSERT INTO canonical_messages (
                telegram_message_id,
                date,
                text_plain,
                text_char_count,
                source_variant_count,
                has_real_photo,
                has_real_file
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (7299, "2026-05-02T11:19:01", text, len(text), 1, 1, 0),
        )
        conn.execute(
            """
            INSERT INTO media (telegram_message_id, media_kind, absolute_path)
            VALUES (?, ?, ?)
            """,
            (7299, "photo", str(photo_path)),
        )


def test_extract_facts_command_creates_expected_outputs(tmp_path: Path) -> None:
    db_path = tmp_path / "fixture.sqlite"
    out_dir = tmp_path / "extracted"
    photo_path = tmp_path / "photo.jpg"
    photo_path.write_bytes(b"photo")
    create_extract_fixture(db_path, photo_path)

    main(
        [
            "extract-facts",
            "--db",
            str(db_path),
            "--out-dir",
            str(out_dir),
            "--source",
            "project_articles",
        ]
    )

    expected_files = {
        "summary.md",
        "extraction_quality_summary.md",
        "extracted_facts_all.csv",
        "extracted_facts_clean.csv",
        "extracted_facts_needs_review.csv",
        "extracted_facts.jsonl",
        "extracted_facts.sqlite",
    }
    assert expected_files <= {path.name for path in out_dir.iterdir()}
    category_files = {
        "flooring.md",
        "wall_colors.md",
        "kitchens.md",
        "chairs.md",
        "tables.md",
        "sofas.md",
        "hallway.md",
        "living_room_furniture.md",
    }
    assert category_files <= {path.name for path in (out_dir / "by_category").iterdir()}
    assert category_files <= {path.name for path in (out_dir / "by_category_clean").iterdir()}
    assert category_files <= {path.name for path in (out_dir / "by_category_needs_review").iterdir()}

    with (out_dir / "extracted_facts_all.csv").open("r", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    with (out_dir / "extracted_facts_clean.csv").open("r", encoding="utf-8", newline="") as fh:
        clean_rows = list(csv.DictReader(fh))
    with (out_dir / "extracted_facts_needs_review.csv").open("r", encoding="utf-8", newline="") as fh:
        review_rows = list(csv.DictReader(fh))

    assert len(rows) >= 5
    assert any(row["category"] == "kitchens" and row["item_type"] == "kitchen_facades" for row in rows)
    assert any(row["category"] == "sofas" and row["vendor_normalized"] == "Divan.ru" for row in rows)
    assert any(row["category"] == "wall_colors" and row["color_code"] == "G482" for row in rows)
    assert any(row["item_type"] == "bundle_purchase" and row["needs_review"] == "1" for row in review_rows)
    assert all(row["needs_review"] == "0" for row in clean_rows)
    assert all(row["needs_review"] == "1" for row in review_rows)
    assert not any(row["item_type"] == "bundle_purchase" for row in clean_rows)

    with sqlite3.connect(out_dir / "extracted_facts.sqlite") as conn:
        fact_count = conn.execute("SELECT COUNT(*) FROM extracted_facts").fetchone()[0]

    assert fact_count == len(rows)
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "Total facts extracted" in summary
    assert "Clean facts" in summary
    assert "Needs review" in summary
