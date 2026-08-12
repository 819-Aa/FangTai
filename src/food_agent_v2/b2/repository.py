"""B2 固定档案 Repository（MC-02）。

B2 在线运行只读取：
- data_builds 中唯一 status='ready' 的构建；
- fixed_artifact_records 中 artifact_name='user_profiles' 的记录（ORDER BY record_index）。

不读取 data/cleaned、JSONL、CSV 或原始用户文件，数据库不可用时不回退文件。
ready 构建必须恰好一个；档案必须精确 50 份；user_id 必须唯一且精确为 1..50；
每条 payload.build_id 必须与 ready 构建一致。任一违约即 fail-closed。
"""

from __future__ import annotations

import json
from typing import Protocol

from food_agent_v2.core.config import MySQLConfig, load_config

#: 固定档案必须精确包含 50 份；user_id 必须是 1..50 的完整覆盖。
USER_PROFILE_COUNT = 50
USER_ID_RANGE = range(1, USER_PROFILE_COUNT + 1)


class ProfileRepositoryError(RuntimeError):
    """B2 档案 Repository 确定性失败（fail-closed，不回退文件）。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class UserProfileSource(Protocol):
    """固定档案来源端口（MySQL 生产实现或测试 InMemory Fake）。"""

    def load_users(self, expected_build_id: str | None = None) -> list[dict]: ...


def _validate_user_profile_records(build_id: str, records: list[dict]) -> list[dict]:
    """共用的 fail-closed 校验：build 身份、精确 50 份、user_id 唯一且 1..50。

    - 每条记录必须携带与 ready build 一致的 build_id（混合 build 即失败）；
    - 缺失/重复/越界 user_id、数量非 50 一律失败；绝不下发部分档案。
    """
    for record in records:
        if not isinstance(record, dict) or str(record.get("build_id")) != build_id:
            raise ProfileRepositoryError(
                "BUILD_IDENTITY_MISMATCH",
                "user_profiles 记录 build_id 与 ready 构建不一致",
            )
    if len(records) != USER_PROFILE_COUNT:
        raise ProfileRepositoryError(
            "USER_PROFILE_COUNT_INVALID",
            f"user_profiles 必须精确 {USER_PROFILE_COUNT} 份，实际 {len(records)}",
        )
    user_ids: list[int] = []
    for record in records:
        uid = record.get("user_id")
        # bool 是 int 的子类；JSON true/false 不得伪装为 user_id 1/0。
        if type(uid) is not int or uid not in USER_ID_RANGE:
            raise ProfileRepositoryError(
                "USER_PROFILE_USER_ID_INVALID",
                f"user_id 缺失/非法/越界: {uid!r}",
            )
        user_ids.append(uid)
    if len(set(user_ids)) != len(user_ids):
        raise ProfileRepositoryError("USER_PROFILE_DUPLICATE_USER_ID", "user_id 重复")
    if set(user_ids) != set(USER_ID_RANGE):
        raise ProfileRepositoryError(
            "USER_PROFILE_USER_ID_INVALID", "user_id 未精确覆盖 1..50")
    return records


class MySQLUserProfileSource:
    """生产实现：从 MySQL data_builds + fixed_artifact_records 只读加载固定档案。

    延迟建连且仅执行 SELECT；每次 ``load_users`` 成功或失败后均确定关闭，
    任何数据库异常稳定失败。
    """

    def __init__(self, *, config: MySQLConfig | None = None) -> None:
        self._config = config or load_config().mysql
        self._connection = None
        self._cursor = None

    def _connect(self):
        if self._connection is not None:
            return self._connection
        import pymysql

        self._connection = pymysql.connect(
            host=self._config.host,
            port=self._config.port,
            user=self._config.user,
            password=self._config.password,
            database=self._config.database,
            charset="utf8mb4",
            autocommit=True,
        )
        self._cursor = self._connection.cursor()
        return self._connection

    @property
    def cursor(self):
        self._connect()
        return self._cursor

    def close(self) -> None:
        """确定释放本 source 拥有的短生命周期数据库资源。"""
        cursor, connection = self._cursor, self._connection
        self._cursor = None
        self._connection = None
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

    def ready_build_id(self) -> str:
        """查询 data_builds 唯一 ready 构建；0 个或多于 1 个即 fail-closed。"""
        try:
            self.cursor.execute("SELECT build_id FROM data_builds WHERE status='ready'")
            rows = self.cursor.fetchall()
        except Exception as exc:
            raise ProfileRepositoryError(
                "B2_DATABASE_UNAVAILABLE", f"B2 固定档案数据库不可用: {exc}") from exc
        if len(rows) != 1:
            raise ProfileRepositoryError(
                "BUILD_IDENTITY_UNAVAILABLE",
                f"data_builds 中 ready 构建必须恰好一个，实际 {len(rows)}",
            )
        return str(rows[0][0])

    def load_users(self, expected_build_id: str | None = None) -> list[dict]:
        try:
            try:
                build_id = self.ready_build_id()
                if expected_build_id is not None and str(expected_build_id) != build_id:
                    raise ProfileRepositoryError(
                        "BUILD_IDENTITY_MISMATCH",
                        f"expected_build_id={expected_build_id} 与 ready build {build_id} 不一致",
                    )
                self.cursor.execute(
                    "SELECT payload FROM fixed_artifact_records "
                    "WHERE build_id=%s AND artifact_name='user_profiles' ORDER BY record_index",
                    (build_id,),
                )
                rows = self.cursor.fetchall()
            except ProfileRepositoryError:
                raise
            except Exception as exc:
                raise ProfileRepositoryError(
                    "B2_DATABASE_UNAVAILABLE", f"B2 固定档案数据库不可用: {exc}") from exc

            records: list[dict] = []
            for (payload,) in rows:
                try:
                    record = json.loads(payload)
                except (json.JSONDecodeError, TypeError) as exc:
                    raise ProfileRepositoryError(
                        "USER_PROFILE_INVALID_JSON",
                        f"user_profiles 记录非法 JSON: {exc}",
                    ) from exc
                records.append(record)
            return _validate_user_profile_records(build_id, records)
        finally:
            self.close()


class InMemoryUserProfileSource:
    """单元测试 Fake：按注入的 ready 构建与记录返回档案（仅测试）。

    与生产实现共享同一套 fail-closed 校验（_validate_user_profile_records），
    使 Repository 的违约分支可离线测试。
    """

    def __init__(self, records: list[dict] | None = None,
                 *, ready_builds: list[str] | None = None) -> None:
        self._records = list(records or [])
        if ready_builds is None:
            ready_builds = ["b2-test-ready"]
        self._ready_builds = [str(b) for b in ready_builds]

    def ready_build_id(self) -> str:
        ready = list(self._ready_builds)
        if len(ready) != 1:
            raise ProfileRepositoryError(
                "BUILD_IDENTITY_UNAVAILABLE",
                f"ready 构建必须恰好一个，实际 {len(ready)}",
            )
        return ready[0]

    def load_users(self, expected_build_id: str | None = None) -> list[dict]:
        build_id = self.ready_build_id()
        if expected_build_id is not None and str(expected_build_id) != build_id:
            raise ProfileRepositoryError(
                "BUILD_IDENTITY_MISMATCH",
                "expected_build_id 与 ready build 不一致",
            )
        return _validate_user_profile_records(build_id, self._records)


def default_mysql_user_profile_source() -> MySQLUserProfileSource:
    """默认生产来源（B2 在线运行只读 MySQL 固定档案）。"""
    return MySQLUserProfileSource()
