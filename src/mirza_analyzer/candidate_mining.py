from __future__ import annotations

import csv
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal

from .categories import (
    PROJECT_ARTICLE_REGEXES,
    PROJECT_ARTICLE_TERMS,
    CategoryConfig,
    load_category_configs,
)
from .db import connect, scalar_count
from .utils import compact_whitespace, json_dumps, truncate, utc_now_iso


OutputFormat = Literal["markdown", "csv", "jsonl", "all"]

WORD_CHARS = "0-9A-Za-zА-Яа-яЁё"
CONFIDENCE_SORT_VALUE = {"low": 0, "medium": 1, "high": 2}


@dataclass(frozen=True)
class CompiledCategory:
    config: CategoryConfig
    strong_patterns: list[tuple[str, re.Pattern[str]]]
    weak_patterns: list[tuple[str, re.Pattern[str]]]
    regex_patterns: list[tuple[str, re.Pattern[str]]]
    negative_patterns: list[tuple[str, re.Pattern[str]]]


@dataclass(frozen=True)
class ProjectArticleDetection:
    is_project_article: bool
    project_article_score: int
    detected_article_terms: list[str]


@dataclass(frozen=True)
class CategoryTextMatch:
    category_id: str
    score: int
    confidence_level: str
    matched_strong_terms: list[str]
    matched_weak_terms: list[str]
    matched_regexes: list[str]
    negative_terms: list[str]
    max_confidence_reason: str | None = None


@dataclass(frozen=True)
class SourcePost:
    telegram_message_id: int
    date: str | None
    text_plain: str
    text_char_count: int
    source_variant_count: int
    canonical_has_real_photo: bool
    canonical_has_real_file: bool
    photo_count: int
    first_photo_paths: list[str]


@dataclass(frozen=True)
class CategoryCandidate:
    category_id: str
    category_display_name: str
    telegram_message_id: int
    date: str | None
    score: int
    confidence_level: str
    matched_strong_terms: list[str]
    matched_weak_terms: list[str]
    matched_regexes: list[str]
    negative_terms: list[str]
    text_excerpt: str
    full_text: str
    photo_count: int
    first_photo_paths: list[str]
    is_project_article: bool
    project_article_score: int
    detected_article_terms: list[str]
    source_flags: dict[str, Any]

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "category_id": self.category_id,
            "category_display_name": self.category_display_name,
            "telegram_message_id": self.telegram_message_id,
            "date": self.date,
            "score": self.score,
            "confidence_level": self.confidence_level,
            "matched_strong_terms": self.matched_strong_terms,
            "matched_weak_terms": self.matched_weak_terms,
            "matched_regexes": self.matched_regexes,
            "negative_terms": self.negative_terms,
            "text_excerpt": self.text_excerpt,
            "full_text": self.full_text,
            "photo_count": self.photo_count,
            "first_photo_paths": self.first_photo_paths,
            "is_project_article": self.is_project_article,
            "project_article_score": self.project_article_score,
            "detected_article_terms": self.detected_article_terms,
            "source_flags": self.source_flags,
        }


@dataclass(frozen=True)
class ProjectArticlePost:
    telegram_message_id: int
    date: str | None
    project_article_score: int
    detected_article_terms: list[str]
    text_excerpt: str
    photo_count: int
    first_photo_paths: list[str]


@dataclass(frozen=True)
class CandidateRunResult:
    db_path: Path
    out_dir: Path
    generated_at: str
    total_text_posts: int
    total_posts_with_photos: int
    candidates: list[CategoryCandidate]
    candidates_by_category: dict[str, list[CategoryCandidate]]
    project_article_posts: list[ProjectArticlePost]
    output_files: list[Path]


def normalize_for_matching(value: str) -> str:
    return compact_whitespace(value.casefold().replace("ё", "е"))


def keyword_to_regex(keyword: str) -> str:
    normalized = normalize_for_matching(keyword)
    pieces = [re.escape(piece) for piece in re.split(r"\s+", normalized) if piece]
    body = r"[\s-]+".join(pieces)
    return rf"(?<![{WORD_CHARS}]){body}(?![{WORD_CHARS}])"


def normalize_regex_pattern(pattern: str) -> str:
    return pattern.casefold().replace("ё", "е")


def compile_keyword_patterns(keywords: Iterable[str]) -> list[tuple[str, re.Pattern[str]]]:
    return [
        (keyword, re.compile(keyword_to_regex(keyword), re.IGNORECASE))
        for keyword in keywords
    ]


def compile_regex_patterns(patterns: Iterable[str]) -> list[tuple[str, re.Pattern[str]]]:
    return [
        (pattern, re.compile(normalize_regex_pattern(pattern), re.IGNORECASE))
        for pattern in patterns
    ]


def compile_category(config: CategoryConfig) -> CompiledCategory:
    return CompiledCategory(
        config=config,
        strong_patterns=compile_keyword_patterns(config.strong_keywords),
        weak_patterns=compile_keyword_patterns(config.weak_keywords),
        regex_patterns=compile_regex_patterns(config.regex_patterns),
        negative_patterns=compile_regex_patterns(config.negative_patterns),
    )


def compiled_categories() -> list[CompiledCategory]:
    return [compile_category(config) for config in load_category_configs()]


def compiled_category_by_id(category_id: str) -> CompiledCategory:
    for category in compiled_categories():
        if category.config.category_id == category_id:
            return category
    raise KeyError(f"Unknown category id: {category_id}")


def detect_project_article(text: str) -> ProjectArticleDetection:
    text_for_matching = normalize_for_matching(text)
    detected_terms = match_named_patterns(
        text_for_matching,
        compile_keyword_patterns(PROJECT_ARTICLE_TERMS),
    )
    detected_terms.extend(
        match_named_patterns(
            text_for_matching,
            compile_regex_patterns(PROJECT_ARTICLE_REGEXES),
        )
    )
    detected_terms = sorted(set(detected_terms), key=detected_terms.index)
    score = len(detected_terms) * 2
    return ProjectArticleDetection(
        is_project_article=bool(detected_terms),
        project_article_score=score,
        detected_article_terms=detected_terms,
    )


def score_category_text(
    category_id: str,
    text: str,
    *,
    photo_count: int = 0,
    project_detection: ProjectArticleDetection | None = None,
) -> CategoryTextMatch | None:
    return score_compiled_category_text(
        compiled_category_by_id(category_id),
        text,
        photo_count=photo_count,
        project_detection=project_detection,
    )


def score_compiled_category_text(
    category: CompiledCategory,
    text: str,
    *,
    photo_count: int,
    project_detection: ProjectArticleDetection | None,
) -> CategoryTextMatch | None:
    text_for_matching = normalize_for_matching(text)
    if not text_for_matching:
        return None

    matched_strong_terms = match_named_patterns(text_for_matching, category.strong_patterns)
    matched_weak_terms = match_named_patterns(text_for_matching, category.weak_patterns)
    matched_regexes = match_named_patterns(text_for_matching, category.regex_patterns)
    negative_terms = match_named_patterns(text_for_matching, category.negative_patterns)

    if not (matched_strong_terms or matched_weak_terms or matched_regexes):
        return None

    score = (
        len(matched_strong_terms) * 3
        + len(matched_regexes) * 4
        + len(matched_weak_terms)
    )
    if project_detection and project_detection.is_project_article:
        score += 2
    if photo_count > 0:
        score += 1
    if negative_terms:
        score -= len(negative_terms) * 2

    suppression = category_specific_adjustment(
        category.config.category_id,
        matched_strong_terms,
        matched_weak_terms,
        matched_regexes,
        text_for_matching,
    )
    if suppression["suppress"]:
        return None

    if score <= 0:
        return None

    confidence_level = confidence_from_score(score)
    max_confidence = suppression["max_confidence"]
    if max_confidence is not None:
        confidence_level = min_confidence(confidence_level, max_confidence)

    return CategoryTextMatch(
        category_id=category.config.category_id,
        score=score,
        confidence_level=confidence_level,
        matched_strong_terms=matched_strong_terms,
        matched_weak_terms=matched_weak_terms,
        matched_regexes=matched_regexes,
        negative_terms=negative_terms,
        max_confidence_reason=suppression["reason"],
    )


def match_named_patterns(
    text_for_matching: str,
    named_patterns: Iterable[tuple[str, re.Pattern[str]]],
) -> list[str]:
    matches: list[str] = []
    for name, pattern in named_patterns:
        if pattern.search(text_for_matching):
            matches.append(name)
    return matches


def category_specific_adjustment(
    category_id: str,
    matched_strong_terms: list[str],
    matched_weak_terms: list[str],
    matched_regexes: list[str],
    text_for_matching: str,
) -> dict[str, str | bool | None]:
    if category_id == "flooring":
        if not matched_strong_terms and not matched_regexes and set(matched_weak_terms) <= {"пол"}:
            return {
                "suppress": True,
                "max_confidence": None,
                "reason": "weak 'пол' alone is too broad for flooring",
            }

    if category_id == "hallway":
        if not matched_strong_terms and not matched_regexes and set(matched_weak_terms) <= {
            "шкаф",
            "зеркало",
        }:
            return {
                "suppress": False,
                "max_confidence": "low",
                "reason": "generic hallway weak term without hallway context",
            }

    if category_id == "living_room_furniture":
        if not matched_strong_terms and not matched_regexes and set(matched_weak_terms) <= {
            "тумба",
            "шкаф",
            "полка",
        }:
            return {
                "suppress": False,
                "max_confidence": "low",
                "reason": "generic living-room furniture weak term without room context",
            }

    if category_id == "tables":
        kitchen_context = re.search(r"\b(кухн[а-яе]*|фасад[а-яе]*|фартук[а-яе]*)\b", text_for_matching)
        if set(matched_strong_terms) <= {"столешница"} and not matched_regexes and kitchen_context:
            return {
                "suppress": False,
                "max_confidence": "low",
                "reason": "lone tabletop term inside kitchen context",
            }

    return {"suppress": False, "max_confidence": None, "reason": None}


def confidence_from_score(score: int) -> str:
    if score >= 7:
        return "high"
    if score >= 4:
        return "medium"
    return "low"


def min_confidence(confidence: str, max_confidence: str) -> str:
    if CONFIDENCE_SORT_VALUE[confidence] <= CONFIDENCE_SORT_VALUE[max_confidence]:
        return confidence
    return max_confidence


def mine_candidates(
    db_path: Path,
    *,
    limit_per_category: int = 100,
    min_score: int = 2,
    include_low_confidence: bool = False,
    photos_per_post: int = 3,
) -> CandidateRunResult:
    generated_at = utc_now_iso()
    posts, total_text_posts, total_posts_with_photos = load_source_posts(db_path, photos_per_post)
    categories = compiled_categories()
    candidates_by_category: dict[str, list[CategoryCandidate]] = {
        category.config.category_id: [] for category in categories
    }
    project_article_posts: list[ProjectArticlePost] = []

    for post in posts:
        project_detection = detect_project_article(post.text_plain)
        if project_detection.is_project_article:
            project_article_posts.append(
                ProjectArticlePost(
                    telegram_message_id=post.telegram_message_id,
                    date=post.date,
                    project_article_score=project_detection.project_article_score,
                    detected_article_terms=project_detection.detected_article_terms,
                    text_excerpt=excerpt_around_match(
                        post.text_plain,
                        project_detection.detected_article_terms,
                        [],
                    ),
                    photo_count=post.photo_count,
                    first_photo_paths=post.first_photo_paths,
                )
            )

        for category in categories:
            match = score_compiled_category_text(
                category,
                post.text_plain,
                photo_count=post.photo_count,
                project_detection=project_detection,
            )
            if match is None:
                continue
            if match.score < min_score:
                continue
            if match.confidence_level == "low" and not include_low_confidence:
                continue
            candidates_by_category[category.config.category_id].append(
                build_candidate(category.config, post, match, project_detection)
            )

    limited_by_category: dict[str, list[CategoryCandidate]] = {}
    for category in categories:
        category_id = category.config.category_id
        sorted_candidates = sort_candidates(candidates_by_category[category_id])
        limited_by_category[category_id] = sorted_candidates[:limit_per_category]

    candidates = [
        candidate
        for category in categories
        for candidate in limited_by_category[category.config.category_id]
    ]
    project_article_posts = sorted(
        project_article_posts,
        key=lambda post: (
            post.project_article_score,
            post.date or "",
            post.telegram_message_id,
        ),
        reverse=True,
    )

    return CandidateRunResult(
        db_path=db_path,
        out_dir=Path(),
        generated_at=generated_at,
        total_text_posts=total_text_posts,
        total_posts_with_photos=total_posts_with_photos,
        candidates=candidates,
        candidates_by_category=limited_by_category,
        project_article_posts=project_article_posts,
        output_files=[],
    )


def load_source_posts(
    db_path: Path,
    photos_per_post: int,
) -> tuple[list[SourcePost], int, int]:
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
                telegram_message_id,
                date,
                text_plain,
                text_char_count,
                source_variant_count,
                has_real_photo,
                has_real_file
            FROM canonical_messages
            ORDER BY telegram_message_id
            """
        ).fetchall()

        photo_counts: Counter[int] = Counter()
        photo_paths: defaultdict[int, list[str]] = defaultdict(list)
        for row in conn.execute(
            """
            SELECT telegram_message_id, absolute_path
            FROM media
            WHERE media_kind = 'photo'
            ORDER BY telegram_message_id, id
            """
        ):
            telegram_message_id = int(row["telegram_message_id"])
            photo_counts[telegram_message_id] += 1
            if len(photo_paths[telegram_message_id]) < photos_per_post:
                photo_paths[telegram_message_id].append(str(row["absolute_path"]))

        posts = [
            SourcePost(
                telegram_message_id=int(row["telegram_message_id"]),
                date=row["date"],
                text_plain=row["text_plain"] or "",
                text_char_count=int(row["text_char_count"] or 0),
                source_variant_count=int(row["source_variant_count"] or 0),
                canonical_has_real_photo=bool(row["has_real_photo"]),
                canonical_has_real_file=bool(row["has_real_file"]),
                photo_count=int(photo_counts[int(row["telegram_message_id"])]),
                first_photo_paths=photo_paths[int(row["telegram_message_id"])],
            )
            for row in rows
        ]
        total_text_posts = scalar_count(
            conn,
            "SELECT COUNT(*) FROM canonical_messages WHERE trim(text_plain) <> ''",
        )
        total_posts_with_photos = scalar_count(
            conn,
            "SELECT COUNT(DISTINCT telegram_message_id) FROM media WHERE media_kind = 'photo'",
        )

    return posts, total_text_posts, total_posts_with_photos


def build_candidate(
    config: CategoryConfig,
    post: SourcePost,
    match: CategoryTextMatch,
    project_detection: ProjectArticleDetection,
) -> CategoryCandidate:
    return CategoryCandidate(
        category_id=config.category_id,
        category_display_name=config.display_name,
        telegram_message_id=post.telegram_message_id,
        date=post.date,
        score=match.score,
        confidence_level=match.confidence_level,
        matched_strong_terms=match.matched_strong_terms,
        matched_weak_terms=match.matched_weak_terms,
        matched_regexes=match.matched_regexes,
        negative_terms=match.negative_terms,
        text_excerpt=excerpt_around_match(
            post.text_plain,
            match.matched_strong_terms + match.matched_weak_terms,
            match.matched_regexes,
        ),
        full_text=post.text_plain,
        photo_count=post.photo_count,
        first_photo_paths=post.first_photo_paths,
        is_project_article=project_detection.is_project_article,
        project_article_score=project_detection.project_article_score,
        detected_article_terms=project_detection.detected_article_terms,
        source_flags={
            "has_text": bool(post.text_plain.strip()),
            "canonical_has_real_photo": post.canonical_has_real_photo,
            "canonical_has_real_file": post.canonical_has_real_file,
            "source_variant_count": post.source_variant_count,
            "text_char_count": post.text_char_count,
        },
    )


def sort_candidates(candidates: list[CategoryCandidate]) -> list[CategoryCandidate]:
    return sorted(
        candidates,
        key=lambda candidate: (
            CONFIDENCE_SORT_VALUE[candidate.confidence_level],
            candidate.is_project_article,
            candidate.score,
            candidate.date or "",
            candidate.telegram_message_id,
        ),
        reverse=True,
    )


def excerpt_around_match(
    text: str,
    matched_terms: list[str],
    matched_regexes: list[str],
    *,
    max_chars: int = 500,
) -> str:
    compact = compact_whitespace(text)
    if not compact:
        return ""

    normalized = normalize_for_matching(compact)
    match_positions: list[int] = []
    for term in matched_terms:
        normalized_term = normalize_for_matching(term)
        index = normalized.find(normalized_term)
        if index >= 0:
            match_positions.append(index)

    for regex in matched_regexes:
        try:
            match = re.search(normalize_regex_pattern(regex), normalized, re.IGNORECASE)
        except re.error:
            match = None
        if match:
            match_positions.append(match.start())

    if not match_positions:
        return truncate(compact, max_chars)

    center = min(match_positions)
    half = max_chars // 2
    start = max(0, center - half)
    end = min(len(compact), start + max_chars)
    start = max(0, end - max_chars)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(compact) else ""
    return f"{prefix}{compact[start:end].strip()}{suffix}"


def write_candidate_outputs(
    db_path: Path,
    out_dir: Path,
    *,
    limit_per_category: int = 100,
    min_score: int = 2,
    include_low_confidence: bool = False,
    photos_per_post: int = 3,
    output_format: OutputFormat = "all",
) -> CandidateRunResult:
    out_dir.mkdir(parents=True, exist_ok=True)
    result = mine_candidates(
        db_path,
        limit_per_category=limit_per_category,
        min_score=min_score,
        include_low_confidence=include_low_confidence,
        photos_per_post=photos_per_post,
    )
    output_files: list[Path] = []

    if output_format in {"markdown", "all"}:
        output_files.extend(write_markdown_outputs(result, out_dir))
    if output_format in {"csv", "all"}:
        csv_path = out_dir / "candidates.csv"
        write_candidates_csv(result.candidates, csv_path)
        output_files.append(csv_path)
    if output_format in {"jsonl", "all"}:
        jsonl_path = out_dir / "candidates.jsonl"
        write_candidates_jsonl(result.candidates, jsonl_path)
        output_files.append(jsonl_path)

    return CandidateRunResult(
        db_path=result.db_path,
        out_dir=out_dir,
        generated_at=result.generated_at,
        total_text_posts=result.total_text_posts,
        total_posts_with_photos=result.total_posts_with_photos,
        candidates=result.candidates,
        candidates_by_category=result.candidates_by_category,
        project_article_posts=result.project_article_posts,
        output_files=output_files,
    )


def write_markdown_outputs(result: CandidateRunResult, out_dir: Path) -> list[Path]:
    output_files: list[Path] = []
    categories = load_category_configs()
    category_map = {category.category_id: category for category in categories}

    summary_path = out_dir / "summary.md"
    summary_path.write_text(build_summary_markdown(result, categories), encoding="utf-8")
    output_files.append(summary_path)

    for category in categories:
        path = out_dir / f"{category.category_id}.md"
        candidates = result.candidates_by_category.get(category.category_id, [])
        path.write_text(build_category_markdown(category, candidates), encoding="utf-8")
        output_files.append(path)

    project_path = out_dir / "project_article_posts.md"
    project_path.write_text(build_project_article_markdown(result.project_article_posts), encoding="utf-8")
    output_files.append(project_path)

    missing_categories = set(result.candidates_by_category) - set(category_map)
    if missing_categories:
        raise RuntimeError(f"Unknown category ids in result: {sorted(missing_categories)}")

    return output_files


def build_summary_markdown(result: CandidateRunResult, categories: list[CategoryConfig]) -> str:
    lines: list[str] = []
    lines.append("# Category Candidate Mining Summary")
    lines.append("")
    lines.append(f"- Database: `{result.db_path.resolve()}`")
    lines.append(f"- Generated at: `{result.generated_at}`")
    lines.append(f"- Total text posts: {result.total_text_posts}")
    lines.append(f"- Total posts with real photos: {result.total_posts_with_photos}")
    lines.append(f"- Candidate rows written: {len(result.candidates)}")
    lines.append(f"- Project/article posts detected: {len(result.project_article_posts)}")
    lines.append("")
    lines.append(
        "This is deterministic candidate mining for manual review only. "
        "It does not use LLMs, OCR, VLMs, OpenRouter, or produce final renovation recommendations."
    )
    lines.append("")
    lines.append("## Counts by Category")
    lines.append("")
    lines.append("| Category | Candidates | High | Medium | Low | With photo | Project/article overlap |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for category in categories:
        candidates = result.candidates_by_category.get(category.category_id, [])
        confidence_counts = Counter(candidate.confidence_level for candidate in candidates)
        with_photo = sum(1 for candidate in candidates if candidate.photo_count > 0)
        project_overlap = sum(1 for candidate in candidates if candidate.is_project_article)
        lines.append(
            "| "
            f"{category.display_name} (`{category.category_id}`) | "
            f"{len(candidates)} | "
            f"{confidence_counts['high']} | "
            f"{confidence_counts['medium']} | "
            f"{confidence_counts['low']} | "
            f"{with_photo} | "
            f"{project_overlap} |"
        )
    lines.append("")
    lines.append("## Top Matched Terms")
    lines.append("")
    for category in categories:
        candidates = result.candidates_by_category.get(category.category_id, [])
        term_counts: Counter[str] = Counter()
        for candidate in candidates:
            term_counts.update(candidate.matched_strong_terms)
            term_counts.update(candidate.matched_weak_terms)
            term_counts.update(f"regex:{regex}" for regex in candidate.matched_regexes)
        top_terms = ", ".join(f"`{term}` ({count})" for term, count in term_counts.most_common(12))
        lines.append(f"- **{category.display_name}**: {top_terms or 'no matched terms'}")
    lines.append("")
    return "\n".join(lines)


def build_category_markdown(category: CategoryConfig, candidates: list[CategoryCandidate]) -> str:
    lines: list[str] = []
    lines.append(f"# {category.display_name}: Candidate Posts")
    lines.append("")
    lines.append(f"- Category ID: `{category.category_id}`")
    lines.append(f"- Description: {category.description}")
    lines.append(f"- Strong keywords: {format_inline_values(category.strong_keywords)}")
    lines.append(f"- Weak keywords: {format_inline_values(category.weak_keywords)}")
    lines.append(f"- Regex patterns: {format_inline_values(category.regex_patterns)}")
    lines.append(f"- Candidate count: {len(candidates)}")
    lines.append("")

    if not candidates:
        lines.append("No candidates matched the current threshold.")
        lines.append("")
        return "\n".join(lines)

    for candidate in candidates:
        lines.append(f"## Message {candidate.telegram_message_id}")
        lines.append("")
        lines.append(f"- Date: `{candidate.date or ''}`")
        lines.append(f"- Score: {candidate.score}")
        lines.append(f"- Confidence level: `{candidate.confidence_level}`")
        lines.append(f"- Matched strong terms: {format_inline_values(candidate.matched_strong_terms)}")
        lines.append(f"- Matched weak terms: {format_inline_values(candidate.matched_weak_terms)}")
        lines.append(f"- Matched regexes: {format_inline_values(candidate.matched_regexes)}")
        lines.append(f"- Negative terms: {format_inline_values(candidate.negative_terms)}")
        lines.append(f"- Project/article: {candidate.is_project_article}")
        if candidate.detected_article_terms:
            lines.append(
                f"- Project/article terms: {format_inline_values(candidate.detected_article_terms)}"
            )
        lines.append(f"- Photo count: {candidate.photo_count}")
        if candidate.first_photo_paths:
            lines.append("- Photo paths:")
            for path in candidate.first_photo_paths:
                lines.append(f"  - `{path}`")
        else:
            lines.append("- Photo paths: none")
        lines.append("- Text excerpt:")
        lines.append("")
        lines.append(f"> {candidate.text_excerpt or '[empty]'}")
        lines.append("")
        lines.append("- Source note: candidate only, needs review")
        lines.append("")

    return "\n".join(lines)


def build_project_article_markdown(project_posts: list[ProjectArticlePost]) -> str:
    lines: list[str] = []
    lines.append("# Project / Article Posts")
    lines.append("")
    lines.append(
        "These posts matched project/article purchase-list phrases. "
        "They are listed even when they do not strongly match one Stage 1 category."
    )
    lines.append("")
    lines.append(f"- Count: {len(project_posts)}")
    lines.append("")

    for post in project_posts:
        lines.append(f"## Message {post.telegram_message_id}")
        lines.append("")
        lines.append(f"- Date: `{post.date or ''}`")
        lines.append(f"- Project/article score: {post.project_article_score}")
        lines.append(f"- Detected terms: {format_inline_values(post.detected_article_terms)}")
        lines.append(f"- Photo count: {post.photo_count}")
        if post.first_photo_paths:
            lines.append("- Photo paths:")
            for path in post.first_photo_paths:
                lines.append(f"  - `{path}`")
        else:
            lines.append("- Photo paths: none")
        lines.append("- Text excerpt:")
        lines.append("")
        lines.append(f"> {post.text_excerpt or '[empty]'}")
        lines.append("")

    return "\n".join(lines)


def write_candidates_csv(candidates: list[CategoryCandidate], path: Path) -> None:
    fieldnames = [
        "category_id",
        "telegram_message_id",
        "date",
        "score",
        "confidence_level",
        "is_project_article",
        "matched_strong_terms",
        "matched_weak_terms",
        "matched_regexes",
        "negative_terms",
        "photo_count",
        "first_photo_path",
        "text_excerpt",
    ]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for candidate in candidates:
            writer.writerow(
                {
                    "category_id": candidate.category_id,
                    "telegram_message_id": candidate.telegram_message_id,
                    "date": candidate.date or "",
                    "score": candidate.score,
                    "confidence_level": candidate.confidence_level,
                    "is_project_article": int(candidate.is_project_article),
                    "matched_strong_terms": join_values(candidate.matched_strong_terms),
                    "matched_weak_terms": join_values(candidate.matched_weak_terms),
                    "matched_regexes": join_values(candidate.matched_regexes),
                    "negative_terms": join_values(candidate.negative_terms),
                    "photo_count": candidate.photo_count,
                    "first_photo_path": candidate.first_photo_paths[0]
                    if candidate.first_photo_paths
                    else "",
                    "text_excerpt": candidate.text_excerpt,
                }
            )


def write_candidates_jsonl(candidates: list[CategoryCandidate], path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for candidate in candidates:
            fh.write(json_dumps(candidate.to_json_dict()))
            fh.write("\n")


def format_inline_values(values: list[str]) -> str:
    if not values:
        return "none"
    return ", ".join(f"`{value}`" for value in values)


def join_values(values: list[str]) -> str:
    return "; ".join(values)

