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
    CONFIDENCE_SORT_VALUE,
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
    remove_price_and_article,
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
    if not include_needs_review:
        facts = [fact for fact in facts if not fact.needs_review]
    facts = sort_facts(facts)

    output_files: list[Path] = []
    if output_format in {"csv", "all"}:
        csv_path = out_dir / "extracted_facts.csv"
        write_facts_csv(facts, csv_path)
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
                "include_needs_review": include_needs_review,
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
            **context,
        )
    return make_product_fact(
        block,
        category="kitchens",
        item_type="kitchen",
        extraction_method="regex:kitchen",
        confidence_hint="medium",
        **context,
    )


def extract_table_fact(block: str, **context: Any) -> ExtractedFact | None:
    lowered = normalize_for_match(block)
    if "стол" not in lowered and "подстоль" not in lowered:
        return None
    if "столешниц" in lowered and "кухн" in lowered:
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
    if not any(marker in lowered for marker in ["диван", "софа"]):
        return None
    item_type = "sofa"
    if "диван-кровать" in lowered or "кровать-диван" in lowered:
        item_type = "sofa_bed"
    elif "софа" in lowered:
        item_type = "couch"
    descriptor = descriptor_after_keywords(block, ["диван-кровать", "кровать-диван", "диван", "софа"])
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
        ["рабочее кресло", "кресло", "обеденный стул", "кухонный стул", "стул", "стулья"],
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
    if "фартук" in lowered and not any(marker in lowered for marker in ["на полу", "на пол", "для пола"]):
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
    if any(marker in lowered for marker in ["ванн", "сануз", "душев"]):
        room_context = "bathroom"
        needs_review = True

    descriptor = descriptor_after_keywords(
        block,
        ["плитка на полу", "плитка на пол", "керамогранит", "кварцвинил", "ламинат", "паркет"],
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
        notes="bathroom floor tile needs room-specific review" if needs_review else None,
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
    if not any(
        marker in lowered
        for marker in ["тв тумб", "тв-тумб", "тумба под тв", "телевизор", "гостин", "комод", "стеллаж", "полк", "консол"]
    ):
        return None
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
    descriptor = descriptor_after_keywords(
        block,
        ["тв тумба", "тв-тумба", "тумба под тв", "комод", "стеллаж", "полки", "полка", "консоль"],
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

    review_notes: list[str] = []
    if notes:
        review_notes.append(notes)
    if confidence == "low":
        review_notes.append("low confidence deterministic match")
    if not (vendor_normalized or article_id or price):
        review_notes.append("no vendor, article id, or price parsed")
    if suspiciously_long_descriptor(item_name) or suspiciously_long_descriptor(model):
        review_notes.append("suspiciously long descriptor")
    if count_item_triggers(evidence_quote) > 2:
        review_notes.append("quote may contain several unrelated items")

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
    cleaned = remove_price_and_article(without_promos(block))
    vendor = find_vendor(cleaned)
    if vendor:
        cleaned = re.sub(re.escape(vendor.raw), " ", cleaned, flags=re.IGNORECASE)
    lowered = normalize_for_match(cleaned)
    best_index: int | None = None
    best_keyword = ""
    for keyword in keywords:
        index = lowered.find(normalize_for_match(keyword))
        if index >= 0 and (best_index is None or index < best_index):
            best_index = index
            best_keyword = keyword
    if best_index is not None:
        cleaned = cleaned[best_index + len(best_keyword) :]
    cleaned = re.sub(r"^\s*\d+[.)]\s*", "", cleaned)
    cleaned = re.sub(r"(?i)\b(ozon|wb|вб|ям)\b", " ", cleaned)
    cleaned = compact_whitespace(cleaned.strip(" -:;,.+"))
    return cleaned or None


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

    by_category = out_dir / "by_category"
    by_category.mkdir(parents=True, exist_ok=True)
    descriptions = {category.category_id: category.description for category in load_category_configs()}
    for category in STRICT_CATEGORIES:
        path = by_category / f"{category}.md"
        path.write_text(
            build_category_markdown(
                category,
                [fact for fact in result.facts if fact.category == category],
                descriptions.get(category, ""),
            ),
            encoding="utf-8",
        )
        output_files.append(path)
    review_path = by_category / "needs_review.md"
    review_path.write_text(
        build_category_markdown(
            "needs_review",
            [fact for fact in result.facts if fact.needs_review],
            "Facts retained with needs_review = true.",
        ),
        encoding="utf-8",
    )
    output_files.append(review_path)
    return output_files


def build_summary_markdown(result: FactExtractionResult) -> str:
    facts = result.facts
    category_counts = Counter(fact.category for fact in facts)
    confidence_counts = Counter(fact.confidence for fact in facts)
    vendor_counts = Counter(fact.vendor_normalized for fact in facts if fact.vendor_normalized)
    item_counts = Counter(fact.item_type for fact in facts)
    color_counts = Counter(fact.color_code for fact in facts if fact.color_code)
    kitchen_facades = Counter(
        fact.finish for fact in facts if fact.category == "kitchens" and fact.finish
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
    lines.append(f"- Needs review: {sum(1 for fact in facts if fact.needs_review)}")
    lines.append("")
    lines.append(
        "This is a deterministic evidence table, not a father-facing renovation cheat sheet. "
        "It does not use LLMs, OCR, VLMs, LM Studio, OpenRouter, comments sync, or internet search."
    )
    lines.append("")
    lines.append("## Facts by Category")
    lines.append("")
    lines.append("| Category | Facts |")
    lines.append("|---|---:|")
    for category in STRICT_CATEGORIES:
        lines.append(f"| `{category}` | {category_counts[category]} |")
    lines.append("")
    lines.append("## Confidence")
    lines.append("")
    lines.append("| Confidence | Facts |")
    lines.append("|---|---:|")
    for confidence in ["high", "medium", "low"]:
        lines.append(f"| `{confidence}` | {confidence_counts[confidence]} |")
    lines.append("")
    lines.append(f"## Top Normalized Vendors\n\n{format_counter(vendor_counts)}")
    lines.append(f"## Top Item Types\n\n{format_counter(item_counts)}")
    lines.append(f"## Top Wall Color Codes\n\n{format_counter(color_counts)}")
    lines.append(f"## Top Kitchen Facade Phrases\n\n{format_counter(kitchen_facades)}")
    lines.append(f"## Top Sofa Fabrics / Colors\n\n{format_counter(sofa_values)}")
    lines.append("## Limitations")
    lines.append("")
    lines.append("- The parser is deterministic and conservative; unclear fields are left empty.")
    lines.append("- Needs-review rows flag ambiguous room context, sparse details, or broad matches.")
    lines.append("- Generated facts are draft evidence rows for later validation, not recommendations.")
    lines.append("")
    return "\n".join(lines)


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
