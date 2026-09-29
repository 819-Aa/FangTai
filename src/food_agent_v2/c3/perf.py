"""C3 性能观测基线（P1）—— 节点级阶段计时 + 模型调用计数。

纯观测：只记录单调时钟时间戳与调用元数据，不改变任何业务行为。
每次请求一个 PerfTrace 实例（`_trigger_workflow` 每请求新建 LangGraphRecommendationOrchestrator，
故实例属性非并发共享）。最终以一行结构化 JSON 日志输出，供性能 harness
采集诊断瓶颈。

设计文档 §9.1 时间点在此的映射（P1 先覆盖服务端处理段）：
- processing_started_at   → runner.run 入口
- node_starts / node_ends → 各节点进入/离开（intent_ready / health_ready /
                            menu_validated / answer_built 对应节点结束）
- model_calls             → LLMClient 每次实际调用（role/model/elapsed_ms）
- terminal_at             → _finalize 完成
首 Token（visible_ttft / authoritative_ttft）在 P2 的 SSE 即时通知落地后，
由 harness 从客户端侧测量；P1 只测服务端处理耗时与模型调用分解。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field


@dataclass
class PerfTrace:
    request_id: str
    processing_started_at: float = field(default_factory=time.perf_counter)
    node_starts: dict[str, float] = field(default_factory=dict)
    node_ends: dict[str, float] = field(default_factory=dict)
    model_calls: list[dict] = field(default_factory=list)
    terminal_at: float | None = None

    def mark_node_start(self, node: str) -> None:
        # 记录首次进入时间；重入（replan/revision）不覆盖，end 记录最后离开。
        self.node_starts.setdefault(node, time.perf_counter())

    def mark_node_end(self, node: str) -> None:
        self.node_ends[node] = time.perf_counter()

    def add_model_call(self, role: str, model: str, elapsed_ms: float) -> None:
        self.model_calls.append({
            "role": role,
            "model": model,
            "elapsed_ms": round(float(elapsed_ms), 2),
        })

    def _node_ms(self, node: str) -> float | None:
        start = self.node_starts.get(node)
        end = self.node_ends.get(node)
        if start is None or end is None:
            return None
        return round((end - start) * 1000, 2)

    def to_dict(self) -> dict:
        total_ms = (
            round((self.terminal_at - self.processing_started_at) * 1000, 2)
            if self.terminal_at is not None else None
        )
        model_total_ms = round(sum(c["elapsed_ms"] for c in self.model_calls), 2)
        return {
            "request_id": self.request_id,
            "total_ms": total_ms,
            "nodes_ms": {n: self._node_ms(n) for n in self.node_starts},
            "model_call_count": len(self.model_calls),
            "model_total_ms": model_total_ms,
            "model_calls": self.model_calls,
        }

    def log_line(self) -> str:
        return f"[V2][perf] {json.dumps(self.to_dict(), ensure_ascii=False)}"


class PerformanceBudget:
    """请求级单调时钟预算（L2）：限制外部调用与可选增强，不耗尽主链预算。

    用单调时钟（perf_counter）记录剩余时间；require 用于必需阶段，allow_optional
    用于可选增强（如 NarrativePolisher 需 ≥2.5s 剩余才启用）。
    """

    def __init__(self, total_seconds: float) -> None:
        self._start = time.perf_counter()
        self._total = float(total_seconds)

    def remaining_seconds(self) -> float:
        return max(0.0, self._total - (time.perf_counter() - self._start))

    def require(self, stage: str) -> bool:
        """必需阶段是否还有剩余预算；超限返回 False。"""
        return self.remaining_seconds() > 0

    def allow_optional(self, seconds: float) -> bool:
        """可选增强是否有足够剩余预算（如润色需 ≥2.5s）。"""
        return self.remaining_seconds() >= float(seconds)
