"""MC-02 C：B2/C3/C4 在线边界测试。

- 在线模块（b2 service/repository、c3 tool_handler、c4）不再导入 CLEANED_USERS /
  pathlib / JSONL 读取；
- MySQL/source 不可用或 expected build 与 ready build 不一致时不得调用 LLM；
- 真实 B2PermanentConstraintLoader + InMemory source 接线可加载永久约束；
- 公共响应/角色投影不增加任何隐私字段。
"""

from __future__ import annotations

import ast
import copy
import uuid
from pathlib import Path

import pytest

from food_agent_v2.b2.repository import InMemoryUserProfileSource, ProfileRepositoryError
from food_agent_v2.b2.service import UserHealthProfileService
from food_agent_v2.c3.runner import WorkflowRunner
from food_agent_v2.c3.tool_handler import ToolContext, ToolHandler, _retrieve_recipes
from food_agent_v2.c4 import (
    B2PermanentConstraintLoader,
    ConstraintScope,
    ContextService,
)
from food_agent_v2.c4.mysql_repository import InMemorySessionMemorySource
from food_agent_v2.d1 import api as d1_api

RID = "33333333-3333-3333-3333-333333333333"
BID = "22222222-2222-2222-2222-222222222222"

ONLINE_FILES = (
    "src/food_agent_v2/b2/service.py",
    "src/food_agent_v2/b2/repository.py",
    "src/food_agent_v2/c3/tool_handler.py",
    "src/food_agent_v2/c4/__init__.py",
)


def _uniq(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _reset_d1() -> None:
    d1_api._requests.clear()
    d1_api._events.clear()
    d1_api._event_cursors.clear()
    d1_api._idempotency.clear()
    d1_api._requests[RID] = {
        "request_id": RID, "session_id": "sess_mc02", "status": "accepted",
        "created_at": "t", "updated_at": "t",
    }


def _isolate_runtime(c4: ContextService) -> ContextService:
    """只隔离会话锁/持久化基础设施，不替换被测 B2/C3/C4 业务路径。"""
    c4.acquire_session_lock = lambda _session_id, _worker_id: "1"
    c4.renew_session_lock = lambda _session_id, _token: True
    c4.is_session_lock_held = lambda _session_id, _token: True
    c4.release_session_lock = lambda _session_id, _token: True
    c4._persist_session = lambda _ctx, _token=None: None
    return c4


def _code_strings_and_imports(rel: str) -> tuple[str, list[str]]:
    """提取非 docstring 字符串字面量与会话期导入名（供边界静态扫描）。"""
    text = (Path(__file__).resolve().parents[2] / rel).read_text(encoding="utf-8")
    tree = ast.parse(text)
    docstrings = set()
    for node in [tree, *ast.walk(tree)]:
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)
    strings: list[str] = []
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value not in docstrings:
                strings.append(node.value)
        elif isinstance(node, ast.Import):
            imports.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    return "\n".join(strings), imports


class _CountingLLM:
    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, *a, **k):
        self.calls += 1
        return {"status": "ok", "content": "{}", "tool_calls": []}


class TestOnlineNoJsonlDependency:
    def test_online_b2_paths_no_cleaned_users_or_file_reads(self) -> None:
        """b2 service/repository、c3 tool_handler、c4 不再读 CLEANED_USERS/JSONL。"""
        for rel in ONLINE_FILES:
            strings, imports = _code_strings_and_imports(rel)
            assert "CLEANED_USERS" not in strings, rel
            assert ".jsonl" not in strings, rel
            assert "data/cleaned" not in strings, rel
            assert "core.paths" not in imports, rel
            assert "pathlib" not in imports, rel


class TestNoLlmWhenB2Unavailable:
    def test_mysql_unavailable_does_not_call_llm(self, user_records) -> None:
        """B2 source 不可用 → runner fail（PERMANENT_CONSTRAINT_LOAD_FAILED），不调 LLM。"""
        _reset_d1()

        class _FailingSource:
            def load_users(self, expected_build_id=None):
                raise ProfileRepositoryError("B2_DATABASE_UNAVAILABLE", "mysql down")

        c4 = _isolate_runtime(ContextService(
            memory_source=InMemorySessionMemorySource(),
            permanent_constraint_loader=B2PermanentConstraintLoader(source=_FailingSource())))
        llm = _CountingLLM()
        runner = WorkflowRunner(build_id=BID, llm=llm, c4=c4)
        runner.run(RID, _uniq("sess"), "推荐家常菜", [{"participant_ref": "p1", "user_id": "1"}])
        status = d1_api.get_request_status(RID)[1]["status"]
        assert status == "failed"
        error = d1_api.get_request_status(RID)[1].get("error") or {}
        assert error.get("code") == "PERMANENT_CONSTRAINT_LOAD_FAILED"
        assert llm.calls == 0  # 不得进入第一个模型节点

    def test_build_mismatch_does_not_call_llm(self, user_records) -> None:
        """expected build 与 ready build 不一致 → fail-closed，不调 LLM。"""
        _reset_d1()
        source = InMemoryUserProfileSource(
            copy.deepcopy(user_records), ready_builds=["build-OTHER"])
        c4 = _isolate_runtime(ContextService(
            memory_source=InMemorySessionMemorySource(),
            permanent_constraint_loader=B2PermanentConstraintLoader(source=source)))
        llm = _CountingLLM()
        runner = WorkflowRunner(build_id=BID, llm=llm, c4=c4)
        runner.run(RID, _uniq("sess"), "推荐家常菜", [{"participant_ref": "p1", "user_id": "1"}])
        status = d1_api.get_request_status(RID)[1]["status"]
        assert status == "failed"
        error = d1_api.get_request_status(RID)[1].get("error") or {}
        assert error.get("code") == "PERMANENT_CONSTRAINT_LOAD_FAILED"
        assert llm.calls == 0

    def test_real_loader_wiring_loads_permanent_constraints(self, user_records) -> None:
        """真实 B2PermanentConstraintLoader + 匹配 build 的 source → 永久约束进入会话。"""
        _reset_d1()
        records = copy.deepcopy(user_records)
        for record in records:
            record["build_id"] = BID
        source = InMemoryUserProfileSource(records, ready_builds=[BID])
        c4 = _isolate_runtime(ContextService(
            memory_source=InMemorySessionMemorySource(),
            permanent_constraint_loader=B2PermanentConstraintLoader(source=source)))
        llm = _CountingLLM()
        runner = WorkflowRunner(build_id=BID, llm=llm, c4=c4)
        sid = _uniq("sess")
        runner.run(RID, sid, "推荐家常菜", [{"participant_ref": "p1", "user_id": "1"}])
        ctx = c4._sessions.get(sid)
        assert ctx is not None
        codes = {c.constraint_code for c in ctx.effective_constraints
                 if c.scope == ConstraintScope.PERMANENT}
        assert "disease_hypertension" in codes
        assert "allergy_seafood" in codes or "allergy_peanut" in codes

    def test_empty_build_identity_fails_in_permanent_loader(self, user_records) -> None:
        source = InMemoryUserProfileSource(
            user_records, ready_builds=[str(user_records[0]["build_id"])])
        loader = B2PermanentConstraintLoader(source=source)
        with pytest.raises(ProfileRepositoryError) as excinfo:
            loader.load({"p1": 1}, build_id="")
        assert excinfo.value.code == "BUILD_IDENTITY_MISMATCH"

    def test_multiplayer_retrieval_does_not_swallow_b2_failure(self, monkeypatch) -> None:
        """多人偏好读取的 B2 故障必须成为失败回执，不能降级为普通检索。"""
        class _RetrievalService:
            def retrieve(self, *args, **kwargs):
                raise AssertionError("B2 失败后不得继续普通检索")

        def _fail_load(self, expected_build_id=None):
            raise ProfileRepositoryError("B2_DATABASE_UNAVAILABLE", "mysql down")

        monkeypatch.setattr("food_agent_v2.c1.get_retrieval_service",
                            lambda: _RetrievalService())
        monkeypatch.setattr(UserHealthProfileService, "load", _fail_load)
        ctx = ToolContext(
            request_id=RID,
            build_id=BID,
            participant_user_mapping={"p1": 1, "p2": 2},
        )
        with pytest.raises(ProfileRepositoryError) as excinfo:
            _retrieve_recipes({"query": "清淡家常菜"}, ctx)
        assert excinfo.value.code == "B2_DATABASE_UNAVAILABLE"

    def test_multiplayer_b2_failure_produces_failed_tool_receipt(self, monkeypatch) -> None:
        """真实 ToolHandler 边界把 B2 异常记录为失败回执，而不是成功检索。"""
        class _RetrievalService:
            pass

        def _fail_load(self, expected_build_id=None):
            raise ProfileRepositoryError("B2_DATABASE_UNAVAILABLE", "mysql down")

        monkeypatch.setattr("food_agent_v2.c1.get_retrieval_service",
                            lambda: _RetrievalService())
        monkeypatch.setattr(UserHealthProfileService, "load", _fail_load)
        ctx = ToolContext(
            request_id=RID,
            node_id="query_understanding",
            build_id=BID,
            participant_user_mapping={"p1": 1, "p2": 2},
        )

        result = ToolHandler(ctx).execute("retrieve_recipes", {"query": "清淡家常菜"})

        assert result["error"].startswith("TOOL_EXECUTION_FAILED:")
        assert len(ctx.tool_receipts) == 1
        assert ctx.tool_receipts[0]["success"] is False
        assert ctx.tool_receipts[0]["error_code"].startswith("TOOL_EXECUTION_FAILED:")


class TestPrivacyBoundary:
    def test_public_answer_schema_has_no_privacy_fields(self) -> None:
        """公共回答 Artifact 不携带 user_id/疾病/过敏/健康指标字段。"""
        from food_agent_v2.contracts.artifacts import AnswerArtifact, AnswerContent

        fields = set(AnswerArtifact.model_fields) | set(AnswerContent.model_fields)
        for forbidden in ("user_id", "allergies", "diseases", "health_metrics",
                          "dietary_preferences", "special_group"):
            assert forbidden not in fields

    def test_b2_projection_never_exposes_user_id(self, user_profiles_source) -> None:
        """B2 角色投影只含匿名 participant_ref 与约束摘要，不含 user_id/详情。"""
        svc = UserHealthProfileService(source=user_profiles_source)
        svc.load()
        cs = svc.derive_constraints(1, "p1")
        for role in ("query_view", "model_view", "b4_view"):
            projection = svc.project_health_context(role, [cs])
            assert projection[0]["participant_ref"] == "p1"
            assert "user_id" not in projection[0]
            assert "disease" not in projection[0]
            assert "allergies" not in projection[0]
