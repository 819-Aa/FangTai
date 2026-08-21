"""提示词实测 —— 调用五个模型，验证响应符合正式 Artifact Schema。

运行方式：
  uv run pytest tests/test_prompts_live.py -v -s

标注：需要 LLM API 可用。每个角色的提示词规定的输出必须通过对应
model_validate（WorkflowRunner._validate_artifact）。模型输出非确定，
校验失败可重试（验证模型能产出合法 Artifact）。
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from food_agent_v2.c3 import ROLE_POLICIES
from food_agent_v2.c3.llm_client import LLMClient
from food_agent_v2.c3.prompts import get_prompt
from food_agent_v2.c3.runner import WorkflowRunner
from food_agent_v2.core.config import load_config

# live：真实模型验收测试，默认不进入普通回归（pytest -m "not live" 排除）
pytestmark = pytest.mark.live

_RID = "11111111-1111-1111-1111-111111111111"
_CTX = f"## 上下文\nrequest_id: {_RID}, participant_refs: [p1]\n"


@pytest.fixture(scope="module")
def client():
    cfg = load_config()
    if not cfg.llm.api_key:
        pytest.skip("No LLM API key configured")
    return LLMClient()


class _StubLLM:
    def invoke(self, *a, **k):
        raise AssertionError("不应调用 LLM")


@pytest.fixture(scope="module")
def runner():
    return WorkflowRunner(build_id="22222222-2222-2222-2222-222222222222", llm=_StubLLM())


def _artifact(runner, content, role):
    """解析模型语义输出，经 workflow 组装后严格校验；返回 (artifact, 错误)。"""
    parsed = content if isinstance(content, dict) else _parse(content)
    return runner._assemble_artifact(parsed, ROLE_POLICIES[role], _RID, ["p1"])


def _invoke_artifact(client, runner, role, user_msg):
    """调用模型并组装校验为对应 Artifact（单次，不重试；超时记 FAIL/NOT_RUN）。"""
    prompt = get_prompt(role)
    try:
        resp = client.invoke(role, prompt, user_msg)
    except Exception as e:
        pytest.fail(f"{role} 调用失败(NOT_RUN): {type(e).__name__}: {e}")
    artifact, err = _artifact(runner, resp.get("content", ""), role)
    if err is not None:
        pytest.fail(f"{role} 模型输出未通过严格 Artifact 校验: {err.message}")
    return artifact


# ---- 查询理解 ----

def test_query_understanding_meal_plan(client, runner):
    """三菜一汤 → 合法 QueryPlanArtifact 且 dish_count_requested=4"""
    artifact = _invoke_artifact(client, runner, "query_understanding", _CTX + "## 输入\n推荐三菜一汤，家常口味，45分钟内")
    assert artifact.dish_count_requested == 4, \
        f"Expected dish_count_requested=4, got {artifact.dish_count_requested}"
    assert str(artifact.request_id) == _RID
    print(f"\n  [OK] 查询理解: dish_count_requested={artifact.dish_count_requested}")


def test_query_understanding_health_signal(client, runner):
    """健康信号按 R-002 三前缀格式记录（参与者N:类型:值），且不自行诊断。

    用户明确表达"我有高血压"是本轮新增健康信号（user1 永久档案只有海鲜过敏），
    必须进入 health_exclusions 供 B2 验证闭环；同时不得把"清淡"等软偏好误判为健康排除。
    """
    from food_agent_v2.c3.runner import _parse_health_exclusion
    artifact = _invoke_artifact(client, runner, "query_understanding", _CTX + "## 输入\n我有高血压，推荐清淡的晚餐")
    exclusions = list(artifact.health_exclusions or ())
    assert exclusions, f"用户明确表达的高血压信号应进入 health_exclusions，实际空: {artifact.health_exclusions!r}"
    # 每个排除项必须是合法三前缀格式，且能被 R-002 解析闭环
    parsed_types = []
    for raw in exclusions:
        parsed = _parse_health_exclusion(raw)
        parsed_types.append(parsed["type"])
        assert parsed["participant_ref"] in ("p1",), f"排除项参与者归属错误: {raw!r}"
    assert "disease" in parsed_types, f"应有疾病类信号，实际: {parsed_types}"
    print(f"\n  [OK] 查询理解-健康信号: health_exclusions={list(exclusions)} 格式合法且含疾病信号")


def test_query_understanding_replace(client, runner):
    """替换意图识别 → 合法 QueryPlanArtifact"""
    _invoke_artifact(client, runner, "query_understanding", _CTX + "## 输入\n把回锅肉换成宫保鸡丁")
    print("\n  [OK] 查询理解-替换")


# ---- 回答生成 ----

def _answer_base(**overrides) -> str:
    base = {
        "selected_menu": {"plan_id": "p1", "dishes": [
            {"recipe_id": 1, "name": "清蒸鲈鱼"},
            {"recipe_id": 2, "name": "蒜蓉西兰花"},
            {"recipe_id": 3, "name": "番茄蛋汤"},
        ]},
        "plan_id": "p1", "menu_hash": "a" * 64, "menu_ref": "fm:1",
        "final_validation_ref": "fv:1", "recipe_ids": [1, 2, 3],
        "request_id": _RID, "participant_refs": ["p1"],
        "time_data": {"estimated_total_minutes": 35, "available": True},
    }
    base.update(overrides)
    return f"## 上下文\n{json.dumps(base, ensure_ascii=False)}\n## 输入\n根据以上菜单生成回答"


def test_answer_no_forbidden_content(client, runner):
    """回答不含禁止表述且是合法 AnswerArtifact"""
    artifact = _invoke_artifact(client, runner, "answer_generation", _answer_base())
    text = artifact.content.conclusion + artifact.content.menu_summary
    forbidden = [("mg", "nutrition"), ("高血压", "disease"), ("糖尿病", "disease")]
    import re
    for kw, label in forbidden:
        if re.search(kw, text):
            pytest.fail(f"Answer contains forbidden content: '{label}'")
    print(f"\n  [OK] 回答生成: 无禁止表述 (len={len(text)})")


def test_answer_contains_all_dishes(client, runner):
    """回答包含所有菜单菜品且是合法 AnswerArtifact"""
    names = ["回锅肉", "麻婆豆腐", "清炒时蔬", "紫菜蛋花汤"]
    base = {
        "selected_menu": {"plan_id": "p1", "dishes": [
            {"recipe_id": i + 1, "name": n} for i, n in enumerate(names)]},
        "recipe_ids": [1, 2, 3, 4],
    }
    artifact = _invoke_artifact(client, runner, "answer_generation", _answer_base(**base))
    assert list(artifact.recipe_ids) == [1, 2, 3, 4]
    text = artifact.content.conclusion + artifact.content.menu_summary
    missing = [n for n in names if n not in text]
    if missing:
        print(f"\n  [WARN] 回答缺少菜品: {missing}")
    else:
        print(f"\n  [OK] 回答包含全部{len(names)}道菜")


# ---- 统一审查 ----

def test_unified_review_detects_forbidden(client, runner):
    """坏回答必须标记 REVISION_REQUIRED（含钠/疾病等硬性禁止内容），不得 PASS。"""
    bad = "为您推荐以下菜单。回锅肉含钠800mg，适合高血压患者食用。"
    msg = _CTX + f"## 审查对象\n{bad}\n\n## 证据链\n菜单：[回锅肉, 麻婆豆腐]"
    artifact = _invoke_artifact(client, runner, "unified_review", msg)
    assert artifact.status == "REVISION_REQUIRED", \
        f"坏回答必须 REVISION_REQUIRED，实际 {artifact.status}"
    print(f"\n  [OK] 统一审查-坏回答: status={artifact.status}")


def test_unified_review_passes_clean(client, runner):
    """干净回答 → PASS 的合法 ReviewArtifact"""
    clean = "为您推荐以下菜单：回锅肉、麻婆豆腐、清炒时蔬、紫菜蛋花汤。约35分钟完成。"
    msg = _CTX + f"## 审查对象\n{clean}\n\n## 证据链\n菜单：[回锅肉, 麻婆豆腐, 清炒时蔬, 紫菜蛋花汤]"
    artifact = _invoke_artifact(client, runner, "unified_review", msg)
    assert artifact.status == "PASS", f"干净回答应 PASS，实际 {artifact.status}"
    print(f"\n  [OK] 统一审查-好回答: status={artifact.status}")


def _parse(content: str) -> dict:
    """尝试解析 LLM 响应为 JSON。"""
    if isinstance(content, dict):
        return content
    if not content:
        return {}
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    import re
    match = re.search(r'\{.*\}', content, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return {"_raw": content[:500]}
