import { defineConfig } from "@playwright/test";
import { fileURLToPath } from "node:url";

export default defineConfig({
  testDir: ".", testMatch: "browser-audit.ts", timeout: 15000, workers: 1,
  expect: { timeout: 4000 },
  reporter: [["line"], ["json", { outputFile: fileURLToPath(new URL("../../verification/artifacts/browser-audit.json", import.meta.url)) }]],
  outputDir: fileURLToPath(new URL("../../verification/artifacts/browser-audit", import.meta.url)),
  use: { baseURL: "http://127.0.0.1:5175" },
  webServer: {
    command: "npm run dev -- --port 5175 --strictPort",
    url: "http://127.0.0.1:5175", reuseExistingServer: false,
    env: { VITE_API_BASE_URL: "/api" },
  },
});
