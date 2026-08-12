"""MC-03：固定 ready build 菜单公开投影。"""

import pytest

from food_agent_v2.application.menu_projection import (
    MenuProjectionError,
    build_current_public_menu,
    build_public_menu,
)
from food_agent_v2.b3.repository import (
    RecipeRetrievalView,
    RepositoryError,
)


class _Repo:
    def __init__(self, build_id: str = "build-ready") -> None:
        self.build_id = build_id

    def ready_build_id(self) -> str:
        return self.build_id

    def get_retrieval_view(self, recipe_ids, build_id):
        if build_id != self.build_id:
            raise RepositoryError("BUILD_IDENTITY_MISMATCH", "wrong build")
        by_id = {
            1: RecipeRetrievalView(1, "番茄炒蛋", [], [], {}, None, None, []),
            2: RecipeRetrievalView(2, "清炒时蔬", [], [], {}, None, None, []),
        }
        try:
            return [by_id[int(recipe_id)] for recipe_id in recipe_ids]
        except KeyError as exc:
            raise RepositoryError("UNKNOWN_RECIPE_ID", str(exc)) from exc

    def close(self):
        self.closed = True


def test_public_menu_preserves_committed_order() -> None:
    assert build_public_menu([2, 1], "build-ready", repository=_Repo()) == [
        {"recipe_id": 2, "name": "清炒时蔬"},
        {"recipe_id": 1, "name": "番茄炒蛋"},
    ]


@pytest.mark.parametrize("recipe_ids", [[], [1, 1], [True]])
def test_public_menu_rejects_invalid_identity(recipe_ids) -> None:
    with pytest.raises(MenuProjectionError):
        build_public_menu(recipe_ids, "build-ready", repository=_Repo())


def test_public_menu_rejects_build_mismatch() -> None:
    with pytest.raises(MenuProjectionError) as excinfo:
        build_public_menu([1], "build-other", repository=_Repo())
    assert excinfo.value.code == "BUILD_IDENTITY_MISMATCH"


def test_public_menu_rejects_unknown_recipe() -> None:
    with pytest.raises(MenuProjectionError) as excinfo:
        build_public_menu([999], "build-ready", repository=_Repo())
    assert excinfo.value.code == "UNKNOWN_RECIPE_ID"


def test_current_public_menu_returns_ready_build_identity() -> None:
    build_id, items = build_current_public_menu([1], repository=_Repo())
    assert build_id == "build-ready"
    assert items == [{"recipe_id": 1, "name": "番茄炒蛋"}]
