// Completion checks beyond the original A01-A10 regression probes.
import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useRecommendationStore } from "../src/stores/recommendation";
import { cancelRequest, createRequest, getStatus, subscribeEvents } from "../src/api/client";

vi.mock("../src/api/client", () => ({
  createSession: vi.fn(), createRequest: vi.fn(), getSession: vi.fn(),
  getStatus: vi.fn(), cancelRequest: vi.fn(),
  subscribeEvents: vi.fn(() => ({ close: vi.fn(), lastEventId: "" })),
}));

let store: ReturnType<typeof useRecommendationStore>;
beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  setActivePinia(createPinia());
  store = useRecommendationStore();
  store.selectedRefs = ["p1"];
  store.sessionId = "audit-session";
  store.sessionRefs = ["p1"];
  vi.mocked(subscribeEvents).mockReturnValue({ close: vi.fn(), lastEventId: "" });
});
afterEach(() => store.closeConnection());

it("C01: reopening a pending-verification chat must reconcile its request", async () => {
  store.activeChatId = "pending-chat";
  store.requestId = "pending-request";
  store.isStreaming = true;
  store.messages = [{ id: "pending-assistant", role: "assistant", content: "",
    createdAt: 1, status: "sending", requestId: "pending-request" }];
  vi.mocked(cancelRequest).mockRejectedValueOnce(Object.assign(new Error("REQUEST_ALREADY_TERMINAL"), {
    status: 409, body: {},
  }));
  vi.mocked(getStatus).mockRejectedValueOnce(new Error("temporary network failure"));
  await store.cancel();
  expect(store.status).toBe("pending_verification");
  store.newChat();
  vi.mocked(getStatus).mockResolvedValueOnce({ request_id: "pending-request",
    session_id: "audit-session", status: "completed", created_at: "t" });
  await store.openChat("pending-chat");
  console.log("C01 observed", { status: store.status, statusRequests: vi.mocked(getStatus).mock.calls.length });
  expect(getStatus).toHaveBeenCalledTimes(2);
  expect(store.status).toBe("completed");
});

it("C02: an unaccepted clarification must not already be marked confirmed", async () => {
  store.activeClarification = { question_id: "q", question_text: "确认口味",
    options: [{ option_id: 1, text: "清淡" }] };
  store.messages = [{ id: "question", role: "assistant", content: "", createdAt: 1,
    status: "complete", clarification: store.activeClarification }];
  let release!: (value: any) => void;
  vi.mocked(createRequest).mockReturnValueOnce(new Promise((resolve) => { release = resolve; }));
  const selecting = store.selectOption(1, "question");
  await vi.waitFor(() => expect(createRequest).toHaveBeenCalled());
  const confirmedBeforeAcceptance = store.messages[0].selectedOptionId;
  release({ request_id: "new-request", status: "running", created_at: "t" });
  await selecting;
  console.log("C02 observed", { confirmedBeforeAcceptance });
  expect(confirmedBeforeAcceptance).toBeUndefined();
});
