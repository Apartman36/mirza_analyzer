from __future__ import annotations

import csv
import json
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import parse_qs, urlparse

from .kitchen_palette_report import (
    CHANNEL_LINK_STATUS,
    CanonicalMessage,
    KitchenProject,
    build_kitchen_projects,
    classify_projects,
    extract_designer,
    extract_object_name,
    load_canonical_messages,
    load_context_facts,
    load_kitchen_facts,
    load_photo_paths,
    normalize_for_match,
    telegram_post_url,
)
from .utils import compact_whitespace


HIGH_LINK_CONFIDENCE = "high"
LINK_STATUS = CHANNEL_LINK_STATUS
WALL_CONTEXT_RE = re.compile(
    r"(?:стен|краск|покрас|перекрас|цвет|акцентн|колеров|эмал|интерьерн|wall|paint)",
    flags=re.IGNORECASE,
)
URL_RE = re.compile(r"https?://[^\s<>\]\[()]+", flags=re.IGNORECASE)


@dataclass(frozen=True)
class SourceEntity:
    source_message_id: int
    visible_text: str
    href: str
    entity_type: str
    start: int
    end: int
    context: str
    source_priority: str


@dataclass(frozen=True)
class ProjectLink:
    source_message_id: int
    project_post_id: int | None
    project_key: str | None
    project_name: str | None
    object_name: str | None
    street: str | None
    designer: str | None
    link_method: str
    link_confidence: str
    evidence: str


@dataclass(frozen=True)
class WallPaintMention:
    source_message_id: int
    manufacturer: str | None
    product_line: str | None
    color_code_raw: str | None
    color_code_normalized: str | None
    descriptive_color_raw: str | None
    shade_family: str
    shade_family_method: str
    evidence_quote: str
    telegram_post_url: str
    project_post_id: int | None = None
    project_key: str | None = None
    project_name: str | None = None
    link_method: str | None = None
    link_confidence: str | None = None
    raw_fact_mentions: int = 0


@dataclass(frozen=True)
class ApplianceMention:
    source_message_id: int
    appliance_type: str
    brand: str | None
    model: str | None
    article_id: str | None
    merchant: str | None
    merchant_domain: str | None
    product_url: str | None
    telegram_post_url: str
    evidence_quote: str
    evidence_class: str
    confidence: str
    notes: str | None = None
    project_post_id: int | None = None
    project_key: str | None = None
    project_name: str | None = None
    link_method: str | None = None
    link_confidence: str | None = None


@dataclass(frozen=True)
class FurnitureMakerMention:
    source_message_id: int
    maker_name_raw: str
    maker_name_normalized: str
    classification: str
    person_name: str | None
    phone: str | None
    telegram: str | None
    whatsapp: str | None
    instagram: str | None
    website: str | None
    what_was_made: str | None
    telegram_post_url: str
    evidence_quote: str
    confidence: str
    project_post_id: int | None = None
    project_key: str | None = None
    project_name: str | None = None
    link_method: str | None = None
    link_confidence: str | None = None


@dataclass(frozen=True)
class PaintRanking:
    rank: int
    manufacturer: str | None
    product_line: str | None
    color_code: str
    unique_projects: int
    unique_messages: int
    raw_fact_mentions: int
    example_projects: tuple[str, ...]
    confidence_notes: str


@dataclass(frozen=True)
class ShadeRanking:
    rank: int
    shade_family: str
    unique_projects: int
    unique_messages: int
    representative_raw_descriptions: tuple[str, ...]
    representative_codes: tuple[str, ...]
    confidence_notes: str


@dataclass
class FatherQueryIndex:
    messages: dict[int, CanonicalMessage]
    entities_by_message: dict[int, list[SourceEntity]]
    project_links: dict[int, ProjectLink]
    wall_paint_mentions: list[WallPaintMention]
    appliance_mentions: list[ApplianceMention]
    furniture_maker_mentions: list[FurnitureMakerMention]
    kitchen_projects: list[KitchenProject]
    project_linkage_review: list[ProjectLink]
    raw_wall_fact_counts: dict[str, int]
    facts_db: Path
    canonical_db: Path


@dataclass(frozen=True)
class FatherQueryRunResult:
    out_dir: Path
    output_files: tuple[Path, ...]
    index: FatherQueryIndex


def extract_message_entities(
    message: CanonicalMessage,
    variant_raw_jsons: Sequence[str] = (),
) -> list[SourceEntity]:
    """Read URL-bearing Telegram entities using the canonical/fallback priority."""

    candidates: list[tuple[str, Any]] = []
    canonical_entities = _json_value(message.text_entities_json)
    if isinstance(canonical_entities, list):
        candidates.append(("canonical_messages.text_entities_json", canonical_entities))

    raw_best = _json_value(message.raw_best_json)
    raw_best_entities = _entities_from_raw_payload(raw_best)
    if raw_best_entities is not None:
        candidates.append(("canonical_messages.raw_best_json", raw_best_entities))

    for raw in variant_raw_jsons:
        payload = _json_value(raw)
        entities = _entities_from_raw_payload(payload)
        if entities is not None:
            candidates.append(("message_variants.raw_json", entities))

    for priority, payload in candidates:
        parsed = _parse_entity_payload(
            payload,
            message_id=message.message_id,
            text=message.text_plain,
            priority=priority,
        )
        if parsed:
            return _append_raw_text_urls(message, parsed, priority=priority)

    return _append_raw_text_urls(message, [], priority="canonical_messages.text_plain")


def _json_value(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _entities_from_raw_payload(payload: Any) -> list[dict[str, Any]] | None:
    if not isinstance(payload, dict):
        return None
    for key in ("text_entities", "caption_entities"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    value = payload.get("text")
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return None


def _parse_entity_payload(
    payload: Sequence[Any],
    *,
    message_id: int,
    text: str,
    priority: str,
) -> list[SourceEntity]:
    result: list[SourceEntity] = []
    cursor = 0
    sequential_offset = 0
    for item in payload:
        if not isinstance(item, dict):
            continue
        visible = str(item.get("text") or "")
        start = _entity_start(text, visible, cursor=cursor, sequential_offset=sequential_offset)
        end = start + len(visible)
        if visible:
            cursor = max(cursor, end)
            sequential_offset += len(visible)
        href = str(item.get("href") or "").strip()
        entity_type = str(item.get("type") or "")
        if not href and entity_type in {"link", "url"} and URL_RE.fullmatch(visible.strip()):
            href = visible.strip()
        if not href:
            continue
        result.append(
            SourceEntity(
                source_message_id=message_id,
                visible_text=compact_whitespace(visible),
                href=href,
                entity_type=entity_type,
                start=start,
                end=end,
                context=_context_for_span(text, start, end),
                source_priority=priority,
            )
        )
    return result


def _entity_start(text: str, visible: str, *, cursor: int, sequential_offset: int) -> int:
    if not visible:
        return min(cursor, len(text))
    if text.startswith(visible, cursor):
        return cursor
    found = text.find(visible, cursor)
    if found >= 0:
        return found
    if text.startswith(visible, sequential_offset):
        return sequential_offset
    found = text.find(visible)
    return found if found >= 0 else min(cursor, len(text))


def _append_raw_text_urls(
    message: CanonicalMessage,
    entities: Sequence[SourceEntity],
    *,
    priority: str,
) -> list[SourceEntity]:
    result = list(entities)
    known = {entity.href.rstrip(".,;)") for entity in result}
    for match in URL_RE.finditer(message.text_plain or ""):
        href = match.group(0).rstrip(".,;)")
        if href in known:
            continue
        result.append(
            SourceEntity(
                source_message_id=message.message_id,
                visible_text=href,
                href=href,
                entity_type="raw_url",
                start=match.start(),
                end=match.start() + len(href),
                context=_context_for_span(message.text_plain, match.start(), match.end()),
                source_priority=priority,
            )
        )
        known.add(href)
    return sorted(result, key=lambda item: (item.start, item.end, item.href))


def attribute_nearby_urls(
    text: str,
    entities: Sequence[SourceEntity],
    start: int,
    end: int,
) -> list[SourceEntity]:
    """Return URLs from the same blank-line-delimited product block only."""

    block_start, block_end = _paragraph_bounds(text, start, end)
    same_block = [
        entity
        for entity in entities
        if entity.start < block_end and entity.end > block_start
    ]
    if same_block:
        return sorted(same_block, key=lambda item: (abs(item.start - start), item.start))

    line_start, line_end = _line_bounds(text, start, end)
    return sorted(
        [entity for entity in entities if entity.start < line_end and entity.end > line_start],
        key=lambda item: (abs(item.start - start), item.start),
    )


def _paragraph_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    left_matches = list(re.finditer(r"\n\s*\n", text[: max(0, start)]))
    left = left_matches[-1].end() if left_matches else 0
    right_match = re.search(r"\n\s*\n", text[max(end, 0) :])
    right = end + right_match.start() if right_match else len(text)
    return left, right


def _line_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    left = text.rfind("\n", 0, max(0, start)) + 1
    right = text.find("\n", max(0, end))
    return left, len(text) if right < 0 else right


def _context_for_span(text: str, start: int, end: int) -> str:
    left, right = _paragraph_bounds(text, start, end)
    context = compact_whitespace(text[left:right])
    if len(context) <= 500:
        return context
    radius = 220
    return compact_whitespace(text[max(left, start - radius) : min(right, end + radius)])


PAINT_CODE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "little_greene",
        re.compile(r"\bLittle\s+Green(?:e)?\s+(?P<number>\d{2,3})\b", flags=re.IGNORECASE),
    ),
    (
        "ncs",
        re.compile(r"\b(?:NCS\s+)?S\s*\d{4}\s*-\s*[YRGBN](?:\d{1,2})?[YRGBN]?\b", flags=re.IGNORECASE),
    ),
    ("ral", re.compile(r"\bRAL\s*-?\s*\d{4}\b", flags=re.IGNORECASE)),
    (
        "dulux",
        re.compile(r"\b\d{2}\s*[A-Z]{2}\s+\d{2}\s*/\s*\d{3}\b", flags=re.IGNORECASE),
    ),
    ("letter", re.compile(r"(?<![A-ZА-Я0-9])[GHKLFVS]\s?\d{3}(?![A-ZА-Я0-9])", flags=re.IGNORECASE)),
)


def normalize_paint_code(raw: str) -> str:
    value = compact_whitespace(raw).upper().replace("–", "-").replace("—", "-")
    little = re.fullmatch(r"LITTLE\s+GREEN(?:E)?\s+(\d{2,3})", value, flags=re.IGNORECASE)
    if little:
        return little.group(1)
    ral = re.fullmatch(r"RAL\s*-?\s*(\d{4})", value, flags=re.IGNORECASE)
    if ral:
        return f"RAL {ral.group(1)}"
    ncs = re.fullmatch(
        r"(?:NCS\s+)?S\s*(\d{4})\s*-\s*([YRGBN](?:\d{1,2})?[YRGBN]?)",
        value,
        flags=re.IGNORECASE,
    )
    if ncs:
        return f"S {ncs.group(1)}-{ncs.group(2).upper()}"
    slash = re.fullmatch(r"(\d{2})\s*([A-Z]{2})\s+(\d{2})\s*/\s*(\d{3})", value)
    if slash:
        return f"{slash.group(1)}{slash.group(2)} {slash.group(3)}/{slash.group(4)}"
    letter = re.fullmatch(r"([GHKLFVS])\s?(\d{3})", value)
    if letter:
        return f"{letter.group(1)}{letter.group(2)}"
    return value


SHADE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("terracotta", ("терракот", "кирпичн", "ржаво", "охрист")),
    ("green", ("зелен", "зелён", "олив", "хаки", "шалфе", "фисташ")),
    ("blue", ("голуб", "син", "лазур", "бирюз")),
    ("brown", ("коричнев", "шоколад", "кофейн")),
    ("greige", ("грейдж", "greige", "серо-беж", "серо беж", "теплый сер", "тёплый сер")),
    ("warm_white_milk", ("молоч", "сливоч", "айвори", "ivory", "off-white", "теплый бел", "тёплый бел", "меренг")),
    ("cool_white", ("холодный бел", "холодн бел", "снежно-бел")),
    ("light_gray", ("светло-сер", "светлый сер", "светлая сер")),
    ("gray", ("серый", "серая", "серого", "серые", "графит", "антрацит")),
    ("warm_beige", ("беж", "песоч", "кремов", "капучино", "латте", "карамел", "ваниль")),
)


def normalize_shade_family(text: str) -> tuple[str, str | None, str]:
    normalized = normalize_for_match(text)
    for family, tokens in SHADE_RULES:
        for token in tokens:
            position = normalized.find(token)
            if position < 0:
                continue
            raw = _extract_color_description(text, token)
            return family, raw or token, "explicit_text"
    return "other", None, "unknown"


def _extract_color_description(text: str, matched_token: str | None = None) -> str | None:
    patterns = (
        r"\b(т[её]пл(?:ый|ая|ое)\s+(?:бел(?:ый|ая|ое)|сер(?:ый|ая|ое)))\b",
        r"\b(светло-сер\w*|серо-бежев\w*|молочн\w*|бежев\w*|зел[её]н\w*|терракотов\w*|голуб\w*|син\w*|коричнев\w*|кремов\w*)\b",
        r"(?:цвет(?:\s+стен)?|стены?|оттенок)\s*[:\-]?\s*([а-яёa-z-]+(?:\s+[а-яёa-z-]+){0,2})",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            candidate = compact_whitespace(match.group(1)).strip(" .,:;-")
            candidate = re.split(
                r"\s+(?:RAL|NCS|[GHKLFVS]\s?\d{3}|\d{2}\s*[A-Z]{2}\s+\d{2}/\d{3})\b",
                candidate,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0]
            return candidate or None
    if matched_token:
        match = re.search(re.escape(matched_token), text, flags=re.IGNORECASE)
        if match:
            return compact_whitespace(match.group(0))
    return None


BRAND_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Little Greene", re.compile(r"\bLittle\s+Green(?:e)?\b", flags=re.IGNORECASE)),
    ("Tikkurila", re.compile(r"\bTikkuril+l?a\b|\bТиккурила\b", flags=re.IGNORECASE)),
    ("Dulux", re.compile(r"\bDulux\b|\bДюлакс\b", flags=re.IGNORECASE)),
    ("Caparol", re.compile(r"\bCaparol\b|\bКапарол\b", flags=re.IGNORECASE)),
    ("Dufa", re.compile(r"\bDufa\b|\bDüfa\b|\bДюфа\b", flags=re.IGNORECASE)),
    ("Benjamin Moore", re.compile(r"\bBenjamin\s+Moore\b", flags=re.IGNORECASE)),
)


def extract_wall_paint_mentions(
    message: CanonicalMessage,
    project_link: ProjectLink | None = None,
    *,
    channel_username: str = "olya_homestaging",
) -> list[WallPaintMention]:
    mentions: list[WallPaintMention] = []
    occupied: list[tuple[int, int]] = []
    for unit_start, unit_end, unit in _local_text_units(message.text_plain):
        local_brand_signal = any(pattern.search(unit) for _, pattern in BRAND_PATTERNS)
        if not WALL_CONTEXT_RE.search(unit) and not local_brand_signal and not re.search(
            r"Little\s+Green(?:e)?|Tikkuril+l?a\s+Luja\s+Extra",
            unit,
            flags=re.IGNORECASE,
        ):
            continue
        for pattern_name, pattern in PAINT_CODE_PATTERNS:
            for match in pattern.finditer(unit):
                absolute_span = (unit_start + match.start(), unit_start + match.end())
                if any(_spans_overlap(absolute_span, previous) for previous in occupied):
                    continue
                raw = match.group(0)
                manufacturer = _local_manufacturer(
                    unit,
                    raw,
                    pattern_name=pattern_name,
                    code_start=match.start(),
                    code_end=match.end(),
                )
                product_line = _local_product_line(unit, manufacturer)
                family, description, method = normalize_shade_family(unit)
                evidence = compact_whitespace(unit)
                mentions.append(
                    WallPaintMention(
                        source_message_id=message.message_id,
                        manufacturer=manufacturer,
                        product_line=product_line,
                        color_code_raw=raw,
                        color_code_normalized=normalize_paint_code(raw),
                        descriptive_color_raw=description,
                        shade_family=family,
                        shade_family_method=method,
                        evidence_quote=evidence,
                        telegram_post_url=telegram_post_url(channel_username, message.message_id),
                        project_post_id=project_link.project_post_id if project_link else None,
                        project_key=project_link.project_key if project_link else None,
                        project_name=project_link.project_name if project_link else None,
                        link_method=project_link.link_method if project_link else None,
                        link_confidence=project_link.link_confidence if project_link else None,
                    )
                )
                occupied.append(absolute_span)

        product_match = re.search(r"\bTikkurila\s+Luja\s+Extra\b", unit, flags=re.IGNORECASE)
        if product_match and not any(
            mention.product_line == "Luja Extra" and mention.evidence_quote == compact_whitespace(unit)
            for mention in mentions
        ):
            family, description, method = normalize_shade_family(unit)
            mentions.append(
                WallPaintMention(
                    source_message_id=message.message_id,
                    manufacturer="Tikkurila",
                    product_line="Luja Extra",
                    color_code_raw=None,
                    color_code_normalized=None,
                    descriptive_color_raw=description,
                    shade_family=family,
                    shade_family_method=method,
                    evidence_quote=compact_whitespace(unit),
                    telegram_post_url=telegram_post_url(channel_username, message.message_id),
                    project_post_id=project_link.project_post_id if project_link else None,
                    project_key=project_link.project_key if project_link else None,
                    project_name=project_link.project_name if project_link else None,
                    link_method=project_link.link_method if project_link else None,
                    link_confidence=project_link.link_confidence if project_link else None,
                )
            )

    return _deduplicate_paint_mentions(mentions)


def _local_text_units(text: str) -> Iterable[tuple[int, int, str]]:
    for line_match in re.finditer(r"[^\n]+", text or ""):
        line = line_match.group(0)
        for sentence_match in re.finditer(r"[^.!?]+(?:[.!?]+|$)", line):
            raw = sentence_match.group(0)
            value = raw.strip()
            if not value:
                continue
            leading = len(raw) - len(raw.lstrip())
            start = line_match.start() + sentence_match.start() + leading
            end = line_match.start() + sentence_match.end()
            yield start, end, value


def _spans_overlap(first: tuple[int, int], second: tuple[int, int]) -> bool:
    return first[0] < second[1] and first[1] > second[0]


def _local_manufacturer(
    unit: str,
    raw_code: str,
    *,
    pattern_name: str,
    code_start: int,
    code_end: int,
) -> str | None:
    candidates: list[tuple[int, int, str]] = []
    for brand, pattern in BRAND_PATTERNS:
        for match in pattern.finditer(unit):
            if match.end() <= code_start:
                distance = code_start - match.end()
            elif match.start() >= code_end:
                distance = match.start() - code_end
            else:
                distance = 0
            candidates.append((distance, match.start(), brand))
    if candidates:
        return min(candidates)[2]
    if pattern_name == "little_greene":
        return "Little Greene"
    return None


def _local_product_line(unit: str, manufacturer: str | None) -> str | None:
    if manufacturer == "Tikkurila" and re.search(r"\bLuja\s+Extra\b", unit, flags=re.IGNORECASE):
        return "Luja Extra"
    return None


def _deduplicate_paint_mentions(mentions: Sequence[WallPaintMention]) -> list[WallPaintMention]:
    result: list[WallPaintMention] = []
    seen: set[tuple[Any, ...]] = set()
    for mention in mentions:
        key = (
            mention.source_message_id,
            mention.color_code_normalized,
            mention.product_line,
            mention.evidence_quote.casefold(),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(mention)
    return result


def link_message_to_project(
    message: CanonicalMessage,
    *,
    channel_username: str,
    stage4_source_to_project: Mapping[int, int] | None = None,
    project_name: str | None = None,
    entities: Sequence[SourceEntity] | None = None,
) -> ProjectLink:
    """Resolve a message conservatively; a repeated ЖК name is never a merge key."""

    source_entities = list(entities or extract_message_entities(message))
    explicit_targets: list[tuple[int, str]] = []
    for entity in source_entities:
        target = _channel_message_id(entity.href, channel_username)
        if target is None or not _is_explicit_project_entity(entity):
            continue
        explicit_targets.append((target, f"{entity.visible_text}: {entity.href}"))

    target_ids = sorted({target for target, _ in explicit_targets})
    metadata = _message_project_metadata(message, project_name=project_name)
    if len(target_ids) == 1:
        target = target_ids[0]
        return ProjectLink(
            source_message_id=message.message_id,
            project_post_id=target,
            project_key=f"telegram:{target}",
            project_name=metadata[0],
            object_name=metadata[1],
            street=metadata[2],
            designer=metadata[3],
            link_method="explicit_project_link",
            link_confidence="high",
            evidence=explicit_targets[0][1],
        )
    if len(target_ids) > 1:
        return ProjectLink(
            source_message_id=message.message_id,
            project_post_id=None,
            project_key=None,
            project_name=metadata[0],
            object_name=metadata[1],
            street=metadata[2],
            designer=metadata[3],
            link_method="ambiguous_metadata_match",
            link_confidence="low",
            evidence="Несколько явных ссылок на проекты: " + ", ".join(str(value) for value in target_ids),
        )

    stage4_target = (stage4_source_to_project or {}).get(message.message_id)
    if stage4_target is not None and stage4_target != message.message_id:
        if _safe_stage4_target_in_entities(source_entities, stage4_target, channel_username):
            return ProjectLink(
                source_message_id=message.message_id,
                project_post_id=stage4_target,
                project_key=f"telegram:{stage4_target}",
                project_name=metadata[0],
                object_name=metadata[1],
                street=metadata[2],
                designer=metadata[3],
                link_method="stage4_project_post",
                link_confidence="high",
                evidence=f"Проверенная привязка Stage 4 к post_id={stage4_target}",
            )

    # Historical Stage 2 ``project_name`` values are useful labels but are not
    # reliable enough to establish identity on their own (some contain product
    # lines such as "Панели под покраску").  Require project evidence from the
    # canonical message itself before assigning a high-confidence message key.
    if metadata[1] or metadata[2] or _has_direct_project_signal(message.text_plain):
        return ProjectLink(
            source_message_id=message.message_id,
            project_post_id=message.message_id,
            project_key=f"telegram:{message.message_id}",
            project_name=metadata[0] or metadata[1],
            object_name=metadata[1],
            street=metadata[2],
            designer=metadata[3],
            link_method="same_message",
            link_confidence="high",
            evidence="Проект и доказательство находятся в одном сообщении; ключ основан на message_id.",
        )

    return ProjectLink(
        source_message_id=message.message_id,
        project_post_id=None,
        project_key=None,
        project_name=metadata[0],
        object_name=None,
        street=None,
        designer=None,
        link_method="ambiguous_metadata_match",
        link_confidence="low",
        evidence="Недостаточно метаданных для консервативной привязки к проекту.",
    )


def _channel_message_id(href: str, channel_username: str) -> int | None:
    match = re.search(
        rf"https?://t\.me/(?:s/)?{re.escape(channel_username)}/(\d+)(?:\?single)?",
        href or "",
        flags=re.IGNORECASE,
    )
    return int(match.group(1)) if match else None


def _is_explicit_project_entity(entity: SourceEntity) -> bool:
    anchor = normalize_for_match(entity.visible_text)
    context = normalize_for_match(entity.context)
    if any(token in anchor for token in ("подборк", "других", "тариф", "услуг", "канал")):
        return False
    if anchor in {"проекта", "проект", "о проекте", "пост о проекте", "артикулы проекта"}:
        return True
    if "проект" in anchor and not any(token in anchor for token in ("купить", "подбор")):
        return True
    return bool(
        entity.entity_type == "raw_url"
        and re.search(r"(?:пост|артикулы)\s+(?:о\s+)?проект", context)
        and not re.search(r"подборк\w*\s+друг", context)
    )


def _safe_stage4_target_in_entities(
    entities: Sequence[SourceEntity],
    target: int,
    channel_username: str,
) -> bool:
    return any(
        _channel_message_id(entity.href, channel_username) == target and _is_explicit_project_entity(entity)
        for entity in entities
    )


def _message_project_metadata(
    message: CanonicalMessage,
    *,
    project_name: str | None,
) -> tuple[str | None, str | None, str | None, str | None]:
    object_name, object_source = extract_object_name(
        message.text_plain,
        date=message.date,
        project_post_id=message.message_id,
    )
    if object_source == "fallback_post_id":
        object_name = None
    designer, designer_source = extract_designer(message.text_plain)
    if designer_source != "credited_in_post":
        designer = None
    street = _extract_street(message.text_plain)
    return object_name or _validated_historical_project_name(project_name), object_name, street, designer


def _validated_historical_project_name(value: str | None) -> str | None:
    """Keep only Stage 2 labels that visibly identify a place/project.

    Older extraction sometimes stored the first product line as ``project_name``.
    A missing label is safer than publishing one of those values as a project.
    """

    if not value:
        return None
    cleaned = compact_whitespace(value).strip(" .,:;-")
    if re.search(
        r"(?:^|\s)(?:жк|жилой\s+комплекс|ул\.?|улица|проспект|пр-т|бульвар|шоссе|переулок|наб\.?|набережная)(?:\s|$)",
        cleaned,
        flags=re.IGNORECASE,
    ):
        return cleaned
    return None


def _extract_street(text: str) -> str | None:
    for raw_line in (text or "").splitlines()[:30]:
        line = compact_whitespace(raw_line).strip(" .,:;-")
        if re.search(
            r"\b(?:ул\.|улица|проспект|пр-т|бульвар|шоссе|переулок|наб\.|набережная)\b",
            line,
            flags=re.IGNORECASE,
        ):
            return line
    return None


def _has_direct_project_signal(text: str) -> bool:
    return bool(
        re.search(r"(?:^|\n)\s*(?:#?ЖК|Артикулы\s+проекта|Проект\s+квартиры)", text or "", flags=re.IGNORECASE)
    )


REFRIGERATOR_PATTERN = re.compile(r"\bхолодильник\w*\b", flags=re.IGNORECASE)
BUILT_IN_REFRIGERATOR_PATTERN = re.compile(
    r"(?:встро\w*|встраив\w*)\s+(?:двухдверн\w+\s+)?холодильник\w*|"
    r"холодильник\w*\s+(?:встро\w*|встраив\w*)",
    flags=re.IGNORECASE,
)


APPLIANCE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("refrigerator", REFRIGERATOR_PATTERN),
    ("microwave", re.compile(r"\b(?:СВЧ|микроволнов\w*)\b", flags=re.IGNORECASE)),
    ("oven", re.compile(r"\b(?:духовк\w*|духов(?:ой|ого|ому|ым)\s+шкаф\w*)\b", flags=re.IGNORECASE)),
    (
        "cooktop",
        re.compile(r"\b(?:варочн\w*\s+(?:панел\w*|поверхност\w*)|электроплит\w*|индукцион\w*\s+панел\w*)\b", flags=re.IGNORECASE),
    ),
)

MERCHANT_NAMES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("DNS", ("dns-shop.ru", "днс", "dns")),
    ("OZON", ("ozon.ru", "озон", "ozon")),
    ("Wildberries", ("wildberries.ru", "вайлдберриз", "wildberries", "wb")),
    ("М.Видео", ("mvideo.ru", "м.видео", "мвидео")),
    ("Эльдорадо", ("eldorado.ru", "эльдорадо")),
    ("Лемана Про", ("lemanapro.ru", "лемана про", "леруа мерлен")),
)


def extract_appliance_mentions(
    message: CanonicalMessage,
    entities: Sequence[SourceEntity],
    project_link: ProjectLink | None,
    channel_username: str,
) -> list[ApplianceMention]:
    result: list[ApplianceMention] = []
    seen: set[tuple[int, str, str]] = set()
    for detected_type, pattern in APPLIANCE_PATTERNS:
        for match in pattern.finditer(message.text_plain or ""):
            context_start, context_end, local_context = _appliance_context_for_span(
                message.text_plain,
                match.start(),
                match.end(),
            )
            appliance_type = _resolved_appliance_type(detected_type, local_context)
            key = (message.message_id, appliance_type, local_context.casefold())
            if key in seen:
                continue
            seen.add(key)
            nearby = [
                entity
                for entity in entities
                if entity.start < context_end and entity.end > context_start
            ]
            product_entity = _choose_appliance_url(
                nearby,
                local_context,
                appliance_type,
                match_start=match.start(),
                match_end=match.end(),
            )
            product_url = product_entity.href if product_entity else None
            merchant, domain = _extract_merchant(local_context, product_entity)
            brand, model = _extract_appliance_brand_model(local_context, product_url)
            article = _extract_article_id(local_context)
            evidence_class, confidence, notes = _classify_appliance_evidence(
                appliance_type=appliance_type,
                block=local_context,
                product_url=product_url,
                merchant=merchant,
                brand=brand,
                model=model,
            )
            result.append(
                ApplianceMention(
                    source_message_id=message.message_id,
                    appliance_type=appliance_type,
                    brand=brand,
                    model=model,
                    article_id=article,
                    merchant=merchant,
                    merchant_domain=domain,
                    product_url=product_url,
                    telegram_post_url=telegram_post_url(channel_username, message.message_id),
                    evidence_quote=local_context,
                    evidence_class=evidence_class,
                    confidence=confidence,
                    notes=notes,
                    project_post_id=project_link.project_post_id if project_link else None,
                    project_key=project_link.project_key if project_link else None,
                    project_name=project_link.project_name if project_link else None,
                    link_method=project_link.link_method if project_link else None,
                    link_confidence=project_link.link_confidence if project_link else None,
                )
            )
    return result


def _resolved_appliance_type(detected_type: str, context: str) -> str:
    if detected_type != "refrigerator":
        return detected_type
    if BUILT_IN_REFRIGERATOR_PATTERN.search(context):
        return "built_in_refrigerator"
    return "refrigerator_unconfirmed"


def _appliance_context_for_span(text: str, start: int, end: int) -> tuple[int, int, str]:
    """Return the target sentence/line plus an explicit adjacent metadata line.

    Telegram product lists commonly put ``Арт. ...`` on the line immediately after
    a linked product name.  Keeping only that continuation preserves the DEXP oven
    evidence without borrowing brands, merchants, or URLs from other products in
    the same paragraph.
    """

    unit_start, unit_end, unit = _local_unit_bounds_for_span(text, start, end)
    context_end = unit_end
    line_start, line_end = _line_bounds(text, start, end)
    if unit_end >= line_end:
        next_start = min(len(text), line_end + (1 if line_end < len(text) else 0))
        next_end = text.find("\n", next_start)
        if next_end < 0:
            next_end = len(text)
        continuation = text[next_start:next_end].strip()
        if continuation and re.match(
            r"(?:арт(?:икул)?\b|article\b|модел[ьи]\b|model\b)",
            continuation,
            flags=re.IGNORECASE,
        ):
            context_end = next_end
    return unit_start, context_end, compact_whitespace(text[unit_start:context_end] or unit)


def _local_unit_for_span(text: str, start: int, end: int) -> str:
    return _local_unit_bounds_for_span(text, start, end)[2]


def _local_unit_bounds_for_span(text: str, start: int, end: int) -> tuple[int, int, str]:
    for unit_start, unit_end, unit in _local_text_units(text):
        if unit_start <= start and unit_end >= end:
            return unit_start, unit_end, compact_whitespace(unit)
    line_start, line_end = _line_bounds(text, start, end)
    return line_start, line_end, compact_whitespace(text[line_start:line_end])


def _choose_appliance_url(
    entities: Sequence[SourceEntity],
    block: str,
    appliance_type: str,
    *,
    match_start: int,
    match_end: int,
) -> SourceEntity | None:
    ordered = sorted(
        entities,
        key=lambda entity: (_span_gap(match_start, match_end, entity.start, entity.end), entity.start),
    )
    for entity in ordered:
        href = entity.href.casefold()
        if not _is_product_like_url(href):
            continue
        entity_signal = normalize_for_match(f"{entity.visible_text} {href}")
        if _appliance_context_matches(entity_signal, appliance_type) or _url_has_appliance_hint(
            href,
            appliance_type,
        ):
            return entity
        gap = _span_gap(match_start, match_end, entity.start, entity.end)
        if gap <= 120 and _entity_names_known_merchant(entity):
            return entity
        if gap <= 80 and re.search(
            r"\b(?:ссылка|товар|купить|здесь|смотреть)\b",
            normalize_for_match(entity.visible_text),
        ) and _appliance_context_matches(normalize_for_match(block), appliance_type):
            return entity
    return None


def _span_gap(first_start: int, first_end: int, second_start: int, second_end: int) -> int:
    if first_start < second_end and first_end > second_start:
        return 0
    if second_end <= first_start:
        return first_start - second_end
    return second_start - first_end


APPLIANCE_URL_HINTS: dict[str, tuple[str, ...]] = {
    "built_in_refrigerator": ("holodil", "refriger", "fridge"),
    "refrigerator_unconfirmed": ("holodil", "refriger", "fridge"),
    "microwave": ("mikrovol", "microvol", "microwave", "svch"),
    "oven": ("duhov", "oven"),
    "cooktop": ("varochn", "cooktop", "hob", "indukcion", "induction"),
}


def _url_has_appliance_hint(href: str, appliance_type: str) -> bool:
    normalized = normalize_for_match(href)
    return any(token in normalized for token in APPLIANCE_URL_HINTS.get(appliance_type, ()))


def _entity_names_known_merchant(entity: SourceEntity) -> bool:
    haystack = normalize_for_match(
        f"{entity.visible_text} {urlparse(entity.href).netloc.casefold().removeprefix('www.')}"
    )
    return any(
        normalize_for_match(token) in haystack
        for _, tokens in MERCHANT_NAMES
        for token in tokens
    )


def _is_product_like_url(href: str) -> bool:
    parsed = urlparse(href)
    host = parsed.netloc.casefold().removeprefix("www.")
    if not host or host in {"t.me", "telegram.me", "instagram.com", "wa.me"}:
        return False
    path = parsed.path.strip("/")
    return bool(path and ("product" in path.casefold() or "/catalog/" in parsed.path.casefold() or len(path) > 12))


def _appliance_context_matches(text: str, appliance_type: str) -> bool:
    if appliance_type in {"built_in_refrigerator", "refrigerator_unconfirmed"}:
        pattern = REFRIGERATOR_PATTERN
    else:
        pattern = dict(APPLIANCE_PATTERNS)[appliance_type]
    return bool(pattern.search(text))


def _extract_merchant(
    block: str,
    entity: SourceEntity | None,
) -> tuple[str | None, str | None]:
    domain = None
    haystack = normalize_for_match(block)
    if entity:
        domain = urlparse(entity.href).netloc.casefold().removeprefix("www.") or None
        haystack += " " + normalize_for_match(f"{entity.visible_text} {domain or ''}")
    for merchant, tokens in MERCHANT_NAMES:
        if any(normalize_for_match(token) in haystack for token in tokens):
            return merchant, domain
    return (compact_whitespace(entity.visible_text) if entity else None), domain


def _extract_article_id(block: str) -> str | None:
    match = re.search(r"\b(?:арт(?:икул)?|article)\b\.?\s*[:№#-]?\s*([A-ZА-Я0-9][A-ZА-Я0-9._/-]{2,})", block, flags=re.IGNORECASE)
    return match.group(1).rstrip(".,;") if match else None


KNOWN_APPLIANCE_BRANDS = (
    "DEXP",
    "Weissgauff",
    "Haier",
    "Smeg",
    "Bosch",
    "Electrolux",
    "Gorenje",
    "Samsung",
    "LG",
    "Korting",
    "Kuppersberg",
    "Maunfeld",
)


def _extract_appliance_brand_model(block: str, product_url: str | None) -> tuple[str | None, str | None]:
    brand = None
    for candidate in KNOWN_APPLIANCE_BRANDS:
        if re.search(rf"\b{re.escape(candidate)}\b", block, flags=re.IGNORECASE):
            brand = candidate
            break
    model = None
    if product_url:
        slug = urlparse(product_url).path
        dexp = re.search(r"(?:^|[-_/])dexp[-_/]([a-z0-9]+)(?:[-_/]|$)", slug, flags=re.IGNORECASE)
        if dexp:
            brand = "DEXP"
            model = dexp.group(1).upper()
    if brand:
        explicit = re.search(
            rf"\b{re.escape(brand)}\s+([A-ZА-Я0-9][A-ZА-Я0-9._/-]{{3,}})\b",
            block,
            flags=re.IGNORECASE,
        )
        if explicit:
            candidate = explicit.group(1).upper()
            if candidate not in {"ДНС", "DNS", "ОТ"}:
                model = candidate
    return brand, model


def _classify_appliance_evidence(
    *,
    appliance_type: str,
    block: str,
    product_url: str | None,
    merchant: str | None,
    brand: str | None,
    model: str | None,
) -> tuple[str, str, str | None]:
    normalized = normalize_for_match(block)
    unconfirmed_refrigerator_note = (
        "Обычное упоминание холодильника; встраивание не подтверждено."
        if appliance_type == "refrigerator_unconfirmed"
        else None
    )
    if re.search(
        r"заказчик\w*\s+(?:куп|приобр|привез|передал)|"
        r"(?:куп|приобр|привез|передан)\w*\s+заказчик|"
        r"от\s+заказчик\w*[^.!?]{0,80}(?:привез|передал)|самостоятельн",
        normalized,
    ):
        return (
            "customer_supplied",
            "medium",
            _join_notes(
                "Техника приобретена заказчиком; это не рекомендация канала.",
                unconfirmed_refrigerator_note,
            ),
        )
    if product_url:
        return (
            "direct_product_link",
            "medium" if unconfirmed_refrigerator_note else "high",
            unconfirmed_refrigerator_note,
        )
    if brand and model:
        return "model_without_link", "medium", unconfirmed_refrigerator_note
    if merchant:
        return "merchant_text_only", "medium", unconfirmed_refrigerator_note
    if re.search(r"(?:совет|лучше|можно|важно|почему|как\s+выбрать|рекоменд)", normalized):
        return (
            "general_advice",
            "low",
            _join_notes(
                "Общий совет без подтвержденной покупки.",
                unconfirmed_refrigerator_note,
            ),
        )
    return "mentioned_no_source", "low", unconfirmed_refrigerator_note


def _join_notes(*values: str | None) -> str | None:
    present = [value for value in values if value]
    return " ".join(present) or None


MAKER_ALIASES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("Mebel.in", re.compile(r"\b(?:mebel[ ._-]*in|мебель\s+инн?)\b", flags=re.IGNORECASE), "confirmed_maker"),
    ("VERESK", re.compile(r"\b(?:veresk(?:_mebel)?|вереск)\b", flags=re.IGNORECASE), "confirmed_maker"),
    ("Guter Mebel", re.compile(r"\bguter[ ._-]*m[оo]bel\b", flags=re.IGNORECASE), "likely_maker"),
    ("Стильные кухни", re.compile(r"\bстильн\w*\s+кухн\w*\b", flags=re.IGNORECASE), "likely_maker"),
    ("Алюмика", re.compile(r"\bалюмик\w*\b", flags=re.IGNORECASE), "likely_maker"),
    ("Divan.ru", re.compile(r"\b(?:divan\.ru|диван\.ру)\b", flags=re.IGNORECASE), "retailer"),
    ("HOFF", re.compile(r"\bhoff\b", flags=re.IGNORECASE), "retailer"),
    ("Лемана Про", re.compile(r"\b(?:лемана\s+про|леруа\s+мерлен)\b", flags=re.IGNORECASE), "retailer"),
)

FURNITURE_CONTEXT_RE = re.compile(
    r"(?:мебел|кухн|шкаф|гардероб|стеллаж|комод|консол|тумб|кровать|диван|двер[ьи]-?купе|на\s+заказ)",
    flags=re.IGNORECASE,
)


def extract_furniture_maker_mentions(
    message: CanonicalMessage,
    entities: Sequence[SourceEntity],
    project_link: ProjectLink | None,
    channel_username: str,
) -> list[FurnitureMakerMention]:
    result: list[FurnitureMakerMention] = []
    seen: set[tuple[int, str, str]] = set()
    for start, end, raw_block in _paragraph_units(message.text_plain):
        block = compact_whitespace(raw_block)
        if not FURNITURE_CONTEXT_RE.search(block):
            continue
        nearby = [entity for entity in entities if entity.start < end and entity.end > start]
        candidates: list[tuple[str, str, str]] = []
        combined = " ".join([block, *(entity.visible_text for entity in nearby)])
        for normalized, pattern, classification in MAKER_ALIASES:
            match = pattern.search(combined)
            if match:
                candidates.append((match.group(0), normalized, classification))
        if not candidates and re.search(r"(?:мебел|кухн|шкаф|гардероб|кровать)\w*(?:[^\n]{0,80})\bна\s+заказ\b|\bна\s+заказ\b", block, flags=re.IGNORECASE):
            candidates.append(("Производитель не указан", "unnamed_custom_maker", "ambiguous"))

        for raw_name, normalized_name, classification in candidates:
            key = (message.message_id, normalized_name, block.casefold())
            if key in seen:
                continue
            seen.add(key)
            local_block, local_start, local_end = _local_maker_context(
                raw_block,
                paragraph_start=start,
                maker_name=normalized_name,
                raw_name=raw_name,
            )
            related_entities = _entities_for_maker(
                nearby,
                normalized_name,
                local_start=local_start,
                local_end=local_end,
            )
            contacts = _extract_local_contacts(related_entities, local_block, normalized_name)
            what = _extract_what_was_made(local_block, raw_name, normalized_name)
            result.append(
                FurnitureMakerMention(
                    source_message_id=message.message_id,
                    maker_name_raw=compact_whitespace(raw_name),
                    maker_name_normalized=normalized_name,
                    classification=classification,
                    person_name=_extract_contact_person(local_block),
                    phone=contacts[0],
                    telegram=contacts[1],
                    whatsapp=contacts[2],
                    instagram=contacts[3],
                    website=contacts[4],
                    what_was_made=what,
                    telegram_post_url=telegram_post_url(channel_username, message.message_id),
                    evidence_quote=compact_whitespace(local_block),
                    confidence="high" if classification in {"confirmed_maker", "retailer"} else "medium" if classification == "likely_maker" else "low",
                    project_post_id=project_link.project_post_id if project_link else None,
                    project_key=project_link.project_key if project_link else None,
                    project_name=project_link.project_name if project_link else None,
                    link_method=project_link.link_method if project_link else None,
                    link_confidence=project_link.link_confidence if project_link else None,
                )
            )
    return result


def _paragraph_units(text: str) -> Iterable[tuple[int, int, str]]:
    cursor = 0
    for match in re.finditer(r"\n\s*\n", text or ""):
        if match.start() > cursor:
            yield cursor, match.start(), text[cursor:match.start()]
        cursor = match.end()
    if cursor < len(text or ""):
        yield cursor, len(text), text[cursor:]


def _extract_local_contacts(
    entities: Sequence[SourceEntity],
    block: str,
    maker_name: str,
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    phone = None
    telegram = None
    whatsapp = None
    instagram = None
    website = None
    for entity in entities:
        href = entity.href.strip()
        parsed = urlparse(href)
        host = parsed.netloc.casefold().removeprefix("www.")
        if host == "wa.me" or "whatsapp" in host:
            digits = re.sub(r"\D", "", parsed.path)
            if not digits:
                digits = re.sub(r"\D", "", parse_qs(parsed.query).get("phone", [""])[0])
            whatsapp = f"https://wa.me/{digits}" if digits else href
            phone = phone or (f"+{digits}" if digits else None)
        elif host in {"t.me", "telegram.me"}:
            telegram = f"https://t.me/{parsed.path.strip('/')}" if parsed.path.strip("/") else href
            phone_match = re.search(r"\+(\d{8,15})", href)
            if phone_match and phone_match.group(1).startswith("7"):
                phone = phone or f"+{phone_match.group(1)}"
        elif "instagram.com" in host:
            instagram = href
        elif host:
            website = href
    if not phone:
        phone_match = re.search(r"(?:\+7|8)[\s()\-\u2011]*\d{3}[\s()\-\u2011]*\d{3}[\s\-\u2011]*\d{2}[\s\-\u2011]*\d{2}", block)
        if phone_match:
            phone = compact_whitespace(phone_match.group(0))
    if not instagram and maker_name == "Mebel.in" and re.search(r"(?:инст|instagram)[^@]{0,20}@mebel\.in", block, flags=re.IGNORECASE):
        instagram = "@mebel.in"
    return phone, telegram, whatsapp, instagram, website


def _maker_alias_pattern(maker_name: str) -> re.Pattern[str] | None:
    for normalized, pattern, _ in MAKER_ALIASES:
        if normalized == maker_name:
            return pattern
    return None


def _entities_for_maker(
    entities: Sequence[SourceEntity],
    maker_name: str,
    *,
    local_start: int,
    local_end: int,
) -> list[SourceEntity]:
    pattern = _maker_alias_pattern(maker_name)
    if pattern is None:
        return []
    result: list[SourceEntity] = []
    for entity in entities:
        if entity.start < local_end and entity.end > local_start:
            result.append(entity)
            continue
        if pattern.search(entity.visible_text):
            result.append(entity)
            continue
        parsed = urlparse(entity.href)
        target = normalize_for_match(f"{parsed.netloc} {parsed.path}")
        if maker_name == "Mebel.in" and "mebel.in" in target:
            result.append(entity)
        elif maker_name == "Guter Mebel" and "gutermebel" in target:
            result.append(entity)
    return result


def _local_maker_context(
    raw_block: str,
    *,
    paragraph_start: int,
    maker_name: str,
    raw_name: str,
) -> tuple[str, int, int]:
    pattern = _maker_alias_pattern(maker_name)
    lines: list[tuple[str, int, int]] = []
    cursor = 0
    for raw_line in raw_block.splitlines(keepends=True):
        content = raw_line.rstrip("\r\n")
        content_start = cursor
        cursor += len(raw_line)
        if content.strip():
            leading = len(content) - len(content.lstrip())
            lines.append((content.strip(), content_start + leading, content_start + len(content)))
    if cursor < len(raw_block):
        tail = raw_block[cursor:]
        if tail.strip():
            leading = len(tail) - len(tail.lstrip())
            lines.append((tail.strip(), cursor + leading, len(raw_block)))
    if not lines:
        return raw_block, paragraph_start, paragraph_start + len(raw_block)
    for index, (line, line_start, line_end) in enumerate(lines):
        match = pattern.search(line) if pattern else re.search(re.escape(raw_name), line, flags=re.IGNORECASE)
        if not match:
            continue
        if len(line) > 500:
            left = max(0, match.start() - 220)
            right = min(len(line), match.end() + 220)
            return (
                line[left:right],
                paragraph_start + line_start + left,
                paragraph_start + line_start + right,
            )
        selected_start = index
        selected_end = index
        prefix = line[: match.start()]
        if (
            index > 0
            and not FURNITURE_CONTEXT_RE.search(prefix)
            and FURNITURE_CONTEXT_RE.search(lines[index - 1][0])
            and len(line) < 100
        ):
            selected_start = index - 1
        if index + 1 < len(lines) and re.search(
            r"^\s*(?:контакт|менеджер|телефон|whatsapp|telegram|instagram|\+7|8\s*\(?\d{3})",
            lines[index + 1][0],
            flags=re.IGNORECASE,
        ):
            selected_end = index + 1
        selected = [value[0] for value in lines[selected_start : selected_end + 1]]
        return (
            "\n".join(selected),
            paragraph_start + lines[selected_start][1],
            paragraph_start + lines[selected_end][2],
        )
    return raw_block[:500], paragraph_start, paragraph_start + min(len(raw_block), 500)


def _extract_what_was_made(block: str, raw_name: str, maker_name: str) -> str | None:
    value = compact_whitespace(block)
    if raw_name and raw_name != "Производитель не указан":
        value = re.split(re.escape(raw_name), value, maxsplit=1, flags=re.IGNORECASE)[0]
    normalized = normalize_for_match(value)
    labels: list[str] = []
    rules = (
        ("кухня", r"\bкухн"),
        ("шкафы / гардеробная", r"\bшкаф|гардероб"),
        ("стеллаж", r"стеллаж"),
        ("комод", r"\bкомод"),
        ("консоль", r"консол"),
        ("тумба", r"\bтумб"),
        ("стеновые панели", r"(?:стенов|тв)\w*\s+панел|\bпанел"),
        ("двери-купе / перегородка", r"двер[ьи]-?купе|перегород"),
        ("кровать", r"\bкроват"),
        ("мебель на заказ", r"мебел\w*\s+на\s+заказ"),
        ("корпусная / иная мебель", r"\bмебел"),
    )
    for label, pattern in rules:
        if re.search(pattern, normalized):
            labels.append(label)
    allowed_by_maker = {
        "VERESK": {
            "шкафы / гардеробная",
            "стеллаж",
            "комод",
            "консоль",
            "тумба",
            "стеновые панели",
            "корпусная / иная мебель",
        },
        "Алюмика": {"двери-купе / перегородка", "стеновые панели"},
        "Guter Mebel": {"кровать", "шкафы / гардеробная", "корпусная / иная мебель"},
        "Стильные кухни": {"кухня"},
    }
    if maker_name in allowed_by_maker:
        labels = [label for label in labels if label in allowed_by_maker[maker_name]]
    if labels:
        return "; ".join(labels)
    return None


def _extract_contact_person(block: str) -> str | None:
    match = re.search(r"\b(?:менеджер|контакт)\s+([А-ЯЁ][а-яё]{2,})\b", block, flags=re.IGNORECASE)
    return match.group(1) if match else None


def build_father_query_index(
    *,
    facts_db: Path,
    canonical_db: Path,
    out_dir: Path | None = None,
    channel_username: str = "olya_homestaging",
) -> FatherQueryIndex:
    if not facts_db.exists():
        raise FileNotFoundError(f"facts database not found: {facts_db}")
    if not canonical_db.exists():
        raise FileNotFoundError(f"canonical database not found: {canonical_db}")

    messages = load_canonical_messages(canonical_db)
    variants = _load_variant_raw_jsons(canonical_db)
    entities_by_message = {
        message_id: extract_message_entities(message, variants.get(message_id, ()))
        for message_id, message in messages.items()
    }
    project_names_by_source = _load_project_names_by_source(facts_db)
    raw_wall_fact_counts = _load_raw_wall_fact_counts(facts_db)

    kitchen_facts = load_kitchen_facts(facts_db)
    context_facts = load_context_facts(facts_db)
    photos = load_photo_paths(canonical_db)
    kitchen_projects = build_kitchen_projects(
        kitchen_facts=kitchen_facts,
        context_facts=context_facts,
        messages=messages,
        photos_by_message=photos,
        channel_username=channel_username,
    )
    classify_projects(kitchen_projects)
    stage4_source_to_project: dict[int, int] = {}
    for project in kitchen_projects:
        for source_id in project.source_message_ids:
            stage4_source_to_project[source_id] = project.project_post_id
        stage4_source_to_project.setdefault(project.project_post_id, project.project_post_id)

    project_links: dict[int, ProjectLink] = {}
    for message_id, message in messages.items():
        project_links[message_id] = link_message_to_project(
            message,
            channel_username=channel_username,
            stage4_source_to_project=stage4_source_to_project,
            project_name=project_names_by_source.get(message_id),
            entities=entities_by_message.get(message_id, ()),
        )

    paint_mentions: list[WallPaintMention] = []
    appliance_mentions: list[ApplianceMention] = []
    maker_mentions: list[FurnitureMakerMention] = []
    for message_id, message in messages.items():
        project_link = project_links[message_id]
        paint_mentions.extend(
            extract_wall_paint_mentions(
                message,
                project_link,
                channel_username=channel_username,
            )
        )
        appliance_mentions.extend(
            extract_appliance_mentions(
                message,
                entities_by_message.get(message_id, ()),
                project_link,
                channel_username,
            )
        )
        maker_mentions.extend(
            extract_furniture_maker_mentions(
                message,
                entities_by_message.get(message_id, ()),
                project_link,
                channel_username,
            )
        )

    paint_mentions = [
        replace(
            mention,
            raw_fact_mentions=raw_wall_fact_counts.get(mention.color_code_normalized or "", 0),
        )
        for mention in paint_mentions
    ]
    relevant_ids = {
        *(mention.source_message_id for mention in paint_mentions),
        *(mention.source_message_id for mention in appliance_mentions),
        *(mention.source_message_id for mention in maker_mentions),
        *(source_id for project in kitchen_projects for source_id in project.source_message_ids),
    }
    review = [
        project_links[source_id]
        for source_id in sorted(relevant_ids)
        if source_id in project_links and project_links[source_id].link_confidence != HIGH_LINK_CONFIDENCE
    ]
    return FatherQueryIndex(
        messages=messages,
        entities_by_message=entities_by_message,
        project_links=project_links,
        wall_paint_mentions=paint_mentions,
        appliance_mentions=appliance_mentions,
        furniture_maker_mentions=maker_mentions,
        kitchen_projects=kitchen_projects,
        project_linkage_review=review,
        raw_wall_fact_counts=raw_wall_fact_counts,
        facts_db=facts_db,
        canonical_db=canonical_db,
    )


def _load_variant_raw_jsons(canonical_db: Path) -> dict[int, tuple[str, ...]]:
    uri = f"{canonical_db.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_variants'"
        ).fetchone()
        if not table_exists:
            return {}
        rows = conn.execute(
            "SELECT telegram_message_id, raw_json FROM message_variants ORDER BY telegram_message_id, id"
        ).fetchall()
    grouped: defaultdict[int, list[str]] = defaultdict(list)
    for message_id, raw_json in rows:
        grouped[int(message_id)].append(str(raw_json))
    return {message_id: tuple(values) for message_id, values in grouped.items()}


def _load_project_names_by_source(facts_db: Path) -> dict[int, str]:
    uri = f"{facts_db.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        rows = conn.execute(
            """
            SELECT source_message_id, project_name, COUNT(*) AS mention_count
            FROM extracted_facts
            WHERE project_name IS NOT NULL AND TRIM(project_name) <> ''
            GROUP BY source_message_id, project_name
            ORDER BY source_message_id, mention_count DESC, project_name
            """
        ).fetchall()
    result: dict[int, str] = {}
    for source_message_id, project_name, _ in rows:
        result.setdefault(int(source_message_id), str(project_name))
    return result


def _load_raw_wall_fact_counts(facts_db: Path) -> dict[str, int]:
    uri = f"{facts_db.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        rows = conn.execute(
            """
            SELECT color_code, COUNT(*)
            FROM extracted_facts
            WHERE category = 'wall_colors' AND color_code IS NOT NULL AND TRIM(color_code) <> ''
            GROUP BY color_code
            """
        ).fetchall()
    counts: Counter[str] = Counter()
    for raw_code, count in rows:
        counts[normalize_paint_code(str(raw_code))] += int(count)
    return dict(counts)


def rank_exact_paints(
    mentions: Sequence[WallPaintMention],
    raw_fact_counts: Mapping[str, int] | None = None,
) -> list[PaintRanking]:
    groups: defaultdict[str, list[WallPaintMention]] = defaultdict(list)
    for mention in mentions:
        if mention.link_confidence != HIGH_LINK_CONFIDENCE or not mention.project_key:
            continue
        key = _paint_ranking_key(mention)
        if key:
            groups[key].append(mention)

    rows: list[tuple[str, list[WallPaintMention]]] = sorted(
        groups.items(),
        key=lambda item: (
            -len({mention.project_key for mention in item[1]}),
            -len({mention.source_message_id for mention in item[1]}),
            item[0],
        ),
    )
    rankings: list[PaintRanking] = []
    for rank, (key, values) in enumerate(rows, start=1):
        manufacturers = _unique(value.manufacturer for value in values)
        products = _unique(value.product_line for value in values)
        projects = _unique(value.project_name or value.project_key for value in values)
        raw_count = (raw_fact_counts or {}).get(key, max((value.raw_fact_mentions for value in values), default=0))
        unique_message_count = len({value.source_message_id for value in values})
        branded_message_count = len({value.source_message_id for value in values if value.manufacturer})
        notes = "Учтены только уникальные высокоуверенные ключи проекта/проектного поста."
        if len(manufacturers) > 1:
            notes += " В локальных источниках встречаются разные производители; они не были домыслены."
        elif not manufacturers:
            notes += " Производитель рядом с кодом не указан."
        elif branded_message_count < unique_message_count:
            notes += (
                f" Производитель назван рядом с кодом только в {branded_message_count} из "
                f"{unique_message_count} сообщений; для остальных он не предполагается."
            )
        linked_keys = {
            value.project_key
            for value in values
            if value.link_method in {"explicit_project_link", "stage4_project_post"}
        }
        proxy_keys = {
            value.project_key for value in values if value.link_method == "same_message"
        } - linked_keys
        notes += (
            f" Из них {len(linked_keys)} идентичностей подтверждены межпостовой связью, "
            f"{len(proxy_keys)} — самостоятельные проектные посты без безопасной межпостовой склейки."
        )
        rankings.append(
            PaintRanking(
                rank=rank,
                manufacturer="; ".join(manufacturers) or None,
                product_line="; ".join(products) or None,
                color_code=key,
                unique_projects=len({value.project_key for value in values}),
                unique_messages=unique_message_count,
                raw_fact_mentions=raw_count,
                example_projects=tuple(projects[:5]),
                confidence_notes=notes,
            )
        )
    return rankings


def _paint_ranking_key(mention: WallPaintMention) -> str | None:
    if mention.color_code_normalized:
        return mention.color_code_normalized
    if mention.product_line:
        return " ".join(value for value in (mention.manufacturer, mention.product_line) if value)
    return None


def rank_shade_families(mentions: Sequence[WallPaintMention]) -> list[ShadeRanking]:
    groups: defaultdict[str, list[WallPaintMention]] = defaultdict(list)
    for mention in mentions:
        if (
            mention.link_confidence == HIGH_LINK_CONFIDENCE
            and mention.project_key
            and mention.shade_family_method == "explicit_text"
            and mention.shade_family != "other"
        ):
            groups[mention.shade_family].append(mention)
    rows = sorted(
        groups.items(),
        key=lambda item: (
            -len({mention.project_key for mention in item[1]}),
            -len({mention.source_message_id for mention in item[1]}),
            item[0],
        ),
    )
    result: list[ShadeRanking] = []
    for rank, (family, values) in enumerate(rows, start=1):
        result.append(
            ShadeRanking(
                rank=rank,
                shade_family=family,
                unique_projects=len({value.project_key for value in values}),
                unique_messages=len({value.source_message_id for value in values}),
                representative_raw_descriptions=tuple(_unique(value.descriptive_color_raw for value in values)[:5]),
                representative_codes=tuple(_unique(value.color_code_normalized for value in values)[:5]),
                confidence_notes="Семейство учтено только при явном словесном описании оттенка в локальном контексте.",
            )
        )
    return result


def _unique(values: Iterable[str | None]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        clean = compact_whitespace(str(value))
        key = clean.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(clean)
    return result


def generate_father_query_index(
    *,
    facts_db: Path,
    canonical_db: Path,
    out_dir: Path,
    channel_username: str = "olya_homestaging",
    kitchen_palette_dir: Path | None = None,
) -> FatherQueryRunResult:
    index = build_father_query_index(
        facts_db=facts_db,
        canonical_db=canonical_db,
        out_dir=out_dir,
        channel_username=channel_username,
    )
    files = write_index_outputs(index, out_dir=out_dir, channel_username=channel_username)
    return FatherQueryRunResult(out_dir=out_dir, output_files=tuple(files), index=index)


def generate_father_wall_paints(
    *,
    facts_db: Path,
    canonical_db: Path,
    out_dir: Path,
    channel_username: str = "olya_homestaging",
    kitchen_palette_dir: Path = Path("outputs/kitchen_palette_report"),
) -> FatherQueryRunResult:
    index = build_father_query_index(
        facts_db=facts_db,
        canonical_db=canonical_db,
        out_dir=out_dir,
        channel_username=channel_username,
    )
    files = write_index_outputs(index, out_dir=out_dir, channel_username=channel_username)
    files.extend(
        write_wall_outputs(
            index,
            out_dir=out_dir,
            channel_username=channel_username,
            kitchen_palette_dir=kitchen_palette_dir,
        )
    )
    return FatherQueryRunResult(out_dir=out_dir, output_files=tuple(_unique_paths(files)), index=index)


def generate_father_appliances(
    *,
    facts_db: Path,
    canonical_db: Path,
    out_dir: Path,
    channel_username: str = "olya_homestaging",
    kitchen_palette_dir: Path | None = None,
) -> FatherQueryRunResult:
    index = build_father_query_index(
        facts_db=facts_db,
        canonical_db=canonical_db,
        out_dir=out_dir,
        channel_username=channel_username,
    )
    files = write_index_outputs(index, out_dir=out_dir, channel_username=channel_username)
    files.extend(write_appliance_outputs(index, out_dir=out_dir))
    return FatherQueryRunResult(out_dir=out_dir, output_files=tuple(_unique_paths(files)), index=index)


def generate_father_furniture_makers(
    *,
    facts_db: Path,
    canonical_db: Path,
    out_dir: Path,
    channel_username: str = "olya_homestaging",
    kitchen_palette_dir: Path | None = None,
) -> FatherQueryRunResult:
    index = build_father_query_index(
        facts_db=facts_db,
        canonical_db=canonical_db,
        out_dir=out_dir,
        channel_username=channel_username,
    )
    files = write_index_outputs(index, out_dir=out_dir, channel_username=channel_username)
    files.extend(write_maker_outputs(index, out_dir=out_dir))
    return FatherQueryRunResult(out_dir=out_dir, output_files=tuple(_unique_paths(files)), index=index)


def generate_all_father_queries(
    *,
    facts_db: Path,
    canonical_db: Path,
    out_dir: Path,
    channel_username: str = "olya_homestaging",
    kitchen_palette_dir: Path = Path("outputs/kitchen_palette_report"),
) -> FatherQueryRunResult:
    index = build_father_query_index(
        facts_db=facts_db,
        canonical_db=canonical_db,
        out_dir=out_dir,
        channel_username=channel_username,
    )
    files = write_index_outputs(index, out_dir=out_dir, channel_username=channel_username)
    files.extend(
        write_wall_outputs(
            index,
            out_dir=out_dir,
            channel_username=channel_username,
            kitchen_palette_dir=kitchen_palette_dir,
        )
    )
    files.extend(write_appliance_outputs(index, out_dir=out_dir))
    files.extend(write_maker_outputs(index, out_dir=out_dir))
    summary = out_dir / "father_queries_summary.md"
    summary.write_text(_build_summary_markdown(index), encoding="utf-8")
    files.append(summary)
    return FatherQueryRunResult(out_dir=out_dir, output_files=tuple(_unique_paths(files)), index=index)


def write_index_outputs(
    index: FatherQueryIndex,
    *,
    out_dir: Path,
    channel_username: str,
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    sqlite_path = out_dir / "father_queries.sqlite"
    _write_index_sqlite(index, sqlite_path)
    review_path = out_dir / "project_linkage_review.csv"
    _write_project_linkage_review(index.project_linkage_review, review_path)
    validation_path = out_dir / "source_link_validation.csv"
    _write_source_link_validation(index, validation_path, channel_username=channel_username)
    return [sqlite_path, review_path, validation_path]


def _write_index_sqlite(index: FatherQueryIndex, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # A fresh derived database keeps page layout and file hashes reproducible;
    # only this Stage 6 output is replaced, never either source database.
    if path.exists():
        path.unlink()
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            DROP TABLE IF EXISTS project_links;
            DROP TABLE IF EXISTS wall_paint_mentions;
            DROP TABLE IF EXISTS appliance_mentions;
            DROP TABLE IF EXISTS furniture_maker_mentions;

            CREATE TABLE project_links (
                source_message_id INTEGER PRIMARY KEY,
                project_post_id INTEGER,
                project_key TEXT,
                project_name TEXT,
                object_name TEXT,
                street TEXT,
                designer TEXT,
                link_method TEXT NOT NULL,
                link_confidence TEXT NOT NULL,
                evidence TEXT NOT NULL
            );
            CREATE TABLE wall_paint_mentions (
                id INTEGER PRIMARY KEY,
                source_message_id INTEGER NOT NULL,
                project_post_id INTEGER,
                project_key TEXT,
                project_name TEXT,
                manufacturer TEXT,
                product_line TEXT,
                color_code_raw TEXT,
                color_code_normalized TEXT,
                descriptive_color_raw TEXT,
                shade_family TEXT NOT NULL,
                shade_family_method TEXT NOT NULL,
                evidence_quote TEXT NOT NULL,
                telegram_post_url TEXT NOT NULL,
                link_method TEXT,
                link_confidence TEXT,
                raw_fact_mentions INTEGER NOT NULL
            );
            CREATE TABLE appliance_mentions (
                id INTEGER PRIMARY KEY,
                source_message_id INTEGER NOT NULL,
                project_post_id INTEGER,
                project_key TEXT,
                project_name TEXT,
                link_method TEXT,
                link_confidence TEXT,
                appliance_type TEXT NOT NULL,
                brand TEXT,
                model TEXT,
                article_id TEXT,
                merchant TEXT,
                merchant_domain TEXT,
                product_url TEXT,
                telegram_post_url TEXT NOT NULL,
                evidence_quote TEXT NOT NULL,
                evidence_class TEXT NOT NULL,
                confidence TEXT NOT NULL,
                notes TEXT
            );
            CREATE TABLE furniture_maker_mentions (
                id INTEGER PRIMARY KEY,
                source_message_id INTEGER NOT NULL,
                project_post_id INTEGER,
                project_key TEXT,
                project_name TEXT,
                link_method TEXT,
                link_confidence TEXT,
                maker_name_raw TEXT NOT NULL,
                maker_name_normalized TEXT NOT NULL,
                classification TEXT NOT NULL,
                person_name TEXT,
                phone TEXT,
                telegram TEXT,
                whatsapp TEXT,
                instagram TEXT,
                website TEXT,
                what_was_made TEXT,
                telegram_post_url TEXT NOT NULL,
                evidence_quote TEXT NOT NULL,
                confidence TEXT NOT NULL
            );
            """
        )
        relevant_ids = {
            *(mention.source_message_id for mention in index.wall_paint_mentions),
            *(mention.source_message_id for mention in index.appliance_mentions),
            *(mention.source_message_id for mention in index.furniture_maker_mentions),
            *(source_id for project in index.kitchen_projects for source_id in project.source_message_ids),
        }
        conn.executemany(
            "INSERT INTO project_links VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    link.source_message_id,
                    link.project_post_id,
                    link.project_key,
                    link.project_name,
                    link.object_name,
                    link.street,
                    link.designer,
                    link.link_method,
                    link.link_confidence,
                    link.evidence,
                )
                for source_id, link in sorted(index.project_links.items())
                if source_id in relevant_ids
            ],
        )
        conn.executemany(
            """
            INSERT INTO wall_paint_mentions (
                source_message_id, project_post_id, project_key, project_name,
                manufacturer, product_line, color_code_raw, color_code_normalized,
                descriptive_color_raw, shade_family, shade_family_method,
                evidence_quote, telegram_post_url, link_method, link_confidence,
                raw_fact_mentions
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    item.source_message_id,
                    item.project_post_id,
                    item.project_key,
                    item.project_name,
                    item.manufacturer,
                    item.product_line,
                    item.color_code_raw,
                    item.color_code_normalized,
                    item.descriptive_color_raw,
                    item.shade_family,
                    item.shade_family_method,
                    item.evidence_quote,
                    item.telegram_post_url,
                    item.link_method,
                    item.link_confidence,
                    item.raw_fact_mentions,
                )
                for item in index.wall_paint_mentions
            ],
        )
        conn.executemany(
            """
            INSERT INTO appliance_mentions (
                source_message_id, project_post_id, project_key, project_name,
                link_method, link_confidence, appliance_type,
                brand, model, article_id, merchant, merchant_domain, product_url,
                telegram_post_url, evidence_quote, evidence_class, confidence, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    item.source_message_id,
                    item.project_post_id,
                    item.project_key,
                    item.project_name,
                    item.link_method,
                    item.link_confidence,
                    item.appliance_type,
                    item.brand,
                    item.model,
                    item.article_id,
                    item.merchant,
                    item.merchant_domain,
                    item.product_url,
                    item.telegram_post_url,
                    item.evidence_quote,
                    item.evidence_class,
                    item.confidence,
                    item.notes,
                )
                for item in index.appliance_mentions
            ],
        )
        conn.executemany(
            """
            INSERT INTO furniture_maker_mentions (
                source_message_id, project_post_id, project_key, project_name,
                link_method, link_confidence, maker_name_raw,
                maker_name_normalized, classification, person_name, phone,
                telegram, whatsapp, instagram, website, what_was_made,
                telegram_post_url, evidence_quote, confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    item.source_message_id,
                    item.project_post_id,
                    item.project_key,
                    item.project_name,
                    item.link_method,
                    item.link_confidence,
                    item.maker_name_raw,
                    item.maker_name_normalized,
                    item.classification,
                    item.person_name,
                    item.phone,
                    item.telegram,
                    item.whatsapp,
                    item.instagram,
                    item.website,
                    item.what_was_made,
                    item.telegram_post_url,
                    item.evidence_quote,
                    item.confidence,
                )
                for item in index.furniture_maker_mentions
            ],
        )


def _write_project_linkage_review(links: Sequence[ProjectLink], path: Path) -> None:
    fields = [
        "source_message_id",
        "project_post_id",
        "project_key",
        "project_name",
        "object_name",
        "street",
        "designer",
        "link_method",
        "link_confidence",
        "evidence",
    ]
    _write_csv(path, fields, ({field: getattr(link, field) or "" for field in fields} for link in links))


def _write_source_link_validation(
    index: FatherQueryIndex,
    path: Path,
    *,
    channel_username: str,
) -> None:
    records: defaultdict[int, set[str]] = defaultdict(set)
    for item in index.wall_paint_mentions:
        records[item.source_message_id].add("wall_paint")
    for item in index.appliance_mentions:
        records[item.source_message_id].add("appliance")
    for item in index.furniture_maker_mentions:
        records[item.source_message_id].add("furniture_maker")
    for project in index.kitchen_projects:
        for source_id in project.source_message_ids:
            records[source_id].add("kitchen_project")
        records[project.project_post_id].add("kitchen_project")
    rows = [
        {
            "source_message_id": message_id,
            "record_types": ";".join(sorted(record_types)),
            "candidate_telegram_url": telegram_post_url(channel_username, message_id),
            "status": LINK_STATUS,
            "notes": "Кандидатная ссылка; требуется ручная проверка.",
        }
        for message_id, record_types in sorted(records.items())
    ]
    _write_csv(
        path,
        ["source_message_id", "record_types", "candidate_telegram_url", "status", "notes"],
        rows,
    )


def _write_csv(path: Path, fields: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _csv_value(row.get(field)) for field in fields})


def _csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return int(value)
    return value


def _unique_paths(paths: Iterable[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        result.append(path)
    return result


KITCHEN_WALL_FIELDS = [
    "project_key",
    "example_id",
    "project_post_id",
    "source_message_ids",
    "object_name",
    "street",
    "designer",
    "facade_combination",
    "wood_neutral_subfamily",
    "wall_manufacturer",
    "wall_product_line",
    "wall_color_codes",
    "wall_descriptions",
    "shade_families",
    "project_telegram_url",
    "source_telegram_urls",
    "kitchen_evidence_quote",
    "wall_evidence_quotes",
    "wall_paint_evidence_json",
    "link_method",
    "link_confidence",
    "source_link_status",
    "contact_sheet_path",
]


def write_wall_outputs(
    index: FatherQueryIndex,
    *,
    out_dir: Path,
    channel_username: str,
    kitchen_palette_dir: Path,
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    wall_all = out_dir / "wall_paints_all.csv"
    _write_wall_mentions_csv(index.wall_paint_mentions, wall_all)
    exact_rankings = rank_exact_paints(index.wall_paint_mentions, index.raw_wall_fact_counts)
    shade_rankings = rank_shade_families(index.wall_paint_mentions)
    top_report = out_dir / "wall_paints_top3.md"
    top_report.write_text(
        _build_wall_rankings_markdown(index, exact_rankings, shade_rankings),
        encoding="utf-8",
    )

    project_rows = _build_kitchen_wall_rows(
        index,
        channel_username=channel_username,
        kitchen_palette_dir=kitchen_palette_dir,
    )
    projects_csv = out_dir / "kitchen_wall_color_projects.csv"
    _write_csv(projects_csv, KITCHEN_WALL_FIELDS, project_rows)
    projects_report = out_dir / "kitchen_wall_colors.md"
    projects_report.write_text(
        _build_kitchen_wall_markdown(project_rows, index),
        encoding="utf-8",
    )
    return [projects_report, projects_csv, top_report, wall_all]


def _write_wall_mentions_csv(mentions: Sequence[WallPaintMention], path: Path) -> None:
    fields = [
        "source_message_id",
        "project_post_id",
        "project_key",
        "project_name",
        "manufacturer",
        "product_line",
        "color_code_raw",
        "color_code_normalized",
        "descriptive_color_raw",
        "shade_family",
        "shade_family_method",
        "evidence_quote",
        "telegram_post_url",
        "link_method",
        "link_confidence",
        "raw_fact_mentions",
        "source_link_status",
    ]
    rows = []
    for item in sorted(
        mentions,
        key=lambda value: (value.source_message_id, value.color_code_normalized or "", value.product_line or ""),
    ):
        row = {field: getattr(item, field, "") or "" for field in fields}
        row["source_link_status"] = LINK_STATUS
        rows.append(row)
    _write_csv(path, fields, rows)


def _build_kitchen_wall_rows(
    index: FatherQueryIndex,
    *,
    channel_username: str,
    kitchen_palette_dir: Path,
) -> list[dict[str, Any]]:
    by_project: defaultdict[str, list[WallPaintMention]] = defaultdict(list)
    for mention in index.wall_paint_mentions:
        if mention.project_key and mention.link_confidence == HIGH_LINK_CONFIDENCE:
            by_project[mention.project_key].append(mention)

    rows: list[dict[str, Any]] = []
    for project in sorted(
        (item for item in index.kitchen_projects if item.palette_category_id == "wood_neutral"),
        key=lambda item: (item.date or "", item.project_post_id),
        reverse=True,
    ):
        key = f"telegram:{project.project_post_id}"
        paints = sorted(
            by_project.get(key, []),
            key=lambda item: (item.source_message_id, item.color_code_normalized or "", item.product_line or ""),
        )
        source_links = [
            telegram_post_url(channel_username, source_id)
            for source_id in _unique_ints([project.project_post_id, *project.source_message_ids])
        ]
        linkage = _best_project_link(index, project)
        contact_candidate = kitchen_palette_dir / "contact_sheets" / f"{project.example_id}.jpg"
        contact_path = str(contact_candidate) if contact_candidate.exists() else ""
        rows.append(
            {
                "project_key": key,
                "example_id": project.example_id,
                "project_post_id": project.project_post_id,
                "source_message_ids": ";".join(str(value) for value in project.source_message_ids),
                "object_name": project.object_name,
                "street": linkage.street if linkage else "",
                "designer": project.designer if project.designer_source == "credited_in_post" else "",
                "facade_combination": project.facade_finish_raw or "",
                "wood_neutral_subfamily": classify_wood_neutral_subfamily(project.facade_finish_raw or ""),
                "wall_manufacturer": "; ".join(_unique(item.manufacturer for item in paints)),
                "wall_product_line": "; ".join(_unique(item.product_line for item in paints)),
                "wall_color_codes": "; ".join(_unique(item.color_code_normalized for item in paints)),
                "wall_descriptions": "; ".join(_unique(item.descriptive_color_raw for item in paints)),
                "shade_families": "; ".join(
                    _unique(item.shade_family for item in paints if item.shade_family_method == "explicit_text")
                ),
                "project_telegram_url": telegram_post_url(channel_username, project.project_post_id),
                "source_telegram_urls": ";".join(source_links),
                "kitchen_evidence_quote": project.evidence_quotes[0] if project.evidence_quotes else "",
                "wall_evidence_quotes": " || ".join(_unique(item.evidence_quote for item in paints)),
                "wall_paint_evidence_json": json.dumps(
                    [_paint_claim_payload(item) for item in paints],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "_wall_paint_mentions": tuple(paints),
                "link_method": ";".join(_unique(item.link_method for item in paints)) or (linkage.link_method if linkage else "same_message"),
                "link_confidence": (
                    "high"
                    if paints or (linkage and linkage.link_confidence == "high")
                    else (linkage.link_confidence if linkage else "medium")
                ),
                "source_link_status": LINK_STATUS,
                "contact_sheet_path": contact_path,
            }
        )
    return rows


def _paint_claim_payload(item: WallPaintMention) -> dict[str, Any]:
    return {
        "source_message_id": item.source_message_id,
        "manufacturer": item.manufacturer,
        "product_line": item.product_line,
        "color_code": item.color_code_normalized,
        "description": item.descriptive_color_raw,
        "shade_family": item.shade_family if item.shade_family_method == "explicit_text" else None,
        "evidence_quote": item.evidence_quote,
        "telegram_post_url": item.telegram_post_url,
        "link_method": item.link_method,
        "link_confidence": item.link_confidence,
    }


def _best_project_link(index: FatherQueryIndex, project: KitchenProject) -> ProjectLink | None:
    candidates = [
        index.project_links[source_id]
        for source_id in _unique_ints([*project.source_message_ids, project.project_post_id])
        if source_id in index.project_links
    ]
    for method in ("explicit_project_link", "stage4_project_post", "same_message"):
        for link in candidates:
            if link.link_method == method and link.link_confidence == "high":
                return link
    return candidates[0] if candidates else None


def _unique_ints(values: Iterable[int]) -> list[int]:
    result: list[int] = []
    seen: set[int] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def classify_wood_neutral_subfamily(value: str) -> str:
    text = normalize_for_match(value)
    rules = (
        ("wood_cappuccino", ("капучино",)),
        ("wood_cashmere", ("кашемир",)),
        ("wood_latte", ("латте",)),
        ("wood_milk_offwhite", ("молоч", "меренг", "бел", "тальк", "нубук")),
        ("wood_light_greige", ("грейдж", "greige", "теплый сер", "тёплый сер", "светло-сер")),
        ("wood_beige", ("беж", "крем", "песоч")),
    )
    for subfamily, tokens in rules:
        if any(token in text for token in tokens):
            return subfamily
    return "wood_other_warm_neutral"


SHADE_LABELS_RU = {
    "warm_beige": "тёплый бежевый",
    "greige": "грейдж / тёплый серо-бежевый",
    "warm_white_milk": "тёплый белый / молочный",
    "cool_white": "холодный белый",
    "light_gray": "светло-серый",
    "gray": "серый",
    "green": "зелёный",
    "blue": "синий / голубой",
    "terracotta": "терракотовый",
    "brown": "коричневый",
    "other": "другое / не определено",
}

SUBFAMILY_LABELS_RU = {
    "wood_cappuccino": "дерево + капучино",
    "wood_cashmere": "дерево + кашемир",
    "wood_beige": "дерево + бежевый",
    "wood_latte": "дерево + латте",
    "wood_milk_offwhite": "дерево + молочный / светлый",
    "wood_light_greige": "дерево + светлый грейдж",
    "wood_other_warm_neutral": "дерево + другой тёплый нейтральный",
}


def _build_wall_rankings_markdown(
    index: FatherQueryIndex,
    exact: Sequence[PaintRanking],
    shades: Sequence[ShadeRanking],
) -> str:
    exact_mentions = [
        mention
        for mention in index.wall_paint_mentions
        if mention.project_key
        and mention.link_confidence == HIGH_LINK_CONFIDENCE
        and (mention.color_code_normalized or mention.product_line)
    ]
    exact_projects = {mention.project_key for mention in exact_mentions}
    linked_exact_projects = {
        mention.project_key
        for mention in exact_mentions
        if mention.link_method in {"explicit_project_link", "stage4_project_post"}
    }
    same_message_exact_projects = {
        mention.project_key for mention in exact_mentions if mention.link_method == "same_message"
    } - linked_exact_projects
    shade_projects = {
        mention.project_key
        for mention in index.wall_paint_mentions
        if mention.project_key
        and mention.link_confidence == HIGH_LINK_CONFIDENCE
        and mention.shade_family_method == "explicit_text"
        and mention.shade_family != "other"
    }
    lines = [
        "# Самые частые краски и оттенки стен",
        "",
        (
            f"В замороженном архиве нашлось {len(exact_projects)} высокоуверенных ключей проекта/проектного поста с "
            f"точным кодом или явно названным продуктом. Межпостовая связь подтверждает "
            f"{len(linked_exact_projects)} {_ru_count_word(len(linked_exact_projects), 'ключ', 'ключа', 'ключей')}; ещё "
            f"{len(same_message_exact_projects)} самостоятельных проектных постов считаются "
            "отдельно, потому что безопасно склеить их с другими постами нельзя. Поэтому числа ниже — консервативные "
            "проектные ключи, а не обещание точного количества разных квартир. Повторы внутри одного подтверждённого "
            f"ключа считаются один раз. Рейтинг оттенков опирается только на явные словесные описания; таких ключей {len(shade_projects)}. "
            "Непрозрачный код без словесного цвета не использовался для угадывания оттенка."
        ),
        "",
        "## Топ точных кодов и продуктов",
        "",
        "| Место | Производитель / продукт | Точный код | Ключей проекта/поста | Сообщений | Что важно |",
        "|---:|---|---|---:|---:|---|",
    ]
    for row in exact[:3]:
        product = " / ".join(value for value in (row.manufacturer, row.product_line) if value) or "не указан рядом с кодом"
        lines.append(
            f"| {row.rank} | {_md(product)} | `{_md(row.color_code)}` | {row.unique_projects} | "
            f"{row.unique_messages} | {_md(row.confidence_notes)} |"
        )
    if not exact:
        lines.append("| — | Недостаточно данных | — | — | — | Надёжный рейтинг не сформирован |")
    lines.extend(["", "## Примеры доказательств для точных кодов", ""])
    for row in exact[:3]:
        values = [
            mention
            for mention in exact_mentions
            if _paint_ranking_key(mention) == row.color_code
        ]
        values.sort(key=lambda item: (not bool(item.manufacturer or item.product_line), item.source_message_id))
        lines.extend([f"### {row.rank}. `{_md(row.color_code)}`", ""])
        for item in _distinct_paint_sources(values, limit=3):
            local_product = " / ".join(
                value for value in (item.manufacturer, item.product_line) if value
            ) or "производитель рядом не указан"
            project_label = item.project_name or f"проектный пост {item.project_post_id or item.source_message_id}"
            lines.append(
                f"- {_md(project_label)} — {_md(local_product)}; «{_md(_short(item.evidence_quote, 260))}» "
                f"([пост {item.source_message_id}]({item.telegram_post_url}), связь `{_md(item.link_method or 'не указана')}`)."
            )
        lines.append("")
    lines.extend(
        [
            "",
            "## Топ словесных семейств оттенков",
            "",
            "| Место | Семейство | Ключей проекта/поста | Сообщений | Примеры описаний | Примеры кодов |",
            "|---:|---|---:|---:|---|---|",
        ]
    )
    for row in shades[:3]:
        lines.append(
            f"| {row.rank} | {_md(SHADE_LABELS_RU.get(row.shade_family, row.shade_family))} | "
            f"{row.unique_projects} | {row.unique_messages} | "
            f"{_md('; '.join(row.representative_raw_descriptions) or '—')} | "
            f"{_md('; '.join(row.representative_codes) or '—')} |"
        )
    if not shades:
        lines.append("| — | Недостаточно явных описаний | — | — | — | — |")
    lines.extend(["", "## Примеры доказательств для семейств оттенков", ""])
    for row in shades[:3]:
        values = [
            mention
            for mention in index.wall_paint_mentions
            if mention.project_key
            and mention.link_confidence == HIGH_LINK_CONFIDENCE
            and mention.shade_family_method == "explicit_text"
            and mention.shade_family == row.shade_family
        ]
        values.sort(key=lambda item: item.source_message_id)
        lines.extend(
            [
                f"### {row.rank}. {_md(SHADE_LABELS_RU.get(row.shade_family, row.shade_family))}",
                "",
            ]
        )
        for item in _distinct_paint_sources(values, limit=3):
            code = item.color_code_normalized or item.product_line or "без точного кода"
            lines.append(
                f"- `{_md(code)}`: «{_md(_short(item.evidence_quote, 260))}» "
                f"([пост {item.source_message_id}]({item.telegram_post_url}))."
            )
        lines.append("")
    lines.extend(
        [
            "",
            "## Как читать результат",
            "",
            "Точные коды не объединялись по похожему названию оттенка. Производитель указан только тогда, когда он находится рядом с конкретным кодом или продуктом в исходном тексте. Полная построчная provenance находится в `wall_paints_all.csv`. Все ссылки на Telegram являются кандидатными и требуют ручной проверки.",
            "",
        ]
    )
    return "\n".join(lines)


def _distinct_paint_sources(
    values: Sequence[WallPaintMention],
    *,
    limit: int,
) -> list[WallPaintMention]:
    result: list[WallPaintMention] = []
    seen: set[int] = set()
    for item in values:
        if item.source_message_id in seen:
            continue
        seen.add(item.source_message_id)
        result.append(item)
        if len(result) >= limit:
            break
    return result


def _build_kitchen_wall_markdown(rows: Sequence[Mapping[str, Any]], index: FatherQueryIndex) -> str:
    with_paint = sum(bool(row.get("wall_color_codes") or row.get("wall_product_line")) for row in rows)
    lines = [
        "# Двухцветные кухни: дерево + светлый тёплый нейтральный",
        "",
        (
            f"В архиве найдено {len(rows)} кухонных проектов, которые проходят детерминированное правило «дерево + светлый/тёплый нейтральный фасад». "
            f"Для {with_paint} из них в связанных постах найден точный код или явно названный продукт для стен. "
            "Проекты не объединялись только по названию ЖК: основой служит ссылка статьи на проект либо собственный message_id поста. "
            "Если краска не найдена, это означает отсутствие надёжного локального свидетельства в замороженном архиве, а не рекомендацию выбрать цвет самостоятельно."
        ),
        "",
        "## Проекты",
        "",
        "| Проект | Фасады | Подгруппа | Стены | Дизайнер | Источник |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows:
        paint_mentions = tuple(row.get("_wall_paint_mentions") or ())
        paint = (
            "; ".join(_unique(_paint_claim_display(item) for item in paint_mentions))
            or "в связанных постах не найдено"
        )
        source = f"[Telegram]({row['project_telegram_url']})"
        lines.append(
            f"| **{_md(str(row['example_id']))} — {_md(str(row['object_name']))}** | "
            f"{_md(str(row['facade_combination']))} | "
            f"{_md(SUBFAMILY_LABELS_RU.get(str(row['wood_neutral_subfamily']), str(row['wood_neutral_subfamily'])))} | "
            f"{_md(paint)} | {_md(str(row.get('designer') or 'не указан'))} | {source} |"
        )
    lines.extend(["", "## Доказательства по проектам", ""])
    for row in rows:
        paint_mentions = tuple(row.get("_wall_paint_mentions") or ())
        paint_summary = (
            "; ".join(_unique(_paint_claim_display(item) for item in paint_mentions))
            or "надёжное упоминание не найдено"
        )
        lines.extend(
            [
                f"### {_md(str(row['example_id']))} — {_md(str(row['object_name']))}",
                "",
                f"- Фасады: {_md(str(row['facade_combination']))}.",
                f"- Стены: {_md(paint_summary)}.",
                f"- Связь: `{_md(str(row['link_method']))}`, уверенность — {_md(str(row['link_confidence']))}.",
                f"- Источники: {_source_links_markdown(str(row['source_telegram_urls']))}.",
                f"- Цитата о кухне: «{_md(_short(str(row.get('kitchen_evidence_quote') or ''), 420))}»",
            ]
        )
        if paint_mentions:
            lines.append("- Доказательства по краске (каждый код связан со своим локальным источником):")
            for item in paint_mentions:
                lines.append(
                    f"  - {_md(_paint_claim_display(item))}: «{_md(_short(item.evidence_quote, 320))}» "
                    f"([пост {item.source_message_id}]({item.telegram_post_url}))."
                )
        else:
            lines.append("- Доказательство по краске: не найдено.")
        if row.get("contact_sheet_path"):
            lines.append(f"- Контактный лист Stage 4: `{_md(str(row['contact_sheet_path']))}`.")
        lines.append("")
    lines.extend(
        [
            "## Ограничения",
            "",
            "Архив заморожен примерно на 17 мая 2026 года. Изображения повторно не анализировались; существующие контактные листы Stage 4 приведены только для ручной проверки. Все Telegram-ссылки имеют статус `unverified_requires_manual_verification`.",
            "",
        ]
    )
    return "\n".join(lines)


def _paint_claim_display(item: WallPaintMention) -> str:
    product = " / ".join(value for value in (item.manufacturer, item.product_line) if value)
    paint = item.color_code_normalized or ""
    description = item.descriptive_color_raw or ""
    parts = [value for value in (product, paint, description) if value]
    return " — ".join(parts) or "краска без уточнения"


def _md(value: str) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _short(value: str, limit: int) -> str:
    clean = compact_whitespace(value)
    return clean if len(clean) <= limit else clean[: limit - 1].rstrip() + "…"


def _source_links_markdown(value: str) -> str:
    return ", ".join(
        f"[пост {url.rsplit('/', 1)[-1]}]({url})"
        for url in value.split(";")
        if url
    )


def write_appliance_outputs(index: FatherQueryIndex, *, out_dir: Path) -> list[Path]:
    csv_path = out_dir / "built_in_appliances.csv"
    fields = [
        "appliance_type",
        "brand",
        "model",
        "article_id",
        "merchant",
        "merchant_domain",
        "product_url",
        "source_message_id",
        "project_post_id",
        "project_key",
        "project_name",
        "link_method",
        "link_confidence",
        "telegram_post_url",
        "evidence_quote",
        "evidence_class",
        "confidence",
        "notes",
        "source_link_status",
    ]
    rows = []
    for item in sorted(index.appliance_mentions, key=lambda value: (value.appliance_type, value.source_message_id)):
        row = {field: getattr(item, field, "") or "" for field in fields}
        row["source_link_status"] = LINK_STATUS
        rows.append(row)
    _write_csv(csv_path, fields, rows)
    md_path = out_dir / "built_in_appliances.md"
    md_path.write_text(_build_appliance_markdown(index.appliance_mentions), encoding="utf-8")
    return [md_path, csv_path]


APPLIANCE_LABELS_RU = {
    "built_in_refrigerator": "встроенный холодильник",
    "refrigerator_unconfirmed": "холодильник (встраивание не подтверждено)",
    "microwave": "микроволновая печь / СВЧ",
    "oven": "духовой шкаф",
    "cooktop": "варочная панель",
}

EVIDENCE_CLASS_LABELS_RU = {
    "direct_product_link": "прямая товарная ссылка",
    "merchant_text_only": "магазин назван без товарной ссылки",
    "model_without_link": "модель без ссылки",
    "mentioned_no_source": "упоминание без источника покупки",
    "customer_supplied": "куплено заказчиком",
    "general_advice": "общий совет",
    "irrelevant": "нерелевантно",
}


def _build_appliance_markdown(mentions: Sequence[ApplianceMention]) -> str:
    direct = [item for item in mentions if item.evidence_class == "direct_product_link"]
    exact = [item for item in direct if item.model or item.article_id]
    built_in_fridge_models = [
        item
        for item in mentions
        if item.appliance_type == "built_in_refrigerator"
        and item.model
        and re.search(r"встро|встраив", normalize_for_match(item.evidence_quote))
    ]
    exact_microwaves = [item for item in mentions if item.appliance_type == "microwave" and item.model]
    merchant_sources: dict[str, set[int]] = defaultdict(set)
    for item in direct:
        if item.merchant:
            merchant_sources[item.merchant].add(item.source_message_id)
    merchant_counts = Counter(
        {merchant: len(source_ids) for merchant, source_ids in merchant_sources.items()}
    )
    lines = [
        "# Встроенная техника по сообщениям канала",
        "",
        (
            f"В замороженном архиве найдено {len(direct)} {_ru_count_word(len(direct), 'прямая товарная ссылка', 'прямые товарные ссылки', 'прямых товарных ссылок')}; "
            f"в {len(exact)} из них явно определена модель или артикул. "
            "Данных недостаточно, чтобы составить полезный рейтинг магазинов: одна ссылка не превращает магазин в регулярно подтверждённое место покупки. "
            f"Конкретных моделей именно встроенного холодильника найдено {len(built_in_fridge_models)}, конкретных моделей СВЧ — {len(exact_microwaves)}. "
            "Обычные холодильники без явного признака встраивания помечены отдельно. Упоминания техники, купленной заказчиком самостоятельно, и общие советы сохранены ниже, но не считаются рекомендациями Мирзабаевой."
        ),
        "",
        "## Прямые товарные ссылки",
        "",
    ]
    if direct:
        lines.extend(
            [
                "| Тип | Бренд / модель | Артикул | Магазин | Товар | Доказательство / оговорка | Telegram |",
                "|---|---|---|---|---|---|---|",
            ]
        )
        for item in direct:
            product = f"[прямая ссылка]({item.product_url})" if item.product_url else "—"
            evidence = f"«{_md(_appliance_relevant_quote(item, 220))}»"
            if item.notes:
                evidence += f"; {_md(item.notes)}"
            lines.append(
                f"| {_md(_appliance_display_label(item))} | "
                f"{_md(' '.join(value for value in (item.brand, item.model) if value) or 'не указаны')} | "
                f"{_md(item.article_id or '—')} | {_md(item.merchant or '—')} | {product} | "
                f"{evidence} | "
                f"[пост {item.source_message_id}]({item.telegram_post_url}) |"
            )
    else:
        lines.append("Прямых товарных ссылок по целевым упоминаниям в корпусе не найдено.")
    if not built_in_fridge_models:
        lines.extend(
            [
                "",
                "**Важно:** конкретной модели встроенного холодильника с надёжным подтверждением в архиве нет.",
            ]
        )
    lines.extend(["", "## Где покупали", ""])
    if len(merchant_counts) < 2 or sum(merchant_counts.values()) < 3:
        detail = ", ".join(
            f"{name}: {count} {_ru_count_word(count, 'сообщение', 'сообщения', 'сообщений')}"
            for name, count in merchant_counts.most_common()
        ) or "нет прямых ссылок"
        lines.append(
            f"Надёжный вывод о главных местах покупки сделать нельзя. Прямые ссылки по уникальным сообщениям: {detail}."
        )
    else:
        lines.append("Магазины по числу независимых сообщений с прямой товарной ссылкой:")
        for name, count in merchant_counts.most_common():
            lines.append(f"- {name}: {count} сообщений.")
    weaker = [item for item in mentions if item.evidence_class != "direct_product_link"]
    lines.extend(["", "## Остальные упоминания", ""])
    if weaker:
        lines.extend(
            [
                "| Тип | Класс свидетельства | Бренд / модель | Артикул | Магазин | Что сказано | Оговорка | Источник |",
                "|---|---|---|---|---|---|---|---|",
            ]
        )
        for item in weaker:
            lines.append(
                f"| {_md(_appliance_display_label(item))} | "
                f"{_md(EVIDENCE_CLASS_LABELS_RU.get(item.evidence_class, item.evidence_class))} | "
                f"{_md(' '.join(value for value in (item.brand, item.model) if value) or '—')} | "
                f"{_md(item.article_id or '—')} | {_md(item.merchant or '—')} | "
                f"{_md(_appliance_relevant_quote(item, 260))} | {_md(item.notes or '—')} | "
                f"[пост {item.source_message_id}]({item.telegram_post_url}) |"
            )
    else:
        lines.append("Других упоминаний не найдено.")
    lines.extend(
        [
            "",
            "Все Telegram-ссылки являются кандидатными и требуют ручной проверки. Архив не обновлялся и внешние магазины не проверялись.",
            "",
        ]
    )
    return "\n".join(lines)


def _appliance_display_label(item: ApplianceMention) -> str:
    return APPLIANCE_LABELS_RU.get(item.appliance_type, item.appliance_type)


def _appliance_relevant_quote(item: ApplianceMention, limit: int) -> str:
    """Center long legacy quotes on the appliance rather than their first words."""

    quote = compact_whitespace(item.evidence_quote)
    if len(quote) <= limit:
        return quote
    if item.appliance_type in {"built_in_refrigerator", "refrigerator_unconfirmed"}:
        pattern = REFRIGERATOR_PATTERN
    else:
        pattern = dict(APPLIANCE_PATTERNS)[item.appliance_type]
    match = pattern.search(quote)
    if not match:
        return _short(quote, limit)
    start = max(0, match.start() - limit // 3)
    end = min(len(quote), start + limit)
    excerpt = quote[start:end].strip()
    if start:
        excerpt = "…" + excerpt
    if end < len(quote):
        excerpt += "…"
    return excerpt


def write_maker_outputs(index: FatherQueryIndex, *, out_dir: Path) -> list[Path]:
    csv_path = out_dir / "furniture_makers.csv"
    fields = [
        "maker_name_raw",
        "maker_name_normalized",
        "classification",
        "person_name",
        "phone",
        "telegram",
        "whatsapp",
        "instagram",
        "website",
        "what_was_made",
        "source_message_id",
        "project_post_id",
        "project_key",
        "project_name",
        "link_method",
        "link_confidence",
        "telegram_post_url",
        "evidence_quote",
        "confidence",
        "source_link_status",
    ]
    rows = []
    for item in sorted(
        index.furniture_maker_mentions,
        key=lambda value: (value.classification, value.maker_name_normalized.casefold(), value.source_message_id),
    ):
        row = {field: getattr(item, field, "") or "" for field in fields}
        row["source_link_status"] = LINK_STATUS
        rows.append(row)
    _write_csv(csv_path, fields, rows)
    md_path = out_dir / "furniture_makers.md"
    md_path.write_text(
        _build_maker_markdown(index.furniture_maker_mentions, index.project_links),
        encoding="utf-8",
    )
    return [md_path, csv_path]


def _build_maker_markdown(
    mentions: Sequence[FurnitureMakerMention],
    project_links: Mapping[int, ProjectLink] | None = None,
) -> str:
    grouped: defaultdict[tuple[str, str], list[FurnitureMakerMention]] = defaultdict(list)
    for item in mentions:
        grouped[(item.classification, item.maker_name_normalized)].append(item)
    confirmed = [(name, values) for (classification, name), values in grouped.items() if classification == "confirmed_maker"]
    likely = [(name, values) for (classification, name), values in grouped.items() if classification == "likely_maker"]
    retailers = [(name, values) for (classification, name), values in grouped.items() if classification == "retailer"]
    ambiguous = [(name, values) for (classification, name), values in grouped.items() if classification == "ambiguous"]
    for collection in (confirmed, likely, retailers, ambiguous):
        collection.sort(key=lambda item: (-len({value.source_message_id for value in item[1]}), item[0].casefold()))
    confirmed_names = ", ".join(name for name, _ in confirmed) or "нет"
    lines = [
        "# Производители мебели на заказ",
        "",
        (
            f"В локальном архиве уверенно подтверждены {len(confirmed)} производителя: {confirmed_names}. "
            f"Ещё {len(likely)} {_ru_count_word(len(likely), 'название', 'названия', 'названий')} оставлены как вероятные производители, потому что контекст слабее или ассортимент не всегда означает индивидуальное изготовление. "
            "Обычные магазины вынесены в отдельный раздел и не считаются мебельщиками Мирзабаевой. "
            "Список основан только на постах канала и не является исчерпывающим."
        ),
        "",
        "## Подтверждённые производители",
        "",
    ]
    if confirmed:
        for name, values in confirmed:
            lines.extend(_maker_group_markdown(name, values, project_links or {}))
    else:
        lines.append("Подтверждённых производителей не найдено.")
    lines.extend(["", "## Вероятные производители", ""])
    if likely:
        for name, values in likely:
            lines.extend(_maker_group_markdown(name, values, project_links or {}))
    else:
        lines.append("Вероятных производителей с достаточным контекстом не найдено.")
    lines.extend(["", "## Магазины — не производители на заказ", ""])
    if retailers:
        lines.append(
            "Следующие названия встречаются рядом с мебелью, но в этом отчёте классифицированы как магазины/ритейлеры: "
            + ", ".join(name for name, _ in retailers)
            + "."
        )
        lines.append("")
        for name, values in retailers:
            lines.extend(_retailer_group_markdown(name, values, project_links or {}))
    else:
        lines.append("Отдельных ритейлеров в выборке не найдено.")
    lines.extend(["", "## Неоднозначные и неназванные", ""])
    if ambiguous:
        for name, values in ambiguous[:10]:
            example = _maker_representative_mentions(values, limit=1)[0]
            lines.append(
                f"- **{_md(name)}** — {_md(_short(example.evidence_quote, 240))} "
                f"([пост {example.source_message_id}]({example.telegram_post_url}))."
            )
    else:
        lines.append("Неоднозначных имён не найдено.")
    lines.extend(
        [
            "",
            "## Ограничения",
            "",
            "Комментарии Telegram в замороженном корпусе отсутствуют. В части постов сказано только «мебель/кухня на заказ» без названия исполнителя. Поэтому список доказательный, но не полный; контакты приведены только при локальной связи с конкретным мебельщиком. Все Telegram-ссылки требуют ручной проверки.",
            "",
        ]
    )
    return "\n".join(lines)


def _maker_group_markdown(
    name: str,
    values: Sequence[FurnitureMakerMention],
    project_links: Mapping[int, ProjectLink],
) -> list[str]:
    total_sources = {value.source_message_id for value in values}
    representatives = _maker_representative_mentions(values, limit=6)
    result = [f"### {_md(name)}", ""]
    result.append(f"- Сообщений с доказательством: {len(total_sources)}.")
    result.append(
        "- Ниже показаны наиболее информативные источники. Изделия, контактные лица "
        "и контакты относятся только к цитате и Telegram-посту в том же пункте."
    )
    for value in representatives:
        result.extend(
            _maker_evidence_markdown(
                value,
                project_links,
                what_label="Что сделали",
            )
        )
    if len(total_sources) > len({value.source_message_id for value in representatives}):
        result.append(
            "- Остальные строки сохранены в `furniture_makers.csv`; в краткий отчёт их "
            "контакты и изделия без собственной цитаты не объединялись."
        )
    result.append("")
    return result


def _retailer_group_markdown(
    name: str,
    values: Sequence[FurnitureMakerMention],
    project_links: Mapping[int, ProjectLink],
) -> list[str]:
    representative = _maker_representative_mentions(values, limit=1)[0]
    result = [f"### {_md(name)}", ""]
    result.append("- Классификация: магазин/ритейлер, не производитель мебели на заказ.")
    result.extend(
        _maker_evidence_markdown(
            representative,
            project_links,
            what_label="Что упомянуто",
            include_contacts=False,
        )
    )
    result.append(f"- Всего сообщений с таким названием: {len({value.source_message_id for value in values})}.")
    result.append("")
    return result


def _maker_representative_mentions(
    values: Sequence[FurnitureMakerMention],
    *,
    limit: int,
) -> list[FurnitureMakerMention]:
    richest_by_source: dict[int, FurnitureMakerMention] = {}
    for value in values:
        existing = richest_by_source.get(value.source_message_id)
        if existing is None or _maker_evidence_score(value) > _maker_evidence_score(existing):
            richest_by_source[value.source_message_id] = value
    ordered = sorted(
        richest_by_source.values(),
        key=lambda value: (
            -_maker_evidence_score(value)[0],
            -_maker_evidence_score(value)[1],
            -_maker_evidence_score(value)[2],
            -_maker_evidence_score(value)[3],
            value.source_message_id,
            value.evidence_quote.casefold(),
        ),
    )
    return ordered[:limit]


def _maker_evidence_score(value: FurnitureMakerMention) -> tuple[int, int, int, int]:
    contact_count = sum(
        bool(candidate)
        for candidate in (
            value.phone,
            value.telegram,
            value.whatsapp,
            value.instagram,
            value.website,
        )
    )
    return (
        contact_count,
        int(bool(value.person_name)),
        int(bool(value.what_was_made)),
        min(len(value.evidence_quote), 500),
    )


def _maker_evidence_markdown(
    value: FurnitureMakerMention,
    project_links: Mapping[int, ProjectLink],
    *,
    what_label: str,
    include_contacts: bool = True,
) -> list[str]:
    project_label = _maker_project_label(value, project_links)
    source_label = f"пост {value.source_message_id}"
    if project_label:
        source_label += f" — {project_label}"
    result = [f"- **[{_md(source_label)}]({value.telegram_post_url})**"]
    if value.what_was_made:
        result.append(f"  - {what_label}: {_md(value.what_was_made)}.")
    if value.person_name:
        result.append(f"  - Контактное лицо: {_md(value.person_name)}.")
    contacts = _maker_contact_claims(value) if include_contacts else []
    if contacts:
        result.append(f"  - Контакты из этого же сообщения: {'; '.join(contacts)}.")
    result.append(f"  - Цитата: «{_md(_short(value.evidence_quote, 420))}»")
    return result


def _maker_project_label(
    value: FurnitureMakerMention,
    project_links: Mapping[int, ProjectLink],
) -> str | None:
    link = project_links.get(value.source_message_id)
    project_label = (link.object_name if link else None) or (link.project_name if link else None)
    if not project_label or not re.search(
        r"(?:\bЖК\b|улиц|ул\.|проспект|бульвар|шоссе|набереж|квартал|парк)",
        project_label,
        flags=re.IGNORECASE,
    ):
        return None
    return project_label


def _maker_contact_claims(value: FurnitureMakerMention) -> list[str]:
    claims: list[str] = []
    for label, candidate in (
        ("телефон", value.phone),
        ("Telegram", value.telegram),
        ("WhatsApp", value.whatsapp),
        ("Instagram", value.instagram),
        ("сайт", value.website),
    ):
        if not candidate:
            continue
        rendered = f"[{_md(candidate)}]({candidate})" if candidate.startswith("http") else _md(candidate)
        claims.append(f"{label}: {rendered}")
    return claims


def _ru_count_word(value: int, one: str, few: str, many: str) -> str:
    last_two = value % 100
    if 11 <= last_two <= 14:
        return many
    last = value % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def _build_summary_markdown(index: FatherQueryIndex) -> str:
    exact = rank_exact_paints(index.wall_paint_mentions, index.raw_wall_fact_counts)
    shades = rank_shade_families(index.wall_paint_mentions)
    wood_projects = [item for item in index.kitchen_projects if item.palette_category_id == "wood_neutral"]
    direct_appliances = [item for item in index.appliance_mentions if item.evidence_class == "direct_product_link"]
    makers = {
        item.maker_name_normalized
        for item in index.furniture_maker_mentions
        if item.classification == "confirmed_maker"
    }
    likely = {
        item.maker_name_normalized
        for item in index.furniture_maker_mentions
        if item.classification == "likely_maker"
    }
    top_exact = ", ".join(f"{row.color_code} ({row.unique_projects})" for row in exact[:3]) or "нет"
    top_shades = ", ".join(
        f"{SHADE_LABELS_RU.get(row.shade_family, row.shade_family)} ({row.unique_projects})"
        for row in shades[:3]
    ) or "нет"
    return "\n".join(
        [
            "# Краткий итог целевых запросов",
            "",
            "Отчёты построены детерминированно по замороженному локальному архиву без сети, LLM, OCR и анализа изображений. Все выводы снабжены исходным message_id и кандидатной ссылкой Telegram.",
            "",
            f"- Кухни «дерево + светлый нейтральный»: {len(wood_projects)} проектов.",
            f"- Топ точных красок по уникальным ключам проекта/проектного поста: {top_exact}.",
            f"- Топ семейств оттенков по тем же консервативным ключам: {top_shades}.",
            f"- Прямые товарные ссылки на целевую технику: {len(direct_appliances)}.",
            f"- Подтверждённые мебельщики: {len(makers)}; вероятные: {len(likely)}.",
            "",
            "Подробности и ограничения находятся в отдельных отчётах. Telegram-ссылки не проходили ручную валидацию.",
            "",
        ]
    )
