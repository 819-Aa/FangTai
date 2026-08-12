"""已提交菜单的公开菜品投影（MC-03）。

菜单身份来自已校验 Artifact；菜名只经 B3 Repository 从同一唯一 ready build 的
固定 ``recipe_retrieval_build_views`` 派生。不得解析模型回答或读取离线文件。
"""

from __future__ import annotations

from collections.abc import Sequence

from food_agent_v2.b3.repository import (
    FixedDataRepository,
    RepositoryError,
    default_mysql_repository,
)


class MenuProjectionError(RuntimeError):
    """菜单公开投影无法绑定到当前固定构建。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _validated_recipe_ids(recipe_ids: Sequence[int]) -> list[int]:
    ids = list(recipe_ids)
    if (not ids or any(type(recipe_id) is not int or recipe_id <= 0 for recipe_id in ids)
            or len(set(ids)) != len(ids)):
        raise MenuProjectionError(
            "MENU_RECIPE_IDS_INVALID",
            "菜单 recipe_ids 必须为非空、正整数且不重复",
        )
    return ids


def build_public_menu(
    recipe_ids: Sequence[int],
    build_id: str,
    *,
    repository: FixedDataRepository | None = None,
) -> list[dict]:
    """按已提交顺序返回最小公开菜品投影 ``recipe_id/name``。

    ready build 不一致、菜品缺失、返回顺序/数量异常均 fail-closed。
    """
    ids = _validated_recipe_ids(recipe_ids)
    if not isinstance(build_id, str) or not build_id.strip():
        raise MenuProjectionError("BUILD_IDENTITY_MISMATCH", "build_id 为空")

    owns_repository = repository is None
    repo = repository or default_mysql_repository()
    try:
        ready_build = repo.ready_build_id()
        if ready_build != build_id:
            raise MenuProjectionError(
                "BUILD_IDENTITY_MISMATCH",
                f"菜单 build {build_id} 与唯一 ready build {ready_build} 不一致",
            )
        views = list(repo.get_retrieval_view(ids, build_id))
        if len(views) != len(ids):
            raise MenuProjectionError(
                "MENU_PROJECTION_INCOMPLETE", "公开菜品投影数量与菜单不一致")
        by_id = {view.recipe_id: view for view in views}
        if set(by_id) != set(ids):
            raise MenuProjectionError(
                "MENU_PROJECTION_INCOMPLETE", "公开菜品投影身份与菜单不一致")
        items = []
        for recipe_id in ids:
            name = str(by_id[recipe_id].name or "").strip()
            if not name:
                raise MenuProjectionError(
                    "MENU_PROJECTION_INCOMPLETE", f"recipe {recipe_id} 缺少公开菜名")
            items.append({"recipe_id": recipe_id, "name": name})
        return items
    except MenuProjectionError:
        raise
    except RepositoryError as exc:
        raise MenuProjectionError(exc.code, exc.message) from exc
    except Exception as exc:
        raise MenuProjectionError(
            "MENU_PROJECTION_UNAVAILABLE", "固定菜品公开投影不可用") from exc
    finally:
        if owns_repository:
            repo.close()


def build_current_public_menu(
    recipe_ids: Sequence[int],
    *,
    repository: FixedDataRepository | None = None,
) -> tuple[str, list[dict]]:
    """从当前唯一 ready build 派生公开菜单，并返回其构建身份。"""
    owns_repository = repository is None
    repo = repository or default_mysql_repository()
    try:
        build_id = repo.ready_build_id()
        items = build_public_menu(recipe_ids, build_id, repository=repo)
        return build_id, items
    finally:
        if owns_repository:
            repo.close()
