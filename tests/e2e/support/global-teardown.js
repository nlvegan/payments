/**
 * Playwright Global Teardown
 *
 * Runs once after all tests to:
 * 1. Cancel and delete test invoices created during setup
 * 2. Clean up test data file
 */

const { chromium } = require("@playwright/test");
const fs = require("fs");
const path = require("path");

const TEST_DATA_FILE = path.join(__dirname, ".test-data.json");

async function globalTeardown(config) {
  console.log("\n[Global Teardown] Starting...");

  // Skip cleanup if NO_CLEANUP env var is set (useful for debugging)
  if (process.env.NO_CLEANUP) {
    console.log("[Global Teardown] Skipped (NO_CLEANUP=1)");
    return;
  }

  // Read test data
  let testData = {};
  try {
    testData = JSON.parse(fs.readFileSync(TEST_DATA_FILE, "utf-8"));
  } catch (e) {
    console.log(
      "[Global Teardown] No test data file found - nothing to clean up"
    );
    return;
  }

  // Only clean up invoices we created
  if (!testData.createdBySetup || !testData.invoiceName) {
    console.log(
      "[Global Teardown] Skipped (using externally provided test invoice)"
    );
    // Clean up test data file
    try {
      fs.unlinkSync(TEST_DATA_FILE);
    } catch (e) {
      /* ignore */
    }
    return;
  }

  const baseURL = config.projects[0].use.baseURL || "http://localhost:8000";

  const browser = await chromium.launch();
  const context = await browser.newContext({ baseURL });
  const page = await context.newPage();

  try {
    // Login
    await page.goto("/login");
    await page.waitForLoadState("networkidle");

    const emailField = page.locator('input[data-fieldname="usr"]');
    if (await emailField.isVisible({ timeout: 5000 }).catch(() => false)) {
      await emailField.fill("Administrator");
      await page
        .locator('input[data-fieldname="pwd"]')
        .fill(process.env.ADMIN_PASSWORD || "admin");
      await page.locator(".btn-login").click();
    }

    await page.waitForURL("**/app/**", { timeout: 15000 }).catch(() => {});

    // Cancel and delete the test invoice
    console.log(
      `[Global Teardown] Cleaning up invoice: ${testData.invoiceName}`
    );

    const cleanupResult = await page.evaluate(async (invoiceName) => {
      try {
        // First check the invoice status
        const getResp = await fetch("/api/method/frappe.client.get", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Frappe-CSRF-Token": frappe.csrf_token,
          },
          body: JSON.stringify({
            doctype: "Sales Invoice",
            name: invoiceName,
          }),
        });
        const getData = await getResp.json();

        if (!getData.message) {
          return {
            success: true,
            note: "Invoice not found (already deleted?)",
          };
        }

        const docstatus = getData.message.docstatus;

        // Only cancel if submitted (docstatus=1)
        if (docstatus === 1) {
          const cancelResp = await fetch("/api/method/frappe.client.cancel", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-Frappe-CSRF-Token": frappe.csrf_token,
            },
            body: JSON.stringify({
              doctype: "Sales Invoice",
              name: invoiceName,
            }),
          });
          const cancelData = await cancelResp.json();
          if (cancelData.exc) {
            return {
              success: false,
              error: `Cancel failed: ${cancelData.exc}`,
            };
          }
        }

        // Delete the invoice
        const deleteResp = await fetch("/api/method/frappe.client.delete", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Frappe-CSRF-Token": frappe.csrf_token,
          },
          body: JSON.stringify({
            doctype: "Sales Invoice",
            name: invoiceName,
          }),
        });
        const deleteData = await deleteResp.json();
        if (deleteData.exc) {
          return { success: false, error: `Delete failed: ${deleteData.exc}` };
        }

        return { success: true };
      } catch (e) {
        return { success: false, error: e.message };
      }
    }, testData.invoiceName);

    if (cleanupResult.success) {
      console.log(`[Global Teardown] Deleted invoice: ${testData.invoiceName}`);
      if (cleanupResult.note) {
        console.log(`[Global Teardown] Note: ${cleanupResult.note}`);
      }
    } else {
      console.log(
        `[Global Teardown] Could not delete invoice: ${cleanupResult.error}`
      );
    }
  } catch (error) {
    console.error("[Global Teardown] Error:", error.message);
  } finally {
    await browser.close();
  }

  // Clean up test data file
  try {
    fs.unlinkSync(TEST_DATA_FILE);
  } catch (e) {
    // File may not exist
  }

  console.log("[Global Teardown] Complete\n");
}

module.exports = globalTeardown;
