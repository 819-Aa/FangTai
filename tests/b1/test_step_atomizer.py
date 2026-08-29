from food_agent_v2.b1.step_atomizer import (
    atomize_step,
    is_non_task_text,
    is_passive_wait_text,
    parse_explicit_duration,
)


def test_non_task_phrases_are_locked_to_zero() -> None:
    for text in (
        "准备好所有食材",
        "食材准备",
        "准备食材",
        "放在一旁备用",
        "装盘尽情享用",
        "成品展示",
        "尽情品尝吧",
        "烹饪结束",
        "烹饪结束，即可食用",
        "烹饪结束，趁热享用",
        "结束后",
        "盛出即可食用",
        "趁热享用",
        "无需预热",
        "12寸原料是10寸的1.4倍",
        "面团揉好后的样子",
        "若此时蛋糕还未准备好",
        "9分满即可",
    ):
        atoms = atomize_step(recipe_id=5, source_step_index=1, text=text)
        assert len(atoms) == 1
        assert atoms[0].explicit_duration_seconds == 0
        assert atoms[0].duration_locked is True
        assert is_non_task_text(atoms[0].text)


def test_oven_startup_and_unattended_run_are_separate_atoms() -> None:
    atoms = atomize_step(
        recipe_id=5,
        source_step_index=3,
        text="放入烤箱，烤30分钟",
    )

    assert [atom.text for atom in atoms] == ["放入烤箱", "烤30分钟"]
    assert atoms[0].explicit_duration_seconds is None
    assert atoms[0].duration_locked is False
    assert atoms[1].explicit_duration_seconds == 1800
    assert atoms[1].duration_locked is True


def test_real_wait_fragments_are_not_silently_classified_as_non_tasks() -> None:
    for text in (
        "至其入味",
        "至食材熟透",
        "预热结束后",
        "重复这个步骤三次",
        "使面团全部变硬",
    ):
        atom = atomize_step(recipe_id=7, source_step_index=1, text=text)[0]
        assert atom.explicit_duration_seconds != 0
        assert not is_non_task_text(atom.text)


def test_timed_wait_is_split_from_active_work() -> None:
    atoms = atomize_step(
        recipe_id=6,
        source_step_index=2,
        text="加入融化的黄油，拌匀，静置半小时以上",
    )

    assert [atom.text for atom in atoms] == ["加入融化的黄油", "拌匀", "静置半小时以上"]
    assert [atom.explicit_duration_seconds for atom in atoms] == [None, None, 1800]


def test_repeated_identical_timed_clauses_receive_stable_unique_ids() -> None:
    atoms = atomize_step(
        recipe_id=325,
        source_step_index=3,
        text="搅拌1分钟，搅拌1分钟",
    )

    assert len(atoms) == 2
    assert atoms[0].atom_id != atoms[1].atom_id


def test_atom_id_is_stable_for_equivalent_whitespace_and_punctuation() -> None:
    first = atomize_step(recipe_id=9, source_step_index=2, text="  切成细丝。 ")
    second = atomize_step(recipe_id=9, source_step_index=2, text="切成细丝")

    assert first[0].atom_id == second[0].atom_id


def test_atom_id_changes_when_recipe_or_source_step_changes() -> None:
    base = atomize_step(recipe_id=9, source_step_index=2, text="切成细丝")[0]
    other_recipe = atomize_step(recipe_id=10, source_step_index=2, text="切成细丝")[0]
    other_step = atomize_step(recipe_id=9, source_step_index=3, text="切成细丝")[0]

    assert len({base.atom_id, other_recipe.atom_id, other_step.atom_id}) == 3


def test_english_minute_is_duration_but_fill_fraction_is_not() -> None:
    assert parse_explicit_duration("煎至两面微黄，约2min") == 120
    assert parse_explicit_duration("静置腌制1h") == 3600
    assert parse_explicit_duration("倒入蛋挞液至9分满") is None
    assert parse_explicit_duration("10分钟开始预热") is None


def test_reviewed_ginger_recipe_locks_english_hour_and_minute() -> None:
    wait_atoms = atomize_step(
        recipe_id=1138,
        source_step_index=2,
        text="将姜片加入红糖混合均匀，静置腌制1h后红糖化为汁水",
    )
    cook_atoms = atomize_step(
        recipe_id=1138,
        source_step_index=3,
        text="开中小火加热，炒至糖液比较粘稠，约30min",
    )

    assert any(atom.explicit_duration_seconds == 3600 for atom in wait_atoms)
    assert any(atom.explicit_duration_seconds == 1800 for atom in cook_atoms)


def test_completion_transition_with_real_work_is_not_non_task() -> None:
    text = "烹饪结束后取出鸡胸肉，起锅热油，放入鸡胸肉煎约2min"

    atoms = atomize_step(recipe_id=65, source_step_index=3, text=text)

    assert all(atom.explicit_duration_seconds != 0 for atom in atoms)
    assert not any(is_non_task_text(atom.text) for atom in atoms)
    assert any(atom.explicit_duration_seconds == 120 for atom in atoms)


def test_manual_work_and_following_proof_are_separate_atoms() -> None:
    atoms = atomize_step(
        recipe_id=840,
        source_step_index=3,
        text="揉至面团光滑，装入盆内发酵至两倍大",
    )

    assert [atom.text for atom in atoms] == ["揉至面团光滑，装入盆内", "发酵至两倍大"]
    assert not is_passive_wait_text(atoms[0].text)
    assert is_passive_wait_text(atoms[1].text)


def test_completed_cold_storage_is_manual_transition_not_passive_wait() -> None:
    text = "冷藏结束后取出面团，在料理台上撒黄豆粉并擀成长方形"

    assert not is_passive_wait_text(text)


def test_wait_duration_suffix_stays_attached_to_the_wait() -> None:
    atoms = atomize_step(
        recipe_id=1944,
        source_step_index=7,
        text="排气整形切块，再次醒发成2倍大，用了1小时",
    )

    assert [atom.text for atom in atoms] == [
        "排气整形切块",
        "再次醒发成2倍大，用了1小时",
    ]
    assert atoms[1].explicit_duration_seconds == 3600
    assert is_passive_wait_text(atoms[1].text)


def test_approximate_duration_suffix_stays_attached_to_second_proof() -> None:
    atoms = atomize_step(
        recipe_id=1092,
        source_step_index=9,
        text="放蒸盘里进行二次发酵，10分钟左右",
    )

    assert [atom.text for atom in atoms] == ["放蒸盘里", "进行二次发酵，10分钟左右"]
    assert atoms[1].explicit_duration_seconds == 600
    assert is_passive_wait_text(atoms[1].text)


def test_timed_cold_water_soak_is_passive() -> None:
    atom = atomize_step(
        recipe_id=1822,
        source_step_index=1,
        text="干荷叶用冷水泡10分钟",
    )[0]

    assert atom.explicit_duration_seconds == 600
    assert is_passive_wait_text(atom.text)


def test_chinese_ten_minutes_is_explicit_duration() -> None:
    assert parse_explicit_duration("腌制十分钟") == 600


def test_completed_fermentation_followed_by_manual_work_is_not_passive() -> None:
    assert not is_passive_wait_text("把发酵好的面团揉面排气2分钟")
    assert not is_passive_wait_text("发酵好的面团拉开里面如海绵")


def test_prepared_ingredient_adjective_is_not_split_as_a_new_wait() -> None:
    atoms = atomize_step(
        recipe_id=1139,
        source_step_index=6,
        text="放上腌制好的带子",
    )

    assert [atom.text for atom in atoms] == ["放上腌制好的带子"]
    assert not is_passive_wait_text(atoms[0].text)


def test_shape_commentary_is_non_task() -> None:
    assert is_non_task_text("发酵好后不至于变成没有嘴的小胖子")
    assert is_non_task_text("影响造型")
    assert is_non_task_text("还要移位，影响造型")


def test_bare_proof_start_and_condition_are_passive() -> None:
    assert is_passive_wait_text("进行发酵")
    assert is_passive_wait_text("等面团大约膨胀到1.5倍")
    assert is_passive_wait_text("面粉发酵约2倍大时")
    assert is_passive_wait_text(
        "进行发酵，发酵2.5倍大小，用手指戳一个洞不回缩，面团发酵好"
    )


def test_wait_completion_then_manual_work_is_split() -> None:
    atoms = atomize_step(
        recipe_id=860,
        source_step_index=1,
        text="用纯净水泡发后顺纹理撕开",
    )

    assert [atom.text for atom in atoms] == ["用纯净水泡发", "顺纹理撕开"]
    assert is_passive_wait_text(atoms[0].text)
    assert not is_passive_wait_text(atoms[1].text)


def test_timed_wait_then_manual_work_is_split_without_losing_duration() -> None:
    atoms = atomize_step(
        recipe_id=675,
        source_step_index=2,
        text="浸泡2小时取出切块即可食用",
    )

    assert [atom.text for atom in atoms] == ["浸泡2小时", "取出切块即可食用"]
    assert atoms[0].explicit_duration_seconds == 7200
    assert is_passive_wait_text(atoms[0].text)
    assert atoms[1].explicit_duration_seconds is None


def test_duration_only_suffix_is_attached_to_preceding_active_task() -> None:
    atoms = atomize_step(
        recipe_id=65,
        source_step_index=3,
        text="放入鸡胸肉煎至两面微黄，约2min",
    )

    assert [atom.text for atom in atoms] == ["放入鸡胸肉煎至两面微黄，约2min"]
    assert atoms[0].explicit_duration_seconds == 120


def test_device_completion_wait_is_not_forced_to_counter_passive() -> None:
    assert not is_passive_wait_text("待烹饪结束取出")
    assert not is_passive_wait_text("预热结束后，将蒸烤盘放入第2层")


def test_device_loading_and_unattended_run_are_separate_without_explicit_time() -> None:
    atoms = atomize_step(
        recipe_id=305,
        source_step_index=6,
        text="将炖盅放入一体机下层，开始蒸制",
    )

    assert [atom.text for atom in atoms] == ["将炖盅放入一体机下层", "开始蒸制"]
    assert not is_passive_wait_text(atoms[1].text)


def test_consecutive_setup_clauses_after_proof_remain_one_device_context() -> None:
    atoms = atomize_step(
        recipe_id=348,
        source_step_index=9,
        text=(
            "将花卷摆在蒸格上进行二次发酵，放入智能烹饪设备第2层，"
            "开启电源，取出水箱加满水，选择开始烹饪，按屏幕提示操作"
        ),
    )

    assert [atom.text for atom in atoms] == [
        "将花卷摆在蒸格上",
        "进行二次发酵",
        "放入智能烹饪设备第2层，开启电源，取出水箱加满水，选择开始烹饪，按屏幕提示操作",
    ]


def test_reviewed_semantic_split_does_not_invalidate_unreviewed_recipe_atoms() -> None:
    atoms = atomize_step(
        recipe_id=1,
        source_step_index=9,
        text="放蒸盘里进行二次发酵，10分钟左右",
    )

    assert [atom.text for atom in atoms] == ["放蒸盘里进行二次发酵", "10分钟左右"]


def test_reviewed_parser_and_non_task_fix_do_not_change_unreviewed_atom_hash_inputs() -> None:
    legacy_completion = atomize_step(
        recipe_id=1,
        source_step_index=3,
        text="烹饪结束后取出食材，切片",
    )
    legacy_english_duration = atomize_step(
        recipe_id=1,
        source_step_index=4,
        text="煎至两面微黄，约2min",
    )

    assert legacy_completion[0].explicit_duration_seconds == 0
    assert legacy_english_duration[0].explicit_duration_seconds is None
