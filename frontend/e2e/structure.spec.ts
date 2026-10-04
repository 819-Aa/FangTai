import { expect, test } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

test("四菜一汤：真实五道菜、汤槽位、执行身份与刷新恢复", async ({ page }) => {
  await page.goto("/");
  await page.locator(".picker-trigger").click();
  await page.locator(".picker-item").first().click();
  await page.locator(".picker-trigger").click();
  await page.locator(".composer textarea").fill("今晚晚餐请安排四菜一汤，家常口味，180分钟内完成");
  const accepted = page.waitForResponse(r => r.request().method() === "POST"
    && new URL(r.url()).pathname.endsWith("/recommendation-requests"));
  await page.locator(".send-button").click();
  const rid = (await (await accepted).json()).request_id;
  await expect(page.locator(".terminal-bar")).toHaveText("已完成", { timeout: 300000 });
  await expect(page.locator("[data-testid='committed-menu'] li")).toHaveCount(5);
  const response = await page.request.get(`http://127.0.0.1:8003/v1/recommendation-requests/${rid}`);
  expect(response.ok()).toBe(true);
  const state = await response.json();
  expect(state.status).toBe("completed");
  const menu = state.result_summary.menu_summary;
  const names = menu.items.map((it: { name: string }) => it.name);
  const python = fileURLToPath(new URL("../../.venv/Scripts/python.exe", import.meta.url));
  const root = fileURLToPath(new URL("../../", import.meta.url));
  const kinds = JSON.parse(execFileSync(python, ["-c",
    "import json,sys; from food_agent_v2.c2.planner import MenuPlanner; print(json.dumps([MenuPlanner._classify_dish_type(n) for n in json.loads(sys.argv[1])]))",
    JSON.stringify(names)], { cwd: root, encoding: "utf8" }));
  expect(kinds.filter((k: string) => k === "soup")).toHaveLength(1);
  expect(kinds.filter((k: string) => k === "main")).toHaveLength(4);
  const chats = await page.evaluate(() => JSON.parse(localStorage.getItem("v2.chat_history.v1") || "[]"));
  const thoughts = chats.flatMap((c: { messages: Array<{ thoughts?: unknown[] }> }) => c.messages.flatMap(m => m.thoughts || []));
  const bound = thoughts.filter((t: { invocation_id?: string }) => t.invocation_id);
  expect(bound.length).toBeGreaterThan(3);
  expect(bound.every((t: { execution_generation?: number }) => t.execution_generation === 1)).toBe(true);
  expect(new Set(bound.map((t: { invocation_id: string }) => t.invocation_id)).size).toBe(bound.length);
  await page.reload();
  await expect(page.locator(".conversation-empty")).toBeVisible();
  await page.locator(".sidebar-chat").first().click();
  await expect(page.locator("[data-testid='committed-menu'] li")).toHaveCount(5);
  await test.info().attach("real-menu-and-progress", { contentType: "application/json",
    body: Buffer.from(JSON.stringify({ request_id: rid, items: menu.items, dish_kinds: kinds, invocation_count: bound.length })) });
});
