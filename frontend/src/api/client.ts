import type { Participant, PhaseEvent, RequestState, UserProfile } from "@/types";

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

export function getProfiles(): Promise<{ items: UserProfile[]; total: number }> {
  return request<{ items: UserProfile[]; total: number }>("/v1/users");
}

export function createRequest(payload: {
  idempotency_key: string;
  participants: Participant[];
  message: string;
}): Promise<RequestState> {
  return request<RequestState>("/v1/recommendation-requests", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getStatus(requestId: string): Promise<RequestState> {
  return request<RequestState>(`/v1/recommendation-requests/${requestId}`);
}

export function subscribeEvents(
  requestId: string,
  onEvent: (e: PhaseEvent) => void,
): EventSource {
  const es = new EventSource(`${API}/v1/recommendation-requests/${requestId}/events`);
  const types = [
    "request_accepted", "analysis_ready", "answer_ready", "result_committed",
    "error", "request_cancelled", "clarification_needed",
  ];
  for (const t of types) {
    es.addEventListener(t, (e) => {
      try {
        onEvent({
          id: (e as MessageEvent).lastEventId,
          event: t,
          data: JSON.parse((e as MessageEvent).data),
        });
      } catch {
        /* 忽略非 JSON */
      }
    });
  }
  return es;
}
