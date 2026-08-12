import type {
  AnonymousParticipant,
  PhaseEvent,
  RequestState,
} from "@/types";

const API = (import.meta.env.VITE_API_BASE_URL || "/api").replace(/\/$/, "");

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`${API}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    const detail = body?.error || body?.detail || `请求失败（${r.status}）`;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
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

export function createRequest(payload: {
  idempotency_key: string;
  participants: AnonymousParticipant[];
  message: string;
  session_id: string;
}): Promise<RequestState> {
  return request<RequestState>("/v1/recommendation-requests", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getStatus(requestId: string): Promise<RequestState> {
  return request<RequestState>(`/v1/recommendation-requests/${requestId}`);
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
        if (me.lastEventId) lastEventId = me.lastEventId;
        failures = 0; // 收到新事件 → 重置连续失败计数
        try {
          onEvent({ id: lastEventId, event: t, data: JSON.parse(me.data) });
        } catch {
          /* 忽略非 JSON */
        }
      });
    }
    es.onerror = () => {
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
