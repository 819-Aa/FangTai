"""Regression coverage for authoritative validation and health revision boundaries."""

import json
import uuid
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from food_agent_v2.c2.schemas import FeasibleMenu, menu_hash_for
from food_agent_v2.c3.agent_actions import ActionType, AgentAction
from food_agent_v2.c3.agent_policy import AgentPolicy
from food_agent_v2.c3.graph_orchestrator import LangGraphRecommendationOrchestrator
from food_agent_v2.c3.state import RequestStatus, WorkflowError, WorkflowState
from food_agent_v2.c3.tool_handler import ToolContext, ToolHandler
from food_agent_v2.contracts.artifacts import FinalValidationArtifact


@pytest.fixture
def execution(monkeypatch):
    request_id = str(uuid.uuid4())
    build_id = str(uuid.uuid4())
    plan = FeasibleMenu(
        plan_id="current-plan", recipe_ids=[1, 2],
        menu_hash=menu_hash_for("current-plan", [1, 2]),
        dominant_objective="balanced",
    )
    ctx = ToolContext(
        request_id=request_id, build_id=build_id,
        participant_user_mapping={"p1": 1},
    )
    ctx.previous_results["feasible_menus"] = [plan]
    state = {
        "request_id": request_id, "build_id": build_id,
        "participant_refs": ["p1"], "tool_context": ctx,
        "workflow_state": WorkflowState(
            request_id=request_id, build_id=build_id,
            status=RequestStatus.RUNNING, participant_refs=["p1"],
        ),
        "current_action": AgentAction(
            action=ActionType.VALIDATE_SELECTED_MENU,
            arguments={"plan_id": plan.plan_id, "recipe_ids": [1, 2]},
        ),
        "policy": AgentPolicy(), "feasible_menus": [plan],
        "candidate_recipes": [1, 2, 3], "safe_recipe_ids": [1, 2, 3],
        "execution_context": {
            "health_evaluated": True, "safe_recipe_ids": {1, 2, 3},
            "feasible_plan_ids": {plan.plan_id}, "known_candidates": {1, 2, 3},
            "known_evidence": set(),
        },
    }
    runner = LangGraphRecommendationOrchestrator(llm=SimpleNamespace())
    monkeypatch.setattr(runner, "_guard_active", lambda *args: None)
    return runner, state


def validation(state, **changes):
    plan = state["feasible_menus"][0]
    artifact = FinalValidationArtifact(
        artifact_id=uuid.uuid4(), request_id=uuid.UUID(state["request_id"]),
        plan_id=plan.plan_id, recipe_ids=tuple(plan.recipe_ids),
        menu_artifact_ref=str(uuid.uuid4()), participant_refs=("p1",),
        participant_recipe_results=(), menu_hash=plan.menu_hash,
        input_fingerprint="0" * 64, status="PASS",
    )
    return artifact.model_copy(update=changes)


def install_validation(monkeypatch, artifact):
    def execute(handler, name, arguments):
        assert name == "validate_selected_menu_health"
        handler._ctx.previous_results["final_validation"] = artifact
        return {
            "verdict": artifact.status, "plan_id": artifact.plan_id,
            "recipe_ids": list(artifact.recipe_ids), "menu_hash": artifact.menu_hash,
            "final_validation_ref": str(artifact.artifact_id),
        }
    monkeypatch.setattr(ToolHandler, "execute", execute)


def test_validation_error_cannot_reuse_previous_pass(execution, monkeypatch):
    runner, state = execution
    stale = validation(state)
    state["tool_context"].previous_results["final_validation"] = stale
    state["execution_context"].update(final_validation=stale, final_validation_ref=str(stale.artifact_id))
    monkeypatch.setattr(ToolHandler, "execute", lambda *args: {"error": "DATABASE_UNAVAILABLE"})

    result = runner._node_execute_tool(state)

    assert result["workflow_state"].status == RequestStatus.FAILED
    assert result["observations"][-1].status == "error"
    assert "DATABASE_UNAVAILABLE" in result["observations"][-1].error_code
    assert "final_validation" not in state["tool_context"].previous_results
    assert not result["execution_context"].get("final_validation")


def test_mock_handler_without_receipt_must_not_fabricate_pass(execution, monkeypatch):
    runner, state = execution
    monkeypatch.setattr("food_agent_v2.c3.graph_orchestrator.ToolHandler", MagicMock())
    monkeypatch.setattr(runner, "_select_validate_answer", MagicMock())

    result = runner._node_execute_tool(state)

    assert result["workflow_state"].status == RequestStatus.FAILED
    assert "final_validation" not in state["tool_context"].previous_results
    assert not result["execution_context"].get("final_validation")


@pytest.mark.parametrize("changes", [
    {"request_id": uuid.uuid4()}, {"plan_id": "other-plan"},
    {"recipe_ids": (1, 3)}, {"participant_refs": ("other-person",)},
    {"menu_hash": "f" * 64},
])
def test_final_validation_must_bind_current_request_and_menu(execution, monkeypatch, changes):
    runner, state = execution
    install_validation(monkeypatch, validation(state, **changes))
    result = runner._node_execute_tool(state)
    assert result["workflow_state"].status == RequestStatus.FAILED
    assert not result["execution_context"].get("final_validation")


def test_current_validation_pass_is_accepted(execution, monkeypatch):
    runner, state = execution
    artifact = validation(state)
    install_validation(monkeypatch, artifact)
    result = runner._node_execute_tool(state)
    assert result["observations"][-1].status == "ok"
    assert result["execution_context"]["final_validation"] == artifact


@pytest.mark.parametrize("source", ["invoke", "scripted"])
@pytest.mark.parametrize("payload", [
    {"action": "search_candidates", "arguments": {"query": "dinner"}, "extra": True},
    {"action": "search_candidates", "arguments": {"query": "dinner"}, "evidence_refs": "invalid"},
    {"action": "invented_action", "arguments": {}},
])
def test_invalid_action_envelope_fails_without_reconstruction(execution, source, payload):
    runner, state = execution
    if source == "invoke":
        runner._llm = SimpleNamespace(invoke=lambda messages: {"content": json.dumps(payload)})
    else:
        runner._llm = SimpleNamespace(decide_action=lambda state: payload)
    result = runner._node_decide(state)
    assert result["workflow_state"].status == RequestStatus.FAILED
    assert result["error_code"] == "MODEL_ACTION_SCHEMA_INVALID"


def test_exclude_invalidates_old_health_and_planning_evidence(execution, monkeypatch):
    runner, state = execution
    ctx = state["tool_context"]
    ctx.previous_results["health_evaluation"] = object()
    ctx.previous_results["feasible_menu_artifact"] = object()
    artifact = validation(state, status="EXCLUDE")
    install_validation(monkeypatch, artifact)

    result = runner._node_execute_tool(state)
    state.update(result)

    assert result["observations"][-1].status == "no_solution"
    assert result["execution_context"]["is_health_revision"] is True
    assert not result["execution_context"].get("health_evaluated")
    assert not result["execution_context"].get("feasible_plan_ids")
    assert not state.get("safe_recipe_ids")
    assert not state.get("feasible_menus")
    assert "health_evaluation" not in ctx.previous_results
    assert "feasible_menus" not in ctx.previous_results
    combine = AgentAction(action=ActionType.COMBINE_NUTRITIONAL_MENU, arguments={})
    gate = state["policy"].validate_action_gate(combine, state["execution_context"])
    assert not gate.allowed
    assert gate.error_code == "AUDIT_REQUIRED_BEFORE_COMBINE"


def test_finish_requires_exact_validation_reference(execution):
    _, state = execution
    artifact = validation(state)
    context = state["execution_context"]
    context.update(final_validation=artifact, final_validation_ref=str(artifact.artifact_id))
    action = AgentAction(action=ActionType.FINISH, arguments={
        "plan_id": artifact.plan_id, "final_validation_ref": "wrong-ref",
    })
    assert not state["policy"].validate_action_gate(action, context).allowed


def test_select_validate_answer_mismatch_plan_id_fails_closed(execution, monkeypatch):
    runner, state = execution
    mismatch_artifact = validation(state, plan_id="unrelated-plan-id")
    state["tool_context"].previous_results["final_validation"] = mismatch_artifact
    monkeypatch.setattr(
        runner,
        "_build_dual_artifacts",
        lambda st, ctx: (st, SimpleNamespace(artifact_id=uuid.uuid4())),
    )
    res_wf = runner._select_validate_answer(
        state["workflow_state"],
        state["tool_context"],
        state["feasible_menus"],
        state["request_id"],
        state["participant_refs"],
        c4=None,
        session_id=state.get("session_id", ""),
        lock_token=state.get("lock_token", ""),
        lost=None,
    )
    assert res_wf.status == RequestStatus.FAILED
    assert res_wf.error is not None
    assert res_wf.error.error_code == "FINAL_HEALTH_VALIDATION_PLAN_MISMATCH"
    assert "不在当前可行方案列表中" in res_wf.error.message


def test_upstream_combine_invalidates_downstream_validation(execution, monkeypatch):
    runner, state = execution
    pass_artifact = validation(state)
    install_validation(monkeypatch, pass_artifact)
    val_res = runner._node_execute_tool(state)
    state.update(val_res)
    assert state["execution_context"]["final_validation"] == pass_artifact
    assert state["tool_context"].previous_results["final_validation"] == pass_artifact

    # Upstream COMBINE_NUTRITIONAL_MENU re-runs
    monkeypatch.setattr(
        runner,
        "_node_combine_menu",
        lambda st: {
            "feasible_menus": [
                FeasibleMenu(
                    plan_id="plan-new",
                    recipe_ids=[1, 2, 3],
                    menu_hash=menu_hash_for("plan-new", [1, 2, 3]),
                    dominant_objective="balanced",
                )
            ]
        },
    )
    state["current_action"] = AgentAction(
        action=ActionType.COMBINE_NUTRITIONAL_MENU,
        arguments={"dish_count": 3},
    )
    combine_res = runner._node_execute_tool(state)
    state.update(combine_res)

    assert "final_validation" not in state["tool_context"].previous_results
    assert not state["execution_context"].get("final_validation")
    assert not state["execution_context"].get("final_validation_ref")
    assert state["execution_context"]["feasible_plan_ids"] == {"plan-new"}

    finish_action = AgentAction(
        action=ActionType.FINISH,
        arguments={"plan_id": "plan-new", "final_validation_ref": str(pass_artifact.artifact_id)},
    )
    gate = state["policy"].validate_action_gate(finish_action, state["execution_context"])
    assert not gate.allowed
    assert gate.error_code == "FINAL_HEALTH_VALIDATION_MISSING"


def test_upstream_audit_invalidates_downstream_plans_and_validation(execution, monkeypatch):
    runner, state = execution
    pass_artifact = validation(state)
    install_validation(monkeypatch, pass_artifact)
    val_res = runner._node_execute_tool(state)
    state.update(val_res)
    assert state["execution_context"]["final_validation"] == pass_artifact

    # Upstream AUDIT_RECIPE_HEALTH re-runs
    monkeypatch.setattr(
        runner,
        "_node_audit_health",
        lambda st: {"safe_recipe_ids": [1, 2], "excluded_recipe_ids": []},
    )
    state["current_action"] = AgentAction(
        action=ActionType.AUDIT_RECIPE_HEALTH,
        arguments={"candidate_recipe_ids": [1, 2]},
    )
    audit_res = runner._node_execute_tool(state)
    state.update(audit_res)

    assert "feasible_menus" not in state["tool_context"].previous_results
    assert "final_validation" not in state["tool_context"].previous_results
    assert not state["execution_context"].get("feasible_plan_ids")
    assert not state["execution_context"].get("final_validation")

    val_action = AgentAction(
        action=ActionType.VALIDATE_SELECTED_MENU,
        arguments={"plan_id": "plan-stale", "recipe_ids": [1, 2]},
    )
    gate = state["policy"].validate_action_gate(val_action, state["execution_context"])
    assert not gate.allowed
    assert gate.error_code == "UNKNOWN_PLAN_ID"


def test_ask_user_gate_validation():
    """验证 ASK_USER 选项数量约束 (2~3项) 与 modifications 字段白名单。"""
    policy = AgentPolicy()
    exec_ctx = {"known_evidence": set()}

    # 1. 选项数量少于 2 项 -> 拒绝
    action_1_opt = AgentAction(
        action=ActionType.ASK_USER,
        arguments={
            "inquiry_category": "NEEDS_CLARIFICATION",
            "reason": "测试原因",
            "options": [{"option_id": 1, "text": "单一选项", "modifications": {}}],
        },
    )
    gate = policy.validate_action_gate(action_1_opt, exec_ctx)
    assert not gate.allowed
    assert gate.error_code == "INVALID_OPTION_COUNT"

    # 2. 选项数量超过 3 项 -> 拒绝
    action_4_opts = AgentAction(
        action=ActionType.ASK_USER,
        arguments={
            "inquiry_category": "NEEDS_CLARIFICATION",
            "reason": "测试原因",
            "options": [
                {"option_id": 1, "text": "选项1", "modifications": {}},
                {"option_id": 2, "text": "选项2", "modifications": {}},
                {"option_id": 3, "text": "选项3", "modifications": {}},
                {"option_id": 4, "text": "选项4", "modifications": {}},
            ],
        },
    )
    gate = policy.validate_action_gate(action_4_opts, exec_ctx)
    assert not gate.allowed
    assert gate.error_code == "INVALID_OPTION_COUNT"

    # 3. 包含白名单之外的修改字段 -> 拒绝
    action_bad_mod = AgentAction(
        action=ActionType.ASK_USER,
        arguments={
            "inquiry_category": "NEEDS_CLARIFICATION",
            "reason": "测试原因",
            "options": [
                {"option_id": 1, "text": "合法选项", "modifications": {"dish_count_requested": 3}},
                {"option_id": 2, "text": "非法选项", "modifications": {"unauthorized_key": "bad"}},
            ],
        },
    )
    gate = policy.validate_action_gate(action_bad_mod, exec_ctx)
    assert not gate.allowed
    assert gate.error_code == "UNSUPPORTED_MODIFICATION_FIELD"

    # 4. 合法 2 项且 modifications 均在白名单 -> 通过
    action_valid = AgentAction(
        action=ActionType.ASK_USER,
        arguments={
            "inquiry_category": "NEEDS_CLARIFICATION",
            "reason": "测试原因",
            "options": [
                {"option_id": 1, "text": "改餐次和口味", "modifications": {"meal_type": "lunch", "taste_tags": ["清淡"]}},
                {"option_id": 2, "text": "减少菜数", "modifications": {"dish_count_requested": 2}},
            ],
        },
    )
    gate = policy.validate_action_gate(action_valid, exec_ctx)
    assert gate.allowed


def test_format_agent_prompt_projection():
    """验证 format_agent_prompt 包含原始输入、意图、锁定与排除菜品等关键上下文。"""
    from food_agent_v2.c3.agent_prompts import format_agent_prompt
    from food_agent_v2.c3.fast_intent import IntentDelta

    intent = IntentDelta(intent="replace", target_recipe_id=103)
    prompt = format_agent_prompt(
        query_plan=None,
        observations=[],
        current_menu_summary="[101] 宫保鸡丁, [102] 西红柿炒蛋, [103] 青椒肉丝",
        user_message="把青椒肉丝换成红烧茄子",
        intent=intent,
        locked_recipes_summary="[101] 宫保鸡丁, [102] 西红柿炒蛋",
        rejected_recipes_summary="[103] 青椒肉丝",
    )

    assert "把青椒肉丝换成红烧茄子" in prompt
    assert "局部菜品替换" in prompt
    assert "必须保留的锁定菜品: [101] 宫保鸡丁, [102] 西红柿炒蛋" in prompt
    assert "已明确排除的目标菜品: [103] 青椒肉丝" in prompt
    assert "[101] 宫保鸡丁, [102] 西红柿炒蛋, [103] 青椒肉丝" in prompt


@pytest.mark.parametrize(
    ("refs", "expected"),
    [([], "当前可用证据引用: []"), (["history:abc123"], '当前可用证据引用: ["history:abc123"]')],
)
def test_agent_prompt_exposes_only_current_evidence_refs(refs, expected):
    """模型只能引用本次 Gate 认可的证据，不能自拟语义引用名。"""
    from food_agent_v2.c3.agent_prompts import format_agent_prompt

    prompt = format_agent_prompt(
        query_plan=None,
        observations=[],
        available_evidence_refs=refs,
    )

    assert expected in prompt
    assert "evidence_refs 只能" in prompt


def test_agent_decision_receives_gate_evidence_refs(execution):
    """编排器必须把 Gate 的允许引用传到真实模型调用边界。"""
    runner, state = execution
    captured = []
    state["execution_context"]["known_evidence"] = {"history:abc123"}
    state["message"] = "恢复上一版菜单"

    def invoke(role, system_prompt, user_message, **kwargs):
        captured.append(user_message)
        return {"content": json.dumps({
            "action": "audit_recipe_health",
            "arguments": {"candidate_recipe_ids": [1]},
            "evidence_refs": ["history:abc123"],
        })}

    runner._llm = SimpleNamespace(invoke=invoke)
    action = runner._decide_next_action(state, state["policy"])

    assert action.evidence_refs == ["history:abc123"]
    assert '当前可用证据引用: ["history:abc123"]' in captured[0]


def test_commit_failure_keeps_pending_clarification_for_retry(execution, monkeypatch):
    """业务提交返回失败时，选择仍须留在 C4，供下次请求重试。"""
    runner, state = execution
    consumed = []
    state.update(
        session_id="session-retry",
        lock_token="token-retry",
        pending_clarification_to_consume="question-retry",
        c4=SimpleNamespace(
            consume_pending_clarification=lambda *args, **kwargs: consumed.append((args, kwargs))
        ),
    )
    monkeypatch.setattr(runner, "_finalize", lambda *args, **kwargs: "failed")

    runner._node_commit(state)

    assert consumed == []


def test_qwen_agent_decision_uses_bounded_reasoning_without_changing_other_roles(monkeypatch):
    """行动选择使用低推理档，避免 Qwen3.8 默认 xhigh 耗尽单次时限。"""
    from food_agent_v2.c3.llm_client import LLMClient
    from food_agent_v2.core.config import LLMConfig

    cfg = LLMConfig(
        api_key="test", base_url="https://example.invalid",
        model_reasoning="qwen3.8-max",
    )
    monkeypatch.setattr(
        "food_agent_v2.c3.llm_client.load_config", lambda: SimpleNamespace(llm=cfg)
    )
    client = LLMClient()
    calls = []
    monkeypatch.setattr(client, "_call_openai", lambda **kw: calls.append(kw) or {"content": "{}"})

    client.invoke("menu_decision", "system", "next action")
    client.invoke("answer_generation", "system", "answer")

    assert calls[0]["reasoning_effort"] == "low"
    assert calls[1]["reasoning_effort"] is None


@pytest.mark.parametrize(
    ("extra_body", "expected_effort"),
    [({"reasoning_effort": "medium"}, "medium"), ({"thinking_budget": 2048}, None)],
)
def test_qwen_agent_decision_respects_explicit_reasoning_config(
    monkeypatch, extra_body, expected_effort
):
    from food_agent_v2.c3.llm_client import LLMClient
    from food_agent_v2.core.config import LLMConfig

    cfg = LLMConfig(
        api_key="test", base_url="https://example.invalid",
        model_reasoning="qwen3.8-max", reasoning_extra_body=extra_body,
    )
    monkeypatch.setattr(
        "food_agent_v2.c3.llm_client.load_config", lambda: SimpleNamespace(llm=cfg)
    )
    client = LLMClient()
    calls = []
    monkeypatch.setattr(client, "_call_openai", lambda **kw: calls.append(kw) or {"content": "{}"})

    client.invoke("menu_decision", "system", "next action")

    assert calls[0]["reasoning_effort"] == expected_effort
    assert calls[0]["extra_body"] == (
        {} if expected_effort == "medium" else {"thinking_budget": 2048}
    )


def test_application_commit_error_does_not_consume_pending_selection(execution, monkeypatch):
    """真实 _finalize 会吞掉提交异常并返回 failed，提交节点仍要保留选择。"""
    runner, state = execution
    consumed = []
    statuses = []
    state.update(
        session_id="session-commit-error",
        lock_token="token-commit-error",
        pending_clarification_to_consume="question-commit-error",
        c4=SimpleNamespace(
            consume_pending_clarification=lambda *args, **kwargs: consumed.append(args),
            commit_session_state=lambda *args, **kwargs: None,
        ),
    )
    state["workflow_state"] = replace(
        state["workflow_state"],
        status=RequestStatus.COMPLETED,
        final_validation_artifact=validation(state),
    )
    monkeypatch.setattr(
        "food_agent_v2.application.commit_request_result",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("commit failed")),
    )
    monkeypatch.setattr(
        "food_agent_v2.application.menu_projection.build_public_menu",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        "food_agent_v2.c3.runtime.d1_api.update_status",
        lambda request_id, status, **kwargs: statuses.append(status),
    )
    monkeypatch.setattr("food_agent_v2.c3.runtime.d1_api.publish_terminal", lambda *args, **kwargs: None)

    runner._node_commit(state)

    assert statuses == ["failed"]
    assert consumed == []


def test_completed_commit_records_accepted_question_in_mysql_envelope(execution, monkeypatch):
    """业务提交事实必须携带已接受的问题 ID，以便 Redis 失败后阻断重放。"""
    runner, state = execution
    captured = []
    state["workflow_state"] = replace(
        state["workflow_state"],
        status=RequestStatus.COMPLETED,
        final_validation_artifact=validation(state),
        shared_context_ref="session-durable-question",
    )
    c4 = SimpleNamespace(commit_session_state=lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "food_agent_v2.application.commit_request_result",
        lambda **kwargs: captured.append(kwargs) or {"committed": True, "status": "completed"},
    )
    monkeypatch.setattr(
        "food_agent_v2.application.menu_projection.build_public_menu",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr("food_agent_v2.c3.runtime.d1_api.update_status", lambda *args, **kwargs: None)

    status = runner._finalize(
        state["workflow_state"], state["request_id"], c4, "token-1",
        clarification_question_id="question-durable",
    )

    assert status == "completed"
    assert captured[0]["health_evidence"]["accepted_clarification_question_id"] == "question-durable"


def test_followup_clarification_records_accepted_question_in_mysql_envelope(execution, monkeypatch):
    """接受旧选项后再次追问，也必须先持久化旧问题的已接受事实。"""
    runner, state = execution
    captured = []
    state["workflow_state"] = replace(
        state["workflow_state"],
        status=RequestStatus.NEEDS_CLARIFICATION,
        shared_context_ref="session-followup-question",
    )
    c4 = SimpleNamespace(commit_session_state=lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "food_agent_v2.application.commit_request_result",
        lambda **kwargs: captured.append(kwargs) or {
            "committed": True, "status": "needs_clarification"
        },
    )
    monkeypatch.setattr("food_agent_v2.c3.runtime.d1_api.update_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "food_agent_v2.c3.runtime.d1_api.publish_clarification_event",
        lambda *args, **kwargs: None,
    )

    status = runner._finalize(
        state["workflow_state"], state["request_id"], c4, "token-1",
        clarification_question_id="question-followup",
    )

    assert status == "needs_clarification"
    assert len(captured) == 1
    assert captured[0]["status"] == "needs_clarification"
    assert captured[0]["health_evidence"]["accepted_clarification_question_id"] == "question-followup"


def test_followup_clarification_commit_error_does_not_consume_old_question(execution, monkeypatch):
    runner, state = execution
    consumed = []
    statuses = []
    state.update(
        session_id="session-followup-error",
        lock_token="token-followup-error",
        pending_clarification_to_consume="question-followup-error",
        c4=SimpleNamespace(
            consume_pending_clarification=lambda *args, **kwargs: consumed.append(args),
            commit_session_state=lambda *args, **kwargs: None,
        ),
    )
    state["workflow_state"] = replace(
        state["workflow_state"],
        status=RequestStatus.NEEDS_CLARIFICATION,
        shared_context_ref="session-followup-error",
    )
    monkeypatch.setattr(
        "food_agent_v2.application.commit_request_result",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("commit failed")),
    )
    monkeypatch.setattr(
        "food_agent_v2.c3.runtime.d1_api.update_status",
        lambda request_id, status, **kwargs: statuses.append(status),
    )
    monkeypatch.setattr("food_agent_v2.c3.runtime.d1_api.publish_terminal", lambda *args, **kwargs: None)

    runner._node_commit(state)

    assert statuses == ["failed"]
    assert consumed == []


def test_clarification_commit_failure_reports_commit_error_not_inquiry_reason(execution, monkeypatch):
    runner, state = execution
    state["workflow_state"] = replace(
        state["workflow_state"],
        status=RequestStatus.NEEDS_CLARIFICATION,
        shared_context_ref="session-commit-error",
        error=WorkflowError("constraint_conflict", "请先选择放宽时间或减少菜数"),
    )
    captured = []
    monkeypatch.setattr(
        "food_agent_v2.application.commit_request_result",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("CLARIFICATION_REVISION_REQUIRED")),
    )
    monkeypatch.setattr(
        "food_agent_v2.c3.runtime.d1_api.update_status",
        lambda _rid, status, **kwargs: captured.append((status, kwargs.get("error"))),
    )
    monkeypatch.setattr("food_agent_v2.c3.runtime.d1_api.publish_terminal", lambda *args, **kwargs: None)

    status = runner._finalize(
        state["workflow_state"], state["request_id"],
        SimpleNamespace(commit_session_state=lambda *args, **kwargs: None), "458",
    )

    assert status == "failed"
    assert captured == [("failed", {
        "code": "AUDIT_COMMIT_FAILED", "message": "CLARIFICATION_REVISION_REQUIRED",
    })]


def test_duplicate_selection_after_mysql_commit_is_rejected(execution):
    """即使 Redis 留有旧 pending，MySQL 已提交的选项也不能再应用一次。"""
    runner, state = execution
    sid = "session-stale-pending"
    consumed = []
    state.update(
        session_id=sid,
        message="选第一个",
        lock_token="token-1",
        user_id_mapping={"p1": 1},
        c4=SimpleNamespace(
            build_shared_context=lambda *args, **kwargs: (SimpleNamespace(session_id=sid), None),
            validate_context_integrity=lambda *args, **kwargs: {"valid": True},
            get_pending_clarifications=lambda *args: [{
                "question_id": "question-durable", "status": "pending",
                "expires_at": 9999999999,
                "options": [
                    {"option_id": 1, "text": "接受清淡", "modifications": {"taste_tags": ["清淡"]}},
                    {"option_id": 2, "text": "接受香辣", "modifications": {"taste_tags": ["香辣"]}},
                ],
            }],
            is_clarification_committed=lambda *args: True,
            consume_pending_clarification=lambda *args, **kwargs: consumed.append(kwargs["question_id"]),
        ),
    )

    result = runner._node_understand_intent(state)

    assert result["is_terminal"] is True
    assert result["error_code"] == "CLARIFICATION_ALREADY_APPLIED"
    assert consumed == ["question-durable"]


def test_older_committed_pending_is_cleaned_behind_new_question(execution):
    """再次追问时新问题在前，旧问题的 Redis 残留仍应被清除。"""
    runner, state = execution
    sid = "session-two-pendings"
    consumed = []
    state.update(
        session_id=sid,
        message="选第一个",
        lock_token="token-1",
        user_id_mapping={"p1": 1},
        c4=SimpleNamespace(
            build_shared_context=lambda *args, **kwargs: (SimpleNamespace(session_id=sid), None),
            validate_context_integrity=lambda *args, **kwargs: {"valid": True},
            get_pending_clarifications=lambda *args: [
                {
                    "question_id": "old-committed", "status": "pending",
                    "options": [{"option_id": 1, "text": "旧选项", "modifications": {}}],
                },
                {
                    "question_id": "new-uncommitted", "status": "pending",
                    "expires_at": 9999999999,
                    "options": [{"option_id": 1, "text": "新选项", "modifications": {}}],
                },
            ],
            is_clarification_committed=lambda _sid, qid: qid == "old-committed",
            consume_pending_clarification=lambda *args, **kwargs: consumed.append(kwargs["question_id"]),
        ),
    )

    result = runner._node_understand_intent(state)

    assert consumed == ["old-committed"]
    assert result.get("pending_clarification_to_consume") == "new-uncommitted"


def test_redis_consume_error_after_business_commit_keeps_completed(execution, monkeypatch):
    """MySQL 已提交时，Redis 投影故障不能把 completed 覆盖成 failed。"""
    runner, state = execution
    state.update(
        session_id="session-redis-error",
        lock_token="token-redis-error",
        pending_clarification_to_consume="question-redis-error",
        c4=SimpleNamespace(
            consume_pending_clarification=lambda *args, **kwargs: (_ for _ in ()).throw(
                RuntimeError("redis unavailable")
            ),
        ),
    )
    monkeypatch.setattr(runner, "_finalize", lambda *args, **kwargs: "completed")

    result = runner._node_commit(state)

    assert result == {}


def test_redis_consume_error_after_followup_clarification_keeps_terminal(execution, monkeypatch):
    """旧问题已被 MySQL 标记，Redis 故障不能把新澄清覆盖为 failed。"""
    runner, state = execution
    state.update(
        session_id="session-followup-redis-error",
        lock_token="token-followup-redis-error",
        pending_clarification_to_consume="question-followup-redis-error",
        c4=SimpleNamespace(
            consume_pending_clarification=lambda *args, **kwargs: (_ for _ in ()).throw(
                RuntimeError("redis unavailable")
            ),
        ),
    )
    monkeypatch.setattr(runner, "_finalize", lambda *args, **kwargs: "needs_clarification")

    assert runner._node_commit(state) == {}


@pytest.mark.parametrize("terminal_status", ["completed", "needs_clarification"])
def test_c4_projection_error_after_mysql_commit_keeps_terminal(
    execution, monkeypatch, terminal_status
):
    runner, state = execution
    state["workflow_state"] = replace(
        state["workflow_state"],
        status=RequestStatus(terminal_status),
        shared_context_ref="session-projection-error",
    )
    c4 = SimpleNamespace(
        commit_session_state=lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("redis projection unavailable")
        ),
    )
    if terminal_status == "completed":
        state["workflow_state"] = replace(
            state["workflow_state"], final_validation_artifact=validation(state)
        )
        monkeypatch.setattr(
            "food_agent_v2.application.menu_projection.build_public_menu",
            lambda *args, **kwargs: [],
        )
    monkeypatch.setattr(
        "food_agent_v2.application.commit_request_result",
        lambda **kwargs: {"committed": True, "status": kwargs["status"]},
    )
    monkeypatch.setattr("food_agent_v2.c3.runtime.d1_api.update_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "food_agent_v2.c3.runtime.d1_api.publish_clarification_event",
        lambda *args, **kwargs: None,
    )

    assert runner._finalize(
        state["workflow_state"], state["request_id"], c4, "token-1",
        clarification_question_id="question-projection-error",
    ) == terminal_status


def test_current_menu_projection_error_after_mysql_commit_keeps_completed(
    execution, monkeypatch
):
    runner, state = execution
    state["workflow_state"] = replace(
        state["workflow_state"],
        status=RequestStatus.COMPLETED,
        shared_context_ref="session-menu-projection-error",
        final_validation_artifact=validation(state),
    )
    c4 = SimpleNamespace(
        _sessions={"session-menu-projection-error": SimpleNamespace(
            current_menu=SimpleNamespace(plan_id=None, recipe_ids=[])
        )},
        _recompute_manifest=lambda *args: None,
        _persist_session=lambda *args: (_ for _ in ()).throw(RuntimeError("redis unavailable")),
        commit_session_state=lambda *args, **kwargs: None,
    )
    statuses = []
    monkeypatch.setattr(
        "food_agent_v2.application.commit_request_result",
        lambda **kwargs: {"committed": True, "status": "completed"},
    )
    monkeypatch.setattr(
        "food_agent_v2.application.menu_projection.build_public_menu",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        "food_agent_v2.c3.runtime.d1_api.update_status",
        lambda request_id, status, **kwargs: statuses.append(status),
    )

    assert runner._finalize(state["workflow_state"], state["request_id"], c4, "token-1") == "completed"
    assert statuses == ["completed"]


def test_fast_intent_chinese_numeral_dish_count():
    """验证中文数字菜品数量解析准确无误。"""
    from food_agent_v2.c3.fast_intent import FastIntentRouter

    cases = [
        ("三道清淡家常菜", 3),
        ("推荐3道菜", 3),
        ("两道菜", 2),
        ("四个菜", 4),
        ("三菜一汤", 4),
        ("两菜一汤", 3),
        ("四菜一汤", 5),
    ]
    for text, expected in cases:
        delta = FastIntentRouter.route(text, ("p1",))
        assert delta.dish_count_requested == expected, f"Failed for {text}: expected {expected}, got {delta.dish_count_requested}"

    # 三杯鸡不应被误识别为 3 道菜
    delta_sbj = FastIntentRouter.route("推荐三杯鸡", ("p1",))
    assert delta_sbj.dish_count_requested is None


def test_pending_clarification_consumed_only_at_commit(execution, monkeypatch):
    """验证 pending clarification 在 understand_intent 匹配时不立即消耗，而在 commit 时才事务性消耗。"""
    from food_agent_v2.c2.schemas import FeasibleMenu, menu_hash_for

    runner, state = execution
    session_id = "test-sess-pending"
    state["session_id"] = session_id
    state["message"] = "1"
    state["lock_token"] = "token-1"
    state["user_id_mapping"] = {"p1": 1}

    consumed_calls = []
    stored_clarification = {
        "question_id": "q-12345",
        "status": "pending",
        "options": [
            {"option_id": 1, "text": "接受推荐 2 道菜", "modifications": {"dish_count_requested": 2}},
            {"option_id": 2, "text": "放宽时间要求", "modifications": {"time_constraint_policy": "relax"}},
        ],
        "query_plan_snapshot": {
            "request_id": str(uuid.uuid4()),
            "dish_count_requested": 3,
            "meal_types": ["dinner"],
        },
    }

    mock_c4 = SimpleNamespace(
        build_shared_context=lambda *args, **kwargs: (SimpleNamespace(session_id=session_id), None),
        validate_context_integrity=lambda *args, **kwargs: {"valid": True},
        get_session_state=lambda sid: {"current_menu": {}},
        get_pending_clarifications=lambda sid: [stored_clarification],
        consume_pending_clarification=lambda sid, question_id, token: consumed_calls.append((sid, question_id, token)),
        commit_session_state=lambda sid, record, token: True,
    )
    state["c4"] = mock_c4

    # 1. 运行 _node_understand_intent
    ui_res = runner._node_understand_intent(state)
    assert ui_res.get("pending_clarification_to_consume") == "q-12345"
    assert ui_res["query_plan"].dish_count_requested == 2
    # 关键断言：此时 C4 的 consume 尚未被调用
    assert len(consumed_calls) == 0

    # 2. 模拟后续决策失败或超时，不调用 _node_commit
    # C4 中仍保留该 clarification，不会丢失

    # 3. 模拟正常走到 _node_commit
    state.update(ui_res)
    plan = FeasibleMenu(
        plan_id="plan-1", recipe_ids=[1, 2],
        menu_hash=menu_hash_for("plan-1", [1, 2]),
        dominant_objective="balanced",
    )
    state["feasible_menus"] = [plan]
    val_artifact = validation(state)
    state["execution_context"]["final_validation"] = val_artifact
    state["final_validation"] = val_artifact
    monkeypatch.setattr(runner, "_finalize", lambda *args, **kwargs: "completed")
    commit_res = runner._node_commit(state)
    assert commit_res == {}

    # 关键断言：在 commit 节点成功执行后，consume 最终被调用
    assert len(consumed_calls) == 1
    assert consumed_calls[0] == (session_id, "q-12345", "token-1")
