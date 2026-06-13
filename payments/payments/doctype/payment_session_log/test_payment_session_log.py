# Copyright (c) 2021, Frappe and Contributors
# See LICENSE

import json
import unittest
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from payments.payments.doctype.payment_session_log.payment_session_log import (
	PaymentSessionLog,
	create_log,
	select_button,
)
from payments.types import TxData


def _make_tx_data() -> TxData:
	return TxData(
		amount=25.00,
		currency="EUR",
		reference_doctype="User",
		reference_docname="Administrator",
		payer_contact={},
		payer_address={},
		loyalty_points=None,
		discount_amount=None,
	)


class TestPaymentSessionLogTerminalStates(unittest.TestCase):
	"""Unit tests for terminal state methods."""

	def test_is_terminal_returns_true_for_paid(self):
		psl = PaymentSessionLog.__new__(PaymentSessionLog)
		psl.status = "Paid"
		self.assertTrue(psl.is_terminal())

	def test_is_terminal_returns_true_for_declined(self):
		psl = PaymentSessionLog.__new__(PaymentSessionLog)
		psl.status = "Declined"
		self.assertTrue(psl.is_terminal())

	def test_is_terminal_returns_false_for_created(self):
		psl = PaymentSessionLog.__new__(PaymentSessionLog)
		psl.status = "Created"
		self.assertFalse(psl.is_terminal())

	def test_is_terminal_returns_false_for_initiated(self):
		psl = PaymentSessionLog.__new__(PaymentSessionLog)
		psl.status = "Initiated"
		self.assertFalse(psl.is_terminal())

	def test_get_indicator_color_returns_green_for_paid(self):
		psl = PaymentSessionLog.__new__(PaymentSessionLog)
		psl.status = "Paid"
		self.assertEqual(psl.get_indicator_color(), "green")

	def test_get_indicator_color_returns_red_for_error(self):
		psl = PaymentSessionLog.__new__(PaymentSessionLog)
		psl.status = "Error"
		self.assertEqual(psl.get_indicator_color(), "red")

	def test_get_indicator_color_returns_gray_for_unknown(self):
		psl = PaymentSessionLog.__new__(PaymentSessionLog)
		psl.status = "SomeUnknownStatus"
		self.assertEqual(psl.get_indicator_color(), "gray")


class TestSelectButtonAuthorization(IntegrationTestCase):
	"""Integration tests for select_button security validations."""

	def setUp(self):
		# Create a test Stripe Settings (Gateway Controller) first
		if not frappe.db.exists("Stripe Settings", "_Test Controller"):
			self.controller = frappe.get_doc(
				{
					"doctype": "Stripe Settings",
					"gateway_name": "_Test Controller",
					"publishable_key": "pk_test_dummy",
					"secret_key": "sk_test_dummy",
				}
			)
			self.controller.flags.ignore_mandatory = True  # Skip API key validation
			self.controller.insert(ignore_permissions=True)

		# Create a test payment button (autoname from label)
		if not frappe.db.exists("Payment Button", "_Test PSL Button"):
			self.btn = frappe.get_doc(
				{
					"doctype": "Payment Button",
					"label": "_Test PSL Button",
					"enabled": 1,
					"gateway_settings": "Stripe Settings",
					"gateway_controller": "_Test Controller",
				}
			)
			self.btn.insert(ignore_permissions=True)
		else:
			self.btn = frappe.get_doc("Payment Button", "_Test PSL Button")

		# Create a disabled button for testing
		if not frappe.db.exists("Payment Button", "_Test PSL Disabled"):
			self.disabled_btn = frappe.get_doc(
				{
					"doctype": "Payment Button",
					"label": "_Test PSL Disabled",
					"enabled": 0,
					"gateway_settings": "Stripe Settings",
					"gateway_controller": "_Test Controller",
				}
			)
			self.disabled_btn.insert(ignore_permissions=True)

	def _create_psl(self, status="Created", gateway=None):
		"""Helper to create a test PSL."""
		psl = frappe.get_doc(
			{
				"doctype": "Payment Session Log",
				"status": status,
				"tx_data": json.dumps({"amount": 100, "currency": "USD"}),
				"gateway": gateway,
			}
		)
		psl.insert(ignore_permissions=True)
		return psl

	def test_select_button_rejects_disabled_button(self):
		"""select_button should reject disabled buttons."""
		psl = self._create_psl()

		result = select_button(pslName=psl.name, buttonName="_Test PSL Disabled")

		self.assertIsNone(result)
		psl.reload()
		self.assertIsNone(psl.button)

	def test_select_button_rejects_terminal_psl(self):
		"""select_button should reject PSL in terminal state."""
		psl = self._create_psl(status="Paid")

		result = select_button(pslName=psl.name, buttonName="_Test PSL Button")

		self.assertIsNone(result)
		psl.reload()
		self.assertIsNone(psl.button)

	def test_select_button_rejects_mismatched_gateway(self):
		"""select_button should reject button that doesn't match PSL gateway filter."""
		# PSL requires a specific gateway
		gateway_filter = json.dumps(
			{
				"gateway_settings": "GoCardless Settings",
				"gateway_controller": "Different Controller",
			}
		)
		psl = self._create_psl(gateway=gateway_filter)

		result = select_button(pslName=psl.name, buttonName="_Test PSL Button")

		self.assertIsNone(result)
		psl.reload()
		self.assertIsNone(psl.button)

	def test_select_button_accepts_valid_selection(self):
		"""select_button should accept valid button selection."""
		psl = self._create_psl()

		result = select_button(pslName=psl.name, buttonName="_Test PSL Button")

		self.assertIsNotNone(result)
		self.assertTrue(result.get("reload"))
		psl.reload()
		self.assertEqual(psl.button, "_Test PSL Button")

	def test_select_button_accepts_matching_gateway(self):
		"""select_button should accept button matching PSL gateway filter."""
		gateway_filter = json.dumps(
			{
				"gateway_settings": "Stripe Settings",
				"gateway_controller": "_Test Controller",
			}
		)
		psl = self._create_psl(gateway=gateway_filter)

		result = select_button(pslName=psl.name, buttonName="_Test PSL Button")

		self.assertIsNotNone(result)
		psl.reload()
		self.assertEqual(psl.button, "_Test PSL Button")


class TestPaymentSessionLogStatusDefault(IntegrationTestCase):
	"""The schema default for `status` must match the code's initial state."""

	def test_create_log_starts_in_created(self):
		"""create_log() inserts a PSL in the 'Created' state."""
		psl = create_log(tx_data=_make_tx_data())
		self.assertEqual(psl.status, "Created")

	def test_bare_insert_defaults_to_created(self):
		"""A PSL inserted without an explicit status falls back to the schema
		default, which must be 'Created' (a state the state machine knows),
		not the unrecognized 'Queued'."""
		psl = frappe.get_doc(
			{
				"doctype": "Payment Session Log",
				"tx_data": json.dumps({"amount": 100, "currency": "USD"}),
			}
		)
		psl.insert(ignore_permissions=True)
		self.assertEqual(psl.status, "Created")

	def test_created_is_a_recognized_non_terminal_state(self):
		"""'Created' must be a valid, non-terminal state the machine accepts."""
		psl = PaymentSessionLog.__new__(PaymentSessionLog)
		psl.status = "Created"
		self.assertFalse(psl.is_terminal())
