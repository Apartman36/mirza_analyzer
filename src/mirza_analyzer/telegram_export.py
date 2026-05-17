from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .utils import compact_whitespace, json_load


MEDIA_KEYS = {"photo", "file", "thumbnail"}
MEDIA_PATH_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".tgs",
    ".webm",
    ".m4a",
    ".mp3",
    ".mp4",
    ".mov",
    ".pdf",
}
PLACEHOLDER_MARKERS = (
    "file not included",
    "change data exporting settings to download",
)


@dataclass(frozen=True)
class MediaCandidate:
    field_path: str
    media_kind: str
    value: str
    relative_path: str | None
    absolute_path: Path | None
    is_real_file: bool
    missing_reason: str | None = None


@dataclass
class TelegramExport:
    json_path: Path
    data_root: Path
    raw: dict[str, Any]
    messages: list[dict[str, Any]]
    detected_export_type: str = ""
    key_counts: Counter[str] = field(default_factory=Counter)

    @property
    def folder(self) -> Path:
        return self.json_path.parent

    @property
    def is_root_export(self) -> bool:
        try:
            return self.json_path.resolve() == (self.data_root.resolve() / "result.json")
        except OSError:
            return self.json_path == self.data_root / "result.json"


@dataclass(frozen=True)
class InvalidExport:
    json_path: Path
    error: str


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(normalize_text(item.get("text")))
            elif item is not None:
                parts.append(str(item))
        return "".join(parts)
    return str(value)


def normalized_text_preview(value: Any, limit: int = 120) -> str:
    text = compact_whitespace(normalize_text(value))
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def is_placeholder_media_value(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip().lower()
    if not normalized:
        return True
    return any(marker in normalized for marker in PLACEHOLDER_MARKERS)


def find_export_jsons(data_root: Path) -> list[Path]:
    root = data_root.resolve()
    paths = [path for path in root.rglob("result.json") if path.is_file()]
    return sorted(paths, key=lambda path: (len(path.relative_to(root).parts), str(path).lower()))


def load_export_json(json_path: Path, data_root: Path) -> TelegramExport:
    raw = json_load(json_path)
    if not isinstance(raw, dict):
        raise ValueError("Top-level JSON value is not an object")
    messages = raw.get("messages")
    if not isinstance(messages, list):
        raise ValueError("Export JSON does not contain a messages list")
    typed_messages = [message for message in messages if isinstance(message, dict)]
    key_counts: Counter[str] = Counter()
    for message in typed_messages:
        key_counts.update(message.keys())
    export = TelegramExport(
        json_path=json_path.resolve(),
        data_root=data_root.resolve(),
        raw=raw,
        messages=typed_messages,
        key_counts=key_counts,
    )
    export.detected_export_type = detect_export_type(export)
    return export


def load_valid_exports(data_root: Path) -> tuple[list[TelegramExport], list[InvalidExport]]:
    valid: list[TelegramExport] = []
    invalid: list[InvalidExport] = []
    for json_path in find_export_jsons(data_root):
        try:
            valid.append(load_export_json(json_path, data_root))
        except Exception as exc:  # noqa: BLE001 - audit should report every invalid export.
            invalid.append(InvalidExport(json_path=json_path, error=str(exc)))
    return valid, invalid


def date_range(messages: Iterable[dict[str, Any]]) -> tuple[str | None, str | None]:
    dates = [str(message["date"]) for message in messages if message.get("date")]
    if not dates:
        return None, None
    return min(dates), max(dates)


def message_id(message: dict[str, Any]) -> int | None:
    raw_id = message.get("id")
    if raw_id is None:
        return None
    try:
        return int(raw_id)
    except (TypeError, ValueError):
        return None


def extract_media_candidates(
    message: dict[str, Any],
    export_folder: Path,
    *,
    include_nested: bool = True,
) -> list[MediaCandidate]:
    candidates: list[MediaCandidate] = []
    seen: set[tuple[str, str]] = set()

    def add_candidate(
        field_path: str,
        key: str,
        raw_value: Any,
        *,
        media_kind: str | None = None,
    ) -> None:
        if not isinstance(raw_value, str):
            return
        resolved_media_kind = media_kind or key_to_media_kind(key, raw_value)
        value = raw_value.strip()
        if not value:
            candidate = MediaCandidate(
                field_path=field_path,
                media_kind=resolved_media_kind,
                value=raw_value,
                relative_path=None,
                absolute_path=None,
                is_real_file=False,
                missing_reason="empty",
            )
            candidates.append(candidate)
            return
        if is_placeholder_media_value(value):
            candidates.append(
                MediaCandidate(
                    field_path=field_path,
                    media_kind=resolved_media_kind,
                    value=raw_value,
                    relative_path=None,
                    absolute_path=None,
                    is_real_file=False,
                    missing_reason="placeholder",
                )
            )
            return
        absolute_path = resolve_media_path(export_folder, value)
        dedup_key = (field_path, value)
        if dedup_key in seen:
            return
        seen.add(dedup_key)
        if absolute_path is None:
            candidates.append(
                MediaCandidate(
                    field_path=field_path,
                    media_kind=resolved_media_kind,
                    value=raw_value,
                    relative_path=value,
                    absolute_path=None,
                    is_real_file=False,
                    missing_reason="missing",
                )
            )
            return
        candidates.append(
            MediaCandidate(
                field_path=field_path,
                media_kind=resolved_media_kind,
                value=raw_value,
                relative_path=value,
                absolute_path=absolute_path,
                is_real_file=True,
            )
        )

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}.{key}" if path else str(key)
                if key in MEDIA_KEYS:
                    add_candidate(child_path, key, child)
                elif include_nested and is_existing_media_like_value(export_folder, child):
                    add_candidate(
                        child_path,
                        key,
                        child,
                        media_kind=key_to_media_kind(key, child),
                    )
                if include_nested and isinstance(child, (dict, list)):
                    walk(child, child_path)
        elif isinstance(value, list) and include_nested:
            for index, child in enumerate(value):
                child_path = f"{path}[{index}]"
                if is_existing_media_like_value(export_folder, child):
                    add_candidate(child_path, "nested", child, media_kind="document")
                else:
                    walk(child, child_path)

    walk(message, "")
    return candidates


def resolve_media_path(export_folder: Path, value: str) -> Path | None:
    if looks_like_external_reference(value):
        return None
    candidate = Path(value)
    if candidate.is_absolute():
        return None
    try:
        base = export_folder.resolve()
        resolved = (base / candidate).resolve()
        if not resolved.is_relative_to(base):
            return None
    except OSError:
        return None
    if not resolved.is_file():
        return None
    return resolved


def looks_like_external_reference(value: str) -> bool:
    lowered = value.strip().lower()
    return lowered.startswith(("http://", "https://", "tg://", "mailto:"))


def is_existing_media_like_value(export_folder: Path, value: Any) -> bool:
    if not isinstance(value, str):
        return False
    if is_placeholder_media_value(value) or looks_like_external_reference(value):
        return False
    suffix = Path(value).suffix.lower()
    if suffix not in MEDIA_PATH_EXTENSIONS:
        return False
    return resolve_media_path(export_folder, value) is not None


def key_to_media_kind(key: str, value: str | None = None) -> str:
    if key == "photo":
        return "photo"
    if key == "thumbnail":
        return "thumbnail"
    if key == "document_id":
        if value and "sticker" in value.replace("\\", "/").lower():
            return "sticker"
        return "document"
    return "file"


def detect_export_type(export: TelegramExport) -> str:
    if export.is_root_export:
        return "full_text_export"
    has_real_photo = False
    has_real_non_photo_media = False
    for message in export.messages:
        for candidate in extract_media_candidates(message, export.folder):
            if not candidate.is_real_file:
                continue
            if candidate.media_kind == "photo":
                has_real_photo = True
            else:
                has_real_non_photo_media = True
    if has_real_photo and has_real_non_photo_media:
        return "mixed_export"
    if has_real_photo:
        return "photo_export"
    if has_real_non_photo_media:
        return "file_export"
    return "file_export"
