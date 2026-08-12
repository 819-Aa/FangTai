import { defineConfig } from "@playwright/test";

// T23 浏览器跨端验收：真实页面渲染、SSE Last-Event-ID 断线重连、
// 页面刷新继续同 session、前端菜单与后端跨库结果一致性。
// 需先按 scripts/run_full_acceptance.ps1 启动 T23 环境与隔离 API。
const API = process.env.T23_API_BASE || "http://localhost:38001";
const WEB = process.env.T23_WEB_BASE || "http://localhost:5174";

export default defineConfig({
  testDir: "./e2e",
  timeout: 60000,
  use: {
    baseURL: WEB,
    extraHTTPHeaders: { "x-e2e-api": API },
  },
  webServer: {
    command: "npm run dev -- --port 5174",
    url: WEB,
    reuseExistingServer: true,
    timeout: 60000,
  },
});
