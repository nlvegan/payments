/**
 * Playwright Configuration for Stripe E2E Tests
 *
 * Run with: npx playwright test
 * Debug with: npx playwright test --debug
 */

const { defineConfig, devices } = require("@playwright/test");

module.exports = defineConfig({
  testDir: "./tests/e2e",
  timeout: 60000,
  expect: {
    timeout: 10000,
  },
  fullyParallel: false, // Sequential to avoid DB conflicts in Frappe
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: 1, // Single worker to avoid database conflicts
  reporter: process.env.CI
    ? [["github"], ["html", { open: "never" }]]
    : [["list"], ["html", { open: "on-failure" }]],

  use: {
    baseURL: process.env.TEST_SITE_URL || "https://veg11.veganisme.org",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: process.env.CI ? "retain-on-failure" : "off",
    actionTimeout: 15000,
    navigationTimeout: 30000,
    ignoreHTTPSErrors: true, // For self-signed/local certs
  },

  projects: [
    {
      name: "stripe-checkout",
      testMatch: /stripe-checkout.*\.spec\.js/,
      use: { ...devices["Desktop Chrome"] },
    },
  ],

  globalSetup: "./tests/e2e/support/global-setup.js",
  globalTeardown: "./tests/e2e/support/global-teardown.js",

  outputDir: "test-results/",
});
