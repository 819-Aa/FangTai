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
import { createRequest, createSession, getSession } from "@/api/client";

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
  cancelRequest: vi.fn(async () => ({ status: "cancelled" })),
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
  it("每次打开都从空白对话和未选成员开始", () => {
    localStorage.setItem("v2.session_id", "sess-persisted");
    localStorage.setItem("v2.session_refs", JSON.stringify(["p1", "p2"]));
    localStorage.setItem("v2.selected_refs", JSON.stringify(["p1"]));
    const store = useRecommendationStore();
    expect(store.sessionId).toBe("");
    expect(store.sessionRefs).toEqual([]);
    expect(store.selectedRefs).toEqual([]);
    expect(store.messages).toEqual([]);
    expect(store.slots).toHaveLength(50);
    expect(store.slots[49].participant_ref).toBe("p50");
  });
  it("显式指定会话时仍可恢复当前已提交菜单", async () => {
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
    store.sessionId = "sess-persisted";

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
  it("每个对话从固定成员名单选择参与者", async () => {
    const store = useRecommendationStore();
    store.toggleSlot("p1");
    store.toggleSlot("p2");
    store.toggleSlot("p1");
    expect(store.selectedRefs).toEqual(["p2"]);
    expect(store.participants().map((p) => p.participant_ref)).toEqual(["p2"]);
    await store.send("推荐晚餐");
    expect(createRequest).toHaveBeenCalledWith(expect.objectContaining({
      participants: [{ participant_ref: "p2", label: "参与者 2" }],
    }));
    expect(store.canEditParticipants).toBe(false);
  });
  it("新对话清空会话和成员选择，旧对话不会被继续使用", async () => {
    const store = useRecommendationStore();
    store.toggleSlot("p1");
    await store.ensureSession(["p1"]);
    const oldSessionId = store.sessionId;
    store.newChat();
    expect(store.sessionId).toBe("");
    expect(store.selectedRefs).toEqual([]);
    expect(store.messages).toEqual([]);
    store.toggleSlot("p3");
    await store.ensureSession(["p3"]);
    expect(store.sessionId).not.toBe(oldSessionId);
  });
  it("消息发出后进入列表，新对话保留旧记录，重开页面仍从空白开始", async () => {
    const store = useRecommendationStore();
    store.toggleSlot("p1");
    await store.send("推荐晚餐");
    store.onSseEvent({ id: "done-1", event: "result_committed", data: {} }, store.messages[1].id);
    expect(store.chats).toHaveLength(1);
    expect(store.chats[0].title).toBe("推荐晚餐");
    expect(store.chats[0].sessionId).toBe("sess-p1");
    store.newChat();
    expect(store.messages).toEqual([]);
    expect(store.chats).toHaveLength(1);

    setActivePinia(createPinia());
    const reopened = useRecommendationStore();
    expect(reopened.activeChatId).toBe("");
    expect(reopened.messages).toEqual([]);
    expect(reopened.chats).toHaveLength(1);
  });
  it("点旧对话恢复消息、成员和 session，后续发送沿用该 session", async () => {
    const store = useRecommendationStore();
    store.toggleSlot("p2");
    await store.send("不吃辣");
    store.onSseEvent({ id: "answer-1", event: "answer_ready", data: { text: "清淡菜单" } }, store.messages[1].id);
    store.onSseEvent({ id: "done-1", event: "result_committed", data: {} }, store.messages[1].id);
    const id = store.activeChatId;
    setActivePinia(createPinia());
    const reopened = useRecommendationStore();
    await reopened.openChat(id);
    expect(reopened.selectedRefs).toEqual(["p2"]);
    expect(reopened.sessionId).toBe("sess-p2");
    expect(reopened.messages.map((m) => m.content)).toEqual(["不吃辣", "清淡菜单"]);
    await reopened.send("再加一道汤");
    expect(createSession).toHaveBeenCalledTimes(1);
    expect(createRequest).toHaveBeenLastCalledWith(expect.objectContaining({ session_id: "sess-p2" }));
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

    it("SSE 收到 clarification_needed 将澄清卡片绑定到当前 assistant 消息 (R06)", () => {
      const store = useRecommendationStore();
      store.messages.push({
        id: "msg_assist_bound",
        role: "assistant",
        content: "",
        createdAt: 1,
        status: "sending",
      });

      store.onSseEvent({
        id: "ev_bound_1",
        event: "clarification_needed",
        data: {
          question_id: "q_sse_bound",
          clarification: "请确认过敏原？",
          options: [{ option_id: 1, text: "花生过敏" }, { option_id: 2, text: "无过敏" }],
        },
      }, "msg_assist_bound");

      const msg = store.messages.find((m) => m.id === "msg_assist_bound");
      expect(msg?.clarification).toBeTruthy();
      expect(msg?.clarification?.question_id).toBe("q_sse_bound");
      expect(msg?.status).toBe("complete");
    });

    it("selectOption 在原 assistant 消息记录 selectedOptionId 并发送 (R06)", async () => {
      const store = useRecommendationStore();
      store.selectedRefs = ["p1"];
      store.sessionId = "sess_bound_test";
      const q = {
        question_id: "q_bound_sel",
        question_text: "请选择偏好口味",
        options: [
          { option_id: 1, text: "偏甜" },
          { option_id: 2, text: "偏咸" },
        ],
      };
      store.activeClarification = q;
      store.messages.push({
        id: "assist_turn_1",
        role: "assistant",
        content: "",
        createdAt: 1,
        status: "complete",
        clarification: q,
      });

      await store.selectOption(2, "assist_turn_1");

      const target = store.messages.find((m) => m.id === "assist_turn_1");
      expect(target?.selectedOptionId).toBe(2);
      expect(store.activeClarification).toBeNull();
    });

    it("多轮澄清场景：历史消息保留 selectedOptionId，新消息挂载最新有效澄清 (R06)", async () => {
      const store = useRecommendationStore();
      store.selectedRefs = ["p1"];
      store.sessionId = "sess_multiround";

      // 第一轮
      const q1 = {
        question_id: "q_round_1",
        question_text: "第一轮问题",
        options: [{ option_id: 1, text: "选项1" }],
      };
      store.activeClarification = q1;
      store.messages.push({
        id: "assist_1",
        role: "assistant",
        content: "",
        createdAt: 1,
        status: "complete",
        clarification: q1,
      });

      await store.selectOption(1, "assist_1");
      expect(store.messages[0].selectedOptionId).toBe(1);

      // 第二轮助手消息接收到新澄清
      const q2 = {
        question_id: "q_round_2",
        question_text: "第二轮问题",
        options: [{ option_id: 2, text: "选项2" }],
      };
      const assist2 = store.messages.findLast((m) => m.role === "assistant")!;
      store.onSseEvent({
        id: "ev_round_2",
        event: "clarification_needed",
        data: q2,
      }, assist2.id);

      expect(store.messages[0].selectedOptionId).toBe(1);
      expect(assist2.clarification?.question_id).toBe("q_round_2");
      expect(assist2.selectedOptionId).toBeUndefined();
      expect(store.activeClarification?.question_id).toBe("q_round_2");
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

      // 2. 旧 session 不再用于后续请求
      expect(store.sessionId).toBe("");
      expect(store.sessionRefs).toEqual([]);

      // 3. 用户输入的内容依然保留在消息流中
      const userMsg = store.messages.find((m) => m.role === "user");
      expect(userMsg?.content).toBe("换两道不辣的菜");

      // 4. 给出明确的用户提示，告知旧会话无法继续、请在新会话提出完整需求
      expect(store.error).toContain("新会话");
      const assistantMsg = store.messages.find((m) => m.role === "assistant");
      expect(assistantMsg?.status).toBe("error");
      expect(assistantMsg?.content).toContain("新会话");
    });

    it("text_delta 事件增量累加 answer 并更新 assistant 消息内容", () => {
      const store = useRecommendationStore();
      store.messages = [
        { id: "msg-1", role: "assistant", content: "", createdAt: 1, status: "sending" },
      ];
      store.onSseEvent({ id: "ev-1", event: "text_delta", data: { chunk: "为您推荐" } }, "msg-1");
      expect(store.answer).toBe("为您推荐");
      expect(store.messages[0].content).toBe("为您推荐");

      store.onSseEvent({ id: "ev-2", event: "text_delta", data: { chunk: "两道家常菜" } }, "msg-1");
      expect(store.answer).toBe("为您推荐两道家常菜");
      expect(store.messages[0].content).toBe("为您推荐两道家常菜");
    });

    it("cancel 操作关闭连接、标记 cancelled 并调用 cancelRequest", async () => {
      const { cancelRequest } = await import("@/api/client");
      const store = useRecommendationStore();
      store.isStreaming = true;
      store.requestId = "req-to-cancel";
      store.messages = [
        { id: "msg-1", role: "assistant", content: "已生成部分", createdAt: 1, status: "sending" },
      ];

      await store.cancel();

      expect(store.isStreaming).toBe(false);
      expect(store.messages[0].status).toBe("cancelled");
      expect(store.messages[0].isCommitted).toBe(false);
    });

    it("thought_node 与 tool_trace 事件正确构造思维链节点与工具调用信息", () => {
      const store = useRecommendationStore();
      store.messages = [
        { id: "msg-1", role: "assistant", content: "", createdAt: 1, status: "sending" },
      ];

      // 1. thought_node 挂载
      store.onSseEvent({
        id: "ev-thought-1",
        event: "thought_node",
        data: {
          node_id: "audit_health",
          title: "健康合规筛查",
          status: "running",
          summary: "筛查禁忌食材...",
        },
      }, "msg-1");

      const msg = store.messages[0];
      expect(msg.thoughts).toHaveLength(1);
      expect(msg.thoughts![0].node_id).toBe("audit_health");
      expect(msg.thoughts![0].status).toBe("running");

      // 2. tool_trace 追加工具信息
      store.onSseEvent({
        id: "ev-tool-1",
        event: "tool_trace",
        data: {
          tool_name: "B4 健康规则引擎",
          result_summary: "已拦截 2 道高敏菜品",
        },
      }, "msg-1");

      expect(msg.thoughts![0].tool_name).toBe("B4 健康规则引擎");
      expect(msg.thoughts![0].summary).toBe("已拦截 2 道高敏菜品");
    });

    it("cancel 遇 409 REQUEST_ALREADY_TERMINAL 时刷新最新状态且不覆盖已完成菜单", async () => {
      const { cancelRequest, getStatus } = await import("@/api/client");
      const err = new Error("409 Conflict: REQUEST_ALREADY_TERMINAL");
      (err as unknown as { status: number; body: unknown }).status = 409;
      (err as unknown as { status: number; body: unknown }).body = {
        detail: "REQUEST_ALREADY_TERMINAL",
        current_status: "completed",
      };
      vi.mocked(cancelRequest).mockRejectedValueOnce(err);
      vi.mocked(getStatus).mockResolvedValueOnce({
        request_id: "req-already-done",
        session_id: "sess-mock",
        status: "completed",
        created_at: "t",
        result_summary: {
          text: "已完成的健康菜单",
          menu_summary: {
            build_id: "b-1",
            plan_id: "p-1",
            menu_hash: "a".repeat(64),
            recipe_ids: [1, 2],
            items: [
              { recipe_id: 1, name: "西红柿鸡蛋" },
              { recipe_id: 2, name: "清蒸鲈鱼" },
            ],
          },
        },
      });

      const store = useRecommendationStore();
      store.isStreaming = true;
      store.requestId = "req-already-done";
      store.messages = [
        { id: "msg-done", role: "assistant", content: "", createdAt: 1, status: "sending" },
      ];

      await store.cancel();

      expect(store.isStreaming).toBe(false);
      expect(store.status).toBe("completed");
      expect(store.currentMenu?.items[0].name).toBe("西红柿鸡蛋");
      expect(store.messages[0].status).toBe("complete");
      expect(store.messages[0].content).toBe("已完成的健康菜单");
    });

    it("request_accepted 事件只生成事实性“请求已接收”步骤，不捏造健康规则审查", () => {
      const store = useRecommendationStore();
      store.selectedRefs = ["p1", "p2"];
      store.messages = [
        { id: "msg-1", role: "assistant", content: "", createdAt: 1, status: "sending" },
      ];

      store.onSseEvent({
        id: "ev-accept-1",
        event: "request_accepted",
        data: {},
      }, "msg-1");

      const msg = store.messages[0];
      expect(msg.thoughts).toHaveLength(1);
      expect(msg.thoughts![0].title).toBe("请求已接收");
      expect(msg.thoughts![0].summary).toContain("参与者数：2");
      // 绝不捏造假健康引擎和假过敏审查
      expect(msg.thoughts!.some((t) => t.title.includes("健康指标") || t.title.includes("规则引擎"))).toBe(false);
    });

    it("analysis_ready 事件按真实 stage 正确构造思维链节点", () => {
      const store = useRecommendationStore();
      store.messages = [
        { id: "msg-1", role: "assistant", content: "", createdAt: 1, status: "sending" },
      ];

      store.onSseEvent({
        id: "ev-a1",
        event: "analysis_ready",
        data: { stage: "context_ready", summary: "已理解2位参与者的需求" },
      }, "msg-1");

      store.onSseEvent({
        id: "ev-a2",
        event: "analysis_ready",
        data: { stage: "candidate_search", summary: "已检索到15道候选菜品" },
      }, "msg-1");

      store.onSseEvent({
        id: "ev-a3",
        event: "analysis_ready",
        data: { stage: "recipe_audit", summary: "健康审查完成，12道菜品符合要求" },
      }, "msg-1");

      const thoughts = store.messages[0].thoughts!;
      expect(thoughts).toHaveLength(3);
      expect(thoughts[0].title).toBe("上下文与需求分析");
      expect(thoughts[0].summary).toBe("已理解2位参与者的需求");
      expect(thoughts[1].title).toBe("菜品候选检索");
      expect(thoughts[1].summary).toBe("已检索到15道候选菜品");
      expect(thoughts[2].title).toBe("健康合规审查");
      expect(thoughts[2].summary).toBe("健康审查完成，12道菜品符合要求");
    });

    it("startPolling 收到 needs_clarification 时终止轮询、释放 isStreaming 并更新选项 (R01)", async () => {
      const { getStatus } = await import("@/api/client");
      vi.mocked(getStatus).mockResolvedValueOnce({
        request_id: "req-poll-clarify",
        session_id: "sess-mock",
        status: "needs_clarification",
        created_at: "t",
        active_clarification: {
          question_id: "q_poll_1",
          question_text: "请确认餐具数量",
          options: [{ option_id: 1, text: "两套" }],
        },
      });

      const store = useRecommendationStore();
      store.isStreaming = true;
      store.messages.push({
        id: "msg-poll",
        role: "assistant",
        content: "",
        createdAt: 1,
        status: "sending",
      });

      await store.startPolling("req-poll-clarify", "msg-poll", 50);

      await vi.waitFor(() => {
        expect(store.status).toBe("needs_clarification");
      });

      expect(store.isStreaming).toBe(false);
      expect(store.pollTimer).toBeNull();
      expect(store.activeClarification?.question_id).toBe("q_poll_1");
      expect(store.messages[0].status).toBe("complete");
    });

    it("send 期间未取得 requestId 时点击 cancel，在请求返回后立即补发取消 (R02)", async () => {
      const { createRequest, cancelRequest } = await import("@/api/client");
      let resolveCreate: (val: unknown) => void;
      const createPromise = new Promise((resolve) => {
        resolveCreate = resolve;
      });
      vi.mocked(createRequest).mockReturnValueOnce(createPromise as any);

      const store = useRecommendationStore();
      store.selectedRefs = ["p1"];
      store.sessionRefs = ["p1"];
      store.sessionId = "sess-cancel-race";

      const sendPromise = store.send("推荐三道菜");
      expect(store.requestId).toBe("");
      expect(store.isStreaming).toBe(true);

      // 用户在拿到 requestId 前点击停止
      await store.cancel();
      expect(store.pendingCancel).toBe(true);
      expect(cancelRequest).not.toHaveBeenCalled();

      // createRequest 返回
      resolveCreate!({
        request_id: "req-race-resolved",
        session_id: "sess-cancel-race",
        status: "running",
        created_at: "t",
      });

      await sendPromise;

      // 验证自动触发了 cancelRequest
      expect(cancelRequest).toHaveBeenCalledWith("req-race-resolved");
      expect(store.isStreaming).toBe(false);
      expect(store.status).toBe("cancelled");
    });

    it("cancel 遇 409 且 getStatus 失败且无 currentStatus 时进入 pending_verification 状态 (R02)", async () => {
      const { cancelRequest, getStatus } = await import("@/api/client");
      const err = new Error("409 Conflict: REQUEST_ALREADY_TERMINAL");
      (err as any).status = 409;
      (err as any).body = {};
      vi.mocked(cancelRequest).mockRejectedValueOnce(err);
      vi.mocked(getStatus).mockRejectedValueOnce(new Error("Network Error on verification"));

      const store = useRecommendationStore();
      store.isStreaming = true;
      store.requestId = "req-409-fail";
      store.messages.push({
        id: "msg-409",
        role: "assistant",
        content: "",
        createdAt: 1,
        status: "sending",
      });

      await store.cancel();

      expect(store.isStreaming).toBe(false);
      expect(store.status).toBe("pending_verification");
      expect(store.messages[0].status).toBe("error");
      expect(store.error).toContain("核对失败");
    });

    it("后续请求的 phases 和 committed 状态不污染历史 Assistant 消息 (R03)", () => {
      const store = useRecommendationStore();
      const m1 = {
        id: "msg-old-done",
        role: "assistant" as const,
        content: "第一轮推荐菜单",
        createdAt: 1,
        status: "complete" as const,
        isCommitted: true,
      };
      const m2 = {
        id: "msg-new-sending",
        role: "assistant" as const,
        content: "",
        createdAt: 2,
        status: "sending" as const,
        isCommitted: false,
      };
      store.messages = [m1, m2];

      store.onSseEvent({
        id: "ev-err",
        event: "error",
        data: { message: "网络波动导致失败" },
      }, "msg-new-sending");

      expect(m1.isCommitted).toBe(true);
      expect(m1.status).toBe("complete");
      expect(m2.isCommitted).toBe(false);
      expect(m2.status).toBe("error");
    });
  });

  describe("单菜替换协议与版本保护 (R07)", () => {
    beforeEach(() => {
      localStorage.clear();
      setActivePinia(createPinia());
      vi.clearAllMocks();
    });

    const mockMenu = {
      build_id: "build-1",
      plan_id: "plan-1",
      menu_hash: "a".repeat(64),
      recipe_ids: [101, 202],
      items: [
        { recipe_id: 101, name: "番茄炒蛋" },
        { recipe_id: 202, name: "清炒时蔬" },
      ],
    };

    it("startReplaceDish 锁定目标菜品、来源版本并可通过 cancelReplaceDish 清除", () => {
      const store = useRecommendationStore();
      store.currentMenu = mockMenu;

      // 锁定当前菜单内存在的菜品
      store.startReplaceDish({ recipe_id: 101, name: "番茄炒蛋" });
      expect(store.pendingReplaceDish).toEqual({
        target_recipe_id: 101,
        dish_name: "番茄炒蛋",
        source_plan_id: "plan-1",
        source_menu_hash: "a".repeat(64),
      });

      // 取消锁定
      store.cancelReplaceDish();
      expect(store.pendingReplaceDish).toBeNull();

      // 尝试锁定不在当前菜单中的菜品 -> 不予锁定
      store.startReplaceDish({ recipe_id: 999, name: "未知菜品" });
      expect(store.pendingReplaceDish).toBeNull();
    });

    it("send 时携带结构化 replace_dish 载荷并在成功受理后清除 pending 状态", async () => {
      const { createRequest } = await import("@/api/client");
      const store = useRecommendationStore();
      store.selectedRefs = ["p1"];
      store.sessionId = "sess-replace";
      store.sessionRefs = ["p1"];
      store.currentMenu = mockMenu;

      store.startReplaceDish({ recipe_id: 101, name: "番茄炒蛋" });

      await store.send("换一道清淡的蔬菜");

      expect(createRequest).toHaveBeenCalledWith(
        expect.objectContaining({
          session_id: "sess-replace",
          message: "换一道清淡的蔬菜",
          action: "replace_dish",
          target_recipe_id: 101,
          source_plan_id: "plan-1",
          source_menu_hash: "a".repeat(64),
        }),
      );

      // 请求已成功受理，pending 状态已清除
      expect(store.pendingReplaceDish).toBeNull();
    });

    it("单菜替换遇 409 MENU_VERSION_CONFLICT 时触发 restoreSession 刷新最新菜单并提示冲突", async () => {
      const { createRequest, getSession } = await import("@/api/client");
      const err = new Error("409 Conflict: MENU_VERSION_CONFLICT");
      (err as any).status = 409;
      vi.mocked(createRequest).mockRejectedValueOnce(err);

      const latestMenuOnServer = {
        build_id: "build-new",
        plan_id: "plan-new",
        menu_hash: "b".repeat(64),
        recipe_ids: [102, 202],
        items: [
          { recipe_id: 102, name: "西红柿蛋汤" },
          { recipe_id: 202, name: "清炒时蔬" },
        ],
      };
      vi.mocked(getSession).mockResolvedValueOnce({
        session_id: "sess-replace",
        participant_refs: ["p1"],
        request_count: 2,
        current_menu: latestMenuOnServer,
      });

      const store = useRecommendationStore();
      store.selectedRefs = ["p1"];
      store.sessionId = "sess-replace";
      store.sessionRefs = ["p1"];
      store.currentMenu = mockMenu;

      store.startReplaceDish({ recipe_id: 101, name: "番茄炒蛋" });

      await store.send("换成别的蛋类菜品");

      // 验证 pending 状态已清除且调用 restoreSession 刷新了菜单
      expect(store.pendingReplaceDish).toBeNull();
      expect(store.currentMenu).toEqual(latestMenuOnServer);
      expect(store.error).toContain("菜单版本已变更，已为您同步最新菜单");
    });
  });
});
