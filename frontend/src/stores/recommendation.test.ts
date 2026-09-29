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
import { createSession, getSession } from "@/api/client";

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
  getSession: vi.fn(async () => ({
    session_id: "sess-mock",
    participant_refs: ["p1"],
    request_count: 1,
    current_menu: null,
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
    expect(terminalLabel("reconnect")).toBe("连接中断，正在重连");
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
  it("answer_ready 不完成（待最终确认），正文写入 message.content", () => {
    const store = useRecommendationStore();
    const mid = assistant(store);
    store.isStreaming = true; // send() 中置位
    store.onSseEvent(
      { id: "ev_answer", event: "answer_ready", data: { text: "推荐菜单" } },
      mid,
    );
    expect(store.answer).toBe("推荐菜单");
    const msg = store.messages.find((m) => m.id === mid);
    expect(msg?.content).toBe("推荐菜单"); // 正文已写入 message
    expect(msg?.status).toBe("sending"); // 保持待最终确认
    expect(store.status).not.toBe("completed");
    expect(store.isStreaming).toBe(true); // 仍等待 result_committed
  });
  it("result_committed 只标 complete，不覆盖正文", () => {
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
    const msg = store.messages.find((m) => m.id === mid);
    expect(msg?.content).toBe("推荐菜单"); // 正文未被覆盖
    expect(msg?.status).toBe("complete");
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
  it("result_committed 只消费后端结构化菜单，不解析回答正文", () => {
    const store = useRecommendationStore();
    const mid = assistant(store);
    store.onSseEvent({
      id: "ev_result_menu",
      event: "result_committed",
      data: {
        menu_summary: {
          build_id: "build-1",
          plan_id: "plan-1",
          menu_hash: "a".repeat(64),
          recipe_ids: [101, 202],
          items: [
            { recipe_id: 101, name: "番茄炒蛋" },
            { recipe_id: 202, name: "清炒时蔬" },
          ],
        },
      },
    }, mid);
    expect(store.currentMenu?.items.map((item) => item.name)).toEqual([
      "番茄炒蛋", "清炒时蔬",
    ]);
  });
  it("非法菜单身份不进入 UI", () => {
    const store = useRecommendationStore();
    const mid = assistant(store);
    store.onSseEvent({
      id: "ev_bad_menu",
      event: "result_committed",
      data: {
        menu_summary: {
          build_id: "build-1", plan_id: "plan-1", menu_hash: "x",
          recipe_ids: [101], items: [{ recipe_id: 999, name: "错菜" }],
        },
      },
    }, mid);
    expect(store.currentMenu).toBeNull();
  });
  it("轮询完成响应恢复回答正文与结构化菜单", () => {
    const store = useRecommendationStore();
    const mid = assistant(store);
    store.applyResultSummary({
      request_id: "r-1",
      session_id: "sess-1",
      status: "completed",
      created_at: "t",
      result_summary: {
        status: "completed",
        answer: { text: "轮询恢复的回答", menu_ref: "menu:1", evidence_refs: [] },
        menu_summary: {
          build_id: "build-1", plan_id: "plan-1", menu_hash: "c".repeat(64),
          recipe_ids: [303], items: [{ recipe_id: 303, name: "轮询恢复菜品" }],
        },
      },
    }, mid);
    expect(store.answer).toBe("轮询恢复的回答");
    expect(store.messages.find((message) => message.id === mid)?.content)
      .toBe("轮询恢复的回答");
    expect(store.currentMenu?.items[0].name).toBe("轮询恢复菜品");
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
  it("request_terminal 各业务终态分别展示并立即关闭连接", () => {
    for (const status of ["no_safe_menu", "no_feasible_menu",
                           "strict_time_indeterminate", "failed", "interrupted"]) {
      setActivePinia(createPinia());
      localStorage.clear();
      const store = useRecommendationStore();
      const mid = assistant(store);
      store.onSseEvent(
        { id: `ev_term_${status}`, event: "request_terminal", data: { status } },
        mid,
      );
      expect(store.status).toBe(status);
      expect(store.isStreaming).toBe(false);
    }
  });
  it("收到新事件后从 reconnect 恢复 running", () => {
    const store = useRecommendationStore();
    store.status = "reconnect";
    store.onSseEvent(
      { id: "ev_an", event: "analysis_ready", data: { stage: "x", summary: "y" } },
      "m1",
    );
    expect(store.status).toBe("running"); // 未停留在 reconnect/已恢复
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
  it("刷新后从 session 恢复当前已提交菜单", async () => {
    localStorage.setItem("v2.session_id", "sess-persisted");
    localStorage.setItem("v2.session_refs", JSON.stringify(["p1"]));
    vi.mocked(getSession).mockResolvedValueOnce({
      session_id: "sess-persisted",
      participant_refs: ["p1"],
      request_count: 1,
      current_menu: {
        build_id: "build-1",
        plan_id: "plan-1",
        menu_hash: "b".repeat(64),
        recipe_ids: [101],
        items: [{ recipe_id: 101, name: "恢复菜品" }],
      },
    });
    const store = useRecommendationStore();

    await store.restoreSession();

    expect(store.currentMenu?.items[0].name).toBe("恢复菜品");
    expect(store.status).toBe("completed");
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

  describe("结构化澄清响应与恢复（Task 6 契约）", () => {
    it("SSE 收到 clarification_needed 能够解析并保存 activeClarification", () => {
      const store = useRecommendationStore();
      store.messages.push({
        id: "m_assist", role: "assistant", content: "", createdAt: 1, status: "sending",
      });

      store.onSseEvent({
        id: "ev_clarify_1",
        event: "clarification_needed",
        data: {
          question_id: "q_sse_1",
          clarification: "请问需要调整几个菜？",
          options: [
            { option_id: 1, text: "2道菜" },
            { option_id: 2, text: "3道菜" },
          ],
        },
      }, "m_assist");

      expect(store.status).toBe("needs_clarification");
      expect(store.activeClarification).toBeTruthy();
      expect(store.activeClarification?.question_id).toBe("q_sse_1");
      expect(store.activeClarification?.options).toHaveLength(2);
      expect(store.activeClarification?.options[0]).toEqual({ option_id: 1, text: "2道菜" });
    });

    it("点击选项触发 selectOption 发送结构化 clarification_response", async () => {
      const { createRequest } = await import("@/api/client");
      const store = useRecommendationStore();
      store.selectedRefs = ["p1"];
      store.sessionId = "sess_clarify_test";
      store.activeClarification = {
        question_id: "q_click_1",
        question_text: "请选择辣度",
        options: [
          { option_id: 1, text: "不辣" },
          { option_id: 2, text: "微辣" },
        ],
      };

      await store.selectOption(1);

      expect(createRequest).toHaveBeenCalledWith(expect.objectContaining({
        clarification_response: {
          question_id: "q_click_1",
          option_id: 1,
        },
        message: "不辣",
      }));
    });

    it("普通文本输入“选第一个”不生成结构化 clarification_response", async () => {
      const { createRequest } = await import("@/api/client");
      const store = useRecommendationStore();
      store.selectedRefs = ["p1"];
      store.sessionId = "sess_clarify_test";
      store.activeClarification = {
        question_id: "q_click_1",
        question_text: "请选择辣度",
        options: [{ option_id: 1, text: "不辣" }],
      };

      await store.send("选第一个");

      expect(createRequest).toHaveBeenCalledWith(expect.objectContaining({
        message: "选第一个",
        clarification_response: undefined,
      }));
    });

    it("CLARIFICATION_ALREADY_APPLIED/STALE/EXPIRED 触发刷新最新问题且不自动重试", async () => {
      const { createRequest, getSession } = await import("@/api/client");
      vi.mocked(createRequest).mockRejectedValueOnce(new Error("CLARIFICATION_ALREADY_APPLIED: 该澄清选项已提交"));
      vi.mocked(getSession).mockResolvedValueOnce({
        session_id: "sess_refresh_test",
        participant_refs: ["p1"],
        request_count: 2,
        current_menu: null,
        active_clarification: {
          question_id: "q_new_active",
          question_text: "新问题：请确认餐具",
          options: [{ option_id: 1, text: "无需餐具" }],
        },
      });

      const store = useRecommendationStore();
      store.selectedRefs = ["p1"];
      store.sessionRefs = ["p1"];
      store.sessionId = "sess_refresh_test";
      store.activeClarification = {
        question_id: "q_old_stale",
        question_text: "旧问题",
        options: [{ option_id: 1, text: "旧选项" }],
      };

      await store.selectOption(1);

      // 不自动重试，createRequest 仅调用一次
      expect(createRequest).toHaveBeenCalledTimes(1);
      // 调用 getSession 刷新了最新问题
      expect(getSession).toHaveBeenCalledWith("sess_refresh_test");
      expect(store.activeClarification?.question_id).toBe("q_new_active");
      expect(store.activeClarification?.question_text).toBe("新问题：请确认餐具");
    });

    it("restoreSession 从服务端会话恢复 activeClarification", async () => {
      const { getSession } = await import("@/api/client");
      vi.mocked(getSession).mockResolvedValueOnce({
        session_id: "sess_restore_test",
        participant_refs: ["p1"],
        request_count: 1,
        current_menu: null,
        active_clarification: {
          question_id: "q_restored",
          question_text: "已恢复的问题",
          options: [{ option_id: 1, text: "选项A" }],
        },
      });

      const store = useRecommendationStore();
      store.sessionId = "sess_restore_test";

      await store.restoreSession();

      expect(store.activeClarification).toBeTruthy();
      expect(store.activeClarification?.question_id).toBe("q_restored");
      expect(store.status).toBe("needs_clarification");
    });

    it("SESSION_PROTOCOL_UNSUPPORTED 清除旧 session 状态，提示用户在新会话提出完整需求且不自动重发", async () => {
      const { createRequest } = await import("@/api/client");
      vi.mocked(createRequest).mockRejectedValueOnce(new Error("409 SESSION_PROTOCOL_UNSUPPORTED: 该会话协议为旧版本，不再支持推荐生成，请创建新会话"));

      const store = useRecommendationStore();
      store.selectedRefs = ["p1"];
      store.sessionRefs = ["p1"];
      store.sessionId = "sess_legacy_old";
      localStorage.setItem("v2.session_id", "sess_legacy_old");
      localStorage.setItem("v2.session_refs", JSON.stringify(["p1"]));

      await store.send("换两道不辣的菜");

      // 1. 不自动重发：createRequest 仅被调用 1 次
      expect(createRequest).toHaveBeenCalledTimes(1);

      // 2. store 与 localStorage 中的旧 session 被清空
      expect(store.sessionId).toBe("");
      expect(localStorage.getItem("v2.session_id")).toBe("");

      // 3. 用户输入的内容依然保留在消息流中
      const userMsg = store.messages.find((m) => m.role === "user");
      expect(userMsg?.content).toBe("换两道不辣的菜");

      // 4. 给出明确的用户提示，告知旧会话无法继续、请在新会话提出完整需求
      expect(store.error).toContain("新会话");
      const assistantMsg = store.messages.find((m) => m.role === "assistant");
      expect(assistantMsg?.status).toBe("error");
      expect(assistantMsg?.content).toContain("新会话");
    });
  });
});
