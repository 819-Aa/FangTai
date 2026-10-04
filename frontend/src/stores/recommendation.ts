import { defineStore } from "pinia";

import {
  ApiError,
  cancelRequest,
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
  type ClarificationOptionView,
  type ClarificationResponse,
  type ClarificationView,
  type CreateRequestPayload,
  type PhaseEvent,
  type PublicMenuItem,
  type PublicMenuSummary,
  type ReplaceDishTarget,
  type RequestState,
  type ThoughtStep,
} from "@/types";

export function parseClarificationView(data: unknown): ClarificationView | null {
  if (!data || typeof data !== "object") return null;
  const raw = data as Record<string, unknown>;
  const question_id = String(raw.question_id || "");
  if (!question_id) return null;
  const question_text = String(raw.question_text || raw.clarification || "");
  const rawOptions = Array.isArray(raw.options) ? raw.options : [];
  const options: ClarificationOptionView[] = [];
  for (const opt of rawOptions) {
    if (opt && typeof opt === "object") {
      const o = opt as Record<string, unknown>;
      const option_id = Number(o.option_id);
      const text = String(o.text || "");
      if (Number.isInteger(option_id) && option_id > 0 && text) {
        options.push({ option_id, text });
      }
    }
  }
  return {
    question_id,
    question_text,
    options,
    expires_at: typeof raw.expires_at === "number" ? raw.expires_at : null,
    status: typeof raw.status === "string" ? raw.status : "pending",
  };
}

function stageTitle(stage: string): string {
  switch (stage) {
    case "context_ready":
      return "上下文与需求分析";
    case "query_understanding":
      return "需求理解与规划";
    case "candidate_search":
      return "菜品候选检索";
    case "recipe_audit":
      return "健康合规审查";
    case "menu_combination":
      return "营养与偏好组合";
    case "final_validation":
      return "全流程合规复核";
    case "authoritative_answer":
      return "推荐回答生成";
    case "commit":
      return "结果持久化";
    default:
      return stage ? `阶段分析（${stage}）` : "阶段分析";
  }
}

export function updateMessageThoughts(
  msg: ChatMessage,
  event: string,
  data: Record<string, unknown>,
  participantCount: number,
): void {
  msg.thoughts = msg.thoughts || [];
  const invocation = typeof data.invocation_id === "string" ? data.invocation_id : undefined;
  const generation = typeof data.execution_generation === "number" ? data.execution_generation : undefined;
  const matchingStep = (nodeId: string) => msg.thoughts!.find((t) =>
    t.node_id === nodeId && (invocation
      ? t.invocation_id === invocation && t.execution_generation === generation
      : !t.invocation_id),
  );

  if (event === "thought_node") {
    const nodeId = String(data.node_id || "");
    if (!nodeId) return;
    const existing = matchingStep(nodeId);
    const step: ThoughtStep = {
      node_id: nodeId,
      invocation_id: invocation,
      execution_generation: generation,
      title: String(data.title || nodeId),
      status: (data.status as ThoughtStep["status"]) || "running",
      summary: data.summary ? String(data.summary) : undefined,
      tool_name: data.tool_name ? String(data.tool_name) : undefined,
      duration_ms: typeof data.duration_ms === "number" ? data.duration_ms : undefined,
    };
    if (existing) {
      Object.assign(existing, step);
    } else {
      msg.thoughts.push(step);
    }
    return;
  }

  if (event === "tool_trace") {
    const target = invocation
      ? msg.thoughts.find((t) => t.invocation_id === invocation && t.execution_generation === generation)
      : data.node_id ? matchingStep(String(data.node_id)) : msg.thoughts.at(-1);
    if (target) {
      if (data.tool_name) target.tool_name = String(data.tool_name);
      if (data.result_summary) target.summary = String(data.result_summary);
    }
    return;
  }

  if (event === "request_accepted") {
    if (!msg.thoughts.some((t) => t.node_id === "accepted")) {
      msg.thoughts.push({
        node_id: "accepted",
        title: "请求已接收",
        status: "done",
        summary: `服务端已受理推荐请求（参与者数：${participantCount}），进入排队处理`,
      });
    }
  } else if (event === "analysis_ready") {
    const stage = String(data.stage || "");
    const title = stageTitle(stage);
    const summary = String(data.summary || `${title}已完成`);
    const nodeId = invocation && data.node_id ? String(data.node_id) : stage ? `analysis_${stage}` : "analysis";
    const status = (data.status as ThoughtStep["status"]) || "done";
    const existing = matchingStep(nodeId);
    if (existing) {
      existing.status = status;
      existing.summary = summary;
    } else {
      msg.thoughts.push({
        node_id: nodeId,
        invocation_id: invocation,
        execution_generation: generation,
        title,
        status,
        summary,
      });
    }
  } else if (event === "answer_ready") {
    if (!msg.thoughts.some((t) => t.node_id === "answer_ready")) {
      msg.thoughts.push({
        node_id: "answer_ready",
        title: "推荐回答已生成",
        status: "done",
        summary: "候选菜单已生成，待最终确认提交",
      });
    }
  } else if (event === "result_committed") {
    for (const t of msg.thoughts) {
      if (t.status === "running") t.status = "done";
    }
    if (!msg.thoughts.some((t) => t.node_id === "result_committed")) {
      msg.thoughts.push({
        node_id: "result_committed",
        title: "菜单结果已提交",
        status: "done",
        summary: "全流程通过健康合规复核并事务性持久化",
      });
    }
  } else if (event === "clarification_needed") {
    const running = msg.thoughts.find((t) => t.status === "running");
    if (running) {
      running.status = "warning";
      running.summary = String(data.clarification || "等待用户确认澄清选项");
    } else {
      msg.thoughts.push({
        node_id: "clarification",
        title: "等待用户确认",
        status: "warning",
        summary: String(data.clarification || "检测到约束冲突，需进一步澄清"),
      });
    }
  } else if (event === "request_cancelled" || event === "error" || event === "request_terminal") {
    const running = msg.thoughts.find((t) => t.status === "running");
    if (running) {
      running.status = event === "request_cancelled" ? "warning" : "error";
      running.summary = event === "request_cancelled" ? "请求已取消" : "处理未完成终止";
    }
  }
}

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
    case "pending_verification":
      return "状态待核对";
    case "recovery_required":
      return "等待恢复生成";
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

// 每次打开页面都是新对话；成员名单固定，选择只属于当前对话。
const MEMBER_COUNT = 50;

const POLL_INTERVAL_MS = 5000;
const CHAT_STORAGE_KEY = "v2.chat_history.v1";

export interface ChatRecord {
  id: string;
  title: string;
  updatedAt: number;
  selectedRefs: string[];
  sessionId: string;
  sessionRefs: string[];
  messages: ChatMessage[];
  requestId: string;
  status: string;
  phases: PhaseEvent[];
  answer: string;
  currentMenu: PublicMenuSummary | null;
  clarification: string;
  activeClarification: ClarificationView | null;
  error: string;
  pendingRequest?: PendingRequest | null;
}

interface PendingRequest {
  payload: CreateRequestPayload;
  assistantId: string;
  clarificationMessageId?: string;
}

function loadChats(): ChatRecord[] {
  try {
    const value = JSON.parse(localStorage.getItem(CHAT_STORAGE_KEY) || "[]");
    if (!Array.isArray(value)) return [];
    return value.filter((chat): chat is ChatRecord =>
      chat && typeof chat.id === "string" && typeof chat.title === "string"
      && Array.isArray(chat.messages) && Array.isArray(chat.selectedRefs)
      && typeof chat.sessionId === "string",
    );
  } catch {
    return [];
  }
}

export const useRecommendationStore = defineStore("recommendation", {
  state: () => {
    return {
      slots: Array.from({ length: MEMBER_COUNT }, (_, i) => slotFromRef(`p${i + 1}`)) as AnonymousParticipant[],
      chats: loadChats(),
      activeChatId: "" as string,
      storageError: "" as string,
      selectedRefs: [] as string[],
      sessionId: "" as string,
      sessionRefs: [] as string[],
      _sessionPromise: null as Promise<string> | null,
      messages: [] as ChatMessage[],
      requestId: "" as string,
      status: "" as string,
      phases: [] as PhaseEvent[],
      answer: "" as string,
      currentMenu: null as PublicMenuSummary | null,
      pendingReplaceDish: null as ReplaceDishTarget | null,
      clarification: "" as string,
      activeClarification: null as ClarificationView | null,
      isStreaming: false,
      isCancelling: false,
      pendingCancel: false,
      pendingRequest: null as PendingRequest | null,
      isRecovering: false,
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
      return this.selectedRefs.length >= 1 && !this.isStreaming && !this.isCancelling
        && !this.isRecovering && this.status !== "recovery_required";
    },
    canRecover(): boolean {
      return this.status === "recovery_required" && !!this.pendingRequest && !this.isRecovering;
    },
    canEditParticipants(): boolean {
      return !this.isStreaming && !this.sessionId && this.messages.length === 0;
    },
  },
  actions: {
    persistActiveChat() {
      if (!this.activeChatId) return;
      const firstUserMessage = this.messages.find((message) => message.role === "user");
      const record: ChatRecord = {
        id: this.activeChatId,
        title: firstUserMessage?.content.slice(0, 40) || "新对话",
        updatedAt: Date.now(),
        selectedRefs: [...this.selectedRefs],
        sessionId: this.sessionId,
        sessionRefs: [...this.sessionRefs],
        messages: this.messages.map((message) => ({
          ...message,
          thoughts: message.thoughts ? message.thoughts.map((t) => ({ ...t })) : undefined,
        })),
        requestId: this.requestId,
        status: this.status,
        phases: this.phases.map((phase) => ({ ...phase, data: { ...phase.data } })),
        answer: this.answer,
        currentMenu: this.currentMenu ? JSON.parse(JSON.stringify(this.currentMenu)) as PublicMenuSummary : null,
        clarification: this.clarification,
        activeClarification: this.activeClarification
          ? JSON.parse(JSON.stringify(this.activeClarification)) as ClarificationView : null,
        error: this.error,
        pendingRequest: this.pendingRequest ? JSON.parse(JSON.stringify(this.pendingRequest)) : null,
      };
      const index = this.chats.findIndex((chat) => chat.id === this.activeChatId);
      if (index >= 0) this.chats.splice(index, 1);
      this.chats.unshift(record);
      try {
        localStorage.setItem(CHAT_STORAGE_KEY, JSON.stringify(this.chats));
        this.storageError = "";
      } catch {
        this.storageError = "浏览器存储空间不足，对话历史可能无法在刷新后恢复。";
      }
    },
    async openChat(id: string) {
      if (this.isStreaming || this.isRecovering || id === this.activeChatId) return;
      const chat = this.chats.find((item) => item.id === id);
      if (!chat) return;
      this.persistActiveChat();
      this.reset();
      this.activeChatId = chat.id;
      this.selectedRefs = [...chat.selectedRefs];
      this.sessionId = chat.sessionId;
      this.sessionRefs = [...chat.sessionRefs];
      this.messages = chat.messages.map((message) => ({
        ...message,
        thoughts: message.thoughts ? message.thoughts.map((t) => ({ ...t })) : undefined,
        clarification: message.clarification
          ? {
              ...message.clarification,
              options: message.clarification.options.map((o) => ({ ...o })),
            }
          : undefined,
      }));
      this.requestId = chat.requestId;
      this.status = chat.status;
      this.phases = chat.phases.map((phase) => ({ ...phase, data: { ...phase.data } }));
      this.answer = chat.answer;
      this.currentMenu = chat.currentMenu;
      this.clarification = chat.clarification;
      this.activeClarification = chat.activeClarification;
      this.error = chat.error;
      this.pendingRequest = chat.pendingRequest ? JSON.parse(JSON.stringify(chat.pendingRequest)) : null;
      const isPendingVerification = this.status === "pending_verification";
      const isRecovery = this.status === "recovery_required";
      const pending = this.messages.findLast(
        (message) =>
          message.role === "assistant" &&
          (message.status === "sending" || ((isPendingVerification || isRecovery) && (!message.requestId || message.requestId === this.requestId))),
      ) || (isPendingVerification ? this.messages.findLast((m) => m.role === "assistant") : undefined);

      if (!pending && !isPendingVerification && !isRecovery) return;
      if (!this.requestId) {
        if (pending) {
          pending.status = "error";
          pending.content = "上次请求未能确认提交，请重新发送。";
        }
        this.persistActiveChat();
        return;
      }
      try {
        const state = await getStatus(this.requestId);
        if (this.activeChatId !== id) return;
        this.status = state.status;
        if (pending) {
          if (state.status === "recovery_required") {
            this.pauseForRecovery(pending.id);
            return;
          }
          this.applyResultSummary(state, pending.id);
          if (isTerminal(state.status) || state.status === "needs_clarification") {
            this.finishStreaming(
              pending.id,
              state.status === "completed" || state.status === "needs_clarification" ? "complete" : "error",
            );
          } else {
            this.isStreaming = true;
            this.openSse(this.requestId, pending.id);
          }
        }
        this.persistActiveChat();
      } catch {
        if (this.activeChatId !== id) return;
        if (!isPendingVerification && !isRecovery) {
          this.isStreaming = true;
          if (pending) this.openSse(this.requestId, pending.id);
        }
      }
    },
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
    toggleSlot(ref: string) {
      if (!this.canEditParticipants || !this.slots.some((slot) => slot.participant_ref === ref)) return;
      if (this.selectedRefs.includes(ref)) {
        this.selectedRefs = this.selectedRefs.filter((r) => r !== ref);
      } else {
        this.selectedRefs = this.slots
          .map((slot) => slot.participant_ref)
          .filter((slotRef) => slotRef === ref || this.selectedRefs.includes(slotRef));
      }
    },
    participants(): AnonymousParticipant[] {
      return this.selectedRefs.map((r) => ({
        participant_ref: r,
        label: this.slotLabel(r),
      }));
    },
    reset() {
      this.closeConnection();
      this.isStreaming = false;
      this.isCancelling = false;
      this.pendingCancel = false;
      this.pendingRequest = null;
      this.isRecovering = false;
      this.messages = [];
      this.requestId = "";
      this.status = "";
      this.phases = [];
      this.answer = "";
      this.currentMenu = null;
      this.pendingReplaceDish = null;
      this.clarification = "";
      this.activeClarification = null;
      this.error = "";
    },
    newChat() {
      if (this.isStreaming || this.isRecovering) return;
      this.persistActiveChat();
      this.reset();
      this.activeChatId = "";
      this.sessionId = "";
      this.sessionRefs = [];
      this.selectedRefs = [];
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
    async cancel() {
      if (!this.isStreaming || this.isCancelling) return;
      this.isCancelling = true;

      // 若尚未获得 requestId（还在 createRequest 等待期间），记录待取消意图，拿到 ID 后立即向服务端补发
      if (!this.requestId) {
        this.pendingCancel = true;
        return;
      }

      this.pendingCancel = false;
      const targetReqId = this.requestId;

      try {
        const res = await cancelRequest(targetReqId);
        if (res.status === "cancelled") {
          this.closeConnection();
          this.isStreaming = false;
          this.status = "cancelled";
          this.pendingRequest = null;
          const pending = this.messages.findLast((m) => m.role === "assistant" && m.status === "sending");
          if (pending) {
            pending.status = "cancelled";
            pending.isCommitted = false;
            pending.isAnswered = false;
            if (!pending.content) {
              pending.content = "已停止生成。";
            }
          }
        }
      } catch (e) {
        const errStatus = (e as ApiError)?.status;
        const errBody = (e as ApiError)?.body || {};
        const currentStatus = String(errBody.current_status || "");

        if (errStatus === 409 || currentStatus) {
          // 409 REQUEST_ALREADY_TERMINAL: 终态竞态，服务端已完成或已进入其他终态，绝不改写为 cancelled
          try {
            const latestState = await getStatus(targetReqId);
            this.status = latestState.status;
            const pending = this.messages.findLast((m) => m.role === "assistant");
            if (pending) {
              this.applyResultSummary(latestState, pending.id);
              this.finishStreaming(
                pending.id,
                latestState.status === "completed" || latestState.status === "needs_clarification"
                  ? "complete"
                  : "error",
              );
            }
          } catch {
            if (currentStatus) {
              this.status = currentStatus;
              const pending = this.messages.findLast((m) => m.role === "assistant");
              if (pending) {
                this.finishStreaming(
                  pending.id,
                  currentStatus === "completed" || currentStatus === "needs_clarification"
                    ? "complete"
                    : "error",
                );
              }
            } else {
              // 状态无法核实：维持“状态待核对”，绝不根据缺失的 GET 结果猜测完成（R02）
              this.status = "pending_verification";
              this.error = "请求已终态但状态核对失败，请刷新页面核对";
              const pending = this.messages.findLast((m) => m.role === "assistant");
              if (pending) this.finishStreaming(pending.id, "error");
            }
          }
        } else {
          // 网络错误或其它异常：不把本地状态当作服务端成功，给出提示并维持连接/轮询兜底
          this.error = `取消请求未获服务端确认：${errMsg(e)}`;
        }
      } finally {
        this.isCancelling = false;
        this.persistActiveChat();
      }
    },
    async selectOption(optionId: number, messageId?: string) {
      if (!this.activeClarification || this.isStreaming) return;
      const opt = this.activeClarification.options.find((o) => o.option_id === optionId);
      if (!opt) return;

      // 查找对应 assistant message，但在服务端受理前不提前标记 selectedOptionId (C02)
      let targetMsg = messageId ? this.messages.find((m) => m.id === messageId) : undefined;
      if (!targetMsg) {
        targetMsg = this.messages.findLast(
          (m) =>
            m.role === "assistant" &&
            m.clarification?.question_id === this.activeClarification?.question_id,
        );
      }

      const clarificationResponse: ClarificationResponse = {
        question_id: this.activeClarification.question_id,
        option_id: opt.option_id,
      };
      await this.send(opt.text, clarificationResponse, targetMsg?.id || messageId);
    },
    async refreshClarification() {
      if (!this.sessionId) return;
      try {
        const state = await getSession(this.sessionId);
        if (state.active_clarification) {
          const parsed = parseClarificationView(state.active_clarification);
          this.activeClarification = parsed;
          this.clarification = parsed?.question_text || "";
          this.status = "needs_clarification";
          if (parsed) {
            const target = this.messages.findLast(
              (m) =>
                m.role === "assistant" &&
                (!m.clarification || m.clarification.question_id === parsed.question_id),
            );
            if (target) {
              target.clarification = parsed;
            }
          }
        } else {
          this.activeClarification = null;
          this.clarification = "";
        }
        this.persistActiveChat();
      } catch {
        /* 忽略刷新错误 */
      }
    },
    startReplaceDish(dish: PublicMenuItem) {
      if (this.isStreaming || !this.currentMenu) return;
      if (!this.currentMenu.recipe_ids.includes(dish.recipe_id)) return;
      this.pendingReplaceDish = {
        target_recipe_id: dish.recipe_id,
        dish_name: dish.name,
        source_plan_id: this.currentMenu.plan_id,
        source_menu_hash: this.currentMenu.menu_hash,
      };
    },
    cancelReplaceDish() {
      this.pendingReplaceDish = null;
    },
    async send(
      message: string,
      clarificationResponse?: ClarificationResponse,
      clarificationMessageId?: string,
    ) {
      const content = message.trim();
      if (!content || !this.canSend) return;
      this.error = "";
      this.requestId = "";
      this.isStreaming = true;
      this.isCancelling = false;
      this.pendingCancel = false;
      this.phases = [];
      this.answer = "";
      this.clarification = "";
      const previousClarification = this.activeClarification;
      this.activeClarification = null;
      this.status = "";
      this.messages.push({
        id: messageId(), role: "user", content,
        createdAt: Date.now(), status: "complete",
      });
      const assistantId = messageId();
      this.messages.push({
        id: assistantId, role: "assistant", content: "",
        createdAt: Date.now(), status: "sending",
        isAnswered: false, isCommitted: false,
      });
      if (!this.activeChatId) this.activeChatId = messageId();
      this.persistActiveChat();

      const lockedReplace = this.pendingReplaceDish;
      try {
        const refs = this.selectedRefs.slice();
        const sessionId = await this.ensureSession(refs);
        this.persistActiveChat();
        const participants = refs.map((r) => ({
          participant_ref: r,
          label: this.slotLabel(r),
        }));
        const payload: CreateRequestPayload = {
          idempotency_key: `web-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
          participants,
          message: content,
          session_id: sessionId,
          clarification_response: clarificationResponse,
          ...(lockedReplace
            ? {
                action: "replace_dish",
                target_recipe_id: lockedReplace.target_recipe_id,
                source_plan_id: lockedReplace.source_plan_id,
                source_menu_hash: lockedReplace.source_menu_hash,
              }
            : {}),
        };
        // 在 POST 前保存原载荷，恢复时复用幂等键及版本/澄清字段。
        this.pendingRequest = { payload, assistantId, clarificationMessageId };
        this.persistActiveChat();
        const resp = await createRequest(payload);
        this.requestId = resp.request_id;
        this.status = resp.status;
        this.pendingReplaceDish = null;
        const pendingAssistant = this.messages.find((m) => m.id === assistantId);
        if (pendingAssistant) {
          pendingAssistant.requestId = resp.request_id;
        }

        // 服务端受理后，再将选项标记为已确认 (C02 / R06)
        if (clarificationResponse) {
          let qMsg = clarificationMessageId ? this.messages.find((m) => m.id === clarificationMessageId) : undefined;
          if (!qMsg) {
            qMsg = this.messages.findLast(
              (m) =>
                m.role === "assistant" &&
                m.clarification?.question_id === clarificationResponse.question_id,
            );
          }
          if (qMsg) {
            qMsg.selectedOptionId = clarificationResponse.option_id;
          }
        }

        if (this.pendingCancel) {
          // 创建请求等待期间用户已点击取消，立即向服务端发起取消（R02）
          this.pendingCancel = false;
          this.isCancelling = false;
          await this.cancel();
          // 若取消未能获得服务端成功确认（例如网络错误），须维持连接/轮询兜底 (A02)
          if (this.status !== "cancelled" && this.isStreaming) {
            this.openSse(resp.request_id, assistantId);
          }
          return;
        }

        this.persistActiveChat();
        if (resp.status === "recovery_required") {
          this.pauseForRecovery(assistantId);
          return;
        }
        // 打开 SSE；SSE 存活期间不主动轮询
        this.openSse(resp.request_id, assistantId);
      } catch (e) {
        this.pendingCancel = false;
        this.isCancelling = false;
        const errText = errMsg(e);
        this.error = errText;
        const pending = this.messages.find((m) => m.id === assistantId);
        if (pending) {
          pending.content = this.error;
          pending.status = "error";
        }
        this.isStreaming = false;

        if (clarificationResponse) {
          // 澄清提交未被受理时，恢复可重试状态 (A03)
          let qMsg = clarificationMessageId ? this.messages.find((m) => m.id === clarificationMessageId) : undefined;
          if (!qMsg) {
            qMsg = this.messages.find(
              (m) => m.clarification?.question_id === clarificationResponse.question_id,
            );
          }
          if (!qMsg) {
            qMsg = this.messages.findLast((m) => m.role === "assistant" && m.clarification);
          }
          if (qMsg) {
            qMsg.selectedOptionId = undefined;
          }
          if (previousClarification) {
            this.activeClarification = previousClarification;
            this.clarification = previousClarification.question_text || "";
            this.status = "needs_clarification";
          }
        }

        const errStatus = (e as ApiError)?.status;
        const errBody = (e as ApiError)?.body || {};
        const errCode = String(errBody.error || errBody.code || "");
        if (errCode === "REQUEST_RECOVERY_PENDING" && this.pendingRequest) {
          this.requestId = String(errBody.request_id || this.requestId);
          if (pending) pending.requestId = this.requestId;
          this.pauseForRecovery(assistantId);
          return;
        }
        this.pendingRequest = null;

        if (
          errText.includes("MENU_VERSION_CONFLICT") ||
          errCode === "MENU_VERSION_CONFLICT" ||
          (errStatus === 409 && lockedReplace)
        ) {
          // 409 版本冲突：服务端菜单已发生变化，清除待替换目标并主动刷新服务端最新 session 菜单 (R07)
          this.pendingReplaceDish = null;
          await this.restoreSession();
          const conflictMsg = "菜单版本已变更，已为您同步最新菜单，请重新选择要替换的菜品。";
          this.error = conflictMsg;
          if (pending) {
            pending.content = conflictMsg;
          }
        } else if (
          errText.includes("CLARIFICATION_") ||
          errCode.startsWith("CLARIFICATION_") ||
          (errStatus === 409 && clarificationResponse)
        ) {
          // 409 澄清冲突 (A04)：不能报为菜单版本冲突
          await this.refreshClarification();
          const clarErrMsg = "当前澄清选项已失效或已过期，请根据最新提示重新确认。";
          this.error = clarErrMsg;
          if (pending) {
            pending.content = clarErrMsg;
          }
        } else if (
          errText.includes("TARGET_RECIPE_NOT_IN_MENU") ||
          (errStatus === 422 && errText.includes("target_recipe_id"))
        ) {
          this.pendingReplaceDish = null;
          await this.restoreSession();
          const notInMenuMsg = "目标菜品不在当前菜单中，已为您同步最新菜单。";
          this.error = notInMenuMsg;
          if (pending) {
            pending.content = notInMenuMsg;
          }
        } else if (errText.includes("SESSION_PROTOCOL_UNSUPPORTED")) {
          // 清除旧 session 状态，重置为新会话准备，不自动重发
          this.sessionId = "";
          this.sessionRefs = [];
          this.activeClarification = null;
          this.clarification = "";
          const friendlyMsg = "当前会话协议已废弃，无法继续。请直接在新会话中提出完整用餐需求。";
          this.error = friendlyMsg;
          if (pending) {
            pending.content = friendlyMsg;
          }
        }
        this.persistActiveChat();
      }
    },
    pauseForRecovery(assistantId: string) {
      this.status = "recovery_required";
      this.isStreaming = false;
      this.closeConnection();
      this.error = this.pendingRequest
        ? "请求暂未完成，可以点击“恢复生成”继续。"
        : "请求暂未完成，当前浏览器缺少恢复记录，请新建对话。";
      const pending = this.messages.find((m) => m.id === assistantId);
      if (pending) {
        pending.status = "error";
        pending.isCommitted = false;
        if (!pending.content) pending.content = terminalLabel(this.status);
      }
      this.persistActiveChat();
    },
    async recoverRequest() {
      if (!this.canRecover || !this.pendingRequest) return;
      const saved = this.pendingRequest;
      this.isRecovering = true;
      this.error = "";
      try {
        const resp = await createRequest(saved.payload);
        if (this.requestId && resp.request_id !== this.requestId) {
          throw new Error("恢复请求标识不一致");
        }
        this.requestId = resp.request_id;
        const pending = this.messages.find((m) => m.id === saved.assistantId);
        if (pending) pending.requestId = resp.request_id;
        const clarification = saved.payload.clarification_response;
        if (clarification) {
          const question = this.messages.find((m) => m.id === saved.clarificationMessageId)
            || this.messages.findLast((m) => m.clarification?.question_id === clarification.question_id);
          if (question) question.selectedOptionId = clarification.option_id;
          this.activeClarification = null;
          this.clarification = "";
        }
        this.status = resp.status;
        if (isTerminal(resp.status) || resp.status === "needs_clarification") {
          const state = await getStatus(resp.request_id);
          this.status = state.status;
          this.applyResultSummary(state, saved.assistantId);
          this.finishStreaming(saved.assistantId,
            state.status === "completed" || state.status === "needs_clarification" ? "complete" : "error");
        } else if (resp.status === "recovery_required") {
          this.pauseForRecovery(saved.assistantId);
        } else {
          this.phases = [];
          if (pending) {
            pending.status = "sending";
            pending.content = "";
            pending.thoughts = [];
            pending.isAnswered = false;
          }
          this.isStreaming = true;
          this.openSse(resp.request_id, saved.assistantId);
        }
      } catch (e) {
        this.pauseForRecovery(saved.assistantId);
        this.error = `恢复暂未成功，请稍后再试：${errMsg(e)}`;
      } finally {
        this.isRecovering = false;
        this.persistActiveChat();
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
      this.persistActiveChat();
    },
    onSseEvent(e: PhaseEvent, assistantId: string) {
      const before = this.phases.length;
      this.phases = reducePhases(this.phases, [e]);
      if (this.phases.length === before) return; // 重复 event_id 不重复改变 UI
      // 收到新事件 → 连接已恢复（不能停留在"reconnect"）
      if (this.status === "reconnect") this.status = "running";
      const pending = this.messages.find((m) => m.id === assistantId);
      if (pending) {
        updateMessageThoughts(
          pending,
          e.event,
          (e.data || {}) as Record<string, unknown>,
          this.selectedRefs.length,
        );
      }
      switch (e.event) {
        case "request_recovery_required":
          this.pauseForRecovery(assistantId);
          return;
        case "text_delta": {
          const chunk = String(
            (e.data as { chunk?: string; text?: string }).chunk ||
            (e.data as { text?: string }).text || ""
          );
          if (chunk) {
            this.answer = (this.answer || "") + chunk;
            if (pending) pending.content = this.answer;
          }
          break;
        }
        case "menu_artifact": {
          // 仅作为中间事件透传，在对应请求提交前不得写入“已提交菜单”视图（R03）
          break;
        }
        case "answer_ready":
          this.answer = String((e.data as { text?: string }).text || "");
          // 正文写入 assistant message.content；保持 sending/待最终确认，不 completed
          {
            const pending = this.messages.find((m) => m.id === assistantId);
            if (pending) {
              pending.content = this.answer;
              pending.isAnswered = true;
            }
          }
          break;
        case "result_committed":
          this.currentMenu = publicMenu(
            (e.data as { menu_summary?: unknown }).menu_summary,
          );
          this.pendingReplaceDish = null;
          this.status = "completed";
          this.activeClarification = null;
          this.clarification = "";
          {
            const pending = this.messages.find((m) => m.id === assistantId);
            if (pending) {
              pending.isCommitted = true;
              pending.isAnswered = true;
            }
          }
          this.finishStreaming(assistantId, "complete");
          break;
        case "error":
          this.error = String((e.data as { message?: string }).message || "流程失败");
          this.status = "failed";
          this.finishStreaming(assistantId, "error");
          break;
        case "request_cancelled":
          this.status = "cancelled";
          this.finishStreaming(assistantId, "cancelled");
          break;
        case "request_terminal": {
          // 业务终态：no_safe_menu/no_feasible_menu/strict_time_indeterminate/
          // failed/interrupted 分别保持语义，立即关闭连接
          const status = String((e.data as { status?: string }).status || "failed");
          this.status = status;
          if (status !== "needs_clarification") {
            this.activeClarification = null;
            this.clarification = "";
          }
          const msg = String((e.data as { message?: string }).message || "");
          if (msg) this.error = msg;
          this.finishStreaming(assistantId, "error");
          break;
        }
        case "clarification_needed": {
          this.status = "needs_clarification";
          const parsed = parseClarificationView(e.data);
          this.activeClarification = parsed;
          this.clarification = parsed?.question_text
            || String((e.data as { clarification?: string }).clarification || "请补充必要信息");
          const pending = this.messages.find((m) => m.id === assistantId);
          if (pending && parsed) {
            pending.clarification = parsed;
          }
          this.finishStreaming(assistantId, "complete");
          break;
        }
        default:
          break;
      }
      this.persistActiveChat();
    },
    finishStreaming(assistantId: string, msgStatus: "complete" | "error" | "cancelled") {
      if (isTerminal(this.status) || this.status === "needs_clarification") this.pendingRequest = null;
      this.isStreaming = false;
      this.closeConnection();
      const pending = this.messages.find((m) => m.id === assistantId);
      if (pending) {
        if (this.status === "cancelled") {
          pending.status = "cancelled";
          pending.isCommitted = false;
        } else {
          pending.status = msgStatus;
          if (this.status === "completed") {
            pending.isCommitted = true;
          } else {
            pending.isCommitted = false;
          }
        }
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
          if (st.status === "recovery_required") {
            this.pauseForRecovery(assistantId);
            return;
          }
          if (isTerminal(st.status) || st.status === "needs_clarification") {
            if (st.error) {
              this.error = `${st.error.code || "error"}: ${st.error.message || ""}`;
            }
            this.applyResultSummary(st, assistantId);
            this.finishStreaming(
              assistantId,
              st.status === "completed" || st.status === "needs_clarification"
                ? "complete"
                : "error",
            );
            this.persistActiveChat();
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
      if (state.active_clarification) {
        const parsed = parseClarificationView(state.active_clarification);
        this.activeClarification = parsed;
        this.clarification = parsed?.question_text || "";
        const pending = this.messages.find((message) => message.id === assistantId);
        if (pending && parsed) {
          pending.clarification = parsed;
        }
      }
      const summary = state.result_summary;
      if (!summary) return;
      const menu = publicMenu(summary.menu_summary);
      if (menu) this.currentMenu = menu;
      const text = summary.answer?.text?.trim()
        || (typeof (summary as Record<string, unknown>).text === "string"
          ? ((summary as Record<string, unknown>).text as string).trim()
          : "")
        || "";
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
        if (state.active_clarification) {
          const parsed = parseClarificationView(state.active_clarification);
          this.activeClarification = parsed;
          this.clarification = parsed?.question_text || "";
          this.status = "needs_clarification";
          if (parsed) {
            const target = this.messages.findLast(
              (m) =>
                m.role === "assistant" &&
                (!m.clarification || m.clarification.question_id === parsed.question_id),
            );
            if (target) {
              target.clarification = parsed;
            }
          }
        }
        this.persistActiveChat();
      } catch {
        // 持久化 session 不存在或暂不可用时保留匿名参与者选择，允许用户继续新建会话。
      }
    },
  },
});
