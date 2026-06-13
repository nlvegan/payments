# Copyright (c) 2021, Frappe and contributors
# For license information, please see LICENSE

import dataclasses
import json
from typing import TYPE_CHECKING, ClassVar, TypedDict

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.query_builder import Interval
from frappe.query_builder.functions import Now

from payments.types import GatewayProcessingResponse, GatewayRef, RemoteServerInitiationPayload, TxData

if TYPE_CHECKING:
	from payments.controllers import PaymentController
	from payments.payments.doctype.payment_button.payment_button import PaymentButton


class PSLState(TypedDict):
	"""State returned by PaymentSessionLog.load_state()"""

	psl: dict
	tx_data: TxData


class PaymentSessionLog(Document):
	# TODO: Remove vestigial `mandate` field from payment_session_log.json
	# The mandate system was removed from PaymentController but the DocType field
	# remains to avoid a schema migration. Clean up when convenient.

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		button: DF.Data | None
		correlation_id: DF.Data | None
		decline_reason: DF.Data | None
		flow_type: DF.Data | None
		gateway: DF.Data | None
		initiation_response_payload: DF.Code | None
		mandate: DF.Data | None
		processing_response_payload: DF.Code | None
		status: DF.Data | None
		title: DF.Data | None
		tx_data: DF.Code | None
	# end: auto-generated types

	# Centralized terminal state definitions - single source of truth
	# Used by /pay endpoint and other consumers
	TERMINAL_STATES: ClassVar[dict[str, str]] = {
		"Paid": "green",
		"Authorized": "green",
		"Processing": "yellow",
		"Declined": "red",
		"Cancelled": "red",
		"Error": "red",
		"Error - RefDoc": "red",
	}

	def is_terminal(self) -> bool:
		"""Check if PSL is in a terminal state (no further action possible)."""
		return self.status in self.TERMINAL_STATES

	def get_indicator_color(self) -> str:
		"""Get the indicator color for the current status."""
		return self.TERMINAL_STATES.get(self.status, "gray")

	def update_tx_data(self, tx_data: dict, status: str) -> None:
		# tx_data is a dict of updates (the controller passes
		# _filter_tx_data_updates(...) output). Reconstruct a TxData from the
		# merged result before persisting so a type-mismatched update raises
		# TypeError here instead of silently corrupting the stored JSON and
		# blowing up later in load_state() (TxData(**json.loads(...))).
		merged = {**json.loads(self.tx_data), **tx_data}
		validated = TxData(**merged)
		self.db_set(
			{
				"tx_data": frappe.as_json(dataclasses.asdict(validated)),
				"status": status,
			},
			commit=True,
		)

	def update_gateway_specific_state(self, data: dict, status: str) -> None:
		"""Store gateway-specific state during data capture phase."""
		self.db_set(
			{
				"initiation_response_payload": frappe.as_json(data),
				"status": status,
			},
			commit=True,
		)

	def set_initiation_payload(self, initiation_payload: RemoteServerInitiationPayload, status: str) -> None:
		self.db_set(
			{
				"initiation_response_payload": frappe.as_json(initiation_payload),
				"status": status,
			},
			commit=True,
		)

	def set_processing_payload(self, processing_response: GatewayProcessingResponse, status: str) -> None:
		self.db_set(
			{
				"processing_response_payload": frappe.as_json(processing_response.payload),
				"status": status,
			},
			commit=True,
		)

	def load_state(self):
		return frappe._dict(
			psl=frappe._dict(self.as_dict()),
			tx_data=TxData(**json.loads(self.tx_data)),
		)

	def get_controller(self) -> "PaymentController":
		"""For perfomance reasons, this is not implemented as a dynamic link but a json value
		so that it is only fetched when absolutely necessary.
		"""
		if not self.gateway:
			self.log_error("No gateway selected yet")
			frappe.throw(_("No gateway selected for this payment session"))
		ref = GatewayRef.from_json(self.gateway)
		# Use get_doc (NOT get_cached_doc) so each resolution yields a fresh
		# controller instance with a fresh `self.state`. A cached controller
		# reused within one request (e.g. a webhook processing several events
		# for the same gateway) would otherwise carry stale state between calls.
		return frappe.get_doc(ref.gateway_settings, ref.gateway_controller)

	def get_button(self) -> "PaymentButton":
		if not self.button:
			self.log_error("No button selected yet")
			frappe.throw(_("No button selected for this payment session"))
		return frappe.get_cached_doc("Payment Button", self.button)

	@staticmethod
	def clear_old_logs(days=90):
		# Clean ALL terminal-state logs older than the window, not just "Paid":
		# Declined/Cancelled/Error/Error - RefDoc logs are equally done and would
		# otherwise grow without bound. Reuse TERMINAL_STATES as the single source
		# of truth for what counts as terminal.
		# NOTE: the retention window (days) could be made site-configurable in a
		# future change; intentionally left hardcoded here.
		table = frappe.qb.DocType("Payment Session Log")
		terminal_states = list(PaymentSessionLog.TERMINAL_STATES)
		frappe.db.delete(
			table,
			filters=(table.modified < (Now() - Interval(days=days))) & (table.status.isin(terminal_states)),
		)


def _error_ref(error_log) -> str:
	"""Short, opaque correlation code for a guest-facing message.

	str(error_log) is the Error Log docname, which embeds an internal
	timestamp/naming scheme. Expose only the trailing 8 chars so support can
	still correlate against the full server-side Error Log without leaking the
	internal naming/timestamp to the guest. (M4)
	"""
	return str(error_log)[-8:]


# CSRF (M3): Frappe skips CSRF validation for guest sessions, so this guest
# endpoint could otherwise be driven by a cross-origin POST. Restricting it to
# POST blocks the trivial GET/<img>/link vectors and simple cross-origin form
# submits that don't already know the ~35-bit PSL name. Residual risk: a fully
# scripted cross-origin POST (fetch/XHR) is still possible if the attacker
# knows a valid, non-terminal PSL name; impact is bounded (it only switches
# among already-enabled buttons matching the PSL's gateway filter, never alters
# amount/refdoc). A per-session CSRF token issued in the /pay page context is
# the recommended follow-up; deliberately not built here to avoid half-baked
# token infra on this branch.
@frappe.whitelist(allow_guest=True, methods=["POST"])
def select_button(pslName: str | None = None, buttonName: str | None = None) -> str:
	"""Select a payment button for a payment session.

	Security validations:
	- Button must be enabled
	- Button must match PSL gateway filter (if set)
	- PSL must be in a pre-terminal state (not already paid/failed)
	"""
	try:
		psl = frappe.get_doc("Payment Session Log", pslName)
	except Exception:
		e = frappe.log_error("Payment Session Log not found", reference_doctype="Payment Session Log")
		# Return an opaque correlation code, not the Error Log docname: the name
		# embeds internal timestamp/naming. Support can still correlate via the
		# trailing chars stored in the (full) server-side Error Log. (M4)
		frappe.local.message_log = [_("Server Failure! Reference: {0}").format(_error_ref(e))]
		return

	# Validate PSL is in a state where button selection is allowed
	if psl.is_terminal():
		frappe.log_error(
			f"Attempted button selection on terminal PSL: {pslName} (status: {psl.status})",
			reference_doctype="Payment Session Log",
		)
		frappe.local.message_log = [_("This payment session is no longer active.")]
		return

	try:
		btn: PaymentButton = frappe.get_cached_doc("Payment Button", buttonName)
	except Exception:
		e = frappe.log_error("Payment Button not found", reference_doctype="Payment Button")
		# Return an opaque correlation code, not the Error Log docname (M4),
		# matching the PSL-not-found path above.
		frappe.local.message_log = [_("Server Failure! Reference: {0}").format(_error_ref(e))]
		return

	# Validate button is enabled
	if not btn.enabled:
		frappe.log_error(
			f"Attempted to select disabled button: {buttonName}",
			reference_doctype="Payment Button",
		)
		frappe.local.message_log = [_("This payment method is not available.")]
		return

	# Validate button matches PSL gateway filter (if set)
	if psl.gateway:
		try:
			gateway_filter = json.loads(psl.gateway)
			# Check if selected button matches the required gateway settings/controller
			if (
				gateway_filter.get("gateway_settings")
				and gateway_filter["gateway_settings"] != btn.gateway_settings
			):
				frappe.log_error(
					f"Button gateway mismatch: expected {gateway_filter.get('gateway_settings')}, got {btn.gateway_settings}",
					reference_doctype="Payment Session Log",
				)
				frappe.local.message_log = [_("This payment method is not available for this transaction.")]
				return
			if (
				gateway_filter.get("gateway_controller")
				and gateway_filter["gateway_controller"] != btn.gateway_controller
			):
				frappe.log_error(
					f"Button controller mismatch: expected {gateway_filter.get('gateway_controller')}, got {btn.gateway_controller}",
					reference_doctype="Payment Session Log",
				)
				frappe.local.message_log = [_("This payment method is not available for this transaction.")]
				return
		except (json.JSONDecodeError, TypeError):
			pass  # No valid gateway filter, allow any button

	psl.db_set(
		{
			"button": buttonName,
			"gateway": GatewayRef(btn.gateway_settings, btn.gateway_controller).to_json(),
		}
	)
	# once state set: reload the page to activate widget
	return {"reload": True}


# Data minimization (M2): TxData.payer_contact / payer_address arrive as full
# document dicts (contact.as_dict() / address.as_dict()), which carry PII and
# bookkeeping fields we neither need nor want to persist for the PSL retention
# window or expose to guest-rendered templates (owner, modified_by, timestamps,
# custom fields, etc.). Project them down to the minimal fields actually used
# for the payment + display. Anything not on these allowlists is stripped.
_PAYER_CONTACT_ALLOWLIST = frozenset({"full_name", "email_id", "phone", "mobile_no"})
_PAYER_ADDRESS_ALLOWLIST = frozenset(
	{"address_line1", "address_line2", "city", "state", "country", "pincode"}
)


def _project_allowed(value, allowlist: frozenset) -> dict:
	"""Return only the allowlisted keys of a dict; defensive against non-dicts
	and missing keys (tolerates partially-populated payer documents)."""
	if not isinstance(value, dict):
		return value
	return {k: value[k] for k in allowlist if k in value}


def _minimize_payer_pii(tx_data_dict: dict) -> dict:
	"""Strip non-essential PII from payer_contact/payer_address before persisting."""
	if "payer_contact" in tx_data_dict:
		tx_data_dict["payer_contact"] = _project_allowed(
			tx_data_dict["payer_contact"], _PAYER_CONTACT_ALLOWLIST
		)
	if "payer_address" in tx_data_dict:
		tx_data_dict["payer_address"] = _project_allowed(
			tx_data_dict["payer_address"], _PAYER_ADDRESS_ALLOWLIST
		)
	return tx_data_dict


def create_log(
	tx_data: TxData,
	controller: "PaymentController" = None,
	status: str = "Created",
) -> PaymentSessionLog:
	log = frappe.new_doc("Payment Session Log")
	# TxData is a dataclass — convert to dict for JSON serialization
	tx_data_dict = dataclasses.asdict(tx_data) if dataclasses.is_dataclass(tx_data) else tx_data
	tx_data_dict = _minimize_payer_pii(tx_data_dict)
	log.tx_data = frappe.as_json(tx_data_dict)
	log.status = status
	if controller:
		log.gateway = GatewayRef(controller.doctype, controller.name).to_json()

	log.insert(ignore_permissions=True)
	return log
