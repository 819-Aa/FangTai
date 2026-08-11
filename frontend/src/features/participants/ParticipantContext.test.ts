import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import ParticipantContext from "./ParticipantContext.vue";

describe("ParticipantContext（匿名参与者）", () => {
  it("只渲染匿名槽位，不含真实 user_id/健康详情", () => {
    const wrapper = mount(ParticipantContext, {
      props: {
        slots: [{ participant_ref: "p1", label: "参与者 1" }],
        selectedRefs: ["p1"],
        disabled: false,
      },
    });
    expect(wrapper.text()).toContain("参与者 1");
    expect(wrapper.text()).toContain("匿名");
    expect(wrapper.text()).not.toContain("user_id");
    expect(wrapper.text()).not.toContain("过敏");
    expect(wrapper.text()).not.toContain("疾病");
    expect(wrapper.text()).not.toContain("年龄");
  });

  it("点击添加按钮触发 add 事件", async () => {
    const wrapper = mount(ParticipantContext, {
      props: { slots: [], selectedRefs: [], disabled: false },
    });
    await wrapper.find(".add-slot").trigger("click");
    expect(wrapper.emitted("add")).toHaveLength(1);
  });

  it("点击成员移除按钮触发 remove(ref) 事件", async () => {
    const wrapper = mount(ParticipantContext, {
      props: {
        slots: [{ participant_ref: "p1", label: "参与者 1" }],
        selectedRefs: ["p1"],
        disabled: false,
      },
    });
    await wrapper.find(".chip-x").trigger("click");
    expect(wrapper.emitted("remove")?.[0]).toEqual(["p1"]);
  });

  it("disabled 时按钮不可交互", async () => {
    const wrapper = mount(ParticipantContext, {
      props: {
        slots: [{ participant_ref: "p1", label: "参与者 1" }],
        selectedRefs: ["p1"],
        disabled: true,
      },
    });
    const addBtn = wrapper.find(".add-slot").element as HTMLButtonElement;
    const xBtn = wrapper.find(".chip-x").element as HTMLButtonElement;
    expect(addBtn.disabled).toBe(true);
    expect(xBtn.disabled).toBe(true);
  });
});
