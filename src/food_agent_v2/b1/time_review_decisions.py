"""Project-owner-approved time atom overrides kept outside regenerable caches."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from food_agent_v2.b1.schemas import StepAtom

ReviewStatus = Literal["pending", "approved", "modified", "rejected"]
DecisionAction = Literal["set_duration", "ignore"]
ReviewedTaskType = Literal[
    "manual",
    "attended_equipment",
    "unattended_equipment",
    "passive",
    "non_task",
]

_FIELDS = (
    "decision_id",
    "recipe_id",
    "source_step_index",
    "atom_text",
    "occurrence",
    "action",
    "duration_seconds",
    "review_status",
    "task_type",
    "resources",
    "depends_on_decision_ids",
)
_ACTIVE_STATUSES = frozenset({"approved", "modified"})
_TASK_TYPES = frozenset({
    "manual", "attended_equipment", "unattended_equipment", "passive", "non_task",
})
_RESOURCES = frozenset({
    "cook", "burner", "oven", "steamer", "microwave", "blender", "fridge", "counter",
})
_DEVICES = frozenset({"burner", "oven", "steamer", "microwave", "blender"})


@dataclass(frozen=True)
class TimeReviewDecision:
    decision_id: str
    recipe_id: int
    source_step_index: int
    atom_text: str
    occurrence: int
    action: DecisionAction
    duration_seconds: int | None
    review_status: ReviewStatus
    task_type: ReviewedTaskType | None = None
    resources: tuple[str, ...] = ()
    depends_on_decision_ids: tuple[str, ...] = ()

    @property
    def target(self) -> tuple[int, int, str, int]:
        return (
            self.recipe_id,
            self.source_step_index,
            self.atom_text,
            self.occurrence,
        )


def load_time_review_decisions(path: Path) -> tuple[TimeReviewDecision, ...]:
    """Load strict decisions; pending/rejected rows remain inert and auditable."""
    decision_path = Path(path)
    if not decision_path.exists():
        return ()
    with decision_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != _FIELDS:
            raise ValueError("时间决定 CSV 表头非法")
        rows = tuple(_parse_row(row, line_number) for line_number, row in enumerate(reader, 2))

    ids = [row.decision_id for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("时间决定 decision_id 重复")
    active_targets = [row.target for row in rows if row.review_status in _ACTIVE_STATUSES]
    if len(active_targets) != len(set(active_targets)):
        raise ValueError("时间决定存在重复目标")
    by_id = {row.decision_id: row for row in rows}
    for row in rows:
        if row.review_status not in _ACTIVE_STATUSES:
            continue
        for dependency_id in row.depends_on_decision_ids:
            dependency = by_id.get(dependency_id)
            if (
                dependency is None
                or dependency.review_status not in _ACTIVE_STATUSES
                or dependency.recipe_id != row.recipe_id
                or dependency.action == "ignore"
            ):
                raise ValueError(
                    f"时间决定 {row.decision_id} 引用了无效依赖 {dependency_id}"
                )
    return rows


def apply_time_review_decisions(
    *,
    recipe_id: int,
    atoms: tuple[StepAtom, ...],
    decisions: tuple[TimeReviewDecision, ...],
) -> tuple[StepAtom, ...]:
    """Apply only approved/modified decisions and fail closed on atom drift."""
    occurrence_counts: dict[tuple[int, str], int] = {}
    atoms_by_target: dict[tuple[int, int, str, int], StepAtom] = {}
    for atom in atoms:
        key = (atom.source_step_index, atom.text)
        occurrence_counts[key] = occurrence_counts.get(key, 0) + 1
        target = (recipe_id, *key, occurrence_counts[key])
        atoms_by_target[target] = atom

    active = tuple(
        decision
        for decision in decisions
        if decision.recipe_id == recipe_id
        and decision.review_status in _ACTIVE_STATUSES
    )
    matched_by_decision_id: dict[str, StepAtom] = {}
    for decision in active:
        atom = atoms_by_target.get(decision.target)
        if atom is None:
            raise ValueError(
                f"时间决定 {decision.decision_id} 未唯一匹配当前 atom"
            )
        matched_by_decision_id[decision.decision_id] = atom

    replacements: dict[str, StepAtom] = {}
    ignored_ids: set[str] = set()
    for decision in active:
        atom = matched_by_decision_id[decision.decision_id]
        if decision.action == "ignore":
            ignored_ids.add(atom.atom_id)
        else:
            assert decision.duration_seconds is not None
            update: dict[str, object] = {
                "explicit_duration_seconds": decision.duration_seconds,
                "duration_locked": True,
            }
            if decision.task_type is not None:
                update.update(
                    {
                        "reviewed_task_type": decision.task_type,
                        "reviewed_resources": decision.resources,
                    }
                )
            if decision.depends_on_decision_ids:
                update["reviewed_depends_on"] = tuple(
                    matched_by_decision_id[dependency_id].atom_id
                    for dependency_id in decision.depends_on_decision_ids
                )
            replacements[atom.atom_id] = atom.model_copy(
                update=update
            )

    return tuple(
        replacements.get(atom.atom_id, atom)
        for atom in atoms
        if atom.atom_id not in ignored_ids
    )


def _parse_row(row: dict[str, str], line_number: int) -> TimeReviewDecision:
    try:
        if set(row) != set(_FIELDS):
            raise ValueError("字段不完整或含额外字段")
        decision_id = row["decision_id"].strip()
        atom_text = row["atom_text"].strip()
        recipe_id = int(row["recipe_id"])
        source_step_index = int(row["source_step_index"])
        occurrence = int(row["occurrence"])
        action = row["action"].strip()
        review_status = row["review_status"].strip()
        task_type = (row["task_type"] or "").strip()
        resources = tuple(
            item.strip()
            for item in (row["resources"] or "").split(";")
            if item.strip()
        )
        dependency_ids = tuple(
            item.strip()
            for item in (row["depends_on_decision_ids"] or "").split(";")
            if item.strip()
        )
        raw_duration = row["duration_seconds"].strip()
        duration = int(raw_duration) if raw_duration else None
        if not decision_id or not atom_text:
            raise ValueError("decision_id/atom_text 不能为空")
        if recipe_id < 1 or source_step_index < 1 or occurrence < 1:
            raise ValueError("recipe/step/occurrence 必须为正整数")
        if action not in {"set_duration", "ignore"}:
            raise ValueError("action 越出封闭词表")
        if review_status not in {"pending", "approved", "modified", "rejected"}:
            raise ValueError("review_status 越出封闭词表")
        if action == "set_duration" and (duration is None or duration <= 0):
            raise ValueError("set_duration 必须提供正整数秒数")
        if action == "ignore" and duration is not None:
            raise ValueError("ignore 不得携带时长")
        if task_type and task_type not in _TASK_TYPES:
            raise ValueError("task_type 越出封闭词表")
        if task_type == "non_task":
            raise ValueError("正时长决定不得标为 non_task")
        if bool(task_type) != bool(resources):
            raise ValueError("task_type 与 resources 必须同时填写")
        if len(resources) != len(set(resources)) or set(resources) - _RESOURCES:
            raise ValueError("resources 非法或重复")
        if task_type and not _resource_combination_valid(task_type, frozenset(resources)):
            raise ValueError("task_type/resources 组合非法")
        if len(dependency_ids) != len(set(dependency_ids)):
            raise ValueError("depends_on_decision_ids 不得重复")
        if decision_id in dependency_ids:
            raise ValueError("决定不得依赖自身")
        if action == "ignore" and (task_type or dependency_ids):
            raise ValueError("ignore 不得携带任务语义")
        return TimeReviewDecision(
            decision_id=decision_id,
            recipe_id=recipe_id,
            source_step_index=source_step_index,
            atom_text=atom_text,
            occurrence=occurrence,
            action=action,  # type: ignore[arg-type]
            duration_seconds=duration,
            review_status=review_status,  # type: ignore[arg-type]
            task_type=task_type or None,  # type: ignore[arg-type]
            resources=resources,
            depends_on_decision_ids=dependency_ids,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"时间决定 CSV 第 {line_number} 行非法") from exc


def _resource_combination_valid(task_type: str, resources: frozenset[str]) -> bool:
    if task_type == "manual":
        return "cook" in resources and resources <= {"cook", "counter"}
    if task_type == "attended_equipment":
        return "cook" in resources and bool(resources & _DEVICES) and resources <= (
            {"cook"} | _DEVICES
        )
    if task_type == "unattended_equipment":
        return bool(resources) and resources <= _DEVICES
    if task_type == "passive":
        return resources <= {"fridge", "counter"}
    return not resources
