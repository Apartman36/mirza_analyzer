from __future__ import annotations

import csv
import json
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .extraction_patterns import CONFIDENCE_SORT_VALUE, STRICT_CATEGORIES
from .utils import compact_whitespace, utc_now_iso


FATHER_CATEGORIES = tuple(STRICT_CATEGORIES)
REPORT_FORMATS = ("markdown",)
DEFAULT_REPORT_TITLE = "Шпаргалка по повторяющимся решениям Ольги Мирзабаевой"

CATEGORY_TITLES = {
    "flooring": "Напольные покрытия",
    "wall_colors": "Цвета стен",
    "kitchens": "Кухни и фасады",
    "chairs": "Стулья",
    "tables": "Столы",
    "sofas": "Диваны",
    "hallway": "Прихожая",
    "living_room_furniture": "Мебель гостиной",
}

MAIN_SECTION_ORDER = [
    "wall_colors",
    "flooring",
    "kitchens",
    "sofas",
    "chairs",
    "tables",
    "hallway",
    "living_room_furniture",
]

CONFIDENCE_LABELS = {
    "high": "высокая",
    "medium": "средняя",
    "low": "низкая",
}


@dataclass(frozen=True)
class SourceFact:
    fact_id: int
    source_message_id: int
    category: str
    item_type: str
    evidence_quote: str
    source_date: str | None = None
    item_name: str | None = None
    vendor_raw: str | None = None
    vendor_normalized: str | None = None
    brand_raw: str | None = None
    brand_normalized: str | None = None
    model: str | None = None
    material: str | None = None
    finish: str | None = None
    color: str | None = None
    color_code: str | None = None
    article_id: str | None = None
    marketplace: str | None = None
    price_value: float | int | None = None
    price_currency: str | None = None
    price_unit: str | None = None
    room_context: str | None = None
    source_confidence: str = "medium"
    needs_review: bool = False
    notes: str | None = None
    photo_path: str | None = None


@dataclass(frozen=True)
class LLMReview:
    fact_id: int
    source_message_id: int
    decision: str
    category_correct: bool
    item_type_correct: bool
    price_correct: bool | None
    is_bundle: bool
    is_context_false_positive: bool
    is_non_target_room: bool
    corrected: dict[str, Any]
    normalized_terms: dict[str, Any]
    rationale_short: str
    confidence: str
    review_db: str
    created_at: str | None = None


@dataclass(frozen=True)
class EffectiveFact:
    fact_id: int
    source_message_id: int
    category: str
    item_type: str
    evidence_quote: str
    source_date: str | None = None
    vendor: str | None = None
    brand: str | None = None
    model: str | None = None
    material: str | None = None
    finish: str | None = None
    color: str | None = None
    color_code: str | None = None
    article_id: str | None = None
    marketplace: str | None = None
    price_value: float | int | None = None
    price_currency: str | None = None
    price_unit: str | None = None
    room_context: str | None = None
    photo_path: str | None = None
    source_confidence: str = "medium"
    review_decision: str | None = None
    report_status: str = "main"
    report_notes: str | None = None
    price_reliable: bool = True


@dataclass(frozen=True)
class ExcludedFact:
    fact_id: int
    source_message_id: int
    category: str
    item_type: str
    evidence_quote: str
    reason: str
    source_confidence: str
    needs_review: bool
    review_decision: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class ReportOptions:
    min_confidence: str = "medium"
    include_low_confidence: bool = False
    max_examples_per_section: int = 12
    max_top_values: int = 15
    strict: bool = True
    include_appendix: bool = True
    report_title: str = DEFAULT_REPORT_TITLE
    generated_note: bool = True
    language: str = "ru"


@dataclass(frozen=True)
class ReportDataset:
    facts: list[EffectiveFact]
    excluded: list[ExcludedFact]
    source_fact_count: int
    review_count: int
    review_decisions: Counter[str] = field(default_factory=Counter)
    exclusion_reasons: Counter[str] = field(default_factory=Counter)
    applied_fix_count: int = 0
    price_suppressed_count: int = 0


@dataclass(frozen=True)
class FatherReportResult:
    out_dir: Path
    output_files: list[Path]
    dataset: ReportDataset
    generated_at: str


def build_father_report(
    *,
    facts_db: Path,
    out_dir: Path,
    llm_review_dbs: Sequence[Path] = (),
    canonical_db: Path | None = Path("outputs/mirza.sqlite"),
    output_format: str = "markdown",
    min_confidence: str = "medium",
    include_low_confidence: bool = False,
    max_examples_per_section: int = 12,
    max_top_values: int = 15,
    strict: bool = True,
    include_appendix: bool = True,
    report_title: str = DEFAULT_REPORT_TITLE,
    generated_note: bool = True,
    language: str = "ru",
) -> FatherReportResult:
    if output_format not in REPORT_FORMATS:
        raise ValueError("Stage 3 supports Markdown output only. Use --format markdown.")
    if min_confidence not in CONFIDENCE_SORT_VALUE:
        raise ValueError("--min-confidence must be one of: low, medium, high")
    if language != "ru":
        raise ValueError("Stage 3 currently supports only Russian Markdown output: --language ru")
    if not facts_db.exists():
        raise FileNotFoundError(f"facts database not found: {facts_db}")

    out_dir.mkdir(parents=True, exist_ok=True)
    generated_at = utc_now_iso()
    options = ReportOptions(
        min_confidence=min_confidence,
        include_low_confidence=include_low_confidence,
        max_examples_per_section=max_examples_per_section,
        max_top_values=max_top_values,
        strict=strict,
        include_appendix=include_appendix,
        report_title=report_title,
        generated_note=generated_note,
        language=language,
    )

    source_facts = load_source_facts(facts_db)
    photo_paths = load_canonical_photo_paths(canonical_db) if canonical_db else {}
    source_facts = [
        fact if fact.photo_path else _replace_photo_path(fact, photo_paths.get(fact.source_message_id))
        for fact in source_facts
    ]
    reviews = load_llm_reviews(llm_review_dbs)
    dataset = build_report_dataset(source_facts, reviews, options)

    output_files = write_report_outputs(
        dataset=dataset,
        out_dir=out_dir,
        options=options,
        generated_at=generated_at,
        facts_db=facts_db,
        llm_review_dbs=llm_review_dbs,
    )
    return FatherReportResult(
        out_dir=out_dir,
        output_files=output_files,
        dataset=dataset,
        generated_at=generated_at,
    )


def load_source_facts(facts_db: Path) -> list[SourceFact]:
    sqlite_uri = f"{facts_db.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(sqlite_uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                id, source_message_id, date, category, item_type, item_name,
                vendor_raw, vendor_normalized, brand_raw, brand_normalized,
                model, material, finish, color, color_code, article_id,
                marketplace, price_value, price_currency, price_unit,
                room_context, evidence_quote, confidence, needs_review,
                notes, first_photo_path
            FROM extracted_facts
            ORDER BY id
            """
        ).fetchall()
    return [
        SourceFact(
            fact_id=int(row["id"]),
            source_message_id=int(row["source_message_id"]),
            source_date=row["date"],
            category=row["category"],
            item_type=row["item_type"],
            item_name=_clean(row["item_name"]),
            vendor_raw=_clean(row["vendor_raw"]),
            vendor_normalized=_clean(row["vendor_normalized"]),
            brand_raw=_clean(row["brand_raw"]),
            brand_normalized=_clean(row["brand_normalized"]),
            model=_clean(row["model"]),
            material=_clean(row["material"]),
            finish=_clean(row["finish"]),
            color=_clean(row["color"]),
            color_code=_clean(row["color_code"]),
            article_id=_clean(row["article_id"]),
            marketplace=_clean(row["marketplace"]),
            price_value=row["price_value"],
            price_currency=_clean(row["price_currency"]),
            price_unit=_clean(row["price_unit"]),
            room_context=_clean(row["room_context"]),
            evidence_quote=_clean(row["evidence_quote"]) or "",
            source_confidence=row["confidence"] or "medium",
            needs_review=bool(row["needs_review"]),
            notes=_clean(row["notes"]),
            photo_path=_clean(row["first_photo_path"]),
        )
        for row in rows
    ]


def load_llm_reviews(review_dbs: Sequence[Path]) -> dict[int, LLMReview]:
    reviews: dict[int, LLMReview] = {}
    for db_path in review_dbs:
        if not db_path.exists():
            raise FileNotFoundError(f"LLM review database not found: {db_path}")
        sqlite_uri = f"{db_path.resolve().as_uri()}?mode=ro"
        with sqlite3.connect(sqlite_uri, uri=True) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT
                    fact_id, source_message_id, decision, category_correct,
                    item_type_correct, price_correct, is_bundle,
                    is_context_false_positive, is_non_target_room,
                    corrected_json, normalized_terms_json, rationale_short,
                    confidence, created_at
                FROM llm_review_results
                ORDER BY id
                """
            ).fetchall()
        for row in rows:
            reviews[int(row["fact_id"])] = LLMReview(
                fact_id=int(row["fact_id"]),
                source_message_id=int(row["source_message_id"]),
                decision=row["decision"],
                category_correct=bool(row["category_correct"]),
                item_type_correct=bool(row["item_type_correct"]),
                price_correct=(
                    None if row["price_correct"] is None else bool(row["price_correct"])
                ),
                is_bundle=bool(row["is_bundle"]),
                is_context_false_positive=bool(row["is_context_false_positive"]),
                is_non_target_room=bool(row["is_non_target_room"]),
                corrected=_load_json_object(row["corrected_json"]),
                normalized_terms=_load_json_object(row["normalized_terms_json"]),
                rationale_short=row["rationale_short"] or "",
                confidence=row["confidence"] or "medium",
                review_db=str(db_path),
                created_at=row["created_at"],
            )
    return reviews


def load_canonical_photo_paths(canonical_db: Path | None) -> dict[int, str]:
    if not canonical_db or not canonical_db.exists():
        return {}
    sqlite_uri = f"{canonical_db.resolve().as_uri()}?mode=ro"
    try:
        with sqlite3.connect(sqlite_uri, uri=True) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT telegram_message_id, absolute_path
                FROM media
                WHERE media_kind = 'photo'
                ORDER BY telegram_message_id, id
                """
            ).fetchall()
    except sqlite3.Error:
        return {}
    paths: dict[int, str] = {}
    for row in rows:
        paths.setdefault(int(row["telegram_message_id"]), row["absolute_path"])
    return paths


def build_report_dataset(
    source_facts: Sequence[SourceFact],
    reviews: dict[int, LLMReview],
    options: ReportOptions | None = None,
) -> ReportDataset:
    options = options or ReportOptions()
    effective: list[EffectiveFact] = []
    excluded: list[ExcludedFact] = []
    applied_fix_count = 0
    price_suppressed_count = 0

    for fact in source_facts:
        review = reviews.get(fact.fact_id)
        if review is not None:
            result = _effective_from_reviewed_fact(fact, review, options)
            if isinstance(result, EffectiveFact):
                effective.append(result)
                if review.decision == "fix":
                    applied_fix_count += 1
                if not result.price_reliable:
                    price_suppressed_count += 1
            else:
                excluded.append(result)
            continue

        result = _effective_from_unreviewed_fact(fact, options)
        if isinstance(result, EffectiveFact):
            effective.append(result)
        else:
            excluded.append(result)

    return ReportDataset(
        facts=sort_effective_facts(effective),
        excluded=excluded,
        source_fact_count=len(source_facts),
        review_count=len(reviews),
        review_decisions=Counter(review.decision for review in reviews.values()),
        exclusion_reasons=Counter(item.reason for item in excluded),
        applied_fix_count=applied_fix_count,
        price_suppressed_count=price_suppressed_count,
    )


def _effective_from_unreviewed_fact(
    fact: SourceFact,
    options: ReportOptions,
) -> EffectiveFact | ExcludedFact:
    if fact.category not in FATHER_CATEGORIES:
        return _exclude(fact, "outside_father_categories")
    if not fact.evidence_quote:
        return _exclude(fact, "missing_evidence_quote")
    if fact.needs_review:
        return _exclude(fact, "deterministic_needs_review_without_llm")
    if (
        not options.include_low_confidence
        and CONFIDENCE_SORT_VALUE.get(fact.source_confidence, 0)
        < CONFIDENCE_SORT_VALUE[options.min_confidence]
    ):
        return _exclude(fact, "below_min_confidence")
    if options.strict and _looks_like_general_essay(fact):
        return _exclude(fact, "general_or_non_concrete_evidence")
    return _source_to_effective(fact, review_decision=None, report_notes=None)


def _effective_from_reviewed_fact(
    fact: SourceFact,
    review: LLMReview,
    options: ReportOptions,
) -> EffectiveFact | ExcludedFact:
    if not fact.evidence_quote:
        return _exclude(fact, "missing_evidence_quote", review)
    if review.decision == "discard":
        return _exclude(fact, "llm_discard", review)
    if review.decision == "needs_human":
        return _exclude(fact, "llm_needs_human", review)
    if review.decision not in {"keep", "fix"}:
        return _exclude(fact, "llm_unknown_decision", review)
    if review.is_context_false_positive:
        return _exclude(fact, "llm_context_false_positive", review)
    if options.strict and review.is_non_target_room:
        return _exclude(fact, "llm_non_target_room", review)

    corrected = review.corrected or {}
    corrected_category = _clean(corrected.get("category"))
    target_category = fact.category
    if corrected_category:
        if corrected_category not in FATHER_CATEGORIES:
            return _exclude(fact, "corrected_category_outside_father_categories", review)
        if review.decision == "fix" or review.category_correct:
            target_category = corrected_category
    if target_category not in FATHER_CATEGORIES:
        return _exclude(fact, "outside_father_categories", review)
    if not review.category_correct and review.decision != "fix":
        return _exclude(fact, "llm_category_conflict", review)

    corrected_item_type = _clean(corrected.get("item_type"))
    if not review.item_type_correct and not corrected_item_type:
        return _exclude(fact, "llm_item_type_conflict", review)

    if options.strict and _rationale_has_hard_conflict(review.rationale_short):
        return _exclude(fact, "llm_rationale_conflict", review)
    if options.strict and _looks_like_general_essay(fact):
        return _exclude(fact, "general_or_non_concrete_evidence", review)

    price_reliable = review.price_correct is not False
    effective = _source_to_effective(
        fact,
        review_decision=review.decision,
        report_notes=_review_note(review),
    )
    effective = _apply_review_overrides(effective, review, target_category, corrected_item_type)
    if not price_reliable:
        effective = _replace_effective(
            effective,
            price_value=None,
            price_unit=None,
            price_currency=None,
            price_reliable=False,
            report_notes=_join_notes(effective.report_notes, "цена скрыта: LLM отметил цену как ненадёжную"),
        )
    if review.decision == "fix" and not _fix_is_coherent(fact, review, effective):
        return _exclude(fact, "llm_fix_not_coherent", review)
    return effective


def _apply_review_overrides(
    fact: EffectiveFact,
    review: LLMReview,
    target_category: str,
    corrected_item_type: str | None,
) -> EffectiveFact:
    corrected = review.corrected or {}
    terms = review.normalized_terms or {}
    evidence = fact.evidence_quote

    values: dict[str, Any] = {"category": target_category}
    if corrected_item_type:
        values["item_type"] = corrected_item_type

    field_map = {
        "model": "model",
        "material": "material",
        "finish": "finish",
        "color": "color",
        "color_code": "color_code",
        "article_id": "article_id",
        "price_unit": "price_unit",
        "room_context": "room_context",
    }
    for source_name, target_name in field_map.items():
        value = _clean(corrected.get(source_name))
        if not value:
            continue
        if source_name in {"room_context", "price_unit"} or _field_supported_by_evidence(value, evidence):
            values[target_name] = value

    vendor_value = _clean(corrected.get("vendor_normalized")) or _clean(terms.get("vendor"))
    brand_update, material_update = _spc_vendor_repairs(vendor_value, evidence)
    if brand_update and not fact.brand:
        values["brand"] = brand_update
    if material_update and not fact.material:
        values["material"] = material_update
    if vendor_value:
        normalized_vendor = normalize_vendor(vendor_value)
        if normalized_vendor and normalized_vendor.lower() != "spc":
            if _field_supported_by_evidence(vendor_value, evidence) or _vendor_supported_by_evidence(normalized_vendor, evidence):
                values["vendor"] = normalized_vendor

    corrected_price = corrected.get("price_value")
    if corrected_price is not None and review.price_correct is not False:
        if _price_is_plausible(corrected_price) and _number_supported_by_evidence(corrected_price, evidence):
            values["price_value"] = corrected_price

    if not fact.color_code:
        color_code = _clean(terms.get("wall_color_code"))
        if color_code and _field_supported_by_evidence(color_code, evidence):
            values["color_code"] = normalize_wall_color_code(color_code)
    if fact.category == "sofas" or target_category == "sofas":
        sofa_fabric = _clean(terms.get("sofa_fabric"))
        sofa_color = _clean(terms.get("sofa_color"))
        if sofa_fabric and _field_supported_by_evidence(sofa_fabric, evidence):
            values["material"] = sofa_fabric
        if sofa_color and _field_supported_by_evidence(sofa_color, evidence):
            values["color"] = sofa_color
    if target_category == "flooring" and not fact.brand:
        flooring_brand = _clean(terms.get("flooring_brand_or_collection"))
        if flooring_brand and flooring_brand.lower() not in {"арт", "art", "spc"}:
            if _field_supported_by_evidence(flooring_brand, evidence):
                values["brand"] = flooring_brand
    if target_category == "kitchens" and not fact.finish:
        facade_materials = terms.get("facade_materials") or []
        if isinstance(facade_materials, list):
            supported = [
                _clean(value)
                for value in facade_materials
                if _clean(value) and _field_supported_by_evidence(str(value), evidence)
            ]
            if supported:
                values["finish"] = normalize_kitchen_finish(" + ".join(str(v) for v in supported))

    if "color_code" in values:
        values["color_code"] = normalize_wall_color_code(values["color_code"])
    if "finish" in values:
        values["finish"] = normalize_kitchen_finish(values["finish"])
    if "vendor" not in values and fact.vendor:
        values["vendor"] = normalize_vendor(fact.vendor)
    if "marketplace" not in values and fact.marketplace:
        values["marketplace"] = normalize_vendor(fact.marketplace) or fact.marketplace

    return _replace_effective(fact, **values)


def _source_to_effective(
    fact: SourceFact,
    *,
    review_decision: str | None,
    report_notes: str | None,
) -> EffectiveFact:
    brand_update, material_update = _spc_vendor_repairs(
        fact.vendor_normalized or fact.vendor_raw,
        fact.evidence_quote,
    )
    vendor = normalize_vendor(fact.vendor_normalized or fact.vendor_raw)
    if vendor and vendor.lower() == "spc":
        vendor = None
    brand = fact.brand_normalized or fact.brand_raw or brand_update
    material = fact.material or material_update
    return EffectiveFact(
        fact_id=fact.fact_id,
        source_message_id=fact.source_message_id,
        source_date=fact.source_date,
        category=fact.category,
        item_type=fact.item_type,
        vendor=vendor,
        brand=brand,
        model=fact.model,
        material=material,
        finish=normalize_kitchen_finish(fact.finish) if fact.finish else None,
        color=fact.color,
        color_code=normalize_wall_color_code(fact.color_code) if fact.color_code else None,
        article_id=fact.article_id,
        marketplace=normalize_vendor(fact.marketplace) or fact.marketplace,
        price_value=fact.price_value,
        price_currency=fact.price_currency,
        price_unit=fact.price_unit,
        room_context=fact.room_context,
        evidence_quote=fact.evidence_quote,
        photo_path=fact.photo_path,
        source_confidence=fact.source_confidence,
        review_decision=review_decision,
        report_status="main",
        report_notes=report_notes,
        price_reliable=True,
    )


def sort_effective_facts(facts: Iterable[EffectiveFact]) -> list[EffectiveFact]:
    return sorted(
        facts,
        key=lambda fact: (
            CONFIDENCE_SORT_VALUE.get(fact.source_confidence, 0),
            1 if fact.review_decision in {"keep", "fix"} else 0,
            fact.source_date or "",
            fact.fact_id,
        ),
        reverse=True,
    )


def write_report_outputs(
    *,
    dataset: ReportDataset,
    out_dir: Path,
    options: ReportOptions,
    generated_at: str,
    facts_db: Path,
    llm_review_dbs: Sequence[Path],
) -> list[Path]:
    output_files: list[Path] = []

    category_dir = out_dir / "category_sections"
    category_dir.mkdir(parents=True, exist_ok=True)
    section_texts: dict[str, str] = {}
    for category in FATHER_CATEGORIES:
        text = build_category_section(
            category,
            [fact for fact in dataset.facts if fact.category == category],
            dataset=dataset,
            options=options,
            standalone=True,
        )
        path = category_dir / f"{category}.md"
        path.write_text(text, encoding="utf-8")
        output_files.append(path)
        section_texts[category] = build_category_section(
            category,
            [fact for fact in dataset.facts if fact.category == category],
            dataset=dataset,
            options=options,
            standalone=False,
        )

    report_path = out_dir / "father_report.md"
    report_path.write_text(
        build_full_report_markdown(
            dataset=dataset,
            options=options,
            generated_at=generated_at,
            facts_db=facts_db,
            llm_review_dbs=llm_review_dbs,
            section_texts=section_texts,
        ),
        encoding="utf-8",
    )
    output_files.insert(0, report_path)

    summary_path = out_dir / "father_report_summary.md"
    summary_path.write_text(
        build_summary_markdown(dataset, options, generated_at),
        encoding="utf-8",
    )
    output_files.append(summary_path)

    quality_path = out_dir / "data_quality_notes.md"
    quality_path.write_text(
        build_data_quality_notes(dataset, facts_db, llm_review_dbs, generated_at),
        encoding="utf-8",
    )
    output_files.append(quality_path)

    used_csv = out_dir / "source_facts_used.csv"
    write_used_csv(dataset.facts, used_csv)
    output_files.append(used_csv)

    excluded_csv = out_dir / "source_facts_excluded.csv"
    write_excluded_csv(dataset.excluded, excluded_csv)
    output_files.append(excluded_csv)

    return output_files


def build_full_report_markdown(
    *,
    dataset: ReportDataset,
    options: ReportOptions,
    generated_at: str,
    facts_db: Path,
    llm_review_dbs: Sequence[Path],
    section_texts: dict[str, str],
) -> str:
    lines: list[str] = [f"# {options.report_title}", ""]
    if options.generated_note:
        lines.extend(
            [
                f"_Сгенерировано локально: {generated_at}. Формат: Markdown._",
                "",
            ]
        )
    lines.extend(build_how_to_read(facts_db, llm_review_dbs))
    lines.extend(build_executive_summary(dataset, options))
    lines.extend(build_reliability_section(dataset))
    for category in MAIN_SECTION_ORDER:
        lines.append(section_texts[category])
    lines.extend(build_suppliers_section(dataset, options))
    lines.extend(build_manual_review_section(dataset))
    if options.include_appendix:
        lines.extend(build_appendix_sources(dataset, options))
        lines.extend(build_technical_appendix(dataset, facts_db, llm_review_dbs))
    return "\n".join(lines).rstrip() + "\n"


def build_how_to_read(facts_db: Path, llm_review_dbs: Sequence[Path]) -> list[str]:
    review_note = (
        "LLM-проверка использована как слой QA/уточнений, а не как финальная истина."
        if llm_review_dbs
        else "LLM-проверка не передана; использованы только чистые детерминированные факты."
    )
    return [
        "## Как читать этот документ",
        "",
        "Это рабочая шпаргалка по повторяющимся решениям из локального Telegram-экспорта канала. "
        "Это не дизайн-проект, не план конкретной квартиры и не рекомендация купить конкретную позицию.",
        "",
        "Основа отчёта — текстовые посты, извлечённые артикулы, цены, магазины, модели, цвета и короткие цитаты-доказательства. "
        "Фотографии в этой версии не анализируются OCR/VLM; если путь к фото уже есть в данных, он указан только как локальная ссылка текстом.",
        "",
        f"{review_note} Детерминированная база фактов остаётся источником записи: `{facts_db}`.",
        "",
    ]


def build_executive_summary(dataset: ReportDataset, options: ReportOptions) -> list[str]:
    facts = dataset.facts
    all_vendor_counts = count_values(facts, lambda fact: fact.vendor)
    kitchen_facts = [fact for fact in facts if fact.category == "kitchens"]
    flooring_facts = [fact for fact in facts if fact.category == "flooring"]
    wall_counts = count_values(
        [fact for fact in facts if fact.category == "wall_colors"],
        lambda fact: normalize_wall_color_code(fact.color_code or fact.color),
    )
    marketplace_counts = count_values(facts, lambda fact: fact.marketplace)

    bullets: list[str] = []
    if wall_counts.get("G482", 0):
        bullets.append(
            f"Главный повторяющийся код стен по извлечённым фактам — `G482` ({wall_counts['G482']} упомин.)."
        )
    if all_vendor_counts.get("Divan.ru", 0):
        bullets.append(
            f"`Divan.ru` часто встречается в строках про диваны, стулья и мебель ({all_vendor_counts['Divan.ru']} фактов в отчёте)."
        )
    if all_vendor_counts.get("Mebel.in", 0):
        bullets.append(
            f"`Mebel.in` заметно связан с кухнями, фасадами и корпусной мебелью ({all_vendor_counts['Mebel.in']} фактов)."
        )
    shop_hits = [
        shop for shop in ("OZON", "Wildberries", "Yandex Market")
        if all_vendor_counts.get(shop, 0) or marketplace_counts.get(shop, 0)
    ]
    if shop_hits:
        bullets.append(
            "Маркетплейсы "
            + ", ".join(f"`{shop}`" for shop in shop_hits)
            + " регулярно появляются для отдельных предметов и артикулов."
        )
    if _kitchen_has_wood_plus_neutral(kitchen_facts):
        bullets.append(
            "В кухнях похоже на повторяющийся паттерн: древесный декор фасадов плюс спокойный матовый/нейтральный цвет."
        )
    if flooring_facts:
        flooring_values = count_values(
            flooring_facts,
            lambda fact: normalize_flooring_material(fact.material or fact.brand or fact.finish or fact.evidence_quote),
        )
        top_flooring = ", ".join(
            f"`{value}`" for value, count in flooring_values.most_common(3) if value
        )
        if top_flooring:
            bullets.append(
                f"По полу выборка меньше, но в ней есть повторяемые строки про {top_flooring}; плитку для пола лучше перепроверять по контексту помещения."
            )
    if dataset.review_decisions:
        bullets.append(
            f"LLM-слой применил {dataset.applied_fix_count} исправлений; discard/needs_human не попали в основные разделы."
        )
    if not bullets:
        bullets.append("В текущей выборке недостаточно надёжных повторов для короткого вывода.")

    return [
        "## Короткий вывод",
        "",
        *[f"- {line}" for line in bullets[:10]],
        "",
    ]


def build_reliability_section(dataset: ReportDataset) -> list[str]:
    by_category = Counter(fact.category for fact in dataset.facts)
    excluded_by_category = Counter(item.category for item in dataset.excluded)
    fixed_labels = {
        "wall_colors": "высокая",
        "kitchens": "средняя/высокая",
        "sofas": "средняя/высокая",
        "chairs": "средняя/высокая",
        "tables": "средняя",
        "hallway": "средняя",
        "living_room_furniture": "средняя/низкая",
        "flooring": "средняя/низкая",
    }
    reasons = {
        "wall_colors": "коды цвета обычно извлекаются прямо из коротких строк",
        "kitchens": "много строк по фасадам и фартукам, но широкие покупки требуют проверки",
        "sofas": "прямые товарные строки надёжны; bundles и контекстные строки исключаются",
        "chairs": "много прямых товарных строк, часть маркетплейсных позиций требует проверки",
        "tables": "есть разные типы столов; журнальные столики перенесены сюда при LLM-fix",
        "hallway": "часть строк относится к наборам или широкому контексту прихожей",
        "living_room_furniture": "широкая категория, выше риск декора/света и контекстных строк",
        "flooring": "меньше фактов; плитка может относиться к ванной или фартуку",
    }
    lines = [
        "## Уровень надёжности данных",
        "",
        "| Раздел | Фактов в отчёте | Исключено | Надёжность | Почему |",
        "|---|---:|---:|---|---|",
    ]
    for category in FATHER_CATEGORIES:
        lines.append(
            f"| {CATEGORY_TITLES[category]} | {by_category[category]} | "
            f"{excluded_by_category[category]} | {fixed_labels[category]} | {reasons[category]} |"
        )
    lines.append("")
    return lines


def build_category_section(
    category: str,
    facts: list[EffectiveFact],
    *,
    dataset: ReportDataset,
    options: ReportOptions,
    standalone: bool,
) -> str:
    heading_number = MAIN_SECTION_ORDER.index(category) + 1 if category in MAIN_SECTION_ORDER else 0
    title = CATEGORY_TITLES.get(category, category)
    heading = f"# {title}" if standalone else f"## {heading_number}. {title}"
    if category == "wall_colors":
        lines = build_wall_colors_section(heading, facts, options)
    elif category == "flooring":
        lines = build_flooring_section(heading, facts, options)
    elif category == "kitchens":
        lines = build_kitchens_section(heading, facts, options)
    elif category == "sofas":
        lines = build_sofas_section(heading, facts, options)
    elif category == "chairs":
        lines = build_chairs_section(heading, facts, options)
    elif category == "tables":
        lines = build_tables_section(heading, facts, options)
    elif category == "hallway":
        lines = build_hallway_section(heading, facts, options)
    elif category == "living_room_furniture":
        lines = build_living_room_furniture_section(heading, facts, options)
    else:
        lines = build_generic_category_section(heading, facts, options)
    return "\n".join(lines).rstrip() + "\n"


def build_wall_colors_section(
    heading: str,
    facts: list[EffectiveFact],
    options: ReportOptions,
) -> list[str]:
    color_counts = count_values(
        facts,
        lambda fact: normalize_wall_color_code(fact.color_code or fact.color),
    )
    lines = [heading, "", f"Фактов в разделе: {len(facts)}.", ""]
    if color_counts.get("G482"):
        lines.extend(
            [
                f"Главный повторяющийся код — `G482`: {color_counts['G482']} упомин.",
                "",
            ]
        )
    lines.extend(counter_table("Код/цвет", color_counts, options.max_top_values))
    lines.extend(example_table(facts, limit=options.max_examples_per_section, include_value=True))
    lines.append("Важно: внешний вид краски по коду здесь не выводится; отчёт фиксирует только текстовые упоминания.")
    lines.append("")
    return lines


def build_flooring_section(
    heading: str,
    facts: list[EffectiveFact],
    options: ReportOptions,
) -> list[str]:
    type_counts = count_values(facts, lambda fact: readable_item_type(fact.item_type))
    material_counts = count_values(
        facts,
        lambda fact: normalize_flooring_material(fact.material or fact.brand or fact.finish or fact.evidence_quote),
    )
    vendor_counts = count_values(facts, lambda fact: fact.vendor or fact.brand)
    lines = [heading, "", f"Фактов в разделе: {len(facts)}.", ""]
    lines.extend(subsection_counter("Типы покрытия", "Тип", type_counts, options.max_top_values))
    lines.extend(subsection_counter("Материалы / бренды / коллекции", "Значение", material_counts, options.max_top_values))
    lines.extend(subsection_counter("Поставщики / магазины", "Поставщик", vendor_counts, options.max_top_values))
    lines.extend(example_table(facts, limit=options.max_examples_per_section, include_value=True))
    lines.append("Осторожно: напольных фактов меньше, а часть плитки может быть контекстом ванной или фартука.")
    lines.append("")
    return lines


def build_kitchens_section(
    heading: str,
    facts: list[EffectiveFact],
    options: ReportOptions,
) -> list[str]:
    vendor_counts = count_values(facts, lambda fact: fact.vendor or fact.marketplace)
    finish_counts = count_values(
        [fact for fact in facts if fact.finish or "фасад" in fact.item_type.lower()],
        lambda fact: normalize_kitchen_finish(fact.finish or fact.model or fact.color),
    )
    countertop_backsplash = [
        fact for fact in facts if any(token in fact.item_type.lower() for token in ("countertop", "backsplash", "fartuk", "tile"))
    ]
    article_counts = count_values(facts, lambda fact: fact.marketplace or fact.vendor if fact.article_id else None)
    lines = [heading, "", f"Фактов в разделе: {len(facts)}.", ""]
    lines.extend(subsection_counter("Производители / где заказывает", "Поставщик", vendor_counts, options.max_top_values))
    lines.extend(subsection_counter("Фасады", "Фраза фасадов / отделки", finish_counts, options.max_top_values))
    lines.extend(subsection_counter("Столешницы / фартуки / артикулы", "Площадка", article_counts, options.max_top_values))
    if countertop_backsplash:
        lines.extend(example_table(countertop_backsplash, limit=min(6, options.max_examples_per_section), include_value=True))
    if _kitchen_has_wood_plus_neutral(facts):
        lines.append("Что важно: по извлечённым фактам часто встречается связка древесного декора и спокойного матового/нейтрального фасада.")
    else:
        lines.append("Что важно: выводы по фасадам ограничены теми формулировками, которые явно есть в цитатах.")
    lines.append("")
    lines.extend(example_table(facts, limit=options.max_examples_per_section, include_value=True))
    return lines


def build_sofas_section(
    heading: str,
    facts: list[EffectiveFact],
    options: ReportOptions,
) -> list[str]:
    vendor_counts = count_values(facts, lambda fact: fact.vendor or fact.marketplace)
    model_counts = count_values(facts, lambda fact: fact.model)
    material_counts = count_values(facts, lambda fact: normalize_sofa_material(fact.material))
    color_counts = count_values(facts, lambda fact: normalize_sofa_color(fact.color))
    lines = [heading, "", f"Фактов в разделе: {len(facts)}.", ""]
    lines.extend(subsection_counter("Поставщики", "Поставщик", vendor_counts, options.max_top_values))
    lines.extend(subsection_counter("Модели", "Модель", model_counts, options.max_top_values))
    lines.extend(subsection_counter("Материалы / ткани", "Материал", material_counts, options.max_top_values))
    lines.extend(subsection_counter("Цвета", "Цвет", color_counts, options.max_top_values))
    lines.extend(example_table(facts, limit=options.max_examples_per_section, include_value=True))
    lines.append("Bundles с общей ценой и строки, где диван был только контекстом, не включаются в основные выводы.")
    lines.append("")
    return lines


def build_chairs_section(
    heading: str,
    facts: list[EffectiveFact],
    options: ReportOptions,
) -> list[str]:
    vendor_counts = count_values(facts, lambda fact: fact.vendor or fact.marketplace)
    material_counts = count_values(facts, lambda fact: normalize_sofa_material(fact.material))
    color_counts = count_values(facts, lambda fact: normalize_sofa_color(fact.color))
    lines = [heading, "", f"Фактов в разделе: {len(facts)}.", ""]
    lines.extend(subsection_counter("Поставщики / магазины", "Поставщик", vendor_counts, options.max_top_values))
    lines.extend(subsection_counter("Материалы", "Материал", material_counts, options.max_top_values))
    lines.extend(subsection_counter("Цвета", "Цвет", color_counts, options.max_top_values))
    lines.extend(example_table(facts, limit=options.max_examples_per_section, include_value=True))
    return lines


def build_tables_section(
    heading: str,
    facts: list[EffectiveFact],
    options: ReportOptions,
) -> list[str]:
    type_counts = count_values(facts, lambda fact: readable_item_type(fact.item_type))
    vendor_counts = count_values(facts, lambda fact: fact.vendor or fact.marketplace)
    article_counts = count_values(facts, lambda fact: fact.article_id)
    lines = [heading, "", f"Фактов в разделе: {len(facts)}.", ""]
    lines.extend(subsection_counter("Типы столов", "Тип", type_counts, options.max_top_values))
    lines.extend(subsection_counter("Поставщики / магазины", "Поставщик", vendor_counts, options.max_top_values))
    lines.extend(subsection_counter("Артикулы", "Артикул", article_counts, options.max_top_values))
    lines.append("Журнальные столики относятся к разделу столов, включая строки, которые LLM исправил из мебели гостиной.")
    lines.append("")
    lines.extend(example_table(facts, limit=options.max_examples_per_section, include_value=True))
    return lines


def build_hallway_section(
    heading: str,
    facts: list[EffectiveFact],
    options: ReportOptions,
) -> list[str]:
    type_counts = count_values(facts, lambda fact: readable_item_type(fact.item_type))
    vendor_counts = count_values(facts, lambda fact: fact.vendor or fact.marketplace)
    lines = [heading, "", f"Фактов в разделе: {len(facts)}.", ""]
    lines.extend(subsection_counter("Предметы", "Предмет", type_counts, options.max_top_values))
    lines.extend(subsection_counter("Поставщики / магазины", "Поставщик", vendor_counts, options.max_top_values))
    lines.extend(example_table(facts, limit=options.max_examples_per_section, include_value=True))
    lines.append("Часть строк прихожей может быть набором нескольких предметов; такие строки лучше проверять вручную.")
    lines.append("")
    return lines


def build_living_room_furniture_section(
    heading: str,
    facts: list[EffectiveFact],
    options: ReportOptions,
) -> list[str]:
    type_counts = count_values(facts, lambda fact: readable_item_type(fact.item_type))
    vendor_counts = count_values(facts, lambda fact: fact.vendor or fact.marketplace)
    lines = [heading, "", f"Фактов в разделе: {len(facts)}.", ""]
    lines.extend(subsection_counter("Типы мебели", "Тип", type_counts, options.max_top_values))
    lines.extend(subsection_counter("Поставщики / магазины", "Поставщик", vendor_counts, options.max_top_values))
    lines.extend(example_table(facts, limit=options.max_examples_per_section, include_value=True))
    lines.append("В раздел не включаются люстры, бра, постеры, пледы и строки, где гостиная указана только как контекст.")
    lines.append("")
    return lines


def build_generic_category_section(
    heading: str,
    facts: list[EffectiveFact],
    options: ReportOptions,
) -> list[str]:
    vendor_counts = count_values(facts, lambda fact: fact.vendor or fact.marketplace)
    value_counts = count_values(facts, primary_value)
    lines = [heading, "", f"Фактов в разделе: {len(facts)}.", ""]
    lines.extend(subsection_counter("Поставщики / магазины", "Поставщик", vendor_counts, options.max_top_values))
    lines.extend(subsection_counter("Повторяющиеся значения", "Значение", value_counts, options.max_top_values))
    lines.extend(example_table(facts, limit=options.max_examples_per_section, include_value=True))
    return lines


def build_suppliers_section(dataset: ReportDataset, options: ReportOptions) -> list[str]:
    facts = dataset.facts
    vendor_counts = count_values(facts, lambda fact: fact.vendor or fact.marketplace)
    examples_by_vendor: dict[str, list[EffectiveFact]] = defaultdict(list)
    for fact in facts:
        vendor = fact.vendor or fact.marketplace
        if vendor:
            examples_by_vendor[vendor].append(fact)
    lines = [
        "## 9. Повторяющиеся поставщики и магазины",
        "",
        "| Поставщик | Фактов | Где чаще встречается | Что чаще связано | Примеры |",
        "|---|---:|---|---|---|",
    ]
    if not vendor_counts:
        lines.append("| нет данных | 0 | — | — | — |")
    for vendor, count in vendor_counts.most_common(options.max_top_values):
        vendor_facts = examples_by_vendor[vendor]
        where = ", ".join(
            CATEGORY_TITLES[category]
            for category, _ in Counter(fact.category for fact in vendor_facts).most_common(3)
        )
        values = ", ".join(
            value for value, _ in count_values(vendor_facts, primary_value).most_common(3)
        )
        examples = "; ".join(short_quote(fact.evidence_quote, 90) for fact in select_examples(vendor_facts, limit=2))
        lines.append(
            f"| {escape_md(vendor)} | {count} | {escape_md(where)} | {escape_md(values or 'позиции из цитат')} | {escape_md(examples)} |"
        )
    lines.append("")
    return lines


def build_manual_review_section(dataset: ReportDataset) -> list[str]:
    reasons = dataset.exclusion_reasons
    lines = [
        "## 10. Что требует ручной проверки",
        "",
        "В основной отчёт не попали строки, которые выглядят полезными только после ручной проверки или были явно отклонены.",
        "",
        "| Причина | Кол-во | Что это значит |",
        "|---|---:|---|",
    ]
    reason_labels = {
        "deterministic_needs_review_without_llm": "детерминированный факт помечен needs_review и не проверен LLM",
        "llm_discard": "LLM решил, что строка не является фактом для целевой категории",
        "llm_needs_human": "LLM оставил строку на ручную проверку",
        "llm_context_false_positive": "предмет был только контекстом",
        "llm_category_conflict": "конфликт категории",
        "llm_item_type_conflict": "конфликт типа предмета",
        "llm_non_target_room": "нецелевая комната или широкий контекст",
        "llm_rationale_conflict": "в объяснении LLM есть явный конфликт",
        "llm_fix_not_coherent": "исправление LLM не подтверждается цитатой",
        "general_or_non_concrete_evidence": "общий текст без конкретного товара/кода/поставщика",
        "below_min_confidence": "ниже выбранного порога уверенности",
        "missing_evidence_quote": "нет цитаты-доказательства",
    }
    if not reasons:
        lines.append("| нет | 0 | все входные факты прошли фильтр |")
    for reason, count in reasons.most_common():
        lines.append(f"| `{reason}` | {count} | {reason_labels.get(reason, 'исключено фильтром качества')} |")
    lines.extend(
        [
            "",
            "Типовые случаи: bundles с одной общей ценой, контекстные упоминания предметов, общие дизайн-эссе, строки без модели/магазина/кода и нецелевые комнаты.",
            "",
        ]
    )
    return lines


def build_appendix_sources(dataset: ReportDataset, options: ReportOptions) -> list[str]:
    examples = select_examples(dataset.facts, limit=min(40, max(20, options.max_examples_per_section * 2)))
    lines = [
        "## Appendix A. Примеры источников",
        "",
        "| Раздел | message_id | Дата | Цитата | Фото |",
        "|---|---:|---|---|---|",
    ]
    for fact in examples:
        lines.append(
            f"| {CATEGORY_TITLES.get(fact.category, fact.category)} | {fact.source_message_id} | "
            f"{escape_md(fact.source_date or '')} | {escape_md(fact.evidence_quote)} | "
            f"{escape_md(fact.photo_path or '')} |"
        )
    lines.append("")
    return lines


def build_technical_appendix(
    dataset: ReportDataset,
    facts_db: Path,
    llm_review_dbs: Sequence[Path],
) -> list[str]:
    review_paths = ", ".join(f"`{path}`" for path in llm_review_dbs) if llm_review_dbs else "не использовались"
    return [
        "## Appendix B. Техническая заметка",
        "",
        f"- Источник фактов: `{facts_db}`.",
        f"- LLM review DB: {review_paths}.",
        f"- В отчёт вошло фактов: {len(dataset.facts)}.",
        f"- Исключено фактов: {len(dataset.excluded)}.",
        "- Нет OCR/VLM, нет анализа изображений, нет HTML/PDF, нет новых LLM-вызовов.",
        "- Это черновая доказательная база для человека, а не финальный дизайн-проект.",
        "",
    ]


def build_summary_markdown(
    dataset: ReportDataset,
    options: ReportOptions,
    generated_at: str,
) -> str:
    category_counts = Counter(fact.category for fact in dataset.facts)
    lines = [
        "# Father Report Summary",
        "",
        f"- Generated at: `{generated_at}`",
        f"- Effective facts used: {len(dataset.facts)}",
        f"- Source facts read: {dataset.source_fact_count}",
        f"- Facts excluded: {len(dataset.excluded)}",
        f"- LLM review rows loaded: {dataset.review_count}",
        f"- LLM fix decisions applied: {dataset.applied_fix_count}",
        f"- Prices suppressed as unreliable: {dataset.price_suppressed_count}",
        "",
        "## Category Counts",
        "",
        "| Category | Used facts |",
        "|---|---:|",
    ]
    for category in FATHER_CATEGORIES:
        lines.append(f"| `{category}` | {category_counts[category]} |")
    lines.extend(
        [
            "",
            "## Exclusion Reasons",
            "",
            "| Reason | Count |",
            "|---|---:|",
        ]
    )
    if dataset.exclusion_reasons:
        for reason, count in dataset.exclusion_reasons.most_common():
            lines.append(f"| `{reason}` | {count} |")
    else:
        lines.append("| none | 0 |")
    lines.extend(
        [
            "",
            "## Top Vendors",
            "",
        ]
    )
    lines.extend(counter_table("Vendor", count_values(dataset.facts, lambda fact: fact.vendor), options.max_top_values))
    return "\n".join(lines).rstrip() + "\n"


def build_data_quality_notes(
    dataset: ReportDataset,
    facts_db: Path,
    llm_review_dbs: Sequence[Path],
    generated_at: str,
) -> str:
    lines = [
        "# Data Quality Notes",
        "",
        f"- Generated at: `{generated_at}`",
        f"- Deterministic facts DB: `{facts_db}`",
        f"- Source facts read: {dataset.source_fact_count}",
        f"- Effective facts in main report: {len(dataset.facts)}",
        f"- Excluded facts: {len(dataset.excluded)}",
        "",
        "## LLM Review Layer",
        "",
    ]
    if not llm_review_dbs:
        lines.append("No LLM review DB was provided. Only deterministic clean facts were eligible.")
    else:
        lines.append("LLM review was used as an override/enrichment layer. Deterministic facts remain the source of record.")
        lines.append("")
        lines.append("| Decision | Count |")
        lines.append("|---|---:|")
        for decision in ("keep", "fix", "discard", "needs_human"):
            lines.append(f"| `{decision}` | {dataset.review_decisions.get(decision, 0)} |")
    lines.extend(
        [
            "",
            "## Exclusion Reasons",
            "",
            "| Reason | Count |",
            "|---|---:|",
        ]
    )
    if dataset.exclusion_reasons:
        for reason, count in dataset.exclusion_reasons.most_common():
            lines.append(f"| `{reason}` | {count} |")
    else:
        lines.append("| none | 0 |")
    lines.extend(
        [
            "",
            "## Limitations",
            "",
            "- Markdown only.",
            "- No image analysis, OCR, VLM, Telegram comment sync, dashboards, OpenRouter, HTML, or PDF.",
            "- Prices are shown only when they remain attached to a concrete fact after review rules.",
            "- Excluded rows remain auditable in `source_facts_excluded.csv`.",
            "",
        ]
    )
    return "\n".join(lines)


def write_used_csv(facts: Sequence[EffectiveFact], path: Path) -> None:
    fieldnames = [
        "fact_id",
        "source_message_id",
        "category",
        "item_type",
        "vendor",
        "brand",
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
        "price_reliable",
        "source_confidence",
        "review_decision",
        "report_notes",
        "evidence_quote",
        "photo_path",
    ]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for fact in facts:
            writer.writerow({name: getattr(fact, name) for name in fieldnames})


def write_excluded_csv(excluded: Sequence[ExcludedFact], path: Path) -> None:
    fieldnames = [
        "fact_id",
        "source_message_id",
        "category",
        "item_type",
        "reason",
        "source_confidence",
        "needs_review",
        "review_decision",
        "notes",
        "evidence_quote",
    ]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for item in excluded:
            writer.writerow({name: getattr(item, name) for name in fieldnames})


def count_values(
    facts: Iterable[EffectiveFact],
    extractor: Any,
) -> Counter[str]:
    counter: Counter[str] = Counter()
    for fact in facts:
        value = extractor(fact)
        if not value:
            continue
        value = compact_whitespace(str(value))
        if value:
            counter[value] += 1
    return counter


def subsection_counter(title: str, label: str, counter: Counter[str], limit: int) -> list[str]:
    return [f"### {title}", "", *counter_table(label, counter, limit)]


def counter_table(label: str, counter: Counter[str], limit: int) -> list[str]:
    lines = [f"| {label} | Кол-во |", "|---|---:|"]
    if not counter:
        lines.append("| нет надёжных данных | 0 |")
        lines.append("")
        return lines
    for value, count in counter.most_common(limit):
        lines.append(f"| {escape_md(value)} | {count} |")
    lines.append("")
    return lines


def example_table(
    facts: list[EffectiveFact],
    *,
    limit: int,
    include_value: bool = False,
) -> list[str]:
    lines = ["### Примеры доказательств", ""]
    if not facts:
        lines.extend(
            [
                "В этой выборке нет достаточно надёжных фактов для основного раздела.",
                "",
            ]
        )
        return lines
    if include_value:
        lines.append("| Дата | message_id | Значение | Поставщик | Цена | Цитата |")
        lines.append("|---|---:|---|---|---:|---|")
        for fact in select_examples(facts, limit=limit):
            lines.append(
                f"| {escape_md(fact.source_date or '')} | {fact.source_message_id} | "
                f"{escape_md(primary_value(fact) or '')} | {escape_md(fact.vendor or fact.marketplace or '')} | "
                f"{escape_md(format_price(fact))} | {escape_md(fact.evidence_quote)} |"
            )
    else:
        lines.append("| Дата | message_id | Поставщик | Цена | Цитата |")
        lines.append("|---|---:|---|---:|---|")
        for fact in select_examples(facts, limit=limit):
            lines.append(
                f"| {escape_md(fact.source_date or '')} | {fact.source_message_id} | "
                f"{escape_md(fact.vendor or fact.marketplace or '')} | {escape_md(format_price(fact))} | "
                f"{escape_md(fact.evidence_quote)} |"
            )
    lines.append("")
    return lines


def select_examples(facts: Sequence[EffectiveFact], *, limit: int) -> list[EffectiveFact]:
    return sorted(
        facts,
        key=lambda fact: (
            1 if fact.review_decision in {"keep", "fix"} else 0,
            CONFIDENCE_SORT_VALUE.get(fact.source_confidence, 0),
            1 if fact.price_value is not None else 0,
            1 if fact.vendor else 0,
            1 if fact.model else 0,
            1 if fact.article_id else 0,
            1 if fact.photo_path else 0,
            fact.source_date or "",
            -fact.fact_id,
        ),
        reverse=True,
    )[:limit]


def primary_value(fact: EffectiveFact) -> str | None:
    if fact.category == "wall_colors":
        return fact.color_code or fact.color
    if fact.category == "kitchens":
        return fact.finish or fact.model or fact.material or fact.color
    if fact.category == "sofas":
        return fact.model or normalize_sofa_material(fact.material) or normalize_sofa_color(fact.color)
    if fact.category == "flooring":
        return normalize_flooring_material(fact.material or fact.brand or fact.finish or "")
    return fact.model or fact.finish or fact.material or fact.color or fact.article_id


def readable_item_type(item_type: str | None) -> str | None:
    if not item_type:
        return None
    labels = {
        "wall_color": "цвет стен",
        "kitchen_facades": "кухонные фасады",
        "countertop": "столешница",
        "backsplash": "фартук",
        "flooring": "напольное покрытие",
        "flooring_tile": "плитка на пол",
        "sofa": "диван",
        "chair": "стул/кресло",
        "table": "стол",
        "coffee_table": "журнальный столик",
        "bundle_purchase": "набор / bundle",
        "hanger": "вешалка",
        "mirror": "зеркало",
        "pouf": "пуф",
        "cabinet": "шкаф/тумба",
        "tv_unit": "ТВ-тумба",
        "chest": "комод",
        "shelving": "стеллаж/полки",
    }
    return labels.get(item_type, item_type.replace("_", " "))


def format_price(fact: EffectiveFact) -> str:
    if fact.price_value is None or not fact.price_reliable:
        return ""
    value = fact.price_value
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    unit = f"/{fact.price_unit}" if fact.price_unit else ""
    currency = fact.price_currency or "₽"
    return f"{value} {currency}{unit}".strip()


def normalize_vendor(value: str | None) -> str | None:
    if not value:
        return None
    text = compact_whitespace(str(value)).strip(" .,:;")
    lowered = text.casefold()
    aliases: list[tuple[str, tuple[str, ...]]] = [
        ("OZON", ("ozon", "озон")),
        ("Wildberries", ("wildberries", "wb", "вб", "вайлдберриз")),
        ("Yandex Market", ("yandex market", "яндекс маркет", "ям")),
        ("Divan.ru", ("divan.ru", "диван.ру", "диван ру", "official_divan.ru")),
        ("Mebel.in", ("mebel.in", "mebel in", "мебель ин", "мебель inn")),
        ("Лемана Про", ("лемана про", "лемана")),
        ("Леруа Мерлен", ("леруа мерлен", "леруа")),
        ("Сантехника Онлайн", ("сантехника онлайн",)),
        ("VERESK", ("veresk", "вереск")),
        ("Stoolgroup", ("stoolgroup", "stool group")),
        ("Moon", ("moon", "муун")),
        ("Alpine Floor", ("alpine floor",)),
        ("SPC", ("spc",)),
    ]
    for normalized, candidates in aliases:
        if any(candidate == lowered or candidate in lowered for candidate in candidates):
            return normalized
    return text


def normalize_wall_color_code(value: str | None) -> str | None:
    if not value:
        return None
    text = compact_whitespace(str(value)).strip(" .,:;").upper()
    text = re.sub(r"\s+", " ", text)
    g_match = re.search(r"\bG\s*(\d{3})\b", text, flags=re.IGNORECASE)
    if g_match:
        return f"G{g_match.group(1)}"
    ral_match = re.search(r"\bRAL\s*(\d{3,4})\b", text, flags=re.IGNORECASE)
    if ral_match:
        return f"RAL {ral_match.group(1)}"
    ncs_match = re.search(r"\bNCS\s*([A-Z0-9\s/-]+)", text, flags=re.IGNORECASE)
    if ncs_match:
        return "NCS " + compact_whitespace(ncs_match.group(1))
    gy_match = re.search(r"\b(\d{2})\s*([A-Z]{2})\s*(\d{2}/\d{3})\b", text)
    if gy_match:
        return f"{gy_match.group(1)} {gy_match.group(2)} {gy_match.group(3)}"
    return text


def normalize_kitchen_finish(value: str | None) -> str | None:
    if not value:
        return None
    text = compact_whitespace(str(value)).strip(" .,:;")
    text = re.sub(r"\s*\+\s*", " + ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text


def normalize_sofa_material(value: str | None) -> str | None:
    if not value:
        return None
    text = compact_whitespace(str(value)).strip(" .,:;")
    lowered = text.casefold()
    if any(token in lowered for token in ("bucle", "букле")):
        return "букле / Bucle"
    if any(token in lowered for token in ("velvet", "вельвет", "велюр")):
        return "велюр / velvet-like labels"
    if "рогож" in lowered:
        return "рогожка"
    return text


def normalize_sofa_color(value: str | None) -> str | None:
    if not value:
        return None
    text = compact_whitespace(str(value)).strip(" .,:;")
    lowered = text.casefold()
    aliases = [
        ("olive", ("olive", "олив")),
        ("light", ("light", "светл", "молоч")),
        ("terra", ("terra", "терра")),
        ("серый", ("grey", "gray", "сер")),
        ("зелёный", ("green", "зелен", "зелён", "emerald")),
        ("бежевый", ("beige", "беж", "крем")),
    ]
    for normalized, candidates in aliases:
        if any(candidate in lowered for candidate in candidates):
            return normalized
    return text


def normalize_flooring_material(value: str | None) -> str | None:
    if not value:
        return None
    text = compact_whitespace(str(value)).strip(" .,:;")
    lowered = text.casefold()
    if "alpine floor" in lowered:
        return "Alpine Floor"
    if "spc" in lowered or "кварц" in lowered:
        return "SPC / кварцвинил"
    if "плитк" in lowered or "керамогранит" in lowered:
        return "плитка / керамогранит"
    if "ламинат" in lowered:
        return "ламинат"
    if "паркет" in lowered or "инженер" in lowered:
        return "паркет / инженерная доска"
    return text


def escape_md(value: Any) -> str:
    if value is None:
        return ""
    return compact_whitespace(str(value)).replace("\\", "\\\\").replace("|", "\\|")


def short_quote(value: str, limit: int) -> str:
    value = compact_whitespace(value)
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = compact_whitespace(str(value)).strip()
    return text or None


def _load_json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _replace_photo_path(fact: SourceFact, photo_path: str | None) -> SourceFact:
    if not photo_path:
        return fact
    data = fact.__dict__.copy()
    data["photo_path"] = photo_path
    return SourceFact(**data)


def _replace_effective(fact: EffectiveFact, **overrides: Any) -> EffectiveFact:
    data = fact.__dict__.copy()
    data.update(overrides)
    return EffectiveFact(**data)


def _exclude(
    fact: SourceFact,
    reason: str,
    review: LLMReview | None = None,
) -> ExcludedFact:
    return ExcludedFact(
        fact_id=fact.fact_id,
        source_message_id=fact.source_message_id,
        category=fact.category,
        item_type=fact.item_type,
        evidence_quote=fact.evidence_quote,
        reason=reason,
        source_confidence=fact.source_confidence,
        needs_review=fact.needs_review,
        review_decision=review.decision if review else None,
        notes=_join_notes(fact.notes, review.rationale_short if review else None),
    )


def _review_note(review: LLMReview) -> str | None:
    parts = [f"LLM: {review.decision}"]
    if review.rationale_short:
        parts.append(review.rationale_short)
    return "; ".join(parts)


def _join_notes(*values: str | None) -> str | None:
    cleaned = [compact_whitespace(value) for value in values if value and compact_whitespace(value)]
    return "; ".join(cleaned) if cleaned else None


def _rationale_has_hard_conflict(rationale: str | None) -> bool:
    if not rationale:
        return False
    text = rationale.casefold()
    conflict_tokens = (
        "mismatch",
        "wrong category",
        "not a target",
        "false positive",
        "context only",
        "ambiguous price",
        "ambiguous category",
        "несоответ",
        "неоднознач",
        "ложнополож",
        "только контекст",
        "не целев",
    )
    return any(token in text for token in conflict_tokens)


def _looks_like_general_essay(fact: SourceFact) -> bool:
    if len(fact.evidence_quote) < 350:
        return False
    concrete_fields = [
        fact.vendor_raw,
        fact.vendor_normalized,
        fact.brand_raw,
        fact.brand_normalized,
        fact.model,
        fact.material,
        fact.finish,
        fact.color,
        fact.color_code,
        fact.article_id,
        fact.marketplace,
        fact.price_value,
    ]
    if any(value not in (None, "") for value in concrete_fields):
        return False
    evidence = fact.evidence_quote.casefold()
    concrete_tokens = (
        "арт",
        "руб",
        "₽",
        "ozon",
        "wb",
        "divan",
        "mebel",
        "g482",
        "ral",
        "ncs",
    )
    return not any(token in evidence for token in concrete_tokens)


def _field_supported_by_evidence(value: str, evidence: str) -> bool:
    if not value:
        return False
    normalized_value = _normalize_for_evidence(value)
    normalized_evidence = _normalize_for_evidence(evidence)
    if normalized_value and normalized_value in normalized_evidence:
        return True
    if normalize_wall_color_code(value) and normalize_wall_color_code(value) in {
        normalize_wall_color_code(match.group(0))
        for match in re.finditer(r"\bG\s*\d{3}\b|\b\d{2}\s*[A-Z]{2}\s*\d{2}/\d{3}\b", evidence, flags=re.IGNORECASE)
    }:
        return True
    normalized_vendor = normalize_vendor(value)
    return bool(normalized_vendor and _vendor_supported_by_evidence(normalized_vendor, evidence))


def _vendor_supported_by_evidence(vendor: str, evidence: str) -> bool:
    evidence_norm = _normalize_for_evidence(evidence)
    aliases = {
        "OZON": ("ozon", "озон"),
        "Wildberries": ("wildberries", "wb", "вб"),
        "Yandex Market": ("yandex market", "яндекс маркет", "ям"),
        "Divan.ru": ("divan ru", "divan.ru", "диван ру", "диван.ру"),
        "Mebel.in": ("mebel in", "mebel.in", "мебель ин"),
        "Лемана Про": ("лемана", "лемана про"),
        "Леруа Мерлен": ("леруа", "леруа мерлен"),
        "Сантехника Онлайн": ("сантехника онлайн",),
        "VERESK": ("veresk", "вереск"),
        "Stoolgroup": ("stoolgroup", "stool group"),
        "Moon": ("moon", "муун"),
        "Alpine Floor": ("alpine floor",),
    }
    return any(alias in evidence_norm for alias in aliases.get(vendor, (vendor.casefold(),)))


def _normalize_for_evidence(value: str) -> str:
    value = str(value).casefold().replace("ё", "е")
    value = re.sub(r"[^\wа-яa-z0-9/]+", " ", value, flags=re.IGNORECASE)
    return compact_whitespace(value)


def _number_supported_by_evidence(value: Any, evidence: str) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    if number <= 0:
        return False
    normalized_number = str(int(number)) if number.is_integer() else str(number).replace(".", "")
    evidence_digits = re.sub(r"\D", "", evidence)
    return normalized_number in evidence_digits or str(int(number)) in evidence_digits


def _price_is_plausible(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return 0 < number < 20_000_000


def _spc_vendor_repairs(vendor: str | None, evidence: str) -> tuple[str | None, str | None]:
    if not vendor:
        return None, None
    normalized_vendor = normalize_vendor(vendor)
    if normalized_vendor != "SPC":
        return None, None
    if re.search(r"alpine\s*floor", evidence, flags=re.IGNORECASE):
        return "Alpine Floor", "SPC"
    return None, "SPC"


def _fix_is_coherent(
    source: SourceFact,
    review: LLMReview,
    effective: EffectiveFact,
) -> bool:
    corrected = review.corrected or {}
    corrected_category = _clean(corrected.get("category"))
    if corrected_category and corrected_category not in FATHER_CATEGORIES:
        return False
    corrected_price = corrected.get("price_value")
    if corrected_price is not None and review.price_correct is not False:
        if not (_price_is_plausible(corrected_price) and _number_supported_by_evidence(corrected_price, source.evidence_quote)):
            return False
    unsupported_text_fields = []
    for name in ("vendor_normalized", "model", "material", "finish", "color", "color_code", "article_id"):
        value = _clean(corrected.get(name))
        if not value:
            continue
        if name == "vendor_normalized" and normalize_vendor(value) == "SPC":
            continue
        if not _field_supported_by_evidence(value, source.evidence_quote):
            unsupported_text_fields.append(name)
    if unsupported_text_fields and not any(
        getattr(effective, _corrected_name_to_effective_name(name)) for name in unsupported_text_fields
    ):
        return False
    return True


def _corrected_name_to_effective_name(name: str) -> str:
    if name == "vendor_normalized":
        return "vendor"
    return name


def _kitchen_has_wood_plus_neutral(facts: Sequence[EffectiveFact]) -> bool:
    hits = 0
    neutral_tokens = (
        "капуч",
        "эбони",
        "сантьяго",
        "тальк",
        "greige",
        "гринвуд",
        "мат",
        "сер",
        "беж",
        "молоч",
    )
    for fact in facts:
        text = " ".join(
            value for value in [fact.finish, fact.material, fact.color, fact.evidence_quote] if value
        ).casefold()
        if "дуб" in text and any(token in text for token in neutral_tokens):
            hits += 1
    return hits >= 2
