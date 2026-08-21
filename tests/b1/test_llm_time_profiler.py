from copy import deepcopy

from food_agent_v2.b1.llm_time_profiler import (
    LLMStructuredModel,
    generate_time_graph_review,
)
from food_agent_v2.b1.schemas import StepAtom
from food_agent_v2.b1.time_graph_profiler import GeneratedTimeGraph


class _SequenceModel:
    def __init__(self, model_id: str, responses):
        self.model_id = model_id
        self.responses = list(responses)
        self.calls = 0

    def generate(self, payload):
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return deepcopy(response)


class _CaptureClient:
    def __init__(self):
        self.system_prompt = ""

    def invoke(self, role, system_prompt, user_message, response_format):
        self.system_prompt = system_prompt
        return {
            "content": (
                '{"tasks":[{"atom_id":"a","duration_seconds":1,'
                '"task_type":"manual","resources":["cook"],"depends_on":[]}]}'
            )
        }


def test_structured_model_embeds_required_json_schema_in_prompt() -> None:
    client = _CaptureClient()
    model = LLMStructuredModel(
        client=client,
        model_id="model-v1",
        role="unified_review",
        system_prompt="base prompt",
        output_model=GeneratedTimeGraph,
        schema_name="recipe_time_graph",
    )

    model.generate({"recipe_id": 1})

    assert '"depends_on"' in client.system_prompt
    assert '"required"' in client.system_prompt
    assert "recipe_time_graph" in client.system_prompt


def test_time_review_retries_one_failed_graph_generation_and_then_caches(tmp_path) -> None:
    atom = StepAtom(
        atom_id="a-cut",
        source_step_index=1,
        text="切成小块",
        explicit_duration_seconds=None,
        duration_locked=False,
    )
    generator = _SequenceModel(
        "generator-v1",
        (
            {},
            {
                "tasks": [
                    {
                        "atom_id": "a-cut",
                        "duration_seconds": 120,
                        "task_type": "manual",
                        "resources": ["cook"],
                        "depends_on": [],
                    }
                ]
            },
        ),
    )
    verifier = _SequenceModel("verifier-v1", ({"issues": []},))

    report = generate_time_graph_review(
        ((1, "切菜", (atom,)),),
        output=tmp_path / "conflicts.jsonl",
        cache_path=tmp_path / "cache.jsonl",
        generator=generator,
        verifier=verifier,
        max_workers=1,
        max_attempts=2,
    )

    assert report["ready"] == 1
    assert report["conflicts"] == 0
    assert generator.calls == 2
    assert verifier.calls == 1
