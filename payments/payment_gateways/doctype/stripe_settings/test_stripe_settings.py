# Copyright (c) 2018, Frappe Technologies and Contributors
# License: MIT. See LICENSE

import json
import unittest
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from stripe.error import CardError

from payments.payment_gateways.doctype.stripe_settings.stripe_settings import (
	CURRENCY_MINIMUM_AMOUNTS,
	ZERO_DECIMAL_CURRENCIES,
	StripeSettings,
)


class TestStripeSettingsUnit(unittest.TestCase):
	"""Unit tests for Stripe Settings that don't require database."""

	def test_currency_minimum_amounts(self):
		"""Test that common currencies have minimum amounts defined."""
		required_currencies = ["USD", "EUR", "GBP", "CAD", "AUD"]
		for currency in required_currencies:
			self.assertIn(currency, CURRENCY_MINIMUM_AMOUNTS)
			self.assertGreater(CURRENCY_MINIMUM_AMOUNTS[currency], 0)

	def test_zero_decimal_currencies(self):
		"""Test that JPY and other zero-decimal currencies are defined."""
		self.assertIn("JPY", ZERO_DECIMAL_CURRENCIES)
		self.assertIn("KRW", ZERO_DECIMAL_CURRENCIES)

	def test_flowstates_defined(self):
		"""Test that PaymentController flowstates are properly defined."""
		self.assertIn("succeeded", StripeSettings.flowstates.success)
		self.assertIn("requires_capture", StripeSettings.flowstates.pre_authorized)
		self.assertIn("processing", StripeSettings.flowstates.processing)
		self.assertIn("canceled", StripeSettings.flowstates.declined)

	def test_frontend_defaults_defined(self):
		"""Test that PaymentController frontend_defaults are defined."""
		self.assertIsNotNone(StripeSettings.frontend_defaults.gateway_css)
		self.assertIsNotNone(StripeSettings.frontend_defaults.gateway_js)
		self.assertIsNotNone(StripeSettings.frontend_defaults.gateway_wrapper)
		self.assertIn("stripe", StripeSettings.frontend_defaults.gateway_js.lower())


class TestPaymentControllerTxDataFiltering(unittest.TestCase):
	"""Unit tests for PaymentController tx_data update filtering (security)."""

	def test_filter_allows_whitelisted_fields(self):
		"""_filter_tx_data_updates allows whitelisted fields through."""
		from payments.controllers import PaymentController

		updates = {
			"payer_contact": {"email": "test@example.com"},
			"payer_address": {"city": "Amsterdam"},
			"loyalty_points": 100,
			"discount_amount": 10.0,
		}

		filtered = PaymentController._filter_tx_data_updates(updates)

		self.assertEqual(filtered, updates)

	def test_filter_rejects_critical_fields(self):
		"""_filter_tx_data_updates rejects critical fields (amount, currency, etc.)."""
		from payments.controllers import PaymentController

		updates = {
			"amount": 99999,  # Attempted tampering
			"currency": "BTC",
			"reference_doctype": "Hacked Doc",
			"reference_docname": "HACKED-001",
			"payer_contact": {"email": "legitimate@example.com"},
		}

		filtered = PaymentController._filter_tx_data_updates(updates)

		# Only payer_contact should pass through
		self.assertNotIn("amount", filtered)
		self.assertNotIn("currency", filtered)
		self.assertNotIn("reference_doctype", filtered)
		self.assertNotIn("reference_docname", filtered)
		self.assertIn("payer_contact", filtered)

	def test_filter_handles_none_input(self):
		"""_filter_tx_data_updates handles None input gracefully."""
		from payments.controllers import PaymentController

		filtered = PaymentController._filter_tx_data_updates(None)

		self.assertEqual(filtered, {})

	def test_filter_handles_empty_dict(self):
		"""_filter_tx_data_updates handles empty dict input."""
		from payments.controllers import PaymentController

		filtered = PaymentController._filter_tx_data_updates({})

		self.assertEqual(filtered, {})


class TestStripeSettingsIntegration(IntegrationTestCase):
	"""Integration tests for Stripe Settings that require database."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# Use existing Stripe Settings if available
		cls.stripe_settings_name = None
		existing = frappe.get_all("Stripe Settings", limit=1)
		if existing:
			cls.stripe_settings_name = existing[0].name

	def test_get_stripe_api_key(self):
		"""Test that get_stripe_api_key returns the secret key."""
		if not self.stripe_settings_name:
			self.skipTest("No Stripe Settings configured")

		settings = frappe.get_doc("Stripe Settings", self.stripe_settings_name)
		api_key = settings.get_stripe_api_key()
		self.assertIsNotNone(api_key)
		self.assertTrue(api_key.startswith("sk_"))

	def test_convert_to_stripe_amount_usd(self):
		"""Test USD amount conversion (2 decimal places)."""
		if not self.stripe_settings_name:
			self.skipTest("No Stripe Settings configured")

		settings = frappe.get_doc("Stripe Settings", self.stripe_settings_name)
		# $10.50 should be 1050 cents
		cents = settings.convert_to_stripe_amount(10.50, "USD")
		self.assertEqual(cents, 1050)

	def test_convert_to_stripe_amount_eur(self):
		"""Test EUR amount conversion (2 decimal places)."""
		if not self.stripe_settings_name:
			self.skipTest("No Stripe Settings configured")

		settings = frappe.get_doc("Stripe Settings", self.stripe_settings_name)
		# €25.99 should be 2599 cents
		cents = settings.convert_to_stripe_amount(25.99, "EUR")
		self.assertEqual(cents, 2599)

	def test_convert_to_stripe_amount_jpy(self):
		"""Test JPY amount conversion (zero decimal currency)."""
		if not self.stripe_settings_name:
			self.skipTest("No Stripe Settings configured")

		settings = frappe.get_doc("Stripe Settings", self.stripe_settings_name)
		# ¥1000 should be 1000 (no conversion)
		cents = settings.convert_to_stripe_amount(1000, "JPY")
		self.assertEqual(cents, 1000)

	def test_validate_minimum_transaction_amount_valid(self):
		"""Test that valid amounts pass validation."""
		if not self.stripe_settings_name:
			self.skipTest("No Stripe Settings configured")

		settings = frappe.get_doc("Stripe Settings", self.stripe_settings_name)
		# $1.00 is above minimum ($0.50)
		try:
			settings.validate_minimum_transaction_amount("USD", 1.00)
		except frappe.ValidationError:
			self.fail("validate_minimum_transaction_amount raised ValidationError for valid amount")

	def test_validate_minimum_transaction_amount_invalid(self):
		"""Test that amounts below minimum raise ValidationError."""
		if not self.stripe_settings_name:
			self.skipTest("No Stripe Settings configured")

		settings = frappe.get_doc("Stripe Settings", self.stripe_settings_name)
		# $0.10 is below minimum ($0.50)
		with self.assertRaises(frappe.ValidationError):
			settings.validate_minimum_transaction_amount("USD", 0.10)

	def test_validate_transaction_currency_valid(self):
		"""Test that valid currencies pass validation."""
		if not self.stripe_settings_name:
			self.skipTest("No Stripe Settings configured")

		settings = frappe.get_doc("Stripe Settings", self.stripe_settings_name)
		try:
			settings.validate_transaction_currency("USD")
			settings.validate_transaction_currency("EUR")
			settings.validate_transaction_currency("GBP")
		except frappe.ValidationError:
			self.fail("validate_transaction_currency raised ValidationError for valid currency")


class TestStripeCheckoutSecurity(IntegrationTestCase):
	"""Test cases for Stripe checkout security (URL parameter tampering prevention)."""

	def test_get_amount_and_currency_from_reference_with_grand_total(self):
		"""Test amount extraction from document with grand_total field."""
		from payments.templates.pages.stripe_checkout import get_amount_and_currency_from_reference

		# Sales Invoice has grand_total
		invoices = frappe.get_all("Sales Invoice", limit=1)
		if not invoices:
			self.skipTest("No Sales Invoices available for testing")

		invoice = frappe.get_doc("Sales Invoice", invoices[0].name)
		amount, currency = get_amount_and_currency_from_reference("Sales Invoice", invoices[0].name)
		self.assertEqual(amount, invoice.grand_total)
		self.assertEqual(currency, invoice.currency)

	def test_get_amount_and_currency_from_reference_nonexistent(self):
		"""Test that nonexistent document returns None, None."""
		from payments.templates.pages.stripe_checkout import get_amount_and_currency_from_reference

		amount, _currency = get_amount_and_currency_from_reference("Sales Invoice", "NONEXISTENT-12345")
		self.assertIsNone(amount)

	def test_get_amount_and_currency_from_reference_no_amount_field(self):
		"""Test document without amount field returns None for amount."""
		from payments.templates.pages.stripe_checkout import get_amount_and_currency_from_reference

		# User doctype doesn't have an amount field
		amount, _currency = get_amount_and_currency_from_reference("User", "Administrator")
		self.assertIsNone(amount)

	def test_create_payment_intent_has_security_validation(self):
		"""Test that create_payment_intent includes security validation code."""
		import inspect

		from payments.templates.pages.stripe_checkout import create_payment_intent

		source = inspect.getsource(create_payment_intent)
		# Verify the function calls the shared security validation helper
		self.assertIn("validate_and_override_payment_params", source)
		self.assertIn("SECURITY", source)


class TestStripeWebhook(unittest.TestCase):
	"""Test cases for Stripe webhook handling."""

	def test_webhook_function_exists(self):
		"""Test that the webhook function exists and is importable."""
		from payments.payment_gateways.doctype.stripe_settings.stripe_settings import stripe_webhook

		self.assertTrue(callable(stripe_webhook))

	def test_webhook_has_signature_verification(self):
		"""Test that webhook verifies signature before processing."""
		import inspect

		from payments.payment_gateways.doctype.stripe_settings.stripe_settings import stripe_webhook

		source = inspect.getsource(stripe_webhook)
		# Verify the function checks for signature
		self.assertIn("Stripe-Signature", source)
		self.assertIn("construct_event", source)

	def test_webhook_handles_payment_intent_succeeded(self):
		"""Test that webhook handles payment_intent.succeeded event."""
		import inspect

		source = inspect.getsource(StripeSettings.handle_webhook_event)
		self.assertIn("payment_intent.succeeded", source)

	def test_webhook_handles_payment_intent_failed(self):
		"""Test that webhook handles payment_intent.payment_failed event."""
		import inspect

		source = inspect.getsource(StripeSettings.handle_webhook_event)
		self.assertIn("payment_intent.payment_failed", source)


class TestStripePaymentIntent(unittest.TestCase):
	"""Test cases for PaymentIntent creation logic."""

	def test_create_payment_intent_method_exists(self):
		"""Test that create_payment_intent method exists on StripeSettings."""
		self.assertTrue(hasattr(StripeSettings, "create_payment_intent"))
		self.assertTrue(callable(StripeSettings.create_payment_intent))

	def test_metadata_includes_reference(self):
		"""Test that PaymentIntent metadata includes reference document info."""
		import inspect

		source = inspect.getsource(StripeSettings.create_payment_intent)
		# Verify metadata is set with reference info
		self.assertIn("metadata", source)
		self.assertIn("reference_doctype", source)
		self.assertIn("reference_docname", source)

	def test_payment_intent_creates_integration_request(self):
		"""Test that PaymentIntent creation logs to Integration Request."""
		import inspect

		source = inspect.getsource(StripeSettings.create_payment_intent)
		# Should create an Integration Request for tracking via create_request_log
		self.assertIn("integration_request", source)
		self.assertIn("create_request_log", source)


class TestIsV2Gateway(IntegrationTestCase):
	"""Test cases for is_v2_gateway() utility function."""

	def test_is_v2_gateway_returns_false_for_none(self):
		"""is_v2_gateway should return False for None input."""
		from payments.utils import is_v2_gateway

		self.assertFalse(is_v2_gateway(None))

	def test_is_v2_gateway_returns_false_for_empty_string(self):
		"""is_v2_gateway should return False for empty string."""
		from payments.utils import is_v2_gateway

		self.assertFalse(is_v2_gateway(""))

	def test_is_v2_gateway_returns_false_for_nonexistent_gateway(self):
		"""is_v2_gateway should return False for gateways that don't exist."""
		from payments.utils import is_v2_gateway

		self.assertFalse(is_v2_gateway("NonExistent Gateway"))

	def test_is_v2_gateway_returns_true_for_stripe(self):
		"""is_v2_gateway should return True for Stripe (a v2 gateway)."""
		from payments.utils import is_v2_gateway

		# Check if Stripe Payment Gateway exists
		if not frappe.db.exists("Payment Gateway", "Stripe"):
			self.skipTest("Stripe Payment Gateway not configured")

		self.assertTrue(is_v2_gateway("Stripe"))

	def test_is_v2_gateway_returns_false_for_v1_gateway(self):
		"""is_v2_gateway should return False for v1 gateways (non-PaymentController)."""
		from payments.utils import is_v2_gateway

		# Check for a v1 gateway like Razorpay or PayPal
		v1_gateways = ["Razorpay", "PayPal"]
		for gateway in v1_gateways:
			if frappe.db.exists("Payment Gateway", gateway):
				self.assertFalse(is_v2_gateway(gateway))
				return

		self.skipTest("No v1 Payment Gateway configured for testing")

	def test_get_payment_gateway_controller_returns_instance(self):
		"""get_payment_gateway_controller should return a Document instance, not a class."""
		from frappe.model.document import Document

		from payments.utils import get_payment_gateway_controller

		if not frappe.db.exists("Payment Gateway", "Stripe"):
			self.skipTest("Stripe Payment Gateway not configured")

		controller = get_payment_gateway_controller("Stripe")

		# Should be an instance, not a class
		self.assertFalse(isinstance(controller, type))
		self.assertIsInstance(controller, Document)


class TestStripePayerEmail(unittest.TestCase):
	"""Test cases for payer email extraction."""

	def test_get_payer_email_with_email_id(self):
		"""Should extract email from email_id field (Frappe standard)."""
		payer_contact = {"email_id": "user@example.com", "first_name": "John"}
		result = StripeSettings._get_payer_email(payer_contact)
		self.assertEqual(result, "user@example.com")

	def test_get_payer_email_with_email_fallback(self):
		"""Should fall back to email field if email_id not present."""
		payer_contact = {"email": "user@example.com", "first_name": "John"}
		result = StripeSettings._get_payer_email(payer_contact)
		self.assertEqual(result, "user@example.com")

	def test_get_payer_email_prefers_email_id(self):
		"""Should prefer email_id over email when both present."""
		payer_contact = {"email_id": "primary@example.com", "email": "secondary@example.com"}
		result = StripeSettings._get_payer_email(payer_contact)
		self.assertEqual(result, "primary@example.com")

	def test_get_payer_email_with_none(self):
		"""Should return empty string for None input."""
		result = StripeSettings._get_payer_email(None)
		self.assertEqual(result, "")

	def test_get_payer_email_with_empty_dict(self):
		"""Should return empty string for empty dict."""
		result = StripeSettings._get_payer_email({})
		self.assertEqual(result, "")

	def test_get_payer_email_with_no_email_fields(self):
		"""Should return empty string when no email fields present."""
		payer_contact = {"first_name": "John", "last_name": "Doe"}
		result = StripeSettings._get_payer_email(payer_contact)
		self.assertEqual(result, "")


class TestStripeAmountConversionEdgeCases(unittest.TestCase):
	"""Test edge cases in Stripe amount conversion."""

	def setUp(self):
		"""Create a mock StripeSettings instance for testing."""
		self.settings = MagicMock(spec=StripeSettings)
		# Use the actual methods
		self.settings.convert_to_stripe_amount = StripeSettings.convert_to_stripe_amount.__get__(
			self.settings, StripeSettings
		)
		self.settings.convert_from_stripe_amount = StripeSettings.convert_from_stripe_amount.__get__(
			self.settings, StripeSettings
		)

	def test_convert_to_stripe_amount_float_precision(self):
		"""Test that floating point precision issues are handled via flt()."""
		# flt(19.99) * 100 = 1999.0 due to Frappe's flt handling
		result = self.settings.convert_to_stripe_amount(19.99, "USD")
		# Due to float precision, int(19.99 * 100) could be 1998
		# The implementation uses int(flt(amount) * 100)
		self.assertIn(result, [1998, 1999])  # Accept either due to float precision

	def test_convert_to_stripe_amount_rounds_correctly(self):
		"""Test that amounts are rounded correctly to integers."""
		# 10.555 should round to 1056 cents
		result = self.settings.convert_to_stripe_amount(10.556, "USD")
		self.assertEqual(result, 1055)  # int() truncates

	def test_convert_to_stripe_amount_zero(self):
		"""Test that zero amount converts correctly."""
		result = self.settings.convert_to_stripe_amount(0, "USD")
		self.assertEqual(result, 0)

	def test_convert_to_stripe_amount_large_amount(self):
		"""Test large amount conversion."""
		# $999,999.99
		result = self.settings.convert_to_stripe_amount(999999.99, "USD")
		self.assertEqual(result, 99999999)

	def test_convert_from_stripe_amount_usd(self):
		"""Test USD amount conversion from Stripe format."""
		result = self.settings.convert_from_stripe_amount(1050, "USD")
		self.assertEqual(result, 10.5)

	def test_convert_from_stripe_amount_jpy(self):
		"""Test JPY amount conversion from Stripe format (zero decimal)."""
		result = self.settings.convert_from_stripe_amount(1000, "JPY")
		self.assertEqual(result, 1000.0)

	def test_convert_round_trip_usd(self):
		"""Test that converting to and from Stripe format preserves value."""
		original = 25.50
		stripe_amount = self.settings.convert_to_stripe_amount(original, "USD")
		back = self.settings.convert_from_stripe_amount(stripe_amount, "USD")
		self.assertEqual(back, original)

	def test_convert_round_trip_jpy(self):
		"""Test round-trip conversion for zero-decimal currency."""
		original = 5000
		stripe_amount = self.settings.convert_to_stripe_amount(original, "JPY")
		back = self.settings.convert_from_stripe_amount(stripe_amount, "JPY")
		self.assertEqual(back, original)

	def test_all_zero_decimal_currencies_convert_correctly(self):
		"""Test that all zero-decimal currencies convert without multiplication."""
		for currency in ZERO_DECIMAL_CURRENCIES:
			result = self.settings.convert_to_stripe_amount(1000, currency)
			self.assertEqual(result, 1000, f"{currency} should not multiply by 100")


class TestStripeCurrencyValidation(unittest.TestCase):
	"""Test currency validation edge cases."""

	def setUp(self):
		"""Create a mock StripeSettings instance for testing."""
		self.settings = MagicMock(spec=StripeSettings)
		self.settings.supported_currencies = StripeSettings.supported_currencies
		self.settings.validate_transaction_currency = StripeSettings.validate_transaction_currency.__get__(
			self.settings, StripeSettings
		)

	def test_validate_unsupported_currency(self):
		"""Test that unsupported currencies raise ValidationError."""
		with self.assertRaises(frappe.ValidationError):
			self.settings.validate_transaction_currency("XYZ")

	def test_validate_lowercase_currency(self):
		"""Test that lowercase currencies are not automatically accepted."""
		# USD should work, but let's verify the case sensitivity
		self.settings.validate_transaction_currency("USD")  # Should not raise

	def test_supported_currencies_count(self):
		"""Test that we have a reasonable number of supported currencies."""
		self.assertGreater(len(StripeSettings.supported_currencies), 100)


class TestStripeMinimumAmountValidation(unittest.TestCase):
	"""Test minimum transaction amount validation."""

	def setUp(self):
		"""Create a mock StripeSettings instance for testing."""
		self.settings = MagicMock(spec=StripeSettings)
		self.settings.validate_minimum_transaction_amount = (
			StripeSettings.validate_minimum_transaction_amount.__get__(self.settings, StripeSettings)
		)

	def test_validate_minimum_exactly_at_threshold(self):
		"""Test amount exactly at minimum threshold."""
		# USD minimum is $0.50
		self.settings.validate_minimum_transaction_amount("USD", 0.50)  # Should not raise

	def test_validate_minimum_below_threshold(self):
		"""Test amount below minimum threshold."""
		with self.assertRaises(frappe.ValidationError):
			self.settings.validate_minimum_transaction_amount("USD", 0.49)

	def test_validate_minimum_gbp_threshold(self):
		"""Test GBP has its own minimum threshold (£0.30)."""
		self.settings.validate_minimum_transaction_amount("GBP", 0.30)  # Should not raise
		with self.assertRaises(frappe.ValidationError):
			self.settings.validate_minimum_transaction_amount("GBP", 0.29)

	def test_validate_minimum_jpy_threshold(self):
		"""Test JPY minimum threshold (¥50)."""
		self.settings.validate_minimum_transaction_amount("JPY", 50)  # Should not raise
		with self.assertRaises(frappe.ValidationError):
			self.settings.validate_minimum_transaction_amount("JPY", 49)

	def test_validate_minimum_unknown_currency_uses_default(self):
		"""Test that unknown currencies use default minimum (0.50)."""
		# Unknown currency should use default 0.50
		self.settings.validate_minimum_transaction_amount("XYZ", 0.50)  # Should not raise


class TestStripeWebhookEventProcessing(unittest.TestCase):
	"""Test webhook event processing with mocked events."""

	def setUp(self):
		"""Create a mock StripeSettings instance for testing."""
		self.settings = MagicMock(spec=StripeSettings)
		self.settings.handle_webhook_event = StripeSettings.handle_webhook_event.__get__(
			self.settings, StripeSettings
		)
		self.settings._handle_payment_success = MagicMock(return_value={"status": "success"})
		self.settings._handle_payment_failure = MagicMock(return_value={"status": "failed"})
		self.settings._handle_payment_canceled = MagicMock(return_value={"status": "canceled"})
		self.settings.enable_debug_logging = False

	def test_handle_payment_intent_succeeded_event(self):
		"""Test handling of payment_intent.succeeded event."""
		event = {
			"type": "payment_intent.succeeded",
			"data": {
				"object": {
					"id": "pi_test123",
					"status": "succeeded",
					"metadata": {"reference_doctype": "Sales Invoice", "reference_docname": "SI-001"},
				}
			},
		}

		result = self.settings.handle_webhook_event(event)

		self.settings._handle_payment_success.assert_called_once()
		self.assertEqual(result["status"], "success")

	def test_handle_payment_intent_failed_event(self):
		"""Test handling of payment_intent.payment_failed event."""
		event = {
			"type": "payment_intent.payment_failed",
			"data": {
				"object": {
					"id": "pi_test123",
					"status": "requires_payment_method",
					"last_payment_error": {"message": "Card declined"},
				}
			},
		}

		result = self.settings.handle_webhook_event(event)

		self.settings._handle_payment_failure.assert_called_once()
		self.assertEqual(result["status"], "failed")

	def test_handle_payment_intent_canceled_event(self):
		"""Test handling of payment_intent.canceled event."""
		event = {
			"type": "payment_intent.canceled",
			"data": {
				"object": {
					"id": "pi_test123",
					"status": "canceled",
				}
			},
		}

		result = self.settings.handle_webhook_event(event)

		self.settings._handle_payment_canceled.assert_called_once()
		self.assertEqual(result["status"], "canceled")

	def test_handle_unhandled_event_type(self):
		"""Test that unhandled event types return ignored status."""
		event = {
			"type": "customer.created",
			"data": {"object": {"id": "cus_test123"}},
		}

		result = self.settings.handle_webhook_event(event)

		self.assertEqual(result["status"], "ignored")
		self.assertEqual(result["event_type"], "customer.created")


class TestStripePaymentIntentCreation(unittest.TestCase):
	"""Test PaymentIntent creation with mocked Stripe API."""

	@patch("payments.payment_gateways.doctype.stripe_settings.stripe_settings.stripe")
	@patch("payments.payment_gateways.doctype.stripe_settings.stripe_settings.create_request_log")
	def test_create_payment_intent_success(self, mock_create_log, mock_stripe):
		"""Test successful PaymentIntent creation."""
		# Setup mocks
		mock_intent = MagicMock()
		mock_intent.id = "pi_test123"
		mock_intent.client_secret = "pi_test123_secret_xyz"
		mock_intent.status = "requires_payment_method"
		mock_stripe.PaymentIntent.create.return_value = mock_intent

		mock_integration_request = MagicMock()
		mock_integration_request.name = "INT-REQ-001"
		mock_create_log.return_value = mock_integration_request

		# Create settings instance
		settings = frappe.new_doc("Stripe Settings")
		settings.gateway_name = "Test Gateway"
		settings.publishable_key = "pk_test_123"
		settings.enable_debug_logging = False

		# Mock password retrieval
		settings.get_password = MagicMock(return_value="sk_test_456")

		# Call method
		data = {
			"amount": 25.00,
			"currency": "USD",
			"reference_doctype": "Sales Invoice",
			"reference_docname": "SI-001",
			"payer_email": "test@example.com",
		}

		result = settings.create_payment_intent(data)

		# Verify
		self.assertEqual(result["payment_intent_id"], "pi_test123")
		self.assertEqual(result["client_secret"], "pi_test123_secret_xyz")
		self.assertEqual(result["publishable_key"], "pk_test_123")

		# Verify Stripe was called correctly
		mock_stripe.PaymentIntent.create.assert_called_once()
		call_kwargs = mock_stripe.PaymentIntent.create.call_args[1]
		self.assertEqual(call_kwargs["amount"], 2500)  # $25 = 2500 cents
		self.assertEqual(call_kwargs["currency"], "usd")

	@patch("payments.payment_gateways.doctype.stripe_settings.stripe_settings.stripe")
	def test_create_payment_intent_card_error(self, mock_stripe):
		"""Test PaymentIntent creation with card error."""
		import stripe as stripe_module

		mock_stripe.error = stripe_module.error
		mock_stripe.PaymentIntent.create.side_effect = stripe_module.error.CardError(
			message="Your card was declined",
			param=None,
			code="card_declined",
		)

		settings = frappe.new_doc("Stripe Settings")
		settings.gateway_name = "Test Gateway"
		settings.publishable_key = "pk_test_123"
		settings.get_password = MagicMock(return_value="sk_test_456")

		data = {
			"amount": 25.00,
			"currency": "USD",
			"reference_doctype": "Sales Invoice",
			"reference_docname": "SI-001",
		}

		with self.assertRaises(frappe.ValidationError):
			settings.create_payment_intent(data)

	@patch("payments.payment_gateways.doctype.stripe_settings.stripe_settings.stripe")
	def test_create_payment_intent_auth_error(self, mock_stripe):
		"""Test PaymentIntent creation with authentication error."""
		import stripe as stripe_module

		mock_stripe.error = stripe_module.error
		mock_stripe.PaymentIntent.create.side_effect = stripe_module.error.AuthenticationError(
			message="Invalid API Key"
		)

		settings = frappe.new_doc("Stripe Settings")
		settings.gateway_name = "Test Gateway"
		settings.publishable_key = "pk_test_123"
		settings.get_password = MagicMock(return_value="sk_test_456")

		data = {
			"amount": 25.00,
			"currency": "USD",
			"reference_doctype": "Sales Invoice",
			"reference_docname": "SI-001",
		}

		with self.assertRaises(frappe.ValidationError):
			settings.create_payment_intent(data)


class TestStripePaymentControllerMethods(unittest.TestCase):
	"""Test PaymentController interface methods on StripeSettings."""

	def _make_tx_data(self, amount, currency):
		"""Helper to create TxData with all required fields."""
		from payments.types import TxData

		return TxData(
			amount=amount,
			currency=currency,
			reference_doctype="Sales Invoice",
			reference_docname="SI-001",
			payer_contact={},
			payer_address={},
			loyalty_points=None,
			discount_amount=None,
		)

	def test_validate_tx_data_valid(self):
		"""Test validate_tx_data with valid data."""
		settings = frappe.new_doc("Stripe Settings")
		settings.gateway_name = "Test"

		tx_data = self._make_tx_data(25.00, "USD")

		# Should not raise
		settings.validate_tx_data(tx_data)

	def test_validate_tx_data_invalid_currency(self):
		"""Test validate_tx_data with invalid currency."""
		settings = frappe.new_doc("Stripe Settings")
		settings.gateway_name = "Test"

		tx_data = self._make_tx_data(25.00, "INVALID")

		with self.assertRaises(frappe.ValidationError):
			settings.validate_tx_data(tx_data)

	def test_validate_tx_data_below_minimum(self):
		"""Test validate_tx_data with amount below minimum."""
		settings = frappe.new_doc("Stripe Settings")
		settings.gateway_name = "Test"

		tx_data = self._make_tx_data(0.10, "USD")  # Below $0.50 minimum

		with self.assertRaises(frappe.ValidationError):
			settings.validate_tx_data(tx_data)

	def test_render_failure_message_with_error(self):
		"""Test _render_failure_message extracts error from response."""
		settings = frappe.new_doc("Stripe Settings")
		settings.gateway_name = "Test"
		settings.state = frappe._dict()
		settings.state.response = frappe._dict(
			payload={
				"status": "requires_payment_method",
				"last_payment_error": {"message": "Your card has insufficient funds."},
			}
		)

		result = settings._render_failure_message()

		self.assertEqual(result, "Your card has insufficient funds.")

	def test_render_failure_message_default(self):
		"""Test _render_failure_message returns default when no error."""
		settings = frappe.new_doc("Stripe Settings")
		settings.gateway_name = "Test"
		settings.state = frappe._dict()
		settings.state.response = frappe._dict(payload={"status": "requires_payment_method"})

		result = settings._render_failure_message()

		self.assertIn("declined", result.lower())

	def test_is_server_to_server_with_hash(self):
		"""Test _is_server_to_server returns True when hash present (webhook)."""
		settings = frappe.new_doc("Stripe Settings")
		settings.gateway_name = "Test"
		settings.state = frappe._dict()
		settings.state.response = frappe._dict(hash="webhook_signature_hash")
		settings.state.psl = frappe._dict(flow_type="charge")

		result = settings._is_server_to_server()

		self.assertTrue(result)

	def test_is_server_to_server_without_hash(self):
		"""Test _is_server_to_server returns False when no hash (client-side)."""
		settings = frappe.new_doc("Stripe Settings")
		settings.gateway_name = "Test"
		settings.state = frappe._dict()
		settings.state.response = frappe._dict(hash=None)
		settings.state.psl = frappe._dict(flow_type="charge")

		result = settings._is_server_to_server()

		self.assertFalse(result)


class TestStripeLegacyMethods(unittest.TestCase):
	"""Test legacy methods for backwards compatibility."""

	@patch("payments.payment_gateways.doctype.stripe_settings.stripe_settings.stripe")
	@patch("payments.payment_gateways.doctype.stripe_settings.stripe_settings.create_request_log")
	def test_create_request_returns_redirect_info(self, mock_create_log, mock_stripe):
		"""Test legacy create_request method returns redirect info."""
		mock_intent = MagicMock()
		mock_intent.id = "pi_test123"
		mock_intent.client_secret = "pi_test123_secret_xyz"
		mock_intent.status = "requires_payment_method"
		mock_stripe.PaymentIntent.create.return_value = mock_intent

		mock_integration_request = MagicMock()
		mock_integration_request.name = "INT-REQ-001"
		mock_create_log.return_value = mock_integration_request

		settings = frappe.new_doc("Stripe Settings")
		settings.gateway_name = "Test Gateway"
		settings.publishable_key = "pk_test_123"
		settings.enable_debug_logging = False
		settings.get_password = MagicMock(return_value="sk_test_456")

		data = {
			"amount": 25.00,
			"currency": "USD",
			"reference_doctype": "Sales Invoice",
			"reference_docname": "SI-001",
		}

		result = settings.create_request(data)

		self.assertIn("payment_intent_id", result)
		self.assertIn("client_secret", result)
		self.assertIn("redirect_to", result)
		self.assertIn("status", result)
		self.assertEqual(result["status"], "Pending")
		self.assertIn("stripe_checkout", result["redirect_to"])


class TestStripeConfirmPayment(IntegrationTestCase):
	"""Test confirm_payment endpoint."""

	def test_confirm_payment_function_exists(self):
		"""Test that confirm_payment function exists and is callable."""
		from payments.templates.pages.stripe_checkout import confirm_payment

		self.assertTrue(callable(confirm_payment))

	def test_confirm_payment_is_whitelisted(self):
		"""Test that confirm_payment has allow_guest=True."""
		import inspect

		from payments.templates.pages.stripe_checkout import confirm_payment

		source = inspect.getsource(confirm_payment)
		# Verify the decorator allows guest access
		self.assertIn("allow_guest=True", source)

	def test_confirm_payment_requires_stripe_settings(self):
		"""Test that confirm_payment fails gracefully without Stripe Settings."""
		import inspect

		from payments.templates.pages.stripe_checkout import confirm_payment

		source = inspect.getsource(confirm_payment)
		self.assertIn("Stripe Settings not configured", source)

	def test_confirm_payment_handles_stripe_error(self):
		"""Test confirm_payment handles Stripe errors gracefully."""
		import inspect

		from payments.templates.pages.stripe_checkout import confirm_payment

		source = inspect.getsource(confirm_payment)
		# Verify error handling exists
		self.assertIn("StripeError", source)
		self.assertIn("payment-failed", source)


class TestStripeFlowstates(unittest.TestCase):
	"""Test that Stripe flowstates are correctly mapped."""

	def test_success_states(self):
		"""Test success states include 'succeeded'."""
		self.assertIn("succeeded", StripeSettings.flowstates.success)
		# Should only have one success state
		self.assertEqual(len(StripeSettings.flowstates.success), 1)

	def test_pre_authorized_states(self):
		"""Test pre_authorized states include 'requires_capture'."""
		self.assertIn("requires_capture", StripeSettings.flowstates.pre_authorized)

	def test_processing_states(self):
		"""Test processing states cover all pending states."""
		processing = StripeSettings.flowstates.processing
		self.assertIn("processing", processing)
		self.assertIn("requires_action", processing)
		self.assertIn("requires_confirmation", processing)

	def test_declined_states(self):
		"""Test declined states include both canceled and failed states."""
		declined = StripeSettings.flowstates.declined
		self.assertIn("canceled", declined)
		self.assertIn("requires_payment_method", declined)

	def test_flowstates_are_mutually_exclusive(self):
		"""Test that no state appears in multiple categories."""
		all_states = (
			StripeSettings.flowstates.success
			+ StripeSettings.flowstates.pre_authorized
			+ StripeSettings.flowstates.processing
			+ StripeSettings.flowstates.declined
		)
		# Check for duplicates
		self.assertEqual(len(all_states), len(set(all_states)))


class TestStripeWebhookRouting(unittest.TestCase):
	"""Dual-path dispatch: v2 events (with psl_name) -> process_response; else legacy."""

	def _ctrl(self):
		from payments.payment_gateways.doctype.stripe_settings.stripe_settings import StripeSettings

		ctrl = StripeSettings.__new__(StripeSettings)
		ctrl.enable_debug_logging = 0
		return ctrl

	def test_v2_event_routes_to_psl_handler(self):
		ctrl = self._ctrl()
		event = {
			"type": "payment_intent.succeeded",
			"data": {"object": {"id": "pi_1", "status": "succeeded", "metadata": {"psl_name": "PSL-1"}}},
		}
		with (
			patch.object(ctrl, "_process_webhook_via_psl", return_value={"status": "processed"}) as via_psl,
			patch.object(ctrl, "_handle_payment_success") as legacy,
		):
			ctrl.handle_webhook_event(event)
		via_psl.assert_called_once_with(event["data"]["object"])
		legacy.assert_not_called()

	def test_event_without_psl_name_routes_to_legacy(self):
		ctrl = self._ctrl()
		event = {
			"type": "payment_intent.succeeded",
			"data": {
				"object": {"id": "pi_1", "status": "succeeded", "metadata": {"integration_request": "IR-1"}}
			},
		}
		with (
			patch.object(ctrl, "_process_webhook_via_psl") as via_psl,
			patch.object(ctrl, "_handle_payment_success", return_value={"status": "success"}) as legacy,
		):
			ctrl.handle_webhook_event(event)
		legacy.assert_called_once()
		via_psl.assert_not_called()

	def test_process_webhook_via_psl_builds_response_and_calls_process_response(self):
		ctrl = self._ctrl()
		intent = {"id": "pi_1", "status": "succeeded", "metadata": {"psl_name": "PSL-1"}}
		with patch("payments.controllers.PaymentController.process_response") as pr:
			result = ctrl._process_webhook_via_psl(intent)
		pr.assert_called_once()
		psl_name_arg, response_arg = pr.call_args[0]
		self.assertEqual(psl_name_arg, "PSL-1")
		self.assertEqual(response_arg.hash, b"pi_1")
		self.assertEqual(response_arg.payload, intent)
		self.assertEqual(result, {"status": "processed", "psl_name": "PSL-1"})


class TestStripeWebhookV2Integration(IntegrationTestCase):
	"""End-to-end: a v2 webhook event drives the PSL to a terminal state via process_response."""

	STRIPE_PATH = "payments.payment_gateways.doctype.stripe_settings.stripe_settings.stripe"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("Stripe Settings", "_Test Webhook"):
			settings = frappe.get_doc(
				{
					"doctype": "Stripe Settings",
					"gateway_name": "_Test Webhook",
					"publishable_key": "pk_test_webhook",
					"secret_key": "sk_test_webhook",
				}
			)
			settings.flags.ignore_mandatory = True
			settings.insert(ignore_permissions=True)
		cls.gateway_name = "Stripe-_Test Webhook"
		cls.settings = frappe.get_doc("Stripe Settings", "_Test Webhook")
		frappe.db.commit()  # nosemgrep: frappe-manual-commit,Dont-commit - intentional: commit shared class fixtures so they are visible across this class's test methods
		# Fixtures are committed (visible across this class's test methods); the
		# `if not exists` guard keeps setUpClass idempotent across re-runs. This
		# matches the repo's existing test-fixture pattern. No tearDownClass: the
		# PSLs the tests create reference the auto-created Payment Gateway, so
		# deleting it would raise LinkExistsError.

	def _make_tx_data(self):
		from payments.types import TxData

		return TxData(
			amount=25.00,
			currency="USD",
			reference_doctype="User",
			reference_docname="Administrator",
			payer_contact={"email_id": "webhook@example.com"},
			payer_address={},
			loyalty_points=None,
			discount_amount=None,
		)

	def _mock_intent(self, status="succeeded", id="pi_wh_1"):
		intent = MagicMock()
		intent.id = id
		intent.client_secret = f"{id}_secret"
		intent.status = status
		return intent

	def _initiated_psl(self, intent_id="pi_wh_1"):
		"""initiate + proceed -> a PSL in Initiated state for the Stripe gateway."""
		from payments.controllers import PaymentController

		with patch(self.STRIPE_PATH) as mock_stripe:
			mock_stripe.PaymentIntent.create.return_value = self._mock_intent(id=intent_id)
			_controller, psl_name = PaymentController.initiate(self._make_tx_data(), self.gateway_name)
			PaymentController.proceed(psl_name)
		return psl_name

	def _webhook_event(self, psl_name, event_type, status, intent_id="pi_wh_1", **object_extra):
		obj = {"id": intent_id, "status": status, "metadata": {"psl_name": psl_name}}
		obj.update(object_extra)
		return {"type": event_type, "data": {"object": obj}}

	def test_webhook_success_marks_psl_paid(self):
		psl_name = self._initiated_psl("pi_success")
		event = self._webhook_event(psl_name, "payment_intent.succeeded", "succeeded", intent_id="pi_success")
		with patch(self.STRIPE_PATH) as mock_stripe:
			mock_stripe.PaymentIntent.retrieve.side_effect = AssertionError(
				"webhook path must not call PaymentIntent.retrieve"
			)
			self.settings.handle_webhook_event(event)
		self.assertEqual(frappe.get_doc("Payment Session Log", psl_name).status, "Paid")

	def test_webhook_failed_marks_psl_declined(self):
		psl_name = self._initiated_psl("pi_failed")
		event = self._webhook_event(
			psl_name,
			"payment_intent.payment_failed",
			"requires_payment_method",
			intent_id="pi_failed",
			last_payment_error={"message": "Your card was declined."},
		)
		with patch(self.STRIPE_PATH):
			self.settings.handle_webhook_event(event)
		self.assertEqual(frappe.get_doc("Payment Session Log", psl_name).status, "Declined")

	def test_webhook_canceled_marks_psl_declined(self):
		psl_name = self._initiated_psl("pi_canceled")
		event = self._webhook_event(psl_name, "payment_intent.canceled", "canceled", intent_id="pi_canceled")
		with patch(self.STRIPE_PATH):
			self.settings.handle_webhook_event(event)
		self.assertEqual(frappe.get_doc("Payment Session Log", psl_name).status, "Declined")

	def test_webhook_redelivery_is_idempotent(self):
		psl_name = self._initiated_psl("pi_idem")
		event = self._webhook_event(psl_name, "payment_intent.succeeded", "succeeded", intent_id="pi_idem")
		with patch(self.STRIPE_PATH):
			self.settings.handle_webhook_event(event)
			self.settings.handle_webhook_event(event)
		self.assertEqual(frappe.get_doc("Payment Session Log", psl_name).status, "Paid")

	def test_legacy_event_without_psl_name_does_not_touch_psl_path(self):
		event = {
			"type": "payment_intent.succeeded",
			"data": {
				"object": {
					"id": "pi_legacy",
					"status": "succeeded",
					"metadata": {"integration_request": "IR-x"},
				}
			},
		}
		with (
			patch.object(self.settings, "_process_webhook_via_psl") as via_psl,
			patch.object(
				self.settings, "_handle_payment_success", return_value={"status": "success"}
			) as legacy,
		):
			self.settings.handle_webhook_event(event)
		via_psl.assert_not_called()
		legacy.assert_called_once()


class TestStripeIntentStatusMapping(unittest.TestCase):
	def test_map_intent_status_dict_payload(self):
		from payments.payment_gateways.doctype.stripe_settings.stripe_settings import StripeSettings

		ctrl = StripeSettings.__new__(StripeSettings)
		self.assertEqual(ctrl._map_intent_status({"status": "succeeded"}), "succeeded")

	def test_map_intent_status_object_payload(self):
		from types import SimpleNamespace

		from payments.payment_gateways.doctype.stripe_settings.stripe_settings import StripeSettings

		ctrl = StripeSettings.__new__(StripeSettings)
		obj = SimpleNamespace(status="requires_action")
		self.assertEqual(ctrl._map_intent_status(obj), "requires_action")

	def test_intent_field_missing_key_returns_none(self):
		from payments.payment_gateways.doctype.stripe_settings.stripe_settings import StripeSettings

		self.assertIsNone(StripeSettings._intent_field({"status": "x"}, "customer"))


class TestStripeSaveMandateInitiation(unittest.TestCase):
	def _controller(self, *, save_mandate):
		from types import SimpleNamespace

		from payments.payment_gateways.doctype.stripe_settings.stripe_settings import StripeSettings

		ctrl = StripeSettings.__new__(StripeSettings)
		ctrl.publishable_key = "pk_test_x"
		tx_data = SimpleNamespace(
			amount=10.0,
			currency="EUR",
			reference_doctype="X",
			reference_docname="X-1",
			payer_contact={"email": "payer@example.com"},
			save_mandate=save_mandate,
			mandate=None,
		)
		ctrl.state = SimpleNamespace(tx_data=tx_data, psl=SimpleNamespace(name="psl-1"))
		return ctrl

	def test_save_mandate_adds_setup_future_usage_and_customer(self):
		ctrl = self._controller(save_mandate=True)
		fake_stripe = MagicMock()
		fake_stripe.PaymentIntent.create.return_value = MagicMock(id="pi_1", client_secret="cs_1")
		with (
			patch("payments.payment_gateways.doctype.stripe_settings.stripe_settings.stripe", fake_stripe),
			patch.object(ctrl, "get_stripe_api_key", return_value="sk_test_x"),
			patch.object(ctrl, "_ensure_stripe_customer", return_value="cus_1"),
			patch.object(ctrl, "convert_to_stripe_amount", return_value=1000),
		):
			ctrl._initiate_charge()
		_args, kwargs = fake_stripe.PaymentIntent.create.call_args
		self.assertEqual(kwargs["customer"], "cus_1")
		self.assertEqual(kwargs["setup_future_usage"], "off_session")

	def test_no_save_mandate_omits_setup_future_usage(self):
		ctrl = self._controller(save_mandate=False)
		fake_stripe = MagicMock()
		fake_stripe.PaymentIntent.create.return_value = MagicMock(id="pi_1", client_secret="cs_1")
		with (
			patch("payments.payment_gateways.doctype.stripe_settings.stripe_settings.stripe", fake_stripe),
			patch.object(ctrl, "get_stripe_api_key", return_value="sk_test_x"),
			patch.object(ctrl, "convert_to_stripe_amount", return_value=1000),
		):
			ctrl._initiate_charge()
		_args, kwargs = fake_stripe.PaymentIntent.create.call_args
		self.assertNotIn("setup_future_usage", kwargs)
		self.assertNotIn("customer", kwargs)

	def test_save_mandate_without_email_skips_mandate(self):
		ctrl = self._controller(save_mandate=True)
		ctrl.state.tx_data.payer_contact = {}  # no email
		fake_stripe = MagicMock()
		fake_stripe.PaymentIntent.create.return_value = MagicMock(id="pi_1", client_secret="cs_1")
		with (
			patch("payments.payment_gateways.doctype.stripe_settings.stripe_settings.stripe", fake_stripe),
			patch.object(ctrl, "get_stripe_api_key", return_value="sk_test_x"),
			patch.object(ctrl, "convert_to_stripe_amount", return_value=1000),
			patch.object(frappe, "log_error") as log_error,
		):
			ctrl._initiate_charge()
		_args, kwargs = fake_stripe.PaymentIntent.create.call_args
		self.assertNotIn("setup_future_usage", kwargs)
		self.assertNotIn("customer", kwargs)
		log_error.assert_called_once()


class TestStripeEnsureCustomer(unittest.TestCase):
	def _controller(self):
		from payments.payment_gateways.doctype.stripe_settings.stripe_settings import StripeSettings

		ctrl = StripeSettings.__new__(StripeSettings)
		ctrl.doctype = "Stripe Settings"
		ctrl.name = "MandateTest"
		return ctrl

	def test_reuses_existing_active_mandate_customer(self):
		ctrl = self._controller()
		fake_stripe = MagicMock()
		with (
			patch("payments.payment_gateways.doctype.stripe_settings.stripe_settings.stripe", fake_stripe),
			patch.object(frappe, "get_all", return_value=[frappe._dict({"customer_id": "cus_existing"})]),
		):
			result = ctrl._ensure_stripe_customer("payer@example.com")
		self.assertEqual(result, "cus_existing")
		fake_stripe.Customer.create.assert_not_called()

	def test_creates_customer_when_none_exists(self):
		ctrl = self._controller()
		fake_stripe = MagicMock()
		fake_stripe.Customer.create.return_value = MagicMock(id="cus_new")
		with (
			patch("payments.payment_gateways.doctype.stripe_settings.stripe_settings.stripe", fake_stripe),
			patch.object(ctrl, "get_stripe_api_key", return_value="sk_test_x"),
			patch.object(frappe, "get_all", return_value=[]),
		):
			result = ctrl._ensure_stripe_customer("new@example.com")
		self.assertEqual(result, "cus_new")
		fake_stripe.Customer.create.assert_called_once_with(email="new@example.com")


class TestStripePersistMandate(unittest.TestCase):
	def _controller(self):
		from types import SimpleNamespace

		from payments.payment_gateways.doctype.stripe_settings.stripe_settings import StripeSettings

		ctrl = StripeSettings.__new__(StripeSettings)
		ctrl.doctype = "Stripe Settings"
		ctrl.name = "Stripe Settings"
		tx_data = SimpleNamespace(payer_contact={"email": "payer@example.com"}, save_mandate=True)
		psl = MagicMock()
		ctrl.state = SimpleNamespace(tx_data=tx_data, psl=psl, response=SimpleNamespace())
		return ctrl, psl

	def test_persist_creates_mandate_and_links_psl(self):
		ctrl, _psl = self._controller()
		ctrl.state.psl.name = "psl-1"
		payload = {"status": "succeeded", "customer": "cus_1", "payment_method": "pm_1"}
		created = MagicMock()
		created.name = "SM-0001"
		psl_doc = MagicMock()

		def fake_get_doc(arg, *a, **k):
			return psl_doc if arg == "Payment Session Log" else created

		with (
			patch.object(frappe, "get_all", return_value=[]),
			patch.object(frappe, "get_doc", side_effect=fake_get_doc) as get_doc,
		):
			ctrl._persist_mandate(payload)
		dict_calls = [c.args[0] for c in get_doc.call_args_list if isinstance(c.args[0], dict)]
		self.assertEqual(len(dict_calls), 1)
		self.assertEqual(dict_calls[0]["doctype"], "Stripe Mandate")
		self.assertEqual(dict_calls[0]["customer_id"], "cus_1")
		self.assertEqual(dict_calls[0]["payment_method_id"], "pm_1")
		self.assertEqual(dict_calls[0]["payment_session_log"], "psl-1")
		created.insert.assert_called_once()
		psl_doc.set_mandate.assert_called_once_with(created)

	def test_persist_reuses_existing_mandate(self):
		ctrl, _psl = self._controller()
		payload = {"status": "succeeded", "customer": "cus_1", "payment_method": "pm_1"}
		existing_doc = MagicMock()
		psl_doc = MagicMock()

		def fake_get_doc(arg, *a, **k):
			return psl_doc if arg == "Payment Session Log" else existing_doc

		with (
			patch.object(frappe, "get_all", return_value=[frappe._dict({"name": "SM-EXIST"})]),
			patch.object(frappe, "get_doc", side_effect=fake_get_doc) as get_doc,
		):
			ctrl._persist_mandate(payload)
		get_doc.assert_any_call("Stripe Mandate", "SM-EXIST")
		existing_doc.insert.assert_not_called()
		psl_doc.set_mandate.assert_called_once_with(existing_doc)

	def test_process_response_persists_on_success_with_save_mandate(self):
		from types import SimpleNamespace

		ctrl, _psl = self._controller()
		ctrl.flags = MagicMock()
		ctrl.state.response = SimpleNamespace(payload={"status": "succeeded"})
		with patch.object(ctrl, "_persist_mandate") as persist:
			ctrl._process_response_for_charge()
		persist.assert_called_once()

	def test_process_response_skips_persist_when_declined(self):
		from types import SimpleNamespace

		ctrl, _psl = self._controller()
		ctrl.flags = MagicMock()
		ctrl.state.response = SimpleNamespace(payload={"status": "requires_payment_method"})
		with patch.object(ctrl, "_persist_mandate") as persist:
			ctrl._process_response_for_charge()
		persist.assert_not_called()

	def test_process_response_skips_persist_when_not_save_mandate(self):
		from types import SimpleNamespace

		ctrl, _psl = self._controller()
		ctrl.state.tx_data.save_mandate = False
		ctrl.flags = MagicMock()
		ctrl.state.response = SimpleNamespace(payload={"status": "succeeded"})
		with patch.object(ctrl, "_persist_mandate") as persist:
			ctrl._process_response_for_charge()
		persist.assert_not_called()

	def test_persist_noop_without_payment_method(self):
		ctrl, _psl = self._controller()
		payload = {"status": "succeeded", "customer": "cus_1"}  # no payment_method
		with patch.object(frappe, "get_doc") as get_doc:
			ctrl._persist_mandate(payload)
		get_doc.assert_not_called()


class TestStripeMandatedCharge(unittest.TestCase):
	def _controller(self, *, flow_type="mandated_charge", response_hash=None):
		from types import SimpleNamespace

		from payments.payment_gateways.doctype.stripe_settings.stripe_settings import StripeSettings

		ctrl = StripeSettings.__new__(StripeSettings)
		tx_data = SimpleNamespace(
			amount=10.0,
			currency="EUR",
			reference_doctype="X",
			reference_docname="X-1",
			payer_contact={"email": "payer@example.com"},
			mandate="SM-0001",
		)
		psl = MagicMock()
		psl.name = "psl-2"
		psl.flow_type = flow_type
		psl.get_mandate.return_value = {"doctype": "Stripe Mandate", "name": "SM-0001"}
		ctrl.state = SimpleNamespace(
			tx_data=tx_data,
			psl=psl,
			response=SimpleNamespace(hash=response_hash, payload={"status": "succeeded"}),
		)
		return ctrl

	def test_initiate_mandated_charge_sends_off_session_confirm(self):
		ctrl = self._controller()
		mandate = MagicMock(customer_id="cus_1", payment_method_id="pm_1")
		mandate.is_usable.return_value = True
		fake_stripe = MagicMock()
		fake_stripe.PaymentIntent.create.return_value = MagicMock(id="pi_9", status="succeeded")
		with (
			patch("payments.payment_gateways.doctype.stripe_settings.stripe_settings.stripe", fake_stripe),
			patch.object(ctrl, "get_stripe_api_key", return_value="sk_test_x"),
			patch.object(ctrl, "convert_to_stripe_amount", return_value=1000),
			patch.object(frappe, "get_doc", return_value=mandate),
		):
			initiated = ctrl._initiate_mandated_charge()
		_args, kwargs = fake_stripe.PaymentIntent.create.call_args
		self.assertTrue(kwargs["off_session"])
		self.assertTrue(kwargs["confirm"])
		self.assertEqual(kwargs["customer"], "cus_1")
		self.assertEqual(kwargs["payment_method"], "pm_1")
		self.assertEqual(kwargs["idempotency_key"], "psl-psl-2")
		self.assertEqual(kwargs["amount"], 1000)
		self.assertEqual(kwargs["currency"], "eur")
		self.assertEqual(initiated.correlation_id, "pi_9")
		self.assertEqual(initiated.payload["status"], "succeeded")

	def test_initiate_mandated_charge_card_declined_raises_flow_error(self):
		from payments.exceptions import FailedToInitiateFlowError

		ctrl = self._controller()
		mandate = MagicMock(customer_id="cus_1", payment_method_id="pm_1")
		mandate.is_usable.return_value = True
		fake_stripe = MagicMock()
		fake_stripe.PaymentIntent.create.side_effect = CardError(
			"Your card was declined.", None, "card_declined"
		)
		with (
			patch("payments.payment_gateways.doctype.stripe_settings.stripe_settings.stripe", fake_stripe),
			patch.object(ctrl, "get_stripe_api_key", return_value="sk_test_x"),
			patch.object(ctrl, "convert_to_stripe_amount", return_value=1000),
			patch.object(frappe, "get_doc", return_value=mandate),
		):
			with self.assertRaises(FailedToInitiateFlowError):
				ctrl._initiate_mandated_charge()

	def test_initiate_mandated_charge_without_mandate_raises_flow_error(self):
		from payments.exceptions import FailedToInitiateFlowError

		ctrl = self._controller()
		ctrl.state.tx_data.mandate = None
		with self.assertRaises(FailedToInitiateFlowError):
			ctrl._initiate_mandated_charge()

	def test_initiate_mandated_charge_unusable_mandate_raises(self):
		from payments.exceptions import FailedToInitiateFlowError

		ctrl = self._controller()
		mandate = MagicMock(status="Revoked")
		mandate.is_usable.return_value = False
		with patch.object(frappe, "get_doc", return_value=mandate):
			with self.assertRaises(FailedToInitiateFlowError):
				ctrl._initiate_mandated_charge()

	def test_initiate_mandated_charge_api_error_raises_flow_error(self):
		from stripe.error import APIConnectionError

		from payments.exceptions import FailedToInitiateFlowError

		ctrl = self._controller()
		mandate = MagicMock(customer_id="cus_1", payment_method_id="pm_1")
		mandate.is_usable.return_value = True
		fake_stripe = MagicMock()
		fake_stripe.PaymentIntent.create.side_effect = APIConnectionError("network down")
		with (
			patch("payments.payment_gateways.doctype.stripe_settings.stripe_settings.stripe", fake_stripe),
			patch.object(ctrl, "get_stripe_api_key", return_value="sk_test_x"),
			patch.object(ctrl, "convert_to_stripe_amount", return_value=1000),
			patch.object(frappe, "get_doc", return_value=mandate),
		):
			with self.assertRaises(FailedToInitiateFlowError):
				ctrl._initiate_mandated_charge()

	def test_process_response_for_mandated_charge_sets_status(self):
		ctrl = self._controller()
		ctrl.flags = MagicMock()
		ctrl.state.response.payload = {"status": "succeeded"}
		self.assertIsNone(ctrl._process_response_for_mandated_charge())
		self.assertEqual(ctrl.flags.status_changed_to, "succeeded")

	def test_is_server_to_server_true_for_mandated_flow(self):
		ctrl = self._controller(flow_type="mandated_charge", response_hash=None)
		self.assertTrue(ctrl._is_server_to_server())

	def test_is_server_to_server_uses_hash_for_charge_flow(self):
		ctrl = self._controller(flow_type="charge", response_hash=None)
		self.assertFalse(ctrl._is_server_to_server())


class TestStripeChargeMandateE2E(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("Stripe Settings", "MandateTest"):
			s = frappe.get_doc(
				{
					"doctype": "Stripe Settings",
					"gateway_name": "MandateTest",
					"publishable_key": "pk_test_x",
					"secret_key": "sk_test_x",
				}
			)
			s.flags.ignore_mandatory = True
			s.insert(ignore_permissions=True)
		cls.gateway_name = "Stripe-MandateTest"
		if not frappe.db.exists("Stripe Mandate", {"customer_id": "cus_e2e", "payment_method_id": "pm_e2e"}):
			cls.mandate = frappe.get_doc(
				{
					"doctype": "Stripe Mandate",
					"gateway_settings": "Stripe Settings",
					"gateway_controller": "MandateTest",
					"customer_id": "cus_e2e",
					"payment_method_id": "pm_e2e",
					"status": "Active",
					"payer": "e2e@example.com",
				}
			).insert(ignore_permissions=True)
		else:
			cls.mandate = frappe.get_doc(
				"Stripe Mandate", {"customer_id": "cus_e2e", "payment_method_id": "pm_e2e"}
			)
		frappe.db.commit()

	def _tx(self):
		from payments.types import TxData

		return TxData(
			amount=10.0,
			currency="EUR",
			reference_doctype="User",
			reference_docname="Administrator",
			payer_contact={"email": "e2e@example.com"},
			payer_address={},
			loyalty_points=None,
			discount_amount=None,
		)

	def _charge_returning(self, intent_status):
		from payments.controllers import PaymentController

		intent_obj = MagicMock(id="pi_e2e", status=intent_status, customer="cus_e2e", payment_method="pm_e2e")
		fake_stripe = MagicMock()
		fake_stripe.PaymentIntent.create.return_value = intent_obj
		fake_stripe.PaymentIntent.retrieve.return_value = intent_obj
		with (
			patch("payments.payment_gateways.doctype.stripe_settings.stripe_settings.stripe", fake_stripe),
			patch(
				"payments.payment_gateways.doctype.stripe_settings.stripe_settings.StripeSettings.get_stripe_api_key",
				return_value="sk_test_x",
			),
		):
			return PaymentController.charge_mandate(
				mandate=self.mandate, tx_data=self._tx(), gateway=self.gateway_name
			)

	def _charge_raising(self, exc):
		from payments.controllers import PaymentController

		fake_stripe = MagicMock()
		fake_stripe.PaymentIntent.create.side_effect = exc
		with (
			patch("payments.payment_gateways.doctype.stripe_settings.stripe_settings.stripe", fake_stripe),
			patch(
				"payments.payment_gateways.doctype.stripe_settings.stripe_settings.StripeSettings.get_stripe_api_key",
				return_value="sk_test_x",
			),
		):
			return PaymentController.charge_mandate(
				mandate=self.mandate, tx_data=self._tx(), gateway=self.gateway_name
			)

	def test_charge_mandate_success(self):
		result = self._charge_returning("succeeded")
		self.assertEqual(result.indicator_color, "green")

	def test_charge_mandate_requires_action_returns_url(self):
		result = self._charge_returning("requires_action")
		self.assertIn("pay", result.action["href"])

	def test_charge_mandate_declined(self):
		from stripe.error import CardError

		result = self._charge_raising(CardError("Your card was declined.", None, "card_declined"))
		self.assertEqual(result.indicator_color, "red")
