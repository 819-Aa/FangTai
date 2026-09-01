import { expect, test } from "@playwright/test";

// T23 浏览器跨端最小验收（真实浏览器，主用户流程可用优先）。
// 前置：H07 存储 ready、API 运行于 8003、Vite 注入 VITE_API_BASE_URL。
// 成功路径只接受 completed / result_committed。

const API = process.env.H07_API_BASE || "http://localhost:8003";

function terminalLabel(status: string): string {
  const map: Record<string, string> = {
    completed: "已完成",
    no_safe_menu: "无安全菜单",
    no_feasible_menu: "无可行菜单",
    strict_time_indeterminate: "严格时间约束无法判定",
    failed: "处理失败",
    cancelled: "已取消",
    interrupted: "已中断",
    needs_clarification: "待澄清",
  };
  return map[status] || status;
}

async function submitRecommendation(page: import("@playwright/test").Page, text: string) {
  await page.goto("/");
  await expect(page).toHaveTitle(/健康菜品推荐系统/);
  if ((await page.locator(".add-slot").count()) > 0) await page.locator(".add-slot").click();
  await page.locator(".composer textarea").fill(text);
  await page.locator(".send-button").click();
  await expect(page.locator(".terminal-bar")).toBeVisible({ timeout: 300000 });
}

test("成功路径：仅接受 completed / result_committed", async ({ page }) => {
  await submitRecommendation(page, "推荐三菜一汤，家常口味");
  const status = (await page.locator(".terminal-bar").textContent())?.trim();
  expect(status).toBe(terminalLabel("completed"));
  await expect(page.locator(".answer-badge")).toBeVisible({ timeout: 60000 });
  expect((await page.locator(".answer-badge").textContent())).toContain("菜单已生成");
});

test("页面刷新继续同一 session（核对刷新后的第二次请求）", async ({ page }) => {
  // 首次请求完成并取得非空 session_id 后记录 before
  await submitRecommendation(page, "推荐家常菜");
  const before = await page.evaluate(() => ({
    sid: localStorage.getItem("v2.session_id") || "",
    refs: JSON.parse(localStorage.getItem("v2.session_refs") || "[]"),
  }));
  expect(before.sid).toBeTruthy();
  // 只拦截并核对刷新后的第二次请求
  const seen: string[] = [];
  await page.route("**/recommendation-requests", async (route) => {
    const body = route.request().postData();
    if (body) {
      try {
        const p = JSON.parse(body);
        if (p.session_id) seen.push(p.session_id);
      } catch { /* 忽略 */ }
    }
    await route.continue();
  });
  await page.reload();
  const after = await page.evaluate(() => ({
    sid: localStorage.getItem("v2.session_id") || "",
    refs: JSON.parse(localStorage.getItem("v2.session_refs") || "[]"),
  }));
  expect(after.sid).toBe(before.sid);
  expect(after.refs).toEqual(before.refs);
  // 刷新必须从 session 已提交事实恢复结构化菜单，不依赖内存中的回答文本。
  await expect(page.locator("[data-testid='committed-menu']")).toBeVisible();
  const restoredItems = page.locator("[data-testid='committed-menu'] li");
  expect(await restoredItems.count()).toBeGreaterThan(0);
  // 刷新后再次发送，并核对请求载荷继续使用同一 session_id
  await page.locator(".composer textarea").fill("再推荐一道汤");
  await page.locator(".send-button").click();
  await expect(page.locator(".terminal-bar")).toBeVisible({ timeout: 300000 });
  expect(seen.length).toBeGreaterThan(0);
  for (const sid of seen) expect(sid).toBe(before.sid);
});

test("SSE 人为中断一次连接后仍 completed，result_committed 只展示一次", async ({ page }) => {
  // 人为中断一次 events 连接（浏览器 EventSource 自动重连）
  let blocked = false;
  await page.route("**/events", async (route) => {
    if (!blocked) {
      blocked = true;
      await route.abort("connectionfailed");
    } else {
      await route.continue();
    }
  });
  await submitRecommendation(page, "推荐三菜一汤");
  const status = (await page.locator(".terminal-bar").textContent())?.trim();
  expect(status).toBe(terminalLabel("completed"));
  // result_committed 徽标只展示一次
  await expect(page.locator(".answer-badge")).toHaveCount(1, { timeout: 60000 });
});

test("浏览器最终菜单展示（真实可用）：正文与结构化菜单均可见", async ({ page }) => {
  await submitRecommendation(page, "推荐四菜一汤");
  const status = (await page.locator(".terminal-bar").textContent())?.trim();
  expect(status).toBe(terminalLabel("completed"));
  const answerText = await page.locator(".answer-text").textContent();
  expect(answerText?.trim().length).toBeGreaterThan(0);
  const menu = page.locator("[data-testid='committed-menu'] li");
  expect(await menu.count()).toBeGreaterThan(0);
  for (const row of await menu.allTextContents()) expect(row.trim().length).toBeGreaterThan(0);
  const api = await fetch(`${API}/ready`);
  expect(api.ok).toBe(true);
});
