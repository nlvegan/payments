"""Test-mode credential provisioning for live Stripe integration tests.

NOT test_-prefixed on purpose: this module is a helper, not a test module, so
Frappe's test discovery does not collect it.

Reads Stripe TEST keys from site config (set in common_site_config.json):

    "stripe_test_secret_key": "sk_test_...",
    "stripe_test_publishable_key": "pk_test_..."

and configures a dedicated `StripeLiveTest` Stripe Settings document (Stripe
Settings is NOT a Single doctype). Returns the settings doc name, or None when
credentials are missing/invalid so the live test class can skip cleanly.
"""

import frappe

TEST_SETTINGS_NAME = "StripeLiveTest"
# Self-chosen webhook secret so the webhook test can sign its own payloads.
# No real whsec_ provisioning is required.
TEST_WEBHOOK_SECRET = "whsec_test_livetests_fixed_secret"


def ensure_stripe_test_credentials() -> str | None:
	secret_key = frappe.conf.get("stripe_test_secret_key")
	publishable_key = frappe.conf.get("stripe_test_publishable_key")

	if not secret_key or not secret_key.startswith("sk_test_"):
		return None
	if not publishable_key or not publishable_key.startswith("pk_test_"):
		return None

	if frappe.db.exists("Stripe Settings", TEST_SETTINGS_NAME):
		settings = frappe.get_doc("Stripe Settings", TEST_SETTINGS_NAME)
	else:
		settings = frappe.new_doc("Stripe Settings")
		settings.gateway_name = TEST_SETTINGS_NAME

	settings.publishable_key = publishable_key
	settings.secret_key = secret_key
	settings.webhook_secret = TEST_WEBHOOK_SECRET
	# reqd fields + on_update credential validation: bypass both for the test row.
	settings.flags.ignore_mandatory = True
	settings.flags.ignore_validate = True
	settings.save(ignore_permissions=True)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit - persist the provisioned test settings so live tests in other transactions can use it

	frappe.clear_document_cache("Stripe Settings", TEST_SETTINGS_NAME)
	return TEST_SETTINGS_NAME
