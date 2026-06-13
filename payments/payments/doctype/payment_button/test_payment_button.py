# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
import unittest
from unittest.mock import patch

import frappe

from payments.payments.doctype.payment_button.payment_button import PaymentButton


class TestPaymentButtonFrontendSafeContext(unittest.TestCase):
	"""H1: gateway secrets must never reach guest-rendered templates.

	The raw gateway settings document holds API secrets (secret_key etc.).
	_frontend_safe_doc() must expose ONLY the controller's whitelisted,
	non-secret projection — never the raw doc.
	"""

	def test_secret_key_not_in_projection(self):
		btn = PaymentButton.__new__(PaymentButton)
		btn.gateway_settings = "Fake Settings"
		btn.gateway_controller = "Fake Controller"

		# A gateway-settings-like object: holds a secret + a publishable key,
		# and exposes only the publishable key as frontend-safe.
		class FakeController:
			secret_key = "sk_live_SUPERSECRET"
			publishable_key = "pk_live_safe"

			def get_frontend_safe_context(self):
				return {"publishable_key": self.publishable_key}

		with patch("frappe.get_cached_doc", return_value=FakeController()):
			safe = btn._frontend_safe_doc()

		self.assertEqual(safe.get("publishable_key"), "pk_live_safe")
		self.assertNotIn("secret_key", safe)
		self.assertNotIn("sk_live_SUPERSECRET", str(safe))

	def test_default_projection_is_empty(self):
		btn = PaymentButton.__new__(PaymentButton)
		btn.gateway_settings = "Fake Settings"
		btn.gateway_controller = "Fake Controller"

		# Default controller projection exposes nothing.
		class BareController:
			secret_key = "sk_live_SUPERSECRET"

			def get_frontend_safe_context(self):
				return {}

		with patch("frappe.get_cached_doc", return_value=BareController()):
			safe = btn._frontend_safe_doc()

		self.assertEqual(dict(safe), {})


class TestPaymentButtonProperty(unittest.TestCase):
	"""Verify requires_data_capture property exists and works.

	Regression: property was misspelled as requires_data_catpure,
	and was accessed on PSL instead of PaymentButton.
	"""

	def test_requires_data_capture_true_for_data_capture_variant(self):
		btn = frappe.new_doc("Payment Button")
		btn.implementation_variant = "Data Capture"
		self.assertTrue(btn.requires_data_capture)

	def test_requires_data_capture_false_for_widget_variant(self):
		btn = frappe.new_doc("Payment Button")
		btn.implementation_variant = "Third Party Widget"
		self.assertFalse(btn.requires_data_capture)

	def test_requires_data_capture_attribute_exists(self):
		"""Property should be accessible — not raise AttributeError."""
		btn = frappe.new_doc("Payment Button")
		# This would have raised AttributeError with the old typo
		_ = btn.requires_data_capture
