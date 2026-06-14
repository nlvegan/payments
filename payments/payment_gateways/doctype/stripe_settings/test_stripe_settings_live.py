"""Live Stripe test-mode integration tests.

Skip-guarded: when `stripe_test_secret_key` / `stripe_test_publishable_key` are
absent from site config, the whole class skips. To run live, add to
common_site_config.json:

    "stripe_test_secret_key": "sk_test_...",
    "stripe_test_publishable_key": "pk_test_..."

CI: inject the same keys from a CI secret; absent => skip (same as Mollie).
"""

import hashlib
import hmac
import json
import time

import frappe
import stripe
from frappe.tests import IntegrationTestCase

from payments.controllers import PaymentController
from payments.payment_gateways.doctype.stripe_settings.stripe_test_helper import (
	TEST_SETTINGS_NAME,
	TEST_WEBHOOK_SECRET,
	ensure_stripe_test_credentials,
)
from payments.types import GatewayProcessingResponse, TxData


class TestStripeSettingsLive(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.settings_name = ensure_stripe_test_credentials()
		cls.has_credentials = bool(cls.settings_name)
		cls.gateway = f"Stripe-{TEST_SETTINGS_NAME}"
		# Start each run from a clean slate: purge any test mandates left by a prior
		# run so _ensure_stripe_customer never reuses a customer already deleted on
		# Stripe. Raw delete bypasses PSL-link checks and on_trash.
		if cls.has_credentials:
			frappe.db.delete("Stripe Mandate", {"gateway_controller": TEST_SETTINGS_NAME})
			frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		# Best-effort: drop the test settings doc so it does not leak into other
		# suites' stripe_webhook() get_all("Stripe Settings", limit=1) selection.
		try:
			if cls.has_credentials and frappe.db.exists("Stripe Settings", TEST_SETTINGS_NAME):
				frappe.delete_doc(
					"Stripe Settings", TEST_SETTINGS_NAME, force=True, ignore_permissions=True
				)
				frappe.db.commit()
		except Exception:
			pass
		super().tearDownClass()

	def setUp(self):
		if not self.has_credentials:
			self.skipTest("No Stripe test key configured (stripe_test_secret_key)")
		self._customer_ids = []
		self._pi_ids = []
		self._mandate_names = []

	def tearDown(self):
		api_key = None
		try:
			api_key = self._api_key()
		except Exception:
			pass
		# Cancel any cancelable PaymentIntents (succeeded ones cannot be canceled; harmless).
		for pi_id in getattr(self, "_pi_ids", []):
			try:
				stripe.PaymentIntent.cancel(pi_id, api_key=api_key)
			except Exception:
				pass
		# Delete created customers (cascades payment-method detach).
		for cid in getattr(self, "_customer_ids", []):
			try:
				stripe.Customer.delete(cid, api_key=api_key)
			except Exception:
				pass
		# Purge test mandates via raw delete (bypasses PSL-link checks / on_trash)
		# so no stale Active mandate survives into the next test or run.
		try:
			frappe.db.delete("Stripe Mandate", {"gateway_controller": TEST_SETTINGS_NAME})
			frappe.db.commit()
		except Exception:
			pass
		super().tearDown()

	def test_smoke_settings_configured(self):
		self.assertTrue(frappe.db.exists("Stripe Settings", TEST_SETTINGS_NAME))
		self.assertTrue(frappe.db.exists("Payment Gateway", self.gateway))

	# ---- helpers -------------------------------------------------------
	def _payer(self) -> str:
		# Unique per test method so _ensure_stripe_customer never reuses a
		# customer/mandate across tests (test isolation).
		return f"livetest+{self._testMethodName}@example.com"

	def _tx(self, *, save_mandate: bool) -> TxData:
		return TxData(
			amount=10.0,
			currency="EUR",
			reference_doctype="User",
			reference_docname="Administrator",
			# Use email_id: the PSL minimizes payer_contact to an allowlist
			# ({full_name, email_id, phone, mobile_no}); a bare "email" key is stripped.
			payer_contact={"email_id": self._payer()},
			payer_address={},
			loyalty_points=None,
			discount_amount=None,
			save_mandate=save_mandate,
		)

	def _api_key(self) -> str:
		return frappe.get_doc("Stripe Settings", self.settings_name).get_stripe_api_key()

	def _mandate(self):
		return frappe.get_doc(
			"Stripe Mandate",
			{"gateway_controller": TEST_SETTINGS_NAME, "payer": self._payer()},
		)

	def _create_and_confirm_mandate(self) -> str:
		"""Run the real charge+save flow to a succeeded mandate; return PSL name."""
		_controller, psl_name = PaymentController.initiate(
			tx_data=self._tx(save_mandate=True), gateway=self.gateway
		)
		# proceed runs the app's real _initiate_charge (PaymentIntent.create)
		proceeded = PaymentController.proceed(psl_name)
		pi_id = proceeded.payload["payment_intent_id"]
		self._pi_ids.append(pi_id)

		# Simulate the browser: confirm server-side. return_url is required because
		# the app enables automatic_payment_methods without allow_redirects="never".
		confirmed = stripe.PaymentIntent.confirm(
			pi_id,
			payment_method="pm_card_visa",
			return_url="https://example.com/return",
			api_key=self._api_key(),
		)
		self.assertEqual(confirmed.status, "succeeded")
		if confirmed.customer:
			self._customer_ids.append(confirmed.customer)

		# Feed back through the framework -> real _validate_response (re-retrieves the
		# succeeded intent) -> _process_response_for_charge -> _persist_mandate.
		PaymentController.process_response(
			psl_name,
			GatewayProcessingResponse(
				hash=None,
				message=None,
				payload={"id": pi_id, "payment_intent_id": pi_id, "status": "succeeded"},
			),
		)
		return psl_name

	# ---- tests ---------------------------------------------------------
	def test_charge_saves_usable_mandate(self):
		self._create_and_confirm_mandate()
		mandate = self._mandate()
		self._mandate_names.append(mandate.name)
		self.assertTrue(mandate.is_usable())
		self.assertTrue(mandate.customer_id.startswith("cus_"))
		self.assertTrue(mandate.payment_method_id.startswith("pm_"))

	def test_off_session_charge_succeeds(self):
		self._create_and_confirm_mandate()
		mandate = self._mandate()
		self._mandate_names.append(mandate.name)

		result = PaymentController.charge_mandate(
			mandate=mandate, tx_data=self._tx(save_mandate=False), gateway=self.gateway
		)
		self.assertEqual(result.indicator_color, "green")

	def test_revoke_detaches_and_is_idempotent(self):
		self._create_and_confirm_mandate()
		mandate = self._mandate()
		self._mandate_names.append(mandate.name)

		mandate.revoke()
		mandate.reload()
		self.assertEqual(mandate.status, "Revoked")
		# Second revoke: payment method already detached -> InvalidRequestError caught, no raise.
		mandate.revoke()

	def _sign(self, payload: str, secret: str, timestamp: int) -> str:
		signed = f"{timestamp}.{payload}".encode()
		sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
		return f"t={timestamp},v1={sig}"

	def test_webhook_construct_event_verifies_signature(self):
		payload = json.dumps({"id": "evt_test", "type": "payment_intent.succeeded"})
		ts = int(time.time())
		header = self._sign(payload, TEST_WEBHOOK_SECRET, ts)

		event = stripe.Webhook.construct_event(payload, header, TEST_WEBHOOK_SECRET)
		self.assertEqual(event["type"], "payment_intent.succeeded")

		bad = header.replace("v1=", "v1=deadbeef")
		with self.assertRaises(stripe.error.SignatureVerificationError):
			stripe.Webhook.construct_event(payload, bad, TEST_WEBHOOK_SECRET)
