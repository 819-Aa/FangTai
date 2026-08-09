"""T06 食材解析器黄金用例测试。

组合项、可选项、替代项、数量单位、切法/泡发/去皮处理语、调料组、
非食材说明，以及旧系统暴露的"海鲜菇/红酒醋/素蚝油"名称边界。
"""

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
