"""B1 步骤原子构建入口。

本阶段只拆分步骤并锁定原文明确的单值时长；缺失值由离线整菜时间图
生成器补齐，运行时不会再对步骤文本做解析或估算。
"""

from __future__ import annotations

import json
import re
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from food_agent_v2.b1.consumer_views import RecipeStepBindingView
from food_agent_v2.b1.schemas import StepAtom
from food_agent_v2.b1.step_atomizer import atomize_recipe_steps
from food_agent_v2.b1.time_review_decisions import (
    TimeReviewDecision,
    apply_time_review_decisions,
)
from food_agent_v2.core.paths import CLEANED_DIR, CLEANED_RECIPES, PIPELINE_REPORTS_DIR

_NUMBERED_STEP_RE = re.compile(r"(?:第\s*\d+\s*步|步骤\s*\d+)\s*[：:]\s*")
_STEP_DELIMITER_RE = re.compile(r"[；;。\n]+")


class RecipeStepAtomProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    build_id: UUID
    source_manifest_hash: str
    recipe_id: int
    atoms: tuple[StepAtom, ...]


def split_steps(steps_raw: str) -> list[str]:
    """按明确步骤编号、分号、句号和换行分段，保留重复的真实步骤。"""
    if not steps_raw or not steps_raw.strip():
        return []
    numbered_parts = _NUMBERED_STEP_RE.split(steps_raw)
    steps = [
        segment.strip().strip("；;。")
        for part in numbered_parts
        for segment in _STEP_DELIMITER_RE.split(part)
        if segment.strip().strip("；;。")
    ]
    return steps or [steps_raw.strip()]


def build_step_profiles_from_views(
    views: tuple[RecipeStepBindingView, ...],
    *,
    time_decisions: tuple[TimeReviewDecision, ...] = (),
) -> tuple[list[RecipeStepAtomProfile], dict]:
    """把同一构建的步骤绑定视图转换为待补全的原子步骤画像。"""
    profiles: list[RecipeStepAtomProfile] = []
    locked_count = 0
    unlocked_count = 0
    zero_count = 0
    for view in views:
        atoms = atomize_recipe_steps(
            recipe_id=view.recipe_id,
            steps=((step.step_index, step.raw_text) for step in view.steps),
        )
        atoms = apply_time_review_decisions(
            recipe_id=view.recipe_id,
            atoms=atoms,
            decisions=time_decisions,
        )
        locked_count += sum(atom.duration_locked for atom in atoms)
        unlocked_count += sum(not atom.duration_locked for atom in atoms)
        zero_count += sum(atom.explicit_duration_seconds == 0 for atom in atoms)
        profiles.append(
            RecipeStepAtomProfile(
                build_id=view.build_id,
                source_manifest_hash=view.source_manifest_hash,
                recipe_id=view.recipe_id,
                atoms=atoms,
            )
        )
    return profiles, {
        "stage": "step_atomization",
        "total_recipes": len(profiles),
        "locked_atom_count": locked_count,
        "unlocked_atom_count": unlocked_count,
        "zero_atom_count": zero_count,
        "status": "passed",
    }


def build_step_profiles(cleaned_recipes: list[dict]) -> tuple[list[dict], dict]:
    """兼容旧离线入口，但发布新的原子中间态，不再发布旧时间权威字段。"""
    CLEANED_DIR.mkdir(parents=True, exist_ok=True)
    PIPELINE_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    profiles: list[dict] = []
    atom_count = 0
    unlocked_count = 0
    for recipe in cleaned_recipes:
        recipe_id = int(recipe["recipe_id"])
        source_steps = split_steps(str(recipe.get("烹饪步骤", "")))
        atoms = atomize_recipe_steps(
            recipe_id=recipe_id,
            steps=((index, text) for index, text in enumerate(source_steps, start=1)),
        )
        atom_count += len(atoms)
        unlocked_count += sum(not atom.duration_locked for atom in atoms)
        profiles.append(
            {
                "recipe_id": recipe_id,
                "name": recipe.get("名称", ""),
                "atoms": [atom.model_dump(mode="json") for atom in atoms],
            }
        )

    output_path = CLEANED_DIR / "time_profiles.jsonl"
    with output_path.open("w", encoding="utf-8", newline="\n") as handle:
        for profile in profiles:
            handle.write(json.dumps(profile, ensure_ascii=False, sort_keys=True) + "\n")
    return profiles, {
        "stage": "step_atomization",
        "total_recipes": len(profiles),
        "atom_count": atom_count,
        "unlocked_atom_count": unlocked_count,
        "output": str(output_path.relative_to(CLEANED_DIR.parent)),
        "status": "passed",
    }


if __name__ == "__main__":
    with CLEANED_RECIPES.open("r", encoding="utf-8") as handle:
        recipes = [json.loads(line) for line in handle if line.strip()]
    _, report = build_step_profiles(recipes)
    print(json.dumps(report, ensure_ascii=False, indent=2))
