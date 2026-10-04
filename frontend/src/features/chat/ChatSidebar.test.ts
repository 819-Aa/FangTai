import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import ChatSidebar from "./ChatSidebar.vue";
import type { ChatRecord } from "@/stores/recommendation";

const chat: ChatRecord = {
  id: "chat-1", title: "推荐晚餐", updatedAt: 1,
  selectedRefs: ["p1", "p2"], sessionId: "sess-1", sessionRefs: ["p1", "p2"],
  messages: [], requestId: "", status: "completed", phases: [], answer: "",
  currentMenu: null, clarification: "", activeClarification: null, error: "",
};

describe("ChatSidebar", () => {
  it("展示历史对话并允许切换与新建", async () => {
    const wrapper = mount(ChatSidebar, {
      props: { chats: [chat], activeChatId: "chat-1", disabled: false, storageError: "" },
    });
    expect(wrapper.text()).toContain("推荐晚餐");
    expect(wrapper.text()).toContain("2 位成员");
    expect(wrapper.find(".sidebar-chat").attributes("aria-current")).toBe("page");
    await wrapper.find(".sidebar-chat").trigger("click");
    await wrapper.find(".sidebar-new-chat").trigger("click");
    expect(wrapper.emitted("openChat")?.[0]).toEqual(["chat-1"]);
    expect(wrapper.emitted("newChat")).toHaveLength(1);
  });
});
