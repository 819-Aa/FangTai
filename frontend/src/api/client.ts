import type {
  AnonymousParticipant,
  ClarificationResponse,
  CreateRequestPayload,
  PhaseEvent,
  RequestState,
  SessionState,
} from "@/types";

const API = (import.meta.env.VITE_API_BASE_URL || "/api").replace(/\/$/, "");

export class ApiError extends Error {
  status: number;
  body: Record<string, unknown>;

  constructor(message: string, status: number, body: Record<string, unknown> = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`${API}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!r.ok) {
    const body = (await r.json().catch(() => ({}))) as Record<string, unknown>;
    const detail = body?.error || body?.detail || `请求失败（${r.status}）`;
    const message = typeof detail === "string" ? detail : JSON.stringify(detail);
    throw new ApiError(message, r.status, body);
  }
  return (await r.json()) as T;
}

export function createSession(participantRefs: string[]): Promise<{ session_id: string }> {
  return request<{ session_id: string }>("/v1/sessions", {
    method: "POST",
    body: JSON.stringify({
      participants: participantRefs.map((r) => ({ participant_ref: r })),
    }),
  });
}

export function getSession(sessionId: string): Promise<SessionState> {
  return request<SessionState>(`/v1/sessions/${sessionId}`);
}

export function createRequest(payload: CreateRequestPayload): Promise<RequestState> {
  return request<RequestState>("/v1/recommendation-requests", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getStatus(requestId: string): Promise<RequestState> {
  return request<RequestState>(`/v1/recommendation-requests/${requestId}`);
}

export function cancelRequest(requestId: string): Promise<{ status: string }> {
  return request<{ status: string }>(`/v1/recommendation-requests/${requestId}/cancel`, {
    method: "POST",
  });
}

// SSE 连续失败阈值：达到后由调用方关闭并降级轮询
export const SSE_MAX_RETRIES = 3;

export interface SSEConnection {
  close(): void;
  readonly lastEventId: string;
}

export function subscribeEvents(
  requestId: string,
  onEvent: (e: PhaseEvent) => void,
  onError: (failureCount: number) => void,
): SSEConnection {
  const types = [
    "request_accepted", "analysis_ready", "answer_ready", "result_committed",
    "error", "request_cancelled", "clarification_needed", "request_terminal",
    "request_recovery_required",
    "text_delta", "thought_node", "tool_trace", "menu_artifact",
  ];
  let closed = false;
  let lastEventId = "";
  let failures = 0;
  let es: EventSource | null = null;

  function connect(): void {
    if (closed) return;
    // 浏览器 EventSource 原生携带 Last-Event-ID 重连续传
    es = new EventSource(`${API}/v1/recommendation-requests/${requestId}/events`);
    for (const t of types) {
      es.addEventListener(t, (e) => {
        const me = e as MessageEvent;
        // 原生网络 error 没有 data，不能将其当作 SSE 消息清零失败计数。
        if (typeof me.data !== "string") return;
        try {
          const data = JSON.parse(me.data);
          if (me.lastEventId) lastEventId = me.lastEventId;
          failures = 0; // 仅有效 SSE 消息证明连接恢复。
          onEvent({ id: lastEventId, event: t, data });
        } catch {
          /* 忽略非 JSON */
        }
      });
    }
    es.onerror = (e) => {
      // 服务端 event:error 是业务消息，不属于浏览器传输失败。
      if (typeof (e as MessageEvent).data === "string") return;
      failures += 1;
      onError(failures);
      if (failures >= SSE_MAX_RETRIES) {
        // 连续第 3 次失败 → 关闭（调用方降级轮询）；前两次依赖原生重连
        close();
      }
    };
  }

  function close(): void {
    closed = true;
    if (es) {
      es.close();
      es = null;
    }
  }

  connect();
  return {
    close,
    get lastEventId() {
      return lastEventId;
    },
  };
}
