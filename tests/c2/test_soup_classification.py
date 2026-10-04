import pytest

from food_agent_v2.c2.planner import MenuPlanner


@pytest.mark.parametrize(
    "name,expected",
    [
        ("上汤娃娃菜", "main"),
        ("高汤焖豆腐", "main"),
        ("上汤鸡蛋汤", "soup"),
        ("西红柿蛋花汤", "soup"),
        ("麻婆蛋羹", "main"),
        ("虾仁蒸蛋羹", "main"),
        ("鸡蛋羹", "main"),
        ("西红柿蛋花羹", "soup"),
        ("西红柿豆腐羹", "soup"),
    ],
)
def test_stock_preparation_does_not_replace_a_soup_slot(name, expected):
    assert MenuPlanner._classify_dish_type(name) == expected
