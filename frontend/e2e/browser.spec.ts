import { expect, test } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

// T23 浏览器跨端最小验收（真实浏览器，主用户流程可用优先）。
// 前置：H07 存储 ready、API 运行于 8003、Vite 注入 VITE_API_BASE_URL。
// 成功路径只接受 completed / result_committed。

const API = process.env.H07_API_BASE || "http://localhost:8003";
const projectRoot = fileURLToPath(new URL("../../", import.meta.url));
const python = fileURLToPath(new URL("../../.venv/Scripts/python.exe", import.meta.url));

function sessionLock(action: "acquire" | "release", session: string, token = ""): string {
  const source = "import sys; from food_agent_v2.c4 import ContextService; c=ContextService(); "
    + (action === "acquire"
      ? "print(c.acquire_session_lock(sys.argv[1], 'browser-live-verification') or '')"
      : "print(c.release_session_lock(sys.argv[1], sys.argv[2]))");
  return execFileSync(python, ["-c", source, session, token], { cwd: projectRoot, encoding: "utf8" }).trim();
}

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
    pending_verification: "状态待核对",
  };
  return map[status] || status;
}

async function selectMember(page: import("@playwright/test").Page) {
  const trigger = page.locator(".picker-trigger");
  if ((await trigger.count()) > 0 && !(await trigger.isDisabled())) {
    await trigger.click();
    const item = page.locator(".picker-item").first();
    if ((await item.count()) > 0) {
      await item.click();
    }
    await trigger.click();
  }
}

async function submitRecommendation(page: import("@playwright/test").Page, text: string) {
  await page.goto("/");
  await expect(page).toHaveTitle(/健康菜品推荐系统/);
  await selectMember(page);
  await page.locator(".composer textarea").fill(text);
  await page.locator(".send-button").click();
  await expect(page.locator(".terminal-bar")).toBeVisible({ timeout: 300000 });
}

test("首次进入为空白新对话且可选择成员", async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveTitle(/健康菜品推荐系统/);
  // 新对话初始消息列表为空，显示引导提示
  await expect(page.locator(".conversation-empty")).toBeVisible();
  // 尚未发送前参与者可编辑
  const trigger = page.locator(".picker-trigger");
  if ((await trigger.count()) > 0) {
    await expect(trigger).toBeEnabled();
  }
});

test("成功路径：仅接受 completed / result_committed，并归档至侧边栏", async ({ page }) => {
  await submitRecommendation(page, "今晚晚餐，请推荐三道清淡家常菜，45分钟内完成");
  const status = (await page.locator(".terminal-bar").textContent())?.trim();
  expect(status).toBe(terminalLabel("completed"));
  await expect(page.locator(".answer-badge")).toBeVisible({ timeout: 60000 });
  expect((await page.locator(".answer-badge").textContent())).toContain("菜单已生成");

  // 验证侧边栏记录已生成
  await expect(page.locator(".sidebar-chat")).toHaveCount(1);
  expect(await page.locator(".sidebar-chat-title").textContent()).toBeTruthy();
});

test("页面刷新默认进入空白对话，可从侧边栏恢复历史会话与已提交菜单", async ({ page }) => {
  await submitRecommendation(page, "今晚晚餐，请推荐三道清淡家常菜，45分钟内完成");
  await expect(page.locator(".sidebar-chat")).toHaveCount(1);

  // 刷新页面：现行架构默认进入新空白对话
  await page.reload();
  await expect(page.locator(".conversation-empty")).toBeVisible();

  // 点击侧边栏历史会话进行恢复
  await page.locator(".sidebar-chat").first().click();

  // 恢复后：显示历史消息、已提交菜单看板
  await expect(page.locator(".message-list .message")).toHaveCount(2);
  await expect(page.locator("[data-testid='committed-menu']")).toBeVisible();
  const restoredItems = page.locator("[data-testid='committed-menu'] li");
  expect(await restoredItems.count()).toBeGreaterThan(0);
});

test("多轮对话会话内成员固定：首轮发送后禁用参与者切换", async ({ page }) => {
  await submitRecommendation(page, "清淡晚餐");
  // 首次发送后，参与者管理被锁定，不能随意变更成员
  const trigger = page.locator(".picker-trigger");
  if ((await trigger.count()) > 0) {
    await expect(trigger).toBeDisabled();
  }
});

test("生成中点击停止生成能够可靠取消且不展示完成徽标", async ({ page }) => {
  await page.goto("/");
  await selectMember(page);
  await page.locator(".composer textarea").fill("推荐复杂的八道养生菜");
  await page.locator(".send-button").click();

  // 点击停止生成
  const stopBtn = page.locator("[data-testid='stop-generating-button']");
  await expect(stopBtn).toBeVisible({ timeout: 10000 });
  await stopBtn.click();

  // 终态显示已取消
  await expect(page.locator(".terminal-bar")).toBeVisible({ timeout: 60000 });
  const status = (await page.locator(".terminal-bar").textContent())?.trim();
  expect(status).toBe(terminalLabel("cancelled"));

  // 绝不展示“菜单已生成”
  const committedBadges = page.locator(".answer-badge:not(.cancelled):not(.pending)");
  await expect(committedBadges).toHaveCount(0);
});

test("SSE 中断连接后自动恢复并最终 completed", async ({ page }) => {
  let blocked = false;
  await page.route("**/events", async (route) => {
    if (!blocked) {
      blocked = true;
      await route.abort("connectionfailed");
    } else {
      await route.continue();
    }
  });
  // Use the live-verified feasible request so missing meal/data constraints do
  // not hide the SSE recovery behavior under test. Completed remains mandatory.
  await submitRecommendation(page, "今晚晚餐，请推荐三道清淡家常菜，45分钟内完成");
  const status = (await page.locator(".terminal-bar").textContent())?.trim();
  expect(status).toBe(terminalLabel("completed"));
  await expect(page.locator(".answer-badge")).toHaveCount(1, { timeout: 60000 });
});

test("浏览器最终菜单展示（真实可用）：正文与结构化菜单均可见", async ({ page }) => {
  await submitRecommendation(page, "今晚晚餐，请推荐三道清淡家常菜，45分钟内完成");
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

test("真实锁竞争：刷新后点击恢复生成，用原请求成单并与数据库一致", async ({ page }) => {
  let session = "";
  let token = "";
  const bodies: unknown[] = [];
  const requestIds: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "POST" && new URL(request.url()).pathname.endsWith("/recommendation-requests")) {
      bodies.push(request.postDataJSON());
    }
  });
  page.on("response", async (response) => {
    if (response.request().method() === "POST" && new URL(response.url()).pathname.endsWith("/recommendation-requests") && response.ok()) {
      requestIds.push((await response.json()).request_id);
    }
  });
  // Forward the real session response after acquiring its actual Redis lock.
  await page.route("**/v1/sessions", async (route) => {
    const response = await route.fetch();
    expect(response.ok()).toBe(true);
    session = (await response.json()).session_id;
    token = sessionLock("acquire", session);
    expect(token).not.toBe("");
    await route.fulfill({ response });
  });
  try {
    await page.goto("/");
    await selectMember(page);
    await page.locator(".composer textarea").fill("今晚晚餐，请推荐三道清淡家常菜，45分钟内完成");
    await page.getByRole("button", { name: "发送", exact: true }).click();
    await expect(page.getByRole("button", { name: "恢复生成", exact: true })).toBeEnabled({ timeout: 45000 });
    await page.reload();
    await expect(page.locator(".conversation-empty")).toBeVisible();
    await page.locator(".sidebar-chat").first().click();
    await expect(page.getByRole("button", { name: "恢复生成", exact: true })).toBeEnabled();
    expect(sessionLock("release", session, token)).toBe("True");
    token = "";
    await page.getByRole("button", { name: "恢复生成", exact: true }).click();
    await expect(page.locator(".terminal-bar")).toHaveText("已完成", { timeout: 240000 });
    expect(bodies).toHaveLength(2);
    expect(bodies[1]).toEqual(bodies[0]);
    expect(requestIds).toHaveLength(2);
    expect(requestIds[1]).toBe(requestIds[0]);
    await expect(page.locator(".message.user")).toHaveCount(1);
    const response = await page.request.get(`${API}/v1/recommendation-requests/${requestIds[0]}`);
    const state = await response.json();
    expect(state.status).toBe("completed");
    const current = (await (await page.request.get(`${API}/v1/sessions/${session}`)).json()).current_menu;
    const menu = state.result_summary.menu_summary;
    for (const key of ["build_id", "plan_id", "menu_hash", "recipe_ids", "items"]) expect(current[key]).toEqual(menu[key]);
    expect(menu.recipe_ids).toHaveLength(3);
    await expect(page.locator("[data-testid='committed-menu'] li")).toHaveCount(3);
    for (const item of menu.items) await expect(page.locator("[data-testid='committed-menu']")).toContainText(item.name);
    const durable = execFileSync(python, ["-c", "import sys,json; from food_agent_v2.c4 import ContextService; c=ContextService(); l=c.load_recommendation_log(sys.argv[1]); a=c.load_request_acceptance_by_request_id(sys.argv[1]); print(json.dumps({'status':l['status'],'plan_id':l['final_plan_id'],'generation':a['execution_generation']}))", requestIds[0]], { cwd: projectRoot, encoding: "utf8" });
    expect(JSON.parse(durable)).toMatchObject({ status: "completed", plan_id: menu.plan_id, generation: 2 });
  } finally {
    if (token) sessionLock("release", session, token);
  }
});
