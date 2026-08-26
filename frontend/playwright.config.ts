import { defineConfig } from "@playwright/test";

// T23 浏览器跨端验收：真实页面渲染、SSE Last-Event-ID 断线重连、
// 页面刷新继续同 session、前端菜单与后端跨库结果一致性。
// 前置：T23 隔离环境已 data-initialize、隔离 API 运行于 38001。
const API = process.env.T23_API_BASE || "http://localhost:38001";
const WEB = process.env.T23_WEB_BASE || "http://localhost:5174";

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
    // 前端 API 客户端经 VITE_API_BASE_URL 指向 T23 隔离 API（绝不访问默认 API 8000）
    env: {
      VITE_API_BASE_URL: API,
    },
  },
});
