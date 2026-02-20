/**
 * Playwright Global Setup
 *
 * Runs once before all tests to:
 * 1. Verify Stripe credentials are configured
 * 2. Create a submitted Sales Invoice for checkout tests
 * 3. Store test data for use by test specs
 */

const { chromium } = require("@playwright/test");
const fs = require("fs");
const path = require("path");

// File to store test data between setup and tests
const TEST_DATA_FILE = path.join(__dirname, ".test-data.json");

async function globalSetup(config) {
  console.log("\n[Global Setup] Starting...");

  // Check required environment variables
  const requiredEnvVars = [
    "STRIPE_TEST_SECRET_KEY",
    "STRIPE_TEST_PUBLISHABLE_KEY",
  ];

  const missingVars = requiredEnvVars.filter((v) => !process.env[v]);
  if (missingVars.length > 0) {
    console.warn(
      `[Global Setup] Warning: Missing environment variables: ${missingVars.join(
        ", "
      )}`
    );
    console.warn(
      "[Global Setup] Tests will be skipped without Stripe credentials."
    );
  }

  // If TEST_INVOICE_NAME is provided, use it directly (manual/CI mode)
  if (process.env.TEST_INVOICE_NAME) {
    console.log(
      `[Global Setup] Using provided invoice: ${process.env.TEST_INVOICE_NAME}`
    );
    fs.writeFileSync(
      TEST_DATA_FILE,
      JSON.stringify({
        invoiceName: process.env.TEST_INVOICE_NAME,
        paymentGateway: process.env.TEST_PAYMENT_GATEWAY || "Stripe-Stripe",
        createdBySetup: false,
      })
    );
    console.log("[Global Setup] Complete\n");
    return;
  }

  // Get base URL from config
  const baseURL = config.projects[0].use.baseURL || "http://localhost:8000";
  console.log(`[Global Setup] Using base URL: ${baseURL}`);

  // Launch browser to setup test data
  const browser = await chromium.launch();
  const context = await browser.newContext({ baseURL });
  const page = await context.newPage();

  let testData = {
    invoiceName: null,
    paymentGateway: "Stripe-Stripe",
    createdBySetup: false,
  };

  try {
    // Login as Administrator
    console.log("[Global Setup] Logging in...");
    await page.goto("/login");
    await page.waitForLoadState("networkidle");

    // Fill login form
    const emailField = page.locator('input[data-fieldname="usr"]');
    if (await emailField.isVisible({ timeout: 5000 }).catch(() => false)) {
      await emailField.fill("Administrator");
      await page
        .locator('input[data-fieldname="pwd"]')
        .fill(process.env.ADMIN_PASSWORD || "admin");
      await page.locator(".btn-login").click();
    } else {
      // Try alternative selectors
      await page.fill(
        'input[data-fieldname="email"], input#login_email',
        "Administrator"
      );
      await page.fill(
        'input[data-fieldname="password"], input#login_password',
        process.env.ADMIN_PASSWORD || "admin"
      );
      await page.click('button[data-label="Login"], button.btn-login');
    }

    // Wait for login to complete
    await page.waitForURL("**/app/**", { timeout: 30000 }).catch(() => {
      console.log(
        "[Global Setup] Login redirect timeout - checking if already logged in"
      );
    });

    // Verify we can access the API
    const sessionCheck = await page.evaluate(async () => {
      const resp = await fetch("/api/method/frappe.auth.get_logged_user");
      const data = await resp.json();
      return data.message;
    });
    console.log(`[Global Setup] Logged in as: ${sessionCheck}`);

    // Get the first company (before_tests creates "Wind Power LLC")
    const company = await page.evaluate(async () => {
      const resp = await fetch("/api/method/frappe.client.get_list", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Frappe-CSRF-Token": frappe.csrf_token,
        },
        body: JSON.stringify({
          doctype: "Company",
          fields: ["name"],
          limit_page_length: 1,
        }),
      });
      const data = await resp.json();
      return data.message?.[0]?.name;
    });
    console.log(`[Global Setup] Using company: ${company}`);

    if (!company) {
      throw new Error(
        "No default company configured - cannot create test invoice"
      );
    }

    // Get or create Stripe Settings for E2E tests
    const stripeSecretKey = process.env.STRIPE_TEST_SECRET_KEY;
    const stripePublishableKey = process.env.STRIPE_TEST_PUBLISHABLE_KEY;

    const stripeSettings = await page.evaluate(
      async (params) => {
        const { secretKey, publishableKey } = params;

        // Check for existing Stripe Settings
        const checkResp = await fetch("/api/method/frappe.client.get_list", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Frappe-CSRF-Token": frappe.csrf_token,
          },
          body: JSON.stringify({
            doctype: "Stripe Settings",
            fields: ["name", "gateway_name"],
            limit_page_length: 1,
          }),
        });
        const checkData = await checkResp.json();

        if (checkData.message?.length > 0) {
          return { name: checkData.message[0].name, created: false };
        }

        // Create Stripe Settings if none exist and we have credentials
        if (!secretKey || !publishableKey) {
          return null;
        }

        const createResp = await fetch("/api/method/frappe.client.insert", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Frappe-CSRF-Token": frappe.csrf_token,
          },
          body: JSON.stringify({
            doc: {
              doctype: "Stripe Settings",
              gateway_name: "Stripe",
              publishable_key: publishableKey,
              secret_key: secretKey,
            },
          }),
        });
        const createData = await createResp.json();

        if (createData.exc || !createData.message?.name) {
          console.error("Failed to create Stripe Settings:", createData);
          return null;
        }

        return { name: createData.message.name, created: true };
      },
      { secretKey: stripeSecretKey, publishableKey: stripePublishableKey }
    );

    if (stripeSettings) {
      // Payment Gateway name format is "GatewayType-SettingsName"
      const paymentGatewayName = `Stripe-${stripeSettings.name}`;
      testData.paymentGateway = paymentGatewayName;

      if (stripeSettings.created) {
        console.log(
          `[Global Setup] Created Stripe Settings: ${stripeSettings.name}`
        );
      } else {
        console.log(
          `[Global Setup] Found Stripe Settings: ${stripeSettings.name}`
        );
      }

      // Create or verify Payment Gateway exists
      const paymentGateway = await page.evaluate(
        async (params) => {
          const { gatewayName, settingsName } = params;

          // Check if Payment Gateway exists
          const checkResp = await fetch("/api/method/frappe.client.get_count", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-Frappe-CSRF-Token": frappe.csrf_token,
            },
            body: JSON.stringify({
              doctype: "Payment Gateway",
              filters: { name: gatewayName },
            }),
          });
          const checkData = await checkResp.json();

          if (checkData.message > 0) {
            return { name: gatewayName, created: false };
          }

          // Create Payment Gateway
          const createResp = await fetch("/api/method/frappe.client.insert", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-Frappe-CSRF-Token": frappe.csrf_token,
            },
            body: JSON.stringify({
              doc: {
                doctype: "Payment Gateway",
                gateway: gatewayName,
                gateway_settings: "Stripe Settings",
                gateway_controller: settingsName,
              },
            }),
          });
          const createData = await createResp.json();

          if (createData.exc || !createData.message?.name) {
            console.error("Failed to create Payment Gateway:", createData);
            return null;
          }

          return { name: createData.message.name, created: true };
        },
        { gatewayName: paymentGatewayName, settingsName: stripeSettings.name }
      );

      if (paymentGateway?.created) {
        console.log(
          `[Global Setup] Created Payment Gateway: ${paymentGateway.name}`
        );
      } else if (paymentGateway) {
        console.log(
          `[Global Setup] Found Payment Gateway: ${paymentGateway.name}`
        );
      }
    } else {
      console.warn(
        "[Global Setup] No Stripe Settings found and no credentials to create - tests may fail"
      );
    }

    // Create test Sales Invoice
    console.log("[Global Setup] Creating test Sales Invoice...");
    const invoiceResult = await page.evaluate(
      async (params) => {
        const { company } = params;

        // Get or create test customer
        let customer = "_Test Stripe Customer";
        const customerCheck = await fetch(
          "/api/method/frappe.client.get_count",
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-Frappe-CSRF-Token": frappe.csrf_token,
            },
            body: JSON.stringify({
              doctype: "Customer",
              filters: { customer_name: customer },
            }),
          }
        );
        const customerCount = await customerCheck.json();

        if (customerCount.message === 0) {
          const createCustomer = await fetch(
            "/api/method/frappe.client.insert",
            {
              method: "POST",
              headers: {
                "Content-Type": "application/json",
                "X-Frappe-CSRF-Token": frappe.csrf_token,
              },
              body: JSON.stringify({
                doc: {
                  doctype: "Customer",
                  customer_name: customer,
                  customer_type: "Individual",
                  customer_group: "Individual",
                  territory: "All Territories",
                },
              }),
            }
          );
          const customerData = await createCustomer.json();
          if (customerData.exc) {
            throw new Error(`Failed to create customer: ${customerData.exc}`);
          }
          customer = customerData.message.name;
        }

        // Get income account for the company (use root_type instead of account_type)
        const accountResp = await fetch("/api/method/frappe.client.get_list", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Frappe-CSRF-Token": frappe.csrf_token,
          },
          body: JSON.stringify({
            doctype: "Account",
            filters: {
              company: company,
              root_type: "Income",
              is_group: 0,
            },
            fields: ["name"],
            limit_page_length: 1,
          }),
        });
        const accountData = await accountResp.json();
        const incomeAccount = accountData.message?.[0]?.name;

        if (!incomeAccount) {
          throw new Error(
            `No income account found for company ${company}. Response: ${JSON.stringify(
              accountData
            )}`
          );
        }

        // Get cost center
        const costCenterResp = await fetch(
          "/api/method/frappe.client.get_list",
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-Frappe-CSRF-Token": frappe.csrf_token,
            },
            body: JSON.stringify({
              doctype: "Cost Center",
              filters: { company: company, is_group: 0 },
              fields: ["name"],
              limit_page_length: 1,
            }),
          }
        );
        const costCenterData = await costCenterResp.json();
        const costCenter = costCenterData.message?.[0]?.name;

        // Get company currency and default receivable account
        const companyDoc = await fetch("/api/method/frappe.client.get", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Frappe-CSRF-Token": frappe.csrf_token,
          },
          body: JSON.stringify({
            doctype: "Company",
            name: company,
          }),
        });
        const companyData = await companyDoc.json();
        const companyCurrency = companyData.message?.default_currency || "USD";
        const defaultReceivable =
          companyData.message?.default_receivable_account;

        // Create Sales Invoice (draft first)
        // Let ERPNext auto-assign debit_to if not specified
        const invoiceDoc = {
          doctype: "Sales Invoice",
          customer: customer,
          company: company,
          currency: companyCurrency,
          conversion_rate: 1,
          selling_price_list: "Standard Selling",
          price_list_currency: companyCurrency,
          plc_conversion_rate: 1,
          items: [
            {
              item_name: "Stripe Test Payment",
              description: "Test item for Stripe E2E tests",
              qty: 1,
              rate: 25.5,
              amount: 25.5,
              income_account: incomeAccount,
              cost_center: costCenter,
            },
          ],
        };

        // Only set debit_to if we have a default receivable account
        if (defaultReceivable) {
          invoiceDoc.debit_to = defaultReceivable;
        }

        const createInvoice = await fetch("/api/method/frappe.client.insert", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Frappe-CSRF-Token": frappe.csrf_token,
          },
          body: JSON.stringify({ doc: invoiceDoc }),
        });
        const invoiceData = await createInvoice.json();

        if (invoiceData.exc || !invoiceData.message?.name) {
          throw new Error(
            `Failed to create invoice: ${JSON.stringify(invoiceData)}`
          );
        }

        const invoiceName = invoiceData.message.name;

        // Fetch the latest document to get current modified timestamp
        const getInvoice = await fetch("/api/method/frappe.client.get", {
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
        const latestInvoice = await getInvoice.json();

        if (!latestInvoice.message) {
          throw new Error(
            `Failed to fetch invoice: ${JSON.stringify(latestInvoice)}`
          );
        }

        // Submit the invoice with fresh document (required for payment)
        const submitInvoice = await fetch("/api/method/frappe.client.submit", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Frappe-CSRF-Token": frappe.csrf_token,
          },
          body: JSON.stringify({
            doc: latestInvoice.message,
          }),
        });
        const submitData = await submitInvoice.json();

        if (submitData.exc) {
          throw new Error(`Failed to submit invoice: ${submitData.exc}`);
        }

        return {
          success: true,
          name: invoiceName,
          amount: 25.5,
          currency: companyCurrency,
        };
      },
      { company }
    );

    if (invoiceResult.success) {
      testData.invoiceName = invoiceResult.name;
      testData.createdBySetup = true;
      console.log(
        `[Global Setup] Created and submitted invoice: ${invoiceResult.name} (${invoiceResult.currency} ${invoiceResult.amount})`
      );
    } else {
      throw new Error("Invoice creation failed");
    }
  } catch (error) {
    console.error("[Global Setup] Error:", error.message);
    // Take a screenshot for debugging
    await page
      .screenshot({ path: "test-results/global-setup-error.png" })
      .catch(() => {});
  } finally {
    await browser.close();
  }

  // Save test data for specs to use
  fs.writeFileSync(TEST_DATA_FILE, JSON.stringify(testData));

  // Also set environment variable for backward compatibility
  if (testData.invoiceName) {
    process.env.TEST_INVOICE_NAME = testData.invoiceName;
    process.env.TEST_PAYMENT_GATEWAY = testData.paymentGateway;
  }

  console.log("[Global Setup] Complete\n");
}

module.exports = globalSetup;
