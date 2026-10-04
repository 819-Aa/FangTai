import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useRecommendationStore } from "./recommendation";
import { createRequest, getStatus } from "@/api/client";

vi.mock("@/api/client", () => ({
  createSession: vi.fn(), getSession: vi.fn(), cancelRequest: vi.fn(),
  createRequest: vi.fn(), getStatus: vi.fn(),
  subscribeEvents: vi.fn(() => ({ close: vi.fn(), lastEventId: "" })),
}));
let store: ReturnType<typeof useRecommendationStore>;
beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  setActivePinia(createPinia());
  store = useRecommendationStore();
  store.selectedRefs = ["p1"];
  store.sessionRefs = ["p1"];
  store.sessionId = "s";
  vi.mocked(createRequest).mockResolvedValue({ request_id: "r", session_id: "s", status: "accepted", created_at: "t" });
});
afterEach(() => store.closeConnection());

async function pausedRequest() {
  await store.send("今晚三道家常菜");
  const assistantId = store.messages[1].id;
  store.onSseEvent({ id: "recovery-1", event: "request_recovery_required", data: { request_id: "r" } }, assistantId);
  return assistantId;
}

it("pauses recovery and after reload retries the original payload without another turn", async () => {
  const assistantId = await pausedRequest();
  const original = structuredClone(vi.mocked(createRequest).mock.calls[0][0]);
  expect(store.status).toBe("recovery_required");
  expect(store.isStreaming).toBe(false);
  expect(store.canSend).toBe(false);
  expect(store.canRecover).toBe(true);
  const chatId = store.activeChatId;
  setActivePinia(createPinia());
  store = useRecommendationStore();
  vi.mocked(getStatus).mockResolvedValue({ request_id: "r", session_id: "s", status: "recovery_required", created_at: "t" });
  await store.openChat(chatId);
  expect(store.canRecover).toBe(true);
  await store.recoverRequest();
  expect(createRequest).toHaveBeenNthCalledWith(2, original);
  expect(store.messages).toHaveLength(2);
  expect(store.messages[1].id).toBe(assistantId);
  expect(store.requestId).toBe("r");
  expect(store.isStreaming).toBe(true);
  store.onSseEvent({ id: "done", event: "request_terminal", data: { status: "failed" } }, assistantId);
  expect(store.canRecover).toBe(false);
  expect(store.pendingRequest).toBeNull();
});

it("keeps recovery available after network failure and merges double clicks", async () => {
  await pausedRequest();
  let reject!: (reason: Error) => void;
  vi.mocked(createRequest).mockImplementationOnce(() => new Promise((_, no) => { reject = no; }));
  const first = store.recoverRequest();
  const second = store.recoverRequest();
  reject(new Error("connection reset"));
  await Promise.all([first, second]);
  expect(createRequest).toHaveBeenCalledTimes(2);
  expect(store.canRecover).toBe(true);
  expect(store.status).toBe("recovery_required");
});

it("polling recovery stops waiting and exposes the same recovery action", async () => {
  vi.useFakeTimers();
  try {
    await store.send("推荐晚餐");
    vi.mocked(getStatus).mockResolvedValue({ request_id: "r", session_id: "s", status: "recovery_required", created_at: "t" });
    store.startPolling("r", store.messages[1].id, 10);
    await vi.advanceTimersByTimeAsync(10);
    expect(store.isStreaming).toBe(false);
    expect(store.canRecover).toBe(true);
    await vi.advanceTimersByTimeAsync(100);
    expect(getStatus).toHaveBeenCalledTimes(1);
  } finally { vi.useRealTimers(); }
});

it("recovery preserves replacement version fields and the previously committed menu", async () => {
  store.currentMenu = { build_id: "b", plan_id: "old-plan", menu_hash: "a".repeat(64),
    recipe_ids: [101, 202], items: [{ recipe_id: 101, name: "旧菜" }, { recipe_id: 202, name: "保留菜" }] };
  store.startReplaceDish(store.currentMenu.items[0]);
  const previousMenu = JSON.parse(JSON.stringify(store.currentMenu));
  await pausedRequest();
  await store.recoverRequest();
  expect(vi.mocked(createRequest).mock.calls[1][0]).toEqual(vi.mocked(createRequest).mock.calls[0][0]);
  expect(vi.mocked(createRequest).mock.calls[1][0]).toMatchObject({
    action: "replace_dish", target_recipe_id: 101, source_plan_id: "old-plan", source_menu_hash: "a".repeat(64),
  });
  expect(store.currentMenu).toEqual(previousMenu);
});

it("recovery retains the original structured clarification selection", async () => {
  store.activeClarification = { question_id: "q", question_text: "确认口味", options: [{ option_id: 2, text: "清淡" }] };
  store.messages = [{ id: "question", role: "assistant", content: "确认口味", createdAt: 1,
    status: "complete", clarification: store.activeClarification }];
  await store.selectOption(2, "question");
  const assistantId = store.messages[2].id;
  store.onSseEvent({ id: "recover-q", event: "request_recovery_required", data: {} }, assistantId);
  await store.recoverRequest();
  expect(vi.mocked(createRequest).mock.calls[1][0]).toEqual(vi.mocked(createRequest).mock.calls[0][0]);
  expect(vi.mocked(createRequest).mock.calls[1][0].clarification_response).toEqual({ question_id: "q", option_id: 2 });
  expect(store.messages).toHaveLength(3);
});
