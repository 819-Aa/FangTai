"""Mechanical importer for USDA FoodData Central reference archives."""

from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from pathlib import Path

from food_agent_v2.b1.nutrition_reference import (
    IngredientNutritionReference,
    NutritionVector,
    SourceDataset,
)

_DATA_TYPE_BY_SOURCE: dict[SourceDataset, str] = {
    "usda_foundation": "foundation_food",
    "usda_sr_legacy": "sr_legacy_food",
}
_SOURCE_LABELS: dict[SourceDataset, str] = {
    "usda_foundation": "USDA FoodData Central Foundation Foods",
    "usda_sr_legacy": "USDA FoodData Central SR Legacy",
}
_NUTRIENT_IDS: dict[str, tuple[str, ...]] = {
    # Prefer the ordinary reported energy field. Foundation records that omit it
    # fall back to food-specific Atwater energy, then general Atwater energy.
    "energy_kcal": ("1008", "2048", "2047"),
    "protein_g": ("1003",),
    "fat_g": ("1004",),
    "carbohydrate_g": ("1005",),
    "fiber_g": ("1079",),
    "sodium_mg": ("1093",),
    "calcium_mg": ("1087",),
    "iron_mg": ("1089",),
    "cholesterol_mg": ("1253",),
}
_EXPECTED_UNITS = {
    "energy_kcal": "KCAL",
    "protein_g": "G",
    "fat_g": "G",
    "carbohydrate_g": "G",
    "fiber_g": "G",
    "sodium_mg": "MG",
    "calcium_mg": "MG",
    "iron_mg": "MG",
    "cholesterol_mg": "MG",
}


def load_usda_nutrition_archive(
    archive_path: Path,
    *,
    source_dataset: SourceDataset,
    release: str,
    audit: list[dict[str, str]] | None = None,
) -> tuple[IngredientNutritionReference, ...]:
    """Convert one official FDC CSV archive without filling missing nutrients."""
    if source_dataset not in _DATA_TYPE_BY_SOURCE:
        raise ValueError(f"不支持的 USDA 数据集: {source_dataset}")
    path = Path(archive_path)
    with zipfile.ZipFile(path) as archive:
        foods = _read_selected_foods(
            archive,
            expected_data_type=_DATA_TYPE_BY_SOURCE[source_dataset],
        )
        _validate_nutrient_units(archive)
        amounts = _read_food_nutrients(archive, set(foods), audit=audit)

    references = []
    for fdc_id in sorted(foods, key=int):
        description = foods[fdc_id]
        values = {
            field: _first_nutrient(amounts.get(fdc_id, {}), nutrient_ids)
            for field, nutrient_ids in _NUTRIENT_IDS.items()
        }
        references.append(
            IngredientNutritionReference(
                reference_id=f"usda-fdc-{fdc_id}",
                canonical_name=description,
                form=_infer_usda_form(description),
                per_100g=NutritionVector(**values),
                source_name=f"{_SOURCE_LABELS[source_dataset]} ({release})",
                source_url=(
                    "https://fdc.nal.usda.gov/fdc-app.html#/food-details/"
                    f"{fdc_id}/nutrients"
                ),
                source_dataset=source_dataset,
                food_origin="unknown",
            )
        )
    return tuple(references)


def _read_selected_foods(
    archive: zipfile.ZipFile,
    *,
    expected_data_type: str,
) -> dict[str, str]:
    foods: dict[str, str] = {}
    for row in _csv_rows(archive, "food.csv"):
        if row.get("data_type", "").strip() != expected_data_type:
            continue
        fdc_id = row.get("fdc_id", "").strip()
        description = row.get("description", "").strip()
        if not fdc_id or not description:
            raise ValueError("USDA food.csv 存在缺少 fdc_id 或 description 的目标记录")
        if fdc_id in foods:
            raise ValueError(f"USDA food.csv 存在重复 fdc_id: {fdc_id}")
        foods[fdc_id] = description
    return foods


def _validate_nutrient_units(archive: zipfile.ZipFile) -> None:
    expected_by_id = {
        nutrient_id: _EXPECTED_UNITS[field]
        for field, nutrient_ids in _NUTRIENT_IDS.items()
        for nutrient_id in nutrient_ids
    }
    actual_by_id = {
        row.get("id", "").strip(): row.get("unit_name", "").strip().upper()
        for row in _csv_rows(archive, "nutrient.csv")
    }
    for nutrient_id, expected_unit in expected_by_id.items():
        actual = actual_by_id.get(nutrient_id)
        if actual is None:
            continue
        if actual != expected_unit:
            raise ValueError(
                f"USDA nutrient {nutrient_id} 单位异常: expected={expected_unit}, actual={actual}"
            )


def _read_food_nutrients(
    archive: zipfile.ZipFile,
    selected_fdc_ids: set[str],
    *,
    audit: list[dict[str, str]] | None,
) -> dict[str, dict[str, Decimal]]:
    relevant_ids = {
        nutrient_id for ids in _NUTRIENT_IDS.values() for nutrient_id in ids
    }
    amounts: dict[str, dict[str, Decimal]] = {}
    for row in _csv_rows(archive, "food_nutrient.csv"):
        fdc_id = row.get("fdc_id", "").strip()
        nutrient_id = row.get("nutrient_id", "").strip()
        if fdc_id not in selected_fdc_ids or nutrient_id not in relevant_ids:
            continue
        raw_amount = row.get("amount", "").strip()
        if not raw_amount:
            continue
        try:
            amount = Decimal(raw_amount)
        except InvalidOperation as exc:
            raise ValueError(
                f"USDA nutrient amount 非法: fdc_id={fdc_id}, nutrient_id={nutrient_id}"
            ) from exc
        if amount < 0:
            if audit is not None:
                audit.append(
                    {
                        "issue_code": "NEGATIVE_NUTRIENT_TREATED_AS_MISSING",
                        "fdc_id": fdc_id,
                        "nutrient_id": nutrient_id,
                        "raw_amount": raw_amount,
                    }
                )
            continue
        food_values = amounts.setdefault(fdc_id, {})
        previous = food_values.get(nutrient_id)
        if previous is not None and previous != amount:
            raise ValueError(
                f"USDA nutrient 重复且冲突: fdc_id={fdc_id}, nutrient_id={nutrient_id}"
            )
        food_values[nutrient_id] = amount
    return amounts


def _first_nutrient(
    amounts: dict[str, Decimal], nutrient_ids: Iterable[str]
) -> Decimal | None:
    return next((amounts[item] for item in nutrient_ids if item in amounts), None)


def _csv_rows(archive: zipfile.ZipFile, basename: str):
    matching = [name for name in archive.namelist() if name.endswith("/" + basename)]
    if len(matching) != 1:
        raise ValueError(f"USDA archive 中 {basename} 数量不是 1: {matching}")
    with archive.open(matching[0]) as binary:
        with io.TextIOWrapper(binary, encoding="utf-8-sig", newline="") as text:
            yield from csv.DictReader(text)


def _infer_usda_form(description: str) -> str:
    normalized = description.casefold()
    if any(token in normalized for token in (", raw", " raw,")):
        return "raw"
    if any(token in normalized for token in ("dried", "dehydrated", "dry form")):
        return "dried"
    if any(
        token in normalized
        for token in (
            "cooked",
            "boiled",
            "roasted",
            "baked",
            "fried",
            "grilled",
            "steamed",
            "braised",
        )
    ):
        return "cooked"
    return "unspecified"
