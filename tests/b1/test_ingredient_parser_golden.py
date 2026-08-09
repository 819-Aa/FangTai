"""T06 食材解析器黄金用例测试。

组合项、可选项、替代项、数量单位、切法/泡发/去皮处理语、调料组、
非食材说明，以及旧系统暴露的"海鲜菇/红酒醋/素蚝油"名称边界。
"""

import pytest

from food_agent_v2.b1.ingredient_parser import parse_ingredients


def names(ingredients_raw: str) -> list[str]:
    return [o.name_clean for o in parse_ingredients(ingredients_raw) if not o.is_note]


class TestParserGolden:
    def test_composition_ref(self) -> None:
        occs = parse_ingredients("主料：排骨260克；酱料见（蒜蓉酱）")
        refs = [o for o in occs if o.composition_ref]
        assert len(refs) == 1
        assert "蒜蓉酱" in refs[0].composition_ref

    def test_optional_marker(self) -> None:
        occs = parse_ingredients("姜片5克（可选）；盐适量")
        opt = [o for o in occs if o.name_clean == "姜片"]
        assert len(opt) == 1 and opt[0].is_optional is True

    def test_alternatives_share_choice_group(self) -> None:
        occs = [o for o in parse_ingredients("猪肉或牛肉300克") if not o.is_note]
        assert len(occs) == 2
        assert occs[0].choice_group_id == occs[1].choice_group_id
        assert {o.name_clean for o in occs} == {"猪肉", "牛肉"}

    def test_quantity_unit_prefix(self) -> None:
        assert names("盐2克") == ["盐"]

    def test_quantity_unit_suffix_with_processing(self) -> None:
        occs = parse_ingredients("梨肉1000g（（切块））")
        assert occs[0].name_clean == "梨肉"
        assert occs[0].quantity_raw is not None

    def test_processing_parens_stripped(self) -> None:
        assert names("梅干菜(浸泡2小时)60g（干重）") == ["梅干菜"]

    def test_group_prefix_recorded(self) -> None:
        occs = parse_ingredients("主料：鲈鱼600克；辅料：姜丝10克")
        non_notes = [o for o in occs if not o.is_note]
        assert non_notes[0].group == "主料"
        assert non_notes[1].group == "辅料"
        assert {o.name_clean for o in non_notes} == {"鲈鱼", "姜丝"}

    def test_non_ingredient_note(self) -> None:
        occs = parse_ingredients("盐适量；少许")
        notes = [o for o in occs if o.is_note]
        assert any(o.name_clean == "少许" for o in notes)

    def test_name_boundary_seafood_mushroom(self) -> None:
        # 海鲜菇 是一个整体，不能被拆成 海鲜/菇。
        assert names("海鲜菇200克") == ["海鲜菇"]

    def test_name_boundary_wine_vinegar(self) -> None:
        # 红酒醋 是一个整体，不能被拆成 红酒/醋。
        assert names("红酒醋10毫升") == ["红酒醋"]

    def test_name_boundary_vegetarian_oyster(self) -> None:
        # 素蚝油 是一个整体，不能被拆成 素/蚝油。
        assert names("素蚝油10克") == ["素蚝油"]

    def test_fraction_unit_stripped(self) -> None:
        assert names("酱油1/2t") == ["酱油"]

    def test_english_units_stripped(self) -> None:
        assert names("橄榄油30mL") == ["橄榄油"]
        assert names("食用油1.5T") == ["食用油"]

    def test_package_and_count_units_stripped(self) -> None:
        assert names("内酯豆腐1盒") == ["内酯豆腐"]
        assert names("枸杞10粒") == ["枸杞"]
        assert names("香菇8朵") == ["香菇"]

    def test_doubled_unit_stripped(self) -> None:
        assert names("姜2片片") == ["姜"]

    @pytest.mark.parametrize(
        ("fragment", "expected_name", "expected_quantity"),
        [
            ("菠萝半个", "菠萝", "半个"),
            ("胡萝卜小半根", "胡萝卜", "小半根"),
            ("葱两根", "葱", "两根"),
            ("姜片两片", "姜片", "两片"),
            ("料酒一勺", "料酒", "一勺"),
            ("鲜松茸两个", "鲜松茸", "两个"),
        ],
    )
    def test_chinese_number_quantity_suffix_stripped(
        self,
        fragment: str,
        expected_name: str,
        expected_quantity: str,
    ) -> None:
        occurrence = parse_ingredients(fragment)[0]
        assert occurrence.name_clean == expected_name
        assert occurrence.quantity_raw == expected_quantity

    @pytest.mark.parametrize("fragment", ["白糖各", "水淀粉各", "缤纷果蔬粉各"])
    def test_group_quantifier_suffix_is_not_part_of_identity(self, fragment: str) -> None:
        assert names(fragment) == [fragment.removesuffix("各")]

    @pytest.mark.parametrize(
        ("fragment", "expected"),
        [
            ("芝士少許", "芝士"),
            ("寿司紫菜数张", "寿司紫菜"),
            ("饺子皮数张", "饺子皮"),
        ],
    )
    def test_traditional_and_indefinite_quantity_suffix_stripped(
        self, fragment: str, expected: str
    ) -> None:
        assert names(fragment) == [expected]

    def test_incomplete_fraction_remnant_stripped(self) -> None:
        assert names("高汤块1/") == ["高汤块"]

    def test_legal_digit_name_preserved(self) -> None:
        # T55面粉 是真实名称，数字不是数量，必须保留。
        assert names("T55面粉300克") == ["T55面粉"]

    def test_form_attribute_detected(self) -> None:
        occs = parse_ingredients("姜丝5克")
        assert occs[0].name_clean == "姜丝"
        assert occs[0].form == "丝"

    @pytest.mark.parametrize("fragment", ["切3cm段））", "切丝））", "切块））"])
    def test_malformed_processing_fragment_is_not_an_ingredient(self, fragment: str) -> None:
        assert names(fragment) == []

    @pytest.mark.parametrize(
        "fragment",
        ["洗净划花刀））", "洗净", "切小块））", "去皮））", "去内脏洗净", "洗净切4段)）"],
    )
    def test_standalone_preparation_instruction_is_not_an_ingredient(self, fragment: str) -> None:
        assert names(fragment) == []

    @pytest.mark.parametrize("fragment", ["冷冻4小时)", "根和叶分开））"])
    def test_standalone_state_instruction_is_not_an_ingredient(self, fragment: str) -> None:
        assert names(fragment) == []

    @pytest.mark.parametrize("fragment", ["去虾须））", "去脚））", "取净肉））"])
    def test_malformed_standalone_action_is_not_an_ingredient(self, fragment: str) -> None:
        assert names(fragment) == []

    @pytest.mark.parametrize("fragment", ["辅料：包子皮材料", "肉馅材料"])
    def test_fixed_source_group_heading_is_not_an_ingredient(self, fragment: str) -> None:
        assert names(fragment) == []

    def test_qualitative_count_suffix_is_not_part_of_identity(self) -> None:
        assert names("新鲜香菇若干只") == ["新鲜香菇"]

    def test_fixed_compound_mixture_splits_into_real_ingredients(self) -> None:
        assert names("蜂蜜加油混合40毫升") == ["蜂蜜", "油"]

    @pytest.mark.parametrize(
        ("fragment", "expected"),
        [
            ("青红椒20克", [("青椒", None), ("红椒", None)]),
            ("青红椒丝20克", [("青椒", "丝"), ("红椒", "丝")]),
            ("葱姜10克", [("葱", None), ("姜", None)]),
            ("姜葱末10克", [("姜", "末"), ("葱", "末")]),
            ("葱姜蒜各5克", [("葱", None), ("姜", None), ("蒜", None)]),
            ("葱姜汁8克", [("葱", "汁"), ("姜", "汁")]),
            ("葱姜水10克", [("葱", None), ("姜", None), ("水", None)]),
        ],
    )
    def test_fixed_compound_names_split_members(
        self,
        fragment: str,
        expected: list[tuple[str, str | None]],
    ) -> None:
        occurrences = [item for item in parse_ingredients(fragment) if not item.is_note]
        assert [(item.name_clean, item.form) for item in occurrences] == expected

    def test_oil_fritter_is_a_whole_ingredient_not_a_form(self) -> None:
        occs = parse_ingredients("油条1根")
        assert [(o.name_clean, o.form) for o in occs if not o.is_note] == [("油条", None)]
