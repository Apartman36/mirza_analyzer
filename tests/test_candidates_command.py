import csv
import sqlite3
from pathlib import Path

from mirza_analyzer.cli import main


def create_candidate_fixture(db_path: Path, photo_path: Path) -> None:
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
        text = "Кухня фасады Дуб Каселла. Артикулы проекта"
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
            (1, "2024-01-01T10:00:00", text, len(text), 1, 1, 0),
        )
        conn.execute(
            """
            INSERT INTO media (telegram_message_id, media_kind, absolute_path)
            VALUES (?, ?, ?)
            """,
            (1, "photo", str(photo_path)),
        )


def test_candidates_command_creates_expected_output_files(tmp_path: Path) -> None:
    db_path = tmp_path / "fixture.sqlite"
    out_dir = tmp_path / "candidates"
    photo_path = tmp_path / "photo.jpg"
    photo_path.write_bytes(b"photo")
    create_candidate_fixture(db_path, photo_path)

    main(
        [
            "candidates",
            "--db",
            str(db_path),
            "--out-dir",
            str(out_dir),
            "--limit-per-category",
            "10",
        ]
    )

    expected_files = {
        "summary.md",
        "candidates.csv",
        "candidates.jsonl",
        "flooring.md",
        "wall_colors.md",
        "kitchens.md",
        "chairs.md",
        "tables.md",
        "sofas.md",
        "hallway.md",
        "living_room_furniture.md",
        "project_article_posts.md",
    }
    assert expected_files <= {path.name for path in out_dir.iterdir()}

    with (out_dir / "candidates.csv").open("r", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))

    assert any(row["category_id"] == "kitchens" for row in rows)
    assert "Message 1" in (out_dir / "project_article_posts.md").read_text(encoding="utf-8")

