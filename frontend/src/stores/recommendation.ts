import { defineStore } from "pinia";

import {
  createRequest,
  createSession,
  getSession,
  getStatus,
  subscribeEvents,
  type SSEConnection,
} from "@/api/client";
import {
  TERMINAL_SET,
  type AnonymousParticipant,
  type ChatMessage,
  type PhaseEvent,
  type PublicMenuSummary,
  type RequestState,
} from "@/types";

function messageId(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

function errMsg(error: unknown): string {
  return error instanceof Error ? error.message : "发生未知错误。";
}

// ---- 纯函数（可测试）：终态展示与事件去重 Reducer ----

export function isTerminal(status: string): boolean {
  return TERMINAL_SET.has(status);
}

export function terminalLabel(status: string): string {
  switch (status) {
    case "completed":
      return "已完成";
    case "no_safe_menu":
      return "无安全菜单";
    case "no_feasible_menu":
      return "无可行菜单";
    case "strict_time_indeterminate":
      return "严格时间约束无法判定";
    case "failed":
      return "处理失败";
    case "cancelled":
      return "已取消";
    case "interrupted":
      return "已中断";
    case "needs_clarification":
      return "待澄清";
    case "reconnect":
      return "连接中断，正在重连";
    default:
      return status || "";
  }
}

/** 按 event_id 去重合并阶段事件（稳定 outbox event_id 幂等）。 */
export function reducePhases(
  existing: PhaseEvent[],
  incoming: PhaseEvent[],
): PhaseEvent[] {
  const seen = new Set(existing.map((e) => e.id));
  const out = [...existing];
  for (const e of incoming) {
    if (!seen.has(e.id)) {
      seen.add(e.id);
      out.push(e);
    }
  }
  return out;
}

export function refFingerprint(refs: string[]): string {
  return [...refs].sort().join("|");
}

export function slotFromRef(ref: string): AnonymousParticipant {
  const n = /^p(\d+)$/.exec(ref)?.[1] ?? "";
  return { participant_ref: ref, label: `参与者 ${n || ref}` };
}

export function publicMenu(value: unknown): PublicMenuSummary | null {
  if (!value || typeof value !== "object") return null;
  const menu = value as Partial<PublicMenuSummary>;
  if (!menu.build_id || !menu.plan_id || !menu.menu_hash
      || !Array.isArray(menu.recipe_ids) || !Array.isArray(menu.items)
      || menu.recipe_ids.length === 0 || menu.items.length !== menu.recipe_ids.length) {
    return null;
  }
  const ids = menu.recipe_ids;
  if (ids.some((id) => !Number.isInteger(id) || id <= 0)
      || new Set(ids).size !== ids.length) return null;
  for (let index = 0; index < menu.items.length; index += 1) {
    const item = menu.items[index];
    if (!item || item.recipe_id !== ids[index] || !item.name?.trim()) return null;
  }
  return {
    build_id: menu.build_id,
    plan_id: menu.plan_id,
    menu_hash: menu.menu_hash,
    recipe_ids: [...ids],
    items: menu.items.map((item) => ({ recipe_id: item.recipe_id, name: item.name.trim() })),
  };
}

// ---- session 与参与者组合持久化 ----

const SESSION_KEY = "v2.session_id";
const SESSION_REFS_KEY = "v2.session_refs";

function loadSession(): { sessionId: string; refs: string[] } {
  try {
    const sid = localStorage.getItem(SESSION_KEY) || "";
    const refsRaw = localStorage.getItem(SESSION_REFS_KEY);
    const refs = refsRaw ? JSON.parse(refsRaw) : [];
    return { sessionId: sid, refs: Array.isArray(refs) ? refs : [] };
  } catch {
    return { sessionId: "", refs: [] };
  }
}

function saveSession(sessionId: string, refs: string[]): void {
  try {
    localStorage.setItem(SESSION_KEY, sessionId);
    localStorage.setItem(SESSION_REFS_KEY, JSON.stringify(refs));
  } catch {
    /* 隐私模式等忽略 */
  }
}

const POLL_INTERVAL_MS = 5000;

export const useRecommendationStore = defineStore("recommendation", {
  state: () => {
    const { sessionId, refs } = loadSession();
    return {
      slots: refs.map((r) => slotFromRef(r)) as AnonymousParticipant[],
      selectedRefs: [...refs] as string[],
      sessionId: sessionId as string,
      sessionRefs: [...refs] as string[],
      _sessionPromise: null as Promise<string> | null,
      messages: [] as ChatMessage[],
      requestId: "" as string,
      status: "" as string,
      phases: [] as PhaseEvent[],
      answer: "" as string,
      currentMenu: null as PublicMenuSummary | null,
      clarification: "" as string,
      isStreaming: false,
      error: "" as string,
      connection: null as SSEConnection | null,
      pollTimer: null as number | null,
      sseFailures: 0,
    };
  },
  getters: {
    selectedSlots(state): AnonymousParticipant[] {
      return state.slots.filter((s) => state.selectedRefs.includes(s.participant_ref));
    },
    canSend(): boolean {
      return this.selectedRefs.length >= 1 && !this.isStreaming;
    },
  },
  actions: {
    async ensureSession(refs: string[]): Promise<string> {
      const fp = refFingerprint(refs);
      // 同参与者组合 → 持续复用；组合变化 → 明确新建会话
      if (this.sessionId && refFingerprint(this.sessionRefs) === fp) {
        return this.sessionId;
      }
      if (this._sessionPromise) {
        return this._sessionPromise; // 并发合并：最多创建一个 session
      }
      this._sessionPromise = (async () => {
        const { session_id } = await createSession(refs);
        this.sessionId = session_id;
        this.sessionRefs = [...refs];
        saveSession(session_id, refs);
        return session_id;
      })().finally(() => {
        this._sessionPromise = null;
      });
      return this._sessionPromise;
    },
    slotLabel(ref: string): string {
      return this.slots.find((s) => s.participant_ref === ref)?.label
        || slotFromRef(ref).label;
    },
    addSlot() {
      for (let n = 1; n <= 50; n++) {
        const ref = `p${n}`;
        if (!this.slots.some((s) => s.participant_ref === ref)) {
          this.slots.push(slotFromRef(ref));
          if (!this.selectedRefs.includes(ref)) this.selectedRefs.push(ref);
          return;
        }
      }
      this.error = "匿名成员已达上限（p1..p50）";
    },
    removeSlot(ref: string) {
      // 同时删除 slots 与 selectedRefs
      this.slots = this.slots.filter((s) => s.participant_ref !== ref);
      this.selectedRefs = this.selectedRefs.filter((r) => r !== ref);
    },
    participants(): AnonymousParticipant[] {
      return this.selectedRefs.map((r) => ({
        participant_ref: r,
        label: this.slotLabel(r),
      }));
    },
    reset() {
      this.closeConnection();
      this.messages = [];
      this.requestId = "";
      this.status = "";
      this.phases = [];
      this.answer = "";
      this.currentMenu = null;
      this.clarification = "";
      this.error = "";
    },
    closeConnection() {
      if (this.connection) {
        this.connection.close();
        this.connection = null;
      }
      if (this.pollTimer !== null) {
        window.clearTimeout(this.pollTimer);
        this.pollTimer = null;
      }
    },
    async send(message: string) {
      const content = message.trim();
      if (!content || !this.canSend) return;
      this.error = "";
      this.isStreaming = true;
      this.phases = [];
      this.answer = "";
      this.clarification = "";
      this.status = "";
      this.messages.push({
        id: messageId(), role: "user", content,
        createdAt: Date.now(), status: "complete",
      });
      const assistantId = messageId();
      this.messages.push({
        id: assistantId, role: "assistant", content: "",
        createdAt: Date.now(), status: "sending",
      });

      try {
        const refs = this.selectedRefs.slice();
        const sessionId = await this.ensureSession(refs);
        const participants = refs.map((r) => ({
          participant_ref: r,
          label: this.slotLabel(r),
        }));
        const resp = await createRequest({
          idempotency_key: `web-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
          participants,
          message: content,
          session_id: sessionId,
        });
        this.requestId = resp.request_id;
        this.status = resp.status;
        // 打开 SSE；SSE 存活期间不主动轮询
        this.openSse(resp.request_id, assistantId);
      } catch (e) {
        this.error = errMsg(e);
        const pending = this.messages.find((m) => m.id === assistantId);
        if (pending) {
          pending.content = this.error;
          pending.status = "error";
        }
        this.isStreaming = false;
      }
    },
    openSse(requestId: string, assistantId: string) {
      this.closeConnection();
      this.sseFailures = 0;
      this.connection = subscribeEvents(
        requestId,
        (e: PhaseEvent) => this.onSseEvent(e, assistantId),
        (failures: number) => this.onSseFailure(requestId, assistantId, failures),
      );
    },
    onSseFailure(requestId: string, assistantId: string, failures: number) {
      this.sseFailures = failures;
      this.status = "reconnect";
      if (failures >= 3) {
        // 连续第 3 次失败 → 关闭 SSE 并降级轮询（5 秒间隔）
        this.closeConnection();
        this.startPolling(requestId, assistantId, POLL_INTERVAL_MS);
      }
    },
    onSseEvent(e: PhaseEvent, assistantId: string) {
      const before = this.phases.length;
      this.phases = reducePhases(this.phases, [e]);
      if (this.phases.length === before) return; // 重复 event_id 不重复改变 UI
      // 收到新事件 → 连接已恢复（不能停留在"reconnect"）
      if (this.status === "reconnect") this.status = "running";
      switch (e.event) {
        case "answer_ready":
          this.answer = String((e.data as { text?: string }).text || "");
          // 正文写入 assistant message.content；保持 sending/待最终确认，不 completed
          {
            const pending = this.messages.find((m) => m.id === assistantId);
            if (pending) pending.content = this.answer;
          }
          break;
        case "result_committed":
          this.currentMenu = publicMenu(
            (e.data as { menu_summary?: unknown }).menu_summary,
          );
          this.status = "completed";
          this.finishStreaming(assistantId, "complete");
          break;
        case "error":
          this.error = String((e.data as { message?: string }).message || "流程失败");
          this.status = "failed";
          this.finishStreaming(assistantId, "error");
          break;
        case "request_cancelled":
          this.status = "cancelled";
          this.finishStreaming(assistantId, "error");
          break;
        case "request_terminal": {
          // 业务终态：no_safe_menu/no_feasible_menu/strict_time_indeterminate/
          // failed/interrupted 分别保持语义，立即关闭连接
          const status = String((e.data as { status?: string }).status || "failed");
          this.status = status;
          const msg = String((e.data as { message?: string }).message || "");
          if (msg) this.error = msg;
          this.finishStreaming(assistantId, "error");
          break;
        }
        case "clarification_needed":
          this.status = "needs_clarification";
          this.clarification = String(
            (e.data as { clarification?: string }).clarification || "请补充必要信息");
          this.finishStreaming(assistantId, "complete");
          break;
        default:
          break;
      }
    },
    finishStreaming(assistantId: string, msgStatus: "complete" | "error") {
      this.isStreaming = false;
      this.closeConnection();
      const pending = this.messages.find((m) => m.id === assistantId);
      if (pending) {
        pending.status = msgStatus;
        if (!pending.content) pending.content = terminalLabel(this.status);
      }
    },
    async startPolling(requestId: string, assistantId: string, interval: number) {
      if (this.pollTimer !== null) {
        window.clearTimeout(this.pollTimer);
        this.pollTimer = null;
      }
      const tick = async () => {
        try {
          const st = await getStatus(requestId);
          this.status = st.status;
          if (isTerminal(st.status)) {
            if (st.error) {
              this.error = `${st.error.code || "error"}: ${st.error.message || ""}`;
            }
            this.applyResultSummary(st, assistantId);
            this.finishStreaming(assistantId, st.status === "completed" ? "complete" : "error");
            return;
          }
        } catch {
          /* 轮询失败继续重试 */
        }
        this.pollTimer = window.setTimeout(tick, interval);
      };
      this.pollTimer = window.setTimeout(tick, interval);
    },
    applyResultSummary(state: RequestState, assistantId: string) {
      const summary = state.result_summary;
      if (!summary) return;
      const menu = publicMenu(summary.menu_summary);
      if (menu) this.currentMenu = menu;
      const text = summary.answer?.text?.trim() || "";
      if (text) {
        this.answer = text;
        const pending = this.messages.find((message) => message.id === assistantId);
        if (pending) pending.content = text;
      }
    },
    async restoreSession() {
      if (!this.sessionId) return;
      try {
        const state = await getSession(this.sessionId);
        const menu = publicMenu(state.current_menu);
        if (menu) {
          this.currentMenu = menu;
          this.status = "completed";
        }
      } catch {
        // 持久化 session 不存在或暂不可用时保留匿名参与者选择，允许用户继续新建会话。
      }
    },
  },
});
