"""九维营养参考和经审阅的食材映射。"""

from __future__ import annotations

import csv
import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

NUTRIENT_FIELDS = (
    "energy_kcal",
    "protein_g",
    "fat_g",
    "carbohydrate_g",
    "fiber_g",
    "sodium_mg",
    "calcium_mg",
    "iron_mg",
    "cholesterol_mg",
)
NUTRIENT_UNITS = {
    "energy_kcal": "kcal",
    "protein_g": "g",
    "fat_g": "g",
    "carbohydrate_g": "g",
    "fiber_g": "g",
    "sodium_mg": "mg",
    "calcium_mg": "mg",
    "iron_mg": "mg",
    "cholesterol_mg": "mg",
}

SourceDataset = Literal[
    "china_cdc",
    "usda_foundation",
    "usda_sr_legacy",
    "usda_fndds",
    "branded",
]
FoodOrigin = Literal["plant", "animal", "mixed", "unknown"]
ReviewStatus = Literal["pending", "approved", "modified", "rejected"]

_SOURCE_PRIORITIES = {
    "china_cdc": 1,
    "usda_foundation": 2,
    "usda_sr_legacy": 3,
    "usda_fndds": 4,
    "branded": 5,
}


class NutritionVector(BaseModel):
    model_config = ConfigDict(extra="forbid")

    energy_kcal: Decimal | None = None
    protein_g: Decimal | None = None
    fat_g: Decimal | None = None
    carbohydrate_g: Decimal | None = None
    fiber_g: Decimal | None = None
    sodium_mg: Decimal | None = None
    calcium_mg: Decimal | None = None
    iron_mg: Decimal | None = None
    cholesterol_mg: Decimal | None = None

    @field_validator("*")
    @classmethod
    def _non_negative(cls, value):
        if value is not None and value < 0:
            raise ValueError("营养值不得为负数")
        return value

    @property
    def complete(self) -> bool:
        return all(getattr(self, field) is not None for field in NUTRIENT_FIELDS)


class IngredientNutritionReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reference_id: str
    canonical_name: str
    form: str
    per_100g: NutritionVector
    source_name: str
    source_url: str
    source_dataset: SourceDataset
    food_origin: FoodOrigin = "unknown"
    brand_name: str | None = None
    specification: str | None = None

    @model_validator(mode="after")
    def _apply_confirmed_structural_zeroes(self):
        values = self.per_100g.model_dump()
        if self.food_origin == "plant" and values["cholesterol_mg"] is None:
            values["cholesterol_mg"] = Decimal("0")
        if self.food_origin == "animal" and values["fiber_g"] is None:
            values["fiber_g"] = Decimal("0")
        self.per_100g = NutritionVector(**values)
        if self.source_dataset == "branded" and (
            not self.brand_name or not self.specification
        ):
            raise ValueError("品牌营养参考必须包含品牌和规格")
        return self


class NutritionCrosswalkDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ingredient_id: int
    ingredient_name: str
    form: str
    reference_id: str
    match_method: Literal["exact", "approved_alias"]
    review_status: ReviewStatus
    brand_name: str | None = None
    specification: str | None = None


class NutritionReferenceIndex:
    def __init__(self, references) -> None:
        self._references: dict[str, IngredientNutritionReference] = {}
        for reference in references:
            if reference.reference_id in self._references:
                raise ValueError(f"重复营养参考: {reference.reference_id}")
            self._references[reference.reference_id] = reference

    def get(self, reference_id: str) -> IngredientNutritionReference | None:
        return self._references.get(reference_id)

    def find_by_name(self, canonical_name: str) -> tuple[IngredientNutritionReference, ...]:
        name = canonical_name.strip()
        return tuple(
            sorted(
                (
                    item
                    for item in self._references.values()
                    if item.canonical_name.strip() == name
                ),
                key=lambda item: (source_priority(item.source_dataset), item.reference_id),
            )
        )

    def find_by_review_alias(
        self, ingredient_name: str
    ) -> tuple[IngredientNutritionReference, ...]:
        """Return conservative, review-only aliases; never an approved mapping."""
        lookup_names = _ingredient_review_aliases(ingredient_name)
        return tuple(
            sorted(
                (
                    item
                    for item in self._references.values()
                    if lookup_names & _reference_review_aliases(item.canonical_name)
                ),
                key=lambda item: (source_priority(item.source_dataset), item.reference_id),
            )
        )

    @property
    def values(self) -> tuple[IngredientNutritionReference, ...]:
        return tuple(self._references.values())


class NutritionCrosswalkIndex:
    def __init__(self, decisions, references: NutritionReferenceIndex) -> None:
        self._references: dict[tuple[int, str], IngredientNutritionReference] = {}
        for decision in decisions:
            if decision.review_status not in {"approved", "modified"}:
                continue
            key = (decision.ingredient_id, decision.form.strip())
            if key in self._references:
                raise ValueError(f"重复营养映射: {key}")
            reference = references.get(decision.reference_id)
            if reference is None:
                raise ValueError(f"营养映射引用未知参考: {decision.reference_id}")
            if decision.form.strip() != reference.form.strip():
                raise ValueError(f"营养映射形态不一致: {key}")
            if decision.match_method == "exact" and (
                decision.ingredient_name.strip() != reference.canonical_name.strip()
            ):
                raise ValueError(f"exact 营养映射名称不一致: {key}")
            if reference.source_dataset == "branded":
                exact_brand = (
                    decision.match_method == "exact"
                    and decision.ingredient_name.strip() == reference.canonical_name.strip()
                    and decision.brand_name == reference.brand_name
                    and decision.specification == reference.specification
                )
                if not exact_brand:
                    raise ValueError(f"品牌营养参考只能按名称、品牌和规格 exact 匹配: {key}")
            self._references[key] = reference

    def get(self, ingredient_id: int, form: str | None) -> IngredientNutritionReference | None:
        return self._references.get((ingredient_id, (form or "").strip()))

    @property
    def approved_count(self) -> int:
        return len(self._references)


def source_priority(source_dataset: SourceDataset) -> int:
    return _SOURCE_PRIORITIES[source_dataset]


def convert_china_composition_record(record: dict) -> IngredientNutritionReference:
    """机械转换项目内已固化的中国疾控记录；缺失项保持 None。"""
    source_id = record.get("source_id")
    name = str(record.get("name", "")).strip()
    source_name = str(record.get("source_name", "")).strip()
    source_url = str(record.get("source_url", "")).strip()
    if source_id in (None, "") or not name or not source_name or not source_url:
        raise ValueError("中国食物成分记录缺少身份或来源字段")
    return IngredientNutritionReference(
        reference_id=f"china-cdc-{source_id}",
        canonical_name=name,
        form=_infer_source_form(name),
        per_100g=NutritionVector(
            energy_kcal=_record_decimal(record, "energy_kcal_per_100g"),
            protein_g=_record_decimal(record, "protein_g_per_100g"),
            fat_g=_record_decimal(record, "fat_g_per_100g"),
            carbohydrate_g=_record_decimal(record, "carb_g_per_100g"),
            fiber_g=_record_decimal(record, "fiber_g_per_100g"),
            sodium_mg=_record_decimal(record, "sodium_mg_per_100g"),
            calcium_mg=_record_decimal(record, "calcium_mg_per_100g"),
            iron_mg=_record_decimal(record, "iron_mg_per_100g"),
            cholesterol_mg=_record_decimal(record, "cholesterol_mg_per_100g"),
        ),
        source_name=source_name,
        source_url=source_url,
        source_dataset="china_cdc",
        food_origin="unknown",
    )


def _record_decimal(record: dict, key: str) -> Decimal | None:
    value = record.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"营养值类型不合法: {key}")
    try:
        return Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"营养值不是合法十进制数: {key}") from exc


def _infer_source_form(name: str) -> str:
    if any(marker in name for marker in ("水发", "泡发")):
        return "hydrated"
    if any(marker in name for marker in ("干制", "脱水", "干，", "(干)", "（干）")):
        return "dried"
    if any(marker in name for marker in ("熟", "煮", "蒸", "烤", "炸")):
        return "cooked"
    return "unspecified"


def nutrition_form_from_occurrence(form: str | None) -> str:
    value = (form or "").strip()
    if value in {"水发", "泡发", "复水"}:
        return "hydrated"
    if value in {"干", "干制", "脱水", "干燥"}:
        return "dried"
    if value in {"熟", "熟制", "煮熟", "蒸熟", "烤熟"}:
        return "cooked"
    if value in {"生", "生鲜", "未熟"}:
        return "raw"
    return "unspecified"


def write_nutrition_crosswalk_candidates(
    ingredient_form_rows,
    references: NutritionReferenceIndex,
    output_path: Path,
) -> None:
    """输出 exact 名称候选；状态固定 pending，形态差异必须显式审阅。"""
    fieldnames = (
        "ingredient_id",
        "ingredient_name",
        "form",
        "candidate_reference_id",
        "candidate_name",
        "candidate_form",
        "source_dataset",
        "match_method",
        "reason",
        "review_status",
    )
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    seen: set[tuple[int, str]] = set()
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for ingredient_id, ingredient_name, form in ingredient_form_rows:
            key = (int(ingredient_id), str(form).strip())
            if key in seen:
                continue
            seen.add(key)
            candidates = references.find_by_name(str(ingredient_name))
            if not candidates:
                candidates = references.find_by_review_alias(str(ingredient_name))
            if not candidates:
                writer.writerow(
                    {
                        "ingredient_id": ingredient_id,
                        "ingredient_name": ingredient_name,
                        "form": form,
                        "reason": "no_exact_name_match",
                        "review_status": "pending",
                    }
                )
                continue
            for reference in candidates:
                same_form = str(form).strip() == reference.form.strip()
                exact_name = str(ingredient_name).strip() == reference.canonical_name.strip()
                writer.writerow(
                    {
                        "ingredient_id": ingredient_id,
                        "ingredient_name": ingredient_name,
                        "form": form,
                        "candidate_reference_id": reference.reference_id,
                        "candidate_name": reference.canonical_name,
                        "candidate_form": reference.form,
                        "source_dataset": reference.source_dataset,
                        "match_method": "exact" if exact_name else "reference_alias",
                        "reason": (
                            ("exact_candidate" if same_form else "form_state_requires_review")
                            if exact_name
                            else "alias_candidate_requires_review"
                        ),
                        "review_status": "pending",
                    }
                )


_REFERENCE_BASE_RE = re.compile(r"[\[【(（].*$")
_REFERENCE_BRACKET_ALIAS_RE = re.compile(r"[\[【]([^\]】]+)[\]】]")


def _ingredient_review_aliases(name: str) -> set[str]:
    cleaned = name.strip()
    aliases = {cleaned}
    if len(cleaned) > 1 and cleaned.endswith("肉"):
        aliases.add(cleaned[:-1])
    return {alias for alias in aliases if alias}


def _reference_review_aliases(name: str) -> set[str]:
    cleaned = name.strip()
    aliases = {_REFERENCE_BASE_RE.sub("", cleaned).strip()}
    for match in _REFERENCE_BRACKET_ALIAS_RE.finditer(cleaned):
        aliases.update(
            alias.strip()
            for alias in re.split(r"[，、,]", match.group(1))
            if alias.strip()
        )
    return {alias for alias in aliases if alias}


def load_nutrition_references(path: Path) -> NutritionReferenceIndex:
    references = tuple(
        IngredientNutritionReference.model_validate(json.loads(line))
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    return NutritionReferenceIndex(references)


def load_nutrition_crosswalk(
    path: Path,
    references: NutritionReferenceIndex,
) -> NutritionCrosswalkIndex:
    decisions = tuple(
        NutritionCrosswalkDecision.model_validate(json.loads(line))
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    return NutritionCrosswalkIndex(decisions, references)


def build_nutrition_coverage_report(
    references: NutritionReferenceIndex,
    crosswalk: NutritionCrosswalkIndex,
    *,
    ingredient_form_keys,
) -> dict:
    reference_values = references.values
    total_references = len(reference_values)
    non_null_rates = {
        field: round(
            sum(getattr(item.per_100g, field) is not None for item in reference_values)
            / max(total_references, 1),
            4,
        )
        for field in NUTRIENT_FIELDS
    }
    unique_keys = tuple(dict.fromkeys(ingredient_form_keys))
    unresolved = sum(crosswalk.get(ingredient_id, form) is None for ingredient_id, form in unique_keys)
    return {
        "reference_count": total_references,
        "approved_crosswalk_count": crosswalk.approved_count,
        "unresolved_ingredient_count": unresolved,
        "nutrient_non_null_rates": non_null_rates,
    }
