import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import ChatPanel from "./ChatPanel.vue";

describe("ChatPanel（回答正文显示）", () => {
  it("渲染 assistant message 的回答正文（answer_ready 写入 content）", () => {
    const wrapper = mount(ChatPanel, {
      props: {
        messages: [
          {
            id: "user-1", role: "user", content: "推荐菜单",
            createdAt: 1, status: "complete",
          },
          {
            id: "assistant-1", role: "assistant", content: "为您推荐三道家常菜",
            createdAt: 2, status: "sending",
          },
        ],
        phases: [{
          id: "ev_answer", event: "answer_ready",
          data: { text: "为您推荐三道家常菜" },
        }],
        answer: "为您推荐三道家常菜",
        clarification: "",
        status: "",
        isStreaming: true,
        canSend: false,
        participants: [],
      },
    });
    expect(wrapper.text()).toContain("为您推荐三道家常菜");
    expect(wrapper.text()).toContain("待最终确认");
  });

  it("result_committed 后显示已完成徽标且正文仍在", () => {
    const wrapper = mount(ChatPanel, {
      props: {
        messages: [
          {
            id: "assistant-1", role: "assistant", content: "为您推荐三道家常菜",
            createdAt: 1, status: "complete",
          },
        ],
        phases: [
          { id: "ev_answer", event: "answer_ready", data: { text: "为您推荐三道家常菜" } },
          { id: "ev_result", event: "result_committed", data: {} },
        ],
        answer: "为您推荐三道家常菜",
        clarification: "",
        status: "completed",
        isStreaming: false,
        canSend: true,
        participants: [],
      },
    });
    expect(wrapper.text()).toContain("为您推荐三道家常菜");
    expect(wrapper.text()).toContain("菜单已生成");
  });
});
