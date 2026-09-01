import { defineConfig } from "@playwright/test";

// H07 浏览器跨端验收：真实页面渲染、SSE Last-Event-ID 断线重连、
// 页面刷新继续同 session、前端菜单与后端跨库结果一致性。
// 前置：H07 MySQL/Qdrant/Redis ready，API 运行于 8003。
const API = process.env.H07_API_BASE || "http://localhost:8003";
const WEB = process.env.H07_WEB_BASE || "http://localhost:5174";

export default defineConfig({
  testDir: "./e2e",
  // 真实模型单次全链路可超过 180s；总超时必须大于该等待
  timeout: 300000,
  expect: { timeout: 180000 },
  // 严禁复用已启动的 dev server；Vite strict port（端口占用即失败）
  fullyParallel: false,
  workers: 1,
  use: {
    baseURL: WEB,
  },
  webServer: {
    command: "npm run dev -- --port 5174 --strictPort",
    url: WEB,
    reuseExistingServer: false,
    timeout: 120000,
    // 前端 API 客户端经 VITE_API_BASE_URL 指向 H07 API。
    env: {
      VITE_API_BASE_URL: API,
    },
  },
});
