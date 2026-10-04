from types import SimpleNamespace

import pytest

from food_agent_v2.c3.fast_intent import FastIntentRouter
from food_agent_v2.c3.tool_handler import _retrieval_filters_from_query_plan as handler_filters
from food_agent_v2.c3.tools import _retrieval_filters_from_query_plan as agent_filters


def test_explicit_four_dishes_soup_keeps_structure_requirement():
    intent = FastIntentRouter.route("晚餐四菜一汤，180分钟内完成")
    assert intent.dish_count_requested == 5
    assert "汤" in intent.dish_types


@pytest.mark.parametrize("make_filters", [agent_filters, handler_filters])
def test_mixed_menu_does_not_limit_every_candidate_to_soup(make_filters):
    plan = SimpleNamespace(dish_count_requested=5, dish_types=("汤",), meal_types=("晚餐",))
    filters = make_filters(plan)
    assert filters.dish_type_tags == ()
    assert filters.meal_tags == ("晚餐",)
    plan.dish_count_requested = 1
    assert make_filters(plan).dish_type_tags == ("汤",)
