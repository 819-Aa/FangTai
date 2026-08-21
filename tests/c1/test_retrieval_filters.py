from food_agent_v2.c1.filters import RetrievalFilters


def _payload(**overrides):
    payload = {
        "meal_tags": ["晚餐"],
        "population_tags": ["老人"],
        "dish_type_tags": ["汤"],
        "taste_tags": ["清淡"],
        "cuisine_tags": ["家常菜"],
        "scenario_tags": ["暖胃"],
        "ingredient_names": ["豆腐", "番茄"],
    }
    payload.update(overrides)
    return payload


def test_same_field_is_or_and_different_fields_are_and() -> None:
    filters = RetrievalFilters(
        meal_tags=("早餐", "晚餐"),
        population_tags=("老人",),
        dish_type_tags=("汤",),
        taste_tags=("清淡",),
    )

    assert filters.matches_payload(_payload()) is True
    assert filters.matches_payload(_payload(meal_tags=["午餐"])) is False
    assert filters.matches_payload(_payload(population_tags=["儿童"])) is False
    assert filters.matches_payload(_payload(taste_tags=["香辣"])) is False


def test_include_and_exclude_ingredients_apply_to_the_same_payload_truth() -> None:
    filters = RetrievalFilters(
        include_ingredients=("豆腐", "鸡蛋"),
        exclude_ingredients=("辣椒",),
    )

    assert filters.matches_payload(_payload()) is True
    assert filters.matches_payload(_payload(ingredient_names=["鸡肉"])) is False
    assert filters.matches_payload(_payload(ingredient_names=["豆腐", "辣椒"])) is False


def test_qdrant_filter_has_equivalent_must_and_must_not_conditions() -> None:
    filters = RetrievalFilters(
        meal_tags=("早餐", "早午餐"),
        population_tags=("老人",),
        include_ingredients=("豆腐",),
        exclude_ingredients=("辣椒",),
    )

    qdrant_filter = filters.to_qdrant_filter()

    assert [condition.key for condition in qdrant_filter.must] == [
        "meal_tags",
        "population_tags",
        "ingredient_names",
    ]
    assert [condition.key for condition in qdrant_filter.must_not] == [
        "ingredient_names"
    ]
