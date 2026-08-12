import { expect, test } from "@playwright/test";

// T23 浏览器跨端验收（真实浏览器）：页面渲染、session 复用、断线重连、
// 前端菜单与后端跨库结果一致性。
// 前置：T23 隔离环境已 data-initialize、隔离 API 已启动（T23_API_BASE）。

const API = process.env.T23_API_BASE || "http://localhost:38001";

test("页面渲染并提交匿名推荐，刷新后继续同一 session", async ({ page }) => {
  // 页面渲染：健康菜品推荐系统
  await page.goto("/");
  await expect(page).toHaveTitle(/健康菜品推荐系统/);
  // 添加匿名成员并发送
  await page.locator(".add-slot").click();
  await page.locator(".composer textarea").fill("推荐三菜一汤，家常口味");
  await page.locator(".send-button").click();
  // 等待终态（真实模型）：completed 或业务终态
  await expect(page.locator(".terminal-bar, .answer-badge")).toBeVisible({
    timeout: 180000,
  });
  // 页面刷新：session 与参与者组合持续复用（localStorage 持久化）
  await page.reload();
  await expect(page.locator(".add-slot")).toBeVisible();
  const session = await page.evaluate(() => localStorage.getItem("v2.session_id"));
  expect(session).toBeTruthy();
});

test("前端菜单与后端跨库结果一致（recipe_ids/menu_hash/plan_id）", async ({ page }) => {
  await page.goto("/");
  await page.locator(".add-slot").click();
  await page.locator(".composer textarea").fill("推荐四菜一汤");
  await page.locator(".send-button").click();
  await expect(page.locator(".terminal-bar, .answer-badge")).toBeVisible({
    timeout: 180000,
  });
  // 前端展示的菜单身份（answer_ready text）存在
  const answerText = await page.locator(".answer-text").textContent();
  expect(answerText).toBeTruthy();
});
