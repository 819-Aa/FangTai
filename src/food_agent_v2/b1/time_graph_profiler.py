"""整菜时间任务图生成、双阶段校验和可再生缓存。"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError

from food_agent_v2.b1.schemas import StepAtom, StepTask
from food_agent_v2.b1.step_atomizer import (
    is_passive_wait_text,
    ordered_atoms_hash,
)

TIME_GRAPH_PROMPT_VERSION = "time-graph-v4"
ALLOWED_RESOURCES = frozenset(
    {"cook", "burner", "oven", "steamer", "microwave", "blender", "fridge", "counter"}
)
_CAPACITY_DEVICES = frozenset({"burner", "oven", "steamer", "microwave", "blender"})
_TASK_TYPES = frozenset(
    {"manual", "attended_equipment", "unattended_equipment", "passive", "non_task"}
)
_VERIFIER_CODES = frozenset(
    {
        "STEP_MISMATCH",
        "MISSING_WAIT",
        "INVALID_PARALLELISM",
        "TASK_TYPE_MISMATCH",
        "RESOURCE_MISMATCH",
        "DEPENDENCY_MISMATCH",
        "DURATION_IMPLAUSIBLE",
    }
)


class StructuredModel(Protocol):
    model_id: str

    def generate(self, payload: dict) -> dict:
        """对整道菜执行一次结构化生成。"""


class GeneratedTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    atom_id: str
    duration_seconds: int
    task_type: str
    resources: tuple[str, ...]
    depends_on: tuple[str, ...]


class GeneratedTimeGraph(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tasks: tuple[GeneratedTask, ...]


class VerifierIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: Literal[
        "STEP_MISMATCH",
        "MISSING_WAIT",
        "INVALID_PARALLELISM",
        "TASK_TYPE_MISMATCH",
        "RESOURCE_MISMATCH",
        "DEPENDENCY_MISMATCH",
        "DURATION_IMPLAUSIBLE",
    ]
    atom_ids: tuple[str, ...]


class VerifierResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issues: tuple[VerifierIssue, ...]


class RecipeTimeProfile(BaseModel):
    """通过程序和独立 verifier 的最终单菜任务图。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    recipe_id: int
    recipe_name: str
    step_tasks: tuple[StepTask, ...]


class PublishedRecipeTimeProfile(BaseModel):
    """固定 step_tasks Artifact 的 V2 运行时记录。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    build_id: UUID
    source_manifest_hash: str
    recipe_id: int
    recipe_name: str
    active_seconds: int
    estimated_elapsed_seconds: int
    step_tasks: tuple[StepTask, ...]


class GraphValidationError(ValueError):
    """携带封闭问题代码和相关 atom ID，不写入自由解释。"""

    def __init__(self, issues: tuple[tuple[str, tuple[str, ...]], ...]):
        self.issues = issues
        self.codes = tuple(dict.fromkeys(code for code, _ in issues))
        super().__init__(";".join(self.codes))


class TimeGraphCacheMiss(RuntimeError):
    """普通 rebuild 缺少当前严格缓存键时失败，不在事务内调用模型。"""


class TimeGraphCache:
    """JSONL 可再生构建缓存；不参与 canonical source manifest。"""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else None
        self._entries: dict[str, RecipeTimeProfile] = {}
        if self.path is not None and self.path.exists():
            self._load()

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, cache_key: str) -> RecipeTimeProfile | None:
        return self._entries.get(cache_key)

    def put(self, cache_key: str, profile: RecipeTimeProfile) -> None:
        self.put_many({cache_key: profile})

    def put_many(self, entries: Mapping[str, RecipeTimeProfile]) -> None:
        """Persist a batch in one atomic checkpoint instead of rewriting per recipe."""
        self._entries.update(entries)
        if self.path is not None:
            self._write()

    def _load(self) -> None:
        assert self.path is not None
        for line_number, line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                key = str(record["cache_key"])
                if key in self._entries:
                    raise ValueError(f"重复 cache_key: {key}")
                self._entries[key] = RecipeTimeProfile.model_validate(record["profile"])
            except (KeyError, TypeError, json.JSONDecodeError, ValidationError) as exc:
                raise ValueError(f"时间图缓存第 {line_number} 行无效") from exc

    def _write(self) -> None:
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            for cache_key in sorted(self._entries):
                handle.write(
                    json.dumps(
                        {
                            "cache_key": cache_key,
                            "profile": self._entries[cache_key].model_dump(mode="json"),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
        temporary.replace(self.path)


def time_graph_cache_key(
    recipe_id: int,
    atoms: tuple[StepAtom, ...],
    model_id: str,
    *,
    prompt_version: str = TIME_GRAPH_PROMPT_VERSION,
) -> str:
    """严格散列 recipe ID、ordered atoms、Prompt 版本和模型流水线 ID。"""
    material = json.dumps(
        [recipe_id, ordered_atoms_hash(atoms), prompt_version, model_id],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def time_graph_pipeline_model_id(generator_model_id: str, verifier_model_id: str) -> str:
    return f"generator={generator_model_id}|verifier={verifier_model_id}"


def load_cached_recipe_time_graph(
    *,
    recipe_id: int,
    recipe_name: str,
    atoms: tuple[StepAtom, ...],
    generator_model_id: str,
    verifier_model_id: str,
    cache: TimeGraphCache,
) -> RecipeTimeProfile:
    """只读并复验当前键缓存；缺失时绝不隐式调用在线模型。"""
    pipeline_model_id = time_graph_pipeline_model_id(
        generator_model_id,
        verifier_model_id,
    )
    cache_key = time_graph_cache_key(recipe_id, atoms, pipeline_model_id)
    cached = cache.get(cache_key)
    if cached is None:
        raise TimeGraphCacheMiss(f"recipe_id={recipe_id} 当前时间图缓存缺失")
    if cached.recipe_id != recipe_id or cached.recipe_name != recipe_name:
        raise GraphValidationError((("CACHE_IDENTITY_MISMATCH", ()),))
    validate_time_graph(atoms, cached.step_tasks)
    return cached


def profile_recipe_time_graph(
    recipe_id: int,
    recipe_name: str,
    atoms: tuple[StepAtom, ...],
    model: StructuredModel,
    verifier: StructuredModel,
    cache: TimeGraphCache,
) -> RecipeTimeProfile:
    """整菜生成一次、程序校验一次、独立模型复核一次，成功后缓存。"""
    if not atoms:
        raise GraphValidationError((("EMPTY_ATOM_SET", ()),))
    pipeline_model_id = time_graph_pipeline_model_id(model.model_id, verifier.model_id)
    cache_key = time_graph_cache_key(recipe_id, atoms, pipeline_model_id)
    cached = cache.get(cache_key)
    if cached is not None:
        return load_cached_recipe_time_graph(
            recipe_id=recipe_id,
            recipe_name=recipe_name,
            atoms=atoms,
            generator_model_id=model.model_id,
            verifier_model_id=verifier.model_id,
            cache=cache,
        )

    generator_payload = {
        "recipe_id": recipe_id,
        "recipe_name": recipe_name,
        "atoms": [atom.model_dump(mode="json") for atom in atoms],
    }
    try:
        generated = GeneratedTimeGraph.model_validate(model.generate(generator_payload))
    except (ValidationError, TypeError, ValueError) as exc:
        raise GraphValidationError((("INVALID_GENERATOR_OUTPUT", ()),)) from exc

    tasks = _materialize_tasks(atoms, generated.tasks)
    validate_time_graph(atoms, tasks)
    verifier_payload = {
        **generator_payload,
        "step_tasks": [task.model_dump(mode="json") for task in tasks],
    }
    try:
        checked = VerifierResult.model_validate(verifier.generate(verifier_payload))
    except (ValidationError, TypeError, ValueError) as exc:
        raise GraphValidationError((("INVALID_VERIFIER_OUTPUT", ()),)) from exc
    if checked.issues:
        issues = tuple((issue.code, issue.atom_ids) for issue in checked.issues)
        raise GraphValidationError(issues)

    profile = RecipeTimeProfile(
        recipe_id=recipe_id,
        recipe_name=recipe_name,
        step_tasks=tasks,
    )
    cache.put(cache_key, profile)
    return profile


def validate_time_graph(atoms: tuple[StepAtom, ...], tasks: tuple[StepTask, ...]) -> None:
    """独立验证可发布任务图，供缓存读取和普通 rebuild 重用。"""
    atom_by_id = {atom.atom_id: atom for atom in atoms}
    issues: list[tuple[str, tuple[str, ...]]] = []
    if len(atom_by_id) != len(atoms):
        issues.append(("DUPLICATE_SOURCE_ATOM_ID", ()))
    task_ids = [task.atom_id for task in tasks]
    counts = Counter(task_ids)
    duplicates = tuple(sorted(atom_id for atom_id, count in counts.items() if count > 1))
    missing = tuple(sorted(set(atom_by_id) - set(task_ids)))
    unknown = tuple(sorted(set(task_ids) - set(atom_by_id)))
    if duplicates:
        issues.append(("DUPLICATE_ATOM_ID", duplicates))
    if missing:
        issues.append(("MISSING_ATOM_ID", missing))
    if unknown:
        issues.append(("UNKNOWN_ATOM_ID", unknown))

    for task in tasks:
        atom = atom_by_id.get(task.atom_id)
        if atom is None:
            continue
        if task.text != atom.text:
            issues.append(("ATOM_TEXT_CHANGED", (task.atom_id,)))
        if atom.duration_locked and task.duration_seconds != atom.explicit_duration_seconds:
            issues.append(("EXPLICIT_DURATION_CHANGED", (task.atom_id,)))
        issues.extend(_task_issues(atom, task))
        missing_deps = tuple(sorted(set(task.depends_on) - set(atom_by_id)))
        if missing_deps:
            issues.append(("DEPENDENCY_NOT_FOUND", (task.atom_id, *missing_deps)))
        if task.atom_id in task.depends_on:
            issues.append(("SELF_DEPENDENCY", (task.atom_id,)))
        if len(set(task.depends_on)) != len(task.depends_on):
            issues.append(("DUPLICATE_DEPENDENCY", (task.atom_id,)))
    if not missing and not unknown and not duplicates and _has_cycle(tasks):
        issues.append(("DEPENDENCY_CYCLE", tuple(sorted(task_ids))))
    if issues:
        raise GraphValidationError(tuple(issues))


def _materialize_tasks(
    atoms: tuple[StepAtom, ...], candidates: tuple[GeneratedTask, ...]
) -> tuple[StepTask, ...]:
    atom_by_id = {atom.atom_id: atom for atom in atoms}
    counts = Counter(candidate.atom_id for candidate in candidates)
    issues: list[tuple[str, tuple[str, ...]]] = []
    duplicates = tuple(sorted(atom_id for atom_id, count in counts.items() if count > 1))
    missing = tuple(sorted(set(atom_by_id) - set(counts)))
    unknown = tuple(sorted(set(counts) - set(atom_by_id)))
    if duplicates:
        issues.append(("DUPLICATE_ATOM_ID", duplicates))
    if missing:
        issues.append(("MISSING_ATOM_ID", missing))
    if unknown:
        issues.append(("UNKNOWN_ATOM_ID", unknown))
    if issues:
        raise GraphValidationError(tuple(issues))

    candidate_by_id = {candidate.atom_id: candidate for candidate in candidates}
    tasks: list[StepTask] = []
    for atom in atoms:
        candidate = candidate_by_id[atom.atom_id]
        known_non_task = (
            atom.duration_locked and atom.explicit_duration_seconds == 0
        )
        known_passive = not known_non_task and is_passive_wait_text(atom.text)
        duration = 0 if known_non_task else (
            atom.explicit_duration_seconds
            if atom.duration_locked
            else candidate.duration_seconds
        )
        task_type = (
            "non_task"
            if known_non_task
            else atom.reviewed_task_type
            if atom.reviewed_task_type is not None
            else "passive" if known_passive else candidate.task_type
        )
        if known_non_task:
            resources = ()
        elif atom.reviewed_resources is not None:
            resources = atom.reviewed_resources
        elif known_passive:
            resources = (
                ("fridge",)
                if any(marker in atom.text for marker in ("冰箱", "冷藏", "冷冻"))
                else ("counter",)
            )
        else:
            resources = _normalize_resources(candidate.task_type, candidate.resources)
        raw_task = {
            "atom_id": atom.atom_id,
            "text": atom.text,
            "duration_seconds": duration,
            "task_type": task_type,
            "resources": resources,
            "depends_on": (
                atom.reviewed_depends_on
                if atom.reviewed_depends_on is not None
                else candidate.depends_on
            ),
        }
        pre_issues = _raw_task_issues(atom, raw_task)
        if pre_issues:
            issues.extend(pre_issues)
            continue
        try:
            tasks.append(StepTask.model_validate(raw_task))
        except ValidationError as exc:
            raise GraphValidationError((("INVALID_TASK_OUTPUT", (atom.atom_id,)),)) from exc
    if issues:
        raise GraphValidationError(tuple(issues))
    return tuple(tasks)


def _normalize_resources(task_type: str, resources: tuple[str, ...]) -> tuple[str, ...]:
    """Canonicalize known resources after the model has chosen task semantics."""
    if set(resources) - ALLOWED_RESOURCES:
        return resources
    if task_type == "manual":
        return ("cook", "counter") if "counter" in resources else ("cook",)
    if task_type == "attended_equipment":
        devices = tuple(resource for resource in resources if resource in _CAPACITY_DEVICES)
        return ("cook", *devices)
    if task_type == "unattended_equipment":
        return tuple(resource for resource in resources if resource in _CAPACITY_DEVICES)
    if task_type == "passive":
        return tuple(resource for resource in resources if resource in {"fridge", "counter"})
    if task_type == "non_task":
        return ()
    return resources


def _raw_task_issues(
    atom: StepAtom, task: Mapping[str, object]
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    atom_ids = (atom.atom_id,)
    task_type = task["task_type"]
    duration = task["duration_seconds"]
    resources = tuple(task["resources"])  # type: ignore[arg-type]
    dependencies = tuple(task["depends_on"])  # type: ignore[arg-type]
    issues: list[tuple[str, tuple[str, ...]]] = []
    if task_type not in _TASK_TYPES:
        issues.append(("INVALID_TASK_TYPE", atom_ids))
        return tuple(issues)
    if not isinstance(duration, int) or isinstance(duration, bool):
        issues.append(("INVALID_DURATION", atom_ids))
        return tuple(issues)
    known_non_task = atom.duration_locked and atom.explicit_duration_seconds == 0
    if (task_type == "non_task") != known_non_task:
        issues.append(("NON_TASK_CLASSIFICATION_MISMATCH", atom_ids))
    if task_type == "non_task":
        if duration != 0:
            issues.append(("NON_TASK_NONZERO_DURATION", atom_ids))
    elif duration <= 0:
        issues.append(("NON_POSITIVE_DURATION", atom_ids))
    elif not atom.duration_locked:
        limit = 21600 if task_type in {"manual", "attended_equipment"} else 604800
        if duration > limit:
            issues.append(("ESTIMATED_DURATION_EXCEEDS_LIMIT", atom_ids))
    if len(set(resources)) != len(resources):
        issues.append(("DUPLICATE_RESOURCE", atom_ids))
    invalid_resources = tuple(sorted(set(resources) - ALLOWED_RESOURCES))
    if invalid_resources:
        issues.append(("INVALID_RESOURCE", (atom.atom_id, *invalid_resources)))
    elif not _resource_combination_valid(str(task_type), frozenset(resources)):
        issues.append(("RESOURCE_TASK_TYPE_MISMATCH", atom_ids))
    if atom.atom_id in dependencies:
        issues.append(("SELF_DEPENDENCY", atom_ids))
    return tuple(issues)


def _task_issues(atom: StepAtom, task: StepTask) -> tuple[tuple[str, tuple[str, ...]], ...]:
    return _raw_task_issues(atom, task.model_dump(mode="python"))


def _resource_combination_valid(task_type: str, resources: frozenset[str]) -> bool:
    if task_type == "manual":
        return "cook" in resources and resources <= {"cook", "counter"}
    if task_type == "attended_equipment":
        return "cook" in resources and bool(resources & _CAPACITY_DEVICES) and resources <= (
            {"cook"} | _CAPACITY_DEVICES
        )
    if task_type == "unattended_equipment":
        return bool(resources & _CAPACITY_DEVICES) and resources <= _CAPACITY_DEVICES
    if task_type == "passive":
        return resources <= {"fridge", "counter"}
    return not resources


def _has_cycle(tasks: tuple[StepTask, ...]) -> bool:
    graph = {task.atom_id: task.depends_on for task in tasks}
    state: dict[str, int] = {}

    def visit(atom_id: str) -> bool:
        marker = state.get(atom_id, 0)
        if marker == 1:
            return True
        if marker == 2:
            return False
        state[atom_id] = 1
        for dependency in graph.get(atom_id, ()):
            if dependency in graph and visit(dependency):
                return True
        state[atom_id] = 2
        return False

    return any(visit(atom_id) for atom_id in graph if state.get(atom_id, 0) == 0)
