// Explicit audit suite: asserts required behavior; failures reproduce open defects.
// Run: npx vitest run --config verification/vitest.audit.config.ts
import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useRecommendationStore } from "../src/stores/recommendation";
import { cancelRequest, createRequest, getSession, getStatus, subscribeEvents } from "../src/api/client";

vi.mock("../src/api/client", () => ({
  createSession: vi.fn(), createRequest: vi.fn(), getSession: vi.fn(),
  getStatus: vi.fn(), cancelRequest: vi.fn(),
  subscribeEvents: vi.fn(() => ({ close: vi.fn(), lastEventId: "" })),
}));

let store: ReturnType<typeof useRecommendationStore>;
const question = {
  question_id: "audit-question", question_text: "确认口味",
  options: [{ option_id: 1, text: "清淡" }],
};

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  setActivePinia(createPinia());
  store = useRecommendationStore();
  store.selectedRefs = ["p1"];
  store.sessionId = "audit-session";
  store.sessionRefs = ["p1"];
  vi.mocked(subscribeEvents).mockReturnValue({ close: vi.fn(), lastEventId: "" });
  vi.mocked(cancelRequest).mockResolvedValue({ status: "cancelled" });
  vi.mocked(getSession).mockResolvedValue({
    session_id: "audit-session", participant_refs: ["p1"], request_count: 1,
    current_menu: null, active_clarification: question,
  });
});
afterEach(() => store.closeConnection());

it("A01: early cancellation in turn two must target the new request", async () => {
  store.requestId = "previous-request";
  store.messages = [{
    id: "previous-assistant", role: "assistant", content: "old answer",
    createdAt: 1, status: "complete", requestId: "previous-request", isCommitted: true,
  }];
  let release!: (value: any) => void;
  vi.mocked(createRequest).mockReturnValue(new Promise((resolve) => { release = resolve; }));
  vi.mocked(cancelRequest).mockRejectedValueOnce(Object.assign(new Error("already terminal"), {
    status: 409, body: { current_status: "completed" },
  }));
  vi.mocked(getStatus).mockResolvedValueOnce({
    request_id: "previous-request", session_id: "audit-session", status: "completed",
    created_at: "t", result_summary: { status: "completed", answer: {
      text: "previous answer copied into new turn", menu_ref: "old", evidence_refs: [],
    } },
  });
  const sending = store.send("second request");
  await vi.waitFor(() => expect(createRequest).toHaveBeenCalled());
  await store.cancel();
  const beforeNewId = vi.mocked(cancelRequest).mock.calls.map(([id]) => id);
  const overwritten = store.messages.at(-1)?.content;
  release({ request_id: "new-request", status: "running", created_at: "t" });
  await sending;
  console.log("A01 observed", { beforeNewId, overwritten, allCancelledIds: vi.mocked(cancelRequest).mock.calls });
  expect(beforeNewId).toEqual([]);
  expect(cancelRequest).toHaveBeenCalledWith("new-request");
});

it("A02: failed deferred cancel must establish SSE or polling recovery", async () => {
  let release!: (value: any) => void;
  vi.mocked(createRequest).mockReturnValue(new Promise((resolve) => { release = resolve; }));
  vi.mocked(cancelRequest).mockRejectedValueOnce(new Error("audit network failure"));
  const sending = store.send("first request");
  await vi.waitFor(() => expect(createRequest).toHaveBeenCalled());
  await store.cancel();
  release({ request_id: "new-request", status: "running", created_at: "t" });
  await sending;
  console.log("A02 observed", { isStreaming: store.isStreaming, connection: !!store.connection,
    pollTimer: store.pollTimer, error: store.error });
  expect(!!store.connection || store.pollTimer !== null).toBe(true);
});

function setQuestion() {
  store.activeClarification = question;
  store.messages = [{ id: "question-message", role: "assistant", content: "",
    createdAt: 1, status: "complete", clarification: question }];
}

it("A03: rejected clarification submission must stay retryable", async () => {
  setQuestion();
  vi.mocked(createRequest).mockRejectedValueOnce(new Error("audit network failure"));
  await store.selectOption(1, "question-message");
  console.log("A03 observed", { selectedOptionId: store.messages[0].selectedOptionId,
    activeClarification: store.activeClarification });
  expect(store.messages[0].selectedOptionId).toBeUndefined();
  expect(store.activeClarification?.question_id).toBe(question.question_id);
});

it("A04: clarification 409 must not be reported as a menu version conflict", async () => {
  setQuestion();
  vi.mocked(createRequest).mockRejectedValueOnce(Object.assign(new Error("CLARIFICATION_STALE"), {
    status: 409, body: { error: "CLARIFICATION_STALE" },
  }));
  await store.selectOption(1, "question-message");
  console.log("A04 observed", { error: store.error, selectedOptionId: store.messages[0].selectedOptionId });
  expect(store.error).not.toContain("菜单版本");
});
