"""B1 离线 LLM 时间补全 —— 用模型估算菜品制作时间。

设计（2026-08-08 决策）：
- 确定性任务图调度（B5）作为可靠基线（兜底）；
- 本模块用 LLM 在离线阶段估算每道菜的主动/设备/被动分钟，校验后写回
  time_profiles.jsonl 的 llm_estimate 字段，供 B5 在运行时有更高置信度的时长。
- 运行时 B5 仍做确定性调度；LLM 只负责补数据，不参与运行时判断。
- 时间不是健康事实，离线模型辅助数据处理不触碰健康/权限红线。

用法：
  uv run food-agent-v2 time-profiler --sample 50      # 小样本验证
  uv run food-agent-v2 time-profiler                   # 全量（2000 道）
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from food_agent_v2.core.paths import CLEANED_RECIPES, CLEANED_DIR

SYSTEM_PROMPT = (
    "你是中餐菜品制作时间估算专家。根据菜品名称、食材清单和烹饪步骤，"
    "估算一位普通家庭厨师制作这道菜需要的时间。\n"
    "输出严格 JSON，不要输出其他文字：\n"
    '{"active_minutes": <主动操作分钟：切洗拌腌等手工活>, '
    '"equipment_minutes": <设备占用分钟：炒煮蒸烤炖等使用锅/烤箱/蒸箱的时间>, '
    '"passive_minutes": <被动等待分钟：腌制/发酵/冷藏/浸泡等无需操作的时间>, '
    '"total_minutes": <从开始到上桌的总耗时分钟>, '
    '"confidence": "high|medium|low"}\n'
    "要求：所有数值为非负整数；total_minutes 应大致等于主动+设备（被动等待可与其他工作并行，所以 total 可以小于三者和）；"
    "没有把握时 confidence 给 low。\n\n"
    "## 校准示例（参照这个量级，不要系统性高估）\n"
    "西芹炒百合：{\"active_minutes\":5,\"equipment_minutes\":8,\"passive_minutes\":0,\"total_minutes\":13,\"confidence\":\"high\"}\n"
    "蒜蓉塔菜：{\"active_minutes\":5,\"equipment_minutes\":6,\"passive_minutes\":0,\"total_minutes\":11,\"confidence\":\"high\"}\n"
    "清蒸鲈鱼：{\"active_minutes\":8,\"equipment_minutes\":12,\"passive_minutes\":0,\"total_minutes\":20,\"confidence\":\"high\"}\n"
    "番茄蛋汤：{\"active_minutes\":8,\"equipment_minutes\":10,\"passive_minutes\":0,\"total_minutes\":18,\"confidence\":\"high\"}\n"
    "香菇鸡汤：{\"active_minutes\":10,\"equipment_minutes\":40,\"passive_minutes\":0,\"total_minutes\":50,\"confidence\":\"medium\"}\n"
    "卤牛肉：{\"active_minutes\":10,\"equipment_minutes\":60,\"passive_minutes\":0,\"total_minutes\":70,\"confidence\":\"medium\"}\n"
    "注意：简单快炒/清蒸类 10-20 分钟，炖煮类 40-70 分钟；主动备菜按实际切洗拌的分钟数，"
    "简单菜备菜 3-8 分钟即可。"
)


def _extract_json(content: str) -> dict:
    """从 LLM 输出中提取 JSON 对象。"""
    content = (content or "").strip()
    try:
        data = json.loads(content)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    return {}


def _validate_estimate(data: dict) -> dict | None:
    """校验 LLM 估算结果。非法返回 None。"""
    if not isinstance(data, dict):
        return None
    try:
        active = int(float(data.get("active_minutes", -1)))
        equip = int(float(data.get("equipment_minutes", -1)))
        passive = int(float(data.get("passive_minutes", -1)))
        total = int(float(data.get("total_minutes", -1)))
    except (ValueError, TypeError):
        return None
    conf = data.get("confidence", "")
    if conf not in ("high", "medium", "low"):
        return None
    if min(active, equip, passive, total) < 0:
        return None
    # 合理性范围：单道菜总耗时 2 分钟 ~ 6 小时
    if not (2 <= total <= 360):
        return None
    if total < 1:
        return None
    return {
        "active_minutes": active,
        "equipment_minutes": equip,
        "passive_minutes": passive,
        "total_minutes": total,
        "confidence": conf,
    }


def _build_user_prompt(recipe: dict) -> str:
    name = recipe.get("名称", "")
    ingredients = recipe.get("食材清单", "")[:300]
    steps = recipe.get("烹饪步骤", "")[:800]
    return (
        f"菜品：{name}\n"
        f"食材：{ingredients}\n"
        f"步骤：{steps}\n\n"
        "请估算制作时间，只输出 JSON。"
    )


def profile_recipe(recipe: dict, llm_client) -> dict | None:
    """估算单道菜的时间。返回 {'recipe_id', 'llm_estimate'} 或 None。"""
    user = _build_user_prompt(recipe)
    try:
        resp = llm_client.invoke("time_profiling", SYSTEM_PROMPT, user)
    except Exception:
        return None
    data = _extract_json(resp.get("content", ""))
    est = _validate_estimate(data)
    if est is None:
        return None
    return {"recipe_id": recipe["recipe_id"], "llm_estimate": est}


# 进度/部分结果文件（支持实时进度汇报与断点续跑）
PARTIAL_FILE = CLEANED_DIR / "llm_estimates_partial.jsonl"
PROGRESS_FILE = CLEANED_DIR / "llm_time_profiler_progress.json"


def _load_done() -> dict[int, dict]:
    """加载已完成的部分估算（断点续跑）。"""
    done: dict[int, dict] = {}
    if PARTIAL_FILE.exists():
        with PARTIAL_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    rec = json.loads(line)
                    done[rec["recipe_id"]] = rec["llm_estimate"]
    return done


def _save_done(done: dict[int, dict]) -> None:
    """把部分估算写盘（增量）。"""
    PARTIAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    with PARTIAL_FILE.open("w", encoding="utf-8") as f:
        for rid in sorted(done):
            f.write(json.dumps({"recipe_id": rid, "llm_estimate": done[rid]},
                               ensure_ascii=False) + "\n")


def _write_progress(done: int, total: int, ok: int, failed: int) -> None:
    """写进度文件（供外部实时读取）。"""
    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROGRESS_FILE.write_text(json.dumps({
        "done": done, "total": total, "success": ok, "failed": failed,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def merge_estimates() -> int:
    """把 partial 估算文件合并回 time_profiles.jsonl。

    data-rebuild 会重新生成 time_profiles（不含 llm_estimate），
    重建后调用本函数恢复 LLM 时间估算。返回合并条数。
    """
    done = _load_done()
    if not done:
        return 0
    profile_path = CLEANED_DIR / "time_profiles.jsonl"
    if not profile_path.exists():
        return 0
    updated = 0
    lines_out: list[str] = []
    with profile_path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            rid = rec.get("recipe_id")
            if rid in done:
                rec["llm_estimate"] = done[rid]
                updated += 1
            lines_out.append(json.dumps(rec, ensure_ascii=False))
    with profile_path.open("w", encoding="utf-8") as f:
        f.write("\n".join(lines_out) + "\n")
    return updated


def run(sample: int | None = None, start: int = 1, end: int | None = None,
        resume: bool = True) -> dict:
    """批量补全时间画像（增量写盘，支持断点续跑）。"""
    from food_agent_v2.c3.llm_client import get_llm_client

    recipes: list[dict] = []
    with CLEANED_RECIPES.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                recipes.append(json.loads(line))

    # 范围过滤
    if end is None:
        end = len(recipes)
    recipes = [r for r in recipes if start <= r["recipe_id"] <= end]
    if sample:
        recipes = recipes[:sample]

    estimates = _load_done() if resume else {}
    remaining = [r for r in recipes if r["recipe_id"] not in estimates]

    client = get_llm_client()
    ok = len(estimates)
    failed = 0
    done = len(estimates)

    for i, recipe in enumerate(remaining):
        result = profile_recipe(recipe, client)
        if result:
            estimates[result["recipe_id"]] = result["llm_estimate"]
            ok += 1
        else:
            failed += 1
        done = len(estimates)
        if (i + 1) % 25 == 0 or i + 1 == len(remaining):
            _save_done(estimates)
            _write_progress(done, len(recipes), ok, failed)
            print(f"  进度 {i + 1}/{len(remaining)}：累计成功 {ok}，失败 {failed}",
                  flush=True)

    _save_done(estimates)
    _write_progress(done, len(recipes), ok, failed)

    # 合并写回 time_profiles.jsonl
    profile_path = CLEANED_DIR / "time_profiles.jsonl"
    updated = 0
    lines_out: list[str] = []
    with profile_path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            rid = rec.get("recipe_id")
            if rid in estimates:
                rec["llm_estimate"] = estimates[rid]
                updated += 1
            lines_out.append(json.dumps(rec, ensure_ascii=False))
    with profile_path.open("w", encoding="utf-8") as f:
        f.write("\n".join(lines_out) + "\n")

    print(f"\n完成：共 {len(recipes)} 道，成功 {ok}，失败 {failed}，写回 {updated} 条", flush=True)
    return {"total": len(recipes), "success": ok, "failed": failed, "updated": updated}


if __name__ == "__main__":
    sample_arg = None
    if "--sample" in sys.argv:
        sample_arg = int(sys.argv[sys.argv.index("--sample") + 1])
    run(sample=sample_arg)
