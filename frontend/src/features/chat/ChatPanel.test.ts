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

    it("生成中（isStreaming=true）显示停止生成按钮且点击触发 cancel", async () => {
      const wrapper = mount(ChatPanel, {
        props: {
          messages: [],
          phases: [],
          answer: "",
          clarification: "",
          status: "running",
          isStreaming: true,
          canSend: false,
          participants: [],
          currentMenu: null,
        },
      });

      const stopBtn = wrapper.find("[data-testid='stop-generating-button']");
      expect(stopBtn.exists()).toBe(true);
      await stopBtn.trigger("click");
      expect(wrapper.emitted("cancel")).toBeTruthy();
    });

    it("非生成状态（isStreaming=false）显示普通发送按钮", () => {
      const wrapper = mount(ChatPanel, {
        props: {
          messages: [],
          phases: [],
          answer: "",
          clarification: "",
          status: "completed",
          isStreaming: false,
          canSend: true,
          participants: [],
          currentMenu: null,
        },
      });

      expect(wrapper.find("[data-testid='stop-generating-button']").exists()).toBe(false);
      expect(wrapper.find(".send-button").exists()).toBe(true);
    });

    it("渲染 Assistant 消息的可折叠思维链卡片 (Thought Process)", async () => {
      const wrapper = mount(ChatPanel, {
        props: {
          messages: [
            {
              id: "assistant-1",
              role: "assistant",
              content: "为您搭配的菜单",
              createdAt: 1,
              status: "complete",
              thoughts: [
                {
                  node_id: "audit",
                  title: "医学健康规则审查",
                  status: "done",
                  summary: "已完成过敏原硬隔离",
                  tool_name: "B4 健康引擎",
                },
              ],
            },
          ],
          phases: [],
          answer: "为您搭配的菜单",
          clarification: "",
          status: "completed",
          isStreaming: false,
          canSend: true,
          participants: [],
          currentMenu: null,
        },
      });

      const thoughtBox = wrapper.find("[data-testid='thought-process-container']");
      expect(thoughtBox.exists()).toBe(true);
      expect(thoughtBox.text()).toContain("医学健康规则审查");
      expect(thoughtBox.text()).toContain("B4 健康引擎");
      expect(thoughtBox.text()).toContain("已完成过敏原硬隔离");

      // 点击收起思维链
      const headerBtn = thoughtBox.find(".thought-header-btn");
      await headerBtn.trigger("click");
      expect(thoughtBox.classes()).toContain("thought-collapsed");
    });

    it("isCancelling=true 时停止按钮处于禁用并显示取消中", () => {
      const wrapper = mount(ChatPanel, {
        props: {
          messages: [],
          phases: [],
          answer: "",
          clarification: "",
          status: "running",
          isStreaming: true,
          isCancelling: true,
          canSend: false,
          participants: [],
          currentMenu: null,
        },
      });

      const stopBtn = wrapper.find("[data-testid='stop-generating-button']");
      expect(stopBtn.exists()).toBe(true);
      expect(stopBtn.attributes("disabled")).toBeDefined();
      expect(stopBtn.attributes("title")).toBe("正在停止…");
      expect(stopBtn.find(".spin").exists()).toBe(true);
    });

    it("点击菜品卡片的“换这道”按钮在输入框中填入单菜替换意图", async () => {
      const wrapper = mount(ChatPanel, {
        props: {
          messages: [],
          phases: [],
          answer: "",
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

      const replaceBtns = wrapper.findAll(".dish-replace-btn");
      expect(replaceBtns).toHaveLength(2);
      expect(replaceBtns[0].text()).toBe("换这道");

      await replaceBtns[0].trigger("click");
      const textarea = wrapper.find(".composer textarea");
      expect((textarea.element as HTMLTextAreaElement).value).toBe("把番茄炒蛋换掉，推荐一道别的菜");
    });

    it("多轮对话中，已取消的助手消息显示已停止生成且不展示完成徽标 (R03)", () => {
      const wrapper = mount(ChatPanel, {
        props: {
          messages: [
            {
              id: "assistant-1",
              role: "assistant",
              content: "第一轮菜品",
              createdAt: 1,
              status: "complete",
              isCommitted: true,
            },
            {
              id: "assistant-2",
              role: "assistant",
              content: "已停止生成。",
              createdAt: 2,
              status: "cancelled",
              isCommitted: false,
              thoughts: [
                { node_id: "accepted", title: "请求已接收", status: "done" },
              ],
            },
          ],
          phases: [],
          answer: "",
          clarification: "",
          status: "cancelled",
          isStreaming: false,
          canSend: true,
          participants: [],
          currentMenu: null,
        },
      });

      const thoughtBox = wrapper.find("[data-testid='thought-process-container']");
      expect(thoughtBox.exists()).toBe(true);
      // 失败/取消绝不宣称合规完成
      expect(thoughtBox.text()).toContain("用户已停止执行");
      expect(thoughtBox.text()).not.toContain("已完成健康合规与配餐推导");

      const badges = wrapper.findAll(".answer-badge");
      expect(badges).toHaveLength(2);
      expect(badges[0].text()).toContain("菜单已生成");
      expect(badges[1].text()).toContain("已停止生成");
      expect(badges[1].classes()).toContain("cancelled");
    });

    it("消息绑定的澄清卡片：当前有效问题可点击并向外派发 selectOption 及 messageId (R06)", async () => {
      const activeClarification = {
        question_id: "q_bound_1",
        question_text: "请确认烹饪时间上限",
        options: [
          { option_id: 1, text: "30分钟以内" },
          { option_id: 2, text: "60分钟以内" },
        ],
      };

      const wrapper = mount(ChatPanel, {
        props: {
          messages: [
            {
              id: "assistant-1",
              role: "assistant",
              content: "",
              createdAt: 1,
              status: "complete",
              clarification: activeClarification,
            },
          ],
          phases: [],
          answer: "",
          clarification: "请确认烹饪时间上限",
          activeClarification,
          status: "needs_clarification",
          isStreaming: false,
          canSend: true,
          participants: [],
          currentMenu: null,
        },
      });

      const card = wrapper.find(".message-clarification-card");
      expect(card.exists()).toBe(true);
      expect(card.text()).toContain("待确认");
      expect(card.text()).toContain("请确认烹饪时间上限");

      const buttons = card.findAll(".clarification-option-btn");
      expect(buttons).toHaveLength(2);
      expect(buttons[0].attributes("disabled")).toBeUndefined();

      await buttons[0].trigger("click");
      expect(wrapper.emitted("selectOption")).toBeTruthy();
      expect(wrapper.emitted("selectOption")![0]).toEqual([1, "assistant-1"]);
    });

    it("历史已回答的澄清卡片：展示已选标记且全部按钮禁用 (R06)", async () => {
      const wrapper = mount(ChatPanel, {
        props: {
          messages: [
            {
              id: "assistant-history",
              role: "assistant",
              content: "",
              createdAt: 1,
              status: "complete",
              clarification: {
                question_id: "q_prev",
                question_text: "历史问题：请确认主食偏好",
                options: [
                  { option_id: 1, text: "米饭" },
                  { option_id: 2, text: "面食" },
                ],
              },
              selectedOptionId: 1,
            },
          ],
          phases: [],
          answer: "",
          clarification: "",
          activeClarification: null,
          status: "",
          isStreaming: false,
          canSend: true,
          participants: [],
          currentMenu: null,
        },
      });

      const card = wrapper.find(".message-clarification-card");
      expect(card.exists()).toBe(true);
      expect(card.text()).toContain("已确认");
      expect(card.text()).toContain("已选");

      const buttons = card.findAll(".clarification-option-btn");
      expect(buttons[0].classes()).toContain("selected");
      expect(buttons[0].attributes("disabled")).toBeDefined();
      expect(buttons[1].attributes("disabled")).toBeDefined();

      await buttons[0].trigger("click");
      expect(wrapper.emitted("selectOption")).toBeFalsy();
    });

    it("已过期或非当前有效的问题卡片展示已失效且不可点击 (R06)", async () => {
      const wrapper = mount(ChatPanel, {
        props: {
          messages: [
            {
              id: "assistant-stale",
              role: "assistant",
              content: "",
              createdAt: 1,
              status: "complete",
              clarification: {
                question_id: "q_stale",
                question_text: "已过期的旧问题",
                options: [{ option_id: 1, text: "旧选项" }],
              },
            },
          ],
          phases: [],
          answer: "",
          clarification: "",
          activeClarification: {
            question_id: "q_different_active",
            question_text: "当前最新的有效问题",
            options: [{ option_id: 2, text: "新选项" }],
          },
          status: "needs_clarification",
          isStreaming: false,
          canSend: true,
          participants: [],
          currentMenu: null,
        },
      });

      const card = wrapper.find(".message-clarification-card");
      expect(card.exists()).toBe(true);
      expect(card.text()).toContain("已失效");
      const btn = card.find(".clarification-option-btn");
      expect(btn.attributes("disabled")).toBeDefined();
    });
  });

  describe("ChatPanel（单菜替换协议与锁定 R07）", () => {
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

    it("点击菜品卡片的换这道按钮，派发 replaceDish 事件并携带菜品结构化信息", async () => {
      const wrapper = mount(ChatPanel, {
        props: {
          messages: [],
          phases: [],
          answer: "",
          clarification: "",
          status: "completed",
          isStreaming: false,
          canSend: true,
          participants: [],
          currentMenu: mockMenu,
        },
      });

      const replaceBtns = wrapper.findAll(".dish-replace-btn");
      await replaceBtns[0].trigger("click");

      expect(wrapper.emitted("replaceDish")).toBeTruthy();
      expect(wrapper.emitted("replaceDish")![0]).toEqual([{ recipe_id: 101, name: "番茄炒蛋" }]);
    });

    it("处于替换锁定状态时渲染 replace-lock-banner，目标菜品展示高亮状态", async () => {
      const wrapper = mount(ChatPanel, {
        props: {
          messages: [],
          phases: [],
          answer: "",
          clarification: "",
          status: "completed",
          isStreaming: false,
          canSend: true,
          participants: [],
          currentMenu: mockMenu,
          pendingReplaceDish: {
            target_recipe_id: 101,
            dish_name: "番茄炒蛋",
            source_plan_id: "plan-1",
            source_menu_hash: "a".repeat(64),
          },
        },
      });

      const banner = wrapper.find("[data-testid='replace-lock-banner']");
      expect(banner.exists()).toBe(true);
      expect(banner.text()).toContain("正在替换");
      expect(banner.text()).toContain("番茄炒蛋");
      expect(banner.text()).toContain("#101");
      expect(banner.text()).toContain("版本已锁定");

      const replaceBtns = wrapper.findAll(".dish-replace-btn");
      expect(replaceBtns[0].classes()).toContain("is-active");
      expect(replaceBtns[0].text()).toBe("替换中");
      expect(replaceBtns[1].classes()).not.toContain("is-active");
      expect(replaceBtns[1].text()).toBe("换这道");

      // 点击取消按钮派发 cancelReplaceDish
      const cancelBtn = wrapper.find("[data-testid='cancel-replace-btn']");
      await cancelBtn.trigger("click");
      expect(wrapper.emitted("cancelReplaceDish")).toBeTruthy();
    });

    it("替换锁定状态下未输入草稿时，点击发送按钮自动派发默认替换意图", async () => {
      const wrapper = mount(ChatPanel, {
        props: {
          messages: [],
          phases: [],
          answer: "",
          clarification: "",
          status: "completed",
          isStreaming: false,
          canSend: true,
          participants: [],
          currentMenu: mockMenu,
          pendingReplaceDish: {
            target_recipe_id: 101,
            dish_name: "番茄炒蛋",
            source_plan_id: "plan-1",
            source_menu_hash: "a".repeat(64),
          },
        },
      });

      const sendBtn = wrapper.find(".send-button");
      expect(sendBtn.attributes("disabled")).toBeUndefined();

      await sendBtn.trigger("click");
      expect(wrapper.emitted("send")).toBeTruthy();
      expect(wrapper.emitted("send")![0]).toEqual(["换掉番茄炒蛋，推荐一道别的菜"]);
    });
  });
});
