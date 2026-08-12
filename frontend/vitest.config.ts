import { fileURLToPath, URL } from "node:url";

import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    // T23 浏览器 E2E（Playwright）不属于 Vitest 单测，明确排除
    exclude: ["e2e/**", "node_modules/**", "dist/**"],
  },
});
