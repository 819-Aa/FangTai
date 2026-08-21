from types import SimpleNamespace

from food_agent_v2.b4.schemas import HealthEvaluationReceipt
from food_agent_v2.c3.tool_handler import ToolContext, _evaluate_recipe_health


def test_online_health_evaluation_consumes_condition_relations(monkeypatch) -> None:
    view = SimpleNamespace(
        ingredient_ids=[10, 20],
        ingredient_relations=[
            SimpleNamespace(
                ingredient_id=10,
                condition_type="required",
                choice_group_id=None,
                is_default_choice=True,
                is_process_material=False,
            ),
            SimpleNamespace(
                ingredient_id=20,
                condition_type="optional",
                choice_group_id=None,
                is_default_choice=False,
                is_process_material=False,
            ),
        ],
    )

    class _Builder:
        def build_health_ingredient_view(self, recipe_id):
            return view if recipe_id == 1 else None

    class _Engine:
        def load_relations(self):
            return None

        def evaluate_batch(self, *args, **kwargs):
            raise AssertionError("在线入口不得丢弃条件关系后调用平面 ingredient_ids")

        def evaluate_batch_occurrences(self, recipe_ids, occurrence_map, constraints):
            optional = occurrence_map[1][1]
            assert optional.condition_type == "optional"
            return HealthEvaluationReceipt(
                evaluation_id="eval-1",
                request_id=None,
                retrieval_result_ref=None,
                constraint_set_refs=[],
                participant_recipe_results=[],
                safe_recipe_ids=[1],
                excluded_recipe_ids=[],
                input_fingerprint="fixture",
            )

    class _B2:
        def load(self, expected_build_id=None):
            return None

        def derive_constraints(self, user_id, participant_ref):
            return SimpleNamespace(hard_constraints=[])

    monkeypatch.setattr("food_agent_v2.b3.recipe_views.get_view_builder", lambda: _Builder())
    monkeypatch.setattr("food_agent_v2.b4.HealthRuleEngine", _Engine)
    monkeypatch.setattr("food_agent_v2.b2.UserHealthProfileService", _B2)
    context = ToolContext(
        request_id="request-1",
        build_id="build-1",
        participant_user_mapping={"p1": 1},
    )

    result = _evaluate_recipe_health({"recipe_ids": [1]}, context)

    assert result["safe_recipe_ids"] == [1]
