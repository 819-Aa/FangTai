"""B5 时间与步骤规划 —— 运行时服务。

消费 B1 离线生成的 time_profiles.jsonl，
提供单菜时间画像查询、菜单调度计算、公开时间摘要。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from food_agent_v2.core.paths import CLEANED_DIR


@dataclass
class RecipeTimeProfile:
    """单菜时间画像。"""
    recipe_id: int
    name: str
    total_steps: int
    total_active_seconds: int
    total_equipment_seconds: int
    total_passive_seconds: int
    total_elapsed_range: tuple[int, int]   # (min, max) seconds
    overall_confidence: str                # high | medium | low
    step_tasks: list[dict] = field(default_factory=list)
    llm_estimate: dict | None = None       # 离线 LLM 补全的时长估算 {active/equipment/passive/total/confidence}


@dataclass
class MenuScheduleResult:
    """菜单调度结果。"""
    recipe_ids: list[int]
    makespan_seconds: int | None
    makespan_range: tuple[int, int] | None
    strict_time_feasible: str             # true | false | unknown
    total_active_minutes: int
    total_passive_minutes: int
    schedule: dict[int, list[dict]] = field(default_factory=dict)
    time_source: str = "task_graph"       # llm_estimate | task_graph | none


class TimeProfileService:
    """时间画像查询与菜单调度服务。"""

    def __init__(self):
        self._profiles: dict[int, RecipeTimeProfile] = {}
        self._loaded = False

    def load(self, path: Optional[Path] = None) -> None:
        if path is None:
            path = CLEANED_DIR / "time_profiles.jsonl"
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                rec = json.loads(line)
                rid = rec["recipe_id"]
                tasks = rec.get("step_tasks", [])

                active_s = rec.get("total_active_seconds", 0) or 0
                equip_s = rec.get("total_equipment_seconds", 0) or 0
                passive_s = rec.get("total_passive_seconds", 0) or 0

                total_s = active_s + equip_s + passive_s
                # 默认按关键路径估算：active+equip 不可并行，passive 可并行
                if total_s > 0:
                    elapsed_min = active_s + equip_s
                    elapsed_max = total_s
                else:
                    elapsed_min = 0
                    elapsed_max = 0

                # 置信度：基于有时长的步骤占比
                known = sum(1 for t in tasks if t.get("duration_seconds"))
                conf = "high" if known == len(tasks) else (
                    "medium" if known / max(len(tasks), 1) >= 0.5 else "low"
                )

                self._profiles[rid] = RecipeTimeProfile(
                    recipe_id=rid,
                    name=rec.get("name", ""),
                    total_steps=len(tasks),
                    total_active_seconds=active_s,
                    total_equipment_seconds=equip_s,
                    total_passive_seconds=passive_s,
                    total_elapsed_range=(elapsed_min, elapsed_max),
                    overall_confidence=conf,
                    step_tasks=tasks,
                    llm_estimate=rec.get("llm_estimate"),
                )
        self._loaded = True

    def get_recipe_time_profile(self, recipe_id: int) -> RecipeTimeProfile | None:
        return self._profiles.get(recipe_id)

    def compute_menu_schedule(
        self, recipe_ids: list[int], time_limit_minutes: int | None = None
    ) -> MenuScheduleResult:
        """基于步骤任务图计算菜单调度（文档 05 §14：任务图，禁用简化公式）。

        规则：
        - 同一互斥键（cook / 设备类型）的任务不能重叠；
        - 步骤内依赖（depends_on）先于资源约束；
        - 被动等待不占用厨师/设备，可与其他工作并行；
        - 未知时长不估算（记 0，不进入关键路径）。
        """
        from collections import defaultdict

        profiles = [self._profiles[rid] for rid in recipe_ids if rid in self._profiles]

        if not profiles:
            return MenuScheduleResult(
                recipe_ids=recipe_ids,
                makespan_seconds=None,
                makespan_range=None,
                strict_time_feasible="unknown",
                total_active_minutes=0,
                total_passive_minutes=0,
            )

        # 优先使用离线 LLM 估算（文档 05 §14 + 2026-08-08 决策）：
        # 并行模型——厨师备菜（主动操作）串行；设备占用（炒/煮/蒸等）可与其他菜备菜并行
        # （典型多灶头家庭厨房）。makespan ≈ 主动总和 + 最长单菜设备时间 + 其余设备的低效串行段。
        # 避免把全部菜品时间简单相加（那会高估到不现实）。
        if all(p.llm_estimate for p in profiles):
            active_total = 0
            equipment_total = 0
            passive_total = 0
            max_equipment = 0
            all_high = True
            for p in profiles:
                est = p.llm_estimate
                active_total += int(est["active_minutes"])
                equipment_total += int(est["equipment_minutes"])
                passive_total += int(est["passive_minutes"])
                max_equipment = max(max_equipment, int(est["equipment_minutes"]))
                if est.get("confidence") != "high":
                    all_high = False
            # 设备在备菜期间并行运转；超过最长单菜设备的部分按 40% 效率串行
            makespan_min = active_total + max_equipment + (equipment_total - max_equipment) * 0.4
            makespan = int(round(makespan_min * 60))
            if time_limit_minutes:
                strict_feasible = str(makespan <= time_limit_minutes * 60).lower() if all_high else "unknown"
            else:
                strict_feasible = "unknown"
            return MenuScheduleResult(
                recipe_ids=recipe_ids,
                makespan_seconds=makespan,
                makespan_range=(int(makespan * 0.8), int(makespan * 1.2)),
                strict_time_feasible=strict_feasible,
                total_active_minutes=active_total,
                total_passive_minutes=passive_total,
                time_source="llm_estimate",
            )

        resource_free: dict[str, int] = defaultdict(int)   # mutex_key → 下次可开始时间
        task_end: dict[tuple[int, int], int] = {}          # (rid, step_index) → 结束时间
        makespan = 0
        total_active = 0
        total_passive = 0
        all_high = True
        has_duration = False

        for profile in profiles:
            rid = profile.recipe_id
            for task in profile.step_tasks:
                idx = task.get("step_index", 0)
                deps = task.get("depends_on", [])
                dep_end = max((task_end.get((rid, d), 0) for d in deps), default=0)
                mutex = task.get("mutex_key") or "cook"
                res_start = resource_free.get(mutex, 0)
                start = max(dep_end, res_start)

                dur = task.get("duration_seconds")
                if dur is None or dur <= 0:
                    dur = 0  # 未知时长不估算（文档 05：缺失时长保持 null）
                else:
                    has_duration = True
                end = start + int(dur)
                task_end[(rid, idx)] = end
                if mutex != "passive":
                    resource_free[mutex] = end  # 被动等待不占用资源，可并行
                makespan = max(makespan, end)

                if task.get("step_type") == "active":
                    total_active += int(dur)
                elif task.get("step_type") == "passive":
                    total_passive += int(dur)

                # 置信度：存在未知时长或非高置信度 → 不能给出严格时间判断
                if task.get("duration_seconds") is None or \
                        task.get("duration_confidence") != "high":
                    all_high = False

        # 严格时间可行性：仅当全部时长高置信度且有时长数据时才判定 true/false
        if time_limit_minutes and has_duration and all_high:
            feasible = makespan <= time_limit_minutes * 60
            strict_feasible = str(feasible).lower()
        else:
            strict_feasible = "unknown"

        return MenuScheduleResult(
            recipe_ids=recipe_ids,
            makespan_seconds=makespan if makespan > 0 else None,
            makespan_range=(int(makespan * 0.8), int(makespan * 1.2)) if makespan > 0 else None,
            strict_time_feasible=strict_feasible,
            total_active_minutes=total_active // 60,
            total_passive_minutes=total_passive // 60,
            time_source="task_graph" if makespan > 0 else "none",
        )

    def get_public_time_summary(self, recipe_id: int) -> str | None:
        """公开时间摘要（用于回答模型，不含内部时长估算细节）。"""
        profile = self._profiles.get(recipe_id)
        if not profile:
            return None
        if profile.total_elapsed_range[1] == 0:
            return None
        low, high = profile.total_elapsed_range
        if profile.overall_confidence == "high":
            mins = profile.total_active_seconds // 60 + profile.total_equipment_seconds // 60
            return f"约{mins}分钟"
        else:
            return f"约{low // 60}-{high // 60}分钟"

    @property
    def profile_count(self) -> int:
        return len(self._profiles)


# 模块级单例
_service: TimeProfileService | None = None


def get_time_service() -> TimeProfileService:
    global _service
    if _service is None:
        _service = TimeProfileService()
        _service.load()
    return _service
