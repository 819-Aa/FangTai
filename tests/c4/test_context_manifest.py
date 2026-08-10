"""T18 C4 ContextManifest 约束先行 / 压缩核对 / 失败记忆测试。

使用 InMemorySessionMemorySource（单元测试，不依赖 MySQL）；每个用例使用唯一
session_id 避免跨 pytest 运行的 Redis 残留。
"""

import uuid

from food_agent_v2.c4 import (
    ConstraintScope,
    ContextService,
    ConversationEvent,
    CurrentMenu,
    EffectiveConstraint,
    EventType,
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
    return ContextService(memory_source=InMemorySessionMemorySource())


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
        core_before = ctx.context_manifest.immutable_block_hashes["core"]
        svc.store_derived_constraints(
            sid, {"p1": _FakeConstraintSet([
                _FakeConstraint("allergy_seafood", ["s1"]),
                _FakeConstraint("allergy_peanut", ["s2"]),
            ])})
        # 约束变更后 manifest 核心块重算，完整性仍通过
        assert len(ctx.effective_constraints) == 2
        assert ctx.context_manifest.immutable_block_hashes["core"] != core_before
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
        core_before = ctx.context_manifest.immutable_block_hashes["core"]
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
        assert ctx.context_manifest.immutable_block_hashes["core"] == core_before
        assert svc.validate_context_integrity(sid)["valid"] is True


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


class _FakeConstraint:
    def __init__(self, code: str, refs: list[str]) -> None:
        self.constraint_code = code
        self.taboo_ingredient_name = None
        self.source_refs = refs


class _FakeConstraintSet:
    def __init__(self, hard_constraints: list) -> None:
        self.hard_constraints = hard_constraints
