from __future__ import annotations

import argparse
from pathlib import Path

from .audit import write_audit_report
from .candidate_mining import write_candidate_outputs
from .db import create_database_from_data_root, database_stats
from .extraction import extract_facts
from .father_report import DEFAULT_REPORT_TITLE, build_father_report
from .kitchen_palette_report import build_kitchen_palette_report
from .llm_providers import LLMProviderError
from .llm_review import run_llm_review
from .sample import write_sample_posts


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    result = args.func(args)
    return int(result) if isinstance(result, int) else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mirza_analyzer",
        description="Local Telegram Desktop export audit and ingestion foundation.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    audit_parser = subparsers.add_parser("audit", help="Audit Telegram Desktop exports.")
    audit_parser.add_argument("--data-root", required=True, type=Path)
    audit_parser.add_argument("--out", required=True, type=Path)
    audit_parser.set_defaults(func=run_audit)

    ingest_parser = subparsers.add_parser("ingest", help="Create a canonical SQLite database.")
    ingest_parser.add_argument("--data-root", required=True, type=Path)
    ingest_parser.add_argument("--db", required=True, type=Path)
    ingest_parser.set_defaults(func=run_ingest)

    sample_parser = subparsers.add_parser("sample", help="Write a manual-review sample.")
    sample_parser.add_argument("--db", required=True, type=Path)
    sample_parser.add_argument("--out", required=True, type=Path)
    sample_parser.add_argument("--limit", type=int, default=50)
    sample_parser.set_defaults(func=run_sample)

    stats_parser = subparsers.add_parser("stats", help="Print SQLite ingestion statistics.")
    stats_parser.add_argument("--db", required=True, type=Path)
    stats_parser.set_defaults(func=run_stats)

    candidates_parser = subparsers.add_parser(
        "candidates",
        help="Mine deterministic category candidates for manual review.",
    )
    candidates_parser.add_argument("--db", required=True, type=Path)
    candidates_parser.add_argument("--out-dir", required=True, type=Path)
    candidates_parser.add_argument("--limit-per-category", type=int, default=100)
    candidates_parser.add_argument("--min-score", type=int, default=2)
    candidates_parser.add_argument("--include-low-confidence", action="store_true")
    candidates_parser.add_argument("--photos-per-post", type=int, default=3)
    candidates_parser.add_argument(
        "--format",
        choices=["markdown", "csv", "jsonl", "all"],
        default="all",
    )
    candidates_parser.set_defaults(func=run_candidates)

    extract_parser = subparsers.add_parser(
        "extract-facts",
        help="Extract deterministic structured facts from canonical Telegram text.",
    )
    extract_parser.add_argument("--db", required=True, type=Path)
    extract_parser.add_argument("--out-dir", required=True, type=Path)
    extract_parser.add_argument(
        "--source",
        choices=["project_articles", "candidates", "all_text"],
        default="project_articles",
    )
    extract_parser.add_argument("--limit", type=int)
    extract_parser.add_argument("--min-project-article-score", type=int, default=2)
    extract_parser.add_argument(
        "--include-needs-review",
        action="store_true",
        help="Deprecated no-op: all rows are now always written, with clean/review split files.",
    )
    extract_parser.add_argument(
        "--format",
        choices=["markdown", "csv", "jsonl", "sqlite", "all"],
        default="all",
    )
    extract_parser.set_defaults(func=run_extract_facts)

    llm_review_parser = subparsers.add_parser(
        "llm-review",
        help="Stage 2.5: review deterministic facts with an LLM (LM Studio or mock).",
    )
    llm_review_parser.add_argument("--facts-db", required=True, type=Path)
    llm_review_parser.add_argument("--out-dir", required=True, type=Path)
    llm_review_parser.add_argument(
        "--provider",
        choices=["mock", "lmstudio"],
        default="lmstudio",
    )
    llm_review_parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:1234/v1",
    )
    llm_review_parser.add_argument("--model", default="local-model")
    llm_review_parser.add_argument(
        "--source",
        choices=["clean", "needs_review", "mixed", "category"],
        default="mixed",
    )
    llm_review_parser.add_argument("--category", default=None)
    llm_review_parser.add_argument("--sample-size", type=int, default=100)
    llm_review_parser.add_argument("--seed", type=int, default=42)
    llm_review_parser.add_argument("--max-evidence-chars", type=int, default=2500)
    llm_review_parser.add_argument("--temperature", type=float, default=0.0)
    llm_review_parser.add_argument("--dry-run", action="store_true")
    llm_review_parser.add_argument("--limit", type=int, default=None)
    llm_review_parser.add_argument("--resume", action="store_true")
    llm_review_parser.add_argument("--timeout-seconds", type=float, default=120.0)
    llm_review_parser.add_argument(
        "--strict-json",
        dest="strict_json",
        action="store_true",
        default=True,
    )
    llm_review_parser.add_argument(
        "--no-strict-json",
        dest="strict_json",
        action="store_false",
    )
    llm_review_parser.add_argument(
        "--canonical-db",
        type=Path,
        default=None,
        help="Optional canonical SQLite (outputs/mirza.sqlite) for source post excerpts.",
    )
    llm_review_parser.set_defaults(func=run_llm_review_command)

    father_report_parser = subparsers.add_parser(
        "father-report",
        help="Stage 3: write a father-facing Markdown renovation cheat sheet.",
    )
    father_report_parser.add_argument("--facts-db", required=True, type=Path)
    father_report_parser.add_argument("--out-dir", required=True, type=Path)
    father_report_parser.add_argument(
        "--llm-review-db",
        action="append",
        type=Path,
        default=[],
        help="Optional Stage 2.5 LLM review SQLite DB. Can be repeated.",
    )
    father_report_parser.add_argument(
        "--canonical-db",
        type=Path,
        default=Path("outputs/mirza.sqlite"),
    )
    father_report_parser.add_argument(
        "--format",
        choices=["markdown"],
        default="markdown",
    )
    father_report_parser.add_argument(
        "--min-confidence",
        choices=["low", "medium", "high"],
        default="medium",
    )
    father_report_parser.add_argument("--include-low-confidence", action="store_true")
    father_report_parser.add_argument("--max-examples-per-section", type=int, default=12)
    father_report_parser.add_argument("--max-top-values", type=int, default=15)
    father_report_parser.add_argument("--strict", dest="strict", action="store_true", default=True)
    father_report_parser.add_argument("--no-strict", dest="strict", action="store_false")
    father_report_parser.add_argument(
        "--include-appendix",
        dest="include_appendix",
        action="store_true",
        default=True,
    )
    father_report_parser.add_argument("--no-appendix", dest="include_appendix", action="store_false")
    father_report_parser.add_argument("--report-title", default=DEFAULT_REPORT_TITLE)
    father_report_parser.add_argument(
        "--generated-note",
        dest="generated_note",
        action="store_true",
        default=True,
    )
    father_report_parser.add_argument("--no-generated-note", dest="generated_note", action="store_false")
    father_report_parser.add_argument("--language", default="ru")
    father_report_parser.set_defaults(func=run_father_report_command)

    kitchen_palette_parser = subparsers.add_parser(
        "kitchen-palette-report",
        help="Stage 4: write an evidence-based kitchen palette Markdown report.",
    )
    kitchen_palette_parser.add_argument("--facts-db", required=True, type=Path)
    kitchen_palette_parser.add_argument("--canonical-db", required=True, type=Path)
    kitchen_palette_parser.add_argument("--out-dir", required=True, type=Path)
    kitchen_palette_parser.add_argument("--channel-username", required=True)
    kitchen_palette_parser.add_argument("--examples-per-category", type=int, default=6)
    kitchen_palette_parser.add_argument("--photos-per-example", type=int, default=2)
    kitchen_palette_parser.add_argument(
        "--format",
        choices=["markdown"],
        default="markdown",
    )
    kitchen_palette_parser.set_defaults(func=run_kitchen_palette_report_command)

    return parser


def run_audit(args: argparse.Namespace) -> None:
    write_audit_report(args.data_root, args.out)
    print(f"Wrote audit report: {args.out.resolve()}")


def run_ingest(args: argparse.Namespace) -> None:
    result = create_database_from_data_root(args.data_root, args.db)
    print(f"Wrote SQLite database: {result.db_path.resolve()}")
    print(f"Exports imported: {result.export_count}")
    print(f"Invalid exports skipped: {result.invalid_export_count}")
    print(f"Canonical messages: {result.canonical_message_count}")
    print(f"Source variants: {result.source_variant_count}")
    print(f"Media files: {result.media_count}")
    print(f"FTS5 enabled: {result.fts_enabled}")


def run_sample(args: argparse.Namespace) -> None:
    write_sample_posts(args.db, args.out, args.limit)
    print(f"Wrote sample posts: {args.out.resolve()}")


def run_stats(args: argparse.Namespace) -> None:
    stats = database_stats(args.db)
    print(f"Canonical posts: {stats['canonical_posts']}")
    print(f"Source message variants: {stats['source_variants']}")
    print(f"Media files: {stats['media_files']}")
    print(f"Posts with text: {stats['posts_with_text']}")
    print(f"Posts with photos: {stats['posts_with_photos']}")
    print(f"Posts with files: {stats['posts_with_files']}")
    print(f"Date range: {stats['date_min']} -> {stats['date_max']}")
    print("Top file extensions:")
    for extension, count in stats["top_extensions"]:
        print(f"  {extension}: {count}")
    print(f"Reactions records: {stats['reactions_records']}")
    print(f"Reply-to records: {stats['reply_to_message_id_records']}")
    print(f"Forwarded records: {stats['forwarded_records']}")


def run_candidates(args: argparse.Namespace) -> None:
    result = write_candidate_outputs(
        args.db,
        args.out_dir,
        limit_per_category=args.limit_per_category,
        min_score=args.min_score,
        include_low_confidence=args.include_low_confidence,
        photos_per_post=args.photos_per_post,
        output_format=args.format,
    )
    print(f"Wrote candidate outputs: {args.out_dir.resolve()}")
    print(f"Candidate rows: {len(result.candidates)}")
    print(f"Project/article posts: {len(result.project_article_posts)}")
    for category_id, candidates in result.candidates_by_category.items():
        print(f"  {category_id}: {len(candidates)}")


def run_extract_facts(args: argparse.Namespace) -> None:
    result = extract_facts(
        args.db,
        args.out_dir,
        source=args.source,
        limit=args.limit,
        min_project_article_score=args.min_project_article_score,
        include_needs_review=args.include_needs_review,
        output_format=args.format,
    )
    print(f"Wrote extracted fact outputs: {args.out_dir.resolve()}")
    print(f"Source posts processed: {result.source_posts_processed}")
    print(f"Extracted facts: {len(result.facts)}")
    print(f"Clean facts: {sum(1 for fact in result.facts if not fact.needs_review)}")
    print(f"Needs review: {sum(1 for fact in result.facts if fact.needs_review)}")
    for category_id in [
        "flooring",
        "wall_colors",
        "kitchens",
        "chairs",
        "tables",
        "sofas",
        "hallway",
        "living_room_furniture",
    ]:
        count = sum(1 for fact in result.facts if fact.category == category_id)
        print(f"  {category_id}: {count}")


def run_llm_review_command(args: argparse.Namespace) -> int:
    try:
        result = run_llm_review(
            facts_db=args.facts_db,
            out_dir=args.out_dir,
            provider=args.provider,
            base_url=args.base_url,
            model=args.model or "local-model",
            source=args.source,
            category=args.category,
            sample_size=args.sample_size,
            seed=args.seed,
            max_evidence_chars=args.max_evidence_chars,
            temperature=args.temperature,
            dry_run=args.dry_run,
            limit=args.limit,
            resume=args.resume,
            timeout_seconds=args.timeout_seconds,
            strict_json=args.strict_json,
            canonical_db=args.canonical_db,
        )
    except FileNotFoundError as exc:
        print(f"error: {exc}")
        return 2
    except ValueError as exc:
        print(f"error: {exc}")
        return 2
    except LLMProviderError as exc:
        print(
            "error: could not reach LLM provider — "
            f"{exc}. Start LM Studio at the configured --base-url or pass --provider mock."
        )
        return 3

    if result.dry_run:
        print(f"[dry-run] planned rows: {len(result.planned_rows)}")
        for fact in result.planned_rows[:10]:
            print(
                f"  fact_id={fact.fact_id} category={fact.category} "
                f"item_type={fact.item_type} needs_review={int(fact.needs_review)}"
            )
        if len(result.planned_rows) > 10:
            print(f"  ... and {len(result.planned_rows) - 10} more")
        print(f"[dry-run] no model calls made; wrote planned_rows.csv into {args.out_dir.resolve()}")
        return 0

    print(f"Wrote LLM review outputs: {args.out_dir.resolve()}")
    print(f"Provider: {result.provider} | Model: {result.model}")
    print(f"Reviewed: {len(result.records)} | Skipped (resume): {len(result.skipped)}")
    print(f"Invalid/error: {result.invalid_count} | Retries: {result.retry_count}")
    return 0


def run_father_report_command(args: argparse.Namespace) -> int:
    try:
        result = build_father_report(
            facts_db=args.facts_db,
            out_dir=args.out_dir,
            llm_review_dbs=args.llm_review_db,
            canonical_db=args.canonical_db,
            output_format=args.format,
            min_confidence=args.min_confidence,
            include_low_confidence=args.include_low_confidence,
            max_examples_per_section=args.max_examples_per_section,
            max_top_values=args.max_top_values,
            strict=args.strict,
            include_appendix=args.include_appendix,
            report_title=args.report_title,
            generated_note=args.generated_note,
            language=args.language,
        )
    except FileNotFoundError as exc:
        print(f"error: {exc}")
        return 2
    except ValueError as exc:
        print(f"error: {exc}")
        return 2

    print(f"Wrote father-facing Markdown report: {args.out_dir.resolve()}")
    print(f"Effective facts used: {len(result.dataset.facts)}")
    print(f"Facts excluded: {len(result.dataset.excluded)}")
    print(f"LLM fix decisions applied: {result.dataset.applied_fix_count}")
    print("Output files:")
    for path in result.output_files:
        print(f"  {path}")
    return 0


def run_kitchen_palette_report_command(args: argparse.Namespace) -> int:
    try:
        result = build_kitchen_palette_report(
            facts_db=args.facts_db,
            canonical_db=args.canonical_db,
            out_dir=args.out_dir,
            channel_username=args.channel_username,
            examples_per_category=args.examples_per_category,
            photos_per_example=args.photos_per_example,
            output_format=args.format,
        )
    except FileNotFoundError as exc:
        print(f"error: {exc}")
        return 2
    except ValueError as exc:
        print(f"error: {exc}")
        return 2

    print(f"Wrote kitchen palette report: {args.out_dir.resolve()}")
    print(f"Kitchen facts read: {result.kitchen_fact_count}")
    print(f"Project candidates: {result.project_candidate_count}")
    print("Selected examples:")
    for category_id, count in result.selected_by_category.items():
        print(f"  {category_id}: {count}")
    print(f"Contact sheets generated: {result.contact_sheet_count}")
    print(f"Selected examples with fewer than requested photos: {result.examples_without_enough_photos}")
    print("Output files:")
    for path in result.output_files:
        print(f"  {path}")
    return 0
