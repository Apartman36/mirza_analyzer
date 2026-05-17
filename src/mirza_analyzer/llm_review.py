from __future__ import annotations

import csv
import hashlib
import json
import random
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .extraction_patterns import STRICT_CATEGORIES
from .llm_providers import (
    LLMProvider,
    LLMProviderError,
    MockProvider,
    ProviderResponse,
    build_provider,
)
from .llm_schemas import (
    LLMReviewResult,
    LLMReviewValidationError,
    empty_corrected,
    empty_normalized_terms,
    validate_review_payload,
)
from .utils import json_dumps, truncate, utc_now_iso


SOURCE_CHOICES = ("clean", "needs_review", "mixed", "category")


SYSTEM_PROMPT = """\
Ты строгий русскоязычный ревьюер данных по интерьерному ремонту.
Ты проверяешь детерминированно извлечённые факты против предоставленных доказательств.
Ты НЕ изобретаешь данные. Ты используешь только переданное тебе доказательство (evidence) и исходный пост (source_post_excerpt).
Если поле отсутствует в доказательстве — оставь его null, не угадывай.

Правила:
- Если запись описывает оптовую/групповую покупку (несколько товаров одной ценой) — установи is_bundle=true и decision="fix" или "needs_human".
- Если товар упомянут только как контекст (например, «бра НАД диваном» в записи о диване) — is_context_false_positive=true и decision="discard" или "needs_human".
- Если цена относится к нескольким товарам — price_correct=false.
- Если строка — это прямой код цвета стен с поддержкой "Цвет стен ..." — обычно decision="keep".
- "Бра над диваном" — это не диван (sofas). Не оставляй как sofas.
- "Настольная лампа" — это не стол (tables).
- "Люстра в гостиной" — это не living_room_furniture.
- Категории строго ограничены: flooring, wall_colors, kitchens, chairs, tables, sofas, hallway, living_room_furniture. Новых категорий не предлагай.
- Возвращай ТОЛЬКО валидный JSON, без префиксов, без пояснений, без markdown.
"""


USER_PROMPT_TEMPLATE = """\
Ты получаешь один детерминированно извлечённый факт и его доказательство.
Оцени корректность и верни строгий JSON по схеме (см. system).

Допустимые категории:
- flooring — полы, ламинат, кварцвинил, керамогранит, паркет
- wall_colors — цвет стен, краска, шифры цветов (Tikkurila, Dulux, RAL и т.п.)
- kitchens — кухонные фасады, столешница, фартук, кухонные гарнитуры
- chairs — стулья, кресла
- tables — столы (обеденные, журнальные, рабочие), столики
- sofas — диваны, софы, кровати-трансформеры
- hallway — мебель прихожей: обувница, консоль, вешалка, банкетка, пуф, зеркало
- living_room_furniture — тумбы, комоды, шкафы, стеллажи, полки в гостиной

Примеры false-positive, которые надо отсеивать:
- "Бра над диваном" → НЕ sofas (decision discard, is_context_false_positive=true)
- "Настольная лампа" → НЕ tables
- "Люстра в гостиной" → НЕ living_room_furniture
- "Кухня, диван, стол — комплект 270 000₽" → bundle (is_bundle=true)

Факт (детерминированное извлечение):
```json
{fact_json}
```

Доказательство (evidence_quote из строки):
{evidence_quote}

Исходный пост (фрагмент канонического текста, source_message_id={source_message_id}):
{source_post_excerpt}

Верни ТОЛЬКО один JSON-объект по следующей схеме:
{{
  "decision": "keep" | "fix" | "discard" | "needs_human",
  "category_correct": true | false,
  "item_type_correct": true | false,
  "price_correct": true | false | null,
  "is_bundle": true | false,
  "is_context_false_positive": true | false,
  "is_non_target_room": true | false,
  "corrected": {{
    "category": string | null,
    "item_type": string | null,
    "vendor_normalized": string | null,
    "model": string | null,
    "material": string | null,
    "finish": string | null,
    "color": string | null,
    "color_code": string | null,
    "article_id": string | null,
    "price_value": number | null,
    "price_unit": string | null,
    "room_context": string | null
  }},
  "normalized_terms": {{
    "vendor": string | null,
    "facade_materials": [string],
    "sofa_fabric": string | null,
    "sofa_color": string | null,
    "wall_color_code": string | null,
    "flooring_brand_or_collection": string | null
  }},
  "rationale_short": string,
  "confidence": "high" | "medium" | "low"
}}
"""


REPAIR_INSTRUCTION = """\
Твой предыдущий ответ не был валидным JSON или не соответствовал схеме.
Ошибка валидации: {error}

Верни ИСКЛЮЧИТЕЛЬНО один валидный JSON-объект по той же схеме. Без markdown, без преамбулы, без пояснений вне JSON.
"""


REVIEW_FACT_COLUMNS = (
    "id",
    "source_message_id",
    "date",
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
    "room_context",
    "evidence_quote",
    "confidence",
    "needs_review",
    "notes",
)


CSV_FIELDNAMES = [
    "id",
    "fact_id",
    "source_message_id",
    "provider",
    "model",
    "original_category",
    "original_item_type",
    "original_needs_review",
    "decision",
    "category_correct",
    "item_type_correct",
    "price_correct",
    "is_bundle",
    "is_context_false_positive",
    "is_non_target_room",
    "confidence",
    "rationale_short",
    "corrected_category",
    "corrected_item_type",
    "corrected_vendor_normalized",
    "corrected_price_value",
    "normalized_vendor",
    "normalized_wall_color_code",
    "normalized_sofa_fabric",
    "normalized_sofa_color",
    "normalized_flooring_brand_or_collection",
    "normalized_facade_materials",
    "error",
    "created_at",
    "input_hash",
    "prompt_hash",
]


@dataclass
class FactRow:
    fact_id: int
    source_message_id: int
    date: str | None
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
    price_value: float | int | None
    price_currency: str | None
    price_unit: str | None
    room_context: str | None
    evidence_quote: str
    confidence: str
    needs_review: int
    notes: str | None

    def to_fact_dict(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "source_message_id": self.source_message_id,
            "date": self.date,
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
            "room_context": self.room_context,
            "evidence_quote": self.evidence_quote,
            "confidence": self.confidence,
            "needs_review": int(self.needs_review),
            "notes": self.notes,
        }


@dataclass
class ReviewRecord:
    fact: FactRow
    source_post_excerpt: str
    result: LLMReviewResult
    provider: str
    model: str
    input_hash: str
    prompt_hash: str
    raw_response: str
    retry_count: int
    error: str | None
    created_at: str

    def to_jsonl_dict(self) -> dict[str, Any]:
        return {
            "fact": self.fact.to_fact_dict(),
            "source_post_excerpt": self.source_post_excerpt,
            "review": self.result.to_json_dict(),
            "provider": self.provider,
            "model": self.model,
            "input_hash": self.input_hash,
            "prompt_hash": self.prompt_hash,
            "raw_response": self.raw_response,
            "retry_count": self.retry_count,
            "error": self.error,
            "created_at": self.created_at,
        }

    def to_csv_dict(self, *, row_id: int) -> dict[str, Any]:
        corrected = self.result.corrected or {}
        terms = self.result.normalized_terms or {}
        facade_materials = terms.get("facade_materials") or []
        return {
            "id": row_id,
            "fact_id": self.fact.fact_id,
            "source_message_id": self.fact.source_message_id,
            "provider": self.provider,
            "model": self.model,
            "original_category": self.fact.category,
            "original_item_type": self.fact.item_type,
            "original_needs_review": int(self.fact.needs_review),
            "decision": self.result.decision,
            "category_correct": int(self.result.category_correct),
            "item_type_correct": int(self.result.item_type_correct),
            "price_correct": (
                "" if self.result.price_correct is None else int(self.result.price_correct)
            ),
            "is_bundle": int(self.result.is_bundle),
            "is_context_false_positive": int(self.result.is_context_false_positive),
            "is_non_target_room": int(self.result.is_non_target_room),
            "confidence": self.result.confidence,
            "rationale_short": self.result.rationale_short,
            "corrected_category": corrected.get("category") or "",
            "corrected_item_type": corrected.get("item_type") or "",
            "corrected_vendor_normalized": corrected.get("vendor_normalized") or "",
            "corrected_price_value": (
                "" if corrected.get("price_value") is None else corrected.get("price_value")
            ),
            "normalized_vendor": terms.get("vendor") or "",
            "normalized_wall_color_code": terms.get("wall_color_code") or "",
            "normalized_sofa_fabric": terms.get("sofa_fabric") or "",
            "normalized_sofa_color": terms.get("sofa_color") or "",
            "normalized_flooring_brand_or_collection": terms.get("flooring_brand_or_collection") or "",
            "normalized_facade_materials": ", ".join(facade_materials),
            "error": self.error or "",
            "created_at": self.created_at,
            "input_hash": self.input_hash,
            "prompt_hash": self.prompt_hash,
        }


@dataclass
class ReviewRunResult:
    out_dir: Path
    facts_db: Path
    canonical_db: Path | None
    provider: str
    model: str
    base_url: str
    source: str
    category: str | None
    sample_size: int
    seed: int
    settings: dict[str, Any]
    planned_rows: list[FactRow]
    records: list[ReviewRecord]
    skipped: list[FactRow] = field(default_factory=list)
    invalid_count: int = 0
    retry_count: int = 0
    dry_run: bool = False
    created_at: str = ""
    output_files: list[Path] = field(default_factory=list)


def run_llm_review(
    *,
    facts_db: Path,
    out_dir: Path,
    provider: str,
    base_url: str = "http://127.0.0.1:1234/v1",
    model: str = "local-model",
    source: str = "mixed",
    category: str | None = None,
    sample_size: int = 100,
    seed: int = 42,
    max_evidence_chars: int = 2500,
    temperature: float = 0.0,
    dry_run: bool = False,
    limit: int | None = None,
    resume: bool = False,
    timeout_seconds: float = 120.0,
    strict_json: bool = True,
    canonical_db: Path | None = None,
    provider_instance: LLMProvider | None = None,
    mock_handler: Callable[[str, str, str], str] | None = None,
) -> ReviewRunResult:
    if source not in SOURCE_CHOICES:
        raise ValueError(f"--source must be one of {SOURCE_CHOICES}, got {source!r}")
    if source == "category" and not category:
        raise ValueError("`--source category` requires --category to be set")

    out_dir.mkdir(parents=True, exist_ok=True)
    settings: dict[str, Any] = {
        "facts_db": str(facts_db),
        "out_dir": str(out_dir),
        "provider": provider,
        "base_url": base_url,
        "model": model,
        "source": source,
        "category": category,
        "sample_size": sample_size,
        "seed": seed,
        "max_evidence_chars": max_evidence_chars,
        "temperature": temperature,
        "dry_run": dry_run,
        "limit": limit,
        "resume": resume,
        "timeout_seconds": timeout_seconds,
        "strict_json": strict_json,
        "canonical_db": str(canonical_db) if canonical_db else None,
    }

    write_prompt_templates(out_dir)

    facts = load_facts(facts_db)
    planned = select_sample(
        facts,
        source=source,
        category=category,
        sample_size=sample_size,
        seed=seed,
    )
    if limit is not None:
        planned = planned[:limit]

    created_at = utc_now_iso()
    result = ReviewRunResult(
        out_dir=out_dir,
        facts_db=facts_db,
        canonical_db=canonical_db,
        provider=provider,
        model=model or "local-model",
        base_url=base_url,
        source=source,
        category=category,
        sample_size=sample_size,
        seed=seed,
        settings=settings,
        planned_rows=planned,
        records=[],
        dry_run=dry_run,
        created_at=created_at,
    )

    if dry_run:
        write_planned_csv(planned, out_dir / "planned_rows.csv")
        result.output_files.append(out_dir / "planned_rows.csv")
        return result

    if provider_instance is None:
        provider_instance = build_provider(
            provider,
            base_url=base_url,
            mock_handler=mock_handler,
        )

    probe = getattr(provider_instance, "probe", None)
    if callable(probe):
        probe(timeout_seconds=min(timeout_seconds, 10.0))

    existing_hashes: set[str] = set()
    sqlite_path = out_dir / "llm_review.sqlite"
    if resume and sqlite_path.exists():
        existing_hashes = load_existing_input_hashes(
            sqlite_path,
            provider=provider,
            model=model or "local-model",
        )

    excerpts = load_source_excerpts(
        canonical_db,
        [fact.source_message_id for fact in planned],
        max_chars=max_evidence_chars,
    )

    for fact in planned:
        source_excerpt = excerpts.get(fact.source_message_id) or fact.evidence_quote
        fact_dict = fact.to_fact_dict()
        input_hash = compute_input_hash(fact_dict, source_excerpt)
        if input_hash in existing_hashes:
            result.skipped.append(fact)
            continue

        user_prompt = build_user_prompt(
            fact_dict=fact_dict,
            evidence_quote=fact.evidence_quote,
            source_post_excerpt=source_excerpt,
            source_message_id=fact.source_message_id,
        )
        prompt_hash = hashlib.sha256(
            (SYSTEM_PROMPT + "\n---\n" + user_prompt).encode("utf-8")
        ).hexdigest()

        record = invoke_provider_with_retry(
            provider_instance=provider_instance,
            fact=fact,
            source_post_excerpt=source_excerpt,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            model=model or "local-model",
            temperature=temperature,
            timeout_seconds=timeout_seconds,
            input_hash=input_hash,
            prompt_hash=prompt_hash,
            strict_json=strict_json,
        )
        result.records.append(record)
        if record.retry_count:
            result.retry_count += record.retry_count
        if record.error or record.result.decision == "needs_human":
            if record.error:
                result.invalid_count += 1

    result.output_files.extend(
        write_outputs(result, sqlite_path=sqlite_path)
    )
    return result


def invoke_provider_with_retry(
    *,
    provider_instance: LLMProvider,
    fact: FactRow,
    source_post_excerpt: str,
    system_prompt: str,
    user_prompt: str,
    model: str,
    temperature: float,
    timeout_seconds: float,
    input_hash: str,
    prompt_hash: str,
    strict_json: bool,
) -> ReviewRecord:
    created_at = utc_now_iso()
    raw_response = ""
    error_text: str | None = None
    retry_count = 0

    try:
        response = provider_instance.chat(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model,
            temperature=temperature,
            timeout_seconds=timeout_seconds,
        )
    except LLMProviderError as exc:
        error_text = f"provider_error: {exc}"
        return _needs_human_record(
            fact=fact,
            source_post_excerpt=source_post_excerpt,
            input_hash=input_hash,
            prompt_hash=prompt_hash,
            provider=provider_instance.name,
            model=model,
            raw_response="",
            retry_count=0,
            error=error_text,
            created_at=created_at,
        )

    raw_response = response.text
    parse_error = None
    try:
        payload = parse_strict_json(raw_response, strict_json=strict_json)
        result = validate_review_payload(payload)
        return ReviewRecord(
            fact=fact,
            source_post_excerpt=source_post_excerpt,
            result=result,
            provider=provider_instance.name,
            model=response.model or model,
            input_hash=input_hash,
            prompt_hash=prompt_hash,
            raw_response=raw_response,
            retry_count=0,
            error=None,
            created_at=created_at,
        )
    except (json.JSONDecodeError, LLMReviewValidationError) as exc:
        parse_error = str(exc)

    retry_count = 1
    repair_prompt = (
        user_prompt
        + "\n\n"
        + REPAIR_INSTRUCTION.format(error=truncate(parse_error or "invalid JSON", 200))
    )
    try:
        repair_response = provider_instance.chat(
            system_prompt=system_prompt,
            user_prompt=repair_prompt,
            model=model,
            temperature=temperature,
            timeout_seconds=timeout_seconds,
        )
    except LLMProviderError as exc:
        return _needs_human_record(
            fact=fact,
            source_post_excerpt=source_post_excerpt,
            input_hash=input_hash,
            prompt_hash=prompt_hash,
            provider=provider_instance.name,
            model=model,
            raw_response=raw_response,
            retry_count=retry_count,
            error=f"repair_provider_error: {exc}",
            created_at=created_at,
        )

    combined_raw = raw_response + "\n---REPAIR---\n" + repair_response.text
    try:
        payload = parse_strict_json(repair_response.text, strict_json=strict_json)
        result = validate_review_payload(payload)
        return ReviewRecord(
            fact=fact,
            source_post_excerpt=source_post_excerpt,
            result=result,
            provider=provider_instance.name,
            model=repair_response.model or model,
            input_hash=input_hash,
            prompt_hash=prompt_hash,
            raw_response=combined_raw,
            retry_count=retry_count,
            error=f"recovered_after_retry: {truncate(parse_error or '', 200)}",
            created_at=created_at,
        )
    except (json.JSONDecodeError, LLMReviewValidationError) as exc:
        return _needs_human_record(
            fact=fact,
            source_post_excerpt=source_post_excerpt,
            input_hash=input_hash,
            prompt_hash=prompt_hash,
            provider=provider_instance.name,
            model=repair_response.model or model,
            raw_response=combined_raw,
            retry_count=retry_count,
            error=f"invalid_json_after_retry: {exc}",
            created_at=created_at,
        )


def _needs_human_record(
    *,
    fact: FactRow,
    source_post_excerpt: str,
    input_hash: str,
    prompt_hash: str,
    provider: str,
    model: str,
    raw_response: str,
    retry_count: int,
    error: str | None,
    created_at: str,
) -> ReviewRecord:
    result = LLMReviewResult(
        decision="needs_human",
        category_correct=False,
        item_type_correct=False,
        price_correct=None,
        is_bundle=False,
        is_context_false_positive=False,
        is_non_target_room=False,
        corrected=empty_corrected(),
        normalized_terms=empty_normalized_terms(),
        rationale_short=truncate(error or "invalid model response", 240),
        confidence="low",
    )
    return ReviewRecord(
        fact=fact,
        source_post_excerpt=source_post_excerpt,
        result=result,
        provider=provider,
        model=model,
        input_hash=input_hash,
        prompt_hash=prompt_hash,
        raw_response=raw_response,
        retry_count=retry_count,
        error=error,
        created_at=created_at,
    )


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_strict_json(text: str, *, strict_json: bool) -> Any:
    text = (text or "").strip()
    if not text:
        raise json.JSONDecodeError("empty response", text or "", 0)
    if strict_json:
        return json.loads(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = _JSON_BLOCK_RE.search(text)
        if match is None:
            raise
        return json.loads(match.group(0))


def load_facts(facts_db: Path) -> list[FactRow]:
    columns = ", ".join(REVIEW_FACT_COLUMNS)
    if not facts_db.exists():
        raise FileNotFoundError(f"facts database not found: {facts_db}")
    sqlite_uri = f"{facts_db.resolve().as_uri()}?mode=ro"
    rows: list[FactRow] = []
    with sqlite3.connect(sqlite_uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        for row in conn.execute(f"SELECT {columns} FROM extracted_facts ORDER BY id"):
            rows.append(_row_to_fact(row))
    return rows


def _row_to_fact(row: sqlite3.Row) -> FactRow:
    data = {key: row[key] for key in row.keys()}
    return FactRow(
        fact_id=int(data["id"]),
        source_message_id=int(data["source_message_id"]),
        date=data.get("date"),
        category=str(data["category"]),
        item_type=str(data["item_type"]),
        item_name=data.get("item_name"),
        vendor_raw=data.get("vendor_raw"),
        vendor_normalized=data.get("vendor_normalized"),
        brand_raw=data.get("brand_raw"),
        brand_normalized=data.get("brand_normalized"),
        model=data.get("model"),
        material=data.get("material"),
        finish=data.get("finish"),
        color=data.get("color"),
        color_code=data.get("color_code"),
        article_id=data.get("article_id"),
        marketplace=data.get("marketplace"),
        price_value=data.get("price_value"),
        price_currency=data.get("price_currency"),
        price_unit=data.get("price_unit"),
        room_context=data.get("room_context"),
        evidence_quote=str(data.get("evidence_quote") or ""),
        confidence=str(data.get("confidence") or "low"),
        needs_review=int(data.get("needs_review") or 0),
        notes=data.get("notes"),
    )


CATEGORY_TARGETS: dict[str, int] = {
    "wall_colors": 8,
    "kitchens": 20,
    "sofas": 18,
    "tables": 14,
    "chairs": 10,
    "hallway": 10,
    "living_room_furniture": 14,
    "flooring": 12,
}


def select_sample(
    facts: list[FactRow],
    *,
    source: str,
    category: str | None,
    sample_size: int,
    seed: int,
) -> list[FactRow]:
    rng = random.Random(seed)
    if source == "category":
        pool = [fact for fact in facts if fact.category == category]
        return _deterministic_sample(pool, sample_size, rng)
    if source == "clean":
        pool = [fact for fact in facts if not fact.needs_review]
    elif source == "needs_review":
        pool = [fact for fact in facts if fact.needs_review]
    elif source == "mixed":
        return _select_mixed_sample(facts, sample_size, rng)
    else:
        raise ValueError(f"Unsupported source {source!r}")

    if category:
        pool = [fact for fact in pool if fact.category == category]
    if source == "needs_review":
        return _stratified_by_review_signal(pool, sample_size, rng)
    return _stratified_by_category(pool, sample_size, rng)


def _deterministic_sample(
    pool: list[FactRow], sample_size: int, rng: random.Random
) -> list[FactRow]:
    if sample_size <= 0 or not pool:
        return []
    ordered = sorted(pool, key=lambda fact: fact.fact_id)
    if sample_size >= len(ordered):
        return ordered
    indexes = rng.sample(range(len(ordered)), sample_size)
    indexes.sort()
    return [ordered[i] for i in indexes]


def _stratified_by_category(
    pool: list[FactRow], sample_size: int, rng: random.Random
) -> list[FactRow]:
    if sample_size <= 0 or not pool:
        return []
    buckets: dict[str, list[FactRow]] = defaultdict(list)
    for fact in pool:
        buckets[fact.category].append(fact)
    return _take_stratified(buckets, sample_size, rng)


def _stratified_by_review_signal(
    pool: list[FactRow], sample_size: int, rng: random.Random
) -> list[FactRow]:
    if sample_size <= 0 or not pool:
        return []
    buckets: dict[str, list[FactRow]] = defaultdict(list)
    for fact in pool:
        signal = _review_signal(fact.notes)
        key = f"{fact.category}|{signal}"
        buckets[key].append(fact)
    return _take_stratified(buckets, sample_size, rng)


def _review_signal(notes: str | None) -> str:
    text = (notes or "").lower()
    if "bundle" in text:
        return "bundle"
    if "non-target" in text or "non target" in text:
        return "non_target_room"
    if "suspicious" in text:
        return "suspicious_descriptor"
    if "lighting" in text or "context" in text:
        return "context"
    if "several unrelated" in text:
        return "multiple_items"
    return "other"


def _take_stratified(
    buckets: dict[str, list[FactRow]],
    sample_size: int,
    rng: random.Random,
) -> list[FactRow]:
    if not buckets:
        return []
    keys = sorted(buckets.keys())
    sampled: list[FactRow] = []
    per_bucket = max(1, sample_size // len(keys))
    for key in keys:
        bucket = sorted(buckets[key], key=lambda fact: fact.fact_id)
        take = min(per_bucket, len(bucket))
        if take == len(bucket):
            sampled.extend(bucket)
        else:
            indexes = rng.sample(range(len(bucket)), take)
            indexes.sort()
            sampled.extend(bucket[i] for i in indexes)
    if len(sampled) >= sample_size:
        return _trim_sample(sampled, sample_size, rng)

    remaining = sample_size - len(sampled)
    leftover: list[FactRow] = []
    selected_ids = {fact.fact_id for fact in sampled}
    for key in keys:
        for fact in sorted(buckets[key], key=lambda f: f.fact_id):
            if fact.fact_id not in selected_ids:
                leftover.append(fact)
    if remaining > 0 and leftover:
        if remaining >= len(leftover):
            sampled.extend(leftover)
        else:
            indexes = rng.sample(range(len(leftover)), remaining)
            indexes.sort()
            sampled.extend(leftover[i] for i in indexes)
    return _trim_sample(sampled, sample_size, rng)


def _trim_sample(
    sampled: list[FactRow], sample_size: int, rng: random.Random
) -> list[FactRow]:
    if len(sampled) <= sample_size:
        return sorted(sampled, key=lambda fact: fact.fact_id)
    ordered = sorted(sampled, key=lambda fact: fact.fact_id)
    indexes = rng.sample(range(len(ordered)), sample_size)
    indexes.sort()
    return [ordered[i] for i in indexes]


def _select_mixed_sample(
    facts: list[FactRow], sample_size: int, rng: random.Random
) -> list[FactRow]:
    if sample_size <= 0 or not facts:
        return []
    clean_pool = [fact for fact in facts if not fact.needs_review]
    review_pool = [fact for fact in facts if fact.needs_review]

    targets_total = sum(CATEGORY_TARGETS.values())
    if targets_total <= 0:
        return _stratified_by_category(facts, sample_size, rng)

    scale = sample_size / targets_total
    selected: list[FactRow] = []
    selected_ids: set[int] = set()

    def take_from(pool: list[FactRow], count: int) -> None:
        if count <= 0 or not pool:
            return
        ordered = sorted(pool, key=lambda fact: fact.fact_id)
        if count >= len(ordered):
            chosen = ordered
        else:
            indexes = rng.sample(range(len(ordered)), count)
            indexes.sort()
            chosen = [ordered[i] for i in indexes]
        for fact in chosen:
            if fact.fact_id in selected_ids:
                continue
            selected.append(fact)
            selected_ids.add(fact.fact_id)

    for category in STRICT_CATEGORIES:
        target = max(1, int(round(CATEGORY_TARGETS.get(category, 4) * scale))) if scale > 0 else 1
        review_target = max(1, target // 2)
        clean_target = max(1, target - review_target)
        take_from(
            [fact for fact in review_pool if fact.category == category],
            review_target,
        )
        take_from(
            [fact for fact in clean_pool if fact.category == category],
            clean_target,
        )

    if len(selected) > sample_size:
        ordered = sorted(selected, key=lambda fact: fact.fact_id)
        indexes = rng.sample(range(len(ordered)), sample_size)
        indexes.sort()
        return [ordered[i] for i in indexes]

    if len(selected) < sample_size:
        remaining = sample_size - len(selected)
        leftovers = [
            fact for fact in sorted(facts, key=lambda f: f.fact_id)
            if fact.fact_id not in selected_ids
        ]
        if leftovers:
            if remaining >= len(leftovers):
                selected.extend(leftovers)
            else:
                indexes = rng.sample(range(len(leftovers)), remaining)
                indexes.sort()
                selected.extend(leftovers[i] for i in indexes)

    return sorted(selected, key=lambda fact: fact.fact_id)


def build_user_prompt(
    *,
    fact_dict: dict[str, Any],
    evidence_quote: str,
    source_post_excerpt: str,
    source_message_id: int,
) -> str:
    fact_json = json_dumps(fact_dict)
    return USER_PROMPT_TEMPLATE.format(
        fact_json=fact_json,
        evidence_quote=evidence_quote or "(no evidence quote)",
        source_post_excerpt=source_post_excerpt or "(no source excerpt available)",
        source_message_id=source_message_id,
    )


def compute_input_hash(fact_dict: dict[str, Any], source_post_excerpt: str) -> str:
    payload = {
        "fact": fact_dict,
        "source_post_excerpt": source_post_excerpt,
    }
    return hashlib.sha256(json_dumps(payload).encode("utf-8")).hexdigest()


def load_source_excerpts(
    canonical_db: Path | None,
    message_ids: Sequence[int],
    *,
    max_chars: int,
) -> dict[int, str]:
    if not canonical_db or not message_ids:
        return {}
    path = Path(canonical_db)
    if not path.exists():
        return {}
    unique_ids = sorted({int(mid) for mid in message_ids})
    excerpts: dict[int, str] = {}
    sqlite_uri = f"{path.resolve().as_uri()}?mode=ro"
    try:
        with sqlite3.connect(sqlite_uri, uri=True) as conn:
            conn.row_factory = sqlite3.Row
            placeholders = ", ".join("?" for _ in unique_ids)
            cursor = conn.execute(
                f"""
                SELECT telegram_message_id, text_plain
                FROM canonical_messages
                WHERE telegram_message_id IN ({placeholders})
                """,
                tuple(unique_ids),
            )
            for row in cursor:
                text = row["text_plain"] or ""
                excerpts[int(row["telegram_message_id"])] = truncate(text, max_chars)
    except sqlite3.Error:
        return {}
    return excerpts


def write_prompt_templates(out_dir: Path) -> None:
    prompts_dir = out_dir / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    (prompts_dir / "system_prompt.txt").write_text(SYSTEM_PROMPT, encoding="utf-8")
    (prompts_dir / "user_prompt_template.txt").write_text(USER_PROMPT_TEMPLATE, encoding="utf-8")


def write_planned_csv(planned: list[FactRow], path: Path) -> None:
    fieldnames = [
        "fact_id",
        "source_message_id",
        "category",
        "item_type",
        "needs_review",
        "evidence_quote",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for fact in planned:
            writer.writerow(
                {
                    "fact_id": fact.fact_id,
                    "source_message_id": fact.source_message_id,
                    "category": fact.category,
                    "item_type": fact.item_type,
                    "needs_review": int(fact.needs_review),
                    "evidence_quote": fact.evidence_quote,
                }
            )


def write_outputs(result: ReviewRunResult, *, sqlite_path: Path) -> list[Path]:
    out_dir = result.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    output_files: list[Path] = []

    jsonl_path = out_dir / "llm_review_results.jsonl"
    write_jsonl(result.records, jsonl_path)
    output_files.append(jsonl_path)

    csv_path = out_dir / "llm_review_results.csv"
    write_csv(result.records, csv_path)
    output_files.append(csv_path)

    write_sqlite(result, sqlite_path)
    output_files.append(sqlite_path)

    summary_path = out_dir / "summary.md"
    summary_path.write_text(build_summary_markdown(result), encoding="utf-8")
    output_files.append(summary_path)

    output_files.extend(write_by_decision(result.records, out_dir / "by_decision"))
    output_files.extend(write_by_category(result.records, out_dir / "by_category"))

    return output_files


def write_jsonl(records: list[ReviewRecord], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for record in records:
            fh.write(json_dumps(record.to_jsonl_dict()))
            fh.write("\n")


def write_csv(records: list[ReviewRecord], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        for index, record in enumerate(records, start=1):
            writer.writerow(record.to_csv_dict(row_id=index))


def write_sqlite(result: ReviewRunResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("-wal", "-shm", "-journal"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            try:
                candidate.unlink()
            except OSError:
                pass
    sqlite_uri = f"{path.resolve().as_uri()}?mode=rwc&nolock=1"
    with sqlite3.connect(sqlite_uri, uri=True) as conn:
        conn.execute("PRAGMA journal_mode = OFF")
        create_review_schema(conn)
        conn.execute(
            """
            INSERT INTO llm_review_runs (
                provider, model, base_url, source, category, sample_size, seed,
                created_at, settings_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.provider,
                result.model,
                result.base_url,
                result.source,
                result.category,
                result.sample_size,
                result.seed,
                result.created_at,
                json.dumps(result.settings, ensure_ascii=False, sort_keys=True),
            ),
        )
        for record in result.records:
            conn.execute(
                """
                INSERT INTO llm_review_results (
                    fact_id, source_message_id, provider, model,
                    input_hash, prompt_hash,
                    original_category, original_item_type, original_needs_review,
                    decision, category_correct, item_type_correct, price_correct,
                    is_bundle, is_context_false_positive, is_non_target_room,
                    corrected_json, normalized_terms_json,
                    rationale_short, confidence, raw_response, error, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.fact.fact_id,
                    record.fact.source_message_id,
                    record.provider,
                    record.model,
                    record.input_hash,
                    record.prompt_hash,
                    record.fact.category,
                    record.fact.item_type,
                    int(record.fact.needs_review),
                    record.result.decision,
                    int(record.result.category_correct),
                    int(record.result.item_type_correct),
                    (
                        None
                        if record.result.price_correct is None
                        else int(record.result.price_correct)
                    ),
                    int(record.result.is_bundle),
                    int(record.result.is_context_false_positive),
                    int(record.result.is_non_target_room),
                    json.dumps(record.result.corrected, ensure_ascii=False, sort_keys=True),
                    json.dumps(
                        record.result.normalized_terms, ensure_ascii=False, sort_keys=True
                    ),
                    record.result.rationale_short,
                    record.result.confidence,
                    record.raw_response,
                    record.error,
                    record.created_at,
                ),
            )
        conn.commit()


def create_review_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS llm_review_results;
        DROP TABLE IF EXISTS llm_review_runs;

        CREATE TABLE llm_review_runs (
            id INTEGER PRIMARY KEY,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            base_url TEXT,
            source TEXT NOT NULL,
            category TEXT,
            sample_size INTEGER NOT NULL,
            seed INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            settings_json TEXT NOT NULL
        );

        CREATE TABLE llm_review_results (
            id INTEGER PRIMARY KEY,
            fact_id INTEGER NOT NULL,
            source_message_id INTEGER NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            input_hash TEXT NOT NULL,
            prompt_hash TEXT NOT NULL,
            original_category TEXT NOT NULL,
            original_item_type TEXT NOT NULL,
            original_needs_review INTEGER NOT NULL,
            decision TEXT NOT NULL,
            category_correct INTEGER NOT NULL,
            item_type_correct INTEGER NOT NULL,
            price_correct INTEGER,
            is_bundle INTEGER NOT NULL,
            is_context_false_positive INTEGER NOT NULL,
            is_non_target_room INTEGER NOT NULL,
            corrected_json TEXT NOT NULL,
            normalized_terms_json TEXT NOT NULL,
            rationale_short TEXT NOT NULL,
            confidence TEXT NOT NULL,
            raw_response TEXT,
            error TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX idx_llm_review_decision ON llm_review_results(decision);
        CREATE INDEX idx_llm_review_fact ON llm_review_results(fact_id);
        CREATE INDEX idx_llm_review_input_hash ON llm_review_results(input_hash);
        """
    )


def load_existing_input_hashes(path: Path, *, provider: str, model: str) -> set[str]:
    if not path.exists():
        return set()
    sqlite_uri = f"{path.resolve().as_uri()}?mode=ro"
    try:
        with sqlite3.connect(sqlite_uri, uri=True) as conn:
            cursor = conn.execute(
                "SELECT input_hash FROM llm_review_results WHERE provider=? AND model=?",
                (provider, model),
            )
            return {row[0] for row in cursor}
    except sqlite3.Error:
        return set()


def build_summary_markdown(result: ReviewRunResult) -> str:
    records = result.records
    decisions = Counter(record.result.decision for record in records)
    by_category = defaultdict(Counter)
    for record in records:
        by_category[record.fact.category][record.result.decision] += 1
    by_review_flag = Counter(
        (record.result.decision, int(record.fact.needs_review)) for record in records
    )
    bundle_count = sum(1 for record in records if record.result.is_bundle)
    context_fp_count = sum(
        1 for record in records if record.result.is_context_false_positive
    )
    price_incorrect = sum(
        1 for record in records if record.result.price_correct is False
    )
    correction_targets = Counter()
    for record in records:
        corrected = record.result.corrected or {}
        for key, value in corrected.items():
            if value not in (None, ""):
                correction_targets[key] += 1

    lines: list[str] = []
    lines.append("# LLM Review Summary")
    lines.append("")
    lines.append(f"- Provider: `{result.provider}`")
    lines.append(f"- Model: `{result.model}`")
    lines.append(f"- Base URL: `{result.base_url}`")
    lines.append(f"- Source: `{result.source}`")
    lines.append(f"- Category filter: `{result.category or '(none)'}`")
    lines.append(f"- Sample size: {result.sample_size}")
    lines.append(f"- Seed: {result.seed}")
    lines.append(f"- Created at: `{result.created_at}`")
    lines.append(f"- Total reviewed: {len(records)}")
    lines.append(f"- Skipped (resume): {len(result.skipped)}")
    lines.append(f"- Invalid JSON / provider errors: {result.invalid_count}")
    lines.append(f"- Retries triggered: {result.retry_count}")
    lines.append("")
    lines.append("## Decisions")
    lines.append("")
    lines.append("| Decision | Count |")
    lines.append("|---|---:|")
    for decision in ("keep", "fix", "discard", "needs_human"):
        lines.append(f"| `{decision}` | {decisions.get(decision, 0)} |")
    lines.append("")
    lines.append("## Decisions by Category")
    lines.append("")
    lines.append("| Category | keep | fix | discard | needs_human |")
    lines.append("|---|---:|---:|---:|---:|")
    for category in STRICT_CATEGORIES:
        counts = by_category.get(category, Counter())
        lines.append(
            f"| `{category}` | {counts.get('keep', 0)} | {counts.get('fix', 0)} | "
            f"{counts.get('discard', 0)} | {counts.get('needs_human', 0)} |"
        )
    lines.append("")
    lines.append("## Decisions by Original `needs_review` Flag")
    lines.append("")
    lines.append("| Original needs_review | Decision | Count |")
    lines.append("|---:|---|---:|")
    for (decision, flag), count in sorted(by_review_flag.items()):
        lines.append(f"| {flag} | `{decision}` | {count} |")
    lines.append("")
    lines.append("## Signals")
    lines.append("")
    lines.append("| Signal | Count |")
    lines.append("|---|---:|")
    lines.append(f"| Bundle confirmations | {bundle_count} |")
    lines.append(f"| Context false-positive confirmations | {context_fp_count} |")
    lines.append(f"| Price incorrect | {price_incorrect} |")
    lines.append("")
    lines.append("## Top Correction Targets")
    lines.append("")
    if not correction_targets:
        lines.append("No corrections returned.")
        lines.append("")
    else:
        lines.append("| Field | Times corrected |")
        lines.append("|---|---:|")
        for field_name, count in correction_targets.most_common(12):
            lines.append(f"| `{field_name}` | {count} |")
        lines.append("")
    lines.append("## Limitations")
    lines.append("")
    lines.append("- LLM output is advisory; deterministic extraction remains the source of record.")
    lines.append("- No image evidence is sent to the model in Stage 2.5.")
    lines.append("- Bad responses are recorded as `needs_human` after one repair retry.")
    lines.append("")
    return "\n".join(lines)


def write_by_decision(records: list[ReviewRecord], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    decisions = ("keep", "fix", "discard", "needs_human")
    paths: list[Path] = []
    for decision in decisions:
        path = out_dir / f"{decision}.md"
        subset = [record for record in records if record.result.decision == decision]
        path.write_text(build_decision_markdown(decision, subset), encoding="utf-8")
        paths.append(path)
    return paths


def build_decision_markdown(decision: str, records: list[ReviewRecord]) -> str:
    lines: list[str] = [f"# Decision: {decision}", "", f"- Count: {len(records)}", ""]
    if not records:
        lines.append("No records.")
        lines.append("")
        return "\n".join(lines)
    for record in records:
        fact = record.fact
        result = record.result
        lines.append(f"## fact_id={fact.fact_id} (source_message_id={fact.source_message_id})")
        lines.append("")
        lines.append(f"- Original category/item: `{fact.category}` / `{fact.item_type}`")
        lines.append(f"- Original confidence: `{fact.confidence}` needs_review={int(fact.needs_review)}")
        lines.append(f"- Vendor: {fact.vendor_normalized or fact.vendor_raw or '—'}")
        if fact.price_value is not None:
            lines.append(
                f"- Price: {fact.price_value} {fact.price_currency or ''} {fact.price_unit or ''}"
            )
        lines.append(f"- Evidence: {_escape_md(fact.evidence_quote)}")
        lines.append("")
        corrected_visible = {
            key: value
            for key, value in (result.corrected or {}).items()
            if value not in (None, "")
        }
        if corrected_visible:
            lines.append("- Corrected fields:")
            for key, value in sorted(corrected_visible.items()):
                lines.append(f"  - `{key}` → {value}")
        lines.append(f"- Rationale: {_escape_md(result.rationale_short)}")
        lines.append(f"- Confidence: `{result.confidence}`")
        lines.append("")
    return "\n".join(lines)


def write_by_category(records: list[ReviewRecord], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for category in STRICT_CATEGORIES:
        path = out_dir / f"{category}.md"
        subset = [record for record in records if record.fact.category == category]
        path.write_text(build_category_markdown(category, subset), encoding="utf-8")
        paths.append(path)
    return paths


def build_category_markdown(category: str, records: list[ReviewRecord]) -> str:
    lines: list[str] = [f"# Category: {category}", "", f"- Count: {len(records)}", ""]
    if not records:
        lines.append("No records.")
        lines.append("")
        return "\n".join(lines)
    decisions = Counter(record.result.decision for record in records)
    lines.append("Decisions:")
    for decision in ("keep", "fix", "discard", "needs_human"):
        lines.append(f"- `{decision}`: {decisions.get(decision, 0)}")
    lines.append("")
    lines.append("| fact_id | item_type | decision | confidence | evidence | rationale |")
    lines.append("|---:|---|---|---|---|---|")
    for record in records:
        lines.append(
            "| "
            f"{record.fact.fact_id} | "
            f"`{record.fact.item_type}` | "
            f"`{record.result.decision}` | "
            f"`{record.result.confidence}` | "
            f"{_escape_md(record.fact.evidence_quote)} | "
            f"{_escape_md(record.result.rationale_short)} |"
        )
    lines.append("")
    return "\n".join(lines)


def _escape_md(value: str | None) -> str:
    if not value:
        return ""
    return (
        value.replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\n", " ")
        .replace("\r", " ")
    )
