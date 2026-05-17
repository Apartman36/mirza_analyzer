from __future__ import annotations

from pathlib import Path

from .db import connect
from .utils import compact_whitespace, ensure_parent_dir, truncate, utc_now_iso


def write_sample_posts(db_path: Path, out_path: Path, limit: int) -> None:
    ensure_parent_dir(out_path)
    lines = build_sample_posts_markdown(db_path, limit)
    out_path.write_text(lines, encoding="utf-8")


def build_sample_posts_markdown(db_path: Path, limit: int) -> str:
    with connect(db_path) as conn:
        posts = conn.execute(
            """
            SELECT
                telegram_message_id,
                date,
                text_plain,
                has_real_photo,
                has_real_file,
                source_variant_count
            FROM canonical_messages
            ORDER BY
                CASE
                    WHEN trim(text_plain) <> '' AND has_real_photo = 1 THEN 0
                    WHEN trim(text_plain) <> '' AND has_real_file = 1 THEN 1
                    WHEN trim(text_plain) <> '' THEN 2
                    WHEN has_real_photo = 1 OR has_real_file = 1 THEN 3
                    ELSE 4
                END,
                telegram_message_id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        lines: list[str] = []
        lines.append("# Sample Canonical Posts")
        lines.append("")
        lines.append(f"- Database: `{db_path.resolve()}`")
        lines.append(f"- Generated at: `{utc_now_iso()}`")
        lines.append(f"- Limit: {limit}")
        lines.append("")

        for post in posts:
            telegram_message_id = int(post["telegram_message_id"])
            media_rows = conn.execute(
                """
                SELECT media_kind, relative_path, absolute_path, extension
                FROM media
                WHERE telegram_message_id = ?
                ORDER BY media_kind, relative_path
                """,
                (telegram_message_id,),
            ).fetchall()
            source_rows = conn.execute(
                """
                SELECT DISTINCT e.export_path
                FROM message_variants AS v
                JOIN exports AS e ON e.id = v.export_id
                WHERE v.telegram_message_id = ?
                ORDER BY e.export_path
                """,
                (telegram_message_id,),
            ).fetchall()

            lines.append(f"## Message {telegram_message_id}")
            lines.append("")
            lines.append(f"- Date: `{post['date'] or ''}`")
            lines.append(f"- Source variants: {post['source_variant_count']}")
            lines.append(f"- Has real photo: {bool(post['has_real_photo'])}")
            lines.append(f"- Has real file: {bool(post['has_real_file'])}")
            preview = truncate(compact_whitespace(post["text_plain"] or ""), 500)
            lines.append(f"- Text preview: {preview if preview else '[empty]'}")
            if media_rows:
                lines.append("- Media:")
                for media in media_rows:
                    lines.append(
                        f"  - `{media['media_kind']}` `{media['relative_path']}` "
                        f"-> `{media['absolute_path']}`"
                    )
            else:
                lines.append("- Media: none")
            lines.append("- Source exports:")
            for source in source_rows:
                lines.append(f"  - `{source['export_path']}`")
            lines.append("")

    return "\n".join(lines)

