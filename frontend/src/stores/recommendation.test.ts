import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  isTerminal,
  refFingerprint,
  reducePhases,
  slotFromRef,
  terminalLabel,
  useRecommendationStore,
} from "./recommendation";
import { createSession } from "@/api/client";

vi.mock("@/api/client", () => ({
  createSession: vi.fn(async (refs: string[]) => ({
    session_id: `sess-${refs.join(",") || "empty"}`,
  })),
  createRequest: vi.fn(async () => ({
    request_id: "r-1", session_id: "sess-mock", status: "accepted",
    created_at: "t",
  })),
  getStatus: vi.fn(async () => ({
    request_id: "r-1", session_id: "sess-mock", status: "running",
    created_at: "t",
  })),
  subscribeEvents: vi.fn(() => ({ close: vi.fn(), lastEventId: "" })),
}));

describe("reducePhases（按 event_id 去重）", () => {
  it("重复 event_id 只保留一份事实", () => {
    const a = { id: "ev_answer_x", event: "answer_ready", data: {} };
    expect(reducePhases([], [a, a])).toHaveLength(1);
  });
  it("追加新事件并保持顺序", () => {
    const a = { id: "ev_answer_x", event: "answer_ready", data: {} };
    const b = { id: "ev_result_x", event: "result_committed", data: {} };
    expect(reducePhases([], [a, b]).map((e) => e.id)).toEqual([
      "ev_answer_x", "ev_result_x",
    ]);
  });
});

describe("terminalLabel（终态独立展示）", () => {
  it("分别展示各终态，禁止统一成普通失败", () => {
    expect(terminalLabel("completed")).toBe("已完成");
    expect(terminalLabel("no_safe_menu")).toBe("无安全菜单");
    expect(terminalLabel("no_feasible_menu")).toBe("无可行菜单");
    expect(terminalLabel("strict_time_indeterminate")).toBe("严格时间约束无法判定");
    expect(terminalLabel("failed")).toBe("处理失败");
    expect(terminalLabel("cancelled")).toBe("已取消");
    expect(terminalLabel("interrupted")).toBe("已中断");
    expect(terminalLabel("needs_clarification")).toBe("待澄清");
    expect(terminalLabel("reconnect")).toBe("连接中断，已恢复");
    expect(terminalLabel("running")).toBe("running");
  });
});

describe("isTerminal", () => {
  it("识别终态与非终态", () => {
    for (const s of [
      "completed", "no_safe_menu", "no_feasible_menu",
      "strict_time_indeterminate", "failed", "cancelled", "interrupted",
    ]) {
      expect(isTerminal(s)).toBe(true);
    }
    expect(isTerminal("running")).toBe(false);
    expect(isTerminal("accepted")).toBe(false);
  });
});

describe("refFingerprint / slotFromRef", () => {
  it("组合指纹与 ref 排序无关", () => {
    expect(refFingerprint(["p2", "p1"])).toBe(refFingerprint(["p1", "p2"]));
  });
  it("槽位只含匿名 ref/label", () => {
    const s = slotFromRef("p1");
    expect(s).toEqual({ participant_ref: "p1", label: "参与者 1" });
  });
});

describe("终态 Reducer（onSseEvent 分支）", () => {
  beforeEach(() => {
    localStorage.clear();
    setActivePinia(createPinia());
  });
  function assistant(store: ReturnType<typeof useRecommendationStore>): string {
    store.messages.push({
      id: "m1", role: "assistant", content: "", createdAt: 1, status: "sending",
    });
    return "m1";
  }
  it("answer_ready 不完成（待最终确认）", () => {
    const store = useRecommendationStore();
    const mid = assistant(store);
    store.isStreaming = true; // send() 中置位
    store.onSseEvent(
      { id: "ev_answer", event: "answer_ready", data: { text: "推荐菜单" } },
      mid,
    );
    expect(store.answer).toBe("推荐菜单");
    expect(store.status).not.toBe("completed");
    expect(store.isStreaming).toBe(true); // 仍等待 result_committed
  });
  it("result_committed 唯一正常完成入口并关闭连接", () => {
    const store = useRecommendationStore();
    const mid = assistant(store);
    store.onSseEvent(
      { id: "ev_answer", event: "answer_ready", data: { text: "推荐菜单" } },
      mid,
    );
    store.onSseEvent(
      { id: "ev_result", event: "result_committed", data: {} },
      mid,
    );
    expect(store.status).toBe("completed");
    expect(store.isStreaming).toBe(false);
    expect(store.connection).toBeNull();
  });
  it("error 分支：显示错误并进入失败状态、关闭连接", () => {
    const store = useRecommendationStore();
    const mid = assistant(store);
    store.onSseEvent(
      { id: "ev_err", event: "error", data: { message: "流程炸了" } },
      mid,
    );
    expect(store.error).toBe("流程炸了");
    expect(store.status).toBe("failed");
    expect(store.isStreaming).toBe(false);
  });
  it("request_cancelled 分支进入 cancelled", () => {
    const store = useRecommendationStore();
    const mid = assistant(store);
    store.onSseEvent({ id: "ev_c", event: "request_cancelled", data: {} }, mid);
    expect(store.status).toBe("cancelled");
    expect(store.isStreaming).toBe(false);
  });
  it("clarification_needed 进入独立待确认状态并展示问题", () => {
    const store = useRecommendationStore();
    const mid = assistant(store);
    store.onSseEvent(
      { id: "ev_cl", event: "clarification_needed", data: { clarification: "请补充忌口" } },
      mid,
    );
    expect(store.status).toBe("needs_clarification");
    expect(store.clarification).toBe("请补充忌口");
    expect(store.isStreaming).toBe(false);
  });
  it("重复 event_id 不重复改变 UI", () => {
    const store = useRecommendationStore();
    const mid = assistant(store);
    const ev = { id: "ev_result", event: "result_committed", data: {} };
    store.onSseEvent(ev, mid);
    expect(store.status).toBe("completed");
    store.onSseEvent(ev, mid);
    expect(store.status).toBe("completed");
    expect(store.isStreaming).toBe(false);
  });
});

describe("SSE 与轮询策略", () => {
  beforeEach(() => {
    localStorage.clear();
    setActivePinia(createPinia());
  });
  it("第 1、2 次失败显示 reconnect 不轮询，第 3 次才以 5 秒间隔轮询", () => {
    const store = useRecommendationStore();
    const poll = vi.spyOn(store, "startPolling").mockImplementation(async () => {});
    store.onSseFailure("r1", "m1", 1);
    expect(store.status).toBe("reconnect");
    expect(poll).not.toHaveBeenCalled();
    store.onSseFailure("r1", "m1", 2);
    expect(poll).not.toHaveBeenCalled();
    store.onSseFailure("r1", "m1", 3);
    expect(poll).toHaveBeenCalledWith("r1", "m1", 5000);
  });
});

describe("session 与参与者生命周期", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
    setActivePinia(createPinia());
  });
  it("并发 ensureSession 只调用一次 createSession", async () => {
    const store = useRecommendationStore();
    const [a, b] = await Promise.all([
      store.ensureSession(["p1"]),
      store.ensureSession(["p1"]),
    ]);
    expect(a).toBe(b);
    expect(createSession).toHaveBeenCalledTimes(1);
  });
  it("刷新复用 session 与参与者组合", () => {
    localStorage.setItem("v2.session_id", "sess-persisted");
    localStorage.setItem("v2.session_refs", JSON.stringify(["p1", "p2"]));
    const store = useRecommendationStore();
    expect(store.sessionId).toBe("sess-persisted");
    expect(store.sessionRefs).toEqual(["p1", "p2"]);
    expect(store.selectedRefs).toEqual(["p1", "p2"]);
    expect(store.slots.map((s) => s.participant_ref)).toEqual(["p1", "p2"]);
  });
  it("组合变化不静默复用旧 session", async () => {
    const store = useRecommendationStore();
    await store.ensureSession(["p1"]);
    const first = store.sessionId;
    await store.ensureSession(["p1", "p2"]);
    expect(store.sessionId).not.toBe(first);
    expect(store.sessionRefs).toEqual(["p1", "p2"]);
  });
  it("removeSlot 同时删除 slots 与 selectedRefs", () => {
    const store = useRecommendationStore();
    store.addSlot();
    store.addSlot();
    store.removeSlot("p1");
    expect(store.slots.map((s) => s.participant_ref)).toEqual(["p2"]);
    expect(store.selectedRefs).toEqual(["p2"]);
  });
  it("addSlot 生成未占用且不超过 p50 的 ref", () => {
    const store = useRecommendationStore();
    const seen = new Set<string>();
    for (let i = 0; i < 50; i++) {
      store.addSlot();
      const ref = store.slots[store.slots.length - 1].participant_ref;
      expect(seen.has(ref)).toBe(false);
      expect(/^p([1-9]|[1-4][0-9]|50)$/.test(ref)).toBe(true);
      seen.add(ref);
    }
    store.addSlot(); // 第 51 个 → 上限
    expect(store.error).toContain("上限");
  });
});
