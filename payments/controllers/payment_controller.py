from __future__ import annotations

import json
from typing import TYPE_CHECKING, ClassVar, NoReturn
from urllib.parse import quote, urlencode

import frappe
from frappe import _
from frappe.desk.form.load import get_document_email
from frappe.email.doctype.email_account.email_account import EmailAccount
from frappe.model.base_document import get_controller
from frappe.model.document import Document
from frappe.utils import get_url
from requests.exceptions import HTTPError

from payments.exceptions import (
	FailedToInitiateFlowError,
	PayloadIntegrityError,
	PaymentControllerProcessingError,
	RefDocHookProcessingError,
)
from payments.payments.doctype.payment_session_log.payment_session_log import (
	PaymentSessionLog,
	create_log,
)
from payments.types import (
	ActionAfterProcessed,
	FrontendDefaults,
	GatewayProcessingResponse,
	Initiated,
	PaymentUrl,
	Proceeded,
	Processed,
	PSLName,
	RemoteServerInitiationPayload,
	SessionStates,
	SessionType,
	TxData,
	_Processed,
)
from payments.utils import PAYMENT_SESSION_REF_KEY

if TYPE_CHECKING:
	from payments.payments.doctype.payment_gateway.payment_gateway import PaymentGateway


def _error_ref(error_log) -> str:
	"""Short, opaque correlation code for a guest-facing message.

	str(error_log) is the Error Log docname, which embeds an internal
	timestamp/naming scheme. Expose only the trailing 8 chars so support can
	still correlate against the full server-side Error Log without leaking the
	internal naming/timestamp to the guest. (M4)
	"""
	return str(error_log)[-8:]


def _error_value(error, flow):
	return _(
		"Our server had a problem processing your {0}. Please contact customer support mentioning: {1}"
	).format(flow, _error_ref(error))


def _redirect_on_initiation_error(psl, error, *, include_psl: bool = False) -> NoReturn:
	"""Redirect the user to a generic payment-gateway error message and raise.

	``psl`` is only interpolated into the message when ``include_psl`` is True.
	The Error Log reference is shortened to an opaque code (the PSL name is
	already known to the user via the /pay URL, so only the Error Log ref needs
	hardening). Always raises ``frappe.Redirect`` — callers never resume.
	"""
	if include_psl:
		body = _("Please contact customer care mentioning: {0} and {1}").format(psl, _error_ref(error))
	else:
		body = _("Please contact customer care mentioning: {0}").format(_error_ref(error))
	frappe.redirect_to_message(
		_("Payment Gateway Error"),
		body,
		http_status_code=401,
		indicator_color="yellow",
	)
	raise frappe.Redirect


class PaymentController(Document):
	"""This controller implements the public API of payment gateway controllers."""

	if TYPE_CHECKING:
		frontend_defaults: FrontendDefaults
		flowstates: SessionStates

	# Fields that can be updated at proceed() time.
	# Critical fields (amount, currency, reference_doctype, reference_docname) are NOT allowed
	# to prevent tampering via the guest-facing /pay endpoint.
	UPDATABLE_TX_DATA_FIELDS = frozenset(
		{
			"payer_contact",
			"payer_address",
			"loyalty_points",
			"discount_amount",
		}
	)

	@staticmethod
	def _filter_tx_data_updates(updates: dict | None) -> dict:
		"""Filter updated_tx_data to only allow whitelisted fields.

		This prevents tampering with critical fields like amount, currency,
		and reference document through the proceed() endpoint.

		Args:
		        updates: The raw updates dict from the caller

		Returns:
		        Filtered dict containing only allowed fields
		"""
		if not updates:
			return {}

		filtered = {}
		rejected = []

		for key, value in updates.items():
			if key in PaymentController.UPDATABLE_TX_DATA_FIELDS:
				filtered[key] = value
			else:
				rejected.append(key)

		if rejected:
			frappe.logger("payments").warning(
				f"Rejected tx_data update for non-whitelisted fields: {rejected}"
			)

		return filtered

	def __init_subclass__(cls, **kwargs):
		# These are subclass-definition invariants (every concrete gateway must
		# declare flowstates/frontend_defaults as CLASS attributes), so validate
		# them once at class-definition (import) time rather than on every
		# instantiation. __init_subclass__ runs for subclasses only, so the
		# PaymentController base class itself — which legitimately doesn't declare
		# them — is never checked here.
		# NOTE: because this fires at class-definition time, an *abstract
		# intermediate* subclass (one that deliberately defers flowstates/
		# frontend_defaults to its own concrete subclasses) would raise here at
		# import. There are none today (gateways subclass PaymentController
		# directly). If one is introduced, guard this check (e.g. skip when the
		# class is marked abstract) rather than declaring placeholder attrs.
		super().__init_subclass__(**kwargs)
		if not (hasattr(cls, "flowstates") and isinstance(cls.flowstates, SessionStates)):
			raise TypeError(
				f"{cls.__name__} must declare cls.flowstates as an instance of payments.types.SessionStates"
			)
		if not (hasattr(cls, "frontend_defaults") and isinstance(cls.frontend_defaults, FrontendDefaults)):
			raise TypeError(
				f"{cls.__name__} must declare cls.frontend_defaults as an instance of payments.types.FrontendDefaults"
			)

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.state = frappe._dict()

	@staticmethod
	def initiate(
		tx_data: TxData,
		gateway: PaymentController | None = None,
		correlation_id: str | None = None,
		name: str | None = None,
	) -> tuple[PaymentController, PSLName]:
		"""Initiate a payment flow from Ref Doc with the given gateway.

		Inheriting methods can invoke super and then set e.g. correlation_id on self.state.psl to save
		and early-obtained correlation id from the payment gateway or to initiate the user flow if delegated to
		the controller (see: is_user_flow_initiation_delegated)
		"""
		if isinstance(gateway, str):
			payment_gateway: PaymentGateway = frappe.get_cached_doc("Payment Gateway", gateway)

			if not payment_gateway.gateway_controller and not payment_gateway.gateway_settings:
				frappe.throw(
					_(
						"{0} is not fully configured, both Gateway Settings and Gateway Controller need to be set"
					).format(gateway)
				)

			# Use get_doc (not get_cached_doc) for the controller: it carries mutable
			# per-flow `self.state`, so a cached instance could bleed state across
			# resolutions within one request. Mirrors PaymentSessionLog.get_controller.
			self = frappe.get_doc(
				payment_gateway.gateway_settings,
				payment_gateway.gateway_controller or payment_gateway.gateway_settings,  # may be a singleton
			)
		else:
			self = gateway

		self.validate_tx_data(tx_data)  # preflight check

		psl = create_log(
			tx_data=tx_data,
			controller=self,
			status="Created",
		)
		return self, psl.name

	@staticmethod
	def get_payment_url(psl_name: PSLName) -> PaymentUrl | None:
		"""Use the payment url to initiate the user flow, for example via email or chat message.

		Beware, that the controller might not implement this and in that case return: None
		"""
		params = {
			PAYMENT_SESSION_REF_KEY: psl_name,
		}
		return get_url(f"./pay?{urlencode(params)}")

	@staticmethod
	def pre_data_capture_hook(psl_name: PSLName) -> dict:
		"""Call this before presenting the user with a form to capture additional data.

		Implementation is optional, but can be used to acquire any additonal data from the remote
		gateway that should be present already during data capture.
		"""

		psl: PaymentSessionLog = frappe.get_doc("Payment Session Log", psl_name)
		self: PaymentController = psl.get_controller()
		data = self._pre_data_capture_hook()
		psl.update_gateway_specific_state(data, "Data Capture")
		return data

	def _run_initiation(self, psl, flow_type) -> Initiated:
		"""Shared initiate->persist primitive used by both proceed() (interactive)
		and charge_mandate() (headless).

		Calls the flow's _initiate_* method, persists correlation_id + initiation
		payload + flow_type, and returns the Initiated result. Raises on failure
		for the caller to present (proceed() redirects; charge_mandate() returns a
		Processed). Keeps the initiate path in exactly one place.
		"""
		frappe.flags.integration_request_doc = psl  # for linking error logs
		method_name = self._INITIATE_DISPATCH[flow_type]
		initiated = getattr(self, method_name)()
		psl.db_set(
			{
				"processing_response_payload": None,  # in case of a reset
				"flow_type": flow_type,
				"correlation_id": initiated.correlation_id,
			},
			commit=True,
		)
		psl.set_initiation_payload(initiated.payload, "Initiated")  # commits
		return initiated

	@staticmethod
	def proceed(psl_name: PSLName, updated_tx_data: TxData = None) -> Proceeded:
		"""Call this when the user agreed to proceed with the payment to initiate the capture with
		the remote payment gateway.

		If the capture is initialized by the gatway, call this immediatly without waiting for the
		user OK signal.

		updated_tx_data:
		   Pass any update to the inital transaction data; this can reflect later customer choices
		   and thereby modify the flow. Only whitelisted fields can be updated (see
		   UPDATABLE_TX_DATA_FIELDS). Critical fields like amount, currency, and reference
		   document cannot be changed to prevent tampering.

		Example:
		```python
		if controller.is_user_flow_initiation_delegated():
		    controller.proceed()
		else:
		    # example (depending on the doctype & business flow):
		    # 1. send email with payment link
		    # 2. let user open the link
		    # 3. upon rendering of the page: call proceed; potentially with tx updates
		    pass
		```
		"""

		psl: PaymentSessionLog = frappe.get_doc("Payment Session Log", psl_name)
		self: PaymentController = psl.get_controller()

		# Idempotency: if already initiated, return existing payload instead of re-initiating
		# This prevents duplicate external API calls on page refresh
		if psl.status == "Initiated" and psl.initiation_response_payload:
			self.state = psl.load_state()
			self.state.tx_data = self._patch_tx_data(self.state.tx_data)
			payload = json.loads(psl.initiation_response_payload)
			return Proceeded(
				integration=self.doctype,
				psltype=psl.flow_type or SessionType.charge,
				txdata=self.state.tx_data,
				payload=payload,
			)

		# Filter updates to only allow whitelisted fields (security: prevents tampering)
		filtered_updates = PaymentController._filter_tx_data_updates(updated_tx_data)
		psl.update_tx_data(filtered_updates, "Started")  # commits

		self.state = psl.load_state()
		# controller specific temporary modifications
		self.state.tx_data = self._patch_tx_data(self.state.tx_data)

		try:
			initiated = self._run_initiation(psl, SessionType.charge)
			return Proceeded(
				integration=self.doctype,
				psltype=SessionType.charge,
				txdata=self.state.tx_data,
				payload=initiated.payload,
			)

		# some gateways don't return HTTP errors ...
		except FailedToInitiateFlowError as err:
			psl.set_initiation_payload(err.data, "Error")
			error = psl.log_error(title=err.message)
			_redirect_on_initiation_error(psl, error, include_psl=True)

		# ... yet others do ...
		except HTTPError as err:
			# v2 sets frappe.flags.integration_request_doc (the PSL), never the v1
			# frappe.flags.integration_request, so reading the latter raised
			# AttributeError and masked the original HTTPError. Read the response
			# body off the exception itself, with a safe fallback.
			try:
				data = err.response.json() if err.response is not None else {}
			except ValueError:
				data = {"error": str(err)}
			psl.set_initiation_payload(data, "Error")
			error = frappe.get_last_doc("Error Log")
			_redirect_on_initiation_error(psl, error, include_psl=True)

		except Exception:
			error = psl.log_error(title="Unknown Initialization Failure")
			_redirect_on_initiation_error(psl, error)

	def get_frontend_safe_context(self) -> dict:
		"""Fields safe to expose to the guest /pay templates. Override per gateway
		to expose ONLY non-secret values (e.g. a publishable key). Default: none.

		Security: the full gateway settings document holds API secrets (secret_key,
		webhook secrets, tokens). It must NEVER be handed to templates whose rendered
		output is injected into the public /pay page — a single `{{ doc.secret_key }}`
		in any gateway template would leak credentials to every visitor. Callers build
		the template context from this projection instead of the raw doc.
		"""
		return {}

	@staticmethod
	def charge_mandate(mandate, tx_data: TxData, gateway=None) -> Processed:
		"""Charge a stored mandate off-session, server-side (no /pay page).

		Trusted backend entry point (NOT whitelisted). Creates a mandated_charge
		session, runs the shared initiation core (which for supporting gateways
		confirms synchronously), and feeds the result through the normal pipeline.

		On the gateway signalling that customer action is required, returns a
		Processed whose action points at the /pay URL so the caller can send a
		re-authentication link. On initiation failure, returns a Declined-style
		Processed.
		"""
		# Normalise the mandate ref onto tx_data so the gateway reads it back.
		if hasattr(mandate, "name"):
			tx_data.mandate = mandate.name
		elif isinstance(mandate, dict):
			tx_data.mandate = mandate.get("name")
		else:
			tx_data.mandate = mandate

		self, psl_name = PaymentController.initiate(tx_data, gateway)
		psl: PaymentSessionLog = frappe.get_doc("Payment Session Log", psl_name)
		if hasattr(mandate, "name") or isinstance(mandate, dict):
			psl.set_mandate(mandate)

		self.state = psl.load_state()
		self.state.tx_data = self._patch_tx_data(self.state.tx_data)

		try:
			initiated = self._run_initiation(psl, SessionType.mandated_charge)
		except FailedToInitiateFlowError as err:
			psl.set_initiation_payload(err.data, "Declined")
			return Processed(
				message=_("The mandate could not be charged. A new authorization may be required."),
				action=dict(href=PaymentController.get_payment_url(psl.name), label=_("Re-authorize")),
				status_changed_to="Declined",
				indicator_color="red",
				payload={},
			)

		# Customer-action required: cannot complete head-less; hand back a payment link.
		if initiated.payload.get("status") in self.flowstates.processing:
			return Processed(
				message=_("Additional authorization is required to complete this charge."),
				action=dict(href=PaymentController.get_payment_url(psl.name), label=_("Authorize")),
				status_changed_to="Processing",
				indicator_color="yellow",
				payload=initiated.payload,
			)

		# Otherwise feed the synchronous result straight into the normal pipeline.
		response = GatewayProcessingResponse(hash=None, message=None, payload=initiated.payload)
		return PaymentController.process_response(psl.name, response)

	def _get_support_email(self):
		"""Look up the support email for the reference document, falling back to default incoming."""
		incoming = get_document_email(
			self.state.tx_data.reference_doctype,
			self.state.tx_data.reference_docname,
		)
		if not incoming:
			account = EmailAccount.find_default_incoming()
			incoming = account.email_id if account else None
		return incoming

	def _build_support_action(self, psl, subject, body, fallback_action):
		"""Build a mailto action for user support, falling back to fallback_action if no email configured."""
		incoming_email = self._get_support_email()
		if incoming_email:
			params = {
				"subject": subject,
				"body": body,
			}
			href = f"mailto:{incoming_email}?{urlencode(params, quote_via=quote)}"
			return dict(href=href, label=_("Email Us"))
		return fallback_action

	def _build_compensatory_action(self, psl, error_log):
		return self._build_support_action(
			psl,
			subject=_("Payment Server Error: {}").format(error_log),
			# nosemgrep: frappe-translation-python-splitting - newlines in email body are intentional
			body=_("Reference:\n\n- PSL: {}\n- Error Log: {}\n- RefDoc: {}\n\nThank you!").format(
				frappe.utils.get_url_to_form("Payment Session Log", psl.name),
				frappe.utils.get_url_to_form("Error Log", error_log.name),
				frappe.utils.get_url_to_form(
					self.state.tx_data.reference_doctype, self.state.tx_data.reference_docname
				),
			),
			fallback_action=dict(href="/", label=_("Go to Homepage")),
		)

	# Status category → (psl_status, indicator_color, message_template, action_label)
	# Note: action labels are raw strings; wrapped in _() at render time to support i18n.
	# Translation markers for extraction: _("Go to Homepage"), _("Refresh")
	# Message markers: _("{} succeeded"), _("{} authorized"), _("{} awaiting further processing by the bank")
	_STATUS_MAP: ClassVar[dict] = {
		"success": ("Paid", "green", "{} succeeded", dict(href="/", label="Go to Homepage")),
		"pre_authorized": ("Authorized", "green", "{} authorized", dict(href="/", label="Go to Homepage")),
		"processing": (
			"Processing",
			"yellow",
			"{} awaiting further processing by the bank",
			dict(href="/", label="Refresh"),
		),
	}

	# flow_type -> initiation method name (parallels _FLOW_DISPATCH for processing)
	_INITIATE_DISPATCH: ClassVar[dict] = {
		SessionType.charge: "_initiate_charge",
		SessionType.mandated_charge: "_initiate_mandated_charge",
	}

	# flow_type -> (process_method_name, refdoc_hook_name, human_label)
	# Resolved via getattr(self, ...) at call time so gateways can override.
	# Unset/unrecognised flow_type falls back to the charge entry.
	_FLOW_DISPATCH: ClassVar[dict] = {
		SessionType.charge: ("_process_response_for_charge", "on_payment_charge_processed", "charge"),
		SessionType.mandated_charge: (
			# _process_response_for_mandated_charge contract is added with the mandate impl
			"_process_response_for_mandated_charge",
			"on_payment_mandated_charge_processed",
			"mandated charge",
		),
	}

	def _process_response(self, psl: PaymentSessionLog, ref_doc: Document) -> Processed:
		self._validate_response()

		flow = self.state.psl.flow_type or SessionType.charge
		process_method_name, hookmethod, flow_label = self._FLOW_DISPATCH.get(
			flow, self._FLOW_DISPATCH[SessionType.charge]
		)

		processed = None
		try:
			processed = getattr(self, process_method_name)()  # idempotent on second run
		except Exception as e:
			raise PaymentControllerProcessingError(f"{process_method_name} failed", flow_label) from e

		all_states = (
			self.flowstates.success
			+ self.flowstates.pre_authorized
			+ self.flowstates.processing
			+ self.flowstates.declined
		)
		if self.flags.status_changed_to not in all_states:
			# An unmapped status must surface as a handled error: process_response's
			# outer try only catches PaymentControllerProcessingError (and siblings),
			# not a bare ValueError. Raising ValueError here would escape with a
			# traceback and return nothing to the frontend; raise the controller
			# error so the existing error path renders a clean red Processed.
			raise PaymentControllerProcessingError(
				f"Gateway returned an unmapped status: {self.flags.status_changed_to}", "charge"
			)

		ret = {
			"status_changed_to": self.flags.status_changed_to,
			"payload": self.state.response.payload,
		}

		changed = False

		# Handle success / pre_authorized / processing (common structure)
		for category, (psl_status, color, msg_template, action_label) in self._STATUS_MAP.items():
			if self.flags.status_changed_to in getattr(self.flowstates, category):
				changed = psl_status != psl.status
				psl.db_set("decline_reason", None)
				psl.set_processing_payload(self.state.response, psl_status)  # commits
				ret["indicator_color"] = color
				processed = processed or Processed(
					message=_(msg_template).format(flow_label.title()),
					action=dict(action_label, label=_(action_label["label"])),
					**ret,
				)
				break

		# Handle declined (structurally different: resets button, builds support mailto)
		if self.flags.status_changed_to in self.flowstates.declined:
			changed = "Declined" != psl.status
			psl.db_set(
				{
					"decline_reason": self._render_failure_message(),
					"button": None,  # reset the button for another chance
				}
			)
			psl.set_processing_payload(self.state.response, "Declined")  # commits
			ret["indicator_color"] = "red"

			action = self._build_support_action(
				psl,
				subject=_("Help! Payment declined: {}, {}").format(
					self.state.tx_data.reference_docname, psl.name
				),
				# nosemgrep: frappe-translation-python-splitting - newlines in email body are intentional
				body=_("Please help me with:\n- PSL: {}\n- RefDoc: {}\n\nThank you!").format(
					frappe.utils.get_url_to_form("Payment Session Log", psl.name),
					frappe.utils.get_url_to_form(
						self.state.tx_data.reference_doctype, self.state.tx_data.reference_docname
					),
				),
				fallback_action=dict(href=PaymentController.get_payment_url(psl.name), label=_("Refresh")),
			)
			processed = processed or Processed(
				message=_("{} declined").format(flow_label.title()),
				action=action,
				**ret,
			)

		return self._invoke_ref_doc_hook(ref_doc, changed, ret, processed, hookmethod, flow_label)

	def _invoke_ref_doc_hook(
		self,
		ref_doc: Document,
		changed: bool,
		ret: dict,
		processed: Processed,
		hookmethod: str,
		flow_label: str,
	) -> Processed:
		"""Invoke the optional flow-specific ref-doc hook (e.g. on_payment_charge_processed).

		``hookmethod`` and ``flow_label`` are resolved from ``_FLOW_DISPATCH`` by
		``_process_response`` and passed in, so this serves the charge and
		mandated-charge flows alike.

		The hook is optional (for smoother adoption); when present it may override
		the default ``processed`` value built by ``_process_response``. Any failure
		in user/server-script code is wrapped in ``RefDocHookProcessingError`` after
		scrubbing the client-visible message log, so no internal details leak.

		Returns the (possibly overridden) ``Processed``.
		"""
		has_hook = hasattr(ref_doc, hookmethod) and callable(getattr(ref_doc, hookmethod, None))

		if not has_hook:
			return processed

		try:
			ref_doc.flags.payment_session = frappe._dict(
				changed=changed, state=self.state, flags=self.flags, flowstates=self.flowstates
			)  # when run as server script: can only set flags
			res = ref_doc.run_method(
				hookmethod,
				changed,
				self.state,
				self.flags,
				self.flowstates,
			)
			# result from server script run
			res = ref_doc.flags.payment_result or res
			if res:
				# type check the result value on user implementations
				res["action"] = ActionAfterProcessed(**res.get("action", {})).__dict__
				_res = _Processed(**res)
				processed = Processed(**(ret | _res.__dict__))
		except Exception as e:
			# Ensure no details are leaked to the client
			frappe.local.message_log = [
				{
					"message": _("Server Processing Failure!"),
					"subtitle": _("(during RefDoc processing)"),
					"body": str(e),
					"indicator": "red",
				}
			]
			raise RefDocHookProcessingError("RefDoc hook processing failed", flow_label) from e

		return processed

	@staticmethod
	def process_response(psl_name: PSLName, response: GatewayProcessingResponse) -> Processed:
		"""Call this from the controlling business logic; either backend or frontend.

		It will recover the correct controller and dispatch the correct processing based on data that is at this
		point already stored in the integration log

		payload:
		    this is a signed, sensitive response containing the payment status; the signature is validated prior
		    to processing by controller._validate_response
		"""

		psl: PaymentSessionLog = frappe.get_doc("Payment Session Log", psl_name)
		self: PaymentController = psl.get_controller()

		# Guard against concurrent processing (e.g. webhook + client confirm race).
		# Acquire the lock *outside* the try/finally below so contention is handled
		# explicitly: psl.lock(timeout=5) raises frappe.DocumentLockedError (a
		# ValidationError subclass) which the outer handler does NOT catch. On the
		# muted server-to-server (webhook) path there is no error surface, so an
		# escaping exception would 500 and make the gateway retry. Instead, treat
		# contention as "someone else is already handling this" and report the PSL's
		# current state.
		try:
			psl.lock(timeout=5)
		except frappe.DocumentLockedError:
			# Another process is already handling this PSL; report its current state.
			psl.reload()
			return Processed(
				message=_(psl.status),
				action=dict(href="/", label=_("Go to Homepage")),
				status_changed_to=psl.status,
				indicator_color=psl.get_indicator_color(),
				payload={},
			)

		try:
			psl.reload()

			# After acquiring the lock, check if another process already handled this
			if psl.is_terminal():
				return Processed(
					message=_(psl.status),
					action=dict(href="/", label=_("Go to Homepage")),
					status_changed_to=psl.status,
					indicator_color=psl.get_indicator_color(),
					payload={},
				)

			self.state = psl.load_state()
			self.state.response = response

			ref_doc = frappe.get_doc(
				self.state.tx_data.reference_doctype,
				self.state.tx_data.reference_docname,
			)

			mute = self._is_server_to_server()

			def make_error_processed(error, message):
				return Processed(
					message=message,
					action=self._build_compensatory_action(psl, error),
					status_changed_to=_("Server Error"),
					indicator_color="red",
					payload={},
				)

			try:
				processed = self._process_response(psl, ref_doc)
				if self.flags.status_changed_to in self.flowstates.declined:
					try:
						msg = self._render_failure_message()
						ref_doc.flags.payment_failure_message = msg
						ref_doc.run_method("on_payment_failed", msg)
					except Exception:
						# Ensure no details are leaked to the client
						frappe.local.message_log = []
						psl.log_error("Setting failure message on ref doc failed")

			except PayloadIntegrityError:
				error = psl.log_error("Response validation failure")
				if not mute:
					return make_error_processed(error, _("There's been an issue with your payment."))

			except PaymentControllerProcessingError as e:
				error = psl.log_error(f"Processing error ({e.psltype})")
				psl.set_processing_payload(response, "Error")
				if not mute:
					return make_error_processed(error, _error_value(error, e.psltype))

			except RefDocHookProcessingError as e:
				error = psl.log_error(f"Processing failure ({e.psltype} - refdoc hook)", e.__cause__)
				psl.set_processing_payload(response, "Error - RefDoc")
				if not mute:
					return make_error_processed(error, _error_value(error, f"{e.psltype} (via ref doc hook)"))
			else:
				return processed
		finally:
			psl.unlock()

	# Lifecycle hooks (contracts)
	#  - implement them for your controller
	# ---------------------------------------

	def validate_tx_data(self, tx_data: TxData) -> None:
		"""Invoked by the reference document for example in order to validate the transaction data.

		Should throw on error with an informative user facing message.
		"""
		raise NotImplementedError

	def is_user_flow_initiation_delegated(self, psl_name: PSLName) -> bool:
		"""If true, you should initiate the user flow from the Ref Doc.

		For example, by sending an email (with a payment url), letting the user make a phone call or initiating a factoring process.

		If false, the gateway initiates the user flow.
		"""
		return False

	# Concrete controller methods
	#  - implement them for your gateway
	# ---------------------------------------

	def _patch_tx_data(self, tx_data: TxData) -> TxData:
		"""Optional: Implement tx_data preprocessing if required by the gateway.
		For example in order to fix rounding or decimal accuracy.
		"""
		return tx_data

	def _pre_data_capture_hook(self) -> dict:
		"""Optional: Implement additional server side control flow prior to data capture.
		For example in order to fetch additional data from the gateway that must be already present
		during the data capture.

		This is NOT used in Buttons with the Third Party Widget implementation variant.
		"""
		return {}

	def _initiate_charge(self) -> Initiated:
		"""Invoked by proceed in order to initiate a charge flow.

		Implementations can read:
		- self.state.psl
		- self.state.tx_data
		"""
		raise NotImplementedError

	def _validate_response(self) -> None:
		"""Implement how the validation of the response signature

		Implementations can read:
		- self.state.psl
		- self.state.tx_data
		- self.state.response
		"""
		raise NotImplementedError

	def _process_response_for_charge(self) -> Processed | None:
		"""Implement how the controller should process charge responses

		Needs to be idempotent.

		Implementations can read:
		- self.state.psl
		- self.state.tx_data
		- self.state.response
		"""
		raise NotImplementedError

	def _initiate_mandated_charge(self) -> Initiated:
		"""Invoked by charge_mandate to initiate an off-session charge against a stored mandate.

		Implementations can read:
		- self.state.psl
		- self.state.tx_data  (tx_data.mandate holds the PaymentMandate ref)

		Should confirm synchronously where the gateway supports it.
		"""
		raise NotImplementedError

	def _process_response_for_mandated_charge(self) -> Processed | None:
		"""Implement how the controller should process off-session mandated-charge responses.

		Needs to be idempotent.

		Implementations can read:
		- self.state.psl
		- self.state.tx_data
		- self.state.response
		"""
		raise NotImplementedError

	def _render_failure_message(self) -> str:
		"""Extract a readable failure message out of the server response

		Implementations can read:
		- self.state.psl
		- self.state.tx_data
		- self.state.response
		"""
		raise NotImplementedError

	def _is_server_to_server(self) -> bool:
		"""If this is a server to server processing flow.

		In this case, no errors will be returned.

		Implementations can read:
		- self.state.response
		"""
		raise NotImplementedError


@frappe.whitelist()
def frontend_defaults(doctype):
	if not isinstance(doctype, str):
		frappe.throw(_("Invalid parameter"), frappe.ValidationError)

	# Only allow DocTypes that are registered as payment gateways
	if not frappe.db.exists("Payment Gateway", {"gateway_settings": doctype}):
		frappe.throw(_("Not a valid payment gateway"), frappe.ValidationError)

	c: PaymentController = get_controller(doctype)
	if issubclass(c, PaymentController):
		d: FrontendDefaults = c.frontend_defaults
		return d.__dict__
