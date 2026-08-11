import { defineStore } from "pinia";

import {
  createRequest,
  createSession,
  getStatus,
  subscribeEvents,
  type SSEConnection,
} from "@/api/client";
import {
  TERMINAL_SET,
  type AnonymousParticipant,
  type ChatMessage,
  type PhaseEvent,
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
    case "reconnect":
      return "连接中断，已恢复";
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

// ---- session_id 持续复用（刷新/多轮不创建无关新会话） ----

const SESSION_KEY = "v2.session_id";

function loadSessionId(): string {
  try {
    return localStorage.getItem(SESSION_KEY) || "";
  } catch {
    return "";
  }
}

function saveSessionId(sid: string): void {
  try {
    localStorage.setItem(SESSION_KEY, sid);
  } catch {
    /* 隐私模式等忽略 */
  }
}

export const useRecommendationStore = defineStore("recommendation", {
  state: () => ({
    slots: [] as AnonymousParticipant[],
    selectedRefs: [] as string[],
    sessionId: loadSessionId() as string,
    messages: [] as ChatMessage[],
    requestId: "" as string,
    status: "" as string,
    phases: [] as PhaseEvent[],
    answer: "" as string,
    isStreaming: false,
    error: "" as string,
    connection: null as SSEConnection | null,
    pollTimer: null as number | null,
  }),
  getters: {
    selectedSlots(state): AnonymousParticipant[] {
      return state.slots.filter((s) => state.selectedRefs.includes(s.participant_ref));
    },
    canSend(): boolean {
      return this.selectedRefs.length >= 1 && !this.isStreaming;
    },
  },
  actions: {
    async ensureSession() {
      if (this.sessionId) return this.sessionId;
      const { session_id } = await createSession();
      this.sessionId = session_id;
      saveSessionId(session_id);
      return session_id;
    },
    addSlot() {
      const idx = this.slots.length + 1;
      const ref = `p${idx}`;
      this.slots.push({ participant_ref: ref, label: `参与者 ${idx}` });
      if (!this.selectedRefs.includes(ref)) this.selectedRefs.push(ref);
    },
    removeSlot(ref: string) {
      this.selectedRefs = this.selectedRefs.filter((x) => x !== ref);
    },
    participants(): AnonymousParticipant[] {
      return this.selectedSlots.map((s) => ({
        participant_ref: s.participant_ref,
        label: s.label,
      }));
    },
    reset() {
      this.closeConnection();
      this.messages = [];
      this.requestId = "";
      this.status = "";
      this.phases = [];
      this.answer = "";
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
        const sessionId = await this.ensureSession();
        const participants = this.participants();
        const resp = await createRequest({
          idempotency_key: `web-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
          participants,
          message: content,
          session_id: sessionId,
        });
        this.requestId = resp.request_id;
        this.status = resp.status;

        // SSE 分阶段：重连携带 Last-Event-ID；连续失败降级轮询
        this.closeConnection();
        this.connection = subscribeEvents(
          resp.request_id,
          (e: PhaseEvent) => this.onSseEvent(e, assistantId),
          () => this.startPolling(resp.request_id, assistantId),
        );

        // 轮询终态兜底（SSE 活跃时也探测终态）
        this.startPolling(resp.request_id, assistantId);
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
    onSseEvent(e: PhaseEvent, assistantId: string) {
      this.phases = reducePhases(this.phases, [e]);
      if (e.event === "answer_ready") {
        const text = String((e.data as { text?: string }).text || "");
        this.answer = text; // 待最终确认（仅 result_committed 才 completed）
        const pending = this.messages.find((m) => m.id === assistantId);
        if (pending) {
          pending.content = text;
          pending.status = "complete";
        }
      }
      if (e.event === "result_committed") {
        const pending = this.messages.find((m) => m.id === assistantId);
        if (pending && !pending.content) {
          pending.content = "菜单已生成。";
        }
      }
      if (e.event === "error") {
        this.error = String((e.data as { message?: string }).message || "流程失败");
      }
    },
    async startPolling(requestId: string, assistantId: string) {
      if (this.pollTimer !== null) {
        window.clearTimeout(this.pollTimer);
        this.pollTimer = null;
      }
      const tick = async () => {
        try {
          const st = await getStatus(requestId);
          this.status = st.status;
          if (isTerminal(st.status)) {
            this.closeConnection();
            this.isStreaming = false;
            if (st.error) {
              this.error = `${st.error.code || "error"}: ${st.error.message || ""}`;
            }
            const pending = this.messages.find((m) => m.id === assistantId);
            if (pending && pending.status === "sending") {
              pending.content = pending.content || st.error?.message || "流程结束";
              pending.status = st.status === "completed" ? "complete" : "error";
            }
            return;
          }
        } catch {
          // 轮询失败：继续重试
        }
        this.pollTimer = window.setTimeout(tick, 2000);
      };
      this.pollTimer = window.setTimeout(tick, 500);
    },
  },
});
