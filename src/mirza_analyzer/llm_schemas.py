from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


VALID_DECISIONS = {"keep", "fix", "discard", "needs_human"}
VALID_CONFIDENCE = {"high", "medium", "low"}

VALID_CATEGORIES = {
    "flooring",
    "wall_colors",
    "kitchens",
    "chairs",
    "tables",
    "sofas",
    "hallway",
    "living_room_furniture",
}

CORRECTED_FIELDS = (
    "category",
    "item_type",
    "vendor_normalized",
    "model",
    "material",
    "finish",
    "color",
    "color_code",
    "article_id",
    "price_value",
    "price_unit",
    "room_context",
)

NORMALIZED_TERM_FIELDS = (
    "vendor",
    "facade_materials",
    "sofa_fabric",
    "sofa_color",
    "wall_color_code",
    "flooring_brand_or_collection",
)


class LLMReviewValidationError(ValueError):
    """Raised when an LLM review response does not match the strict schema."""


@dataclass(frozen=True)
class LLMReviewResult:
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

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "category_correct": self.category_correct,
            "item_type_correct": self.item_type_correct,
            "price_correct": self.price_correct,
            "is_bundle": self.is_bundle,
            "is_context_false_positive": self.is_context_false_positive,
            "is_non_target_room": self.is_non_target_room,
            "corrected": dict(self.corrected),
            "normalized_terms": dict(self.normalized_terms),
            "rationale_short": self.rationale_short,
            "confidence": self.confidence,
        }


def _require_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise LLMReviewValidationError(f"Field `{name}` must be boolean, got {type(value).__name__}")


def _optional_bool(value: Any, name: str) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    raise LLMReviewValidationError(
        f"Field `{name}` must be boolean or null, got {type(value).__name__}"
    )


def _coerce_corrected(value: Any) -> dict[str, Any]:
    if value is None:
        return {field: None for field in CORRECTED_FIELDS}
    if not isinstance(value, dict):
        raise LLMReviewValidationError("`corrected` must be an object")
    cleaned: dict[str, Any] = {}
    for name in CORRECTED_FIELDS:
        raw = value.get(name)
        if raw is None:
            cleaned[name] = None
            continue
        if name == "price_value":
            if isinstance(raw, bool):
                raise LLMReviewValidationError("`corrected.price_value` must be a number or null")
            if isinstance(raw, (int, float)):
                cleaned[name] = raw
            else:
                raise LLMReviewValidationError("`corrected.price_value` must be a number or null")
            continue
        if name == "category":
            if isinstance(raw, str) and raw and raw not in VALID_CATEGORIES:
                raise LLMReviewValidationError(
                    f"`corrected.category` must be one of {sorted(VALID_CATEGORIES)} or null"
                )
        if isinstance(raw, str):
            cleaned[name] = raw
        else:
            raise LLMReviewValidationError(
                f"`corrected.{name}` must be a string or null"
            )
    return cleaned


def _coerce_normalized_terms(value: Any) -> dict[str, Any]:
    if value is None:
        return {
            "vendor": None,
            "facade_materials": [],
            "sofa_fabric": None,
            "sofa_color": None,
            "wall_color_code": None,
            "flooring_brand_or_collection": None,
        }
    if not isinstance(value, dict):
        raise LLMReviewValidationError("`normalized_terms` must be an object")
    cleaned: dict[str, Any] = {}
    for name in NORMALIZED_TERM_FIELDS:
        raw = value.get(name)
        if name == "facade_materials":
            if raw is None:
                cleaned[name] = []
                continue
            if not isinstance(raw, list):
                raise LLMReviewValidationError(
                    "`normalized_terms.facade_materials` must be a list of strings"
                )
            items: list[str] = []
            for entry in raw:
                if not isinstance(entry, str):
                    raise LLMReviewValidationError(
                        "`normalized_terms.facade_materials` must contain strings"
                    )
                items.append(entry)
            cleaned[name] = items
            continue
        if raw is None:
            cleaned[name] = None
            continue
        if not isinstance(raw, str):
            raise LLMReviewValidationError(
                f"`normalized_terms.{name}` must be a string or null"
            )
        cleaned[name] = raw
    return cleaned


def validate_review_payload(data: Any) -> LLMReviewResult:
    """Validate a model-produced object against the strict review schema."""
    if not isinstance(data, dict):
        raise LLMReviewValidationError("Top-level response must be a JSON object")

    decision = data.get("decision")
    if decision not in VALID_DECISIONS:
        raise LLMReviewValidationError(
            f"`decision` must be one of {sorted(VALID_DECISIONS)}, got {decision!r}"
        )

    confidence = data.get("confidence")
    if confidence not in VALID_CONFIDENCE:
        raise LLMReviewValidationError(
            f"`confidence` must be one of {sorted(VALID_CONFIDENCE)}, got {confidence!r}"
        )

    rationale = data.get("rationale_short")
    if not isinstance(rationale, str):
        raise LLMReviewValidationError("`rationale_short` must be a string")

    return LLMReviewResult(
        decision=decision,
        category_correct=_require_bool(data.get("category_correct"), "category_correct"),
        item_type_correct=_require_bool(data.get("item_type_correct"), "item_type_correct"),
        price_correct=_optional_bool(data.get("price_correct"), "price_correct"),
        is_bundle=_require_bool(data.get("is_bundle"), "is_bundle"),
        is_context_false_positive=_require_bool(
            data.get("is_context_false_positive"), "is_context_false_positive"
        ),
        is_non_target_room=_require_bool(data.get("is_non_target_room"), "is_non_target_room"),
        corrected=_coerce_corrected(data.get("corrected")),
        normalized_terms=_coerce_normalized_terms(data.get("normalized_terms")),
        rationale_short=rationale,
        confidence=confidence,
    )


def empty_corrected() -> dict[str, Any]:
    return {field: None for field in CORRECTED_FIELDS}


def empty_normalized_terms() -> dict[str, Any]:
    return {
        "vendor": None,
        "facade_materials": [],
        "sofa_fabric": None,
        "sofa_color": None,
        "wall_color_code": None,
        "flooring_brand_or_collection": None,
    }
