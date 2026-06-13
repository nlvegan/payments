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


class TestGetControllerFreshness(IntegrationTestCase):
	"""get_controller() must return a fresh, uncached controller instance so
	that PaymentController.state never bleeds between resolutions."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("Payment Demo Settings", "Payment Demo Settings"):
			demo = frappe.get_doc({"doctype": "Payment Demo Settings", "gateway_name": "Demo"})
			demo.flags.ignore_mandatory = True
			demo.insert(ignore_permissions=True)
		frappe.db.commit()

	def _create_psl(self):
		from payments.types import GatewayRef

		psl = create_log(tx_data=_make_tx_data())
		psl.db_set(
			"gateway",
			GatewayRef("Payment Demo Settings", "Payment Demo Settings").to_json(),
		)
		return psl

	def test_get_controller_returns_distinct_instances(self):
		"""Two resolutions must not share the same (cached) object."""
		psl = self._create_psl()
		first = psl.get_controller()
		second = psl.get_controller()
		self.assertIsNot(first, second)

	def test_get_controller_state_is_fresh(self):
		"""A controller resolved from a fresh PSL load starts with empty state,
		and mutating one instance's state does not leak into the next."""
		psl = self._create_psl()
		first = psl.get_controller()
		self.assertEqual(first.state, {})
		first.state.leaked = "should-not-persist"
		second = psl.get_controller()
		self.assertEqual(second.state, {})


class TestUpdateTxDataValidation(IntegrationTestCase):
	"""update_tx_data() must validate the merged result against TxData so bad
	updates fail fast instead of silently corrupting the stored JSON."""

	def test_well_formed_update_round_trips(self):
		"""A valid update merges cleanly and load_state() reconstructs TxData."""
		psl = create_log(tx_data=_make_tx_data())
		psl.update_tx_data({"amount": 99.5}, "Started")
		psl.reload()
		self.assertEqual(psl.status, "Started")
		state = psl.load_state()
		self.assertEqual(state.tx_data.amount, 99.5)
		self.assertEqual(state.tx_data.currency, "EUR")

	def test_malformed_update_raises_typeerror(self):
		"""An update introducing an unknown field must raise TypeError at update
		time, not persist silently and break the next load_state()."""
		psl = create_log(tx_data=_make_tx_data())
		with self.assertRaises(TypeError):
			psl.update_tx_data({"not_a_real_field": "x"}, "Started")
		# Nothing corrupt was persisted: original state still loads.
		psl.reload()
		state = psl.load_state()
		self.assertEqual(state.tx_data.amount, 25.00)


class TestCreateLogPayerPIIMinimization(IntegrationTestCase):
	"""M2: create_log() must strip non-essential PII from payer_contact /
	payer_address (full as_dict() output) down to the documented allowlists
	before persisting them in the PSL."""

	def test_strips_non_allowlisted_payer_keys(self):
		tx_data = TxData(
			amount=25.00,
			currency="EUR",
			reference_doctype="User",
			reference_docname="Administrator",
			payer_contact={
				"full_name": "Jane Doe",
				"email_id": "jane@example.com",
				"phone": "123",
				"mobile_no": "456",
				# noise that must NOT be persisted
				"owner": "x@internal",
				"modified_by": "admin@internal",
				"creation": "2020-01-01",
				"secret_note": "y",
			},
			payer_address={
				"address_line1": "1 Main St",
				"city": "Town",
				"country": "NL",
				"owner": "x@internal",
				"custom_internal": "z",
			},
			loyalty_points=None,
			discount_amount=None,
		)
		psl = create_log(tx_data=tx_data)
		psl.reload()
		stored = json.loads(psl.tx_data)

		contact = stored["payer_contact"]
		# allowlisted fields survive
		self.assertEqual(contact.get("full_name"), "Jane Doe")
		self.assertEqual(contact.get("email_id"), "jane@example.com")
		self.assertEqual(contact.get("phone"), "123")
		self.assertEqual(contact.get("mobile_no"), "456")
		# everything else is gone
		for stripped in ("owner", "modified_by", "creation", "secret_note"):
			self.assertNotIn(stripped, contact)

		address = stored["payer_address"]
		self.assertEqual(address.get("address_line1"), "1 Main St")
		self.assertEqual(address.get("city"), "Town")
		self.assertEqual(address.get("country"), "NL")
		for stripped in ("owner", "custom_internal"):
			self.assertNotIn(stripped, address)

	def test_tolerates_empty_and_partial_payer_dicts(self):
		"""Missing keys / empty dicts must not raise."""
		tx_data = TxData(
			amount=10.00,
			currency="EUR",
			reference_doctype="User",
			reference_docname="Administrator",
			payer_contact={},
			payer_address={"city": "Town"},
			loyalty_points=None,
			discount_amount=None,
		)
		psl = create_log(tx_data=tx_data)
		psl.reload()
		stored = json.loads(psl.tx_data)
		self.assertEqual(stored["payer_contact"], {})
		self.assertEqual(stored["payer_address"], {"city": "Town"})


class TestClearOldLogs(IntegrationTestCase):
	"""clear_old_logs() must purge ALL terminal-state logs past the retention
	window, not just 'Paid' ones (else failed/errored logs grow unbounded)."""

	def _create_terminal_log(self, status, *, old):
		"""Create a terminal-status PSL; if old, backdate its modified column
		past the 90-day retention window via a direct, unmodified-tracking write."""
		psl = create_log(tx_data=_make_tx_data(), status=status)
		if old:
			# Bypass Frappe's modified-stamping by writing the column directly.
			frappe.db.set_value(
				"Payment Session Log",
				psl.name,
				"modified",
				"2000-01-01 00:00:00",
				update_modified=False,
			)
		return psl.name

	def test_clears_old_terminal_logs_keeps_recent(self):
		old_declined = self._create_terminal_log("Declined", old=True)
		old_error = self._create_terminal_log("Error", old=True)
		old_paid = self._create_terminal_log("Paid", old=True)
		recent_declined = self._create_terminal_log("Declined", old=False)
		frappe.db.commit()

		PaymentSessionLog.clear_old_logs(days=90)

		self.assertFalse(frappe.db.exists("Payment Session Log", old_declined))
		self.assertFalse(frappe.db.exists("Payment Session Log", old_error))
		self.assertFalse(frappe.db.exists("Payment Session Log", old_paid))
		self.assertTrue(frappe.db.exists("Payment Session Log", recent_declined))
