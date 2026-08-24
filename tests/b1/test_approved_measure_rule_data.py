from decimal import Decimal
from pathlib import Path

from food_agent_v2.b1.quantity_normalizer import load_measure_rules

REPO_ROOT = Path(__file__).resolve().parents[2]
MEASURE_RULES_PATH = REPO_ROOT / "data" / "review" / "ingredient_measure_rules.csv"

EXPECTED_DENSITIES = {
    111: ("纯净水", Decimal("1.000")),
    72: ("醋", Decimal("1.000")),
    122: ("牛奶", Decimal("1.020")),
    154: ("柠檬汁", Decimal("1.020")),
    155: ("色拉油", Decimal("0.920")),
    20: ("橄榄油", Decimal("0.913")),
    996: ("烧烤汁", Decimal("1.130")),
}


def test_only_owner_approved_stable_density_rules_are_effective() -> None:
    rules = load_measure_rules(MEASURE_RULES_PATH)

    for ingredient_id, (expected_name, expected_density) in EXPECTED_DENSITIES.items():
        rule = rules.get_density(ingredient_id, "")
        assert rule is not None
        assert rule.ingredient_name == expected_name
        assert rule.mass_density_g_per_ml == expected_density

    for ingredient_id in (100, 166, 583, 607, 125, 42, 474, 567):
        assert rules.get_density(ingredient_id, "") is None


def test_owner_pending_unit_weights_remain_absent() -> None:
    rules = load_measure_rules(MEASURE_RULES_PATH)

    for ingredient_id, unit in ((125, "勺"), (42, "个"), (474, "根"), (567, "个")):
        assert rules.get_unit_weight(ingredient_id, "", unit) is None


def test_generic_vinegar_density_does_not_match_specific_vinegars() -> None:
    rules = load_measure_rules(MEASURE_RULES_PATH)

    assert rules.get_density(72, "") is not None
    for ingredient_id in (432, 173, 607):
        assert rules.get_density(ingredient_id, "") is None
