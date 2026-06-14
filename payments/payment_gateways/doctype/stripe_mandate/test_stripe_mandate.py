# Copyright (c) 2026, Frappe and contributors
# For license information, please see LICENSE
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase


def _make_mandate(**overrides):
	doc = frappe.get_doc(
		{
			"doctype": "Stripe Mandate",
			"gateway_settings": "Stripe Settings",
			"gateway_controller": "Stripe Settings",
			"customer_id": "cus_demo",
			"payment_method_id": "pm_demo",
			"status": "Active",
			"payer": "payer@example.com",
			**overrides,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc


class TestStripeMandate(IntegrationTestCase):
	def test_is_usable_active(self):
		m = _make_mandate()
		self.assertTrue(m.is_usable())

	def test_is_usable_false_when_revoked(self):
		m = _make_mandate(status="Revoked")
		self.assertFalse(m.is_usable())

	def test_revoke_detaches_and_marks_revoked(self):
		m = _make_mandate()
		fake_stripe = MagicMock()
		fake_controller = MagicMock()
		fake_controller.get_stripe_api_key.return_value = "sk_test_x"
		with (
			patch.dict("sys.modules", {"stripe": fake_stripe}),
			patch.object(frappe, "get_cached_doc", return_value=fake_controller),
		):
			m.revoke()
		fake_stripe.PaymentMethod.detach.assert_called_once_with("pm_demo", api_key="sk_test_x")
		self.assertEqual(frappe.get_doc("Stripe Mandate", m.name).status, "Revoked")

	def test_revoke_without_payment_method_skips_detach(self):
		m = _make_mandate(payment_method_id="")
		fake_stripe = MagicMock()
		with patch.dict("sys.modules", {"stripe": fake_stripe}):
			m.revoke()
		fake_stripe.PaymentMethod.detach.assert_not_called()
		self.assertEqual(frappe.get_doc("Stripe Mandate", m.name).status, "Revoked")

	def test_is_usable_false_when_payment_method_blank(self):
		m = _make_mandate(payment_method_id="")
		self.assertFalse(m.is_usable())
