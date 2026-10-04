// Browser integration with mocked API: does not prove C3/database/model correctness.
import { expect, test, type Page } from "@playwright/test";

const menu = {
  build_id: "audit-build", plan_id: "audit-plan", menu_hash: "a".repeat(64),
  recipe_ids: [101, 202], items: [{ recipe_id: 101, name: "番茄炒蛋" }, { recipe_id: 202, name: "清炒时蔬" }],
};
const newerMenu = {
  ...menu, plan_id: "updated-plan", menu_hash: "b".repeat(64),
  recipe_ids: [303, 202], items: [{ recipe_id: 303, name: "冬瓜汤" }, menu.items[1]],
};

async function selectMember(page: Page) {
  await page.locator(".picker-trigger").click();
  await page.locator(".picker-item").first().click();
  await page.locator(".picker-trigger").click();
}

async function mockApi(page: Page) {
  const captured: any[] = [];
  let conflicted = false;
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/events")) {
      const events = [
        ["accepted", "request_accepted", { request_id: "audit-request" }],
        ["answer", "answer_ready", { text: "测试菜单已生成" }],
        ["committed", "result_committed", { menu_summary: menu }],
      ];
      await route.fulfill({ contentType: "text/event-stream", body: events.map(([id, event, data]) =>
        `id: ${id}\nevent: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join("") });
    } else if (path.endsWith("/recommendation-requests") && route.request().method() === "POST") {
      const payload = route.request().postDataJSON();
      captured.push(payload);
      if (payload.action === "replace_dish") {
        conflicted = true;
        await route.fulfill({ status: 409, json: { error: "MENU_VERSION_CONFLICT", current_menu: newerMenu } });
      } else {
        await route.fulfill({ status: 202, json: {
          request_id: "audit-request", session_id: "audit-session", status: "accepted", created_at: "t",
        } });
      }
    } else if (path.endsWith("/sessions") && route.request().method() === "POST") {
      await route.fulfill({ json: { session_id: "audit-session" } });
    } else if (path.endsWith("/sessions/audit-session")) {
      await route.fulfill({ json: {
        session_id: "audit-session", participant_refs: ["p1"], request_count: 1,
        current_menu: conflicted ? newerMenu : menu, active_clarification: null,
      } });
    } else {
      await route.fulfill({ status: 404, json: { error: "audit route missing" } });
    }
  });
  return captured;
}

async function completeRecommendation(page: Page) {
  await page.goto("/");
  await selectMember(page);
  await page.locator(".composer textarea").fill("推荐家常菜");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(page.locator(".terminal-bar")).toHaveText("已完成");
}

test("B01: real member picker selects a member and enables send", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator(".conversation-empty")).toBeVisible();
  await expect(page.locator(".add-slot")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "发送", exact: true })).toBeDisabled();
  await selectMember(page);
  await page.locator(".composer textarea").fill("测试需求");
  await expect(page.getByRole("button", { name: "发送", exact: true })).toBeEnabled();
});

test("B02: mocked commit locks members; reload starts blank; sidebar restores menu", async ({ page }) => {
  await mockApi(page);
  await completeRecommendation(page);
  await expect(page.locator(".picker-trigger")).toBeDisabled();
  await expect(page.locator(".answer-badge")).toHaveText("菜单已生成");
  await expect(page.locator(".sidebar-chat")).toHaveCount(1);
  await page.reload();
  await expect(page.locator(".conversation-empty")).toBeVisible();
  await page.locator(".sidebar-chat").first().click();
  await expect(page.locator("[data-testid='committed-menu']")).toContainText("番茄炒蛋");
  await expect(page.locator(".picker-trigger")).toBeDisabled();
});

test("B03: structured replacement sends source version and refreshes mocked conflict", async ({ page }) => {
  const captured = await mockApi(page);
  await completeRecommendation(page);
  await page.locator("[data-testid='dish-replace-button']").first().click();
  await expect(page.locator("[data-testid='replace-lock-banner']")).toContainText("番茄炒蛋");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(page.locator(".global-error")).toContainText("菜单版本已变更");
  expect(captured[1]).toMatchObject({
    action: "replace_dish", target_recipe_id: 101,
    source_plan_id: "audit-plan", source_menu_hash: "a".repeat(64),
  });
  await expect(page.locator("[data-testid='committed-menu']")).toContainText("冬瓜汤");
});

test("B04: recovery button survives reload and resends the exact original request", async ({ page }) => {
  const bodies: unknown[] = [];
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/sessions")) {
      await route.fulfill({ json: { session_id: "recovery-session" } });
    } else if (path.endsWith("/recommendation-requests") && route.request().method() === "POST") {
      bodies.push(route.request().postDataJSON());
      await route.fulfill({ json: { request_id: "recovery-request", session_id: "recovery-session", status: "accepted", created_at: "t" } });
    } else if (path.endsWith("/events")) {
      const events = bodies.length === 1
        ? [["recovery-1", "request_recovery_required", { request_id: "recovery-request", status: "recovery_required" }]]
        : [["answer", "answer_ready", { text: "恢复完成" }], ["commit", "result_committed", { menu_summary: menu }]];
      await route.fulfill({ contentType: "text/event-stream", body: events.map(([id, event, data]) =>
        `id: ${id}\nevent: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join("") });
    } else {
      await route.fulfill({ json: { request_id: "recovery-request", session_id: "recovery-session", status: "recovery_required", created_at: "t" } });
    }
  });
  await page.goto("/");
  await selectMember(page);
  await page.locator(".composer textarea").fill("今晚三道家常菜");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(page.getByRole("button", { name: "恢复生成", exact: true })).toBeEnabled();
  await page.reload();
  await expect(page.locator(".conversation-empty")).toBeVisible();
  await page.locator(".sidebar-chat").first().click();
  await page.getByRole("button", { name: "恢复生成", exact: true }).click();
  await expect(page.locator(".terminal-bar")).toHaveText("已完成");
  await expect(page.locator("[data-testid='committed-menu']")).toContainText("番茄炒蛋");
  expect(bodies).toHaveLength(2);
  expect(bodies[1]).toEqual(bodies[0]);
  await expect(page.locator(".message.user")).toHaveCount(1);
});
