/**
 * Stripe E2E Test Helpers
 *
 * Utilities for automating Stripe checkout flows in Playwright tests.
 */

/**
 * Stripe test card numbers for different scenarios.
 * @see https://docs.stripe.com/testing#cards
 */
const TEST_CARDS = {
  SUCCESS: "4242424242424242",
  DECLINE: "4000000000000002",
  DECLINE_INSUFFICIENT_FUNDS: "4000000000009995",
  DECLINE_EXPIRED: "4000000000000069",
  REQUIRES_3DS: "4000002500003155",
  FAILS_3DS: "4000002760003184",
};

/**
 * Test card expiry and CVC values (any future date works with Stripe test cards)
 */
const TEST_CARD_DETAILS = {
  expiry: "12/30",
  cvc: "123",
  postalCode: "12345",
};

/**
 * Fill in Stripe Card Element with test card data.
 *
 * Stripe Elements uses an iframe, so we need to switch context.
 *
 * @param {import('@playwright/test').Page} page - Playwright page object
 * @param {string} cardNumber - Card number from TEST_CARDS
 * @param {Object} options - Additional options
 * @param {string} options.expiry - Expiry date (default: 12/30)
 * @param {string} options.cvc - CVC (default: 123)
 */
async function fillStripeCardElement(page, cardNumber, options = {}) {
  const { expiry = TEST_CARD_DETAILS.expiry, cvc = TEST_CARD_DETAILS.cvc } =
    options;

  // Wait for Stripe Elements to load
  const cardFrame = page
    .frameLocator('iframe[name^="__privateStripeFrame"]')
    .first();

  // Fill card number
  await cardFrame.locator('[name="cardnumber"]').fill(cardNumber);

  // Fill expiry
  await cardFrame.locator('[name="exp-date"]').fill(expiry);

  // Fill CVC
  await cardFrame.locator('[name="cvc"]').fill(cvc);
}

/**
 * Wait for Stripe Elements to be fully loaded and ready.
 *
 * @param {import('@playwright/test').Page} page - Playwright page object
 * @param {number} timeout - Maximum wait time in ms
 */
async function waitForStripeReady(page, timeout = 10000) {
  // Wait for Stripe.js to load
  await page.waitForFunction(() => typeof window.Stripe !== "undefined", {
    timeout,
  });

  // Wait for card element iframe to be present
  await page.waitForSelector('iframe[name^="__privateStripeFrame"]', {
    timeout,
  });

  // Small delay to ensure Elements is fully initialized
  await page.waitForTimeout(500);
}

/**
 * Create a test Sales Invoice via Frappe API.
 * Used to provide a reference document for checkout tests.
 *
 * @param {import('@playwright/test').Page} page - Playwright page object
 * @param {Object} options - Invoice options
 * @returns {Promise<string>} The created invoice name
 */
async function createTestSalesInvoice(page, options = {}) {
  const {
    customer = "_Test Customer",
    amount = 100.0,
    currency = "USD",
  } = options;

  const result = await page.evaluate(
    async ({ customer, amount, currency }) => {
      const response = await fetch("/api/method/frappe.client.insert", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Frappe-CSRF-Token": frappe.csrf_token,
        },
        body: JSON.stringify({
          doc: {
            doctype: "Sales Invoice",
            customer: customer,
            items: [
              {
                item_code: "_Test Item",
                qty: 1,
                rate: amount,
              },
            ],
            currency: currency,
          },
        }),
      });
      const data = await response.json();
      return data.message.name;
    },
    { customer, amount, currency }
  );

  return result;
}

/**
 * Delete a test Sales Invoice via Frappe API.
 *
 * @param {import('@playwright/test').Page} page - Playwright page object
 * @param {string} invoiceName - The invoice name to delete
 */
async function deleteTestSalesInvoice(page, invoiceName) {
  await page.evaluate(async (name) => {
    await fetch("/api/method/frappe.client.delete", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Frappe-CSRF-Token": frappe.csrf_token,
      },
      body: JSON.stringify({
        doctype: "Sales Invoice",
        name: name,
      }),
    });
  }, invoiceName);
}

/**
 * Login to Frappe as Administrator (for API access).
 *
 * @param {import('@playwright/test').Page} page - Playwright page object
 * @param {string} user - Username (default: Administrator)
 * @param {string} password - Password (default: admin)
 */
async function loginToFrappe(page, user = "Administrator", password = "admin") {
  await page.goto("/login");
  await page.fill('input[data-fieldname="email"]', user);
  await page.fill('input[data-fieldname="password"]', password);
  await page.click('button[data-label="Login"]');
  await page.waitForURL("**/app/**", { timeout: 30000 });
}

/**
 * Fill billing address fields if they are present on the page.
 * These fields are required when collect_billing_address is enabled in Stripe Settings.
 *
 * @param {import('@playwright/test').Page} page - Playwright page object
 * @param {Object} address - Address details
 */
async function fillBillingAddress(page, address = {}) {
  const {
    line1 = "123 Test Street",
    line2 = "",
    city = "Test City",
    state = "TC",
    postalCode = "12345",
    country = "NL",
  } = address;

  // Check if billing address fields exist (collect_billing_address is enabled)
  const addressField = page.locator("#billing-address-line1");
  if (await addressField.isVisible({ timeout: 1000 }).catch(() => false)) {
    await addressField.fill(line1);

    if (line2) {
      await page.fill("#billing-address-line2", line2);
    }

    await page.fill("#billing-city", city);
    await page.fill("#billing-state", state);
    await page.fill("#billing-postal-code", postalCode);
    await page.fill("#billing-country", country);
  }
}

/**
 * Handle Stripe 3D Secure authentication modal.
 *
 * Stripe's 3DS modal creates a nested iframe structure:
 * - Outer iframe (full-screen overlay)
 *   - Dialog elements
 *     - Inner iframe (3DS test page with Complete/Fail buttons)
 *
 * @param {import('@playwright/test').Page} page - Playwright page object
 * @param {string} action - 'complete' or 'fail'
 * @param {number} timeout - Maximum wait time in ms
 * @returns {Promise<boolean>} True if action was performed successfully
 */
async function handle3DSAuthentication(
  page,
  action = "complete",
  timeout = 20000,
) {
  const buttonText = action === "complete" ? "Complete" : "Fail";

  // Wait for 3DS modal to appear (Stripe takes time to load it)
  await page.waitForTimeout(2000);

  // Strategy 1: Try using page.frames() to find the button directly
  // This is more reliable than nested frameLocator calls
  const startTime = Date.now();
  while (Date.now() - startTime < timeout) {
    const frames = page.frames();

    for (const frame of frames) {
      try {
        const button = frame.locator(`button:has-text("${buttonText}")`);
        const count = await button.count();
        if (count > 0) {
          const isVisible = await button.first().isVisible({ timeout: 500 });
          if (isVisible) {
            await button.first().click();
            return true;
          }
        }
      } catch {
        // Frame might not be ready, continue
      }
    }

    await page.waitForTimeout(500);
  }

  // Strategy 2: Fallback to nested frameLocator approach
  try {
    // Look for iframe that's NOT a Stripe Elements frame
    const stripeElementsSelector = 'iframe[name^="__privateStripeFrame"]';
    const allIframes = page.locator("iframe");
    const count = await allIframes.count();

    for (let i = 0; i < count; i++) {
      const iframe = allIframes.nth(i);
      const name = await iframe.getAttribute("name");

      // Skip Stripe Elements iframes
      if (name && name.startsWith("__privateStripeFrame")) {
        continue;
      }

      // Try to find the button in this iframe's nested structure
      const frameLocator = page.frameLocator(`iframe >> nth=${i}`);
      const innerFrame = frameLocator.frameLocator("iframe").first();

      try {
        const button = innerFrame.locator(`button:has-text("${buttonText}")`);
        await button.waitFor({ state: "visible", timeout: 2000 });
        await button.click();
        return true;
      } catch {
        // Not the right iframe, continue
      }
    }
  } catch {
    // Fallback failed
  }

  return false;
}

/**
 * Get Integration Request status for a payment.
 *
 * @param {import('@playwright/test').Page} page - Playwright page object
 * @param {string} referenceDocname - The reference document name
 * @returns {Promise<Object>} Integration Request data
 */
async function getIntegrationRequestStatus(page, referenceDocname) {
  const result = await page.evaluate(async (docname) => {
    const response = await fetch("/api/method/frappe.client.get_list", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Frappe-CSRF-Token": frappe.csrf_token,
      },
      body: JSON.stringify({
        doctype: "Integration Request",
        filters: { reference_docname: docname },
        fields: ["name", "status", "error", "output"],
        order_by: "creation desc",
        limit_page_length: 1,
      }),
    });
    const data = await response.json();
    return data.message[0] || null;
  }, referenceDocname);

  return result;
}

module.exports = {
  TEST_CARDS,
  TEST_CARD_DETAILS,
  fillStripeCardElement,
  fillBillingAddress,
  waitForStripeReady,
  handle3DSAuthentication,
  createTestSalesInvoice,
  deleteTestSalesInvoice,
  loginToFrappe,
  getIntegrationRequestStatus,
};
