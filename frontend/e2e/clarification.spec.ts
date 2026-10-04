import { expect, test } from "@playwright/test";

const API = process.env.H07_API_BASE || "http://localhost:8003";

test("真实 SSE 连续失败降级轮询：待澄清停止轮询，选项续接后成单", async ({ page }) => {
  page.on("pageerror", (error) => console.log("SSE_PAGE_ERROR", error.message));
  page.on("console", (message) => { if (message.text().startsWith("SSE_DIAG")) console.log(message.text()); });
  await page.addInitScript(() => {
    const Native = window.EventSource;
    window.EventSource = class extends Native {
      constructor(url: string | URL, options?: EventSourceInit) {
        super(url, options);
        this.addEventListener("error", () => console.log("SSE_DIAG", JSON.stringify({ readyState: this.readyState })));
      }
    };
  });
  let polls = 0;
  const ids: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "GET" && /recommendation-requests\/[^/]+$/.test(new URL(request.url()).pathname)) polls += 1;
  });
  page.on("response", async (response) => {
    if (response.request().method() === "POST" && new URL(response.url()).pathname.endsWith("/recommendation-requests") && response.ok()) {
      ids.push((await response.json()).request_id);
    }
  });
  // Only break the browser transport; all POST and GET responses are real.
  await page.route("**/events", (route) => route.abort("connectionfailed"));
  await page.goto("/");
  await page.locator(".picker-trigger").click();
  await page.locator(".picker-item").first().click();
  await page.locator(".picker-trigger").click();
  await page.locator(".composer textarea").fill("推荐三道晚餐，所有菜品合计必须在1分钟内完成；不能满足时请让我选择放宽时间或减少菜数");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(page.locator(".terminal-bar")).toHaveText("待澄清", { timeout: 60000 });
  const stoppedAt = polls;
  expect(stoppedAt).toBeGreaterThan(0);
  // Five seconds is the real fallback poll interval.
  await page.waitForTimeout(6000);
  expect(polls).toBe(stoppedAt);
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const status = (await page.locator(".terminal-bar").textContent())?.trim();
    expect(["已完成", "待澄清"]).toContain(status);
    if (status === "已完成") break;
    const options = page.locator(".message-clarification-card.is-active .clarification-option-btn");
    const timeOption = options.filter({ hasText: "时间" }).first();
    // A minimum-time estimate may still be infeasible after fresh retrieval.
    // Use the offered explicit relaxation instead of assuming three estimates suffice.
    const unrestrictedTime = options.filter({ hasText: /取消.*时间/ }).first();
    const choice = attempt > 0 && (await unrestrictedTime.count()) ? unrestrictedTime
      : (await timeOption.count()) ? timeOption : options.first();
    await expect(choice).toBeEnabled();
    const accepted = page.waitForResponse((response) => response.request().method() === "POST"
      && new URL(response.url()).pathname.endsWith("/recommendation-requests"));
    await choice.click();
    expect((await accepted).ok()).toBe(true);
    await expect(page.locator(".terminal-bar")).toBeVisible({ timeout: 180000 });
  }
  await expect(page.locator(".terminal-bar")).toHaveText("已完成");
  await expect(page.locator(".message-clarification-card.is-active")).toHaveCount(0);
  await expect(page.locator("[data-testid='committed-menu'] li")).toHaveCount(3);
  const state = await (await page.request.get(`${API}/v1/recommendation-requests/${ids.at(-1)}`)).json();
  expect(state.status).toBe("completed");
  expect(state.result_summary.menu_summary.recipe_ids).toHaveLength(3);
  const evidence = { request_ids: ids, polls_before_clarification: stoppedAt, poll_stopped: true,
    final_status: state.status, session_id: state.session_id, menu: state.result_summary.menu_summary };
  await test.info().attach("real-poll-clarification", {
    contentType: "application/json", body: Buffer.from(JSON.stringify(evidence, null, 2)),
  });
});
