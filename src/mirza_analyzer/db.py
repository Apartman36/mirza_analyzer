from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .merge import (
    CanonicalMessage,
    field_from_best_or_first,
    first_field_value,
    has_meaningful_value,
    merge_exports,
    richest_field_value,
)
from .telegram_export import TelegramExport, date_range, load_valid_exports, normalize_text
from .utils import coerce_int, ensure_parent_dir, json_dumps, sha256_file, utc_now_iso


@dataclass(frozen=True)
class IngestResult:
    db_path: Path
    export_count: int
    invalid_export_count: int
    canonical_message_count: int
    source_variant_count: int
    media_count: int
    fts_enabled: bool


def create_database_from_data_root(data_root: Path, db_path: Path) -> IngestResult:
    exports, invalid = load_valid_exports(data_root)
    return create_database_from_exports(exports, invalid_count=len(invalid), db_path=db_path)


def create_database_from_exports(
    exports: list[TelegramExport],
    *,
    invalid_count: int = 0,
    db_path: Path,
) -> IngestResult:
    ensure_parent_dir(db_path)
    remove_existing_database_files(db_path)

    canonical_messages = merge_exports(exports)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = DELETE")
        create_schema(conn)
        fts_enabled = create_fts_table(conn)

        export_ids = insert_exports(conn, exports)
        insert_canonical_messages(conn, canonical_messages, export_ids)
        source_variant_count = insert_message_variants(conn, canonical_messages, export_ids)
        media_count = insert_media(conn, canonical_messages, export_ids)
        if fts_enabled:
            populate_fts(conn, canonical_messages)
        conn.commit()
    finally:
        conn.close()

    return IngestResult(
        db_path=db_path,
        export_count=len(exports),
        invalid_export_count=invalid_count,
        canonical_message_count=len(canonical_messages),
        source_variant_count=source_variant_count,
        media_count=media_count,
        fts_enabled=fts_enabled,
    )


def remove_existing_database_files(db_path: Path) -> None:
    for path in [db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")]:
        if path.exists():
            path.unlink()


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE exports (
            id INTEGER PRIMARY KEY,
            export_path TEXT UNIQUE NOT NULL,
            export_folder TEXT NOT NULL,
            detected_export_type TEXT NOT NULL,
            json_path TEXT NOT NULL,
            message_count INTEGER NOT NULL,
            date_min TEXT,
            date_max TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE canonical_messages (
            telegram_message_id INTEGER PRIMARY KEY,
            message_type TEXT,
            date TEXT,
            date_unixtime TEXT,
            text TEXT,
            text_plain TEXT NOT NULL,
            text_char_count INTEGER NOT NULL,
            best_text_source_export_id INTEGER,
            from_name TEXT,
            from_id TEXT,
            author TEXT,
            edited TEXT,
            edited_unixtime TEXT,
            reply_to_message_id INTEGER,
            forwarded_from TEXT,
            forwarded_from_id TEXT,
            reactions_json TEXT,
            text_entities_json TEXT,
            raw_best_json TEXT NOT NULL,
            has_real_photo INTEGER NOT NULL DEFAULT 0,
            has_real_file INTEGER NOT NULL DEFAULT 0,
            source_variant_count INTEGER NOT NULL,
            FOREIGN KEY(best_text_source_export_id) REFERENCES exports(id)
        );

        CREATE TABLE message_variants (
            id INTEGER PRIMARY KEY,
            telegram_message_id INTEGER NOT NULL,
            export_id INTEGER NOT NULL,
            raw_json TEXT NOT NULL,
            text TEXT NOT NULL,
            photo_value TEXT,
            file_value TEXT,
            has_real_media INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY(telegram_message_id) REFERENCES canonical_messages(telegram_message_id),
            FOREIGN KEY(export_id) REFERENCES exports(id)
        );

        CREATE TABLE media (
            id INTEGER PRIMARY KEY,
            telegram_message_id INTEGER NOT NULL,
            export_id INTEGER NOT NULL,
            media_kind TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            absolute_path TEXT NOT NULL,
            file_name TEXT NOT NULL,
            extension TEXT NOT NULL,
            mime_type TEXT,
            media_type TEXT,
            width INTEGER,
            height INTEGER,
            file_size_from_json INTEGER,
            actual_file_size INTEGER,
            sha256 TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            is_real_file INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY(telegram_message_id) REFERENCES canonical_messages(telegram_message_id),
            FOREIGN KEY(export_id) REFERENCES exports(id)
        );

        CREATE UNIQUE INDEX idx_media_message_sha256
            ON media(telegram_message_id, sha256);
        CREATE INDEX idx_media_extension ON media(extension);
        CREATE INDEX idx_variants_message ON message_variants(telegram_message_id);
        CREATE INDEX idx_variants_export ON message_variants(export_id);
        CREATE INDEX idx_canonical_date ON canonical_messages(date);
        CREATE INDEX idx_canonical_has_photo ON canonical_messages(has_real_photo);
        CREATE INDEX idx_canonical_has_file ON canonical_messages(has_real_file);
        """
    )


def create_fts_table(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute(
            """
            CREATE VIRTUAL TABLE canonical_messages_fts
            USING fts5(
                text_plain,
                content='canonical_messages',
                content_rowid='telegram_message_id'
            )
            """
        )
    except sqlite3.OperationalError:
        return False
    return True


def insert_exports(conn: sqlite3.Connection, exports: list[TelegramExport]) -> dict[str, int]:
    export_ids: dict[str, int] = {}
    created_at = utc_now_iso()
    for export in exports:
        date_min, date_max = date_range(export.messages)
        cursor = conn.execute(
            """
            INSERT INTO exports (
                export_path,
                export_folder,
                detected_export_type,
                json_path,
                message_count,
                date_min,
                date_max,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(export.json_path),
                str(export.folder),
                export.detected_export_type,
                str(export.json_path),
                len(export.messages),
                date_min,
                date_max,
                created_at,
            ),
        )
        export_ids[str(export.json_path)] = int(cursor.lastrowid)
    return export_ids


def insert_canonical_messages(
    conn: sqlite3.Connection,
    canonical_messages: list[CanonicalMessage],
    export_ids: dict[str, int],
) -> None:
    for canonical in canonical_messages:
        best = canonical.best_text_variant
        best_message = best.message
        variants = canonical.variants
        conn.execute(
            """
            INSERT INTO canonical_messages (
                telegram_message_id,
                message_type,
                date,
                date_unixtime,
                text,
                text_plain,
                text_char_count,
                best_text_source_export_id,
                from_name,
                from_id,
                author,
                edited,
                edited_unixtime,
                reply_to_message_id,
                forwarded_from,
                forwarded_from_id,
                reactions_json,
                text_entities_json,
                raw_best_json,
                has_real_photo,
                has_real_file,
                source_variant_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                canonical.telegram_message_id,
                scalar_or_json(field_from_best_or_first(canonical, "type")),
                scalar_or_json(field_from_best_or_first(canonical, "date")),
                scalar_or_json(field_from_best_or_first(canonical, "date_unixtime")),
                canonical.raw_text_for_storage,
                canonical.text_plain,
                len(canonical.text_plain),
                export_ids[str(best.export_json_path)],
                scalar_or_json(field_from_best_or_first(canonical, "from")),
                scalar_or_json(field_from_best_or_first(canonical, "from_id")),
                scalar_or_json(field_from_best_or_first(canonical, "author")),
                scalar_or_json(field_from_best_or_first(canonical, "edited")),
                scalar_or_json(field_from_best_or_first(canonical, "edited_unixtime")),
                coerce_int(first_field_value(variants, "reply_to_message_id")),
                scalar_or_json(first_field_value(variants, "forwarded_from")),
                scalar_or_json(first_field_value(variants, "forwarded_from_id")),
                optional_json(richest_field_value(variants, "reactions")),
                optional_json(best_message.get("text_entities"))
                or optional_json(richest_field_value(variants, "text_entities")),
                json_dumps(canonical.raw_best_variant.message),
                int(canonical.has_real_photo),
                int(canonical.has_real_file),
                canonical.source_variant_count,
            ),
        )


def insert_message_variants(
    conn: sqlite3.Connection,
    canonical_messages: list[CanonicalMessage],
    export_ids: dict[str, int],
) -> int:
    created_at = utc_now_iso()
    count = 0
    for canonical in canonical_messages:
        for variant in canonical.variants:
            conn.execute(
                """
                INSERT INTO message_variants (
                    telegram_message_id,
                    export_id,
                    raw_json,
                    text,
                    photo_value,
                    file_value,
                    has_real_media,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    variant.telegram_message_id,
                    export_ids[str(variant.export_json_path)],
                    json_dumps(variant.message),
                    variant.text_plain,
                    scalar_or_json(variant.message.get("photo")),
                    scalar_or_json(variant.message.get("file")),
                    int(variant.has_real_media),
                    created_at,
                ),
            )
            count += 1
    return count


def insert_media(
    conn: sqlite3.Connection,
    canonical_messages: list[CanonicalMessage],
    export_ids: dict[str, int],
) -> int:
    count = 0
    sha_cache: dict[Path, str] = {}
    seen: set[tuple[int, str]] = set()
    for canonical in canonical_messages:
        for media in canonical.media:
            candidate = media.candidate
            if candidate.absolute_path is None or candidate.relative_path is None:
                continue
            path = candidate.absolute_path
            if path not in sha_cache:
                sha_cache[path] = sha256_file(path)
            digest = sha_cache[path]
            dedup_key = (media.telegram_message_id, digest)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            message = media.message
            conn.execute(
                """
                INSERT OR IGNORE INTO media (
                    telegram_message_id,
                    export_id,
                    media_kind,
                    relative_path,
                    absolute_path,
                    file_name,
                    extension,
                    mime_type,
                    media_type,
                    width,
                    height,
                    file_size_from_json,
                    actual_file_size,
                    sha256,
                    raw_json,
                    is_real_file
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    media.telegram_message_id,
                    export_ids[str(media.export_json_path)],
                    candidate.media_kind,
                    candidate.relative_path,
                    str(path),
                    path.name,
                    path.suffix.lower(),
                    scalar_or_json(message.get("mime_type")),
                    scalar_or_json(message.get("media_type")),
                    coerce_int(message.get("width")),
                    coerce_int(message.get("height")),
                    file_size_from_json(message, candidate.media_kind),
                    path.stat().st_size,
                    digest,
                    json_dumps(
                        {
                            "field_path": candidate.field_path,
                            "value": candidate.value,
                            "message": message,
                        }
                    ),
                    1,
                ),
            )
            count += 1
    return count


def populate_fts(
    conn: sqlite3.Connection,
    canonical_messages: list[CanonicalMessage],
) -> None:
    conn.executemany(
        """
        INSERT INTO canonical_messages_fts(rowid, text_plain)
        VALUES (?, ?)
        """,
        [
            (canonical.telegram_message_id, canonical.text_plain)
            for canonical in canonical_messages
            if canonical.text_plain.strip()
        ],
    )


def optional_json(value: Any) -> str | None:
    if not has_meaningful_value(value):
        return None
    return json_dumps(value)


def scalar_or_json(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    return json_dumps(value)


def file_size_from_json(message: dict[str, Any], media_kind: str) -> int | None:
    if media_kind == "photo":
        return coerce_int(message.get("photo_file_size"))
    if media_kind == "thumbnail":
        return coerce_int(message.get("thumbnail_file_size"))
    return coerce_int(message.get("file_size"))


def database_stats(db_path: Path) -> dict[str, Any]:
    with connect(db_path) as conn:
        stats: dict[str, Any] = {
            "canonical_posts": scalar_count(conn, "SELECT COUNT(*) FROM canonical_messages"),
            "source_variants": scalar_count(conn, "SELECT COUNT(*) FROM message_variants"),
            "media_files": scalar_count(conn, "SELECT COUNT(*) FROM media"),
            "posts_with_text": scalar_count(
                conn,
                "SELECT COUNT(*) FROM canonical_messages WHERE trim(text_plain) <> ''",
            ),
            "posts_with_photos": scalar_count(
                conn,
                "SELECT COUNT(*) FROM canonical_messages WHERE has_real_photo = 1",
            ),
            "posts_with_files": scalar_count(
                conn,
                "SELECT COUNT(*) FROM canonical_messages WHERE has_real_file = 1",
            ),
            "date_min": conn.execute("SELECT MIN(date) FROM canonical_messages").fetchone()[0],
            "date_max": conn.execute("SELECT MAX(date) FROM canonical_messages").fetchone()[0],
            "reactions_records": scalar_count(
                conn,
                "SELECT COUNT(*) FROM canonical_messages WHERE reactions_json IS NOT NULL",
            ),
            "reply_to_message_id_records": scalar_count(
                conn,
                "SELECT COUNT(*) FROM canonical_messages WHERE reply_to_message_id IS NOT NULL",
            ),
            "forwarded_records": scalar_count(
                conn,
                """
                SELECT COUNT(*)
                FROM canonical_messages
                WHERE forwarded_from IS NOT NULL OR forwarded_from_id IS NOT NULL
                """,
            ),
        }
        stats["top_extensions"] = [
            (row["extension"], row["n"])
            for row in conn.execute(
                """
                SELECT extension, COUNT(*) AS n
                FROM media
                GROUP BY extension
                ORDER BY n DESC, extension
                LIMIT 10
                """
            )
        ]
        return stats


def scalar_count(conn: sqlite3.Connection, sql: str) -> int:
    return int(conn.execute(sql).fetchone()[0])
