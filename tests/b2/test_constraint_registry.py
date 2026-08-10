"""T10 封闭约束代码注册表测试。

未知疾病/过敏/指标/生理阶段不得动态生成代码（fail-closed）；备孕等已知但
无批准代码的阶段必须被显式识别且不产生硬约束。
"""

from food_agent_v2.b2.constraint_registry import (
    ALLOWED_CONSTRAINT_CODES,
    SPECIAL_STAGE_RULES,
    allergy_to_constraint_code,
    disease_to_constraint_code,
    indicator_to_constraint_code,
    is_allowed_code,
    special_group_to_constraint_code,
    special_stage_status,
)


class TestClosedRegistry:
    def test_known_allergy_maps_to_closed_code(self) -> None:
        assert allergy_to_constraint_code("花生") == "allergy_peanut"
        assert allergy_to_constraint_code("海鲜") == "allergy_seafood"
        assert is_allowed_code(allergy_to_constraint_code("牛奶"))

    def test_unknown_allergy_returns_none_not_dynamic(self) -> None:
        assert allergy_to_constraint_code("某种未知过敏") is None

    def test_unknown_disease_returns_none_not_dynamic(self) -> None:
        assert disease_to_constraint_code("某种未知疾病") is None

    def test_known_disease_maps(self) -> None:
        assert disease_to_constraint_code("高血压") == "disease_hypertension"

    def test_all_codes_are_closed(self) -> None:
        for code in ALLOWED_CONSTRAINT_CODES:
            assert is_allowed_code(code)

    def test_dynamic_code_not_allowed(self) -> None:
        assert is_allowed_code("allergy_未知食材") is False

    def test_special_stage_preparing_pregnancy_known_but_no_code(self) -> None:
        code, known = special_stage_status("备孕")
        assert known is True
        assert code is None
        assert "备孕" in SPECIAL_STAGE_RULES

    def test_unknown_special_stage_not_known(self) -> None:
        code, known = special_stage_status("某种未知阶段")
        assert known is False
        assert code is None

    def test_pregnant_maps_to_group_code(self) -> None:
        assert special_group_to_constraint_code("孕妇") == "group_pregnancy"
        assert special_group_to_constraint_code("哺乳期") == "group_lactation"

    def test_indicator_metric_maps(self) -> None:
        assert indicator_to_constraint_code("空腹血糖") == "indicator_high_glucose"
        assert indicator_to_constraint_code("尿酸_mmol/L") == "indicator_high_uric_acid"
