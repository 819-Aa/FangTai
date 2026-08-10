"""T17 Artifact 接地测试（INV-005，menu_hash 接地）。

Answer 引用的菜品必须是已校验最终菜单的子集；最终菜单以规范 menu_hash
接地，任何非菜单菜品引用立即 ANSWER_GROUNDING_FAILED。
"""

from food_agent_v2.c3.runner import WorkflowRunner


class TestMenuHashGrounding:
    def test_menu_hash_order_independent(self) -> None:
        assert WorkflowRunner._menu_hash([3, 1, 2]) == WorkflowRunner._menu_hash([1, 2, 3])

    def test_menu_hash_is_64_hex(self) -> None:
        h = WorkflowRunner._menu_hash([101, 202])
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_menu_hash_distinguishes_menus(self) -> None:
        assert WorkflowRunner._menu_hash([1, 2]) != WorkflowRunner._menu_hash([1, 3])

    def test_menu_hash_empty_menu(self) -> None:
        assert WorkflowRunner._menu_hash([]) == WorkflowRunner._menu_hash([])


class TestAnswerGrounding:
    def test_subset_passes(self) -> None:
        assert WorkflowRunner._grounding_error([101, 202], [101, 202, 303]) is None

    def test_empty_dish_ids_passes(self) -> None:
        assert WorkflowRunner._grounding_error([], [1, 2]) is None

    def test_extra_dish_fails(self) -> None:
        err = WorkflowRunner._grounding_error([101, 999], [101, 202])
        assert err is not None
        assert err.error_code == "ANSWER_GROUNDING_FAILED"

    def test_full_cover_fails(self) -> None:
        err = WorkflowRunner._grounding_error([1, 2, 3, 4], [1, 2, 3])
        assert err is not None
        assert err.error_code == "ANSWER_GROUNDING_FAILED"


class TestAnswerDishExtraction:
    def test_top_level(self) -> None:
        assert WorkflowRunner._extract_answer_dish_ids({"dish_ids": [1, 2]}) == [1, 2]

    def test_nested_artifact(self) -> None:
        r = {"AnswerArtifact": {"dish_ids": [4, 5]}}
        assert WorkflowRunner._extract_answer_dish_ids(r) == [4, 5]

    def test_missing(self) -> None:
        assert WorkflowRunner._extract_answer_dish_ids({"content": "文字"}) == []

    def test_non_int_values_ignored(self) -> None:
        assert WorkflowRunner._extract_answer_dish_ids({"dish_ids": [1, "x"]}) == [1]


class TestExtractMenuDecision:
    def test_top_level(self) -> None:
        plan_id, rids = WorkflowRunner._extract_menu_decision(
            {"plan_id": "p1", "recipe_ids": [1, 2, 3]})
        assert plan_id == "p1"
        assert rids == [1, 2, 3]

    def test_nested_artifact(self) -> None:
        plan_id, rids = WorkflowRunner._extract_menu_decision(
            {"MenuDecisionArtifact": {"selected_plan_id": "p2", "recipe_ids": ["4", "5"]}})
        assert plan_id == "p2"
        assert rids == [4, 5]

    def test_empty(self) -> None:
        plan_id, rids = WorkflowRunner._extract_menu_decision({})
        assert plan_id == ""
        assert rids == []
