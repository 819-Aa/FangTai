"""MC-02 B：UserHealthProfileService 经 MySQL/InMemory source 加载与缓存语义。

- 经 InMemory/Fake source 加载并派生原有约束（封闭注册表语义不变）；
- load 失败后旧 _users 不得继续可用；每次 load 前清空旧缓存；
- 不依赖 JSONL/文件路径（在线路径只经 UserProfileSource 端口）。
"""

from __future__ import annotations

import copy

import pytest

from food_agent_v2.b2.constraint_registry import ALLOWED_CONSTRAINT_CODES
from food_agent_v2.b2.repository import InMemoryUserProfileSource, ProfileRepositoryError
from food_agent_v2.b2.service import UserHealthProfileService

MC02_TEST_READY_BUILD = "3b8a9c2e-4d1f-4a90-9e5b-7a2f6c8d0e11"


class _FailingSource:
    """模拟数据库/source 不可用。"""

    def load_users(self, expected_build_id=None):
        raise ProfileRepositoryError("B2_DATABASE_UNAVAILABLE", "mysql down")


class TestLoadViaSource:
    def test_load_and_derive_via_source(self, user_profiles_source) -> None:
        """经 InMemory source 加载并派生原有约束（阈值/封闭注册表语义不变）。"""
        svc = UserHealthProfileService(source=user_profiles_source)
        svc.load()
        assert len(svc._users) == 50
        cs = svc.derive_constraints(1, "p1")
        assert len(cs.hard_constraints) > 0
        for c in cs.hard_constraints:
            if getattr(c, "constraint_code", None):
                assert c.constraint_code in ALLOWED_CONSTRAINT_CODES

    def test_validate_all_50_valid(self, user_profiles_source) -> None:
        svc = UserHealthProfileService(source=user_profiles_source)
        svc.load()
        report = svc.validate_all()
        assert report["profile_count"] == 50
        assert report["valid_count"] == 50
        assert report["invalid_count"] == 0

    def test_default_source_is_mysql(self) -> None:
        """默认生产 source 必须是 MySQLUserProfileSource（不读文件）。"""
        svc = UserHealthProfileService()
        assert type(svc._source).__name__ == "MySQLUserProfileSource"


class TestLoadCacheSemantics:
    def test_failed_load_clears_previous_users(self, user_profiles_source) -> None:
        """load 失败后旧 _users 不得继续可用（fail-closed）。"""
        svc = UserHealthProfileService(source=user_profiles_source)
        svc.load()
        assert len(svc._users) == 50
        svc._source = _FailingSource()
        with pytest.raises(ProfileRepositoryError) as excinfo:
            svc.load()
        assert excinfo.value.code == "B2_DATABASE_UNAVAILABLE"
        assert svc._users == {}
        assert svc._loaded is False

    def test_reload_replaces_cache(self, user_profiles_source, user_records) -> None:
        """每次 load 前清空旧缓存：重载反映新 source，不残留旧键。"""
        svc = UserHealthProfileService(source=user_profiles_source)
        svc.load()
        changed = copy.deepcopy(user_records)
        changed[0]["age"] = 99
        svc._source = InMemoryUserProfileSource(changed,
                                                ready_builds=[MC02_TEST_READY_BUILD])
        svc.load()
        assert svc.get_user(1)["age"] == 99
        assert len(svc._users) == 50

    def test_expected_build_mismatch_fails_clean(self, user_profiles_source) -> None:
        """expected_build_id 与 ready build 不一致 → 稳定失败且不留数据。"""
        svc = UserHealthProfileService(source=user_profiles_source)
        with pytest.raises(ProfileRepositoryError) as excinfo:
            svc.load(expected_build_id="build-OTHER")
        assert excinfo.value.code == "BUILD_IDENTITY_MISMATCH"
        assert svc._users == {}
        assert svc._loaded is False

    def test_empty_expected_build_is_rejected(self, user_profiles_source) -> None:
        """显式空 build_id 是非法身份，不等价于“未提供”。"""
        svc = UserHealthProfileService(source=user_profiles_source)
        with pytest.raises(ProfileRepositoryError) as excinfo:
            svc.load(expected_build_id="")
        assert excinfo.value.code == "BUILD_IDENTITY_MISMATCH"
        assert svc._users == {}
        assert svc._loaded is False


class TestNoJsonlDependency:
    def test_jsonl_unavailable_still_loads_via_source(
            self, user_profiles_source, monkeypatch) -> None:
        """在线路径不依赖 JSONL：即使 CLEANED_USERS 路径对象已损坏仍经 source 工作。"""
        from food_agent_v2.core import paths

        class _BrokenPath:
            @property
            def open(self):
                raise FileNotFoundError("user_profiles.jsonl 已移除")

        monkeypatch.setattr(paths, "CLEANED_USERS", _BrokenPath())
        svc = UserHealthProfileService(source=user_profiles_source)
        svc.load()
        assert len(svc._users) == 50
