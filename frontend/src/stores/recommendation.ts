import { defineStore } from "pinia";

import {
  createRequest,
  getProfiles,
  getStatus,
  subscribeEvents,
} from "@/api/client";
import type { ChatMessage, PhaseEvent, UserProfile } from "@/types";

function messageId(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

function errMsg(error: unknown): string {
  return error instanceof Error ? error.message : "发生未知错误。";
}

const TERMINAL = new Set([
  "completed", "failed", "no_safe_menu", "no_feasible_menu",
  "needs_clarification", "cancelled",
]);

export const useRecommendationStore = defineStore("recommendation", {
  state: () => ({
    profiles: [] as UserProfile[],
    selectedIds: [] as number[],
    loadingProfiles: false,
    messages: [] as ChatMessage[],
    requestId: "" as string,
    status: "" as string,
    phases: [] as PhaseEvent[],
    answer: "" as string,
    isStreaming: false,
    error: "" as string,
    es: null as EventSource | null,
  }),
  getters: {
    selectedProfiles(state): UserProfile[] {
      return state.profiles.filter((p) => state.selectedIds.includes(p.id));
    },
    canSend(): boolean {
      return this.selectedIds.length >= 1 && !this.isStreaming;
    },
  },
  actions: {
    async loadProfiles() {
      this.loadingProfiles = true;
      this.error = "";
      try {
        const res = await getProfiles();
        this.profiles = res.items;
      } catch (e) {
        this.error = errMsg(e);
      } finally {
        this.loadingProfiles = false;
      }
    },
    bindProfile(id: number) {
      if (!this.selectedIds.includes(id)) this.selectedIds.push(id);
    },
    removeProfile(id: number) {
      this.selectedIds = this.selectedIds.filter((x) => x !== id);
    },
    participants() {
      return this.selectedProfiles.map((p, i) => ({
        participant_ref: `p${i + 1}`,
        user_id: p.id,
        display_name: p.display_name,
      }));
    },
    reset() {
      if (this.es) this.es.close();
      this.messages = [];
      this.requestId = "";
      this.status = "";
      this.phases = [];
      this.answer = "";
      this.error = "";
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
        const participants = this.participants();
        const resp = await createRequest({
          idempotency_key: `web-${Date.now()}`,
          participants,
          message: content,
        });
        this.requestId = resp.request_id;
        this.status = resp.status;

        // SSE 分阶段
        if (this.es) this.es.close();
        this.es = subscribeEvents(resp.request_id, (e: PhaseEvent) => {
          this.phases = [...this.phases, e];
          if (e.event === "answer_ready") {
            const text = String((e.data as { text?: string }).text || "");
            this.answer = text;
            const pending = this.messages.find((m) => m.id === assistantId);
            if (pending) { pending.content = text; pending.status = "complete"; }
          }
          if (e.event === "error") {
            this.error = String((e.data as { message?: string }).message || "流程失败");
          }
        });

        // 轮询终态
        const poll = async () => {
          const st = await getStatus(resp.request_id);
          this.status = st.status;
          if (TERMINAL.has(st.status)) {
            if (this.es) { this.es.close(); this.es = null; }
            this.isStreaming = false;
            if (st.error) this.error = `${st.error.code || "error"}: ${st.error.message || ""}`;
            const pending = this.messages.find((m) => m.id === assistantId);
            if (pending && pending.status === "sending") {
              pending.content = pending.content || st.error?.message || "流程结束";
              pending.status = st.status === "completed" ? "complete" : "error";
            }
            return;
          }
          window.setTimeout(poll, 2000);
        };
        await poll();
      } catch (e) {
        this.error = errMsg(e);
        const pending = this.messages.find((m) => m.id === assistantId);
        if (pending) { pending.content = this.error; pending.status = "error"; }
        this.isStreaming = false;
      }
    },
  },
});
