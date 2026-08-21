from __future__ import annotations

from copy import deepcopy

import pytest

from food_agent_v2.b1.schemas import StepAtom
from food_agent_v2.b1.time_graph_profiler import (
    GraphValidationError,
    TimeGraphCache,
    profile_recipe_time_graph,
)


class FakeStructuredModel:
    def __init__(self, model_id: str, response: dict):
        self.model_id = model_id
        self.response = response
        self.calls: list[dict] = []

    def generate(self, payload: dict) -> dict:
        self.calls.append(payload)
        return deepcopy(self.response)


def _atoms() -> tuple[StepAtom, ...]:
    return (
        StepAtom(
            atom_id="a-cut",
            source_step_index=1,
            text="切成小块",
            explicit_duration_seconds=None,
            duration_locked=False,
        ),
        StepAtom(
            atom_id="a-place",
            source_step_index=2,
            text="放入烤箱",
            explicit_duration_seconds=None,
            duration_locked=False,
        ),
        StepAtom(
            atom_id="a-bake",
            source_step_index=2,
            text="烤30分钟",
            explicit_duration_seconds=1800,
            duration_locked=True,
        ),
    )


def _valid_tasks() -> list[dict]:
    return [
        {
            "atom_id": "a-cut",
            "duration_seconds": 120,
            "task_type": "manual",
            "resources": ["cook"],
            "depends_on": [],
        },
        {
            "atom_id": "a-place",
            "duration_seconds": 20,
            "task_type": "manual",
            "resources": ["cook"],
            "depends_on": ["a-cut"],
        },
        {
            "atom_id": "a-bake",
            "duration_seconds": 999,
            "task_type": "unattended_equipment",
            "resources": ["oven"],
            "depends_on": ["a-place"],
        },
    ]


def _profile(tasks: list[dict], *, cache: TimeGraphCache | None = None):
    generator = FakeStructuredModel("generator-v1", {"tasks": tasks})
    verifier = FakeStructuredModel("verifier-v1", {"issues": []})
    profile = profile_recipe_time_graph(
        recipe_id=11,
        recipe_name="烤蔬菜",
        atoms=_atoms(),
        model=generator,
        verifier=verifier,
        cache=cache if cache is not None else TimeGraphCache(),
    )
    return profile, generator, verifier


def test_one_whole_recipe_generation_then_independent_verification() -> None:
    profile, generator, verifier = _profile(_valid_tasks())

    assert len(generator.calls) == 1
    assert len(verifier.calls) == 1
    assert {atom["atom_id"] for atom in generator.calls[0]["atoms"]} == {
        "a-cut",
        "a-place",
        "a-bake",
    }
    assert profile.step_tasks[2].duration_seconds == 1800
    assert profile.step_tasks[2].text == "烤30分钟"


def test_manual_task_drops_device_occupancy_but_keeps_cook() -> None:
    tasks = _valid_tasks()
    tasks[0]["resources"] = ["cook", "oven"]

    profile, _, _ = _profile(tasks)

    assert profile.step_tasks[0].resources == ("cook",)


def test_same_cache_key_does_not_repeat_either_model_call() -> None:
    cache = TimeGraphCache()
    first, generator, verifier = _profile(_valid_tasks(), cache=cache)
    second, second_generator, second_verifier = _profile(_valid_tasks(), cache=cache)

    assert first == second
    assert len(generator.calls) == 1
    assert len(verifier.calls) == 1
    assert second_generator.calls == []
    assert second_verifier.calls == []


def test_cache_put_many_is_persisted_as_one_reloadable_checkpoint(tmp_path) -> None:
    cache_path = tmp_path / "time-cache.jsonl"
    cache = TimeGraphCache(cache_path)
    first, _, _ = _profile(_valid_tasks())
    second = first.model_copy(update={"recipe_id": 12, "recipe_name": "烤南瓜"})

    cache.put_many({"key-1": first, "key-2": second})
    reloaded = TimeGraphCache(cache_path)

    assert len(reloaded) == 2
    assert reloaded.get("key-1") == first
    assert reloaded.get("key-2") == second


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (lambda rows: rows.pop(), "MISSING_ATOM_ID"),
        (lambda rows: rows.append(deepcopy(rows[0])), "DUPLICATE_ATOM_ID"),
        (lambda rows: rows[0].update(resources=["wok"]), "INVALID_RESOURCE"),
        (lambda rows: rows[0].update(duration_seconds=-1), "NON_POSITIVE_DURATION"),
        (lambda rows: rows[0].update(duration_seconds=21601), "ESTIMATED_DURATION_EXCEEDS_LIMIT"),
        (lambda rows: rows[0].update(depends_on=["missing"]), "DEPENDENCY_NOT_FOUND"),
        (lambda rows: rows[0].update(depends_on=["a-cut"]), "SELF_DEPENDENCY"),
    ],
)
def test_program_rejects_invalid_generated_graph(mutation, expected_code: str) -> None:
    tasks = _valid_tasks()
    mutation(tasks)

    with pytest.raises(GraphValidationError) as caught:
        _profile(tasks)

    assert expected_code in caught.value.codes


def test_program_rejects_dependency_cycle() -> None:
    tasks = _valid_tasks()
    tasks[0]["depends_on"] = ["a-bake"]

    with pytest.raises(GraphValidationError) as caught:
        _profile(tasks)

    assert "DEPENDENCY_CYCLE" in caught.value.codes


def test_known_resource_noise_is_canonicalized_from_task_type() -> None:
    tasks = _valid_tasks()
    tasks[0]["resources"] = []
    tasks[2]["resources"] = ["cook", "oven"]

    profile, _, _ = _profile(tasks)

    assert profile.step_tasks[0].resources == ("cook",)
    assert profile.step_tasks[2].resources == ("oven",)


def test_known_non_task_atom_overrides_model_task_type_duration_and_resources() -> None:
    atom = StepAtom(
        atom_id="a-finish",
        source_step_index=1,
        text="烹饪结束，即可食用",
        explicit_duration_seconds=0,
        duration_locked=True,
    )
    generator = FakeStructuredModel(
        "generator-v1",
        {
            "tasks": [
                {
                    "atom_id": "a-finish",
                    "duration_seconds": 30,
                    "task_type": "manual",
                    "resources": ["cook", "oven"],
                    "depends_on": [],
                }
            ]
        },
    )
    verifier = FakeStructuredModel("verifier-v1", {"issues": []})

    profile = profile_recipe_time_graph(
        recipe_id=11,
        recipe_name="完成提示",
        atoms=(atom,),
        model=generator,
        verifier=verifier,
        cache=TimeGraphCache(),
    )

    assert profile.step_tasks[0].task_type == "non_task"
    assert profile.step_tasks[0].duration_seconds == 0
    assert profile.step_tasks[0].resources == ()


def test_known_passive_wait_overrides_invalid_equipment_resource_semantics() -> None:
    atom = StepAtom(
        atom_id="a-ferment",
        source_step_index=1,
        text="发酵2小时",
        explicit_duration_seconds=7200,
        duration_locked=True,
    )
    generator = FakeStructuredModel(
        "generator-v1",
        {
            "tasks": [
                {
                    "atom_id": "a-ferment",
                    "duration_seconds": 7200,
                    "task_type": "unattended_equipment",
                    "resources": [],
                    "depends_on": [],
                }
            ]
        },
    )
    verifier = FakeStructuredModel("verifier-v1", {"issues": []})

    profile = profile_recipe_time_graph(
        recipe_id=12,
        recipe_name="发酵面团",
        atoms=(atom,),
        model=generator,
        verifier=verifier,
        cache=TimeGraphCache(),
    )

    assert profile.step_tasks[0].task_type == "passive"
    assert profile.step_tasks[0].resources == ("counter",)


def test_closed_verifier_issue_prevents_ready_profile_and_cache_write() -> None:
    cache = TimeGraphCache()
    generator = FakeStructuredModel("generator-v1", {"tasks": _valid_tasks()})
    verifier = FakeStructuredModel(
        "verifier-v1",
        {"issues": [{"code": "INVALID_PARALLELISM", "atom_ids": ["a-cut", "a-bake"]}]},
    )

    with pytest.raises(GraphValidationError) as caught:
        profile_recipe_time_graph(
            recipe_id=11,
            recipe_name="烤蔬菜",
            atoms=_atoms(),
            model=generator,
            verifier=verifier,
            cache=cache,
        )

    assert caught.value.codes == ("INVALID_PARALLELISM",)
    assert len(cache) == 0
