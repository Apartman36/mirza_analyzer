import json
import sqlite3
from pathlib import Path

from mirza_analyzer.db import create_database_from_data_root
from mirza_analyzer.merge import merge_exports
from mirza_analyzer.telegram_export import load_valid_exports


def write_export(folder: Path, messages: list[dict]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "result.json"
    path.write_text(
        json.dumps({"name": "test", "type": "public_channel", "messages": messages}),
        encoding="utf-8",
    )
    return path


def test_merge_prefers_root_text_and_keeps_media_from_partial_export(tmp_path: Path) -> None:
    write_export(
        tmp_path,
        [
            {
                "id": 1,
                "type": "message",
                "date": "2024-01-01T10:00:00",
                "date_unixtime": "1704103200",
                "text": "Root",
            }
        ],
    )
    partial = tmp_path / "ChatExport"
    photos = partial / "photos"
    photos.mkdir(parents=True)
    (photos / "photo_1.jpg").write_bytes(b"photo")
    write_export(
        partial,
        [
            {
                "id": 1,
                "type": "message",
                "date": "2024-01-01T10:00:00",
                "text": "Much longer text from partial export",
                "photo": "photos/photo_1.jpg",
                "photo_file_size": 5,
            }
        ],
    )

    exports, invalid = load_valid_exports(tmp_path)
    assert not invalid

    canonical_messages = merge_exports(exports)

    assert len(canonical_messages) == 1
    canonical = canonical_messages[0]
    assert canonical.telegram_message_id == 1
    assert canonical.text_plain == "Root"
    assert canonical.source_variant_count == 2
    assert canonical.has_real_photo
    assert len(canonical.media) == 1


def test_ingest_does_not_create_media_rows_for_placeholders(tmp_path: Path) -> None:
    write_export(
        tmp_path,
        [
            {
                "id": 1,
                "type": "message",
                "date": "2024-01-01T10:00:00",
                "text": "Root text",
            }
        ],
    )
    write_export(
        tmp_path / "ChatExport",
        [
            {
                "id": 1,
                "type": "message",
                "date": "2024-01-01T10:00:00",
                "text": "",
                "photo": "(File not included. Change data exporting settings to download.)",
            }
        ],
    )

    db_path = tmp_path / "out.sqlite"
    result = create_database_from_data_root(tmp_path, db_path)

    assert result.canonical_message_count == 1
    assert result.source_variant_count == 2
    assert result.media_count == 0

    with sqlite3.connect(db_path) as conn:
        media_rows = conn.execute("SELECT COUNT(*) FROM media").fetchone()[0]
        variant_rows = conn.execute(
            "SELECT COUNT(*) FROM message_variants WHERE photo_value IS NOT NULL"
        ).fetchone()[0]

    assert media_rows == 0
    assert variant_rows == 1

