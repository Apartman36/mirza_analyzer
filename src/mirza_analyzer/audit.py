from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .telegram_export import (
    TelegramExport,
    date_range,
    extract_media_candidates,
    load_valid_exports,
    message_id,
    normalize_text,
)
from .utils import ensure_parent_dir, utc_now_iso


def write_audit_report(data_root: Path, out_path: Path) -> None:
    report = build_audit_report(data_root)
    ensure_parent_dir(out_path)
    out_path.write_text(report, encoding="utf-8")


def build_audit_report(data_root: Path) -> str:
    root = data_root.resolve()
    exports, invalid = load_valid_exports(root)
    all_files = [path for path in root.rglob("*") if path.is_file()]
    json_like_files = [path for path in all_files if path.suffix.lower() == ".json"]

    file_ext_counts: Counter[str] = Counter()
    file_ext_sizes: defaultdict[str, int] = defaultdict(int)
    total_size = 0
    for path in all_files:
        ext = path.suffix.lower() or "[no extension]"
        size = path.stat().st_size
        total_size += size
        file_ext_counts[ext] += 1
        file_ext_sizes[ext] += size

    all_message_ids: Counter[int] = Counter()
    global_key_counts: Counter[str] = Counter()
    export_summaries = [summarize_export(export) for export in exports]
    for export in exports:
        for message in export.messages:
            global_key_counts.update(message.keys())
            telegram_message_id = message_id(message)
            if telegram_message_id is not None:
                all_message_ids[telegram_message_id] += 1

    duplicate_message_ids = sum(1 for count in all_message_ids.values() if count > 1)
    message_id_min = min(all_message_ids) if all_message_ids else None
    message_id_max = max(all_message_ids) if all_message_ids else None

    lines: list[str] = []
    lines.append("# Telegram Export Audit")
    lines.append("")
    lines.append(f"- Data root: `{root}`")
    lines.append(f"- Generated at: `{utc_now_iso()}`")
    lines.append(f"- Total files: {len(all_files)}")
    lines.append(f"- Total size: {format_bytes(total_size)}")
    lines.append(f"- JSON-like files found: {len(json_like_files)}")
    lines.append(f"- Valid Telegram `result.json` exports: {len(exports)}")
    lines.append(f"- Invalid Telegram `result.json` exports: {len(invalid)}")
    lines.append(f"- Unique Telegram message IDs: {len(all_message_ids)}")
    lines.append(f"- Message IDs appearing in more than one export: {duplicate_message_ids}")
    lines.append(f"- Global message ID range: {message_id_min} -> {message_id_max}")
    lines.append("")

    if invalid:
        lines.append("## Invalid Exports")
        lines.append("")
        for item in invalid:
            lines.append(f"- `{item.json_path}`: {item.error}")
        lines.append("")

    lines.append("## Export Summaries")
    lines.append("")
    lines.append(
        "| Export | Type | Messages | Date range | Text messages | Text chars | "
        "Photo keys | File keys | Real media | Missing media |"
    )
    lines.append("|---|---:|---:|---|---:|---:|---:|---:|---:|---:|")
    for summary in export_summaries:
        lines.append(
            "| "
            f"`{summary['path']}` | "
            f"{summary['type']} | "
            f"{summary['messages']} | "
            f"{summary['date_min'] or ''} -> {summary['date_max'] or ''} | "
            f"{summary['text_messages']} | "
            f"{summary['text_chars']} | "
            f"{summary['photo_keys']} | "
            f"{summary['file_keys']} | "
            f"{summary['real_media']} | "
            f"{summary['missing_media']} |"
        )
    lines.append("")

    lines.append("## File Extensions")
    lines.append("")
    lines.append("| Extension | Files | Size |")
    lines.append("|---|---:|---:|")
    for ext, count in file_ext_counts.most_common():
        lines.append(f"| `{ext}` | {count} | {format_bytes(file_ext_sizes[ext])} |")
    lines.append("")

    lines.append("## Global JSON Keys")
    lines.append("")
    lines.append("| Key | Count |")
    lines.append("|---|---:|")
    for key, count in global_key_counts.most_common():
        lines.append(f"| `{key}` | {count} |")
    lines.append("")

    return "\n".join(lines) + "\n"


def summarize_export(export: TelegramExport) -> dict[str, Any]:
    text_messages = 0
    text_chars = 0
    real_media = 0
    missing_media = 0
    for message in export.messages:
        text = normalize_text(message.get("text"))
        if text.strip():
            text_messages += 1
            text_chars += len(text)
        for candidate in extract_media_candidates(message, export.folder):
            if candidate.is_real_file:
                real_media += 1
            else:
                missing_media += 1
    date_min, date_max = date_range(export.messages)
    return {
        "path": str(export.json_path),
        "type": export.detected_export_type,
        "messages": len(export.messages),
        "date_min": date_min,
        "date_max": date_max,
        "text_messages": text_messages,
        "text_chars": text_chars,
        "photo_keys": export.key_counts.get("photo", 0),
        "file_keys": export.key_counts.get("file", 0),
        "real_media": real_media,
        "missing_media": missing_media,
    }


def format_bytes(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{size} B"

