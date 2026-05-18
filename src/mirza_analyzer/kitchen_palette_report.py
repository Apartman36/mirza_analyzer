from __future__ import annotations

import csv
import json
import math
import re
import shutil
import sqlite3
import stat
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .utils import compact_whitespace, json_dumps, truncate, utc_now_iso


REPORT_FORMATS = ("markdown",)
CHANNEL_LINK_STATUS = "unverified_requires_manual_verification"

DIRECT_KITCHEN_ITEM_TYPES = {"kitchen_facades", "countertop", "backsplash"}
CONTEXT_CATEGORIES = {"wall_colors", "flooring"}

DEFAULT_DESIGNER = "Ольга Мирзабаева / команда канала"


@dataclass(frozen=True)
class PaletteCategory:
    category_id: str
    label: str
    report_title: str
    short_description: str


PALETTE_CATEGORIES: tuple[PaletteCategory, ...] = (
    PaletteCategory(
        category_id="wood_neutral",
        label="Светлое дерево + тёплый нейтральный фасад",
        report_title="Категория 1 — Светлое дерево + тёплый нейтральный фасад",
        short_description=(
            "Древесный декор фасадов сочетается с тёплыми нейтральными тонами: "
            "капучино, кашемир, латте, молочный, тальк, бежевый или мягкий серый."
        ),
    ),
    PaletteCategory(
        category_id="wood_nature_accent",
        label="Дерево + цветной/природный акцент",
        report_title="Категория 2 — Дерево + цветной/природный акцент",
        short_description=(
            "Дерево остаётся базой, но рядом появляется более заметный природный "
            "акцент: зелёный, оливковый, синий, сапфировый, терракотовый, тёмное "
            "дерево или похожие насыщенные материалы."
        ),
    ),
    PaletteCategory(
        category_id="light_facade_stone_accent",
        label="Светлый фасад + камень/фартук/столешница как акцент",
        report_title="Категория 3 — Светлый фасад + камень/фартук/столешница как акцент",
        short_description=(
            "Светлый фасад работает как спокойная база, а фартук или столешница "
            "становятся главным материальным акцентом: камень, мраморный рисунок, "
            "выразительная плитка или композит."
        ),
    ),
)

PALETTE_BY_ID = {category.category_id: category for category in PALETTE_CATEGORIES}
PALETTE_ORDER = {category.category_id: index for index, category in enumerate(PALETTE_CATEGORIES)}
CONFIDENCE_SORT_VALUE = {"low": 0, "medium": 1, "high": 2}
CONFIDENCE_RU = {"high": "высокая", "medium": "средняя", "low": "низкая"}
QUALITY_RU = {"high": "🟢 сильный пример", "medium": "🟡 средний пример", "low": "⚪ слабый кандидат"}
MIN_HIGH_SCORE = 8
MIN_MEDIUM_SCORE = 5
MIN_HIGH_PER_CATEGORY = 3


@dataclass(frozen=True)
class KitchenFact:
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
    needs_review: bool
    notes: str | None
    first_photo_path: str | None


@dataclass(frozen=True)
class CanonicalMessage:
    message_id: int
    date: str | None
    text_plain: str
    text_entities_json: str | None
    raw_best_json: str | None


@dataclass(frozen=True)
class TelegramLink:
    href: str
    text: str
    message_id: int
    context: str


@dataclass
class KitchenProject:
    project_post_id: int
    source_message_ids: list[int]
    date: str | None
    object_name: str
    object_source: str
    area_type: str | None
    city: str | None
    designer: str
    designer_source: str
    candidate_project_url: str
    candidate_article_urls: list[str]
    facade_finish_raw: str | None
    facade_parts: list[str]
    countertop_raw: str | None
    backsplash_raw: str | None
    wall_color: str | None
    flooring: str | None
    vendors: list[str]
    prices: list[str]
    evidence_quotes: list[str]
    photo_paths: list[str]
    kitchen_item_types: list[str] = field(default_factory=list)
    copied_photo_paths: list[str] = field(default_factory=list)
    contact_sheet_path: str | None = None
    palette_category_id: str | None = None
    palette_category_label: str | None = None
    secondary_palette_candidates: list[str] = field(default_factory=list)
    palette_summary: str | None = None
    palette_summary_clean: str | None = None
    confidence: str = "low"
    confidence_reason: str = ""
    quality_score: int = 0
    quality_tier: str = "low"
    has_clean_facade: bool = False
    has_countertop: bool = False
    has_backsplash: bool = False
    has_photo: bool = False
    selected_for_report: bool = False
    selected_for_clean_report: bool = False
    exclusion_reason: str | None = None

    @property
    def example_id(self) -> str:
        return f"KITCHEN-{self.project_post_id}"


@dataclass(frozen=True)
class KitchenPaletteReportResult:
    out_dir: Path
    generated_at: str
    kitchen_fact_count: int
    project_candidate_count: int
    selected_by_category: dict[str, int]
    contact_sheet_count: int
    examples_without_enough_photos: int
    output_files: list[Path]
    projects: list[KitchenProject]


def build_kitchen_palette_report(
    *,
    facts_db: Path,
    canonical_db: Path,
    out_dir: Path,
    channel_username: str,
    examples_per_category: int = 6,
    photos_per_example: int = 2,
    output_format: str = "markdown",
) -> KitchenPaletteReportResult:
    if output_format not in REPORT_FORMATS:
        raise ValueError("Stage 4 supports Markdown output only. Use --format markdown.")
    if examples_per_category < 1:
        raise ValueError("--examples-per-category must be at least 1")
    if photos_per_example < 1:
        raise ValueError("--photos-per-example must be at least 1")
    if not facts_db.exists():
        raise FileNotFoundError(f"facts database not found: {facts_db}")
    if not canonical_db.exists():
        raise FileNotFoundError(f"canonical database not found: {canonical_db}")

    out_dir.mkdir(parents=True, exist_ok=True)
    contact_dir = out_dir / "contact_sheets"
    images_dir = out_dir / "images_by_example"
    contact_dir.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(parents=True, exist_ok=True)
    clear_generated_media_dir(contact_dir)
    clear_generated_media_dir(images_dir)

    generated_at = utc_now_iso()
    kitchen_facts = load_kitchen_facts(facts_db)
    context_facts = load_context_facts(facts_db)
    messages = load_canonical_messages(canonical_db)
    photos_by_message = load_photo_paths(canonical_db)

    projects = build_kitchen_projects(
        kitchen_facts=kitchen_facts,
        context_facts=context_facts,
        messages=messages,
        photos_by_message=photos_by_message,
        channel_username=channel_username,
    )
    classify_projects(projects)
    select_report_examples(projects, examples_per_category=examples_per_category)
    prepare_report_images(
        projects,
        out_dir=out_dir,
        contact_dir=contact_dir,
        images_dir=images_dir,
        photos_per_example=photos_per_example,
    )

    output_files = write_outputs(
        projects=projects,
        out_dir=out_dir,
        generated_at=generated_at,
        kitchen_fact_count=len(kitchen_facts),
        channel_username=channel_username,
    )
    selected_by_category = {
        category.category_id: sum(
            1
            for project in projects
            if project.selected_for_report and project.palette_category_id == category.category_id
        )
        for category in PALETTE_CATEGORIES
    }
    return KitchenPaletteReportResult(
        out_dir=out_dir,
        generated_at=generated_at,
        kitchen_fact_count=len(kitchen_facts),
        project_candidate_count=len(projects),
        selected_by_category=selected_by_category,
        contact_sheet_count=sum(1 for project in projects if project.contact_sheet_path),
        examples_without_enough_photos=sum(
            1 for project in projects if project.selected_for_report and len(project.photo_paths) < photos_per_example
        ),
        output_files=output_files,
        projects=projects,
    )


def clear_generated_media_dir(path: Path) -> None:
    for child in path.iterdir():
        try:
            if child.is_dir():
                make_tree_writable(child)
                shutil.rmtree(child)
            elif child.is_file():
                child.chmod(child.stat().st_mode | stat.S_IWRITE)
                child.unlink()
        except OSError:
            continue


def make_tree_writable(path: Path) -> None:
    for child in path.rglob("*"):
        try:
            child.chmod(child.stat().st_mode | stat.S_IWRITE)
        except OSError:
            continue
    try:
        path.chmod(path.stat().st_mode | stat.S_IWRITE)
    except OSError:
        pass


def load_kitchen_facts(facts_db: Path) -> list[KitchenFact]:
    return [
        fact
        for fact in load_facts_by_categories(facts_db, categories={"kitchens"})
        if fact.category == "kitchens"
    ]


def load_context_facts(facts_db: Path) -> list[KitchenFact]:
    return load_facts_by_categories(facts_db, categories=CONTEXT_CATEGORIES)


def load_facts_by_categories(facts_db: Path, *, categories: set[str]) -> list[KitchenFact]:
    sqlite_uri = f"{facts_db.resolve().as_uri()}?mode=ro"
    placeholders = ", ".join("?" for _ in categories)
    with sqlite3.connect(sqlite_uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT
                id, source_message_id, date, category, item_type, item_name,
                vendor_raw, vendor_normalized, brand_raw, brand_normalized,
                model, material, finish, color, color_code, article_id,
                marketplace, price_value, price_currency, price_unit,
                room_context, evidence_quote, confidence, needs_review,
                notes, first_photo_path
            FROM extracted_facts
            WHERE category IN ({placeholders})
            ORDER BY date, source_message_id, id
            """,
            tuple(sorted(categories)),
        ).fetchall()
    return [
        KitchenFact(
            fact_id=int(row["id"]),
            source_message_id=int(row["source_message_id"]),
            date=_clean(row["date"]),
            category=_clean(row["category"]) or "",
            item_type=_clean(row["item_type"]) or "",
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
            confidence=_clean(row["confidence"]) or "medium",
            needs_review=bool(row["needs_review"]),
            notes=_clean(row["notes"]),
            first_photo_path=_clean(row["first_photo_path"]),
        )
        for row in rows
    ]


def load_canonical_messages(canonical_db: Path) -> dict[int, CanonicalMessage]:
    sqlite_uri = f"{canonical_db.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(sqlite_uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                telegram_message_id,
                date,
                text_plain,
                text_entities_json,
                raw_best_json
            FROM canonical_messages
            ORDER BY telegram_message_id
            """
        ).fetchall()
    return {
        int(row["telegram_message_id"]): CanonicalMessage(
            message_id=int(row["telegram_message_id"]),
            date=_clean(row["date"]),
            text_plain=row["text_plain"] or "",
            text_entities_json=row["text_entities_json"],
            raw_best_json=row["raw_best_json"],
        )
        for row in rows
    }


def load_photo_paths(canonical_db: Path) -> dict[int, list[str]]:
    sqlite_uri = f"{canonical_db.resolve().as_uri()}?mode=ro"
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
    photos: defaultdict[int, list[str]] = defaultdict(list)
    for row in rows:
        photos[int(row["telegram_message_id"])].append(str(row["absolute_path"]))
    return dict(photos)


def build_kitchen_projects(
    *,
    kitchen_facts: Sequence[KitchenFact],
    context_facts: Sequence[KitchenFact],
    messages: dict[int, CanonicalMessage],
    photos_by_message: dict[int, list[str]],
    channel_username: str,
) -> list[KitchenProject]:
    source_to_project: dict[int, int] = {}
    for fact in kitchen_facts:
        if fact.source_message_id not in source_to_project:
            message = messages.get(fact.source_message_id)
            source_to_project[fact.source_message_id] = resolve_project_post_id(
                message,
                channel_username=channel_username,
                fallback_message_id=fact.source_message_id,
            )

    grouped: defaultdict[int, list[KitchenFact]] = defaultdict(list)
    source_ids_by_project: defaultdict[int, set[int]] = defaultdict(set)
    for fact in kitchen_facts:
        project_post_id = source_to_project[fact.source_message_id]
        grouped[project_post_id].append(fact)
        source_ids_by_project[project_post_id].add(fact.source_message_id)

    context_by_source: defaultdict[int, list[KitchenFact]] = defaultdict(list)
    for fact in context_facts:
        context_by_source[fact.source_message_id].append(fact)

    projects: list[KitchenProject] = []
    for project_post_id, facts in grouped.items():
        source_message_ids = sorted(source_ids_by_project[project_post_id])
        project_message = messages.get(project_post_id)
        source_messages = [messages[source_id] for source_id in source_message_ids if source_id in messages]
        text_for_context = project_message.text_plain if project_message else ""
        if not text_for_context:
            text_for_context = "\n".join(message.text_plain for message in source_messages if message.text_plain)

        object_name, object_source = extract_object_name(
            text_for_context,
            date=(project_message.date if project_message else facts[0].date),
            project_post_id=project_post_id,
        )
        designer, designer_source = extract_designer(text_for_context)
        area_type = extract_area_type(text_for_context)
        city = extract_city(text_for_context)

        related_context_facts = []
        for source_id in set(source_message_ids) | {project_post_id}:
            related_context_facts.extend(context_by_source.get(source_id, []))

        photo_paths = collect_project_photos(
            project_post_id=project_post_id,
            source_message_ids=source_message_ids,
            messages=messages,
            photos_by_message=photos_by_message,
            max_candidates=12,
        )

        facade_finish_raw = summarize_fact_values(facts, item_types={"kitchen_facades"})
        countertop_raw = summarize_fact_values(facts, item_types={"countertop"})
        backsplash_raw = summarize_fact_values(facts, item_types={"backsplash"})
        wall_color = summarize_context_value(related_context_facts, category="wall_colors")
        flooring = summarize_context_value(related_context_facts, category="flooring")
        vendors = summarize_vendors(facts)
        prices = summarize_prices(facts)
        evidence_quotes = summarize_evidence_quotes(facts)

        project = KitchenProject(
            project_post_id=project_post_id,
            source_message_ids=source_message_ids,
            date=project_message.date if project_message else first_non_empty(fact.date for fact in facts),
            object_name=object_name,
            object_source=object_source,
            area_type=area_type,
            city=city,
            designer=designer,
            designer_source=designer_source,
            candidate_project_url=telegram_post_url(channel_username, project_post_id),
            candidate_article_urls=[
                telegram_post_url(channel_username, source_id)
                for source_id in source_message_ids
                if source_id != project_post_id
            ],
            facade_finish_raw=facade_finish_raw,
            facade_parts=parse_facade_parts(facade_finish_raw),
            countertop_raw=countertop_raw,
            backsplash_raw=backsplash_raw,
            wall_color=wall_color,
            flooring=flooring,
            vendors=vendors,
            prices=prices,
            evidence_quotes=evidence_quotes,
            kitchen_item_types=sorted({fact.item_type for fact in facts}),
            photo_paths=photo_paths,
        )
        score_project_quality(project=project)
        projects.append(project)

    return sorted(projects, key=project_sort_key, reverse=True)


def resolve_project_post_id(
    message: CanonicalMessage | None,
    *,
    channel_username: str,
    fallback_message_id: int,
) -> int:
    if message is None:
        return fallback_message_id
    for link in extract_channel_links(message, channel_username=channel_username):
        if is_project_link_context(link.text, link.context):
            return link.message_id
    return fallback_message_id


def extract_channel_links(message: CanonicalMessage, *, channel_username: str) -> list[TelegramLink]:
    links: list[TelegramLink] = []
    for entity in _load_entities(message.text_entities_json):
        href = str(entity.get("href") or "")
        text = compact_whitespace(str(entity.get("text") or ""))
        message_id = parse_channel_message_id(href, channel_username=channel_username)
        if message_id is None:
            continue
        links.append(
            TelegramLink(
                href=href,
                text=text,
                message_id=message_id,
                context=link_context_from_text(message.text_plain, text),
            )
        )

    for match in channel_url_re(channel_username).finditer(message.text_plain or ""):
        message_id = int(match.group("message_id"))
        href = match.group(0)
        if any(link.href == href or link.message_id == message_id for link in links):
            continue
        links.append(
            TelegramLink(
                href=href,
                text=href,
                message_id=message_id,
                context=surrounding_text(message.text_plain, match.start(), match.end()),
            )
        )
    return links


def parse_channel_message_id(href: str, *, channel_username: str) -> int | None:
    match = channel_url_re(channel_username).search(href or "")
    if not match:
        return None
    return int(match.group("message_id"))


def channel_url_re(channel_username: str) -> re.Pattern[str]:
    escaped = re.escape(channel_username)
    return re.compile(
        rf"https?://t\.me/(?:s/)?{escaped}/(?P<message_id>\d+)(?:\?single)?",
        flags=re.IGNORECASE,
    )


def is_project_link_context(link_text: str, context: str) -> bool:
    text = normalize_for_match(f"{link_text} {context}")
    has_project_signal = any(
        token in text
        for token in (
            "артикулы проекта",
            "артикул проекта",
            "пост о проекте",
            "о проекте",
            "проекта",
            "проект",
        )
    )
    if not has_project_signal:
        return False
    if any(token in text for token in ("тариф", "промокод", "скидк")) and "артикул" not in text:
        return False
    return True


def extract_object_name(
    text: str,
    *,
    date: str | None,
    project_post_id: int,
) -> tuple[str, str]:
    lines = meaningful_lines(text)
    for line in lines[:20]:
        match = re.search(r"\bЖК\.?\s+(.+)", line, flags=re.IGNORECASE)
        if match:
            value = compact_whitespace("ЖК " + match.group(1))
            value = re.split(r"\s{2,}|[|•]", value, maxsplit=1)[0].strip(" .,:;-")
            if value:
                return value, "project_text_jk"

    for line in lines[:25]:
        if re.search(
            r"\b(ул\.|улица|проспект|пр-т|бульвар|шоссе|переулок|наб\.|набережная)\b",
            line,
            flags=re.IGNORECASE,
        ):
            return compact_whitespace(line).strip(" .,:;-"), "project_text_street"

    fallback_date = (date or "").split("T", 1)[0] or "без даты"
    return f"Пост от {fallback_date}, message_id={project_post_id}", "fallback_post_id"


def extract_area_type(text: str) -> str | None:
    for line in meaningful_lines(text)[:15]:
        normalized = normalize_for_match(line)
        if re.search(r"\b(студия|евро\s*-?\s*\d|[1234]\s*-?\s*комнат)", normalized):
            if re.search(r"\b\d+(?:[,.]\d+)?\s*(?:м2|м²|кв\.?\s*м|кв\.)\b", normalized):
                return compact_whitespace(line)
    return None


def extract_city(text: str) -> str | None:
    for line in meaningful_lines(text)[:20]:
        for city in ("Санкт-Петербург", "Москва", "Казань", "Сочи"):
            if city.casefold() in line.casefold():
                return city
    return None


def extract_designer(text: str) -> tuple[str, str]:
    credits: list[str] = []
    for line in meaningful_lines(text)[:30]:
        match = re.search(
            r"\b(Дизайнеры?|Хоумстейджеры?)\s*[:—–-]?\s+(.+)",
            line,
            flags=re.IGNORECASE,
        )
        if not match:
            continue
        role = normalize_credit_role(match.group(1))
        names = clean_credit_names(match.group(2))
        if names:
            credits.append(f"{role} {names}")
    if credits:
        return "; ".join(unique_preserve_order(credits)), "credited_in_post"
    return DEFAULT_DESIGNER, "default_channel_author"


def normalize_credit_role(value: str) -> str:
    value = value.casefold()
    if value.startswith("дизайнер"):
        return "Дизайнеры" if value.startswith("дизайнеры") else "Дизайнер"
    return "Хоумстейджеры" if value.startswith("хоумстейджеры") else "Хоумстейджер"


def clean_credit_names(value: str) -> str | None:
    text = compact_whitespace(value)
    text = re.split(
        r"\s+(?:Задача|Бюджет|Смета|Кухня|Гостиная|Спальня)\b|[.!?]",
        text,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    text = text.strip(" .,:;—–-")
    if not text:
        return None
    if len(text) > 80:
        return None
    return text


def collect_project_photos(
    *,
    project_post_id: int,
    source_message_ids: Sequence[int],
    messages: dict[int, CanonicalMessage],
    photos_by_message: dict[int, list[str]],
    max_candidates: int,
) -> list[str]:
    ordered_message_ids: list[int] = [project_post_id]
    ordered_message_ids.extend(source_id for source_id in source_message_ids if source_id != project_post_id)

    project_date = (messages.get(project_post_id).date if project_post_id in messages else None) or ""
    project_day = project_date[:10]
    for message_id in range(project_post_id + 1, project_post_id + 9):
        message = messages.get(message_id)
        if message is None or message_id not in photos_by_message:
            continue
        if project_day and (message.date or "")[:10] != project_day:
            continue
        if not is_nearby_album_message(message):
            continue
        ordered_message_ids.append(message_id)

    paths: list[str] = []
    seen: set[str] = set()
    for message_id in ordered_message_ids:
        for path in photos_by_message.get(message_id, []):
            normalized = str(path)
            if normalized in seen:
                continue
            seen.add(normalized)
            paths.append(normalized)
            if len(paths) >= max_candidates:
                return paths
    return paths


def is_nearby_album_message(message: CanonicalMessage) -> bool:
    text = compact_whitespace(message.text_plain or "")
    return not text or len(text) <= 180


def summarize_fact_values(facts: Sequence[KitchenFact], *, item_types: set[str]) -> str | None:
    values: list[str] = []
    for fact in facts:
        if fact.item_type not in item_types:
            continue
        value = fact_primary_value(fact, include_evidence=False)
        if value:
            values.append(value)
    return "; ".join(unique_preserve_order(values)) or None


def summarize_context_value(facts: Sequence[KitchenFact], *, category: str) -> str | None:
    values: list[str] = []
    for fact in facts:
        if fact.category != category:
            continue
        value = fact_primary_value(fact, include_evidence=False)
        if value:
            values.append(value)
    return "; ".join(unique_preserve_order(values[:5])) or None


def fact_primary_value(fact: KitchenFact, *, include_evidence: bool = True) -> str | None:
    candidates = [
        fact.finish,
        fact.material,
        fact.model,
        fact.color_code,
        fact.color,
        fact.brand_normalized,
        fact.brand_raw,
        fact.item_name,
    ]
    if include_evidence:
        candidates.append(fact.evidence_quote)
    for candidate in candidates:
        cleaned = clean_kitchen_palette_value(candidate or "")
        if cleaned and is_useful_report_value(cleaned) and is_clean_kitchen_palette_value(cleaned):
            return cleaned
    return None


def is_useful_report_value(value: str) -> bool:
    text = compact_whitespace(value).strip()
    if len(text) < 2:
        return False
    if len(text) > 220:
        return False
    lowered = normalize_for_match(text)
    return lowered not in {"арт", "артикул", "кухня", "фартук", "столешница"}


NOISE_EXACT_VALUES = {
    "задача",
    "арт",
    "артикул",
    "кухня",
    "на кухне",
    "в кухне",
    "рабочее",
    "панель",
    "алюмика",
}

NOISE_PHRASE_TOKENS = (
    "функциональность",
    "максимальное количество мест хранения",
    "заказчики на полном доверии",
    "и панель мы тоже сделали",
    "практически как на заказ",
    "обратите внимание",
    "задача:",
    "на кухне из акрила",
)

PALETTE_MATERIAL_TOKENS = (
    "дуб",
    "каселла",
    "орех",
    "карини",
    "гикори",
    "чарльстон",
    "капучино",
    "латте",
    "кашемир",
    "меренга",
    "нубук",
    "сапфир",
    "эбони",
    "зелен",
    "зелён",
    "олив",
    "графит",
    "мдф",
    "premium white",
    "премиум вайт",
    "бел",
    "светл",
    "пластик",
    "доминикана",
    "форст",
    "камень",
    "мрамор",
    "grandex",
    "laparet",
    "сантехника онлайн",
    "лемана",
    "rusplitka",
    "фартук",
    "столешниц",
    "плитк",
)

KITCHEN_RELEVANT_VENDOR_TOKENS = (
    "mebel.in",
    "мебел",
    "леруа",
    "лемана",
    "сантехника онлайн",
    "rusplitka",
    "laparet",
    "grandex",
)


def clean_kitchen_palette_value(value: str) -> str:
    text = compact_whitespace(value or "")
    text = text.strip(" \t\r\n\"'«».,:;—–-")
    text = re.sub(r"\s+", " ", text)
    if len(text) > 120:
        text = re.split(r"[.!?]\s+|\n", text, maxsplit=1)[0].strip(" .,:;—–-")
    return text


def is_noise_kitchen_phrase(value: str) -> bool:
    text = clean_kitchen_palette_value(value)
    if not text:
        return True
    lowered = normalize_for_match(text)
    if lowered in NOISE_EXACT_VALUES:
        return True
    if lowered.startswith("задача"):
        return True
    if any(token in lowered for token in NOISE_PHRASE_TOKENS):
        return True
    if len(text) > 90 and not (
        ("+" in text or " и " in lowered)
        and contains_any(lowered, PALETTE_MATERIAL_TOKENS)
        and contains_any(lowered, ("фасад", "мдф", "дуб", "орех", "гикори", "каселла", "карини"))
    ):
        return True
    if re.search(r"\b(сдать|аренд|продать|хранени[ея]|доверии|пожелани[ея])\b", lowered):
        if not (contains_any(lowered, PALETTE_MATERIAL_TOKENS) and ("+" in text or " и " in lowered)):
            return True
    return False


def is_clean_kitchen_palette_value(value: str) -> bool:
    text = clean_kitchen_palette_value(value)
    if not text or is_noise_kitchen_phrase(text):
        return False
    lowered = normalize_for_match(text)
    if lowered == "алюмика":
        return False
    if len(text) < 2:
        return False
    return contains_any(lowered, PALETTE_MATERIAL_TOKENS) or bool(re.search(r"\b\d{3,5}\b", text))


def is_strong_facade_phrase(value: str) -> bool:
    text = clean_kitchen_palette_value(value)
    if not text or is_noise_kitchen_phrase(text):
        return False
    lowered = normalize_for_match(text)
    has_pair_signal = "+" in text or " и " in lowered or ";" in text or "," in text
    has_facade_context = contains_any(lowered, ("фасад", "мдф", "дуб", "орех", "гикори", "каселла", "карини", "чарльстон"))
    has_finish_signal = contains_any(
        lowered,
        WOOD_TOKENS + NEUTRAL_TOKENS + ACCENT_TOKENS + LIGHT_FACADE_TOKENS,
    )
    if contains_any(lowered, ("premium white", "премиум вайт")):
        return True
    return has_facade_context and has_finish_signal and (has_pair_signal or contains_any(lowered, ("premium white", "мдф", "нубук")))


def parse_facade_parts(value: str | None) -> list[str]:
    if not value:
        return []
    parts = re.split(r"\s*(?:\+|,|;|/|\band\b|\bи\b)\s*", value, flags=re.IGNORECASE)
    return unique_preserve_order(
        clean_kitchen_palette_value(part)
        for part in parts
        if is_useful_report_value(part) and is_clean_kitchen_palette_value(part)
    )


def summarize_vendors(facts: Sequence[KitchenFact]) -> list[str]:
    values = [
        fact.vendor_normalized or fact.vendor_raw or fact.marketplace
        for fact in facts
        if fact.vendor_normalized or fact.vendor_raw or fact.marketplace
    ]
    return unique_preserve_order(_clean(value) for value in values if value)


def summarize_prices(facts: Sequence[KitchenFact]) -> list[str]:
    prices: list[str] = []
    for fact in facts:
        if fact.price_value is None:
            continue
        value: float | int = fact.price_value
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        currency = fact.price_currency or "₽"
        unit = f"/{fact.price_unit}" if fact.price_unit else ""
        label = readable_item_type(fact.item_type)
        prices.append(f"{label}: {value} {currency}{unit}")
    return unique_preserve_order(prices)


def summarize_evidence_quotes(facts: Sequence[KitchenFact]) -> list[str]:
    direct = [fact.evidence_quote for fact in facts if fact.item_type in DIRECT_KITCHEN_ITEM_TYPES]
    fallback = [fact.evidence_quote for fact in facts if fact.item_type not in DIRECT_KITCHEN_ITEM_TYPES]
    quotes = [
        compact_whitespace(quote)
        for quote in [*direct, *fallback]
        if quote and len(compact_whitespace(quote)) >= 3
    ]
    return unique_preserve_order(quotes)[:6]


def score_project_quality(*, project: KitchenProject) -> None:
    item_types = set(project.kitchen_item_types)
    project.has_clean_facade = bool(project.facade_finish_raw and is_strong_facade_phrase(project.facade_finish_raw))
    project.has_countertop = bool(project.countertop_raw and is_clean_kitchen_palette_value(project.countertop_raw))
    project.has_backsplash = bool(project.backsplash_raw and is_clean_kitchen_palette_value(project.backsplash_raw))
    project.has_photo = bool(project.photo_paths)

    score = 0
    reasons: list[str] = []
    if project.has_clean_facade:
        score += 4
        reasons.append("clean_facade")
    if project.has_countertop:
        score += 2
        reasons.append("countertop")
    if project.has_backsplash:
        score += 2
        reasons.append("backsplash")
    if project.wall_color:
        score += 1
        reasons.append("wall_color")
    if project.flooring:
        score += 1
        reasons.append("flooring")
    if project.object_source != "fallback_post_id":
        score += 1
        reasons.append("object")
    else:
        score -= 2
        reasons.append("synthetic_object")
    if project.designer_source == "credited_in_post":
        score += 1
        reasons.append("designer")
    if project.has_photo:
        score += 2
        reasons.append("photo")
    else:
        score -= 2
        reasons.append("no_photo")
    if any(contains_any(normalize_for_match(vendor), KITCHEN_RELEVANT_VENDOR_TOKENS) for vendor in project.vendors):
        score += 1
        reasons.append("kitchen_vendor")

    if item_types and item_types <= {"bundle_purchase"}:
        score -= 4
        reasons.append("bundle_only")
    if item_types and item_types <= {"kitchen_other", "kitchen_accessory"}:
        score -= 3
        reasons.append("only_other_or_accessory")
    if not project.has_clean_facade:
        score -= 3
        reasons.append("no_clean_facade")
    if project.palette_summary_clean and is_noise_kitchen_phrase(project.palette_summary_clean):
        score -= 5
        reasons.append("noisy_palette_summary")
    if any(len(quote) > 350 or is_noise_kitchen_phrase(quote) for quote in project.evidence_quotes[:2]):
        score -= 2
        reasons.append("long_or_task_quote")
    if any(
        normalize_for_match(value or "") in {"задача", "алюмика", "арт"}
        for value in (project.facade_finish_raw, project.countertop_raw, project.backsplash_raw)
    ):
        score -= 2
        reasons.append("non_palette_token")

    project.quality_score = score
    strong_surface_pair = project.has_countertop and project.has_backsplash
    if score >= MIN_HIGH_SCORE and project.has_clean_facade:
        tier = "high"
    elif score >= MIN_MEDIUM_SCORE and (project.has_clean_facade or strong_surface_pair):
        tier = "medium"
    else:
        tier = "low"
    project.quality_tier = tier
    project.confidence = tier
    project.confidence_reason = f"quality_score={score}; " + ", ".join(reasons)


def classify_projects(projects: Sequence[KitchenProject]) -> None:
    for project in projects:
        category_id, secondary, summary = classify_palette(project)
        project.palette_category_id = category_id
        project.palette_category_label = PALETTE_BY_ID[category_id].label if category_id else None
        project.secondary_palette_candidates = secondary
        project.palette_summary = summary
        project.palette_summary_clean = summarize_kitchen_palette(project)
        score_project_quality(project=project)


def classify_palette(project: KitchenProject) -> tuple[str | None, list[str], str | None]:
    facade_text = normalize_for_match(" ".join(project.facade_parts + [project.facade_finish_raw or ""]))
    surface_text = normalize_for_match(" ".join([project.countertop_raw or "", project.backsplash_raw or ""]))
    all_text = normalize_for_match(
        " ".join(
            [
                project.facade_finish_raw or "",
                project.countertop_raw or "",
                project.backsplash_raw or "",
                *project.evidence_quotes,
            ]
        )
    )

    has_clean_facade = bool(project.facade_finish_raw and is_strong_facade_phrase(project.facade_finish_raw))
    wood = has_clean_facade and contains_any(facade_text, WOOD_TOKENS)
    neutral = contains_any(facade_text, NEUTRAL_TOKENS)
    accent = has_clean_facade and contains_any(facade_text, ACCENT_TOKENS)
    light = contains_any(facade_text, LIGHT_FACADE_TOKENS) or contains_any(
        all_text,
        LIGHT_FACADE_TOKENS,
    )
    has_surface_fact = bool(project.countertop_raw or project.backsplash_raw)
    expressive_surface = has_surface_fact and (
        contains_any(surface_text or all_text, STONE_SURFACE_TOKENS)
        or bool(project.countertop_raw)
        or bool(project.backsplash_raw)
    )

    scores: dict[str, int] = {}
    if has_clean_facade and wood and neutral:
        scores["wood_neutral"] = 4
        if contains_any(facade_text, LIGHT_WOOD_TOKENS):
            scores["wood_neutral"] += 1
    if has_clean_facade and wood and accent:
        scores["wood_nature_accent"] = 5
        if contains_any(facade_text, DARK_OR_COLOR_TOKENS):
            scores["wood_nature_accent"] += 1
    if light and expressive_surface:
        scores["light_facade_stone_accent"] = 4
        if not wood:
            scores["light_facade_stone_accent"] += 1

    if not scores:
        return None, [], None

    category_id = max(
        scores,
        key=lambda item: (scores[item], -PALETTE_ORDER[item]),
    )
    secondary = [
        candidate
        for candidate, score in sorted(
            scores.items(),
            key=lambda item: (-item[1], PALETTE_ORDER[item[0]]),
        )
        if candidate != category_id
    ]
    return category_id, secondary, build_palette_summary(project, category_id)


WOOD_TOKENS = (
    "дуб",
    "каселла",
    "орех",
    "гикори",
    "мадейра",
    "карини",
    "чарльстон",
    "дерево",
    "древес",
    "wood",
    "oak",
    "walnut",
    "hickory",
)
LIGHT_WOOD_TOKENS = ("дуб", "каселла", "карини", "мадейра", "светл")
NEUTRAL_TOKENS = (
    "капучино",
    "кашемир",
    "латте",
    "меренга",
    "бел",
    "молоч",
    "тальк",
    "беж",
    "сер",
    "софт",
    "нубук",
    "greige",
    "грейдж",
)
ACCENT_TOKENS = (
    "зелен",
    "зелён",
    "олив",
    "сапфир",
    "эбони",
    "терракот",
    "бургунди",
    "син",
    "голуб",
    "бирюз",
    "темн",
    "тёмн",
    "холодный серый",
    "безмолвная пустыня",
    "сатин",
    "кастелло",
    "графит",
    "антрацит",
    "черн",
    "чёрн",
)
DARK_OR_COLOR_TOKENS = (
    "зелен",
    "зелён",
    "олив",
    "сапфир",
    "эбони",
    "терракот",
    "бургунди",
    "син",
    "голуб",
    "бирюз",
    "темн",
    "тёмн",
    "графит",
    "антрацит",
)
LIGHT_FACADE_TOKENS = (
    "бел",
    "светл",
    "меренг",
    "premium white",
    "премиум вайт",
    "кашемир",
    "тальк",
    "молоч",
)
STONE_SURFACE_TOKENS = (
    "доминикана",
    "камень",
    "форст",
    "grandex",
    "laparet",
    "сантехника онлайн",
    "rusplitka",
    "петрович",
    "мрамор",
    "патагон",
    "galaxy",
    "pearl",
    "antalya",
    "bianco",
    "calacatta",
    "калакат",
    "керамогранит",
    "плитк",
    "фартук",
    "столешниц",
)


def build_palette_summary(project: KitchenProject, category_id: str) -> str:
    clean = summarize_kitchen_palette(project)
    if clean:
        return clean
    return PALETTE_BY_ID[category_id].label


def summarize_kitchen_palette(project: KitchenProject) -> str:
    bits: list[str] = []
    for label, value in (
        ("фасады", project.facade_finish_raw),
        ("столешница", project.countertop_raw),
        ("фартук", project.backsplash_raw),
    ):
        if value and is_clean_kitchen_palette_value(value):
            bits.append(f"{label}: {clean_kitchen_palette_value(value)}")
    if project.wall_color and is_clean_kitchen_palette_value(project.wall_color):
        bits.append(f"стены: {clean_kitchen_palette_value(project.wall_color)}")
    if project.flooring and is_clean_kitchen_palette_value(project.flooring):
        bits.append(f"пол: {clean_kitchen_palette_value(project.flooring)}")
    return "; ".join(bits)


def select_report_examples(
    projects: Sequence[KitchenProject],
    *,
    examples_per_category: int,
) -> None:
    selected_ids: set[int] = set()
    for category in PALETTE_CATEGORIES:
        high_projects = [
            project
            for project in projects
            if project.palette_category_id == category.category_id
            and project.quality_tier == "high"
            and project.project_post_id not in selected_ids
        ]
        high_projects.sort(key=selection_sort_key, reverse=True)
        selected_for_category = high_projects[:examples_per_category]

        if len(selected_for_category) < MIN_HIGH_PER_CATEGORY:
            medium_projects = [
                project
                for project in projects
                if project.palette_category_id == category.category_id
                and project.quality_tier == "medium"
                and project.project_post_id not in selected_ids
                and project not in selected_for_category
            ]
            medium_projects.sort(key=selection_sort_key, reverse=True)
            selected_for_category.extend(medium_projects[: examples_per_category - len(selected_for_category)])

        for project in selected_for_category:
            project.selected_for_report = True
            project.selected_for_clean_report = True
            project.exclusion_reason = None
            selected_ids.add(project.project_post_id)

    for project in projects:
        if project.selected_for_report:
            continue
        project.selected_for_clean_report = False
        if project.quality_tier == "low":
            project.exclusion_reason = project_exclusion_reason(project)
        elif not project.palette_category_id:
            project.exclusion_reason = "not_classified_by_palette_rules"
        else:
            project.exclusion_reason = "not_needed_after_quality_cutoff_or_duplicate"


def project_exclusion_reason(project: KitchenProject) -> str:
    item_types = set(project.kitchen_item_types)
    if item_types and item_types <= {"bundle_purchase"}:
        return "bundle_only"
    if not project.has_photo:
        return "no_photo"
    if not project.has_clean_facade and not (project.has_countertop and project.has_backsplash):
        return "no_clean_facade"
    if project.palette_summary_clean and is_noise_kitchen_phrase(project.palette_summary_clean):
        return "noisy_palette_summary"
    return "weak_evidence"


def selection_sort_key(project: KitchenProject) -> tuple[int, int, int, int, int, str, int]:
    return (
        project.quality_score,
        CONFIDENCE_SORT_VALUE.get(project.quality_tier, 0),
        int(project.designer_source == "credited_in_post"),
        int(project.object_source != "fallback_post_id"),
        int(bool(project.contact_sheet_path or project.photo_paths)),
        project.date or "",
        project.project_post_id,
    )


def project_sort_key(project: KitchenProject) -> tuple[str, int]:
    return project.date or "", project.project_post_id


def prepare_report_images(
    projects: Sequence[KitchenProject],
    *,
    out_dir: Path,
    contact_dir: Path,
    images_dir: Path,
    photos_per_example: int,
) -> None:
    for project in projects:
        if not project.selected_for_report:
            continue
        project_image_dir = images_dir / project.example_id
        project_image_dir.mkdir(parents=True, exist_ok=True)
        project.copied_photo_paths = copy_example_images(
            project.photo_paths[:photos_per_example],
            project_image_dir,
        )
        if len(project.photo_paths) >= 3:
            contact_path = contact_dir / f"{project.example_id}.jpg"
            if create_contact_sheet(project.photo_paths[:6], contact_path):
                project.contact_sheet_path = str(contact_path)


def copy_example_images(photo_paths: Sequence[str], out_dir: Path) -> list[str]:
    copied: list[str] = []
    for index, source in enumerate(photo_paths, start=1):
        source_path = Path(source)
        if not source_path.exists() or not source_path.is_file():
            continue
        suffix = source_path.suffix.lower() or ".jpg"
        target = out_dir / f"photo_{index:02d}{suffix}"
        try:
            shutil.copy2(source_path, target)
        except OSError:
            continue
        copied.append(str(target))
    return copied


def create_contact_sheet(photo_paths: Sequence[str], out_path: Path, *, max_images: int = 6) -> bool:
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return create_contact_sheet_cv2(photo_paths, out_path, max_images=max_images)

    images = []
    for source in photo_paths[:max_images]:
        path = Path(source)
        if not path.exists() or not path.is_file():
            continue
        try:
            with Image.open(path) as image:
                images.append(image.convert("RGB").copy())
        except OSError:
            continue

    if len(images) < 3:
        return False

    columns = 3 if len(images) > 4 else 2
    rows = math.ceil(len(images) / columns)
    cell_w, cell_h = 420, 315
    padding = 16
    sheet_w = columns * cell_w + (columns + 1) * padding
    sheet_h = rows * cell_h + (rows + 1) * padding
    sheet = Image.new("RGB", (sheet_w, sheet_h), color=(248, 248, 246))

    for index, image in enumerate(images):
        thumbnail = ImageOps.contain(image, (cell_w, cell_h))
        col = index % columns
        row = index // columns
        x = padding + col * (cell_w + padding) + (cell_w - thumbnail.width) // 2
        y = padding + row * (cell_h + padding) + (cell_h - thumbnail.height) // 2
        sheet.paste(thumbnail, (x, y))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path, format="JPEG", quality=88, optimize=True)
    return True


def create_contact_sheet_cv2(photo_paths: Sequence[str], out_path: Path, *, max_images: int = 6) -> bool:
    try:
        import cv2
        import numpy as np
    except ImportError:
        return False

    images = []
    for source in photo_paths[:max_images]:
        path = Path(source)
        if not path.exists() or not path.is_file():
            continue
        image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            continue
        images.append(image)

    if len(images) < 3:
        return False

    columns = 3 if len(images) > 4 else 2
    rows = math.ceil(len(images) / columns)
    cell_w, cell_h = 420, 315
    padding = 16
    sheet_w = columns * cell_w + (columns + 1) * padding
    sheet_h = rows * cell_h + (rows + 1) * padding
    sheet = np.full((sheet_h, sheet_w, 3), 248, dtype=np.uint8)

    for index, image in enumerate(images):
        height, width = image.shape[:2]
        scale = min(cell_w / width, cell_h / height)
        resized_w = max(1, int(width * scale))
        resized_h = max(1, int(height * scale))
        thumbnail = cv2.resize(image, (resized_w, resized_h), interpolation=cv2.INTER_AREA)
        col = index % columns
        row = index // columns
        x = padding + col * (cell_w + padding) + (cell_w - resized_w) // 2
        y = padding + row * (cell_h + padding) + (cell_h - resized_h) // 2
        sheet[y : y + resized_h, x : x + resized_w] = thumbnail

    out_path.parent.mkdir(parents=True, exist_ok=True)
    ok, encoded = cv2.imencode(".jpg", sheet, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
    if not ok:
        return False
    encoded.tofile(str(out_path))
    return True


def write_outputs(
    *,
    projects: Sequence[KitchenProject],
    out_dir: Path,
    generated_at: str,
    kitchen_fact_count: int,
    channel_username: str,
) -> list[Path]:
    output_files: list[Path] = []

    csv_path = out_dir / "kitchen_examples.csv"
    write_examples_csv(projects, csv_path)
    output_files.append(csv_path)

    selected_csv_path = out_dir / "kitchen_examples_selected_clean.csv"
    write_examples_csv([project for project in projects if project.selected_for_clean_report], selected_csv_path)
    output_files.append(selected_csv_path)

    jsonl_path = out_dir / "kitchen_examples.jsonl"
    write_examples_jsonl(projects, jsonl_path)
    output_files.append(jsonl_path)

    todo_path = out_dir / "link_validation_todo.csv"
    write_link_validation_todo(projects, todo_path)
    output_files.append(todo_path)

    full_report_path = out_dir / "kitchen_palette_report.md"
    full_report_path.write_text(
        build_full_report_markdown(
            projects=projects,
            generated_at=generated_at,
            kitchen_fact_count=kitchen_fact_count,
            channel_username=channel_username,
        ),
        encoding="utf-8",
    )
    output_files.append(full_report_path)

    short_report_path = out_dir / "kitchen_palette_short.md"
    short_report_path.write_text(
        build_short_report_markdown(projects=projects, generated_at=generated_at),
        encoding="utf-8",
    )
    output_files.append(short_report_path)

    clean_short_report_path = out_dir / "kitchen_palette_short_clean.md"
    clean_short_report_path.write_text(
        build_clean_short_report_markdown(projects=projects, generated_at=generated_at),
        encoding="utf-8",
    )
    output_files.append(clean_short_report_path)

    quality_notes_path = out_dir / "kitchen_palette_quality_notes.md"
    quality_notes_path.write_text(
        build_quality_notes_markdown(
            projects=projects,
            generated_at=generated_at,
            kitchen_fact_count=kitchen_fact_count,
        ),
        encoding="utf-8",
    )
    output_files.append(quality_notes_path)

    return output_files


CSV_FIELDNAMES = [
    "example_id",
    "palette_category_id",
    "palette_category_label",
    "project_post_id",
    "source_message_ids",
    "post_date",
    "object_name",
    "object_source",
    "designer",
    "designer_source",
    "facade_finish",
    "facade_parts",
    "countertop",
    "backsplash",
    "wall_color",
    "flooring",
    "vendor_summary",
    "price_summary",
    "evidence_quote",
    "quality_score",
    "quality_tier",
    "selected_for_clean_report",
    "palette_summary_clean",
    "has_clean_facade",
    "has_countertop",
    "has_backsplash",
    "has_photo",
    "confidence",
    "confidence_reason",
    "candidate_project_url",
    "candidate_article_urls",
    "link_status",
    "photo_paths",
    "contact_sheet_path",
    "selected_for_report",
    "exclusion_reason",
]


def write_examples_csv(projects: Sequence[KitchenProject], path: Path) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        for project in sorted_projects_for_output(projects):
            writer.writerow(project_to_csv_row(project))


def project_to_csv_row(project: KitchenProject) -> dict[str, Any]:
    return {
        "example_id": project.example_id,
        "palette_category_id": project.palette_category_id or "",
        "palette_category_label": project.palette_category_label or "",
        "project_post_id": project.project_post_id,
        "source_message_ids": ";".join(str(value) for value in project.source_message_ids),
        "post_date": project.date or "",
        "object_name": project.object_name,
        "object_source": project.object_source,
        "designer": project.designer,
        "designer_source": project.designer_source,
        "facade_finish": project.facade_finish_raw or "",
        "facade_parts": "; ".join(project.facade_parts),
        "countertop": project.countertop_raw or "",
        "backsplash": project.backsplash_raw or "",
        "wall_color": project.wall_color or "",
        "flooring": project.flooring or "",
        "vendor_summary": "; ".join(project.vendors),
        "price_summary": "; ".join(project.prices),
        "evidence_quote": project.evidence_quotes[0] if project.evidence_quotes else "",
        "quality_score": project.quality_score,
        "quality_tier": project.quality_tier,
        "selected_for_clean_report": int(project.selected_for_clean_report),
        "palette_summary_clean": project.palette_summary_clean or "",
        "has_clean_facade": int(project.has_clean_facade),
        "has_countertop": int(project.has_countertop),
        "has_backsplash": int(project.has_backsplash),
        "has_photo": int(project.has_photo),
        "confidence": project.confidence,
        "confidence_reason": project.confidence_reason,
        "candidate_project_url": project.candidate_project_url,
        "candidate_article_urls": ";".join(project.candidate_article_urls),
        "link_status": CHANNEL_LINK_STATUS,
        "photo_paths": ";".join(project.photo_paths),
        "contact_sheet_path": project.contact_sheet_path or "",
        "selected_for_report": int(project.selected_for_report),
        "exclusion_reason": project.exclusion_reason or "",
    }


def write_examples_jsonl(projects: Sequence[KitchenProject], path: Path) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for project in sorted_projects_for_output(projects):
            fh.write(json_dumps(project_to_json_dict(project)))
            fh.write("\n")


def project_to_json_dict(project: KitchenProject) -> dict[str, Any]:
    data = project_to_csv_row(project)
    data.update(
        {
            "source_message_ids": project.source_message_ids,
            "candidate_article_urls": project.candidate_article_urls,
            "facade_parts": project.facade_parts,
            "vendor_summary": project.vendors,
            "price_summary": project.prices,
            "evidence_quotes": project.evidence_quotes,
            "photo_paths": project.photo_paths,
            "copied_photo_paths": project.copied_photo_paths,
            "secondary_palette_candidates": project.secondary_palette_candidates,
            "kitchen_item_types": project.kitchen_item_types,
            "area_type": project.area_type,
            "city": project.city,
        }
    )
    return data


def write_link_validation_todo(projects: Sequence[KitchenProject], path: Path) -> None:
    fieldnames = [
        "example_id",
        "project_post_id",
        "source_message_ids",
        "candidate_url",
        "candidate_article_urls",
        "status",
        "notes",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for project in sorted_projects_for_output(projects):
            if not project.selected_for_report:
                continue
            writer.writerow(
                {
                    "example_id": project.example_id,
                    "project_post_id": project.project_post_id,
                    "source_message_ids": ";".join(str(value) for value in project.source_message_ids),
                    "candidate_url": project.candidate_project_url,
                    "candidate_article_urls": ";".join(project.candidate_article_urls),
                    "status": CHANNEL_LINK_STATUS,
                    "notes": "⚠ requires manual verification",
                }
            )


def build_full_report_markdown(
    *,
    projects: Sequence[KitchenProject],
    generated_at: str,
    kitchen_fact_count: int,
    channel_username: str,
) -> str:
    selected = [project for project in projects if project.selected_for_report]
    lines = [
        "# Кухонные палитры Мирзабаевой",
        "",
        "## Кратко",
        f"- Фактов категории `kitchens`: {kitchen_fact_count}.",
        f"- Уникальных проектных кандидатов после группировки по проектному посту: {len(projects)}.",
        f"- Выбрано примеров в основной отчёт: {len(selected)}.",
        "- Найдены три рабочие гипотезы палитр: дерево + тёплый нейтрал, дерево + природный акцент, светлый фасад + выразительный фартук/столешница.",
        "- Выводы основаны на извлечённых текстовых фактах. Фото приложены как локальные вложения, но не анализировались через OCR, VLM или vision-модель.",
        "",
        "## Как читать отчёт",
        "- Это evidence-based отчёт: палитры собраны по словам из постов и строкам с фасадами, фартуками, столешницами и поставщиками.",
        "- Ссылки Telegram являются кандидатами и помечены как `⚠ requires manual verification`.",
        "- Фото и contact sheet берутся механически из поста проекта, поста-источника и ближайших сообщений того же дня.",
        "- Здесь нет 3D, apartment-specific дизайн-генерации, HTML/PDF, OCR, VLM, RAG, новых скрейпов или LLM-вызовов.",
        "",
    ]

    for category in PALETTE_CATEGORIES:
        examples = [
            project
            for project in sorted_projects_for_output(projects)
            if project.selected_for_report and project.palette_category_id == category.category_id
        ]
        lines.extend(
            [
                f"## {category.report_title}",
                "",
                category.short_description,
                "",
                f"Выбрано примеров: {len(examples)}. Цель была до 6, но отчёт не добирает слабые строки искусственно.",
                "",
            ]
        )
        if len(examples) < 6:
            lines.extend(
                [
                    "> В этой категории меньше примеров, потому что автоматически найденных сильных постов с явным сочетанием фасадов/столешницы/фартука меньше. Мы не добирали примеры шумными строками.",
                    "",
                ]
            )
        for index, project in enumerate(examples, start=1):
            lines.extend(build_example_markdown(project, index=index, out_dir_name=""))
            lines.append("")

    lines.extend(build_exclusions_markdown(projects))
    lines.extend(build_link_table_markdown(selected))
    lines.extend(
        [
            "## Ограничения",
            "- Категории являются рабочими гипотезами по текстовым данным, а не визуальной классификацией интерьеров.",
            "- Фото не распознавались и не интерпретировались: contact sheet нужен только для ручного просмотра.",
            "- Telegram-ссылки требуют ручной проверки, особенно если пост-источник ссылался на проектный пост через текстовую ссылку.",
            "- Цены исторические и привязаны к тексту постов; они не являются актуальными коммерческими предложениями.",
            "- Извлечение ЖК, улиц и авторов сделано регулярными правилами и может требовать ручной сверки.",
            "",
            f"_Сгенерировано: {generated_at}. Канал: @{channel_username}._",
            "",
        ]
    )
    return "\n".join(lines)


def build_example_markdown(project: KitchenProject, *, index: int, out_dir_name: str) -> list[str]:
    lines = [
        f"### {index}. {project.example_id} — {project.object_name}",
        f"- Дизайнер/хоумстейджер: {project.designer}.",
        f"- Дата поста: {format_date(project.date)}.",
    ]
    if project.area_type:
        lines.append(f"- Тип/площадь: {project.area_type}.")
    if project.city and project.city not in project.object_name:
        lines.append(f"- Город: {project.city}.")
    lines.extend(
        [
            f"- Палитра: {project.palette_summary or project.palette_category_label or 'нет классификации'}.",
            f"- Чистое резюме палитры: {project.palette_summary_clean or 'нет безопасного резюме для показа'}.",
            f"- Фасады: {project.facade_finish_raw or 'не найдено в извлечённых фактах'}.",
            f"- Столешница: {project.countertop_raw or 'не найдено в извлечённых фактах'}.",
            f"- Фартук: {project.backsplash_raw or 'не найдено в извлечённых фактах'}.",
            f"- Стены/пол: {format_wall_floor(project)}.",
            f"- Доказательство: «{short_quote(project.evidence_quotes[0] if project.evidence_quotes else '', 320)}».",
            f"- Надёжность: {QUALITY_RU.get(project.quality_tier, project.quality_tier)}; score={project.quality_score} — {project.confidence_reason}.",
            f"- Ссылка-кандидат: [{project.candidate_project_url}]({project.candidate_project_url}) ⚠ requires manual verification.",
        ]
    )
    if project.candidate_article_urls:
        lines.append(
            "- Посты-источники артикулов: "
            + "; ".join(f"[{url}]({url}) ⚠" for url in project.candidate_article_urls)
            + "."
        )
    lines.extend(format_photo_markdown(project))
    return lines


def format_photo_markdown(project: KitchenProject) -> list[str]:
    if project.contact_sheet_path:
        rel = relative_report_path(project.contact_sheet_path)
        return [f"- Contact sheet: `{rel}`.", "", f"![{project.example_id}]({rel})"]
    if project.copied_photo_paths:
        lines = ["- Фото:"]
        for path in project.copied_photo_paths:
            rel = relative_report_path(path)
            lines.append(f"  - `{rel}`")
            lines.append(f"![{project.example_id}]({rel})")
        return lines
    if project.photo_paths:
        return ["- Фото: " + "; ".join(f"`{path}`" for path in project.photo_paths[:2]) + "."]
    return ["- Фото: не найдены в локальной базе."]


def build_exclusions_markdown(projects: Sequence[KitchenProject]) -> list[str]:
    counter = Counter(project.exclusion_reason for project in projects if not project.selected_for_report)
    lines = [
        "## Что не вошло",
        "- В основной отчёт не добавлялись bundle-only/general строки без прямых фактов по фасадам, фартуку или столешнице.",
        "- Не включались дубли по одному project_post_id и проекты, которые не прошли палитровые правила.",
        "- Низкая уверенность оставлена в CSV/JSONL для аудита, но не используется как отец-facing пример.",
        "",
        "| Причина | Кол-во |",
        "|---|---:|",
    ]
    if counter:
        for reason, count in counter.most_common():
            lines.append(f"| {escape_md(reason or 'not_selected')} | {count} |")
    else:
        lines.append("| нет исключений | 0 |")
    lines.append("")
    return lines


def build_link_table_markdown(projects: Sequence[KitchenProject]) -> list[str]:
    lines = [
        "## Ссылки для проверки",
        "| example_id | project_post_id | source_message_ids | candidate URL | status |",
        "|---|---:|---|---|---|",
    ]
    for project in sorted_projects_for_output(projects):
        lines.append(
            f"| {project.example_id} | {project.project_post_id} | "
            f"{', '.join(str(value) for value in project.source_message_ids)} | "
            f"[{project.candidate_project_url}]({project.candidate_project_url}) | "
            "⚠ requires manual verification |"
        )
    lines.append("")
    return lines


def build_short_report_markdown(
    *,
    projects: Sequence[KitchenProject],
    generated_at: str,
) -> str:
    lines = [
        "# Коротко: кухонные палитры",
        "",
        "Это короткая версия для ручного просмотра: три группы, примеры и ссылки-кандидаты. Фото приложены как локальные файлы/contact sheet, без анализа изображений.",
        "",
    ]
    for category in PALETTE_CATEGORIES:
        examples = [
            project
            for project in sorted_projects_for_output(projects)
            if project.selected_for_report and project.palette_category_id == category.category_id
        ]
        lines.extend([f"## {category.label}", ""])
        if not examples:
            lines.extend(["Пока нет достаточно сильных примеров в этой группе.", ""])
            continue
        for project in examples:
            materials = "; ".join(
                value
                for value in [project.facade_finish_raw, project.countertop_raw, project.backsplash_raw]
                if value
            )
            photo_hint = relative_report_path(project.contact_sheet_path) if project.contact_sheet_path else (
                relative_report_path(project.copied_photo_paths[0]) if project.copied_photo_paths else ""
            )
            line = (
                f"- {project.example_id}: {project.object_name} — {materials or 'палитра по тексту'}; "
                f"{project.designer}; {format_date(project.date)}; "
                f"[пост]({project.candidate_project_url}) ⚠"
            )
            if photo_hint:
                line += f"; фото: `{photo_hint}`"
            lines.append(line + ".")
        lines.append("")
    lines.extend(
        [
            "Ссылки требуют ручной проверки. Категории не являются визуальным анализом: они собраны по текстовым фактам из постов.",
            "",
            f"_Сгенерировано: {generated_at}._",
            "",
        ]
    )
    return "\n".join(lines)


def build_clean_short_report_markdown(
    *,
    projects: Sequence[KitchenProject],
    generated_at: str,
) -> str:
    selected = [project for project in sorted_projects_for_output(projects) if project.selected_for_clean_report]
    lines = [
        "# Коротко: кухонные палитры Мирзабаевой — чистая версия",
        "",
        "## Как читать",
        "- Включены только сильные и средние примеры; слабые кандидаты оставлены в CSV/quality notes.",
        "- Telegram-ссылки помечены ⚠ и требуют ручной проверки.",
        "- Фото приложены механически из того же поста/серии сообщений, без анализа изображения.",
        "- Категории — гипотезы по тексту постов, а не визуальная классификация фотографий.",
        "",
    ]

    for index, category in enumerate(PALETTE_CATEGORIES, start=1):
        examples = [
            project
            for project in selected
            if project.palette_category_id == category.category_id
        ]
        lines.extend([f"## {index}. {category.label}", "", category.short_description, ""])
        if len(examples) < 6:
            lines.extend(
                [
                    f"Пока найдено {len(examples)} сильных/средних примеров.",
                    "В этой категории меньше примеров, потому что автоматически найденных сильных постов с явным сочетанием фасадов/столешницы/фартука меньше. Мы не добирали примеры шумными строками.",
                    "",
                ]
            )
        if category.category_id == "light_facade_stone_accent":
            lines.extend(["Категория требует ручной проверки по фотографиям.", ""])
        if not examples:
            lines.extend(["Сейчас нет достаточно чистых примеров для показа.", ""])
            continue
        for project in examples:
            lines.extend(build_clean_example_markdown(project))
            lines.append("")

    lines.extend(
        [
            "## Что проверить вручную",
            "- открываются ли ссылки;",
            "- действительно ли фото показывают кухню;",
            "- совпадает ли фото с извлечённым сочетанием фасадов;",
            "- дизайнер/хоумстейджер верно распознан;",
            "- не повторяется ли один и тот же проект.",
            "",
            f"_Сгенерировано: {generated_at}._",
            "",
        ]
    )
    return "\n".join(lines)


def build_clean_example_markdown(project: KitchenProject) -> list[str]:
    photo_hint = ""
    if project.contact_sheet_path:
        photo_hint = relative_report_path(project.contact_sheet_path)
    elif project.copied_photo_paths:
        photo_hint = "; ".join(relative_report_path(path) for path in project.copied_photo_paths[:2])
    elif project.photo_paths:
        photo_hint = "; ".join(project.photo_paths[:2])

    lines = [
        f"### {project.example_id} — {project.object_name}",
        f"- Уверенность: {QUALITY_RU.get(project.quality_tier, project.quality_tier)}; score={project.quality_score}.",
        f"- Дизайнер/хоумстейджер: {project.designer}.",
        f"- Дата: {format_date(project.date)}.",
        f"- Палитра: {project.palette_summary_clean or 'нет безопасного резюме'}.",
        f"- Фасады: {project.facade_finish_raw if project.has_clean_facade else 'не найдено чистое сочетание фасадов'}.",
        f"- Столешница: {project.countertop_raw or 'не найдено в извлечённых фактах'}.",
        f"- Фартук: {project.backsplash_raw or 'не найдено в извлечённых фактах'}.",
        f"- Ссылка-кандидат: [{project.candidate_project_url}]({project.candidate_project_url}) ⚠.",
    ]
    if photo_hint:
        lines.append(f"- Фото/contact sheet: `{photo_hint}`.")
    else:
        lines.append("- Фото/contact sheet: не найдено в локальной базе.")
    return lines


def build_quality_notes_markdown(
    *,
    projects: Sequence[KitchenProject],
    generated_at: str,
    kitchen_fact_count: int,
) -> str:
    selected = [project for project in projects if project.selected_for_clean_report]
    excluded = [project for project in projects if not project.selected_for_clean_report]
    reason_counts = Counter(project.exclusion_reason or "not_selected" for project in excluded)
    lines = [
        "# Kitchen palette quality notes",
        "",
        f"- Generated at: {generated_at}.",
        f"- Total kitchen facts: {kitchen_fact_count}.",
        f"- Project candidates: {len(projects)}.",
        f"- Selected clean examples: {len(selected)}.",
        f"- Excluded or downgraded examples: {len(excluded)}.",
        "- Suppression is display/report-layer only; source rows are preserved in CSV/JSONL.",
        "",
        "## Selected clean examples per category",
        "| category | selected | high | medium |",
        "|---|---:|---:|---:|",
    ]
    for category in PALETTE_CATEGORIES:
        examples = [project for project in selected if project.palette_category_id == category.category_id]
        lines.append(
            f"| {category.category_id} | {len(examples)} | "
            f"{sum(1 for project in examples if project.quality_tier == 'high')} | "
            f"{sum(1 for project in examples if project.quality_tier == 'medium')} |"
        )

    lines.extend(["", "## Exclusion reasons", "| reason | count |", "|---|---:|"])
    if reason_counts:
        for reason, count in reason_counts.most_common():
            lines.append(f"| {escape_md(reason)} | {count} |")
    else:
        lines.append("| none | 0 |")

    lines.extend(["", "## Known examples checked", "| example_id | status | score | tier | reason |", "|---|---|---:|---|---|"])
    for example_id in ("KITCHEN-6187", "KITCHEN-7161", "KITCHEN-1248", "KITCHEN-4007"):
        project = next((candidate for candidate in projects if candidate.example_id == example_id), None)
        if project is None:
            lines.append(f"| {example_id} | not present in candidate set |  |  |  |")
            continue
        status = "selected clean" if project.selected_for_clean_report else "excluded/downgraded"
        lines.append(
            f"| {project.example_id} | {status} | {project.quality_score} | "
            f"{project.quality_tier} | {escape_md(project.exclusion_reason or project.confidence_reason)} |"
        )

    lines.extend(
        [
            "",
            "## Notes",
            "- Low examples are not used in the father-facing clean report.",
            "- Medium examples are used only when a category has fewer than three high-quality examples.",
            "- Contact sheets and image paths are attached mechanically; image content is not interpreted.",
            "",
        ]
    )
    return "\n".join(lines)


def sorted_projects_for_output(projects: Sequence[KitchenProject]) -> list[KitchenProject]:
    return sorted(
        projects,
        key=lambda project: (
            int(not project.selected_for_report),
            PALETTE_ORDER.get(project.palette_category_id or "", 99),
            -CONFIDENCE_SORT_VALUE.get(project.quality_tier, 0),
            -project.quality_score,
            project.date or "",
            project.project_post_id,
        ),
    )


def telegram_post_url(channel_username: str, message_id: int) -> str:
    return f"https://t.me/{channel_username}/{message_id}"


def meaningful_lines(text: str) -> list[str]:
    return [
        compact_whitespace(line).strip(" \t\r\n•-–—")
        for line in (text or "").splitlines()
        if compact_whitespace(line).strip(" \t\r\n•-–—")
    ]


def link_context_from_text(text: str, link_text: str) -> str:
    if link_text:
        index = (text or "").find(link_text)
        if index >= 0:
            return surrounding_text(text, index, index + len(link_text))
    return truncate(text or "", 240)


def surrounding_text(text: str, start: int, end: int, *, radius: int = 160) -> str:
    left = max(0, start - radius)
    right = min(len(text or ""), end + radius)
    return compact_whitespace((text or "")[left:right])


def _load_entities(value: str | None) -> list[dict[str, Any]]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []


def normalize_for_match(value: str) -> str:
    return compact_whitespace(str(value).casefold().replace("ё", "е"))


def contains_any(text: str, tokens: Iterable[str]) -> bool:
    return any(token in text for token in tokens)


def unique_preserve_order(values: Iterable[str | None]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _clean(value)
        if not cleaned:
            continue
        key = normalize_for_match(cleaned)
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
    return result


def first_non_empty(values: Iterable[str | None]) -> str | None:
    for value in values:
        cleaned = _clean(value)
        if cleaned:
            return cleaned
    return None


def readable_item_type(value: str) -> str:
    labels = {
        "kitchen_facades": "фасады",
        "countertop": "столешница",
        "backsplash": "фартук",
        "kitchen_purchase": "кухня",
        "bundle_purchase": "набор",
        "kitchen_other": "кухня/прочее",
        "kitchen_accessory": "аксессуар",
    }
    return labels.get(value, value.replace("_", " "))


def format_wall_floor(project: KitchenProject) -> str:
    values = []
    if project.wall_color:
        values.append(f"стены: {project.wall_color}")
    if project.flooring:
        values.append(f"пол: {project.flooring}")
    return "; ".join(values) if values else "не найдено в извлечённых фактах"


def format_date(value: str | None) -> str:
    return (value or "нет даты").split("T", 1)[0]


def short_quote(value: str, limit: int) -> str:
    return truncate(compact_whitespace(value or ""), limit)


def escape_md(value: Any) -> str:
    return compact_whitespace(str(value)).replace("\\", "\\\\").replace("|", "\\|")


def relative_report_path(value: str | None) -> str:
    if not value:
        return ""
    path = Path(value)
    parts = path.parts
    if "kitchen_palette_report" in parts:
        index = parts.index("kitchen_palette_report")
        return str(Path(*parts[index + 1 :])).replace("\\", "/")
    return str(path).replace("\\", "/")


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = compact_whitespace(str(value)).strip()
    return text or None
