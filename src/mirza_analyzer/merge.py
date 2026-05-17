from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .telegram_export import (
    MediaCandidate,
    TelegramExport,
    extract_media_candidates,
    message_id,
    normalize_text,
)
from .utils import json_dumps


@dataclass(frozen=True)
class SourceVariant:
    telegram_message_id: int
    export_json_path: Path
    export_folder: Path
    is_root_export: bool
    message: dict[str, Any]
    text_plain: str
    media_candidates: list[MediaCandidate]

    @property
    def has_real_media(self) -> bool:
        return any(candidate.is_real_file for candidate in self.media_candidates)


@dataclass(frozen=True)
class VariantMedia:
    telegram_message_id: int
    export_json_path: Path
    message: dict[str, Any]
    candidate: MediaCandidate


@dataclass(frozen=True)
class CanonicalMessage:
    telegram_message_id: int
    variants: list[SourceVariant]
    best_text_variant: SourceVariant
    raw_best_variant: SourceVariant
    media: list[VariantMedia]

    @property
    def source_variant_count(self) -> int:
        return len(self.variants)

    @property
    def text_plain(self) -> str:
        return self.best_text_variant.text_plain

    @property
    def raw_text_for_storage(self) -> str:
        raw_text = self.best_text_variant.message.get("text")
        if isinstance(raw_text, str):
            return raw_text
        return json_dumps(raw_text)

    @property
    def has_real_photo(self) -> bool:
        return any(media.candidate.media_kind == "photo" for media in self.media)

    @property
    def has_real_file(self) -> bool:
        return any(media.candidate.media_kind == "file" for media in self.media)


def collect_source_variants(exports: list[TelegramExport]) -> list[SourceVariant]:
    variants: list[SourceVariant] = []
    for export in exports:
        for message in export.messages:
            telegram_message_id = message_id(message)
            if telegram_message_id is None:
                continue
            variants.append(
                SourceVariant(
                    telegram_message_id=telegram_message_id,
                    export_json_path=export.json_path,
                    export_folder=export.folder,
                    is_root_export=export.is_root_export,
                    message=message,
                    text_plain=normalize_text(message.get("text")),
                    media_candidates=extract_media_candidates(message, export.folder),
                )
            )
    return variants


def merge_exports(exports: list[TelegramExport]) -> list[CanonicalMessage]:
    grouped: dict[int, list[SourceVariant]] = defaultdict(list)
    for variant in collect_source_variants(exports):
        grouped[variant.telegram_message_id].append(variant)

    merged: list[CanonicalMessage] = []
    for telegram_message_id in sorted(grouped):
        variants = sorted(
            grouped[telegram_message_id],
            key=lambda variant: (
                0 if variant.is_root_export else 1,
                str(variant.export_json_path).lower(),
            ),
        )
        best_text_variant = choose_best_text_variant(variants)
        raw_best_variant = choose_raw_best_variant(variants, best_text_variant)
        media = collect_real_media(variants)
        merged.append(
            CanonicalMessage(
                telegram_message_id=telegram_message_id,
                variants=variants,
                best_text_variant=best_text_variant,
                raw_best_variant=raw_best_variant,
                media=media,
            )
        )
    return merged


def choose_best_text_variant(variants: list[SourceVariant]) -> SourceVariant:
    root_with_text = [
        variant for variant in variants if variant.is_root_export and variant.text_plain.strip()
    ]
    if root_with_text:
        return max(root_with_text, key=lambda variant: len(variant.text_plain))

    with_text = [variant for variant in variants if variant.text_plain.strip()]
    if with_text:
        return max(
            with_text,
            key=lambda variant: (
                len(variant.text_plain),
                1 if variant.is_root_export else 0,
                str(variant.export_json_path).lower(),
            ),
        )

    return variants[0]


def choose_raw_best_variant(
    variants: list[SourceVariant],
    best_text_variant: SourceVariant,
) -> SourceVariant:
    if best_text_variant.text_plain.strip():
        return best_text_variant
    variants_with_media = [variant for variant in variants if variant.has_real_media]
    if variants_with_media:
        return variants_with_media[0]
    return best_text_variant


def collect_real_media(variants: list[SourceVariant]) -> list[VariantMedia]:
    media: list[VariantMedia] = []
    seen: set[tuple[str, str]] = set()
    for variant in variants:
        for candidate in variant.media_candidates:
            if not candidate.is_real_file or candidate.absolute_path is None:
                continue
            dedup_key = (
                str(candidate.absolute_path).lower(),
                candidate.field_path,
            )
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            media.append(
                VariantMedia(
                    telegram_message_id=variant.telegram_message_id,
                    export_json_path=variant.export_json_path,
                    message=variant.message,
                    candidate=candidate,
                )
            )
    return media


def richest_field_value(variants: list[SourceVariant], field_name: str) -> Any:
    values = [
        variant.message.get(field_name)
        for variant in variants
        if has_meaningful_value(variant.message.get(field_name))
    ]
    if not values:
        return None
    return max(values, key=richness_score)


def first_field_value(variants: list[SourceVariant], field_name: str) -> Any:
    for variant in variants:
        value = variant.message.get(field_name)
        if has_meaningful_value(value):
            return value
    return None


def field_from_best_or_first(canonical: CanonicalMessage, field_name: str) -> Any:
    value = canonical.best_text_variant.message.get(field_name)
    if has_meaningful_value(value):
        return value
    return first_field_value(canonical.variants, field_name)


def has_meaningful_value(value: Any) -> bool:
    if value is None:
        return False
    if value == "":
        return False
    if value == [] or value == {}:
        return False
    return True


def richness_score(value: Any) -> int:
    if not has_meaningful_value(value):
        return 0
    return len(json_dumps(value))

