import { defineConfig } from "@playwright/test";
import { fileURLToPath } from "node:url";

// One real-chain entrypoint. A separate server preserves the user page on 5174.
const reportName = process.env.PLAYWRIGHT_REPORT_NAME || `live-browser-${Date.now()}`;
if (!/^[a-zA-Z0-9_-]+$/.test(reportName)) throw new Error("Invalid PLAYWRIGHT_REPORT_NAME");

export default defineConfig({
  testDir: "./e2e", timeout: 300000, expect: { timeout: 180000 }, workers: 1,
  reporter: [["line"], ["json", {
    outputFile: fileURLToPath(new URL(`../verification/artifacts/${reportName}.json`, import.meta.url)),
  }]],
  outputDir: fileURLToPath(new URL(`../verification/artifacts/${reportName}`, import.meta.url)),
  use: { baseURL: "http://127.0.0.1:5176", trace: "retain-on-failure", screenshot: "only-on-failure" },
  webServer: {
    command: "npm run preview -- --port 5176 --strictPort",
    url: "http://127.0.0.1:5176", reuseExistingServer: false,
    env: { VITE_API_BASE_URL: "/api" },
  },
});
