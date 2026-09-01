"""T18 C4 ContextManifest 约束先行 / 压缩核对 / 失败记忆测试。

使用 InMemorySessionMemorySource（单元测试，不依赖 MySQL）；每个用例使用唯一
session_id 避免跨 pytest 运行的 Redis 残留。
"""

import uuid

import pytest

from food_agent_v2.c4 import (
    ConstraintScope,
    ContextBudgetExceeded,
    ContextIntegrityFailed,
    ContextService,
    ConversationEvent,
    CurrentMenu,
    EffectiveConstraint,
    EventType,
    SharedWorkflowContext,
)
from food_agent_v2.c4.mysql_repository import InMemorySessionMemorySource


def make_constraint(code: str = "allergy_seafood",
                    scope: ConstraintScope = ConstraintScope.PERMANENT,
                    ref: str = "p1") -> EffectiveConstraint:
    return EffectiveConstraint(
        constraint_code=code, taboo_ingredient_name=None,
        participant_ref=ref, source_refs=["s1"], scope=scope,
    )


def make_service() -> ContextService:
    return ContextService(memory_source=InMemorySessionMemorySource(),
                           permanent_constraint_loader=_EmptyLoader())


def uniq(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


class TestConstraintsBeforeManifest:
    def test_permanent_constraints_loaded_before_manifest(self) -> None:
        svc = make_service()
        sid = uniq("cm")
        permanent = [make_constraint("allergy_seafood")]
        ctx, manifest = svc.build_shared_context(
            sid, ["p1"], {"raw_text": "第一轮"}, {"p1": 1},
            request_id="r1", permanent_constraints=permanent)
        # 约束先于 ContextManifest：effective_constraints 完整
        assert len(ctx.effective_constraints) == 1
        assert ctx.effective_constraints[0].constraint_code == "allergy_seafood"
        assert ctx.effective_constraints[0].scope == ConstraintScope.PERMANENT
        # 核心块哈希包含约束 → 完整性校验通过
        assert svc.validate_context_integrity(sid)["valid"] is True

    def test_derived_constraints_recompute_manifest(self) -> None:
        svc = make_service()
        sid = uniq("cm")
        ctx, manifest = svc.build_shared_context(
            sid, ["p1"], {"raw_text": "hi"}, {"p1": 1}, request_id="r2")
        core_before = ctx.context_manifest.manifest_hash
        svc.store_derived_constraints(
            sid, {"p1": _FakeConstraintSet([
                _FakeConstraint("allergy_seafood", ["s1"]),
                _FakeConstraint("allergy_peanut", ["s2"]),
            ])})
        # 约束变更后 manifest 核心块重算，完整性仍通过
        assert len(ctx.effective_constraints) == 2
        assert ctx.context_manifest.immutable_block_hashes["constraints"] != core_before
        assert svc.validate_context_integrity(sid)["valid"] is True

    def test_temporary_constraint_recompute_manifest(self) -> None:
        svc = make_service()
        sid = uniq("cm")
        ctx, manifest = svc.build_shared_context(
            sid, ["p1"], {"raw_text": "hi"}, {"p1": 1}, request_id="r3")
        svc.store_temporary_constraint(sid, {
            "constraint_code": "allergy_seafood", "participant_ref": "p1",
            "source_refs": [], "scope": "session"})
        assert svc.validate_context_integrity(sid)["valid"] is True

    def test_revoke_temporary_constraint_recompute_manifest(self) -> None:
        svc = make_service()
        sid = uniq("cm")
        ctx, manifest = svc.build_shared_context(
            sid, ["p1"], {"raw_text": "hi"}, {"p1": 1}, request_id="r4")
        cid = svc.store_temporary_constraint(sid, {
            "constraint_code": "allergy_seafood", "participant_ref": "p1",
            "source_refs": [], "scope": "session"})
        svc.revoke_temporary_constraint(sid, cid)
        assert len(ctx.effective_constraints) == 0
        assert svc.validate_context_integrity(sid)["valid"] is True


class TestCompressionCoreHash:
    def test_compression_preserves_core_hash(self) -> None:
        svc = make_service()
        sid = uniq("cc")
        ctx, manifest = svc.build_shared_context(
            sid, ["p1"], {"raw_text": "第一轮"}, {"p1": 1}, request_id="r1")
        svc.store_temporary_constraint(sid, {
            "constraint_code": "allergy_seafood", "participant_ref": "p1",
            "source_refs": [], "scope": "session"})
        core_before = ctx.context_manifest.manifest_hash
        for i in range(250):
            ctx.conversation_events.append(ConversationEvent(
                event_id=f"e{i}", session_id=sid, request_id="r1",
                event_type=EventType.USER_MESSAGE,
                event_summary=f"第{i}轮内容内容内容",
                token_count_estimate=30))
        ctx.context_manifest.total_token_estimate = 300 * 30
        compressed = svc._compress_context(ctx)
        assert compressed
        assert len(ctx.conversation_events) <= 21
        # 压缩前后核心块哈希一致（INV-009 §9.3）
        assert ctx.context_manifest.manifest_hash == core_before
        assert svc.validate_context_integrity(sid)["valid"] is True

    def test_compression_budget_exceeded_raises(self) -> None:
        """压缩后仍超预算 → 抛 ContextBudgetExceeded，不静默删核心块。"""
        svc = make_service()
        sid = uniq("budget")
        ctx, _manifest = svc.build_shared_context(
            sid, ["p1"], {"raw_text": "hi"}, {"p1": 1}, request_id="r1")
        # 大量高 token 事件，使压缩后（摘要 + 最近 20 条）仍超小预算
        for i in range(250):
            ctx.conversation_events.append(ConversationEvent(
                event_id=f"e{i}", session_id=sid, request_id="r1",
                event_type=EventType.USER_MESSAGE,
                event_summary=f"第{i}轮" * 20,
                token_count_estimate=200))
        ctx.context_manifest.total_token_estimate = 250 * 200
        with pytest.raises(ContextBudgetExceeded):
            svc._compress_context(ctx, budget_tokens=100)

    def test_projection_integrity_failed_raises(self) -> None:
        """投影前完整性校验失败（核心块改动未同步 manifest）→ 抛 ContextIntegrityFailed。"""
        svc = make_service()
        sid = uniq("integrity")
        ctx, _manifest = svc.build_shared_context(
            sid, ["p1"], {"raw_text": "hi"}, {"p1": 1}, request_id="r1")
        # 直接改核心块（current_menu）但不同步 manifest → 投影时完整性失败
        ctx.current_menu = CurrentMenu(plan_id="plan-X", recipe_ids=[9, 9])
        with pytest.raises(ContextIntegrityFailed):
            svc.project_model_context("query_understanding", None, sid)


class TestFailedRequestNoMemory:
    def test_failed_request_does_not_commit_menu(self) -> None:
        svc = make_service()
        sid = uniq("f")
        ctx, manifest = svc.build_shared_context(
            sid, ["p1"], {"raw_text": "hi"}, {"p1": 1}, request_id="rf1")
        ctx.current_menu = CurrentMenu(plan_id="plan-A", recipe_ids=[1, 2])
        svc.commit_session_state("rf1", "failed", menu_artifact_ref="art")
        # 失败不留下新菜单版本/成功记忆
        assert ctx.menu_history == []
        assert svc._menu_histories.get(sid, []) == []

    def test_completed_request_commits_menu(self) -> None:
        svc = make_service()
        sid = uniq("f")
        ctx, manifest = svc.build_shared_context(
            sid, ["p1"], {"raw_text": "hi"}, {"p1": 1}, request_id="rf2")
        ctx.current_menu = CurrentMenu(plan_id="plan-B", recipe_ids=[3, 4])
        svc.commit_session_state("rf2", "completed", menu_artifact_ref="art")
        assert ctx.menu_history and ctx.menu_history[0]["plan_id"] == "plan-B"

    def test_model_menu_projection_is_json_serializable(self) -> None:
        svc = make_service()
        sid = uniq("projection")
        ctx, _manifest = svc.build_shared_context(
            sid, ["p1"], {"raw_text": "hi"}, {"p1": 1}, request_id="rp1")
        ctx.current_menu = CurrentMenu(plan_id="plan-C", recipe_ids=[5, 6])
        svc._recompute_manifest(ctx)  # 核心块变更后同步 manifest

        projected = svc.project_model_context("query_understanding", None, sid)

        import json

        json.dumps(projected.menu_visible, ensure_ascii=False)
        assert projected.menu_visible["current_menu"]["plan_id"] == "plan-C"


class TestDefaultLoaderLoadsPermanent:
    def test_default_loader_loads_b2_permanent_constraints(self) -> None:
        """默认生产实现：build_shared_context 从加载器加载永久约束（真实调用路径）。"""

        class _FakeLoader:
            def load(self, mapping):
                assert mapping == {"p1": 1}
                return [make_constraint("allergy_seafood")]

        svc = ContextService(memory_source=InMemorySessionMemorySource(),
                             permanent_constraint_loader=_FakeLoader())
        sid = uniq("dl")
        ctx, manifest = svc.build_shared_context(
            sid, ["p1"], {"raw_text": "hi"}, {"p1": 1}, request_id="r1")
        assert len(ctx.effective_constraints) == 1
        assert ctx.effective_constraints[0].constraint_code == "allergy_seafood"
        assert ctx.effective_constraints[0].scope == ConstraintScope.PERMANENT
        assert svc.validate_context_integrity(sid)["valid"] is True


class TestConstraintLifecycle:
    def test_permanent_and_session_coexist(self) -> None:
        svc = make_service()
        sid = uniq("lc")
        permanent = [make_constraint("allergy_seafood")]
        ctx, manifest = svc.build_shared_context(
            sid, ["p1"], {"raw_text": "hi"}, {"p1": 1}, request_id="r1",
            permanent_constraints=permanent)
        # session 临时约束追加，永久约束保留 → 共存
        cid = svc.store_temporary_constraint(sid, {
            "constraint_code": "allergy_peanut", "participant_ref": "p1",
            "source_refs": [], "scope": "session"})
        assert {c.constraint_code for c in ctx.effective_constraints} == {
            "allergy_seafood", "allergy_peanut"}
        # 重新派生永久约束不覆盖 session
        svc.store_derived_constraints(
            sid, {"p1": _FakeConstraintSet([
                _FakeConstraint("allergy_seafood", ["s1"]),
                _FakeConstraint("allergy_peanut", ["s2"]),
            ])})
        codes = [(c.constraint_code, c.scope) for c in ctx.effective_constraints]
        assert ("allergy_peanut", ConstraintScope.PERMANENT) in codes
        assert ("allergy_peanut", ConstraintScope.SESSION) in codes
        svc.revoke_temporary_constraint(sid, cid)
        assert ("allergy_peanut", ConstraintScope.SESSION) not in [
            (c.constraint_code, c.scope) for c in ctx.effective_constraints]

    def test_session_constraint_survives_new_service(self) -> None:
        svc = make_service()
        sid = uniq("restore")
        ctx, manifest = svc.build_shared_context(
            sid, ["p1"], {"raw_text": "hi"}, {"p1": 1}, request_id="r1")
        svc.store_temporary_constraint(sid, {
            "constraint_code": "allergy_peanut", "participant_ref": "p1",
            "source_refs": [], "scope": "session"})
        # 新 ContextService（内存清空）→ 从 Redis 恢复 session 约束
        svc2 = make_service()
        restored = svc2._restore_session(sid)
        assert restored is not None
        codes = {c.get("constraint_code") for c in restored.get("constraints", [])}
        assert "allergy_peanut" in codes

    def test_turn_constraint_not_restored(self) -> None:
        svc = make_service()
        sid = uniq("turn")
        ctx, manifest = svc.build_shared_context(
            sid, ["p1"], {"raw_text": "hi"}, {"p1": 1}, request_id="r1")
        cid = svc.store_temporary_constraint(sid, {
            "constraint_code": "allergy_peanut", "participant_ref": "p1",
            "source_refs": [], "scope": "turn"})
        assert any(c.constraint_id == cid for c in ctx.effective_constraints)
        # turn 约束不持久化 → 新服务恢复后不存在
        svc2 = make_service()
        restored = svc2._restore_session(sid)
        if restored is not None:
            assert not any(c.get("constraint_id") == cid
                           for c in restored.get("constraints", []))


class TestRestoreAuthority:
    def test_redis_uncommitted_menu_not_restored_as_history(self) -> None:
        """MySQL 已提交菜单优先；Redis 未提交菜单不得恢复为成功历史。"""
        source = InMemorySessionMemorySource()
        sid = uniq("auth")
        source.sessions[sid] = {"session_id": sid, "participant_refs": ["p1"],
                                "current_menu_plan_id": "plan-A", "request_count": 1}
        source.menus[sid] = [{"plan_id": "plan-A", "menu_hash": "a" * 64,
                              "recipe_ids": [1, 2], "committed_at": "t"}]
        svc = ContextService(memory_source=source, permanent_constraint_loader=_EmptyLoader())
        # 往 Redis 写入一个"未提交"菜单（不应进入历史）
        svc._persist_session(SharedWorkflowContext(
            request_id="rx", session_id=sid, participant_refs=["p1"],
            participant_user_id_mapping={"p1": 1}, current_message={},
            menu_history=[{"plan_id": "plan-UNCOMMITTED", "recipe_ids": [9]}],
        ))
        restored = svc._restore_session(sid)
        plans = [m.get("plan_id") for m in restored["menu_history"]]
        assert "plan-UNCOMMITTED" not in plans  # Redis 未提交不恢复为成功历史
        assert "plan-A" in plans                # MySQL 已提交恢复

    def test_mysql_read_failure_not_silently_swallowed(self) -> None:
        """MySQL 读取失败必须上抛（SessionMemoryUnavailable），不得把 Redis 当最终事实。"""

        class _RaisingSource:
            def load_session(self, session_id):
                raise RuntimeError("mysql down")

        svc = ContextService(memory_source=_RaisingSource(),
                             permanent_constraint_loader=_EmptyLoader())
        from food_agent_v2.c4 import SessionMemoryUnavailable
        with pytest.raises(SessionMemoryUnavailable):
            svc._restore_session(uniq("fail"))


class TestManifestFullCoverage:
    """完整核心块覆盖：current_message 完整 dict、约束全字段、key 集合精确、manifest_hash 校验。"""

    def _build(self):
        svc = make_service()
        sid = uniq("mfc")
        ctx, manifest = svc.build_shared_context(
            sid, ["p1"], {"raw_text": "第一轮", "timestamp": 1.0}, {"p1": 1},
            request_id="r")
        svc.store_temporary_constraint(sid, {
            "constraint_code": "allergy_seafood", "participant_ref": "p1",
            "source_refs": ["s1"], "scope": "session"})
        return svc, sid, ctx

    def test_valid_core_block_passes(self):
        svc, sid, ctx = self._build()
        assert svc.validate_context_integrity(sid)["valid"] is True

    def test_current_message_timestamp_change_fails(self):
        svc, sid, ctx = self._build()
        ctx.current_message["timestamp"] = 2.0  # 完整 dict 字段变化
        result = svc.validate_context_integrity(sid)
        assert result["valid"] is False
        assert "current_message" in result["changed_blocks"]

    def test_constraint_effect_change_fails(self):
        svc, sid, ctx = self._build()
        ctx.effective_constraints[0].effect = "soft_prefer"
        result = svc.validate_context_integrity(sid)
        assert result["valid"] is False
        assert "constraints" in result["changed_blocks"]

    def test_constraint_source_refs_change_fails(self):
        svc, sid, ctx = self._build()
        ctx.effective_constraints[0].source_refs.append("hacked")
        assert not svc.validate_context_integrity(sid)["valid"]

    def test_constraint_scope_change_fails(self):
        svc, sid, ctx = self._build()
        ctx.effective_constraints[0].scope = ConstraintScope.TURN
        assert not svc.validate_context_integrity(sid)["valid"]

    def test_constraint_id_change_fails(self):
        svc, sid, ctx = self._build()
        ctx.effective_constraints[0].constraint_id = "hacked"
        assert not svc.validate_context_integrity(sid)["valid"]

    def test_missing_block_hash_fails(self):
        svc, sid, ctx = self._build()
        del ctx.context_manifest.immutable_block_hashes["constraints"]
        result = svc.validate_context_integrity(sid)
        assert result["valid"] is False
        assert "block_keys" in result["changed_blocks"]

    def test_extra_block_hash_fails(self):
        svc, sid, ctx = self._build()
        ctx.context_manifest.immutable_block_hashes["bogus"] = "0" * 64
        result = svc.validate_context_integrity(sid)
        assert result["valid"] is False
        assert "block_keys" in result["changed_blocks"]

    def test_fabricated_manifest_hash_fails(self):
        svc, sid, ctx = self._build()
        ctx.context_manifest.manifest_hash = "0" * 64
        result = svc.validate_context_integrity(sid)
        assert result["valid"] is False
        assert "manifest_hash" in result["changed_blocks"]


class TestRestoreCombined:
    def test_restore_from_mysql_committed_boundary(self) -> None:
        source = InMemorySessionMemorySource()
        sid = uniq("rs")
        source.sessions[sid] = {
            "session_id": sid, "participant_refs": ["p1"],
            "current_menu_plan_id": "plan-A", "request_count": 1,
        }
        source.events[sid] = [{
            "event_id": "ev1", "event_type": "terminal",
            "event_summary": "terminal:completed", "participant_refs": ["p1"],
            "token_count_estimate": 1,
        }]
        source.menus[sid] = [{
            "plan_id": "plan-A", "menu_hash": "a" * 64,
            "recipe_ids": [1, 2], "committed_at": "t",
        }]
        svc = ContextService(memory_source=source)
        # Redis 缓存丢失 → 从 MySQL 已提交边界恢复
        restored = svc._restore_session(sid)
        assert restored is not None
        assert any(m.get("plan_id") == "plan-A" for m in restored["menu_history"])
        assert any(e.get("event_id") == "ev1" for e in restored["events"])
        assert restored["participant_refs"] == ["p1"]

    def test_new_session_returns_none(self) -> None:
        svc = make_service()
        assert svc._restore_session(uniq("none")) is None

    def test_get_session_projects_committed_menu_from_ready_build(self) -> None:
        """公开会话菜单必须携带同一 build 与固定菜名投影。"""
        from food_agent_v2.b3.repository import RecipeRetrievalView, RepositoryError

        class _MenuRepo:
            def ready_build_id(self):
                return "build-ready"

            def get_retrieval_view(self, recipe_ids, build_id):
                if build_id != "build-ready":
                    raise RepositoryError("BUILD_IDENTITY_MISMATCH", "bad build")
                names = {1: "番茄炒蛋", 2: "清炒时蔬"}
                return [RecipeRetrievalView(
                    recipe_id, names[recipe_id], [], [], {}, None, None, [])
                    for recipe_id in recipe_ids]

            def close(self):
                pass

        source = InMemorySessionMemorySource()
        sid = uniq("session-menu")
        source.sessions[sid] = {
            "session_id": sid, "participant_refs": ["p1"],
            "current_menu_plan_id": "plan-A", "request_count": 1,
        }
        source.menus[sid] = [{
            "plan_id": "plan-A", "menu_hash": "a" * 64,
            "recipe_ids": [2, 1], "committed_at": "t",
        }]
        svc = ContextService(memory_source=source, menu_repository=_MenuRepo())

        current = svc.get_session_state(sid)["current_menu"]

        assert current["build_id"] == "build-ready"
        assert current["recipe_ids"] == [2, 1]
        assert current["items"] == [
            {"recipe_id": 2, "name": "清炒时蔬"},
            {"recipe_id": 1, "name": "番茄炒蛋"},
        ]

    def test_get_session_state_binds_query_plan_to_current_menu_plan(self) -> None:
        """不得按时间误取非 current_menu_plan_id 对应的成功计划。"""
        import json

        from food_agent_v2.b3.repository import RecipeRetrievalView
        from food_agent_v2.c4.mysql_repository import MySQLSessionMemorySource

        class _Cursor:
            def __init__(self):
                self.sql = ""

            def execute(self, sql, params):
                self.sql = sql
                self.params = params

            def fetchone(self):
                if "FROM sessions WHERE" in self.sql:
                    return ("sess-plan", '["p1"]', "plan-A", 2, None)
                if "recommendation_logs" in self.sql:
                    return (json.dumps({
                        "query_plan": {
                            "meal_types": ["晚餐"],
                            "exclude_ingredients": ["花生"],
                        }
                    }, ensure_ascii=False),)
                return None

            def fetchall(self):
                if "menu_versions" in self.sql:
                    return (
                        ("plan-A", "a" * 64, "[1]", "2026-01-01"),
                        ("plan-B", "b" * 64, "[2]", "2026-01-02"),
                    )
                return ()

        class _MenuRepo:
            def ready_build_id(self):
                return "build-ready"

            def get_retrieval_view(self, recipe_ids, build_id):
                return [RecipeRetrievalView(
                    recipe_id, f"菜{recipe_id}", [], [], {}, None, None, [])
                    for recipe_id in recipe_ids]

            def close(self):
                pass

        source = MySQLSessionMemorySource()
        source._connection = object()
        source._cursor = _Cursor()
        state = ContextService(
            memory_source=source, menu_repository=_MenuRepo()
        ).get_session_state("sess-plan")

        assert state["current_menu"]["plan_id"] == "plan-A"
        assert state["query_plan"] == {
            "meal_types": ["晚餐"],
            "exclude_ingredients": ["花生"],
        }
        assert source._cursor.params == ("sess-plan", "plan-A")


class _FakeConstraint:
    def __init__(self, code: str, refs: list[str]) -> None:
        self.constraint_code = code
        self.taboo_ingredient_name = None
        self.source_refs = refs


class _FakeConstraintSet:
    def __init__(self, hard_constraints: list) -> None:
        self.hard_constraints = hard_constraints


class _EmptyLoader:
    """单元测试隔离：默认 B2 加载器返回空（不依赖 B2 数据）。"""

    def load(self, participant_user_id_mapping):
        return []
