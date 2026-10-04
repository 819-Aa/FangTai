import { describe, expect, it } from "vitest";
import { updateMessageThoughts } from "./recommendation";
import type { ChatMessage } from "@/types";

function message(): ChatMessage {
  return { id: "m", role: "assistant", content: "", createdAt: 1, status: "sending" };
}

describe("execution progress identity", () => {
  it("keeps separate calls and generations, and merges running/done for one call", () => {
    const msg = message();
    for (const [generation, invocation] of [[1, "a"], [1, "b"], [2, "a"]] as const) {
      for (const status of ["running", "done"]) {
        updateMessageThoughts(msg, "thought_node", {
          node_id: "candidate_search", title: "检索", status,
          execution_generation: generation, invocation_id: invocation,
        }, 1);
      }
    }
    expect(msg.thoughts).toHaveLength(3);
    expect(msg.thoughts!.map(t => t.status)).toEqual(["done", "done", "done"]);
  });

  it("routes late tool facts to the matching call and ignores unrelated calls", () => {
    const msg = message();
    for (const invocation of ["a", "b"]) {
      updateMessageThoughts(msg, "thought_node", {
        node_id: "candidate_search", title: "检索", status: "running",
        execution_generation: 1, invocation_id: invocation,
      }, 1);
    }
    updateMessageThoughts(msg, "tool_trace", {
      node_id: "candidate_search", execution_generation: 1, invocation_id: "a",
      tool_name: "search_candidates", result_summary: "第一轮完成",
    }, 1);
    updateMessageThoughts(msg, "tool_trace", {
      execution_generation: 2, invocation_id: "missing", result_summary: "不应挂载",
    }, 1);
    expect(msg.thoughts![0].summary).toBe("第一轮完成");
    expect(msg.thoughts![1].summary).toBeUndefined();
  });

  it("projects a clarification warning onto its real invocation without a done badge", () => {
    const msg = message();
    const identity = { node_id: "menu_combination", execution_generation: 1, invocation_id: "a" };
    updateMessageThoughts(msg, "thought_node", { ...identity, title: "菜单规划", status: "warning" }, 1);
    updateMessageThoughts(msg, "analysis_ready", {
      ...identity, stage: "menu_planning", status: "warning", summary: "菜单规划需要确认",
    }, 1);
    expect(msg.thoughts).toHaveLength(1);
    expect(msg.thoughts![0].status).toBe("warning");
  });
});
