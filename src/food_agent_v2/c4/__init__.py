"""C4 上下文与记忆 —— 会话管理、SharedWorkflowContext、角色投影。

管理推荐系统需要记住的一切：对话历史、约束状态、菜单版本、
以及按角色裁剪的 ModelContext 投影。
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

# ---- 会话事件 ----

class EventType(str, Enum):
    USER_MESSAGE = "user_message"
    SYSTEM_RESPONSE = "system_response"
    MENU_COMMITTED = "menu_committed"
    CONSTRAINT_UPDATED = "constraint_updated"
    CLARIFICATION_REQUESTED = "clarification_requested"
    CLARIFICATION_RESOLVED = "clarification_resolved"
    TERMINAL = "terminal"


@dataclass
class ConversationEvent:
    event_id: str
    session_id: str
    request_id: str | None
    event_type: EventType
    event_summary: str
    event_detail_ref: str | None = None
    participant_refs: list[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)
    token_count_estimate: int = 0
    compressed_original: str | None = None


# ---- 约束管理 ----

class ConstraintScope(str, Enum):
    PERMANENT = "permanent"
    SESSION = "session"
    TURN = "turn"


@dataclass
class EffectiveConstraint:
    constraint_code: str | None
    taboo_ingredient_name: str | None
    participant_ref: str
    source_refs: list[str]
    scope: ConstraintScope
    effect: str = "hard_exclude"
    constraint_id: str = ""


# ---- SharedWorkflowContext ----

@dataclass
class ContextManifest:
    manifest_hash: str
    immutable_block_hashes: dict[str, str] = field(default_factory=dict)
    compression_count: int = 0
    total_token_estimate: int = 0


@dataclass
class CurrentMenu:
    plan_id: str | None = None
    recipe_ids: list[int] = field(default_factory=list)
    menu_artifact_ref: str | None = None


@dataclass
class SharedWorkflowContext:
    request_id: str
    session_id: str
    participant_refs: list[str]
    participant_user_id_mapping: dict[str, int]   # participant_ref → user_id
    current_message: dict[str, Any]               # {raw_text, timestamp}
    effective_constraints: list[EffectiveConstraint] = field(default_factory=list)
    current_menu: CurrentMenu = field(default_factory=CurrentMenu)
    menu_history: list[dict] = field(default_factory=list)  # 最近 5 个菜单版本
    pending_clarifications: list[dict] = field(default_factory=list)
    conversation_events: list[ConversationEvent] = field(default_factory=list)
    context_manifest: ContextManifest | None = None
    session_metadata: dict[str, Any] = field(default_factory=dict)

    def compute_manifest_hash(self) -> str:
        """计算不可压缩块的哈希。"""
        immutable_data = {
            "current_message": str(self.current_message.get("raw_text", "")),
            "constraints": [c.constraint_code or c.taboo_ingredient_name
                          for c in self.effective_constraints],
            "current_menu": self.current_menu.plan_id,
            "clarifications": str(self.pending_clarifications),
        }
        return hashlib.sha256(
            json.dumps(immutable_data, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()


# ---- ModelContext (角色投影) ----

@dataclass
class ModelContext:
    role: str
    projected_from: str                      # manifest_hash
    system_visible: dict[str, Any] = field(default_factory=dict)
    conversation_visible: dict[str, Any] = field(default_factory=dict)
    constraint_visible: list[dict] = field(default_factory=list)
    menu_visible: dict[str, Any] | None = None
    artifact_refs: list[str] = field(default_factory=list)
    token_budget: dict[str, int] = field(default_factory=dict)
    projection_timestamp: float = field(default_factory=time.time)


# 角色投影规则（C4 §10）
ROLE_PROJECTION_RULES = {
    "query_understanding": {
        "visible_constraints": lambda c: {
            "participant_ref": c.participant_ref,
            "effect": c.effect,
            "scope": c.scope.value if isinstance(c.scope, ConstraintScope) else c.scope,
        },
        "visible_menu": ["recipe_ids", "plan_id"],
        "conceal": ["participant_real_name", "user_id", "specific_disease",
                    "specific_indicator", "raw_health_metrics", "nutrition_values"],
        "anti_injection": True,
    },
    "health_menu_planning": {
        "visible_constraints": lambda c: {
            "constraint_code": c.constraint_code,
            "effect": c.effect,
            "scope": c.scope.value if isinstance(c.scope, ConstraintScope) else c.scope,
        },
        "visible_menu": ["recipe_ids", "plan_id", "menu_history_summary"],
        "conceal": ["health_profile_raw", "raw_health_metrics", "specific_disease",
                    "participant_real_name", "user_id", "b4_internal_relations",
                    "nutrition_values"],
        "anti_injection": True,
    },
    "menu_decision": {
        "visible_constraints": "reference_only",
        "visible_menu": ["all_feasible_plans", "menu_history", "current_menu"],
        "conceal": ["b5_internal_task_graph", "health_profile_raw",
                    "raw_health_metrics", "participant_real_name", "user_id",
                    "nutrition_values"],
        "anti_injection": True,
    },
    "answer_generation": {
        "visible_constraints": "public_health_note_only",
        "visible_menu": ["selected_menu_full_public_facts"],
        "conceal": ["health_profile_raw", "raw_health_metrics", "specific_disease",
                    "specific_indicator", "b4_internal_hit_path",
                    "b5_internal_task_graph", "nutrition_values",
                    "participant_real_name", "user_id"],
        "anti_injection": True,
    },
    "unified_review": {
        "visible_constraints": "reference_only",
        "visible_menu": ["current_menu_reference"],
        "conceal": ["nutrition_values", "participant_real_name", "user_id"],
        "anti_injection": True,
    },
}


class ContextService:
    """上下文与记忆服务。

    管理会话生命周期、SharedWorkflowContext 构建、
    角色投影（ModelContext）、菜单版本滚动。
    """

    def __init__(self, memory_source: Any = None):
        self._sessions: dict[str, SharedWorkflowContext] = {}
        self._events: dict[str, list[ConversationEvent]] = {}
        self._menu_histories: dict[str, list[dict]] = {}
        self._request_results: dict[str, dict] = {}
        self._redis = None  # 惰性初始化
        self._memory_source = memory_source  # 已提交边界来源（默认 MySQL）

    def _get_redis(self):
        if self._redis is None:
            from food_agent_v2.c4.redis_store import RedisSessionStore
            self._redis = RedisSessionStore()
        return self._redis

    def _get_memory_source(self):
        if self._memory_source is None:
            from food_agent_v2.c4.mysql_repository import (
                default_mysql_session_memory_source,
            )
            self._memory_source = default_mysql_session_memory_source()
        return self._memory_source

    def _persist_session(self, ctx: SharedWorkflowContext) -> None:
        """将会话状态写入 Redis。"""
        store = self._get_redis()
        # 会话状态（_restore_session 依赖它判断会话是否存在）
        store.save_session_state(ctx.session_id, {
            "participant_refs": ctx.participant_refs,
            "request_count": len(ctx.conversation_events),
            "saved_at": time.time(),
        })
        # 序列化约束
        constraints_data = [{
            "constraint_code": c.constraint_code,
            "taboo_ingredient_name": c.taboo_ingredient_name,
            "participant_ref": c.participant_ref,
            "source_refs": c.source_refs,
            "scope": c.scope.value if isinstance(c.scope, ConstraintScope) else c.scope,
            "constraint_id": c.constraint_id,
        } for c in ctx.effective_constraints]
        store.save_constraints(ctx.session_id, constraints_data)

        # 序列化事件
        events_data = [{
            "event_id": e.event_id, "event_type": e.event_type.value,
            "event_summary": e.event_summary, "participant_refs": e.participant_refs,
            "timestamp": e.timestamp, "token_count_estimate": e.token_count_estimate,
        } for e in ctx.conversation_events[-50:]]
        store.save_events(ctx.session_id, events_data)

        # 菜单历史
        store.save_menu_history(ctx.session_id, ctx.menu_history)

    def _restore_session(self, session_id: str) -> dict | None:
        """从 MySQL 已提交边界 + Redis 可恢复状态组合恢复（MySQL 权威）。

        - MySQL：sessions 元数据、已提交 conversation_events、menu_versions；
        - Redis：近期可恢复事件、会话级临时约束、运行期菜单。
        事件按 event_id 去重（MySQL 已提交优先）；菜单版本以 MySQL 为准并补 Redis 更新。
        """
        committed_events: list[dict] = []
        menu_versions: list[dict] = []
        session_meta: dict = {}
        try:
            src = self._get_memory_source()
            meta = src.load_session(session_id)
            if meta:
                session_meta = meta
                committed_events = src.load_committed_events(session_id)
                menu_versions = src.load_menu_versions(session_id)
        except Exception:
            pass  # MySQL 不可用 → 仅用 Redis 可恢复状态

        store = self._get_redis()
        redis_state = store.load_session_state(session_id)
        redis_events = store.load_events(session_id)
        redis_constraints = store.load_constraints(session_id)
        redis_menu = store.load_menu_history(session_id)

        if not redis_state and not session_meta and not committed_events and not redis_events:
            return None

        by_id: dict[str, dict] = {}
        for e in committed_events:
            by_id[e.get("event_id", "")] = e
        for e in redis_events:
            by_id.setdefault(e.get("event_id", ""), e)
        merged_events = list(by_id.values())

        menu_history = list(menu_versions)
        committed_plan_ids = {m.get("plan_id") for m in menu_history}
        for m in redis_menu:
            if m.get("plan_id") not in committed_plan_ids:
                menu_history.append(m)
        menu_history = menu_history[-5:]

        return {
            "participant_refs": session_meta.get("participant_refs")
                or (redis_state or {}).get("participant_refs", []),
            "request_count": session_meta.get("request_count", 0),
            "constraints": redis_constraints,
            "events": merged_events,
            "menu_history": menu_history,
        }

    # ---- 会话管理 ----

    def create_session(self, participant_refs: list[str],
                       user_id_mapping: dict[str, int]) -> str:
        session_id = str(uuid.uuid4())[:12]
        return session_id

    def build_shared_context(
        self, session_id: str, participant_refs: list[str],
        current_message: dict, user_id_mapping: dict[str, int],
        request_id: str = "",
        permanent_constraints: list[EffectiveConstraint] | None = None,
    ) -> tuple[SharedWorkflowContext, ContextManifest]:
        """构建 SharedWorkflowContext（C4 §8.1）。

        T18 约束先行：构建 ContextManifest 前必须形成完整有效约束——
        B2 固定档案永久约束（permanent_constraints）为权威基础，会话级临时约束追加。
        """
        if not request_id:
            request_id = str(uuid.uuid4())[:8]

        # 恢复已有会话或创建新会话
        if session_id in self._sessions:
            existing = self._sessions[session_id]
            constraints = [c for c in existing.effective_constraints
                         if c.scope != ConstraintScope.TURN]  # 清理 turn 约束
            menu_hist = existing.menu_history[:5]
            events = existing.conversation_events[:]
            pending = existing.pending_clarifications[:]
            menu = existing.current_menu
        else:
            # 内存没有 → 尝试从 Redis 恢复（跨进程/重启会话仍在，C4 §11）
            restored = self._restore_session(session_id)
            if restored:
                constraints = []
                for c in restored.get("constraints", []):
                    if isinstance(c, dict):
                        try:
                            constraints.append(EffectiveConstraint(
                                constraint_code=c.get("constraint_code"),
                                taboo_ingredient_name=c.get("taboo_ingredient_name"),
                                participant_ref=c.get("participant_ref", ""),
                                source_refs=c.get("source_refs", []),
                                scope=ConstraintScope(c.get("scope", "session")),
                                constraint_id=c.get("constraint_id", ""),
                            ))
                        except ValueError:
                            continue
                menu_hist = list(restored.get("menu_history", []))[:5]
                events = []
                for e in restored.get("events", []):
                    if isinstance(e, dict):
                        try:
                            events.append(ConversationEvent(
                                event_id=e.get("event_id", ""),
                                session_id=e.get("session_id", session_id),
                                request_id=e.get("request_id"),
                                event_type=EventType(e.get("event_type", "user_message")),
                                event_summary=e.get("event_summary", ""),
                                participant_refs=e.get("participant_refs", []),
                                timestamp=e.get("timestamp", 0),
                                token_count_estimate=e.get("token_count_estimate", 0),
                            ))
                        except ValueError:
                            continue
                pending = []
                menu = CurrentMenu()
                if menu_hist:
                    last = menu_hist[-1]
                    menu = CurrentMenu(
                        plan_id=last.get("plan_id"),
                        recipe_ids=last.get("recipe_ids", []),
                    )
            else:
                constraints = []
                menu_hist = []
                events = []
                pending = []
                menu = CurrentMenu()

        # 约束先行（T18）：B2 固定档案永久约束为权威基础，会话级约束追加；
        # 必须在 ContextManifest 计算前形成完整 effective_constraints。
        if permanent_constraints:
            permanent = [c for c in permanent_constraints
                         if c.scope != ConstraintScope.TURN]
            permanent_keys = {(c.constraint_code or c.taboo_ingredient_name)
                              for c in permanent}
            constraints = permanent + [
                c for c in constraints
                if (c.constraint_code or c.taboo_ingredient_name) not in permanent_keys
            ]

        ctx = SharedWorkflowContext(
            request_id=request_id,
            session_id=session_id,
            participant_refs=participant_refs,
            participant_user_id_mapping=user_id_mapping,
            current_message=current_message,
            effective_constraints=constraints,
            current_menu=menu,
            menu_history=menu_hist,
            pending_clarifications=pending,
            conversation_events=events,
            session_metadata={
                "created_at": time.time(),
                "request_count": len([e for e in events
                                     if e.event_type == EventType.SYSTEM_RESPONSE]) + 1,
            },
        )

        # 追加用户消息事件
        event = ConversationEvent(
            event_id=str(uuid.uuid4())[:8],
            session_id=session_id,
            request_id=request_id,
            event_type=EventType.USER_MESSAGE,
            event_summary=current_message.get("raw_text", "")[:200],
            participant_refs=participant_refs,
            token_count_estimate=len(current_message.get("raw_text", "")) // 2,
        )
        ctx.conversation_events.append(event)

        # 计算清单
        core_block = {
            "constraints": [c.constraint_code or c.taboo_ingredient_name
                            for c in constraints],
            "menu": menu.plan_id,
            "clarifications": str(pending),
        }
        manifest = ContextManifest(
            manifest_hash=ctx.compute_manifest_hash(),
            immutable_block_hashes={
                "message": hashlib.sha256(
                    str(current_message).encode()).hexdigest()[:16],
                "constraints": hashlib.sha256(
                    str([c.constraint_code for c in constraints]).encode()
                ).hexdigest()[:16],
                "menu": hashlib.sha256(
                    str(menu.plan_id or "").encode()).hexdigest()[:16],
                "core": hashlib.sha256(
                    json.dumps(core_block, sort_keys=True, ensure_ascii=False).encode()
                ).hexdigest()[:16],
            },
            total_token_estimate=sum(
                e.token_count_estimate for e in ctx.conversation_events[-20:]
            ),
        )
        ctx.context_manifest = manifest

        # INV-009：超预算时压缩历史（总结保留精华，不可压缩块不变）
        self._compress_context(ctx)

        # 缓存
        self._sessions[session_id] = ctx

        # 持久化到 Redis
        self._persist_session(ctx)

        return ctx, manifest

    def _compress_context(self, ctx: SharedWorkflowContext,
                          budget_tokens: int = 6000) -> bool:
        """INV-009：超预算时压缩历史事件（总结保留精华，不可压缩块不变）。

        - 保留不可压缩块：当前消息 / 有效健康约束 / 当前菜单 / 待澄清（哈希不变，完整性校验仍过）
        - 去重 + 旧事件总结为精华摘要（compressed_original 保留原文供审计）
        - 保留最近 20 条事件原样
        """
        manifest = ctx.context_manifest
        if manifest and manifest.total_token_estimate <= budget_tokens:
            return False
        events = ctx.conversation_events
        if len(events) <= 20:
            return False
        # T18：压缩前记录不可压缩核心块哈希（INV-009 §9.3 逐块核对）
        core_before = self._core_block_hash(ctx)
        original_events = events[:]
        recent = events[-20:]
        old = events[:-20]
        # 去重：同 type + 同 summary 的旧事件只留一次
        seen: set[tuple] = set()
        old_dedup: list[ConversationEvent] = []
        for e in old:
            key = (e.event_type.value, e.event_summary)
            if key not in seen:
                seen.add(key)
                old_dedup.append(e)
        # 总结旧事件为精华摘要（保留要点，不丢）
        summary_lines = [f"[{e.event_type.value}] {e.event_summary[:80]}"
                         for e in old_dedup]
        compressed_summary = "\n".join(summary_lines)
        compressed_event = ConversationEvent(
            event_id="compressed_history",
            session_id=ctx.session_id,
            request_id=None,
            event_type=EventType.SYSTEM_RESPONSE,
            event_summary="[已压缩] 此前对话要点：\n" + compressed_summary[:2000],
            compressed_original=compressed_summary,  # 原文保留供审计
            token_count_estimate=len(compressed_summary) // 2,
        )
        ctx.conversation_events = [compressed_event] + recent
        # 压缩后核对核心块哈希不变（INV-009 §9.3）；变化则回滚压缩（fail-safe）
        if self._core_block_hash(ctx) != core_before:
            ctx.conversation_events = original_events
            return False
        # 更新 manifest（不可压缩块哈希不变 → 完整性校验仍通过）
        if manifest:
            manifest.compression_count += 1
            manifest.total_token_estimate = sum(
                e.token_count_estimate for e in ctx.conversation_events[-30:])
        return True

    @staticmethod
    def _core_block_hash(ctx: SharedWorkflowContext) -> str:
        """不可压缩核心块（约束/菜单/待澄清）的规范哈希（INV-009）。"""
        core = {
            "constraints": [c.constraint_code or c.taboo_ingredient_name
                            for c in ctx.effective_constraints],
            "menu": ctx.current_menu.plan_id,
            "clarifications": str(ctx.pending_clarifications),
        }
        return hashlib.sha256(
            json.dumps(core, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()[:16]

    def _recompute_manifest(self, ctx: SharedWorkflowContext) -> None:
        """约束/菜单变更后重算 ContextManifest（约束先行：清单始终反映完整约束）。"""
        if not ctx.context_manifest:
            return
        manifest = ctx.context_manifest
        manifest.immutable_block_hashes["constraints"] = hashlib.sha256(
            str([c.constraint_code for c in ctx.effective_constraints]).encode()
        ).hexdigest()[:16]
        manifest.immutable_block_hashes["menu"] = hashlib.sha256(
            str(ctx.current_menu.plan_id or "").encode()).hexdigest()[:16]
        manifest.immutable_block_hashes["core"] = self._core_block_hash(ctx)
        manifest.manifest_hash = ctx.compute_manifest_hash()

    # ---- 角色投影 ----

    def project_model_context(
        self, role: str, handoff_message: dict | None,
        shared_context_ref: str,
    ) -> ModelContext:
        """按角色裁剪 ModelContext（C4 §8.2 + §10）。"""
        ctx = self._sessions.get(shared_context_ref)
        if not ctx:
            raise ValueError(f"Unknown shared_context_ref: {shared_context_ref}")

        rules = ROLE_PROJECTION_RULES.get(role, ROLE_PROJECTION_RULES["query_understanding"])
        conceal = rules.get("conceal", [])

        # 约束投影
        constraint_visible = []
        if rules["visible_constraints"] != "reference_only":
            for c in ctx.effective_constraints:
                if rules["visible_constraints"] == "public_health_note_only":
                    break
                if callable(rules["visible_constraints"]):
                    constraint_visible.append(rules["visible_constraints"](c))

        # 反注入：system_visible 不含指令式内容
        system_visible = {
            "task_description": f"Role: {role}",
            "allowed_tools_summary": "see role policy",
            "output_requirements": "see artifact schema",
        }

        # 反注入：current_message 仅作为数据
        conversation_visible = {
            "current_message": ctx.current_message,
            "pending_clarifications": ctx.pending_clarifications
                if role == "query_understanding" else [],
        }

        return ModelContext(
            role=role,
            projected_from=ctx.context_manifest.manifest_hash
                if ctx.context_manifest else "unknown",
            system_visible=system_visible,
            conversation_visible=conversation_visible,
            constraint_visible=constraint_visible,
            menu_visible={
                "current_menu": ctx.current_menu,
                "menu_history": ctx.menu_history[:5],
            } if ctx.current_menu.plan_id else None,
            token_budget={
                "total_allocated": 8000,
                "used_by_context": ctx.context_manifest.total_token_estimate
                    if ctx.context_manifest else 0,
                "remaining_for_generation": 4000,
            },
        )

    # ---- 会话状态提交 ----

    def commit_session_state(
        self, request_id: str, final_status: str,
        menu_artifact_ref: str | None = None,
        health_summary_ref: str | None = None,
    ) -> None:
        """请求终态时提交会话状态（C4 §11.1）。"""
        # 找到该 request 对应的 session
        for sid, ctx in self._sessions.items():
            if ctx.request_id == request_id:
                # 记录终态事件
                event = ConversationEvent(
                    event_id=str(uuid.uuid4())[:8],
                    session_id=sid,
                    request_id=request_id,
                    event_type=EventType.TERMINAL,
                    event_summary=f"terminal:{final_status}",
                    event_detail_ref=menu_artifact_ref,
                    participant_refs=ctx.participant_refs,
                )
                ctx.conversation_events.append(event)
                self._events.setdefault(sid, []).append(event)

                # 失败/取消/中断不留下成功记忆（T18）：仅 completed 提交菜单版本
                if final_status == "completed" and menu_artifact_ref and ctx.current_menu.plan_id:
                    self._menu_histories.setdefault(sid, []).append({
                        "plan_id": ctx.current_menu.plan_id,
                        "recipe_ids": ctx.current_menu.recipe_ids,
                        "committed_at": time.time(),
                    })
                    if len(self._menu_histories[sid]) > 5:
                        self._menu_histories[sid] = self._menu_histories[sid][-5:]
                    ctx.menu_history = self._menu_histories[sid]

                # 持久化到 Redis
                self._persist_session(ctx)
                break

    def record_session_event(
        self, request_id: str, terminal_status: str,
        stage_events: list[dict],
    ) -> None:
        """记录 SSE 事件（C4 §11.1）——持久化到 Redis，支持 Last-Event-ID 续传。"""
        store = self._get_redis()
        if store is not None:
            try:
                store.save_request(request_id, {
                    "terminal_status": terminal_status,
                    "stage_events": stage_events,
                })
            except Exception:
                pass  # Redis 不可用时降级为内存，不影响健康/提交

    def validate_context_integrity(
        self, shared_context_ref: str,
    ) -> dict:
        """校验上下文完整性（C4 §11.1 / INV-009）。

        只校验不可压缩核心块（有效健康约束 / 当前菜单 / 待澄清事项），
        不含随轮次变化的 current_message——这样多轮会话不会误报。
        """
        ctx = self._sessions.get(shared_context_ref)
        if not ctx or not ctx.context_manifest:
            return {"valid": False, "reason": "context not found"}

        core = {
            "constraints": [c.constraint_code or c.taboo_ingredient_name
                            for c in ctx.effective_constraints],
            "menu": ctx.current_menu.plan_id,
            "clarifications": str(ctx.pending_clarifications),
        }
        current_core_hash = hashlib.sha256(
            json.dumps(core, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
        stored_core_hash = ctx.context_manifest.immutable_block_hashes.get("core", "")

        return {
            "valid": (not stored_core_hash) or current_core_hash == stored_core_hash,
            "current_core_hash": current_core_hash,
            "stored_core_hash": stored_core_hash,
        }

    # ---- 约束管理 ----

    def store_derived_constraints(
        self, session_id: str,
        constraint_sets: dict[str, Any],
    ) -> None:
        """B2 → C4：把派生的健康约束写入会话上下文。

        constraint_sets: {participant_ref: ParticipantHealthConstraintSet}。
        写入后角色投影（project_model_context）和完整性校验才有真实约束。
        """
        if session_id not in self._sessions:
            return
        ctx = self._sessions[session_id]
        eff: list[EffectiveConstraint] = []
        for ref, cs in (constraint_sets or {}).items():
            hard = getattr(cs, "hard_constraints", []) if cs else []
            for c in hard:
                eff.append(EffectiveConstraint(
                    constraint_code=getattr(c, "constraint_code", None),
                    taboo_ingredient_name=getattr(c, "taboo_ingredient_name", None),
                    participant_ref=ref,
                    source_refs=list(getattr(c, "source_refs", []) or []),
                    scope=ConstraintScope.PERMANENT,
                ))
        ctx.effective_constraints = eff
        self._recompute_manifest(ctx)  # 约束先行：清单重算核心块
        self._persist_session(ctx)

    def store_temporary_constraint(
        self, session_id: str, constraint: dict,
    ) -> str:
        """B2→C4：存储 B2 验证后的临时约束。"""
        cid = str(uuid.uuid4())[:8]
        if session_id in self._sessions:
            ctx = self._sessions[session_id]
            ctx.effective_constraints.append(EffectiveConstraint(
                constraint_code=constraint.get("constraint_code"),
                taboo_ingredient_name=constraint.get("taboo_ingredient_name"),
                participant_ref=constraint.get("participant_ref", ""),
                source_refs=constraint.get("source_refs", []),
                scope=ConstraintScope(constraint.get("scope", "session")),
                constraint_id=cid,
            ))
            self._recompute_manifest(ctx)
        return cid

    def revoke_temporary_constraint(
        self, session_id: str, constraint_id: str,
    ) -> None:
        """B2→C4：撤销临时约束。"""
        if session_id in self._sessions:
            ctx = self._sessions[session_id]
            ctx.effective_constraints = [
                c for c in ctx.effective_constraints
                if c.constraint_id != constraint_id or c.scope == ConstraintScope.PERMANENT
            ]
            self._recompute_manifest(ctx)

    def get_effective_constraints(
        self, session_id: str,
    ) -> list[EffectiveConstraint]:
        """B2→C4：获取有效约束集。"""
        if session_id in self._sessions:
            return self._sessions[session_id].effective_constraints
        return []

    def check_permanent_constraint_override(
        self, constraint_code: str, action: str,
    ) -> bool:
        """检查是否尝试覆盖永久约束（INV-016）。"""
        return True  # 拒绝所有永久约束的覆盖请求
