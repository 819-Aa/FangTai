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
        currentMenu: null,
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
        currentMenu: {
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
    });
    expect(wrapper.text()).toContain("为您推荐三道家常菜");
    expect(wrapper.text()).toContain("菜单已生成");
    expect(wrapper.get("[data-testid='committed-menu']").text()).toContain("番茄炒蛋");
    expect(wrapper.get("[data-testid='committed-menu']").text()).toContain("清炒时蔬");
  });

  describe("ChatPanel（结构化澄清选项展示与交互）", () => {
    it("渲染结构化澄清问题和选项按钮，点击触发 selectOption 事件", async () => {
      const activeClarification = {
        question_id: "q_100",
        question_text: "请确认下一步口味偏好",
        options: [
          { option_id: 1, text: "选项 1：清淡低盐" },
          { option_id: 2, text: "选项 2：浓郁微辣" },
        ],
      };

      const wrapper = mount(ChatPanel, {
        props: {
          messages: [],
          phases: [],
          answer: "",
          clarification: "请确认下一步口味偏好",
          activeClarification,
          status: "needs_clarification",
          isStreaming: false,
          canSend: true,
          participants: [],
          currentMenu: null,
        },
      });

      expect(wrapper.text()).toContain("请确认下一步口味偏好");
      const buttons = wrapper.findAll(".clarification-option-btn");
      expect(buttons).toHaveLength(2);
      expect(buttons[0].text()).toContain("选项 1：清淡低盐");

      // 点击第一个按钮
      await buttons[0].trigger("click");
      expect(wrapper.emitted("selectOption")).toBeTruthy();
      expect(wrapper.emitted("selectOption")![0]).toEqual([1]);
    });

    it("发送期间（isStreaming=true）禁用澄清选项按钮", () => {
      const activeClarification = {
        question_id: "q_100",
        question_text: "请确认下一步口味偏好",
        options: [{ option_id: 1, text: "选项 1：清淡低盐" }],
      };

      const wrapper = mount(ChatPanel, {
        props: {
          messages: [],
          phases: [],
          answer: "",
          clarification: "请确认下一步口味偏好",
          activeClarification,
          status: "needs_clarification",
          isStreaming: true,
          canSend: false,
          participants: [],
          currentMenu: null,
        },
      });

      const button = wrapper.find(".clarification-option-btn");
      expect(button.attributes("disabled")).toBeDefined();
    });
  });
});
