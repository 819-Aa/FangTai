"""Generate offline-only nutrition usage and retention review queues."""

from __future__ import annotations

import csv
import shutil
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal

from food_agent_v2.b1.consumer_views import (
    ConsumerViewSet,
    IngredientIdentityFact,
    IngredientOccurrenceFact,
    RecipeFact,
)
from food_agent_v2.core.paths import PROJECT_ROOT

ReviewStatus = Literal["pending"]
_PENDING: ReviewStatus = "pending"
_USAGE_CODES = frozenset({"main", "supporting", "seasoning", "cooking_fat", "cooking_liquid"})
_COOKING_FAT_IDENTITIES = frozenset(
    {
        "油", "食用油", "植物油", "花生油", "菜籽油", "橄榄油", "玉米油",
        "大豆油", "葵花籽油", "猪油", "色拉油",
    }
)
_COOKING_LIQUID_IDENTITIES = frozenset(
    {
        "水", "清水", "凉水", "热水", "开水", "汤", "高汤", "清汤", "鸡汤",
        "骨汤", "肉汤", "汤汁",
    }
)
_DISCARD_MARKERS = ("倒掉", "弃去", "过滤", "滤出", "捞出", "沥干", "去除", "倒出")
_FORMAL_FILENAMES = frozenset(
    {
        "ingredient_measure_rules.csv",
        "ingredient_quantity_decisions.csv",
        "ingredient_nutrition_usage_decisions.csv",
        "ingredient_nutrition_retention_decisions.csv",
        "ingredient_edible_fraction_rules.csv",
        "ingredient_edible_fraction_decisions.csv",
    }
)
_BASE_HEADERS = (
    "occurrence_id", "recipe_id", "recipe_name", "ingredient_id", "ingredient_name",
    "source_fragment", "normalized_form", "category", "quantity_raw",
    "bound_step_indexes", "bound_step_text",
)
_FILES = (
    ("usage_candidates", "nutrition_usage_candidates.csv", "candidate_usage_code", True),
    ("usage_exceptions", "nutrition_usage_exceptions.csv", "exception_codes", False),
    ("retention_candidates", "nutrition_retention_candidates.csv", "candidate_retained_in_dish", True),
    ("retention_exceptions", "nutrition_retention_exceptions.csv", "exception_codes", False),
)


@dataclass(frozen=True)
class NutritionOccurrenceReviewContext:
    views: ConsumerViewSet
    occurrences: tuple[IngredientOccurrenceFact, ...]
    identities: tuple[IngredientIdentityFact, ...]
    recipes: tuple[RecipeFact, ...]


@dataclass(frozen=True)
class _ReviewRow:
    occurrence_id: str
    recipe_id: int
    recipe_name: str
    ingredient_id: int
    ingredient_name: str
    source_fragment: str
    normalized_form: str
    category: str
    quantity_raw: str
    bound_step_indexes: tuple[int, ...]
    bound_step_text: tuple[str, ...]
    evidence_codes: tuple[str, ...]
    review_status: ReviewStatus = _PENDING


@dataclass(frozen=True)
class NutritionUsageCandidate(_ReviewRow):
    candidate_usage_code: str = ""


@dataclass(frozen=True)
class NutritionUsageException(_ReviewRow):
    exception_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class NutritionRetentionCandidate(_ReviewRow):
    candidate_retained_in_dish: bool = True


@dataclass(frozen=True)
class NutritionRetentionException(_ReviewRow):
    exception_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class NutritionOccurrenceReviewBundle:
    usage_candidates: tuple[NutritionUsageCandidate, ...] = ()
    usage_exceptions: tuple[NutritionUsageException, ...] = ()
    retention_candidates: tuple[NutritionRetentionCandidate, ...] = ()
    retention_exceptions: tuple[NutritionRetentionException, ...] = ()


def generate_nutrition_occurrence_review(
    context: NutritionOccurrenceReviewContext,
) -> NutritionOccurrenceReviewBundle:
    """Route unresolved occurrences using only local, deterministic evidence."""
    occurrences = _unique_by(context.occurrences, "occurrence_id")
    identities = _unique_by(context.identities, "ingredient_id")
    recipes = _unique_by(context.recipes, "recipe_id")
    step_views = _unique_by(context.views.step_views, "recipe_id")
    usage_candidates: list[NutritionUsageCandidate] = []
    usage_exceptions: list[NutritionUsageException] = []
    retention_candidates: list[NutritionRetentionCandidate] = []
    retention_exceptions: list[NutritionRetentionException] = []

    for nutrition_view in sorted(context.views.nutrition_views, key=lambda item: item.recipe_id):
        recipe = recipes.get(nutrition_view.recipe_id)
        step_view = step_views.get(nutrition_view.recipe_id)
        if recipe is None or step_view is None:
            raise ValueError("营养 occurrence 缺少当前菜品或步骤视图")
        for nutrition in nutrition_view.ingredients:
            occurrence = occurrences.get(nutrition.occurrence_id)
            identity = identities.get(nutrition.ingredient_id)
            if occurrence is None or identity is None:
                raise ValueError("营养 occurrence 缺少身份事实")
            if occurrence.recipe_id != nutrition_view.recipe_id:
                raise ValueError("营养 occurrence 与菜品不一致")
            bound_steps = tuple(
                step for step in step_view.steps
                if nutrition.occurrence_id in step.bound_occurrence_ids
            )
            common = _row_kwargs(recipe, occurrence, identity, nutrition.normalized_form, bound_steps)
            if nutrition.usage_requires_review:
                if _is_cooking_fat(identity.name_canonical):
                    usage_candidates.append(
                        NutritionUsageCandidate(
                            **common,
                            candidate_usage_code="cooking_fat",
                            evidence_codes=("NUTRITION_USAGE_CONTROLLED_COOKING_FAT",),
                        )
                    )
                elif _is_cooking_liquid(identity.name_canonical):
                    usage_candidates.append(
                        NutritionUsageCandidate(
                            **common,
                            candidate_usage_code="cooking_liquid",
                            evidence_codes=("NUTRITION_USAGE_CONTROLLED_COOKING_LIQUID",),
                        )
                    )
                else:
                    usage_exceptions.append(
                        NutritionUsageException(
                            **common,
                            evidence_codes=("NUTRITION_USAGE_UNRESOLVED",),
                            exception_codes=("NUTRITION_USAGE_AMBIGUOUS",),
                        )
                    )
            if nutrition.retention_requires_review:
                if not bound_steps:
                    retention_exceptions.append(
                        NutritionRetentionException(
                            **common,
                            evidence_codes=("NUTRITION_RETENTION_UNBOUND",),
                            exception_codes=("NUTRITION_RETENTION_NO_STEP_BINDING",),
                        )
                    )
                elif any(marker in step.raw_text for step in bound_steps for marker in _DISCARD_MARKERS):
                    retention_exceptions.append(
                        NutritionRetentionException(
                            **common,
                            evidence_codes=("NUTRITION_RETENTION_DISCARD_EVIDENCE",),
                            exception_codes=("NUTRITION_RETENTION_DISCARD_RISK",),
                        )
                    )
                elif _is_cooking_fat(identity.name_canonical) or _is_cooking_liquid(identity.name_canonical):
                    retention_exceptions.append(
                        NutritionRetentionException(
                            **common,
                            evidence_codes=("NUTRITION_RETENTION_COOKING_MEDIUM",),
                            exception_codes=("NUTRITION_RETENTION_AMBIGUOUS",),
                        )
                    )
                else:
                    retention_candidates.append(
                        NutritionRetentionCandidate(
                            **common,
                            evidence_codes=("NUTRITION_RETENTION_BOUND_SOLID",),
                        )
                    )

    return NutritionOccurrenceReviewBundle(
        usage_candidates=_sorted_rows(usage_candidates),
        usage_exceptions=_sorted_rows(usage_exceptions),
        retention_candidates=_sorted_rows(retention_candidates),
        retention_exceptions=_sorted_rows(retention_exceptions),
    )


def write_nutrition_occurrence_review(
    bundle: NutritionOccurrenceReviewBundle, output_dir: Path
) -> dict[str, int]:
    """Write the four pending queues together, restoring prior queues on failure."""
    directory = Path(output_dir)
    _refuse_formal_target(directory)
    rows_by_name = {
        "usage_candidates": _sorted_rows(bundle.usage_candidates),
        "usage_exceptions": _sorted_rows(bundle.usage_exceptions),
        "retention_candidates": _sorted_rows(bundle.retention_candidates),
        "retention_exceptions": _sorted_rows(bundle.retention_exceptions),
    }
    _validate_bundle(rows_by_name)
    directory.mkdir(parents=True, exist_ok=True)
    targets = {key: directory / filename for key, filename, _, _ in _FILES}
    for target in targets.values():
        _refuse_formal_target(directory)
        _refuse_formal_target(target)

    temporary_paths: dict[str, Path] = {}
    backup_paths: dict[str, Path] = {}
    replaced: list[str] = []
    try:
        for key, _, special_header, include_evidence_codes in _FILES:
            temporary_paths[key] = _write_temporary(
                directory,
                targets[key],
                rows_by_name[key],
                special_header,
                include_evidence_codes,
            )
        for key, target in targets.items():
            if target.exists():
                with NamedTemporaryFile(
                    "wb", dir=directory, prefix=f".{target.name}.", suffix=".bak", delete=False
                ) as handle:
                    backup = Path(handle.name)
                backup_paths[key] = backup
                shutil.copyfile(target, backup)
        for key, target in targets.items():
            _refuse_formal_target(directory)
            _refuse_formal_target(target)
            temporary_paths[key].replace(target)
            replaced.append(key)
        return {key: len(rows_by_name[key]) for key, _, _, _ in _FILES}
    except BaseException:
        for key in reversed(replaced):
            target = targets[key]
            backup = backup_paths.get(key)
            if backup is None:
                target.unlink(missing_ok=True)
            else:
                backup.replace(target)
                backup_paths.pop(key, None)
        raise
    finally:
        for path in (*temporary_paths.values(), *backup_paths.values()):
            path.unlink(missing_ok=True)


def _row_kwargs(recipe, occurrence, identity, normalized_form, bound_steps) -> dict:
    return {
        "occurrence_id": occurrence.occurrence_id,
        "recipe_id": occurrence.recipe_id,
        "recipe_name": recipe.name,
        "ingredient_id": identity.ingredient_id,
        "ingredient_name": identity.name_canonical,
        "source_fragment": occurrence.source_fragment,
        "normalized_form": (normalized_form or occurrence.form or "").strip(),
        "category": identity.category,
        "quantity_raw": occurrence.quantity_raw or "",
        "bound_step_indexes": tuple(step.step_index for step in bound_steps),
        "bound_step_text": tuple(step.raw_text for step in bound_steps),
    }


def _write_temporary(
    directory: Path, target: Path, rows, special_header: str, include_evidence_codes: bool
) -> Path:
    fieldnames = (*_BASE_HEADERS, special_header)
    if include_evidence_codes:
        fieldnames = (*fieldnames, "evidence_codes")
    fieldnames = (*fieldnames, "review_status")
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            "w", encoding="utf-8", newline="", dir=directory,
            prefix=f".{target.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(_csv_row(row, special_header, include_evidence_codes))
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
    if temporary_path is None:
        raise RuntimeError("营养审阅临时文件未创建")
    return temporary_path


def _csv_row(
    row: _ReviewRow, special_header: str, include_evidence_codes: bool
) -> dict[str, str | int]:
    result: dict[str, str | int] = {
        "occurrence_id": _safe_csv_text(row.occurrence_id),
        "recipe_id": row.recipe_id,
        "recipe_name": _safe_csv_text(row.recipe_name),
        "ingredient_id": row.ingredient_id,
        "ingredient_name": _safe_csv_text(row.ingredient_name),
        "source_fragment": _safe_csv_text(row.source_fragment),
        "normalized_form": _safe_csv_text(row.normalized_form),
        "category": _safe_csv_text(row.category),
        "quantity_raw": _safe_csv_text(row.quantity_raw),
        "bound_step_indexes": _safe_csv_text(";".join(map(str, row.bound_step_indexes))),
        "bound_step_text": _safe_csv_text(";".join(row.bound_step_text)),
        "review_status": row.review_status,
    }
    if include_evidence_codes:
        result["evidence_codes"] = _safe_csv_text(";".join(row.evidence_codes))
    value = getattr(row, special_header)
    result[special_header] = (
        str(value).lower() if isinstance(value, bool) else _safe_csv_text(";".join(value) if isinstance(value, tuple) else value)
    )
    return result


def _validate_bundle(rows_by_name: dict[str, tuple]) -> None:
    for key, rows in rows_by_name.items():
        expected_type = {
            "usage_candidates": NutritionUsageCandidate,
            "usage_exceptions": NutritionUsageException,
            "retention_candidates": NutritionRetentionCandidate,
            "retention_exceptions": NutritionRetentionException,
        }[key]
        ids: set[str] = set()
        for row in rows:
            if not isinstance(row, expected_type):
                raise ValueError("营养审阅队列行类型非法")
            _validate_row(row)
            if row.occurrence_id in ids:
                raise ValueError("营养审阅队列 occurrence_id 重复")
            ids.add(row.occurrence_id)
    for candidate_key, exception_key in (
        ("usage_candidates", "usage_exceptions"),
        ("retention_candidates", "retention_exceptions"),
    ):
        candidate_ids = {row.occurrence_id for row in rows_by_name[candidate_key]}
        exception_ids = {row.occurrence_id for row in rows_by_name[exception_key]}
        if candidate_ids & exception_ids:
            raise ValueError("营养审阅候选与异常 occurrence_id 交叉重复")


def _validate_row(row: _ReviewRow) -> None:
    if row.review_status != _PENDING:
        raise ValueError("营养审阅队列必须保持 pending")
    if type(row.recipe_id) is not int or row.recipe_id < 1:
        raise ValueError("营养审阅 recipe_id 非法")
    if type(row.ingredient_id) is not int or row.ingredient_id < 1:
        raise ValueError("营养审阅 ingredient_id 非法")
    for field in (
        "occurrence_id", "recipe_name", "ingredient_name", "source_fragment",
        "normalized_form", "category", "quantity_raw",
    ):
        if not isinstance(getattr(row, field), str):
            raise ValueError("营养审阅文本字段非法")
    if not row.occurrence_id.strip() or not row.recipe_name.strip() or not row.ingredient_name.strip():
        raise ValueError("营养审阅必填文本不能为空")
    if (
        not isinstance(row.bound_step_indexes, tuple)
        or not all(type(index) is int and index > 0 for index in row.bound_step_indexes)
        or row.bound_step_indexes != tuple(sorted(set(row.bound_step_indexes)))
        or not isinstance(row.bound_step_text, tuple)
        or len(row.bound_step_indexes) != len(row.bound_step_text)
        or not all(isinstance(text, str) for text in row.bound_step_text)
    ):
        raise ValueError("营养审阅步骤绑定非法")
    _validate_codes(row.evidence_codes, "evidence_codes")
    if isinstance(row, NutritionUsageCandidate):
        if row.candidate_usage_code not in _USAGE_CODES:
            raise ValueError("营养用途候选代码非法")
    elif isinstance(row, NutritionRetentionCandidate):
        if row.candidate_retained_in_dish is not True:
            raise ValueError("营养留存候选不得自动写入 false")
    elif isinstance(row, (NutritionUsageException, NutritionRetentionException)):
        _validate_codes(row.exception_codes, "exception_codes")


def _validate_codes(codes: tuple[str, ...], field: str) -> None:
    if (
        not isinstance(codes, tuple)
        or not codes
        or not all(isinstance(code, str) and code for code in codes)
        or codes != tuple(sorted(set(codes)))
    ):
        raise ValueError(f"营养审阅 {field} 非法")


def _unique_by(rows, field: str) -> dict:
    result = {}
    for row in rows:
        key = getattr(row, field)
        if key in result:
            raise ValueError(f"营养审阅 {field} 重复")
        result[key] = row
    return result


def _sorted_rows(rows):
    return tuple(sorted(rows, key=lambda row: (row.recipe_id, row.occurrence_id)))


def _is_cooking_fat(name: str) -> bool:
    return name.lstrip("=+-@") in _COOKING_FAT_IDENTITIES


def _is_cooking_liquid(name: str) -> bool:
    return name.lstrip("=+-@") in _COOKING_LIQUID_IDENTITIES


def _safe_csv_text(value: str) -> str:
    if value.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _refuse_formal_target(path: Path) -> None:
    path = Path(path)
    formal_root = PROJECT_ROOT / "data" / "review"
    formal_paths = tuple(formal_root / name for name in _FORMAL_FILENAMES)
    if path.name.casefold() in _FORMAL_FILENAMES:
        raise ValueError("候选不得写入正式规则或决定文件")
    try:
        resolved_path = path.resolve(strict=False)
        resolved_root = formal_root.resolve(strict=False)
        if resolved_path == resolved_root or resolved_path.is_relative_to(resolved_root):
            raise ValueError("候选不得写入正式规则或决定文件")
        for formal in formal_paths:
            if str(resolved_path).casefold() == str(formal.resolve(strict=False)).casefold():
                raise ValueError("候选不得写入正式规则或决定文件")
            if path.exists() and formal.exists() and path.samefile(formal):
                raise ValueError("候选不得写入正式规则或决定文件")
    except OSError as exc:
        raise ValueError("候选目标路径无法安全校验") from exc
