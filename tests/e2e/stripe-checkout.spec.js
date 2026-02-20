/**
 * Stripe Checkout E2E Tests
 *
 * Test suite covering:
 * - Successful payment flow
 * - Declined card error handling
 * - 3D Secure (SCA) authentication
 * - Form validation and error states
 *
 * Required environment variables:
 * - STRIPE_TEST_SECRET_KEY      (sk_test_...)
 * - STRIPE_TEST_PUBLISHABLE_KEY (pk_test_...)
 *
 * Optional environment variables:
 * - TEST_INVOICE_NAME           (existing Sales Invoice to test with)
 * - TEST_PAYMENT_GATEWAY        (Payment Gateway name, default: Stripe-Stripe)
 * - TEST_SITE_URL               (site URL, default from playwright.config.js)
 */

const { test, expect } = require("@playwright/test");
const fs = require("fs");
const path = require("path");
const {
  TEST_CARDS,
  fillStripeCardElement,
  fillBillingAddress,
  waitForStripeReady,
  handle3DSAuthentication,
} = require("./support/stripe-test-helpers");

// Load test data from global setup
const TEST_DATA_FILE = path.join(__dirname, "support", ".test-data.json");
let testData = { invoiceName: null, paymentGateway: "Stripe-Stripe" };
try {
  testData = JSON.parse(fs.readFileSync(TEST_DATA_FILE, "utf-8"));
} catch (e) {
  // Fall back to environment variables
  testData.invoiceName = process.env.TEST_INVOICE_NAME;
  testData.paymentGateway = process.env.TEST_PAYMENT_GATEWAY || "Stripe-Stripe";
}

// Check for required Stripe credentials
const STRIPE_CREDENTIALS_AVAILABLE =
  process.env.STRIPE_TEST_PUBLISHABLE_KEY && process.env.STRIPE_TEST_SECRET_KEY;

// Log credential status at test startup (visible in test output)
if (!STRIPE_CREDENTIALS_AVAILABLE) {
  console.log("\n" + "=".repeat(70));
  console.log("⚠️  STRIPE E2E TESTS SKIPPED - Missing required credentials");
  console.log("=".repeat(70));
  console.log("\nTo run these tests, set the following environment variables:");
  console.log("  - STRIPE_TEST_PUBLISHABLE_KEY (pk_test_...)");
  console.log("  - STRIPE_TEST_SECRET_KEY (sk_test_...)");
  console.log("\nExample:");
  console.log("  STRIPE_TEST_PUBLISHABLE_KEY=pk_test_xxx \\");
  console.log("  STRIPE_TEST_SECRET_KEY=sk_test_xxx \\");
  console.log("  TEST_INVOICE_NAME=ACC-SINV-2026-00001 \\");
  console.log("  npx playwright test --project stripe-checkout");
  console.log("=".repeat(70) + "\n");
}

// Skip all tests in each describe block if credentials missing
test.beforeEach(async ({ page }, testInfo) => {
  test.skip(
    !STRIPE_CREDENTIALS_AVAILABLE,
    "Stripe credentials not configured (STRIPE_TEST_PUBLISHABLE_KEY and STRIPE_TEST_SECRET_KEY required)"
  );
});

test.describe("Stripe Checkout - CI Suite", () => {
  const testInvoice =
    testData.invoiceName || process.env.TEST_INVOICE_NAME || "TEST-INV-001";
  const paymentGateway =
    testData.paymentGateway ||
    process.env.TEST_PAYMENT_GATEWAY ||
    "Stripe-Stripe";

  test("successful payment with valid card", async ({ page }) => {
    // Navigate to Stripe checkout page with test invoice
    await page.goto(
      `/stripe_checkout?reference_doctype=Sales Invoice&reference_docname=${testInvoice}&payment_gateway=${paymentGateway}`
    );

    // Wait for page to load
    await expect(page.locator("#payment-form")).toBeVisible({ timeout: 15000 });

    // Verify amount is displayed
    await expect(page.locator(".amount")).toBeVisible();

    // Wait for Stripe Elements to load
    await waitForStripeReady(page);

    // Fill cardholder details
    await page.fill("#cardholder-name", "Test User");
    await page.fill("#cardholder-email", "test@example.com");

    // Fill billing address if required
    await fillBillingAddress(page);

    // Fill Stripe card element with success card
    await fillStripeCardElement(page, TEST_CARDS.SUCCESS);

    // Submit payment
    await page.click("#submit-button");

    // Wait for success indication - either:
    // 1. Success message appears (even if briefly)
    // 2. Form gets hidden (happens on success)
    // 3. Page redirects to payment-success
    await Promise.race([
      // Wait for form to be hidden (success hides the form)
      page.waitForFunction(
        () => {
          const form = document.getElementById("payment-form");
          return form && form.style.display === "none";
        },
        { timeout: 45000 }
      ),
      // Or wait for redirect
      page.waitForURL(/payment-success|\/app\//, { timeout: 45000 }),
    ]);

    // Verify we ended up in a success state (form hidden or redirected)
    const formHidden = await page.evaluate(() => {
      const form = document.getElementById("payment-form");
      return form && form.style.display === "none";
    });
    const redirected =
      page.url().includes("payment-success") || page.url().includes("/app/");

    expect(formHidden || redirected).toBeTruthy();
  });

  test("declined card shows error message", async ({ page }) => {
    // Navigate to Stripe checkout page
    await page.goto(
      `/stripe_checkout?reference_doctype=Sales Invoice&reference_docname=${testInvoice}&payment_gateway=${paymentGateway}`
    );

    // Wait for page to load
    await expect(page.locator("#payment-form")).toBeVisible({ timeout: 15000 });

    // Wait for Stripe Elements
    await waitForStripeReady(page);

    // Fill cardholder details
    await page.fill("#cardholder-name", "Test User");
    await page.fill("#cardholder-email", "test@example.com");

    // Fill billing address if required
    await fillBillingAddress(page);

    // Fill Stripe card element with declined card
    await fillStripeCardElement(page, TEST_CARDS.DECLINE);

    // Submit payment
    await page.click("#submit-button");

    // Wait for error message
    await expect(page.locator("#payment-message.error")).toBeVisible({
      timeout: 30000,
    });

    // Verify error mentions decline
    await expect(page.locator("#payment-message.error")).toContainText(
      /declined|fail|error/i
    );

    // Verify form is still visible (user can retry)
    await expect(page.locator("#payment-form")).toBeVisible();

    // Verify submit button is re-enabled
    await expect(page.locator("#submit-button")).toBeEnabled({ timeout: 5000 });
  });

  test("page displays correct payment information", async ({ page }) => {
    // Navigate to checkout
    await page.goto(
      `/stripe_checkout?reference_doctype=Sales Invoice&reference_docname=${testInvoice}&payment_gateway=${paymentGateway}`
    );

    // Wait for page
    await expect(page.locator("#payment-form")).toBeVisible({ timeout: 15000 });

    // Verify amount is displayed (should show the invoice amount)
    await expect(page.locator(".amount")).toBeVisible();
    await expect(page.locator(".amount")).toContainText(/€|EUR|\$/);

    // Verify Stripe Elements container exists
    await expect(page.locator("#card-element")).toBeVisible();

    // Verify submit button exists and shows amount
    await expect(page.locator("#submit-button")).toBeVisible();
    await expect(page.locator("#submit-button")).toContainText(/Pay/i);
  });

  test("form validation - empty cardholder name", async ({ page }) => {
    // Navigate to checkout
    await page.goto(
      `/stripe_checkout?reference_doctype=Sales Invoice&reference_docname=${testInvoice}&payment_gateway=${paymentGateway}`
    );

    await expect(page.locator("#payment-form")).toBeVisible({ timeout: 15000 });
    await waitForStripeReady(page);

    // Fill card but leave name empty
    await page.fill("#cardholder-email", "test@example.com");
    await fillStripeCardElement(page, TEST_CARDS.SUCCESS);

    // Check if browser validation prevents submission (HTML5 required attribute)
    // or if our custom validation kicks in
    const nameField = page.locator("#cardholder-name");
    const isRequired = await nameField.getAttribute("required");

    if (isRequired !== null) {
      // HTML5 validation - submit should not proceed
      await page.click("#submit-button");

      // Form should still be visible (submission blocked)
      await expect(page.locator("#payment-form")).toBeVisible();
    }
  });
});

test.describe("Stripe Checkout - 3D Secure Authentication", () => {
  const testInvoice =
    testData.invoiceName || process.env.TEST_INVOICE_NAME || "TEST-INV-001";
  const paymentGateway =
    testData.paymentGateway ||
    process.env.TEST_PAYMENT_GATEWAY ||
    "Stripe-Stripe";

  test("3D Secure authentication completes successfully", async ({ page }) => {
    // Navigate to Stripe checkout page
    await page.goto(
      `/stripe_checkout?reference_doctype=Sales Invoice&reference_docname=${testInvoice}&payment_gateway=${paymentGateway}`
    );

    // Wait for page to load
    await expect(page.locator("#payment-form")).toBeVisible({ timeout: 15000 });
    await waitForStripeReady(page);

    // Fill cardholder details
    await page.fill("#cardholder-name", "Test 3DS User");
    await page.fill("#cardholder-email", "test3ds@example.com");

    // Fill billing address if required
    await fillBillingAddress(page);

    // Fill Stripe card element with 3DS test card
    await fillStripeCardElement(page, TEST_CARDS.REQUIRES_3DS);

    // Submit payment
    await page.click("#submit-button");

    // Handle 3D Secure authentication modal
    const authSuccess = await handle3DSAuthentication(page, "complete", 20000);
    if (!authSuccess) {
      console.log("Warning: Could not interact with 3DS modal");
    }

    // Wait for success indication (same as regular payment test)
    await Promise.race([
      page.waitForFunction(
        () => {
          const form = document.getElementById("payment-form");
          return form && form.style.display === "none";
        },
        { timeout: 60000 } // Longer timeout for 3DS flow
      ),
      page.waitForURL(/payment-success|\/app\//, { timeout: 60000 }),
      // Also check for success message appearing
      page.waitForSelector("#payment-message.success", {
        state: "attached",
        timeout: 60000,
      }),
    ]);

    // Verify success state
    const formHidden = await page.evaluate(() => {
      const form = document.getElementById("payment-form");
      return form && form.style.display === "none";
    });
    const redirected =
      page.url().includes("payment-success") || page.url().includes("/app/");
    const successMessage = await page
      .locator("#payment-message.success")
      .isVisible()
      .catch(() => false);

    expect(formHidden || redirected || successMessage).toBeTruthy();
  });

  test("3D Secure authentication failure shows error", async ({ page }) => {
    // Navigate to Stripe checkout page
    await page.goto(
      `/stripe_checkout?reference_doctype=Sales Invoice&reference_docname=${testInvoice}&payment_gateway=${paymentGateway}`
    );

    // Wait for page to load
    await expect(page.locator("#payment-form")).toBeVisible({ timeout: 15000 });
    await waitForStripeReady(page);

    // Fill cardholder details
    await page.fill("#cardholder-name", "Test 3DS Fail User");
    await page.fill("#cardholder-email", "test3dsfail@example.com");

    // Fill billing address if required
    await fillBillingAddress(page);

    // Fill Stripe card element with 3DS fail test card
    await fillStripeCardElement(page, TEST_CARDS.FAILS_3DS);

    // Submit payment
    await page.click("#submit-button");

    // Handle 3D Secure authentication modal - click Fail button
    const authResult = await handle3DSAuthentication(page, "fail", 20000);
    if (!authResult) {
      console.log("Warning: Could not interact with 3DS modal");
    }

    // After 3DS failure, we should see an error message
    await expect(page.locator("#payment-message.error")).toBeVisible({
      timeout: 45000,
    });

    // Verify error message mentions authentication failure
    await expect(page.locator("#payment-message.error")).toContainText(
      /authentication|failed|declined|error|unable/i
    );

    // Form should still be visible for retry
    await expect(page.locator("#payment-form")).toBeVisible();
  });
});

test.describe("Stripe Checkout - Error Scenarios", () => {
  const paymentGateway =
    testData.paymentGateway ||
    process.env.TEST_PAYMENT_GATEWAY ||
    "Stripe-Stripe";

  test("invalid reference document shows error", async ({ page }) => {
    // Navigate with non-existent reference (include payment_gateway to test actual error handling)
    await page.goto(
      `/stripe_checkout?reference_doctype=Sales Invoice&reference_docname=NONEXISTENT-12345&payment_gateway=${paymentGateway}`
    );

    // Should show error page or redirect to error
    const pageContent = await page.content();
    const hasError =
      pageContent.includes("error") ||
      pageContent.includes("Error") ||
      pageContent.includes("not found") ||
      pageContent.includes("Invalid");

    // Verify either error message is shown or redirect occurred
    expect(
      hasError || page.url().includes("message") || page.url().includes("error")
    ).toBeTruthy();
  });

  test("missing reference parameters shows error", async ({ page }) => {
    // Navigate without required parameters
    await page.goto("/stripe_checkout");

    // Should show error message
    const pageContent = await page.content();
    const hasError =
      pageContent.includes("Missing") ||
      pageContent.includes("required") ||
      pageContent.includes("error");

    expect(hasError || page.url().includes("message")).toBeTruthy();
  });
});
