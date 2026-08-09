"""端到端测试 —— 对话用例场景覆盖。"""

import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

API = "http://localhost:8001"


def post_request(payload: dict) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{API}/v1/recommendation-requests",
        data=data, headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def get_status(request_id: str) -> dict:
    with urllib.request.urlopen(f"{API}/v1/recommendation-requests/{request_id}") as resp:
        return json.loads(resp.read())


def wait_for_terminal(request_id: str, max_wait: int = 90) -> dict:
    terminal = {"completed", "failed", "no_safe_menu", "no_feasible_menu",
                "needs_clarification", "cancelled"}
    for _ in range(max_wait // 5):
        time.sleep(5)
        s = get_status(request_id)
        if s["status"] in terminal:
            return s
    return get_status(request_id)


def _run_case(label: str, message: str, participants: list[dict]) -> bool:
    print(f"\n{'='*50}")
    print(f"  {label}")
    print(f"  输入: {message[:60]}")
    print(f"  参与者: {len(participants)}人")

    payload = {
        "idempotency_key": f"e2e-case-{int(time.time())}-{hash(message) % 10000}",
        "participants": participants,
        "message": message,
        "config": {},
    }

    try:
        resp = post_request(payload)
        rid = resp["request_id"]
        print(f"  请求: {rid}")

        result = wait_for_terminal(rid)
        status = result["status"]
        error = result.get("error")

        if status == "completed":
            print(f"  [PASS] {status}")
            return True
        elif status in ("needs_clarification",):
            print(f"  [WARN] {status} — 模型需要更多信息")
            return True  # 不算失败
        else:
            print(f"  [FAIL] {status}: {error}")
            return False
    except Exception as e:
        print(f"  [ERROR] {e}")
        return False


# 对话用例数据
with open("data/raw/对话用例.json", "r", encoding="utf-8") as f:
    cases = json.load(f)

# 测试参与者映射 —— 选匹配用例场景的用户档案
# 档案速查：
#   User1: 28岁 海鲜过敏 (一般默认)
#   User4: 26岁 健脾胃目标 (消化问题)
#   User9: 60岁 高血压+高血脂 (老人)
#   User34: 26岁 健脾胃 (消化问题)
#   User2: 32岁 孕妇 (特殊人群)
SINGLE = [{"participant_ref": "p1", "user_id": "1"}]             # 海鲜过敏
SINGLE_DIGEST = [{"participant_ref": "p1", "user_id": "4"}]      # 健脾胃
COUPLE_DIGEST = [{"participant_ref": "p1", "user_id": "4"},      # 健脾胃
                 {"participant_ref": "p2", "user_id": "34"}]     # 健脾胃
FAMILY_ELDERLY = [{"participant_ref": "p1", "user_id": "9"},     # 60岁 高血压
                  {"participant_ref": "p2", "user_id": "2"},     # 32岁 孕妇
                  {"participant_ref": "p3", "user_id": "1"}]     # 28岁 海鲜过敏

# 选择代表性用例
test_configs = [
    # 单人简单场景 (海鲜过敏用户)
    ("Case1 单人-开放式", cases[0]["user_messages"][0], SINGLE),
    ("Case6 单人-时间约束", cases[5]["user_messages"][0], SINGLE),
    ("Case7 单人-严格食材", cases[6]["user_messages"][0], SINGLE),
    # 多人场景 (匹配档案)
    ("Case4 两人-胃口不好", cases[3]["user_messages"][0], COUPLE_DIGEST),  # 健脾胃×2
    ("Case12 两人-四菜一汤营养", cases[11]["user_messages"][0], COUPLE_DIGEST),
    ("Case14 三人-老小", cases[13]["user_messages"][0], FAMILY_ELDERLY),  # 老人+孕妇+过敏
    # 偏好场景
    ("Case3 单人-清爽夏天", cases[2]["user_messages"][0], SINGLE),
]

if __name__ == "__main__":
    print("=" * 60)
    print("V2 端到端对话用例测试")
    print("=" * 60)

    passed = 0
    failed = 0
    for label, msg, parts in test_configs:
        ok = _run_case(label, msg, parts)
        if ok:
            passed += 1
        else:
            failed += 1

    print(f"\n{'='*60}")
    print(f"结果: {passed}/{passed+failed} 通过")
    if failed:
        print(f"失败: {failed} 用例")
