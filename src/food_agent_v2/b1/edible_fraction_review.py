"""Resolve reviewed edible fractions at occurrence or ingredient-form scope."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal

from food_agent_v2.b1.consumer_views import NutritionOccurrenceInput
from food_agent_v2.core.paths import PROJECT_ROOT

ReviewStatus = Literal["pending", "approved", "modified", "rejected"]
_STATUSES = frozenset({"pending", "approved", "modified", "rejected"})
_ACTIVE = frozenset({"approved", "modified"})
_RULE_HEADERS = (
    "ingredient_id",
    "ingredient_name",
    "normalized_form",
    "edible_fraction",
    "review_status",
)
_DECISION_HEADERS = (
    "occurrence_id",
    "recipe_id",
    "ingredient_id",
    "ingredient_name",
    "decision_form",
    "edible_fraction",
    "review_status",
)
_DIAGNOSTIC_REASONS = (
    "blank_form",
    "multi_form_ingredient",
    "unresolved_retention",
    "no_effective_rule_or_occurrence_decision",
)
_USAGE_CODES = frozenset(
    {"main", "supporting", "seasoning", "cooking_fat", "cooking_liquid"}
)
_CANDIDATE_BASES = frozenset(
    {"no_effective_rule_or_occurrence_decision", "unresolved_retention"}
)


@dataclass(frozen=True)
class EdibleFractionRule:
    ingredient_id: int
    ingredient_name: str
    normalized_form: str
    edible_fraction: Decimal
    review_status: ReviewStatus


class EdibleFractionRuleIndex:
    """Effective generic fractions keyed exclusively by ingredient ID and form."""

    def __init__(self, rules) -> None:
        self._effective: dict[tuple[int, str], EdibleFractionRule] = {}
        all_keys: set[tuple[int, str]] = set()
        forms_by_ingredient: dict[int, set[str]] = {}
        for rule in rules:
            _validate_rule(rule)
            key = (rule.ingredient_id, _form(rule.normalized_form))
            if key in all_keys:
                raise ValueError(f"重复可食比例规则: {key}")
            all_keys.add(key)
            forms_by_ingredient.setdefault(rule.ingredient_id, set()).add(key[1])
            if rule.review_status in _ACTIVE:
                self._effective[key] = rule
        if any("" in forms and len(forms) > 1 for forms in forms_by_ingredient.values()):
            raise ValueError("空白形态不得掩盖多形态事实")

    def get(self, ingredient_id: int, normalized_form: str) -> Decimal | None:
        rule = self.get_rule(ingredient_id, normalized_form)
        return rule.edible_fraction if rule is not None else None

    def get_rule(self, ingredient_id: int, normalized_form: str) -> EdibleFractionRule | None:
        return self._effective.get((ingredient_id, _form(normalized_form)))


@dataclass(frozen=True)
class EdibleFractionDecision:
    occurrence_id: str
    recipe_id: int
    ingredient_id: int
    ingredient_name: str
    decision_form: str
    edible_fraction: Decimal
    review_status: ReviewStatus


class EdibleFractionDecisionIndex:
    """Only approved or modified occurrence decisions are queryable."""

    def __init__(self, decisions) -> None:
        self._effective: dict[str, EdibleFractionDecision] = {}
        for decision in decisions:
            _validate_decision(decision)
            if decision.occurrence_id in self._effective:
                raise ValueError("同一 occurrence 存在重复可食比例决定")
            self._effective[decision.occurrence_id] = decision
        self._effective = {
            occurrence_id: decision
            for occurrence_id, decision in self._effective.items()
            if decision.review_status in _ACTIVE
        }

    def get_effective(self, occurrence_id: str) -> EdibleFractionDecision | None:
        return self._effective.get(occurrence_id)


@dataclass(frozen=True)
class EdibleFractionReviewCandidate:
    occurrence_id: str
    recipe_id: int
    ingredient_id: int
    ingredient_name: str
    source_form: str
    decision_form: str
    candidate_edible_fraction: Decimal | None = None
    candidate_basis: str = "no_effective_rule_or_occurrence_decision"
    quantity_raw: str = ""
    usage_code: str | None = None
    retained_in_dish: bool | None = None
    exception_reasons: tuple[str, ...] = ()
    review_status: Literal["pending"] = "pending"


def generate_edible_fraction_candidates(views, rules, decisions):
    """Emit one pending, no-fraction diagnostic for each unresolved retained occurrence."""
    forms_by_ingredient: dict[int, set[str]] = {}
    for view in views:
        for occurrence in view.ingredients:
            if occurrence.retained_in_dish is False:
                continue
            forms_by_ingredient.setdefault(occurrence.ingredient_id, set()).add(
                (occurrence.normalized_form or "").strip()
            )
    candidates = []
    for view in views:
        for occurrence in view.ingredients:
            if occurrence.retained_in_dish is False:
                continue
            retention_unresolved = (
                occurrence.retention_requires_review
                or occurrence.retained_in_dish is None
            )
            resolution = resolve_edible_fraction(occurrence, rules, decisions)
            if (
                not retention_unresolved
                and resolution.edible_fraction is not None
                and not resolution.requires_review
            ):
                continue
            reasons: list[str] = []
            if not (occurrence.normalized_form or "").strip():
                reasons.append("blank_form")
            if len(forms_by_ingredient.get(occurrence.ingredient_id, ())) > 1:
                reasons.append("multi_form_ingredient")
            if retention_unresolved:
                reasons.append("unresolved_retention")
            fraction_unresolved = (
                resolution.edible_fraction is None or resolution.requires_review
            )
            if fraction_unresolved:
                reasons.append("no_effective_rule_or_occurrence_decision")
            decision = decisions.get_effective(occurrence.occurrence_id)
            candidate_basis = (
                "unresolved_retention"
                if retention_unresolved
                else "no_effective_rule_or_occurrence_decision"
            )
            candidates.append(
                EdibleFractionReviewCandidate(
                    occurrence_id=occurrence.occurrence_id,
                    recipe_id=view.recipe_id,
                    ingredient_id=occurrence.ingredient_id,
                    ingredient_name=occurrence.ingredient_name,
                    source_form=(occurrence.normalized_form or "").strip(),
                    decision_form=decision.decision_form if decision is not None else "",
                    candidate_basis=candidate_basis,
                    quantity_raw=occurrence.quantity_raw or "",
                    usage_code=occurrence.usage_code,
                    retained_in_dish=occurrence.retained_in_dish,
                    exception_reasons=tuple(
                        reason for reason in _DIAGNOSTIC_REASONS if reason in reasons
                    ),
                )
            )
    return tuple(candidates)


def write_edible_fraction_candidates(candidates, output_path: Path) -> None:
    """Write pending diagnostics atomically without creating a fraction."""
    path = Path(output_path)
    candidate_rows = tuple(candidates)
    if _is_formal_edible_fraction_path(path) or _is_formal_measure_rule_path(path):
        raise ValueError("可食比例候选不得写入正式规则或决定文件")
    for candidate in candidate_rows:
        _validate_output_candidate(candidate)
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = (
        "occurrence_id",
        "recipe_id",
        "ingredient_id",
        "ingredient_name",
        "source_form",
        "decision_form",
        "candidate_edible_fraction",
        "candidate_basis",
        "quantity_raw",
        "usage_code",
        "retained_in_dish",
        "exception_reasons",
        "review_status",
    )
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            "w", encoding="utf-8", newline="", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for candidate in candidate_rows:
                writer.writerow(
                    {
                        "occurrence_id": _safe_csv_text(candidate.occurrence_id),
                        "recipe_id": candidate.recipe_id,
                        "ingredient_id": candidate.ingredient_id,
                        "ingredient_name": _safe_csv_text(candidate.ingredient_name),
                        "source_form": _safe_csv_text(candidate.source_form),
                        "decision_form": _safe_csv_text(candidate.decision_form),
                        "candidate_edible_fraction": "",
                        "candidate_basis": _safe_csv_text(candidate.candidate_basis),
                        "quantity_raw": _safe_csv_text(candidate.quantity_raw),
                        "usage_code": _safe_csv_text(candidate.usage_code),
                        "retained_in_dish": (
                            "" if candidate.retained_in_dish is None
                            else str(candidate.retained_in_dish).lower()
                        ),
                        "exception_reasons": _safe_csv_text(
                            "|".join(candidate.exception_reasons)
                        ),
                        "review_status": _safe_csv_text(candidate.review_status),
                    }
                )
        if _is_formal_edible_fraction_path(path) or _is_formal_measure_rule_path(path):
            raise ValueError("可食比例候选不得写入正式规则或决定文件")
        temporary_path.replace(path)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def _validate_output_candidate(candidate: EdibleFractionReviewCandidate) -> None:
    if candidate.review_status != "pending":
        raise ValueError("可食比例候选必须保持 pending")
    if candidate.candidate_edible_fraction is not None:
        raise ValueError("可食比例候选不得填写比例")
    if type(candidate.recipe_id) is not int or candidate.recipe_id < 1:
        raise ValueError("可食比例候选 recipe_id 非法")
    if type(candidate.ingredient_id) is not int or candidate.ingredient_id < 1:
        raise ValueError("可食比例候选 ingredient_id 非法")
    for field in (
        "occurrence_id", "ingredient_name", "source_form", "decision_form",
        "candidate_basis", "quantity_raw",
    ):
        if not isinstance(getattr(candidate, field), str):
            raise ValueError("可食比例候选文本字段非法")
    if not candidate.occurrence_id.strip() or not candidate.ingredient_name.strip():
        raise ValueError("可食比例候选必填文本不能为空")
    if candidate.usage_code is not None and not isinstance(candidate.usage_code, str):
        raise ValueError("可食比例候选 usage_code 非法")
    if candidate.usage_code is not None and candidate.usage_code not in _USAGE_CODES:
        raise ValueError("可食比例候选 usage_code 非法")
    if candidate.candidate_basis not in _CANDIDATE_BASES:
        raise ValueError("可食比例候选 candidate_basis 非法")
    if candidate.retained_in_dish not in (True, None):
        raise ValueError("可食比例候选必须为 retained 或未决")
    if (
        not isinstance(candidate.exception_reasons, tuple)
        or not candidate.exception_reasons
        or len(set(candidate.exception_reasons)) != len(candidate.exception_reasons)
        or any(reason not in _DIAGNOSTIC_REASONS for reason in candidate.exception_reasons)
        or candidate.exception_reasons
        != tuple(reason for reason in _DIAGNOSTIC_REASONS if reason in candidate.exception_reasons)
    ):
        raise ValueError("可食比例候选诊断原因非法")
    unresolved = "unresolved_retention" in candidate.exception_reasons
    if (candidate.candidate_basis == "unresolved_retention") != unresolved:
        raise ValueError("可食比例候选 candidate_basis 与诊断原因不一致")
    if (
        candidate.candidate_basis == "no_effective_rule_or_occurrence_decision"
        and "no_effective_rule_or_occurrence_decision" not in candidate.exception_reasons
    ):
        raise ValueError("可食比例候选缺少有效规则诊断原因")


def _safe_csv_text(value: str | None) -> str:
    text = value or ""
    if text.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def _is_formal_edible_fraction_path(path: Path) -> bool:
    return _is_formal_alias(path, _formal_review_paths())


def _is_formal_measure_rule_path(path: Path) -> bool:
    return _is_formal_alias(path, _formal_review_paths())


def _formal_review_paths() -> tuple[Path, ...]:
    review_root = PROJECT_ROOT / "data" / "review"
    return (
        review_root / "ingredient_measure_rules.csv",
        review_root / "ingredient_quantity_decisions.csv",
        review_root / "ingredient_nutrition_usage_decisions.csv",
        review_root / "ingredient_edible_fraction_rules.csv",
        review_root / "ingredient_edible_fraction_decisions.csv",
    )


def _is_formal_alias(path: Path, formal_paths: tuple[Path, ...]) -> bool:
    if any(path.name.casefold() == formal.name.casefold() for formal in formal_paths):
        return True
    try:
        resolved_path = path.resolve(strict=False)
        resolved = str(resolved_path).casefold()
        review_root = (PROJECT_ROOT / "data" / "review").resolve(strict=False)
        if resolved_path == review_root or resolved_path.is_relative_to(review_root):
            return True
        for formal in formal_paths:
            if resolved == str(formal.resolve(strict=False)).casefold():
                return True
            if path.exists() and formal.exists() and path.samefile(formal):
                return True
    except OSError:
        return True
    return False


@dataclass(frozen=True)
class EdibleFractionResolution:
    edible_fraction: Decimal | None
    requires_review: bool


def resolve_edible_fraction(
    occurrence: NutritionOccurrenceInput,
    rules: EdibleFractionRuleIndex,
    decisions: EdibleFractionDecisionIndex,
) -> EdibleFractionResolution:
    """Prefer a matching occurrence decision and fail closed on any drift."""
    decision = decisions.get_effective(occurrence.occurrence_id)
    if decision is not None:
        if not _decision_matches(decision, occurrence):
            return EdibleFractionResolution(None, True)
        return EdibleFractionResolution(decision.edible_fraction, False)
    rule = rules.get_rule(occurrence.ingredient_id, occurrence.normalized_form)
    if rule is None or rule.ingredient_name != occurrence.ingredient_name:
        return EdibleFractionResolution(None, True)
    return EdibleFractionResolution(rule.edible_fraction, False)


def load_edible_fraction_rules(path: Path) -> EdibleFractionRuleIndex:
    return EdibleFractionRuleIndex(_read(path, _RULE_HEADERS, _parse_rule, "可食比例规则"))


def load_edible_fraction_decisions(
    path: Path, *, current_occurrence_ids: set[str] | None = None
) -> EdibleFractionDecisionIndex:
    decisions = _read(path, _DECISION_HEADERS, _parse_decision, "可食比例决定")
    if current_occurrence_ids is not None:
        unknown = sorted(
            decision.occurrence_id
            for decision in decisions
            if decision.occurrence_id not in current_occurrence_ids
        )
        if unknown:
            raise ValueError(f"未知 occurrence_id: {unknown[:5]}")
    return EdibleFractionDecisionIndex(
        decisions
    )


def _decision_matches(
    decision: EdibleFractionDecision, occurrence: NutritionOccurrenceInput
) -> bool:
    source_form = _form(occurrence.normalized_form)
    return (
        decision.recipe_id == _recipe_id(occurrence.occurrence_id)
        and decision.ingredient_id == occurrence.ingredient_id
        and decision.ingredient_name == occurrence.ingredient_name
        and (not source_form or _form(decision.decision_form) == source_form)
    )


def _recipe_id(occurrence_id: str) -> int | None:
    prefix, separator, _ = occurrence_id.partition("-")
    return int(prefix) if separator and prefix.isdecimal() else None


def _validate_rule(rule: EdibleFractionRule) -> None:
    _positive_id(rule.ingredient_id, "可食比例规则 ingredient_id")
    _nonblank(rule.ingredient_name, "可食比例规则 ingredient_name")
    if not isinstance(rule.normalized_form, str):
        raise ValueError("可食比例规则 normalized_form 必须为字符串")
    _fraction(rule.edible_fraction)
    _status(rule.review_status, "可食比例规则")


def _validate_decision(decision: EdibleFractionDecision) -> None:
    _nonblank(decision.occurrence_id, "可食比例决定 occurrence_id")
    _positive_id(decision.recipe_id, "可食比例决定 recipe_id")
    _positive_id(decision.ingredient_id, "可食比例决定 ingredient_id")
    _nonblank(decision.ingredient_name, "可食比例决定 ingredient_name")
    _nonblank(decision.decision_form, "可食比例决定 decision_form")
    _fraction(decision.edible_fraction)
    _status(decision.review_status, "可食比例决定")


def _positive_id(value: int, label: str) -> None:
    if type(value) is not int or value < 1:
        raise ValueError(f"{label} 必须为正整数")


def _nonblank(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} 不能为空")


def _fraction(value: Decimal) -> None:
    if (
        not isinstance(value, Decimal)
        or not value.is_finite()
        or not Decimal("0") < value <= Decimal("1")
    ):
        raise ValueError("可食比例必须在有限区间 (0, 1] 内")


def _status(value: str, label: str) -> None:
    if value not in _STATUSES:
        raise ValueError(f"{label} review_status 非法")


def _form(value: str) -> str:
    return value.strip()


def _read(path: Path, headers: tuple[str, ...], parser, label: str):
    review_path = Path(path)
    if not review_path.exists():
        return ()
    with review_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != headers:
            raise ValueError(f"{label} CSV 表头非法")
        return tuple(parser(row, line_number) for line_number, row in enumerate(reader, 2))


def _parse_rule(row: dict[str, str], line_number: int) -> EdibleFractionRule:
    try:
        return EdibleFractionRule(
            _int(row, "ingredient_id"),
            _text(row, "ingredient_name"),
            _form(row["normalized_form"]),
            _decimal(row, "edible_fraction"),
            _text(row, "review_status"),  # type: ignore[arg-type]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"可食比例规则 CSV 第 {line_number} 行非法: {exc}") from exc


def _parse_decision(row: dict[str, str], line_number: int) -> EdibleFractionDecision:
    try:
        return EdibleFractionDecision(
            _text(row, "occurrence_id"),
            _int(row, "recipe_id"),
            _int(row, "ingredient_id"),
            _text(row, "ingredient_name"),
            _text(row, "decision_form"),
            _decimal(row, "edible_fraction"),
            _text(row, "review_status"),  # type: ignore[arg-type]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"可食比例决定 CSV 第 {line_number} 行非法: {exc}") from exc


def _text(row: dict[str, str], field: str) -> str:
    value = row[field]
    _nonblank(value, field)
    return value.strip()


def _int(row: dict[str, str], field: str) -> int:
    value = _text(row, field)
    if not value.isdecimal():
        raise ValueError(f"{field} 必须为正整数")
    return int(value)


def _decimal(row: dict[str, str], field: str) -> Decimal:
    try:
        return Decimal(_text(row, field))
    except InvalidOperation as exc:
        raise ValueError(f"{field} 必须为十进制数") from exc
