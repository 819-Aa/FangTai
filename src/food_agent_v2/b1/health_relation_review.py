"""Independent approval boundary for the health relation matrix."""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

REQUIRED_COLUMNS = (
    "constraint_code",
    "ingredient_id",
    "decision",
    "evidence",
    "review_status",
    "reviewer",
    "reviewed_at",
)


class ApprovedHealthRelationDecision(BaseModel):
    """One independently reviewed matrix cell."""

    model_config = ConfigDict(extra="forbid")

    constraint_code: str
    ingredient_id: int
    decision: Literal["hard_exclude", "no_hard_relation"]
    evidence: str
    review_status: Literal["approved"] = "approved"
    reviewer: str
    reviewed_at: str
    relation_id: str | None = None

    @model_validator(mode="after")
    def bind_relation_id(self) -> ApprovedHealthRelationDecision:
        expected = (
            f"health-relation:{self.constraint_code}:{self.ingredient_id}"
            if self.decision == "hard_exclude"
            else None
        )
        if self.relation_id not in {None, expected}:
            raise ValueError("relation_id does not match the approved decision")
        self.relation_id = expected
        return self


class HealthRelationReviewError(ValueError):
    """A stable failure code for H03 gate diagnostics."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _fail(code: str, message: str) -> None:
    raise HealthRelationReviewError(code, message)


def _parse_ingredient_id(value: str | None, row_number: int) -> int:
    try:
        return int((value or "").strip())
    except ValueError:
        _fail(
            "HEALTH_RELATION_REVIEW_UNKNOWN_INGREDIENT",
            f"row {row_number} has an invalid ingredient_id",
        )


def load_approved_health_relation_decisions(
    path: Path,
    *,
    allowed_constraint_codes: tuple[str, ...],
    health_ingredient_ids: tuple[int, ...],
    builder_identity: str,
) -> tuple[ApprovedHealthRelationDecision, ...]:
    """Load an exact, independently signed constraint-by-ingredient matrix."""
    if len(set(allowed_constraint_codes)) != len(allowed_constraint_codes):
        _fail(
            "HEALTH_RELATION_REVIEW_INVALID_SCOPE",
            "allowed constraint codes contain duplicates",
        )
    if len(set(health_ingredient_ids)) != len(health_ingredient_ids):
        _fail(
            "HEALTH_RELATION_REVIEW_INVALID_SCOPE",
            "health ingredient ids contain duplicates",
        )
    if not path.is_file():
        _fail("HEALTH_RELATION_REVIEW_MISSING_FILE", f"missing review file: {path}")

    allowed_codes = set(allowed_constraint_codes)
    allowed_ingredients = set(health_ingredient_ids)
    expected = {
        (constraint_code, ingredient_id)
        for constraint_code in allowed_constraint_codes
        for ingredient_id in health_ingredient_ids
    }
    observed: set[tuple[str, int]] = set()
    approved: list[ApprovedHealthRelationDecision] = []

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing_columns = set(REQUIRED_COLUMNS) - set(reader.fieldnames or ())
        if missing_columns:
            _fail(
                "HEALTH_RELATION_REVIEW_INVALID_HEADER",
                f"missing columns: {sorted(missing_columns)}",
            )

        for row_number, row in enumerate(reader, start=2):
            constraint_code = (row.get("constraint_code") or "").strip()
            ingredient_id = _parse_ingredient_id(row.get("ingredient_id"), row_number)
            key = (constraint_code, ingredient_id)

            if constraint_code not in allowed_codes:
                _fail(
                    "HEALTH_RELATION_REVIEW_UNKNOWN_CONSTRAINT",
                    f"row {row_number} contains {constraint_code!r}",
                )
            if ingredient_id not in allowed_ingredients:
                _fail(
                    "HEALTH_RELATION_REVIEW_UNKNOWN_INGREDIENT",
                    f"row {row_number} contains ingredient_id={ingredient_id}",
                )
            if key in observed:
                _fail(
                    "HEALTH_RELATION_REVIEW_DUPLICATE",
                    f"row {row_number} duplicates {key}",
                )
            observed.add(key)

            review_status = (row.get("review_status") or "").strip().casefold()
            if review_status != "approved":
                _fail(
                    "HEALTH_RELATION_REVIEW_PENDING",
                    f"row {row_number} is not approved",
                )

            evidence = (row.get("evidence") or "").strip()
            if not evidence:
                _fail(
                    "HEALTH_RELATION_REVIEW_MISSING_EVIDENCE",
                    f"row {row_number} has no evidence",
                )
            reviewer = (row.get("reviewer") or "").strip()
            if not reviewer:
                _fail(
                    "HEALTH_RELATION_REVIEW_MISSING_REVIEWER",
                    f"row {row_number} has no reviewer",
                )
            if reviewer.casefold() == builder_identity.strip().casefold():
                _fail(
                    "HEALTH_RELATION_REVIEW_SELF_SIGNED",
                    f"row {row_number} was signed by the builder",
                )

            reviewed_at = (row.get("reviewed_at") or "").strip()
            if not reviewed_at:
                _fail(
                    "HEALTH_RELATION_REVIEW_MISSING_REVIEW_DATE",
                    f"row {row_number} has no reviewed_at date",
                )
            try:
                date.fromisoformat(reviewed_at)
            except ValueError:
                _fail(
                    "HEALTH_RELATION_REVIEW_INVALID_REVIEW_DATE",
                    f"row {row_number} has invalid reviewed_at={reviewed_at!r}",
                )

            decision = (row.get("decision") or "").strip()
            if decision not in {"hard_exclude", "no_hard_relation"}:
                _fail(
                    "HEALTH_RELATION_REVIEW_INVALID_DECISION",
                    f"row {row_number} has invalid decision={decision!r}",
                )
            approved.append(
                ApprovedHealthRelationDecision(
                    constraint_code=constraint_code,
                    ingredient_id=ingredient_id,
                    decision=decision,
                    evidence=evidence,
                    reviewer=reviewer,
                    reviewed_at=reviewed_at,
                )
            )

    if observed != expected or len(approved) != len(expected):
        missing = sorted(expected - observed)[:10]
        extra = sorted(observed - expected)[:10]
        _fail(
            "HEALTH_RELATION_REVIEW_INCOMPLETE",
            f"expected {len(expected)} decisions, got {len(approved)}; "
            f"missing={missing}, extra={extra}",
        )

    order = {
        (constraint_code, ingredient_id): index
        for index, (constraint_code, ingredient_id) in enumerate(
            (code, ingredient_id)
            for code in allowed_constraint_codes
            for ingredient_id in health_ingredient_ids
        )
    }
    return tuple(
        sorted(approved, key=lambda item: order[(item.constraint_code, item.ingredient_id)])
    )
