import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useRecommendationStore, isTerminal, reducePhases, terminalLabel } from "./recommendation";

vi.mock("@/api/client", () => ({
  createSession: vi.fn(async () => ({ session_id: "sess-mock" })),
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
    const r1 = reducePhases([], [a]);
    const r2 = reducePhases(r1, [a]);
    expect(r2).toHaveLength(1);
  });
  it("追加新事件并保持顺序", () => {
    const a = { id: "ev_answer_x", event: "answer_ready", data: {} };
    const b = { id: "ev_result_x", event: "result_committed", data: {} };
    const out = reducePhases([], [a, b]).map((e) => e.id);
    expect(out).toEqual(["ev_answer_x", "ev_result_x"]);
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

describe("session_id 持续复用", () => {
  beforeEach(() => {
    localStorage.clear();
    setActivePinia(createPinia());
  });
  it("无会话时创建一次并持久化，重复调用复用", async () => {
    const store = useRecommendationStore();
    expect(store.sessionId).toBe("");
    const sid = await store.ensureSession();
    expect(sid).toBe("sess-mock");
    expect(store.sessionId).toBe("sess-mock");
    expect(localStorage.getItem("v2.session_id")).toBe("sess-mock");
    const again = await store.ensureSession();
    expect(again).toBe("sess-mock");
  });
  it("刷新/新实例复用已持久化 session，不创建无关新会话", () => {
    localStorage.setItem("v2.session_id", "sess-persisted");
    const store = useRecommendationStore();
    expect(store.sessionId).toBe("sess-persisted");
  });
});
