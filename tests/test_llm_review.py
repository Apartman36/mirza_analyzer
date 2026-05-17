from __future__ import annotations

import csv
import json
import random
import sqlite3
from pathlib import Path
from typing import Callable

import pytest

from mirza_analyzer.cli import main
from mirza_analyzer.llm_providers import MockProvider
from mirza_analyzer.llm_review import (
    SYSTEM_PROMPT,
    USER_PROMPT_TEMPLATE,
    build_user_prompt,
    run_llm_review,
    select_sample,
    load_facts,
)
from mirza_analyzer.llm_schemas import (
    LLMReviewValidationError,
    validate_review_payload,
)


FACTS_SCHEMA = """
CREATE TABLE extracted_facts (
    id INTEGER PRIMARY KEY,
    source_message_id INTEGER NOT NULL,
    date TEXT,
    source_scope TEXT,
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
    extraction_method TEXT,
    confidence TEXT NOT NULL,
    needs_review INTEGER NOT NULL,
    notes TEXT,
    source_text_hash TEXT,
    created_at TEXT,
    first_photo_path TEXT
);
"""


def _insert_fact(conn: sqlite3.Connection, **overrides) -> int:
    base = {
        "source_message_id": 1000,
        "date": "2025-01-01T00:00:00",
        "source_scope": "project_articles",
        "project_name": None,
        "category": "kitchens",
        "item_type": "kitchen_facades",
        "item_name": None,
        "vendor_raw": None,
        "vendor_normalized": None,
        "brand_raw": None,
        "brand_normalized": None,
        "model": None,
        "material": None,
        "finish": None,
        "color": None,
        "color_code": None,
        "article_id": None,
        "marketplace": None,
        "price_value": None,
        "price_currency": None,
        "price_unit": None,
        "promo_code": None,
        "room_context": None,
        "evidence_quote": "evidence",
        "extraction_method": "regex",
        "confidence": "medium",
        "needs_review": 0,
        "notes": None,
        "source_text_hash": "h",
        "created_at": "2025-01-01T00:00:00",
        "first_photo_path": None,
    }
    base.update(overrides)
    columns = ", ".join(base)
    placeholders = ", ".join("?" for _ in base)
    cursor = conn.execute(
        f"INSERT INTO extracted_facts ({columns}) VALUES ({placeholders})",
        tuple(base.values()),
    )
    return int(cursor.lastrowid)


@pytest.fixture
def facts_db(tmp_path: Path) -> Path:
    path = tmp_path / "extracted_facts.sqlite"
    with sqlite3.connect(path) as conn:
        conn.executescript(FACTS_SCHEMA)
        _insert_fact(
            conn,
            id=1,
            source_message_id=1001,
            category="kitchens",
            item_type="kitchen_facades",
            vendor_normalized="Mebel.in",
            evidence_quote="Кухня фасады Дуб каселла Mebel.in 270 000₽",
            needs_review=0,
            notes=None,
            confidence="high",
        )
        _insert_fact(
            conn,
            id=2,
            source_message_id=1002,
            category="sofas",
            item_type="sofa",
            vendor_normalized="Divan.ru",
            evidence_quote="Бра над диваном — IKEA, 4 990₽",
            needs_review=1,
            notes="lighting/decor context near sofa",
            confidence="low",
        )
        _insert_fact(
            conn,
            id=3,
            source_message_id=1003,
            category="sofas",
            item_type="bundle_purchase",
            vendor_normalized="Divan.ru",
            evidence_quote="Диваны, стулья, столик, тумба Divan.ru 242 440₽",
            needs_review=1,
            notes="bundle purchase: several unrelated items share price",
            confidence="medium",
            price_value=242440,
            price_currency="RUB",
        )
        _insert_fact(
            conn,
            id=4,
            source_message_id=1004,
            category="wall_colors",
            item_type="wall_color",
            color_code="G482",
            evidence_quote="Цвет стен G482",
            needs_review=0,
            confidence="high",
        )
        _insert_fact(
            conn,
            id=5,
            source_message_id=1005,
            category="tables",
            item_type="table",
            vendor_normalized="OZON",
            evidence_quote="Стол OZON Арт. 1550292417 14 157₽",
            needs_review=0,
            confidence="high",
        )
        _insert_fact(
            conn,
            id=6,
            source_message_id=1006,
            category="flooring",
            item_type="flooring",
            vendor_normalized="Tarkett",
            evidence_quote="Кварцвинил Tarkett",
            needs_review=1,
            notes="non-target room context",
            confidence="low",
        )
        _insert_fact(
            conn,
            id=7,
            source_message_id=1007,
            category="chairs",
            item_type="chair",
            evidence_quote="Стул IKEA",
            needs_review=0,
            confidence="medium",
        )
        _insert_fact(
            conn,
            id=8,
            source_message_id=1008,
            category="hallway",
            item_type="hallway_item",
            evidence_quote="Обувница",
            needs_review=1,
            notes="suspicious descriptor",
            confidence="low",
        )
        _insert_fact(
            conn,
            id=9,
            source_message_id=1009,
            category="living_room_furniture",
            item_type="cabinet",
            evidence_quote="Тумба в гостиной",
            needs_review=1,
            notes="suspicious descriptor",
            confidence="low",
        )
        _insert_fact(
            conn,
            id=10,
            source_message_id=1010,
            category="kitchens",
            item_type="countertop",
            evidence_quote="Столешница кварц",
            needs_review=0,
            confidence="medium",
        )
        conn.commit()
    return path


def _keep_handler(_system: str, _user: str, _model: str) -> str:
    return json.dumps(
        {
            "decision": "keep",
            "category_correct": True,
            "item_type_correct": True,
            "price_correct": True,
            "is_bundle": False,
            "is_context_false_positive": False,
            "is_non_target_room": False,
            "corrected": {},
            "normalized_terms": {"facade_materials": []},
            "rationale_short": "looks good",
            "confidence": "high",
        }
    )


def test_run_llm_review_with_mock_writes_outputs(facts_db: Path, tmp_path: Path) -> None:
    out_dir = tmp_path / "llm_review"
    result = run_llm_review(
        facts_db=facts_db,
        out_dir=out_dir,
        provider="mock",
        sample_size=6,
        seed=42,
        source="mixed",
        provider_instance=MockProvider(handler=_keep_handler),
    )

    expected_files = {
        "llm_review_results.jsonl",
        "llm_review_results.csv",
        "llm_review.sqlite",
        "summary.md",
    }
    assert expected_files <= {p.name for p in out_dir.iterdir()}
    assert (out_dir / "by_decision" / "keep.md").exists()
    assert (out_dir / "by_decision" / "fix.md").exists()
    assert (out_dir / "by_decision" / "discard.md").exists()
    assert (out_dir / "by_decision" / "needs_human.md").exists()
    assert (out_dir / "by_category" / "kitchens.md").exists()
    assert (out_dir / "prompts" / "system_prompt.txt").exists()
    assert (out_dir / "prompts" / "user_prompt_template.txt").exists()

    with (out_dir / "llm_review_results.jsonl").open("r", encoding="utf-8") as fh:
        jsonl_rows = [json.loads(line) for line in fh if line.strip()]
    assert len(jsonl_rows) == len(result.records)
    assert all(row["review"]["decision"] == "keep" for row in jsonl_rows)

    with (out_dir / "llm_review_results.csv").open("r", encoding="utf-8", newline="") as fh:
        csv_rows = list(csv.DictReader(fh))
    assert len(csv_rows) == len(result.records)
    assert all(row["decision"] == "keep" for row in csv_rows)

    with sqlite3.connect(out_dir / "llm_review.sqlite") as conn:
        count = conn.execute("SELECT COUNT(*) FROM llm_review_results").fetchone()[0]
        runs = conn.execute("SELECT provider, source FROM llm_review_runs").fetchall()
    assert count == len(result.records)
    assert runs and runs[0] == ("mock", "mixed")

    summary_text = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "LLM Review Summary" in summary_text
    assert "Decisions" in summary_text


def test_invalid_json_retries_once_then_falls_back_to_needs_human(
    facts_db: Path, tmp_path: Path
) -> None:
    calls = {"n": 0}

    def handler(_system: str, _user: str, _model: str) -> str:
        calls["n"] += 1
        return "this is not JSON at all"

    out_dir = tmp_path / "llm_review"
    result = run_llm_review(
        facts_db=facts_db,
        out_dir=out_dir,
        provider="mock",
        sample_size=1,
        seed=42,
        source="needs_review",
        provider_instance=MockProvider(handler=handler),
    )

    assert len(result.records) == 1
    record = result.records[0]
    assert record.result.decision == "needs_human"
    assert record.retry_count == 1
    assert record.error and "invalid_json_after_retry" in record.error
    assert calls["n"] == 2


def test_recovers_after_one_repair_when_second_call_returns_valid_json(
    facts_db: Path, tmp_path: Path
) -> None:
    state = {"n": 0}

    def handler(_system: str, _user: str, _model: str) -> str:
        state["n"] += 1
        if state["n"] == 1:
            return "garbage"
        return _keep_handler("", "", "")

    out_dir = tmp_path / "llm_review"
    result = run_llm_review(
        facts_db=facts_db,
        out_dir=out_dir,
        provider="mock",
        sample_size=1,
        seed=42,
        source="needs_review",
        provider_instance=MockProvider(handler=handler),
    )
    assert len(result.records) == 1
    record = result.records[0]
    assert record.result.decision == "keep"
    assert record.retry_count == 1
    assert record.error and record.error.startswith("recovered_after_retry")


def test_sampling_is_deterministic_with_same_seed(facts_db: Path) -> None:
    facts = load_facts(facts_db)
    first = select_sample(facts, source="mixed", category=None, sample_size=5, seed=42)
    second = select_sample(facts, source="mixed", category=None, sample_size=5, seed=42)
    assert [f.fact_id for f in first] == [f.fact_id for f in second]

    different = select_sample(facts, source="mixed", category=None, sample_size=5, seed=99)
    assert [f.fact_id for f in different] != [] or different == []


def test_mixed_sampling_includes_clean_and_needs_review(facts_db: Path) -> None:
    facts = load_facts(facts_db)
    sample = select_sample(
        facts, source="mixed", category=None, sample_size=8, seed=42
    )
    flags = {int(fact.needs_review) for fact in sample}
    assert 0 in flags
    assert 1 in flags


def test_prompt_builder_includes_evidence_and_source_message_id() -> None:
    prompt = build_user_prompt(
        fact_dict={"fact_id": 42, "category": "sofas"},
        evidence_quote="Диван Divan.ru 64 990₽",
        source_post_excerpt="Полный текст поста...",
        source_message_id=7299,
    )
    assert "Диван Divan.ru 64 990₽" in prompt
    assert "7299" in prompt
    assert "Полный текст поста" in prompt
    assert '"category":"sofas"' in prompt or '"category": "sofas"' in prompt


def test_validate_review_payload_rejects_invalid_decision_enum() -> None:
    payload = {
        "decision": "approve",  # invalid enum
        "category_correct": True,
        "item_type_correct": True,
        "price_correct": None,
        "is_bundle": False,
        "is_context_false_positive": False,
        "is_non_target_room": False,
        "corrected": {},
        "normalized_terms": {"facade_materials": []},
        "rationale_short": "ok",
        "confidence": "high",
    }
    with pytest.raises(LLMReviewValidationError):
        validate_review_payload(payload)


def test_validate_review_payload_rejects_invalid_corrected_category() -> None:
    payload = {
        "decision": "fix",
        "category_correct": False,
        "item_type_correct": True,
        "price_correct": None,
        "is_bundle": False,
        "is_context_false_positive": False,
        "is_non_target_room": False,
        "corrected": {"category": "lighting"},
        "normalized_terms": {"facade_materials": []},
        "rationale_short": "wrong category",
        "confidence": "medium",
    }
    with pytest.raises(LLMReviewValidationError):
        validate_review_payload(payload)


def test_context_false_positive_can_be_saved_as_discard(facts_db: Path, tmp_path: Path) -> None:
    def handler(_system: str, user_prompt: str, _model: str) -> str:
        if "Бра" in user_prompt:
            return json.dumps(
                {
                    "decision": "discard",
                    "category_correct": False,
                    "item_type_correct": False,
                    "price_correct": None,
                    "is_bundle": False,
                    "is_context_false_positive": True,
                    "is_non_target_room": False,
                    "corrected": {"category": None, "item_type": None},
                    "normalized_terms": {"facade_materials": []},
                    "rationale_short": "Бра — это светильник над диваном, не сам диван",
                    "confidence": "high",
                }
            )
        return _keep_handler("", "", "")

    out_dir = tmp_path / "llm_review"
    result = run_llm_review(
        facts_db=facts_db,
        out_dir=out_dir,
        provider="mock",
        sample_size=10,
        seed=42,
        source="mixed",
        provider_instance=MockProvider(handler=handler),
    )
    discards = [
        record for record in result.records
        if record.result.decision == "discard"
    ]
    assert discards, "Expected at least one discard for the Бра-над-диваном row"
    assert all(record.result.is_context_false_positive for record in discards)

    discard_md = (out_dir / "by_decision" / "discard.md").read_text(encoding="utf-8")
    assert "Бра" in discard_md


def test_bundle_result_can_be_saved_as_fix_with_is_bundle_true(
    facts_db: Path, tmp_path: Path
) -> None:
    def handler(_system: str, user_prompt: str, _model: str) -> str:
        if "Диваны, стулья" in user_prompt or "242 440" in user_prompt:
            return json.dumps(
                {
                    "decision": "fix",
                    "category_correct": True,
                    "item_type_correct": False,
                    "price_correct": False,
                    "is_bundle": True,
                    "is_context_false_positive": False,
                    "is_non_target_room": False,
                    "corrected": {"item_type": "bundle_purchase"},
                    "normalized_terms": {"facade_materials": []},
                    "rationale_short": "groupped purchase: price not for a single item",
                    "confidence": "high",
                }
            )
        return _keep_handler("", "", "")

    out_dir = tmp_path / "llm_review"
    result = run_llm_review(
        facts_db=facts_db,
        out_dir=out_dir,
        provider="mock",
        sample_size=10,
        seed=42,
        source="mixed",
        provider_instance=MockProvider(handler=handler),
    )
    bundles = [r for r in result.records if r.result.is_bundle]
    assert bundles, "Expected a bundle row to be marked"
    assert any(r.result.decision == "fix" for r in bundles)

    with sqlite3.connect(out_dir / "llm_review.sqlite") as conn:
        rows = conn.execute(
            "SELECT decision, is_bundle FROM llm_review_results WHERE is_bundle=1"
        ).fetchall()
    assert rows
    assert all(row[0] in {"fix", "needs_human"} for row in rows)


def test_dry_run_writes_no_model_results(facts_db: Path, tmp_path: Path) -> None:
    out_dir = tmp_path / "llm_review"
    result = run_llm_review(
        facts_db=facts_db,
        out_dir=out_dir,
        provider="mock",
        sample_size=4,
        seed=42,
        source="mixed",
        dry_run=True,
    )
    assert result.dry_run is True
    assert result.records == []
    assert result.planned_rows
    assert (out_dir / "planned_rows.csv").exists()
    assert not (out_dir / "llm_review_results.jsonl").exists()
    assert not (out_dir / "llm_review.sqlite").exists()
    assert not (out_dir / "summary.md").exists()


def test_cli_dry_run_prints_planned_rows(
    facts_db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out_dir = tmp_path / "llm_review"
    code = main(
        [
            "llm-review",
            "--facts-db",
            str(facts_db),
            "--out-dir",
            str(out_dir),
            "--provider",
            "mock",
            "--source",
            "needs_review",
            "--sample-size",
            "3",
            "--dry-run",
        ]
    )
    assert code == 0
    captured = capsys.readouterr().out
    assert "[dry-run] planned rows" in captured
    assert "fact_id=" in captured


def test_cli_with_lmstudio_provider_exits_gracefully_when_unreachable(
    facts_db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out_dir = tmp_path / "llm_review_smoke"
    code = main(
        [
            "llm-review",
            "--facts-db",
            str(facts_db),
            "--out-dir",
            str(out_dir),
            "--provider",
            "lmstudio",
            "--base-url",
            "http://127.0.0.1:1/v1",
            "--source",
            "needs_review",
            "--sample-size",
            "1",
            "--timeout-seconds",
            "1",
        ]
    )
    assert code == 3
    captured = capsys.readouterr().out
    assert "error" in captured.lower()
    assert "LM Studio" in captured or "127.0.0.1" in captured


def test_resume_skips_already_reviewed_inputs(facts_db: Path, tmp_path: Path) -> None:
    out_dir = tmp_path / "llm_review"
    run_llm_review(
        facts_db=facts_db,
        out_dir=out_dir,
        provider="mock",
        sample_size=4,
        seed=42,
        source="mixed",
        provider_instance=MockProvider(handler=_keep_handler),
    )
    second = run_llm_review(
        facts_db=facts_db,
        out_dir=out_dir,
        provider="mock",
        sample_size=4,
        seed=42,
        source="mixed",
        resume=True,
        provider_instance=MockProvider(handler=_keep_handler),
    )
    assert second.records == []
    assert len(second.skipped) > 0
