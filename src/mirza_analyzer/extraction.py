from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .candidate_mining import (
    CONFIDENCE_SORT_VALUE as CANDIDATE_CONFIDENCE_SORT_VALUE,
    detect_project_article,
    load_source_posts,
    score_category_text,
)
from .categories import load_category_configs
from .extraction_patterns import (
    ARTICLE_RE,
    CONFIDENCE_SORT_VALUE,
    PRICE_RE,
    PROMO_RE,
    STRICT_CATEGORIES,
    count_item_triggers,
    detect_brand,
    extract_color_codes,
    find_vendor,
    normalize_for_match,
    parse_article_id,
    parse_price,
    parse_project_name,
    parse_promo_code,
    suspiciously_long_descriptor,
    without_promos,
)
from .utils import compact_whitespace, json_dumps, utc_now_iso


OutputFormat = Literal["markdown", "csv", "jsonl", "sqlite", "all"]
SourceScope = Literal["project_articles", "candidates", "all_text"]


@dataclass(frozen=True)
class ExtractedFact:
    source_message_id: int
    date: str | None
    source_scope: str
    project_name: str | None
    category: str
    item_type: str
    item_name: str | None
    vendor_raw: str | None
    vendor_normalized: str | None
    brand_raw: str | None
    brand_normalized: str | None
    model: str | None
    material: str | None
    finish: str | None
    color: str | None
    color_code: str | None
    article_id: str | None
    marketplace: str | None
    price_value: int | float | None
    price_currency: str | None
    price_unit: str | None
    promo_code: str | None
    room_context: str | None
    evidence_quote: str
    extraction_method: str
    confidence: str
    needs_review: bool
    notes: str | None
    source_text_hash: str
    created_at: str
    first_photo_path: str | None = None

    def to_json_dict(self, *, row_id: int | None = None) -> dict[str, Any]:
        data: dict[str, Any] = {
            "source_message_id": self.source_message_id,
            "date": self.date,
            "source_scope": self.source_scope,
            "project_name": self.project_name,
            "category": self.category,
            "item_type": self.item_type,
            "item_name": self.item_name,
            "vendor_raw": self.vendor_raw,
            "vendor_normalized": self.vendor_normalized,
            "brand_raw": self.brand_raw,
            "brand_normalized": self.brand_normalized,
            "model": self.model,
            "material": self.material,
            "finish": self.finish,
            "color": self.color,
            "color_code": self.color_code,
            "article_id": self.article_id,
            "marketplace": self.marketplace,
            "price_value": self.price_value,
            "price_currency": self.price_currency,
            "price_unit": self.price_unit,
            "promo_code": self.promo_code,
            "room_context": self.room_context,
            "evidence_quote": self.evidence_quote,
            "extraction_method": self.extraction_method,
            "confidence": self.confidence,
            "needs_review": int(self.needs_review),
            "notes": self.notes,
            "source_text_hash": self.source_text_hash,
            "created_at": self.created_at,
            "first_photo_path": self.first_photo_path,
        }
        if row_id is not None:
            data = {"id": row_id, **data}
        return data


@dataclass(frozen=True)
class FactExtractionResult:
    db_path: Path
    out_dir: Path
    generated_at: str
    source_scope: str
    total_text_posts: int
    total_posts_with_photos: int
    source_posts_processed: int
    facts: list[ExtractedFact]
    output_files: list[Path]


CSV_FIELDNAMES = [
    "id",
    "source_message_id",
    "date",
    "source_scope",
    "project_name",
    "category",
    "item_type",
    "item_name",
    "vendor_raw",
    "vendor_normalized",
    "brand_raw",
    "brand_normalized",
    "model",
    "material",
    "finish",
    "color",
    "color_code",
    "article_id",
    "marketplace",
    "price_value",
    "price_currency",
    "price_unit",
    "promo_code",
    "room_context",
    "evidence_quote",
    "extraction_method",
    "confidence",
    "needs_review",
    "notes",
    "source_text_hash",
    "created_at",
    "first_photo_path",
]


WORD_CHARS = r"A-Za-zА-Яа-яЁё0-9"


@dataclass(frozen=True)
class BundleDetection:
    evidence_quote: str
    item_heads: tuple[str, ...]
    category: str
    room_contexts: tuple[str, ...]


BUNDLE_ITEM_HEAD_PATTERNS: tuple[tuple[str, str, str], ...] = (
    ("кухня", "kitchens", r"кухн(?:я|и|ю|ей)?"),
    ("фасады", "kitchens", r"фасад(?:ы|а|ов)?"),
    ("фартук", "kitchens", r"фартук(?:а|и)?"),
    ("диван", "sofas", r"диван(?:ы|а)?"),
    ("софа", "sofas", r"софа|софы"),
    ("кровать", "sofas", r"кровать(?:-трансформер)?|кровати|кроватью"),
    ("стол", "tables", r"стол(?:ы|а|у|ом|е)?"),
    ("столик", "tables", r"столик(?:и|а|ом|е)?"),
    ("стул", "chairs", r"стул(?:ья|ьев|а|ом|е)?"),
    ("кресло", "chairs", r"кресл(?:о|а|ом|е)?"),
    ("тумба", "living_room_furniture", r"тумб(?:а|ы|у|ой|очка|очки|очку|очкой)?"),
    ("мебель", "living_room_furniture", r"мебел(?:ь|и|ью)?"),
    ("комод", "living_room_furniture", r"комод(?:ы|а|ом|е)?"),
    ("шкаф", "living_room_furniture", r"шкаф(?:ы|а|ом|е|чик|чики|чика|чиком)?"),
    ("стеллаж", "living_room_furniture", r"стеллаж(?:и|а|ом|е)?"),
    ("полка", "living_room_furniture", r"полк(?:а|и|у|ой)?"),
    ("обувница", "hallway", r"обувниц(?:а|ы|у|ей)?"),
    ("консоль", "hallway", r"консол(?:ь|и|ью)?"),
    ("вешалка", "hallway", r"вешалк(?:а|и|у|ой)?"),
    ("пуф", "hallway", r"пуф(?:ы|а|ом|е)?"),
    ("банкетка", "hallway", r"банкетк(?:а|и|у|ой)?"),
    ("зеркало", "hallway", r"зеркал(?:о|а|ом|е)?"),
    ("ковер", "living_room_furniture", r"ков(?:е|ё)р|ковр(?:ы|а|ов|ом|е)?"),
    ("картина", "living_room_furniture", r"картин(?:а|ы|у|ой)?"),
    ("подушка", "living_room_furniture", r"подушк(?:а|и|у|ой)?"),
    ("плед", "living_room_furniture", r"плед(?:ы|а|ом|е)?"),
    ("плитка", "flooring", r"плитк(?:а|и|у|ой)?"),
    ("керамогранит", "flooring", r"керамогранит(?:а|ом|е)?"),
    ("кварцвинил", "flooring", r"кварц[\s-]?винил|кварцвинил(?:а|ом|е)?"),
    ("ламинат", "flooring", r"ламинат(?:а|ом|е)?"),
    ("паркет", "flooring", r"паркет(?:а|ом|е)?"),
)

BUNDLE_WORD_RE = re.compile(
    rf"(?i)(?<![{WORD_CHARS}])(комплект|набор|гарнитур|все\s+для|всё\s+для)(?![{WORD_CHARS}])"
)
BUNDLE_LIST_RE = re.compile(r"[,;/]|\s+\+\s+|\s+и\s+", re.IGNORECASE)

LIGHTING_WORD_RE = re.compile(
    rf"(?i)(?<![{WORD_CHARS}])(лампа|светильник|люстра|бра|торшер|подсветка)(?![{WORD_CHARS}])"
)
DECOR_WORD_RE = re.compile(
    rf"(?i)(?<![{WORD_CHARS}])(картина|постер|ковер|ковёр|декор|ваза)(?![{WORD_CHARS}])"
)

ROOM_CONTEXT_PATTERNS: tuple[tuple[str, str], ...] = (
    ("bathroom", r"ванн\w*|сануз\w*|душев\w*|туалет\w*"),
    ("bedroom", r"спальн\w*"),
    ("kids_room", r"детск\w*"),
    ("wardrobe", r"гардероб\w*"),
    ("hallway", r"прихож\w*|коридор\w*"),
    ("living_room", r"гостин\w*"),
    ("kitchen", r"кухн\w*"),
)
NON_TARGET_REVIEW_CONTEXTS = {"bathroom", "bedroom", "kids_room", "wardrobe"}
NON_TARGET_REVIEW_CATEGORIES = {
    "hallway",
    "living_room_furniture",
    "tables",
    "sofas",
    "chairs",
    "kitchens",
}

SUSPICIOUS_DESCRIPTOR_PREFIXES = (
    "ья",
    "ье",
    "ью",
    "ом",
    "ик",
    "ьная",
    "ого",
    "ыми",
    "ая лампа",
    "ьная лампа",
)

ITEM_BOUNDARY_RE = re.compile(
    rf"(?i)(?<![{WORD_CHARS}])("
    r"кухн(?:я|и|ю|ей)?|фасад(?:ы|а|ов)?|фартук(?:а|и)?|стол(?:ы|а|у|ом|е)?|"
    r"столик(?:и|а|ом|е)?|диван(?:ы|а)?|софа|кресл(?:о|а)|стул(?:ья|ьев|а)?|"
    r"плитк(?:а|и|у)|керамогранит|кварц[\s-]?винил|ламинат|паркет|"
    r"прихож\w*|обувниц\w*|зеркал\w*|вешалк\w*|пуф\w*|банкетк\w*|консол\w*|"
    r"тв\s*тумб\w*|тумб\w*|комод\w*|стеллаж\w*|полк\w*|шкаф\w*|цвет\s+стен"
    rf")(?![{WORD_CHARS}])"
)


def extract_facts(
    db_path: Path,
    out_dir: Path,
    *,
    source: SourceScope = "project_articles",
    limit: int | None = None,
    min_project_article_score: int = 2,
    include_needs_review: bool = False,
    output_format: OutputFormat = "all",
    photos_per_post: int = 3,
) -> FactExtractionResult:
    out_dir.mkdir(parents=True, exist_ok=True)
    cleanup_legacy_extraction_outputs(out_dir)
    generated_at = utc_now_iso()
    posts, total_text_posts, total_posts_with_photos = load_source_posts(
        db_path,
        photos_per_post=photos_per_post,
    )
    selected_posts = select_source_posts(
        posts,
        source=source,
        limit=limit,
        min_project_article_score=min_project_article_score,
    )

    facts: list[ExtractedFact] = []
    for post in selected_posts:
        facts.extend(
            extract_facts_from_text(
                text=post.text_plain,
                source_message_id=post.telegram_message_id,
                date=post.date,
                source_scope=source,
                first_photo_path=post.first_photo_paths[0] if post.first_photo_paths else None,
                created_at=generated_at,
            )
        )

    facts = deduplicate_facts(facts)
    facts = sort_facts(facts)
    clean_facts = [fact for fact in facts if not fact.needs_review]
    review_facts = [fact for fact in facts if fact.needs_review]

    output_files: list[Path] = []
    if output_format in {"csv", "all"}:
        csv_specs = [
            ("extracted_facts_all.csv", facts),
            ("extracted_facts_clean.csv", clean_facts),
            ("extracted_facts_needs_review.csv", review_facts),
        ]
        for filename, rows in csv_specs:
            csv_path = out_dir / filename
            write_facts_csv(rows, csv_path)
            output_files.append(csv_path)
    if output_format in {"jsonl", "all"}:
        jsonl_path = out_dir / "extracted_facts.jsonl"
        write_facts_jsonl(facts, jsonl_path)
        output_files.append(jsonl_path)
    if output_format in {"sqlite", "all"}:
        sqlite_path = out_dir / "extracted_facts.sqlite"
        write_facts_sqlite(
            facts,
            sqlite_path,
            db_path=db_path,
            source_scope=source,
            settings={
                "limit": limit,
                "min_project_article_score": min_project_article_score,
                "include_needs_review": "deprecated_noop_all_rows_are_always_written"
                if include_needs_review
                else False,
                "format": output_format,
            },
            created_at=generated_at,
        )
        output_files.append(sqlite_path)
    result = FactExtractionResult(
        db_path=db_path,
        out_dir=out_dir,
        generated_at=generated_at,
        source_scope=source,
        total_text_posts=total_text_posts,
        total_posts_with_photos=total_posts_with_photos,
        source_posts_processed=len(selected_posts),
        facts=facts,
        output_files=output_files,
    )
    if output_format in {"markdown", "all"}:
        output_files.extend(write_markdown_outputs(result, out_dir))

    return FactExtractionResult(
        db_path=result.db_path,
        out_dir=result.out_dir,
        generated_at=result.generated_at,
        source_scope=result.source_scope,
        total_text_posts=result.total_text_posts,
        total_posts_with_photos=result.total_posts_with_photos,
        source_posts_processed=result.source_posts_processed,
        facts=result.facts,
        output_files=output_files,
    )


def cleanup_legacy_extraction_outputs(out_dir: Path) -> None:
    for legacy_path in [
        out_dir / "extracted_facts.csv",
        out_dir / "by_category" / "needs_review.md",
    ]:
        try:
            legacy_path.unlink(missing_ok=True)
        except OSError:
            pass


def select_source_posts(
    posts: list[Any],
    *,
    source: SourceScope,
    limit: int | None,
    min_project_article_score: int,
) -> list[Any]:
    selected: list[Any] = []
    for post in posts:
        if not post.text_plain.strip():
            continue
        if source == "all_text":
            selected.append(post)
            continue

        project_detection = detect_project_article(post.text_plain)
        if source == "project_articles":
            if (
                project_detection.is_project_article
                and project_detection.project_article_score >= min_project_article_score
            ):
                selected.append(post)
            continue

        if source == "candidates" and is_high_confidence_candidate_post(
            post.text_plain,
            photo_count=post.photo_count,
            project_detection=project_detection,
        ):
            selected.append(post)

    if limit is not None:
        return selected[:limit]
    return selected


def is_high_confidence_candidate_post(
    text: str,
    *,
    photo_count: int,
    project_detection: Any,
) -> bool:
    for category in STRICT_CATEGORIES:
        match = score_category_text(
            category,
            text,
            photo_count=photo_count,
            project_detection=project_detection,
        )
        if match and match.confidence_level == "high":
            return True
        if match and CANDIDATE_CONFIDENCE_SORT_VALUE[match.confidence_level] >= 2:
            return True
    return False


def word_re(pattern: str) -> re.Pattern[str]:
    return re.compile(rf"(?i)(?<![{WORD_CHARS}])(?:{pattern})(?![{WORD_CHARS}])")


def has_word_pattern(text: str, pattern: str) -> bool:
    return bool(word_re(pattern).search(text))


def detect_room_contexts(text: str) -> list[str]:
    contexts: list[str] = []
    for room_context, pattern in ROOM_CONTEXT_PATTERNS:
        if re.search(rf"(?i)(?<![{WORD_CHARS}])(?:{pattern})(?![{WORD_CHARS}])", text):
            contexts.append(room_context)
    return contexts


def first_non_target_room_context(text: str) -> str | None:
    for room_context in detect_room_contexts(text):
        if room_context in NON_TARGET_REVIEW_CONTEXTS:
            return room_context
    return None


def has_lighting_or_decor(text: str) -> bool:
    return bool(LIGHTING_WORD_RE.search(text) or DECOR_WORD_RE.search(text))


def find_bundle_item_heads(text: str) -> list[tuple[int, str, str]]:
    matches: list[tuple[int, str, str]] = []
    for item_head, category, pattern in BUNDLE_ITEM_HEAD_PATTERNS:
        for match in word_re(pattern).finditer(text):
            matches.append((match.start(), item_head, category))
    return sorted(matches, key=lambda item: item[0])


def bundle_candidate_texts(block: str) -> list[str]:
    lines = [compact_whitespace(line.strip(" \t-*•🌱")) for line in block.splitlines() if line.strip()]
    candidates = [line for line in lines if line]
    if len(lines) <= 2:
        whole_block = compact_whitespace(block)
        if whole_block and whole_block not in candidates:
            candidates.append(whole_block)
    return candidates


def detect_bundle_purchase(block: str) -> BundleDetection | None:
    for candidate in bundle_candidate_texts(block):
        item_matches = find_bundle_item_heads(candidate)
        if not item_matches:
            continue
        item_heads = tuple(dict.fromkeys(item_head for _, item_head, _ in item_matches))
        item_head_set = set(item_heads)
        candidate_lowered = normalize_for_match(candidate)
        if item_head_set <= {"кухня", "фасады"}:
            continue
        if "фартук" in item_head_set and ({"плитка", "фартук"} & item_head_set):
            continue
        if "столешниц" in candidate_lowered and item_head_set <= {"кухня", "стол"}:
            continue
        distinct_item_count = len(item_heads)
        room_contexts = tuple(detect_room_contexts(candidate))
        has_bundle_word = bool(BUNDLE_WORD_RE.search(candidate))
        has_list = bool(BUNDLE_LIST_RE.search(candidate))
        has_price_or_vendor = bool(parse_price(candidate) or parse_article_id(candidate) or find_vendor(candidate))
        repeated_item_head = len(item_matches) >= 2

        if not (
            (distinct_item_count >= 2 and (has_list or has_bundle_word or has_price_or_vendor))
            or (has_bundle_word and item_matches)
            or (len(room_contexts) >= 2 and repeated_item_head and (has_list or has_price_or_vendor))
            or (len(room_contexts) >= 2 and has_price_or_vendor)
        ):
            continue

        first_category = item_matches[0][2]
        if "hallway" in room_contexts and first_category == "living_room_furniture":
            first_category = "hallway"
        return BundleDetection(
            evidence_quote=candidate,
            item_heads=item_heads,
            category=first_category,
            room_contexts=room_contexts,
        )
    return None


def make_bundle_fact(detection: BundleDetection, **context: Any) -> ExtractedFact:
    room_context = ",".join(detection.room_contexts) if detection.room_contexts else None
    notes = "bundle purchase: detected item heads " + ", ".join(detection.item_heads)
    if detection.room_contexts:
        notes += "; room contexts " + ", ".join(detection.room_contexts)
    return make_product_fact(
        detection.evidence_quote,
        category=detection.category,
        item_type="bundle_purchase",
        item_name=", ".join(detection.item_heads),
        room_context=room_context,
        extraction_method="regex:bundle_purchase",
        confidence_hint="medium",
        extra_needs_review=True,
        notes=notes,
        **context,
    )


def extract_facts_from_text(
    *,
    text: str,
    source_message_id: int,
    date: str | None,
    source_scope: str,
    first_photo_path: str | None = None,
    created_at: str | None = None,
) -> list[ExtractedFact]:
    created_at = created_at or utc_now_iso()
    source_text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    project_name = parse_project_name(text)
    facts: list[ExtractedFact] = []

    for block in split_evidence_blocks(text):
        facts.extend(
            extract_wall_color_facts(
                block,
                source_message_id=source_message_id,
                date=date,
                source_scope=source_scope,
                project_name=project_name,
                source_text_hash=source_text_hash,
                created_at=created_at,
                first_photo_path=first_photo_path,
            )
        )
        bundle_detection = detect_bundle_purchase(block)
        if bundle_detection is not None:
            facts.append(
                make_bundle_fact(
                    bundle_detection,
                    source_message_id=source_message_id,
                    date=date,
                    source_scope=source_scope,
                    project_name=project_name,
                    source_text_hash=source_text_hash,
                    created_at=created_at,
                    first_photo_path=first_photo_path,
                )
            )
            continue
        for extractor in (
            extract_kitchen_fact,
            extract_table_fact,
            extract_sofa_fact,
            extract_chair_fact,
            extract_flooring_fact,
            extract_hallway_fact,
            extract_living_room_fact,
        ):
            fact = extractor(
                block,
                source_message_id=source_message_id,
                date=date,
                source_scope=source_scope,
                project_name=project_name,
                source_text_hash=source_text_hash,
                created_at=created_at,
                first_photo_path=first_photo_path,
            )
            if fact is not None:
                facts.append(fact)

    return deduplicate_facts(facts)


def split_evidence_blocks(text: str) -> list[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = re.split(r"\n\s*\n+", normalized)
    blocks: list[str] = []
    seen: set[str] = set()

    def add_block(value: str) -> None:
        lines = [line.strip(" \t-*•") for line in value.splitlines() if line.strip()]
        if not lines:
            return
        block = "\n".join(lines)
        key = compact_whitespace(block)
        if len(key) < 3 or key in seen:
            return
        seen.add(key)
        blocks.append(block)

    for paragraph in paragraphs:
        add_block(paragraph)
        lines = [line.strip(" \t-*•") for line in paragraph.splitlines() if line.strip()]
        if len(lines) <= 1:
            continue
        for index, line in enumerate(lines):
            if not should_start_line_window(line):
                continue
            window = [line]
            for next_line in lines[index + 1 : index + 4]:
                if should_start_line_window(next_line) and not re.search(
                    r"(?i)\b(арт\.?|код|руб|₽|промокод)\b",
                    next_line,
                ):
                    break
                window.append(next_line)
                if parse_price(next_line) or parse_article_id(next_line):
                    break
            add_block("\n".join(window))

    return blocks


def should_start_line_window(line: str) -> bool:
    return bool(
        re.search(
            r"(?i)\b("
            r"кухня|фасад|фартук|стол|диван|софа|кресл|стул|"
            r"плитк|керамогранит|кварцвинил|ламинат|паркет|"
            r"прихож|обувниц|зеркал|вешал|пуф|банкетк|консол|"
            r"тв\s*тумб|тумб|комод|стеллаж|полк|цвет\s+стен"
            r")\b",
            line,
        )
    )


def extract_wall_color_facts(
    block: str,
    *,
    source_message_id: int,
    date: str | None,
    source_scope: str,
    project_name: str | None,
    source_text_hash: str,
    created_at: str,
    first_photo_path: str | None,
) -> list[ExtractedFact]:
    codes = extract_color_codes(block)
    if not codes:
        return []
    lowered = normalize_for_match(block)
    has_context = any(
        marker in lowered
        for marker in ["цвет стен", "стены цвет", "краска", "tikkurila", "dulux", "v33", "ral"]
    )
    if not has_context:
        return []
    brand_raw, brand_normalized = detect_brand(block)
    facts: list[ExtractedFact] = []
    for code in codes:
        quote = quote_for_token(block, code)
        item_type = "wall_color_accent" if "акцент" in normalize_for_match(quote) else "wall_color"
        confidence = "high" if "цвет стен" in lowered or brand_normalized or code.startswith("RAL") else "medium"
        facts.append(
            make_fact(
                source_message_id=source_message_id,
                date=date,
                source_scope=source_scope,
                project_name=project_name,
                category="wall_colors",
                item_type=item_type,
                evidence_quote=quote,
                extraction_method="regex:wall_color_code",
                confidence=confidence,
                needs_review=confidence != "high",
                source_text_hash=source_text_hash,
                created_at=created_at,
                first_photo_path=first_photo_path,
                brand_raw=brand_raw,
                brand_normalized=brand_normalized,
                color_code=code,
                finish="Symphony" if "symphony" in lowered else None,
            )
        )
    return facts


def extract_kitchen_fact(block: str, **context: Any) -> ExtractedFact | None:
    lowered = normalize_for_match(block)
    if LIGHTING_WORD_RE.search(block):
        return make_product_fact(
            block,
            category="kitchens",
            item_type="kitchen_other",
            extraction_method="regex:kitchen_lighting_review",
            confidence_hint="medium",
            extra_needs_review=True,
            notes="lighting/decor kitchen context needs review",
            **context,
        ) if "кухн" in lowered else None
    if has_word_pattern(block, r"ручк(?:а|и|у|ой)|смесител(?:ь|и|я|ем)|мойк(?:а|и|у|ой)") and "кухн" in lowered:
        return make_product_fact(
            block,
            category="kitchens",
            item_type="kitchen_accessory",
            extraction_method="regex:kitchen_accessory_review",
            confidence_hint="medium",
            extra_needs_review=True,
            notes="kitchen accessory outside current clean summary scope",
            **context,
        )
    if "фартук" in lowered:
        return make_product_fact(
            block,
            category="kitchens",
            item_type="backsplash",
            extraction_method="regex:kitchen_backsplash",
            confidence_hint="high",
            **context,
        )
    if "столешниц" in lowered and ("кухн" in lowered or "фартук" not in lowered):
        descriptor = descriptor_after_keywords(block, ["столешница", "столешницу", "столешницы"])
        return make_product_fact(
            block,
            category="kitchens",
            item_type="countertop",
            item_name=descriptor,
            material=descriptor,
            extraction_method="regex:kitchen_countertop",
            confidence_hint="medium",
            **context,
        )
    if "кухн" not in lowered and "фасад" not in lowered:
        return None
    if "фасад" in lowered:
        finish = descriptor_after_keywords(block, ["кухня фасады", "кухня", "фасады", "фасад"])
        return make_product_fact(
            block,
            category="kitchens",
            item_type="kitchen_facades",
            finish=finish,
            extraction_method="regex:kitchen_facades",
            confidence_hint="high" if finish else "medium",
            extra_needs_review=not bool(finish),
            notes="kitchen facades without parsed finish" if not finish else None,
            **context,
        )
    kitchen_purchase_signal = any(
        marker in lowered
        for marker in ["гарнитур", "на заказ", "мебель", "mebel.in", "кухня"]
    ) and (parse_price(block) or find_vendor(block))
    if kitchen_purchase_signal:
        return make_product_fact(
            block,
            category="kitchens",
            item_type="kitchen_purchase",
            extraction_method="regex:kitchen_purchase",
            confidence_hint="medium",
            extra_needs_review=True,
            notes="broad kitchen purchase row needs review unless validated against facades/countertop/backsplash",
            **context,
        )
    return make_product_fact(
        block,
        category="kitchens",
        item_type="kitchen_other",
        extraction_method="regex:kitchen_other_review",
        confidence_hint="medium",
        extra_needs_review=True,
        notes="broad kitchen context without facade/countertop/backsplash signal",
        **context,
    )


def extract_table_fact(block: str, **context: Any) -> ExtractedFact | None:
    lowered = normalize_for_match(block)
    has_table_head = has_word_pattern(
        block,
        r"журнальн(?:ый|ого|ому|ым)?\s+столик|обеденн(?:ый|ого|ому|ым)?\s+стол|"
        r"кухонн(?:ый|ого|ому|ым)?\s+стол|рабоч(?:ий|его|ему|им)?\s+стол|"
        r"подстолье|столик(?:и|а|ом|е)?|стол(?:ы|а|у)?",
    )
    has_table_context = has_word_pattern(block, r"столом|столе")
    if not has_table_head:
        if has_table_context and LIGHTING_WORD_RE.search(block):
            return make_product_fact(
                block,
                category="tables",
                item_type="table_context_match",
                extraction_method="regex:table_context_review",
                confidence_hint="medium",
                extra_needs_review=True,
                notes="lighting/decor line mentions table only as context",
                **context,
            )
        return None
    if LIGHTING_WORD_RE.search(block):
        return make_product_fact(
            block,
            category="tables",
            item_type="table_context_match",
            extraction_method="regex:table_lighting_review",
            confidence_hint="medium",
            extra_needs_review=True,
            notes="lighting/decor line is not a clean table fact",
            **context,
        )
    if "столешниц" in lowered:
        return None
    item_type = "table"
    if "журналь" in lowered:
        item_type = "coffee_table"
    elif "обеден" in lowered:
        item_type = "dining_table"
    elif "кухон" in lowered:
        item_type = "kitchen_table"
    elif "рабоч" in lowered:
        item_type = "working_table"
    elif "подстоль" in lowered:
        item_type = "table_base"
    elif "столешниц" in lowered:
        item_type = "tabletop"
    descriptor = descriptor_after_keywords(
        block,
        [
            "журнальный стол",
            "обеденный стол",
            "кухонный стол",
            "рабочий стол",
            "столешница",
            "подстолье",
            "журнальный столик",
            "столик",
            "стол",
        ],
    )
    return make_product_fact(
        block,
        category="tables",
        item_type=item_type,
        item_name=descriptor,
        model=descriptor,
        extraction_method="regex:table",
        confidence_hint="high" if parse_price(block) or parse_article_id(block) else "medium",
        **context,
    )


def extract_sofa_fact(block: str, **context: Any) -> ExtractedFact | None:
    lowered = normalize_for_match(block)
    has_sofa_head = has_word_pattern(block, r"диван(?:ы|а)?|софа|диван-кровать|кровать-диван")
    has_sofa_context = has_word_pattern(block, r"диваном|дивана|диване")
    if not has_sofa_head:
        if has_sofa_context and (
            has_lighting_or_decor(block)
            or re.search(r"(?i)\b(над|за|у|рядом\s+с|возле|с)\s+диван", block)
        ):
            return make_product_fact(
                block,
                category="sofas",
                item_type="sofa_context_match",
                extraction_method="regex:sofa_context_review",
                confidence_hint="medium",
                extra_needs_review=True,
                notes="line mentions sofa only as surrounding context",
                **context,
            )
        return None
    if re.search(r"(?i)\b(над|за|у|рядом\s+с|возле)\s+диван", block):
        return make_product_fact(
            block,
            category="sofas",
            item_type="sofa_context_match",
            extraction_method="regex:sofa_context_review",
            confidence_hint="medium",
            extra_needs_review=True,
            notes="line mentions sofa only as surrounding context",
            **context,
        )
    if re.search(r"(?i)\bкровать\b.{0,40}\bс\s+диван", block) and not re.search(
        r"(?i)^\s*диван[-\s]?кровать\b", block
    ):
        return make_product_fact(
            block,
            category="sofas",
            item_type="sofa_context_match",
            extraction_method="regex:sofa_bed_context_review",
            confidence_hint="medium",
            extra_needs_review=True,
            notes="bed line mentions sofa as part of another item",
            **context,
        )
    item_type = "sofa"
    if "диван-кровать" in lowered or "кровать-диван" in lowered:
        item_type = "sofa_bed"
    elif "софа" in lowered:
        item_type = "couch"
    descriptor = descriptor_after_keywords(block, ["диван-кровать", "кровать-диван", "диваны", "диван", "софа"])
    model, material, color = split_model_material_color(descriptor)
    notes: list[str] = []
    if suspiciously_long_descriptor(descriptor):
        notes.append("suspiciously long sofa descriptor")
    return make_product_fact(
        block,
        category="sofas",
        item_type=item_type,
        item_name=None,
        model=model,
        material=material,
        color=color,
        extraction_method="regex:sofa",
        confidence_hint="high" if parse_price(block) or find_vendor(block) else "medium",
        extra_needs_review=bool(notes),
        notes="; ".join(notes) if notes else None,
        **context,
    )


def extract_chair_fact(block: str, **context: Any) -> ExtractedFact | None:
    lowered = normalize_for_match(block)
    if not any(marker in lowered for marker in ["стул", "стуль", "кресл"]):
        return None
    item_type = "chair"
    if "бар" in lowered:
        item_type = "bar_chair"
    elif "рабоч" in lowered or "кресл" in lowered:
        item_type = "working_chair"
    elif "обеден" in lowered or "кухон" in lowered:
        item_type = "dining_chair"
    descriptor = descriptor_after_keywords(
        block,
        ["рабочее кресло", "кресло", "обеденные стулья", "обеденный стул", "кухонные стулья", "кухонный стул", "стулья", "стул"],
    )
    model, material, color = split_model_material_color(descriptor)
    return make_product_fact(
        block,
        category="chairs",
        item_type=item_type,
        item_name=descriptor,
        model=model or descriptor,
        material=material,
        color=color,
        extraction_method="regex:chair",
        confidence_hint="high" if parse_price(block) or parse_article_id(block) else "medium",
        **context,
    )


def extract_flooring_fact(block: str, **context: Any) -> ExtractedFact | None:
    lowered = normalize_for_match(block)
    if "фартук" in lowered:
        return None
    has_floor_context = any(
        marker in lowered
        for marker in [
            "плитка на полу",
            "плитка на пол",
            "плитка для пола",
            "на полу керамогранит",
            "керамогранит на полу",
            "напольное покрытие",
            "покрытие пола",
            "кварцвинил",
            "кварц винил",
            "spc",
            "ламинат",
            "паркет",
        ]
    )
    has_floor_context = has_floor_context or bool(
        re.search(r"(плитк|керамогранит).{0,60}(на\s+полу|на\s+пол|для\s+пола)", lowered)
        or re.search(r"(на\s+полу|на\s+пол|для\s+пола).{0,60}(плитк|керамогранит)", lowered)
    )
    if not has_floor_context:
        return None
    if not (parse_price(block) or parse_article_id(block) or find_vendor(block)):
        return None

    item_type = "flooring_unclear"
    if "кварцвинил" in lowered or "кварц винил" in lowered:
        item_type = "flooring_quartz_vinyl"
    elif "spc" in lowered or "ламинат" in lowered:
        item_type = "flooring_laminate"
    elif "паркет" in lowered:
        item_type = "flooring_parquet"
    elif "плитк" in lowered or "керамогранит" in lowered:
        item_type = "flooring_tile"
    elif "напольн" in lowered:
        item_type = "flooring_material"

    room_context = None
    needs_review = False
    flooring_review_notes: list[str] = []
    non_target_contexts = [
        detected_context
        for detected_context in detect_room_contexts(block)
        if detected_context in NON_TARGET_REVIEW_CONTEXTS
    ]
    if non_target_contexts:
        room_context = non_target_contexts[0]
        needs_review = True
        flooring_review_notes.append("flooring row has non-target room context: " + ", ".join(non_target_contexts))

    descriptor = descriptor_after_keywords(
        block,
        ["плитка на полу", "плитка на пол", "плитка для пола", "керамогранит", "кварцвинил", "кварц винил", "spc ламинат", "ламинат", "паркет"],
    )
    return make_product_fact(
        block,
        category="flooring",
        item_type=item_type,
        item_name=descriptor,
        material=descriptor,
        room_context=room_context,
        extraction_method="regex:flooring",
        confidence_hint="medium" if needs_review else "high",
        extra_needs_review=needs_review,
        notes="; ".join(flooring_review_notes) if flooring_review_notes else None,
        **context,
    )


def extract_hallway_fact(block: str, **context: Any) -> ExtractedFact | None:
    lowered = normalize_for_match(block)
    if not any(
        marker in lowered
        for marker in ["прихож", "коридор", "обувниц", "входная группа", "вешал"]
    ):
        return None
    item_type = "entry_group"
    if "обувниц" in lowered:
        item_type = "shoe_cabinet"
    elif "шкаф" in lowered:
        item_type = "hallway_wardrobe"
    elif "консол" in lowered:
        item_type = "console"
    elif "пуф" in lowered or "банкетк" in lowered:
        item_type = "pouf"
    elif "зеркал" in lowered:
        item_type = "mirror"
    elif "вешал" in lowered:
        item_type = "hanger"
    descriptor = descriptor_after_keywords(
        block,
        ["шкаф", "обувница", "консоль", "пуф", "банкетка", "зеркало", "вешалка", "входная группа"],
    )
    return make_product_fact(
        block,
        category="hallway",
        item_type=item_type,
        item_name=descriptor,
        model=descriptor,
        room_context="hallway",
        extraction_method="regex:hallway",
        confidence_hint="high" if parse_price(block) or parse_article_id(block) else "medium",
        **context,
    )


def extract_living_room_fact(block: str, **context: Any) -> ExtractedFact | None:
    lowered = normalize_for_match(block)
    has_living_furniture = any(
        marker in lowered
        for marker in ["тв тумб", "тв-тумб", "тумба под тв", "телевизор", "комод", "стеллаж", "полк", "консол", "шкаф", "журнальн"]
    )
    if not has_living_furniture:
        if "гостин" in lowered and has_lighting_or_decor(block):
            return make_product_fact(
                block,
                category="living_room_furniture",
                item_type="living_room_context_match",
                extraction_method="regex:living_room_context_review",
                confidence_hint="medium",
                extra_needs_review=True,
                notes="lighting/decor line mentions living room only as context",
                **context,
            )
        return None
    if has_lighting_or_decor(block) and not any(marker in lowered for marker in ["полк", "стеллаж", "комод", "тумб", "шкаф", "консол"]):
        return make_product_fact(
            block,
            category="living_room_furniture",
            item_type="living_room_context_match",
            extraction_method="regex:living_room_decor_review",
            confidence_hint="medium",
            extra_needs_review=True,
            notes="lighting/decor line is not a clean living-room furniture fact",
            **context,
        )
    item_type = "cabinet"
    if any(marker in lowered for marker in ["тв тумб", "тв-тумб", "тумба под тв", "телевизор"]):
        item_type = "tv_unit"
    elif "комод" in lowered:
        item_type = "chest"
    elif "стеллаж" in lowered:
        item_type = "shelving"
    elif "полк" in lowered:
        item_type = "shelves"
    elif "консол" in lowered:
        item_type = "console"
    elif "шкаф" in lowered:
        item_type = "cabinet"
    elif "журнальн" in lowered:
        item_type = "coffee_table"
    descriptor = descriptor_after_keywords(
        block,
        ["тв тумба", "тв-тумба", "тумба под тв", "шкаф", "комод", "стеллаж", "полки", "полка", "консоль", "журнальный столик"],
    )
    return make_product_fact(
        block,
        category="living_room_furniture",
        item_type=item_type,
        item_name=descriptor,
        model=descriptor,
        room_context="living_room" if "гостин" in lowered else None,
        extraction_method="regex:living_room_furniture",
        confidence_hint="high" if parse_price(block) or parse_article_id(block) else "medium",
        **context,
    )


def make_product_fact(
    block: str,
    *,
    category: str,
    item_type: str,
    source_message_id: int,
    date: str | None,
    source_scope: str,
    project_name: str | None,
    source_text_hash: str,
    created_at: str,
    first_photo_path: str | None,
    item_name: str | None = None,
    model: str | None = None,
    material: str | None = None,
    finish: str | None = None,
    color: str | None = None,
    room_context: str | None = None,
    extraction_method: str,
    confidence_hint: str = "medium",
    extra_needs_review: bool = False,
    notes: str | None = None,
) -> ExtractedFact:
    price = parse_price(block)
    article = parse_article_id(block)
    vendor = find_vendor(block)
    promo = parse_promo_code(block)
    vendor_raw = vendor.raw if vendor else None
    vendor_normalized = vendor.normalized if vendor else None
    marketplace = None
    article_id = None
    if article:
        article_id = article.article_id
        marketplace = article.marketplace
        if vendor_raw is None:
            vendor_raw = article.vendor_raw
            vendor_normalized = article.vendor_normalized
    evidence_quote = build_evidence_quote(block)
    detected_room_contexts = detect_room_contexts(block)
    if room_context is None:
        room_context = first_non_target_room_context(block)

    detail_count = sum(
        bool(value)
        for value in [
            vendor_normalized,
            article_id,
            price.value if price else None,
            item_name,
            model,
            material,
            finish,
            color,
        ]
    )
    confidence = confidence_hint
    if detail_count >= 3 and confidence_hint != "low":
        confidence = "high"
    elif detail_count <= 1 and confidence_hint == "high":
        confidence = "medium"
    if not (vendor_normalized or article_id or price):
        confidence = "low" if confidence_hint != "high" else "medium"
    if extra_needs_review and confidence_hint == "medium":
        confidence = "medium"

    review_notes: list[str] = []
    if notes:
        review_notes.append(notes)
    if confidence == "low":
        review_notes.append("low confidence deterministic match")
    if not (vendor_normalized or article_id or price):
        review_notes.append("no vendor, article id, or price parsed")
    if len(list(PRICE_RE.finditer(block))) > 1:
        review_notes.append("multiple prices in quote; price attribution ambiguous")
    if suspiciously_long_descriptor(item_name) or suspiciously_long_descriptor(model):
        review_notes.append("suspiciously long descriptor")
    if any(
        descriptor_has_suspicious_prefix(value)
        for value in [item_name, model, material, finish, color]
    ):
        review_notes.append("suspicious descriptor fragment")
    non_target_contexts = [
        detected_context
        for detected_context in detected_room_contexts
        if detected_context in NON_TARGET_REVIEW_CONTEXTS
    ]
    if category in NON_TARGET_REVIEW_CATEGORIES and non_target_contexts:
        review_notes.append("non-target room context: " + ", ".join(non_target_contexts))
        if room_context is None:
            room_context = non_target_contexts[0]
    if count_item_triggers(evidence_quote) > 2:
        review_notes.append("quote may contain several unrelated items")
    if confidence == "high" and (extra_needs_review or review_notes):
        confidence = "medium"

    needs_review = extra_needs_review or bool(review_notes) or confidence == "low"
    return make_fact(
        source_message_id=source_message_id,
        date=date,
        source_scope=source_scope,
        project_name=project_name,
        category=category,
        item_type=item_type,
        evidence_quote=evidence_quote,
        extraction_method=extraction_method,
        confidence=confidence,
        needs_review=needs_review,
        source_text_hash=source_text_hash,
        created_at=created_at,
        first_photo_path=first_photo_path,
        item_name=clean_optional(item_name),
        vendor_raw=vendor_raw,
        vendor_normalized=vendor_normalized,
        model=clean_optional(model),
        material=clean_optional(material),
        finish=clean_optional(finish),
        color=clean_optional(color),
        article_id=article_id,
        marketplace=marketplace,
        price_value=price.value if price else None,
        price_currency=price.currency if price else None,
        price_unit=price.unit if price else None,
        promo_code=promo.code if promo else None,
        room_context=room_context,
        notes="; ".join(review_notes) if review_notes else None,
    )


def make_fact(
    *,
    source_message_id: int,
    date: str | None,
    source_scope: str,
    project_name: str | None,
    category: str,
    item_type: str,
    evidence_quote: str,
    extraction_method: str,
    confidence: str,
    needs_review: bool,
    source_text_hash: str,
    created_at: str,
    first_photo_path: str | None,
    item_name: str | None = None,
    vendor_raw: str | None = None,
    vendor_normalized: str | None = None,
    brand_raw: str | None = None,
    brand_normalized: str | None = None,
    model: str | None = None,
    material: str | None = None,
    finish: str | None = None,
    color: str | None = None,
    color_code: str | None = None,
    article_id: str | None = None,
    marketplace: str | None = None,
    price_value: int | float | None = None,
    price_currency: str | None = None,
    price_unit: str | None = None,
    promo_code: str | None = None,
    room_context: str | None = None,
    notes: str | None = None,
) -> ExtractedFact:
    return ExtractedFact(
        source_message_id=source_message_id,
        date=date,
        source_scope=source_scope,
        project_name=project_name,
        category=category,
        item_type=item_type,
        item_name=clean_optional(item_name),
        vendor_raw=clean_optional(vendor_raw),
        vendor_normalized=clean_optional(vendor_normalized),
        brand_raw=clean_optional(brand_raw),
        brand_normalized=clean_optional(brand_normalized),
        model=clean_optional(model),
        material=clean_optional(material),
        finish=clean_optional(finish),
        color=clean_optional(color),
        color_code=clean_optional(color_code),
        article_id=clean_optional(article_id),
        marketplace=clean_optional(marketplace),
        price_value=price_value,
        price_currency=clean_optional(price_currency),
        price_unit=clean_optional(price_unit),
        promo_code=clean_optional(promo_code),
        room_context=clean_optional(room_context),
        evidence_quote=compact_whitespace(evidence_quote),
        extraction_method=extraction_method,
        confidence=confidence,
        needs_review=needs_review,
        notes=clean_optional(notes),
        source_text_hash=source_text_hash,
        created_at=created_at,
        first_photo_path=clean_optional(first_photo_path),
    )


def descriptor_after_keywords(block: str, keywords: list[str]) -> str | None:
    cleaned = remove_parse_noise_preserve_lines(strip_promo_lines(block))
    vendor = find_vendor(cleaned)
    if vendor:
        cleaned = re.sub(re.escape(vendor.raw), " ", cleaned, flags=re.IGNORECASE)
    best_match: re.Match[str] | None = None
    for keyword in keywords:
        pattern = keyword_boundary_re(keyword)
        match = pattern.search(cleaned)
        if match and (best_match is None or match.start() < best_match.start()):
            best_match = match
    if best_match is not None:
        cleaned = cleaned[best_match.end() :]
    cleaned = descriptor_same_item_segment(cleaned)
    cleaned = re.sub(r"^\s*\d+[.)]\s*", "", cleaned)
    cleaned = re.sub(rf"(?i)(?<![{WORD_CHARS}])(ozon|wb|вб|ям)(?![{WORD_CHARS}])", " ", cleaned)
    cleaned = compact_whitespace(cleaned.strip(" -:;,.+"))
    if descriptor_has_suspicious_prefix(cleaned):
        return None
    return cleaned or None


def strip_promo_lines(text: str) -> str:
    return "\n".join(
        line
        for line in text.splitlines()
        if not re.search(r"(?i)\bпромо(?:код)?\b|mirzabaeva", line)
    )


def remove_parse_noise_preserve_lines(text: str) -> str:
    cleaned = PRICE_RE.sub(" ", text)
    cleaned = ARTICLE_RE.sub(" ", cleaned)
    cleaned = PROMO_RE.sub(" ", cleaned)
    return "\n".join(compact_whitespace(line) for line in cleaned.splitlines())


def keyword_boundary_re(keyword: str) -> re.Pattern[str]:
    pieces = [re.escape(piece) for piece in compact_whitespace(keyword).split()]
    pattern = r"\s+".join(pieces)
    pattern = pattern.replace(r"\-", r"[-\s]?")
    return re.compile(rf"(?i)(?<![{WORD_CHARS}]){pattern}(?![{WORD_CHARS}])")


def descriptor_same_item_segment(value: str) -> str:
    lines = [line.strip(" -:;,.+") for line in value.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return ""
    first_line = lines[0]
    if compact_whitespace(first_line):
        return first_line
    for line in lines[1:]:
        if ITEM_BOUNDARY_RE.search(line):
            break
        if compact_whitespace(line):
            return line
    return ""


def descriptor_has_suspicious_prefix(value: str | None) -> bool:
    if not value:
        return False
    lowered = normalize_for_match(value).strip()
    return any(lowered.startswith(prefix) for prefix in SUSPICIOUS_DESCRIPTOR_PREFIXES)


def split_model_material_color(descriptor: str | None) -> tuple[str | None, str | None, str | None]:
    if not descriptor:
        return None, None, None
    tokens = re.findall(r"[A-Za-zА-Яа-яЁё0-9.-]+", descriptor)
    if not tokens:
        return None, None, None
    material_index: int | None = None
    for index, token in enumerate(tokens):
        if normalize_for_match(token) in MATERIAL_WORDS_FOR_EXTRACTION:
            material_index = index
            break
    if material_index is not None:
        model = " ".join(tokens[:material_index]) or None
        material = tokens[material_index]
        color_tokens = tokens[material_index + 1 :]
        color = " ".join(color_tokens[:2]) if color_tokens else None
        return model, material, color
    color_index: int | None = None
    for index, token in enumerate(tokens):
        if normalize_for_match(token) in COLOR_WORDS_FOR_EXTRACTION:
            color_index = index
            break
    if color_index is not None:
        return " ".join(tokens[:color_index]) or None, None, " ".join(tokens[color_index:])
    if len(tokens) <= 5:
        return " ".join(tokens), None, None
    return descriptor, None, None


MATERIAL_WORDS_FOR_EXTRACTION = {
    "velvet",
    "вельвет",
    "bucle",
    "букле",
    "рогожка",
    "велюр",
    "экокожа",
    "кожа",
    "шенилл",
}

COLOR_WORDS_FOR_EXTRACTION = {
    "emerald",
    "olive",
    "white",
    "grey",
    "gray",
    "green",
    "red",
    "blue",
    "black",
    "beige",
    "зеленый",
    "зеленая",
    "зеленые",
    "зелёный",
    "оливковый",
    "белый",
    "серый",
    "синий",
    "красный",
    "черный",
    "чёрный",
    "бежевый",
    "коричневый",
    "кирпичный",
}


def build_evidence_quote(block: str) -> str:
    quote = without_promos(block)
    quote = compact_whitespace(quote)
    if len(quote) <= 260:
        return quote
    return quote[:257].rstrip() + "..."


def quote_for_token(block: str, token: str) -> str:
    normalized_token = normalize_for_match(token)
    for line in block.splitlines():
        if normalized_token in normalize_for_match(line):
            return compact_whitespace(line)
    return build_evidence_quote(block)


def clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = compact_whitespace(str(value).strip(" -:;,."))
    return cleaned or None


def deduplicate_facts(facts: list[ExtractedFact]) -> list[ExtractedFact]:
    deduped: dict[tuple[Any, ...], ExtractedFact] = {}
    for fact in facts:
        key = (
            fact.source_message_id,
            fact.category,
            fact.item_type,
            fact.evidence_quote,
            fact.article_id,
            fact.color_code,
        )
        existing = deduped.get(key)
        if existing is None:
            deduped[key] = fact
            continue
        if CONFIDENCE_SORT_VALUE[fact.confidence] > CONFIDENCE_SORT_VALUE[existing.confidence]:
            deduped[key] = fact
    return list(deduped.values())


def sort_facts(facts: list[ExtractedFact]) -> list[ExtractedFact]:
    return sorted(
        facts,
        key=lambda fact: (
            CONFIDENCE_SORT_VALUE[fact.confidence],
            not fact.needs_review,
            fact.date or "",
            fact.source_message_id,
            fact.category,
            fact.item_type,
        ),
        reverse=True,
    )


def write_facts_csv(facts: list[ExtractedFact], path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        for index, fact in enumerate(facts, start=1):
            writer.writerow(fact.to_json_dict(row_id=index))


def write_facts_jsonl(facts: list[ExtractedFact], path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for index, fact in enumerate(facts, start=1):
            fh.write(json_dumps(fact.to_json_dict(row_id=index)))
            fh.write("\n")


def write_facts_sqlite(
    facts: list[ExtractedFact],
    path: Path,
    *,
    db_path: Path,
    source_scope: str,
    settings: dict[str, Any],
    created_at: str,
) -> None:
    rebuild_in_place = path.exists()
    for sqlite_path in [
        Path(str(path) + "-wal"),
        Path(str(path) + "-shm"),
        Path(str(path) + "-journal"),
    ]:
        if sqlite_path.exists():
            try:
                sqlite_path.unlink()
            except PermissionError:
                try:
                    sqlite_path.write_bytes(b"")
                except OSError:
                    pass
                continue
    sqlite_uri = f"{path.resolve().as_uri()}?mode=rwc&nolock=1"
    with sqlite3.connect(sqlite_uri, uri=True) as conn:
        # Generated output DBs are rebuilt from source data; avoiding rollback
        # journals also avoids workspace filesystem lock errors seen on Windows.
        conn.execute("PRAGMA journal_mode = OFF")
        if rebuild_in_place:
            drop_extraction_schema(conn)
        create_extraction_schema(conn)
        conn.execute(
            """
            INSERT INTO extraction_runs (db_path, source_scope, created_at, settings_json)
            VALUES (?, ?, ?, ?)
            """,
            (str(db_path), source_scope, created_at, json.dumps(settings, ensure_ascii=False, sort_keys=True)),
        )
        insert_normalized_vendors(conn)
        for fact in facts:
            data = fact.to_json_dict()
            conn.execute(
                f"""
                INSERT INTO extracted_facts ({", ".join(data)})
                VALUES ({", ".join("?" for _ in data)})
                """,
                tuple(data.values()),
            )
        conn.commit()


def drop_extraction_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS extracted_facts;
        DROP TABLE IF EXISTS normalized_vendors;
        DROP TABLE IF EXISTS extraction_runs;
        """
    )


def create_extraction_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE extracted_facts (
            id INTEGER PRIMARY KEY,
            source_message_id INTEGER NOT NULL,
            date TEXT,
            source_scope TEXT NOT NULL,
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
            extraction_method TEXT NOT NULL,
            confidence TEXT NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
            needs_review INTEGER NOT NULL,
            notes TEXT,
            source_text_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            first_photo_path TEXT
        );

        CREATE TABLE normalized_vendors (
            raw TEXT NOT NULL,
            normalized TEXT NOT NULL,
            aliases TEXT NOT NULL
        );

        CREATE TABLE extraction_runs (
            id INTEGER PRIMARY KEY,
            db_path TEXT NOT NULL,
            source_scope TEXT NOT NULL,
            created_at TEXT NOT NULL,
            settings_json TEXT NOT NULL
        );

        CREATE INDEX idx_extracted_category ON extracted_facts(category);
        CREATE INDEX idx_extracted_message ON extracted_facts(source_message_id);
        CREATE INDEX idx_extracted_vendor ON extracted_facts(vendor_normalized);
        """
    )


def insert_normalized_vendors(conn: sqlite3.Connection) -> None:
    from .extraction_patterns import VENDOR_ALIASES

    for normalized, aliases in VENDOR_ALIASES:
        for alias in aliases:
            conn.execute(
                """
                INSERT INTO normalized_vendors (raw, normalized, aliases)
                VALUES (?, ?, ?)
                """,
                (alias, normalized, json.dumps(aliases, ensure_ascii=False)),
            )


def write_markdown_outputs(result: FactExtractionResult, out_dir: Path) -> list[Path]:
    output_files: list[Path] = []
    summary_path = out_dir / "summary.md"
    summary_path.write_text(build_summary_markdown(result), encoding="utf-8")
    output_files.append(summary_path)

    quality_path = out_dir / "extraction_quality_summary.md"
    quality_path.write_text(build_quality_summary_markdown(result), encoding="utf-8")
    output_files.append(quality_path)

    descriptions = {category.category_id: category.description for category in load_category_configs()}
    category_sets = [
        ("by_category", result.facts, "all retained facts"),
        ("by_category_clean", [fact for fact in result.facts if not fact.needs_review], "clean facts only"),
        ("by_category_needs_review", [fact for fact in result.facts if fact.needs_review], "needs_review facts only"),
    ]
    for dirname, facts, suffix in category_sets:
        by_category = out_dir / dirname
        by_category.mkdir(parents=True, exist_ok=True)
        for category in STRICT_CATEGORIES:
            path = by_category / f"{category}.md"
            path.write_text(
                build_category_markdown(
                    category,
                    [fact for fact in facts if fact.category == category],
                    f"{descriptions.get(category, '')} ({suffix})." if descriptions.get(category, "") else suffix,
                ),
                encoding="utf-8",
            )
            output_files.append(path)
    return output_files


def build_summary_markdown(result: FactExtractionResult) -> str:
    facts = result.facts
    clean_facts = [fact for fact in facts if not fact.needs_review]
    review_facts = [fact for fact in facts if fact.needs_review]
    category_counts = Counter(fact.category for fact in facts)
    clean_category_counts = Counter(fact.category for fact in clean_facts)
    review_category_counts = Counter(fact.category for fact in review_facts)
    confidence_counts = Counter(fact.confidence for fact in facts)
    vendor_counts = Counter(fact.vendor_normalized for fact in clean_facts if fact.vendor_normalized)
    item_counts = Counter(fact.item_type for fact in facts)
    color_counts = Counter(fact.color_code for fact in clean_facts if fact.color_code)
    kitchen_facades = Counter(
        fact.finish for fact in clean_facts if fact.category == "kitchens" and fact.finish
    )
    sofa_values = Counter(
        value
        for fact in facts
        if fact.category == "sofas"
        for value in [fact.material, fact.color]
        if value
    )

    lines: list[str] = []
    lines.append("# Structured Fact Extraction Summary")
    lines.append("")
    lines.append(f"- Database: `{result.db_path.resolve()}`")
    lines.append(f"- Generated at: `{result.generated_at}`")
    lines.append(f"- Source scope: `{result.source_scope}`")
    lines.append(f"- Total text posts in DB: {result.total_text_posts}")
    lines.append(f"- Total posts with real photos in DB: {result.total_posts_with_photos}")
    lines.append(f"- Total source posts processed: {result.source_posts_processed}")
    lines.append(f"- Total facts extracted: {len(facts)}")
    lines.append(f"- Clean facts: {len(clean_facts)}")
    lines.append(f"- Needs review: {len(review_facts)}")
    lines.append("")
    lines.append(
        "This is a deterministic evidence table, not a father-facing renovation cheat sheet. "
        "It does not use LLMs, OCR, VLMs, LM Studio, OpenRouter, comments sync, or internet search."
    )
    lines.append("")
    lines.append("## Facts by Category")
    lines.append("")
    lines.append("| Category | All facts | Clean facts | Needs review |")
    lines.append("|---|---:|---:|---:|")
    for category in STRICT_CATEGORIES:
        lines.append(
            f"| `{category}` | {category_counts[category]} | "
            f"{clean_category_counts[category]} | {review_category_counts[category]} |"
        )
    lines.append("")
    lines.append("## Confidence")
    lines.append("")
    lines.append("| Confidence | Facts |")
    lines.append("|---|---:|")
    for confidence in ["high", "medium", "low"]:
        lines.append(f"| `{confidence}` | {confidence_counts[confidence]} |")
    lines.append("")
    lines.append(f"## Top Normalized Vendors From Clean Rows\n\n{format_counter(vendor_counts)}")
    lines.append(f"## Top Item Types\n\n{format_counter(item_counts)}")
    lines.append(f"## Top Wall Color Codes From Clean Rows\n\n{format_counter(color_counts)}")
    lines.append(f"## Top Kitchen Facade Phrases From Clean Rows\n\n{format_counter(kitchen_facades)}")
    lines.append(f"## Top Sofa Fabrics / Colors\n\n{format_counter(sofa_values)}")
    lines.append("## Limitations")
    lines.append("")
    lines.append("- The parser is deterministic and conservative; unclear fields are left empty.")
    lines.append("- Needs-review rows are retained in all-row outputs and split into review-specific files.")
    lines.append("- Bundle purchases keep the shared price only on a `bundle_purchase` review row.")
    lines.append("- Generated facts are draft evidence rows for later validation, not recommendations.")
    lines.append("")
    return "\n".join(lines)


def build_quality_summary_markdown(result: FactExtractionResult) -> str:
    facts = result.facts
    clean_facts = [fact for fact in facts if not fact.needs_review]
    review_facts = [fact for fact in facts if fact.needs_review]
    note_counts = Counter()
    for fact in review_facts:
        notes = normalize_for_match(fact.notes or "")
        if "bundle purchase" in notes:
            note_counts["bundle_purchase"] += 1
        if "suspicious descriptor" in notes:
            note_counts["suspicious_descriptor"] += 1
        if "non-target room context" in notes:
            note_counts["non_target_room"] += 1
        if "lighting/decor" in notes or "context" in notes:
            note_counts["context_or_false_positive"] += 1
        if "several unrelated items" in notes:
            note_counts["multiple_items"] += 1

    lines: list[str] = []
    lines.append("# Extraction Quality Summary")
    lines.append("")
    lines.append(f"- Total facts: {len(facts)}")
    lines.append(f"- Clean facts: {len(clean_facts)}")
    lines.append(f"- Needs review facts: {len(review_facts)}")
    lines.append("")
    lines.append("## Review Signals")
    lines.append("")
    lines.append("| Signal | Count |")
    lines.append("|---|---:|")
    for signal in [
        "bundle_purchase",
        "suspicious_descriptor",
        "non_target_room",
        "context_or_false_positive",
        "multiple_items",
    ]:
        lines.append(f"| `{signal}` | {note_counts[signal]} |")
    lines.append("")
    lines.append("## Review Examples")
    lines.append("")
    lines.extend(format_fact_examples(review_facts, limit=12))
    lines.append("## Clean Examples")
    lines.append("")
    lines.extend(format_fact_examples(clean_facts, limit=12))
    lines.append("## Category Readiness")
    lines.append("")
    lines.append("| Category | Stage 2.1 status |")
    lines.append("|---|---|")
    readiness = {
        "wall_colors": "ready-ish for deterministic summary",
        "kitchens": "usable for facade/countertop/backsplash rows; broad purchases need review",
        "chairs": "usable after bundle filtering",
        "tables": "clean rows usable; context and lighting rows reviewed",
        "sofas": "clean rows usable; context and bundle rows reviewed",
        "hallway": "needs review for multi-room and grouped purchases",
        "living_room_furniture": "not ready for final summary; review bucket is important",
        "flooring": "not ready for final summary; useful rows retained conservatively",
    }
    for category in STRICT_CATEGORIES:
        lines.append(f"| `{category}` | {readiness[category]} |")
    lines.append("")
    lines.append("False-positive prevention is deterministic. Clear lighting/decor/context rows are either skipped or retained only as needs-review evidence rows.")
    lines.append("")
    return "\n".join(lines)


def format_fact_examples(facts: list[ExtractedFact], *, limit: int) -> list[str]:
    if not facts:
        return ["No examples.", ""]
    lines = ["| Category | Item type | Review | Notes | Evidence |", "|---|---|---:|---|---|"]
    for fact in sort_facts(facts)[:limit]:
        lines.append(
            "| "
            f"`{fact.category}` | "
            f"`{fact.item_type}` | "
            f"{int(fact.needs_review)} | "
            f"{escape_markdown_cell(fact.notes or '')} | "
            f"{escape_markdown_cell(fact.evidence_quote)} |"
        )
    lines.append("")
    return lines


def build_category_markdown(category: str, facts: list[ExtractedFact], description: str) -> str:
    vendor_counts = Counter(fact.vendor_normalized for fact in facts if fact.vendor_normalized)
    repeated_values = Counter(
        value
        for fact in facts
        for value in [fact.color_code, fact.finish, fact.material, fact.color, fact.model]
        if value
    )
    lines: list[str] = []
    lines.append(f"# {category}: Extracted Facts")
    lines.append("")
    if description:
        lines.append(f"- Description: {description}")
    lines.append(f"- Count: {len(facts)}")
    lines.append(f"- Top vendors: {format_counter_inline(vendor_counts)}")
    lines.append(f"- Top repeated values: {format_counter_inline(repeated_values)}")
    lines.append("")
    if not facts:
        lines.append("No facts extracted for this category.")
        lines.append("")
        return "\n".join(lines)
    lines.append("| Confidence | Review | Date | Message | Item type | Vendor | Value | Price | Evidence | Photo |")
    lines.append("|---|---:|---|---:|---|---|---|---:|---|---|")
    for fact in sort_facts(facts)[:50]:
        value = fact.color_code or fact.finish or fact.material or fact.color or fact.model or fact.item_name or ""
        price = format_price(fact)
        lines.append(
            "| "
            f"`{fact.confidence}` | "
            f"{int(fact.needs_review)} | "
            f"`{fact.date or ''}` | "
            f"{fact.source_message_id} | "
            f"`{fact.item_type}` | "
            f"{escape_markdown_cell(fact.vendor_normalized or fact.vendor_raw or '')} | "
            f"{escape_markdown_cell(value)} | "
            f"{escape_markdown_cell(price)} | "
            f"{escape_markdown_cell(fact.evidence_quote)} | "
            f"{escape_markdown_cell(fact.first_photo_path or '')} |"
        )
    lines.append("")
    return "\n".join(lines)


def format_counter(counter: Counter[str], *, limit: int = 12) -> str:
    if not counter:
        return "No values parsed.\n"
    lines = ["| Value | Count |", "|---|---:|"]
    for value, count in counter.most_common(limit):
        lines.append(f"| {escape_markdown_cell(value)} | {count} |")
    lines.append("")
    return "\n".join(lines)


def format_counter_inline(counter: Counter[str], *, limit: int = 8) -> str:
    if not counter:
        return "none"
    return ", ".join(f"`{value}` ({count})" for value, count in counter.most_common(limit))


def format_price(fact: ExtractedFact) -> str:
    if fact.price_value is None:
        return ""
    value = int(fact.price_value) if float(fact.price_value).is_integer() else fact.price_value
    unit = f"/{fact.price_unit}" if fact.price_unit else ""
    return f"{value} {fact.price_currency or ''}{unit}".strip()


def escape_markdown_cell(value: str) -> str:
    return compact_whitespace(value).replace("|", "\\|")
