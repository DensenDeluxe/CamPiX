import { defineConfig } from "@playwright/test";
import fs from "node:fs";

const python =
  process.env.CAMPIX_TEST_PYTHON ||
  (process.platform === "win32"
    ? ".venv/Scripts/python.exe"
    : ".venv/bin/python");
const chrome = process.env.CAMPIX_TEST_CHROME;
export default defineConfig({
  testDir: "./tests/browser",
  fullyParallel: false,
  workers: 1,
  timeout: 45000,
  expect: { timeout: 10000 },
  reporter: "list",
  use: {
    baseURL: "http://127.0.0.1:4401",
    viewport: { width: 1440, height: 1050 },
    trace: "retain-on-failure",
    launchOptions:
      chrome && fs.existsSync(chrome) ? { executablePath: chrome } : {},
  },
  webServer: {
    command: `"${python}" scripts/browser-test-server.py`,
    url: "http://127.0.0.1:4401/healthz",
    reuseExistingServer: false,
    env: { CAMPIX_E2E: "1" },
    gracefulShutdown: { signal: "SIGTERM", timeout: 15000 },
  },
});
