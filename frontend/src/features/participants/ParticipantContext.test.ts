import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import ParticipantContext from "./ParticipantContext.vue";

const members = [
  { participant_ref: "p1", label: "参与者 1" },
  { participant_ref: "p2", label: "参与者 2" },
];

describe("ParticipantContext（本次对话成员选择）", () => {
  it("从已有成员列表选择，而不是逐个创建成员", async () => {
    const wrapper = mount(ParticipantContext, {
      props: { slots: members, selectedRefs: [], disabled: false },
    });
    expect(wrapper.text()).toContain("选择参与成员");
    expect(wrapper.text()).not.toContain("添加匿名成员");
    await wrapper.find(".picker-trigger").trigger("click");
    expect(wrapper.findAll(".picker-item")).toHaveLength(2);
    await wrapper.findAll(".picker-item")[1].trigger("click");
    expect(wrapper.emitted("toggle")?.[0]).toEqual(["p2"]);
  });

  it("只展示本次选中的成员，并允许取消选择", async () => {
    const wrapper = mount(ParticipantContext, {
      props: { slots: members, selectedRefs: ["p2"], disabled: false },
    });
    expect(wrapper.findAll(".chip")).toHaveLength(1);
    expect(wrapper.find(".chip").text()).toContain("参与者 2");
    await wrapper.find(".chip-x").trigger("click");
    expect(wrapper.emitted("toggle")?.[0]).toEqual(["p2"]);
  });

  it("已开始的对话不能更改参与成员", () => {
    const wrapper = mount(ParticipantContext, {
      props: { slots: members, selectedRefs: ["p1"], disabled: true },
    });
    expect((wrapper.find(".picker-trigger").element as HTMLButtonElement).disabled).toBe(true);
    expect((wrapper.find(".chip-x").element as HTMLButtonElement).disabled).toBe(true);
  });
});
