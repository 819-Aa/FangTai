"""MC-02 A：B2 固定档案 Repository 单元测试。

覆盖：唯一 ready build + 50 份成功；0/多个 ready 失败；expected build 不一致；
49/51 份失败；user_id 缺失/重复/越界失败；payload build_id 混合失败；
数据库/source 异常稳定失败且不回退文件。
"""

from __future__ import annotations

import copy
import json

import pytest

from food_agent_v2.b2.repository import (
    InMemoryUserProfileSource,
    MySQLUserProfileSource,
    ProfileRepositoryError,
)
from food_agent_v2.b2.service import UserHealthProfileService

#: 与 tests/conftest.py 的 MC02_TEST_READY_BUILD 保持一致（user_records fixture 自带 build_id）。
MC02_TEST_READY_BUILD = "3b8a9c2e-4d1f-4a90-9e5b-7a2f6c8d0e11"


class _FakeCursor:
    """按调用顺序返回固定结果集；exc 非 None 时 execute 抛错。"""

    def __init__(self, *result_sets, exc=None) -> None:
        self._sets = list(result_sets)
        self._i = 0
        self._exc = exc
        self._execute_count = 0
        self._exc_at_execute = 1
        self.closed = False

    def execute(self, sql, params=None):
        self._execute_count += 1
        if self._exc is not None and self._execute_count == self._exc_at_execute:
            raise self._exc

    def fetchall(self):
        rows = self._sets[self._i] if self._i < len(self._sets) else []
        self._i += 1
        return rows

    def close(self):
        self.closed = True


class _FakeConnection:
    def __init__(self) -> None:
        self.closed = False

    def close(self):
        self.closed = True


def _payload_rows(records: list[dict]) -> list[tuple]:
    return [(json.dumps(r, ensure_ascii=False),) for r in records]


def _mysql_source(ready_rows: list[tuple], records: list[dict],
                   *, exc=None) -> MySQLUserProfileSource:
    """构造只读 MySQL source，注入假 cursor（ready 查询 + payload 查询）。"""
    src = MySQLUserProfileSource()
    src._connection = _FakeConnection()
    src._cursor = _FakeCursor(ready_rows, _payload_rows(records), exc=exc)
    return src


class TestRepositorySuccess:
    def test_unique_ready_build_with_50_profiles(self, user_records) -> None:
        """唯一 ready build + 50 份档案成功加载。"""
        src = _mysql_source([(MC02_TEST_READY_BUILD,)], user_records)
        loaded = src.load_users()
        assert len(loaded) == 50
        assert {int(r["user_id"]) for r in loaded} == set(range(1, 51))

    def test_expected_build_matches(self, user_records) -> None:
        """expected_build_id 与 ready build 一致时成功。"""
        src = _mysql_source([(MC02_TEST_READY_BUILD,)], user_records)
        loaded = src.load_users(expected_build_id=MC02_TEST_READY_BUILD)
        assert len(loaded) == 50

    def test_inmemory_source_success(self, user_records) -> None:
        src = InMemoryUserProfileSource(user_records,
                                        ready_builds=[MC02_TEST_READY_BUILD])
        assert len(src.load_users()) == 50


class TestReadyBuildIdentity:
    def test_zero_ready_builds_fails(self, user_records) -> None:
        src = _mysql_source([], user_records)
        with pytest.raises(ProfileRepositoryError) as excinfo:
            src.load_users()
        assert excinfo.value.code == "BUILD_IDENTITY_UNAVAILABLE"

    def test_multiple_ready_builds_fails(self, user_records) -> None:
        src = _mysql_source([("build-A",), ("build-B",)], user_records)
        with pytest.raises(ProfileRepositoryError) as excinfo:
            src.load_users()
        assert excinfo.value.code == "BUILD_IDENTITY_UNAVAILABLE"

    def test_inmemory_zero_ready_builds_fails(self, user_records) -> None:
        src = InMemoryUserProfileSource(user_records, ready_builds=[])
        with pytest.raises(ProfileRepositoryError) as excinfo:
            src.load_users()
        assert excinfo.value.code == "BUILD_IDENTITY_UNAVAILABLE"

    def test_inmemory_multiple_ready_builds_fails(self, user_records) -> None:
        src = InMemoryUserProfileSource(user_records,
                                        ready_builds=["a", "b"])
        with pytest.raises(ProfileRepositoryError) as excinfo:
            src.load_users()
        assert excinfo.value.code == "BUILD_IDENTITY_UNAVAILABLE"

    def test_expected_build_mismatch_fails(self, user_records) -> None:
        """expected_build_id 与 ready build 不一致 → 稳定失败。"""
        src = _mysql_source([(MC02_TEST_READY_BUILD,)], user_records)
        with pytest.raises(ProfileRepositoryError) as excinfo:
            src.load_users(expected_build_id="build-OTHER")
        assert excinfo.value.code == "BUILD_IDENTITY_MISMATCH"

    def test_inmemory_expected_build_mismatch_fails(self, user_records) -> None:
        src = InMemoryUserProfileSource(user_records,
                                        ready_builds=[MC02_TEST_READY_BUILD])
        with pytest.raises(ProfileRepositoryError) as excinfo:
            src.load_users(expected_build_id="build-OTHER")
        assert excinfo.value.code == "BUILD_IDENTITY_MISMATCH"


class TestProfileCount:
    def test_49_profiles_fails(self, user_records) -> None:
        records = copy.deepcopy(user_records)[:49]
        src = _mysql_source([(MC02_TEST_READY_BUILD,)], records)
        with pytest.raises(ProfileRepositoryError) as excinfo:
            src.load_users()
        assert excinfo.value.code == "USER_PROFILE_COUNT_INVALID"

    def test_51_profiles_fails(self, user_records) -> None:
        records = copy.deepcopy(user_records)
        records.append(copy.deepcopy(records[0]))
        src = _mysql_source([(MC02_TEST_READY_BUILD,)], records)
        with pytest.raises(ProfileRepositoryError) as excinfo:
            src.load_users()
        assert excinfo.value.code == "USER_PROFILE_COUNT_INVALID"

    def test_inmemory_49_profiles_fails(self, user_records) -> None:
        src = InMemoryUserProfileSource(
            copy.deepcopy(user_records)[:49], ready_builds=[MC02_TEST_READY_BUILD])
        with pytest.raises(ProfileRepositoryError) as excinfo:
            src.load_users()
        assert excinfo.value.code == "USER_PROFILE_COUNT_INVALID"


class TestUserIdIntegrity:
    def test_missing_user_id_fails(self, user_records) -> None:
        records = copy.deepcopy(user_records)
        records[0].pop("user_id")
        src = _mysql_source([(MC02_TEST_READY_BUILD,)], records)
        with pytest.raises(ProfileRepositoryError) as excinfo:
            src.load_users()
        assert excinfo.value.code == "USER_PROFILE_USER_ID_INVALID"

    def test_duplicate_user_id_fails(self, user_records) -> None:
        records = copy.deepcopy(user_records)
        records[1]["user_id"] = 1
        src = _mysql_source([(MC02_TEST_READY_BUILD,)], records)
        with pytest.raises(ProfileRepositoryError) as excinfo:
            src.load_users()
        assert excinfo.value.code == "USER_PROFILE_DUPLICATE_USER_ID"

    def test_out_of_range_user_id_fails(self, user_records) -> None:
        records = copy.deepcopy(user_records)
        records[0]["user_id"] = 51
        src = _mysql_source([(MC02_TEST_READY_BUILD,)], records)
        with pytest.raises(ProfileRepositoryError) as excinfo:
            src.load_users()
        assert excinfo.value.code == "USER_PROFILE_USER_ID_INVALID"

    @pytest.mark.parametrize("boolean_id", [True, False])
    def test_boolean_user_id_fails(self, user_records, boolean_id) -> None:
        """JSON true/false 不是整数用户 ID，不得借 Python bool/int 继承通过。"""
        records = copy.deepcopy(user_records)
        records[0]["user_id"] = boolean_id
        src = _mysql_source([(MC02_TEST_READY_BUILD,)], records)
        with pytest.raises(ProfileRepositoryError) as excinfo:
            src.load_users()
        assert excinfo.value.code == "USER_PROFILE_USER_ID_INVALID"

    def test_mixed_build_identity_fails(self, user_records) -> None:
        records = copy.deepcopy(user_records)
        records[0]["build_id"] = "build-OTHER"
        src = _mysql_source([(MC02_TEST_READY_BUILD,)], records)
        with pytest.raises(ProfileRepositoryError) as excinfo:
            src.load_users()
        assert excinfo.value.code == "BUILD_IDENTITY_MISMATCH"


class TestDatabaseUnavailable:
    def test_db_unavailable_no_file_fallback(self, user_records) -> None:
        """数据库异常 → 稳定失败；service 不回退 JSONL 文件。"""
        src = _mysql_source([(MC02_TEST_READY_BUILD,)], user_records,
                            exc=RuntimeError("mysql down"))
        svc = UserHealthProfileService(source=src)
        with pytest.raises(ProfileRepositoryError) as excinfo:
            svc.load()
        assert excinfo.value.code == "B2_DATABASE_UNAVAILABLE"

    def test_invalid_json_payload_fails(self, user_records) -> None:
        src = MySQLUserProfileSource()
        src._connection = _FakeConnection()
        src._cursor = _FakeCursor(
            [(MC02_TEST_READY_BUILD,)], [("{not-json",)])
        with pytest.raises(ProfileRepositoryError) as excinfo:
            src.load_users()
        assert excinfo.value.code == "USER_PROFILE_INVALID_JSON"


class TestConnectionLifecycle:
    @pytest.mark.parametrize("invalid_json", [False, True])
    def test_connection_and_cursor_close_after_load(self, user_records, invalid_json) -> None:
        """短生命周期 B2 source 在成功与解析失败后均确定关闭数据库资源。"""
        src = MySQLUserProfileSource()
        connection = _FakeConnection()
        payload_rows = [("{not-json",)] if invalid_json else _payload_rows(user_records)
        cursor = _FakeCursor([(MC02_TEST_READY_BUILD,)], payload_rows)
        src._connection = connection
        src._cursor = cursor

        if invalid_json:
            with pytest.raises(ProfileRepositoryError):
                src.load_users()
        else:
            assert len(src.load_users()) == 50

        assert cursor.closed is True
        assert connection.closed is True
        assert src._cursor is None
        assert src._connection is None

    def test_connection_and_cursor_close_after_query_failure(self, user_records) -> None:
        """数据库查询异常同样不得遗留 source 持有的连接。"""
        src = MySQLUserProfileSource()
        connection = _FakeConnection()
        cursor = _FakeCursor(exc=RuntimeError("mysql down"))
        src._connection = connection
        src._cursor = cursor

        with pytest.raises(ProfileRepositoryError) as excinfo:
            src.load_users()

        assert excinfo.value.code == "B2_DATABASE_UNAVAILABLE"
        assert cursor.closed is True
        assert connection.closed is True
        assert src._cursor is None
        assert src._connection is None

    def test_connection_closes_when_payload_query_fails(self, user_records) -> None:
        """ready build 查询成功、第二条 payload SELECT 失败时也必须关闭。"""
        src = MySQLUserProfileSource()
        connection = _FakeConnection()
        cursor = _FakeCursor(
            [(MC02_TEST_READY_BUILD,)],
            _payload_rows(user_records),
            exc=RuntimeError("payload query failed"),
        )
        cursor._exc_at_execute = 2
        src._connection = connection
        src._cursor = cursor

        with pytest.raises(ProfileRepositoryError) as excinfo:
            src.load_users()

        assert excinfo.value.code == "B2_DATABASE_UNAVAILABLE"
        assert cursor.closed is True
        assert connection.closed is True
        assert src._cursor is None
        assert src._connection is None


def test_make_records_satisfy_integrity(user_records) -> None:
    """生成器本身必须通过校验（防止测试数据先坏）。"""
    src = InMemoryUserProfileSource(user_records,
                                    ready_builds=[MC02_TEST_READY_BUILD])
    assert len(src.load_users()) == 50
