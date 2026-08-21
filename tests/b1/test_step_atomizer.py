from food_agent_v2.b1.step_atomizer import atomize_step, is_non_task_text


def test_non_task_phrases_are_locked_to_zero() -> None:
    for text in ("准备好所有食材", "放在一旁备用", "装盘尽情享用"):
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


def test_atom_id_is_stable_for_equivalent_whitespace_and_punctuation() -> None:
    first = atomize_step(recipe_id=9, source_step_index=2, text="  切成细丝。 ")
    second = atomize_step(recipe_id=9, source_step_index=2, text="切成细丝")

    assert first[0].atom_id == second[0].atom_id


def test_atom_id_changes_when_recipe_or_source_step_changes() -> None:
    base = atomize_step(recipe_id=9, source_step_index=2, text="切成细丝")[0]
    other_recipe = atomize_step(recipe_id=10, source_step_index=2, text="切成细丝")[0]
    other_step = atomize_step(recipe_id=9, source_step_index=3, text="切成细丝")[0]

    assert len({base.atom_id, other_recipe.atom_id, other_step.atom_id}) == 3
