from __future__ import annotations

import argparse
from pathlib import Path

from .audit import write_audit_report
from .candidate_mining import write_candidate_outputs
from .db import create_database_from_data_root, database_stats
from .sample import write_sample_posts


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


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
