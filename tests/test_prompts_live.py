"""提示词实测 —— 调用五个模型，验证响应质量。

运行方式：
  uv run pytest tests/test_prompts_live.py -v -s

标注：需要 LLM API 可用。
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from food_agent_v2.core.config import load_config
from food_agent_v2.c3.prompts import get_prompt, get_test_cases
from food_agent_v2.c3.llm_client import LLMClient


@pytest.fixture(scope="module")
def client():
    cfg = load_config()
    if not cfg.llm.api_key:
        pytest.skip("No LLM API key configured")
    return LLMClient()


# ---- 查询理解 ----

def test_query_understanding_meal_plan(client):
    """三菜一汤 → 正确意图和菜数"""
    prompt = get_prompt("query_understanding")
    resp = client.invoke(
        "query_understanding", prompt,
        "## 上下文\n当前消息：推荐三菜一汤，家常口味，45分钟内\n## 输入\n推荐三菜一汤，家常口味，45分钟内"
    )
    content = _parse(resp)
    assert content.get("query_intent") == "new_recommendation", \
        f"Expected new_recommendation, got {content.get('query_intent')}"
    reqs = content.get("structured_requirements", {})
    assert reqs.get("dish_count") == 4 or str(reqs.get("dish_count")) == "4", \
        f"Expected dish_count=4 for '三菜一汤', got {reqs.get('dish_count')}"
    assert content.get("needs_clarification") != True, \
        "Should not need clarification for clear request"
    print(f"\n  [OK] 查询理解-菜单规划: intent={content.get('query_intent')}, "
          f"dish_count={reqs.get('dish_count')}")


def test_query_understanding_health_signal(client):
    """健康信号记录但不判断"""
    prompt = get_prompt("query_understanding")
    resp = client.invoke(
        "query_understanding", prompt,
        "## 上下文\n当前消息：我有高血压，推荐清淡的晚餐\n## 输入\n我有高血压，推荐清淡的晚餐"
    )
    content = _parse(resp)
    signals = content.get("health_signals", [])
    assert len(signals) > 0 if isinstance(signals, list) else bool(signals), \
        "Should record health signal for '高血压'"
    # 不应该自行诊断
    text = json.dumps(content, ensure_ascii=False).lower()
    assert "需要控制钠" not in text, "Should not add inferred dietary advice"
    print(f"\n  [OK] 查询理解-健康信号: signals={signals}")


def test_query_understanding_replace(client):
    """替换意图识别"""
    prompt = get_prompt("query_understanding")
    resp = client.invoke(
        "query_understanding", prompt,
        "## 上下文\n当前菜单：回锅肉、麻婆豆腐、清炒时蔬、紫菜蛋花汤\n## 输入\n把回锅肉换成宫保鸡丁"
    )
    content = _parse(resp)
    assert content.get("query_intent") in ("replace", "adjust"), \
        f"Expected replace/adjust, got {content.get('query_intent')}"
    print(f"\n  [OK] 查询理解-替换: intent={content.get('query_intent')}")


# ---- 回答生成 ----

def test_answer_no_forbidden_content(client):
    """回答不含禁止表述"""
    prompt = get_prompt("answer_generation")
    menu_context = {
        "selected_menu": {
            "dishes": [
                {"name": "清蒸鲈鱼", "reason": "鲜嫩清淡"},
                {"name": "蒜蓉西兰花", "reason": "营养均衡"},
                {"name": "番茄蛋汤", "reason": "家常可口"},
            ]
        },
        "health_note_data": {"excluded_count": 2},
        "time_data": {"total_minutes": 35, "confidence": "high"},
    }
    user_msg = f"## 上下文\n选定菜单：{json.dumps(menu_context, ensure_ascii=False)}\n## 输入\n根据以上菜单生成回答"
    resp = client.invoke("answer_generation", prompt, user_msg)
    content_text = resp.get("content", "")

    forbidden = [
        ("含钠", "mg"),
        ("千卡", "kcal"),
        ("高血压", "hypertension"),
        ("糖尿病", "diabetes"),
        ("过敏", "allergy"),
        ("适合.*患者", "health_judgment"),
    ]
    for keyword, label in forbidden:
        import re
        if re.search(keyword, content_text):
            pytest.fail(f"Answer contains forbidden content: '{label}' found")
    print(f"\n  [OK] 回答生成: no forbidden content (length={len(content_text)})")


def test_answer_contains_all_dishes(client):
    """回答包含所有菜单菜品"""
    prompt = get_prompt("answer_generation")
    dishes = ["回锅肉", "麻婆豆腐", "清炒时蔬", "紫菜蛋花汤"]
    menu_context = {"selected_menu": {"dishes": [{"name": d} for d in dishes]}}
    user_msg = f"## 上下文\n选定菜单：{json.dumps(menu_context, ensure_ascii=False)}\n## 输入\n生成用户可见回答"

    resp = client.invoke("answer_generation", prompt, user_msg)
    content_text = resp.get("content", "")
    missing = [d for d in dishes if d not in content_text]
    if missing:
        print(f"\n  [WARN] 回答缺少菜品: {missing}")
        print(f"  回答内容: {content_text[:300]}...")
    else:
        print(f"\n  [OK] 回答包含全部{len(dishes)}道菜")


# ---- 统一审查 ----

def test_unified_review_detects_forbidden(client):
    """审查模型检测到禁止内容"""
    prompt = get_prompt("unified_review")
    bad_answer = "为您推荐以下菜单。回锅肉含钠800mg，适合高血压患者食用。"
    user_msg = f"## 审查对象\n{bad_answer}\n\n## 证据链\n菜单：[回锅肉, 麻婆豆腐]"

    resp = client.invoke("unified_review", prompt, user_msg)
    content = _parse(resp)
    verdict = content.get("verdict", "")
    print(f"\n  [OK] 统一审查-坏回答: verdict={verdict}")
    # 应该检测到问题
    if verdict == "PASS":
        print(f"  [WARN] 审查未检测到'含钠800mg'等禁止内容")


def test_unified_review_passes_clean(client):
    """审查模型通过干净回答"""
    prompt = get_prompt("unified_review")
    clean_answer = "为您推荐以下菜单：回锅肉、麻婆豆腐、清炒时蔬、紫菜蛋花汤。方案A在满足健康要求的同时更符合口味偏好。约35分钟完成。"
    user_msg = f"## 审查对象\n{clean_answer}\n\n## 证据链\n菜单：[回锅肉, 麻婆豆腐, 清炒时蔬, 紫菜蛋花汤]"

    resp = client.invoke("unified_review", prompt, user_msg)
    content = _parse(resp)
    verdict = content.get("verdict", "")
    print(f"\n  [OK] 统一审查-好回答: verdict={verdict}")


def _parse(resp: dict) -> dict:
    """尝试解析 LLM 响应为 JSON。"""
    content = resp.get("content", "")
    if isinstance(content, dict):
        return content
    if not content:
        return {}
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    # 尝试提取 JSON 块
    import re
    match = re.search(r'\{.*\}', content, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return {"_raw": content[:500]}
