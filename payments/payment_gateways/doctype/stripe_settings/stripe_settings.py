# Copyright (c) 2017, Frappe Technologies and contributors
# License: MIT. See LICENSE

import json
from typing import ClassVar
from urllib.parse import urlencode

import frappe
import stripe
from frappe import _
from frappe.integrations.utils import create_request_log
from frappe.utils import call_hook_method, flt, get_url

from payments.controllers import PaymentController
from payments.types import (
	FrontendDefaults,
	GatewayProcessingResponse,
	Initiated,
	Processed,
	RemoteServerInitiationPayload,
	SessionStates,
	TxData,
)
from payments.utils import create_payment_gateway

# Minimum charge amounts by currency (in major currency units)
CURRENCY_MINIMUM_AMOUNTS = {
	"JPY": 50,
	"MXN": 10,
	"DKK": 2.50,
	"HKD": 4.00,
	"NOK": 3.00,
	"SEK": 3.00,
	"USD": 0.50,
	"AUD": 0.50,
	"BRL": 0.50,
	"CAD": 0.50,
	"CHF": 0.50,
	"EUR": 0.50,
	"GBP": 0.30,
	"NZD": 0.50,
	"SGD": 0.50,
}

# Currencies that use zero decimal (amount in smallest unit = amount in major unit)
ZERO_DECIMAL_CURRENCIES = {
	"BIF",
	"CLP",
	"DJF",
	"GNF",
	"JPY",
	"KMF",
	"KRW",
	"MGA",
	"PYG",
	"RWF",
	"UGX",
	"VND",
	"VUV",
	"XAF",
	"XOF",
	"XPF",
}

# Pin API version to ensure consistent behavior across SDK updates
# See: https://docs.stripe.com/api/versioning
STRIPE_API_VERSION = "2025-12-15.clover"

# PII fields to redact from debug logs
PII_FIELDS = {"payer_email", "email", "receipt_email", "name", "phone", "address"}


def _sanitize_for_logging(data: dict) -> dict:
	"""Remove PII from data before logging.

	Recursively redacts sensitive fields from nested dictionaries.
	"""
	if not isinstance(data, dict):
		return data

	sanitized = {}
	for key, value in data.items():
		if key in PII_FIELDS:
			sanitized[key] = "***REDACTED***"
		elif isinstance(value, dict):
			sanitized[key] = _sanitize_for_logging(value)
		else:
			sanitized[key] = value
	return sanitized


# Stripe frontend templates for PaymentController integration
STRIPE_CSS = """
<style>
.stripe-element {
	padding: 12px;
	border: 1px solid #e6ebf1;
	border-radius: 4px;
	background: #fff;
}
.stripe-element.StripeElement--focus {
	border-color: #635bff;
	box-shadow: 0 0 0 3px rgba(99, 91, 255, 0.1);
}
.stripe-element.StripeElement--invalid {
	border-color: #dc3545;
}
</style>
"""

STRIPE_JS = """
<script src="https://js.stripe.com/v3/"></script>
<script>
const stripe = Stripe("{{ doc.publishable_key }}");
const elements = stripe.elements();
const card = elements.create('card', {hidePostalCode: true});
card.mount('#stripe-card-element');

window.stripeConfirmPayment = async function(clientSecret) {
	const { error, paymentIntent } = await stripe.confirmCardPayment(clientSecret, {
		payment_method: { card: card }
	});
	return { error, paymentIntent };
};
</script>
"""

STRIPE_WRAPPER = """
<div id="stripe-card-element" class="stripe-element"></div>
<div id="stripe-errors" style="color: #dc3545; font-size: 13px; margin-top: 8px;"></div>
"""


class StripeSettings(PaymentController):
	"""Stripe payment gateway controller.

	Extends PaymentController to integrate with the payments app architecture
	while maintaining backward compatibility with direct checkout flow.
	"""

	# PaymentController required class attributes
	flowstates = SessionStates(
		success=["succeeded"],
		pre_authorized=["requires_capture"],
		processing=["processing", "requires_action", "requires_confirmation"],
		declined=["canceled", "requires_payment_method"],
	)

	# Webhook event types routed through PaymentController.process_response when the
	# event carries metadata.psl_name (v2 flow). Others fall back to legacy handlers.
	V2_ROUTED_EVENTS: ClassVar[frozenset] = frozenset(
		{
			"payment_intent.succeeded",
			"payment_intent.payment_failed",
			"payment_intent.canceled",
		}
	)

	frontend_defaults = FrontendDefaults(
		gateway_css=STRIPE_CSS,
		gateway_js=STRIPE_JS,
		gateway_wrapper=STRIPE_WRAPPER,
	)
	supported_currencies = (
		"AED",
		"ALL",
		"ANG",
		"ARS",
		"AUD",
		"AWG",
		"BBD",
		"BDT",
		"BIF",
		"BMD",
		"BND",
		"BOB",
		"BRL",
		"BSD",
		"BWP",
		"BZD",
		"CAD",
		"CHF",
		"CLP",
		"CNY",
		"COP",
		"CRC",
		"CVE",
		"CZK",
		"DJF",
		"DKK",
		"DOP",
		"DZD",
		"EGP",
		"ETB",
		"EUR",
		"FJD",
		"FKP",
		"GBP",
		"GIP",
		"GMD",
		"GNF",
		"GTQ",
		"GYD",
		"HKD",
		"HNL",
		"HRK",
		"HTG",
		"HUF",
		"IDR",
		"ILS",
		"INR",
		"ISK",
		"JMD",
		"JPY",
		"KES",
		"KHR",
		"KMF",
		"KRW",
		"KYD",
		"KZT",
		"LAK",
		"LBP",
		"LKR",
		"LRD",
		"MAD",
		"MDL",
		"MGA",
		"MNT",
		"MOP",
		"MRO",
		"MUR",
		"MVR",
		"MWK",
		"MXN",
		"MYR",
		"NAD",
		"NGN",
		"NIO",
		"NOK",
		"NPR",
		"NZD",
		"PAB",
		"PEN",
		"PGK",
		"PHP",
		"PKR",
		"PLN",
		"PYG",
		"QAR",
		"RON",
		"RUB",
		"RWF",
		"SAR",
		"SBD",
		"SCR",
		"SEK",
		"SGD",
		"SHP",
		"SLL",
		"SOS",
		"STD",
		"SVC",
		"SZL",
		"THB",
		"TOP",
		"TTD",
		"TWD",
		"TZS",
		"UAH",
		"UGX",
		"USD",
		"UYU",
		"UZS",
		"VND",
		"VUV",
		"WST",
		"XAF",
		"XOF",
		"XPF",
		"YER",
		"ZAR",
	)

	@staticmethod
	def _get_payer_email(payer_contact: dict | None) -> str:
		"""Extract email from payer_contact dict.

		Frappe Contact uses 'email_id' as the primary email field,
		but some integrations may use 'email'. Check both for compatibility.
		"""
		if not payer_contact:
			return ""
		# Prefer email_id (Frappe standard), fall back to email
		return payer_contact.get("email_id") or payer_contact.get("email") or ""

	def onload(self):
		"""Set computed fields when document is loaded."""
		# Set the webhook endpoint URL for display
		self.set_onload("webhook_endpoint_url", self.get_webhook_endpoint_url())

	def get_webhook_endpoint_url(self):
		"""Get the webhook endpoint URL for this site."""
		return get_url(
			"/api/method/payments.payment_gateways.doctype.stripe_settings.stripe_settings.stripe_webhook"
		)

	def on_update(self):
		create_payment_gateway(
			"Stripe-" + self.gateway_name,
			settings="Stripe Settings",
			controller=self.gateway_name,
		)
		call_hook_method("payment_gateway_enabled", gateway="Stripe-" + self.gateway_name)
		if not self.flags.ignore_mandatory:
			self.validate_stripe_credentials()

	def validate_stripe_credentials(self):
		"""Validate that the API keys are correct by making a test API call."""
		if self.publishable_key and self.secret_key:
			try:
				stripe.api_key = self.get_password(fieldname="secret_key", raise_exception=False)
				stripe.api_version = STRIPE_API_VERSION
				# Use a simple API call to validate credentials
				stripe.Account.retrieve()
			except stripe.error.AuthenticationError:
				frappe.throw(_("Invalid Stripe API keys. Please check your Publishable Key and Secret Key."))
			except Exception as e:
				frappe.throw(_("Error validating Stripe credentials: {0}").format(str(e)))

	def validate_transaction_currency(self, currency):
		if currency not in self.supported_currencies:
			frappe.throw(
				_(
					"Please select another payment method. Stripe does not support transactions in currency '{0}'"
				).format(currency)
			)

	def validate_minimum_transaction_amount(self, currency, amount):
		minimum = CURRENCY_MINIMUM_AMOUNTS.get(currency, 0.50)
		if flt(amount) < minimum:
			frappe.throw(
				_("For currency {0}, the minimum transaction amount is {1}").format(currency, minimum)
			)

	def get_payment_url(self, **kwargs):
		"""Generate the URL to the Stripe checkout page."""
		return get_url(f"./stripe_checkout?{urlencode(kwargs)}")

	def get_stripe_api_key(self):
		"""Get the Stripe secret key."""
		return self.get_password(fieldname="secret_key", raise_exception=False)

	def get_webhook_secret(self):
		"""Get the webhook signing secret."""
		return self.get_password(fieldname="webhook_secret", raise_exception=False)

	def convert_to_stripe_amount(self, amount, currency):
		"""Convert amount to Stripe's smallest currency unit."""
		if currency.upper() in ZERO_DECIMAL_CURRENCIES:
			return int(amount)
		return int(flt(amount) * 100)

	def convert_from_stripe_amount(self, amount, currency):
		"""Convert amount from Stripe's smallest currency unit."""
		if currency.upper() in ZERO_DECIMAL_CURRENCIES:
			return flt(amount)
		return flt(amount) / 100

	def _build_intent_params(self, amount, currency, reference_docname, payer_email="", metadata=None):
		"""Build the core PaymentIntent parameters shared by legacy and v2 flows."""
		intent_params = {
			"amount": self.convert_to_stripe_amount(amount, currency),
			"currency": currency.lower(),
			"automatic_payment_methods": {"enabled": True},
			"metadata": metadata or {},
		}

		intent_params["description"] = f"Payment for {reference_docname}"

		if payer_email:
			intent_params["receipt_email"] = payer_email

		return intent_params

	def create_payment_intent(self, data):
		"""
		Create a Stripe PaymentIntent for the given payment data.

		Args:
		        data: dict containing amount, currency, reference_doctype, reference_docname, etc.

		Returns:
		        dict with client_secret and payment_intent_id
		"""
		self.data = frappe._dict(data)

		# Validate
		self.validate_transaction_currency(self.data.currency)
		self.validate_minimum_transaction_amount(self.data.currency, self.data.amount)

		stripe.api_key = self.get_stripe_api_key()
		stripe.api_version = STRIPE_API_VERSION

		try:
			# Create integration request log
			self.integration_request = create_request_log(self.data, service_name="Stripe")

			intent_params = self._build_intent_params(
				amount=self.data.amount,
				currency=self.data.currency,
				reference_docname=self.data.get("reference_docname", ""),
				payer_email=self.data.get("payer_email", ""),
				metadata={
					"reference_doctype": self.data.get("reference_doctype", ""),
					"reference_docname": self.data.get("reference_docname", ""),
					"integration_request": self.integration_request.name,
					"payer_email": self.data.get("payer_email", ""),
				},
			)

			# Legacy flow: use description from data if provided (overrides default)
			if self.data.get("description"):
				intent_params["description"] = self.data.description

			# Create the PaymentIntent with idempotency key to prevent duplicate charges
			intent = stripe.PaymentIntent.create(
				**intent_params,
				idempotency_key=f"ir-{self.integration_request.name}",
			)

			# Update integration request with PaymentIntent ID
			self.integration_request.db_set(
				"output",
				json.dumps(
					{
						"payment_intent_id": intent.id,
						"status": intent.status,
					}
				),
			)

			if self.enable_debug_logging:
				frappe.log_error(
					title="Stripe PaymentIntent Created",
					message=f"PaymentIntent {intent.id} created for {self.data.reference_docname}",
				)

			return {
				"client_secret": intent.client_secret,
				"payment_intent_id": intent.id,
				"publishable_key": self.publishable_key,
			}

		except stripe.error.CardError as e:
			frappe.log_error(title="Stripe Card Error", message=str(e))
			frappe.throw(_("Card error: {0}").format(e.user_message or str(e)))

		except stripe.error.InvalidRequestError as e:
			frappe.log_error(title="Stripe Invalid Request", message=str(e))
			frappe.throw(_("Invalid request: {0}").format(str(e)))

		except stripe.error.AuthenticationError:
			frappe.log_error(title="Stripe Auth Error", message="Authentication failed")
			frappe.throw(_("Stripe authentication failed. Please check your API keys."))

		except Exception:
			frappe.log_error(title="Stripe Error", message=frappe.get_traceback())
			frappe.throw(_("An error occurred while processing your payment. Please try again."))

	def handle_webhook_event(self, event):
		"""
		Handle a Stripe webhook event.

		Args:
		        event: Stripe event object

		Returns:
		        dict with status
		"""
		event_type = event["type"]
		data = event["data"]["object"]

		if self.enable_debug_logging:
			frappe.log_error(
				title=f"Stripe Webhook: {event_type}",
				message=json.dumps(_sanitize_for_logging(data), indent=2),
			)

		if event_type in self.V2_ROUTED_EVENTS and data.get("metadata", {}).get("psl_name"):
			return self._process_webhook_via_psl(data)

		if event_type == "payment_intent.succeeded":
			return self._handle_payment_success(data)
		elif event_type == "payment_intent.payment_failed":
			return self._handle_payment_failure(data)
		elif event_type == "payment_intent.canceled":
			return self._handle_payment_canceled(data)
		else:
			# Log unhandled events for debugging
			# Note: The following events are intentionally not handled:
			# - payment_intent.requires_action: 3D Secure handled client-side by Stripe.js
			# - charge.refunded: Refunds managed via Frappe document workflows
			# - charge.dispute.*: Disputes require manual review in Stripe Dashboard
			# - payment_intent.amount_capturable: Separate auth/capture not implemented
			if self.enable_debug_logging:
				frappe.log_error(
					title=f"Stripe Webhook Unhandled: {event_type}",
					message=json.dumps(_sanitize_for_logging(data), indent=2),
				)
			return {"status": "ignored", "event_type": event_type}

	def _process_webhook_via_psl(self, payment_intent) -> dict:
		"""Route a v2 (PSL-based) webhook event through the controller pipeline.

		The Stripe signature was already verified in stripe_webhook(); the truthy
		``hash`` marks this as a verified server-to-server response so the controller
		skips client-side re-retrieval and applies error muting. process_response
		resolves its own controller from the PSL and is idempotent via its PSL lock +
		is_terminal() guard, so redelivery after a terminal state is a no-op.

		``payment_intent["id"]`` is a Stripe schema guarantee (same assumption the
		legacy _handle_payment_* handlers make). The process_response return value is
		intentionally discarded — the PSL holds the authoritative outcome (and the
		muted server-to-server path returns None on internal error).
		"""
		psl_name = payment_intent.get("metadata", {}).get("psl_name")
		response = GatewayProcessingResponse(
			hash=payment_intent["id"].encode(),
			message=None,
			payload=payment_intent,
		)
		PaymentController.process_response(psl_name, response)
		return {"status": "processed", "psl_name": psl_name}

	def _handle_payment_success(self, payment_intent):
		"""Handle successful payment."""
		metadata = payment_intent.get("metadata", {})
		reference_doctype = metadata.get("reference_doctype")
		reference_docname = metadata.get("reference_docname")
		integration_request_name = metadata.get("integration_request")

		# Update integration request
		if integration_request_name:
			frappe.db.set_value(
				"Integration Request",
				integration_request_name,
				{
					"status": "Completed",
					"output": json.dumps(payment_intent),
				},
			)

		# Call the on_payment_authorized hook on the reference document
		if reference_doctype and reference_docname:
			try:
				ref_doc = frappe.get_doc(reference_doctype, reference_docname)
				ref_doc.run_method("on_payment_authorized", "Completed")
				frappe.db.commit()
			except Exception:
				frappe.log_error(
					title="Stripe Webhook: on_payment_authorized failed", message=frappe.get_traceback()
				)

		return {"status": "success", "payment_intent_id": payment_intent["id"]}

	def _handle_payment_failure(self, payment_intent):
		"""Handle failed payment."""
		metadata = payment_intent.get("metadata", {})
		integration_request_name = metadata.get("integration_request")
		last_error = payment_intent.get("last_payment_error", {})
		error_message = last_error.get("message", "Payment failed")

		# Update integration request
		if integration_request_name:
			frappe.db.set_value(
				"Integration Request",
				integration_request_name,
				{
					"status": "Failed",
					"error": error_message,
					"output": json.dumps(payment_intent),
				},
			)

		return {"status": "failed", "error": error_message}

	def _handle_payment_canceled(self, payment_intent):
		"""Handle canceled payment."""
		metadata = payment_intent.get("metadata", {})
		integration_request_name = metadata.get("integration_request")

		# Update integration request
		if integration_request_name:
			frappe.db.set_value(
				"Integration Request",
				integration_request_name,
				{
					"status": "Cancelled",
					"output": json.dumps(payment_intent),
				},
			)

		return {"status": "canceled"}

	# PaymentController interface methods
	# ------------------------------------

	def validate_tx_data(self, tx_data: TxData) -> None:
		"""Validate transaction data before initiating payment flow."""
		self.validate_transaction_currency(tx_data.currency)
		self.validate_minimum_transaction_amount(tx_data.currency, tx_data.amount)

	def _initiate_charge(self) -> Initiated:
		"""Create a Stripe PaymentIntent for the charge flow."""
		stripe.api_key = self.get_stripe_api_key()
		stripe.api_version = STRIPE_API_VERSION

		tx_data = self.state.tx_data
		psl = self.state.psl
		payer_email = self._get_payer_email(tx_data.payer_contact)

		intent_params = self._build_intent_params(
			amount=tx_data.amount,
			currency=tx_data.currency,
			reference_docname=tx_data.reference_docname,
			payer_email=payer_email,
			metadata={
				"reference_doctype": tx_data.reference_doctype,
				"reference_docname": tx_data.reference_docname,
				"psl_name": psl.name,
				"payer_email": payer_email,
			},
		)

		intent = stripe.PaymentIntent.create(
			**intent_params,
			idempotency_key=f"psl-{psl.name}",
		)

		return Initiated(
			correlation_id=intent.id,
			payload=RemoteServerInitiationPayload(
				{
					"client_secret": intent.client_secret,
					"payment_intent_id": intent.id,
					"publishable_key": self.publishable_key,
				}
			),
		)

	def _validate_response(self) -> None:
		"""Validate the webhook signature for server-to-server responses."""
		response = self.state.response

		# For webhook responses, signature is already validated in stripe_webhook()
		# For client-side responses, we retrieve the PaymentIntent to verify
		if response.hash:
			# Webhook signature validation was done in stripe_webhook
			pass
		else:
			# Client-side response - verify by retrieving PaymentIntent
			stripe.api_key = self.get_stripe_api_key()
			stripe.api_version = STRIPE_API_VERSION
			intent_id = response.payload.get("id") or response.payload.get("payment_intent_id")
			if intent_id:
				intent = stripe.PaymentIntent.retrieve(intent_id)
				# Update response payload with verified data
				self.state.response = GatewayProcessingResponse(
					hash=None,
					message=None,
					payload=intent,
				)

	def _process_response_for_charge(self) -> Processed | None:
		"""Process the PaymentIntent response and set status."""
		payload = self.state.response.payload
		status = payload.get("status") if isinstance(payload, dict) else payload.status

		# Map Stripe status to our flow states
		self.flags.status_changed_to = status

		# Return None to use default processing from PaymentController
		return None

	def _render_failure_message(self) -> str:
		"""Extract error message from Stripe response."""
		payload = self.state.response.payload
		if isinstance(payload, dict):
			last_error = payload.get("last_payment_error", {})
			return last_error.get("message", _("Payment was declined"))
		return _("Payment was declined")

	def _is_server_to_server(self) -> bool:
		"""Check if this is a webhook (server-to-server) call."""
		# If response has a hash, it came from webhook with signature
		return bool(self.state.response.hash)

	# Legacy method for backwards compatibility
	def create_request(self, data):
		"""
		Legacy method - creates a PaymentIntent and returns redirect info.
		Kept for backwards compatibility with existing integrations.
		"""
		result = self.create_payment_intent(data)
		return {
			"payment_intent_id": result["payment_intent_id"],
			"client_secret": result["client_secret"],
			"redirect_to": get_url(
				f"./stripe_checkout?payment_intent_id={result['payment_intent_id']}"
				f"&reference_doctype={data.get('reference_doctype', '')}"
				f"&reference_docname={data.get('reference_docname', '')}"
			),
			"status": "Pending",
		}


def get_gateway_controller(doctype, docname, payment_gateway=None):
	"""Get the Stripe Settings controller name for a given payment gateway."""
	if not payment_gateway:
		reference_doc = frappe.get_doc(doctype, docname)
		if hasattr(reference_doc, "payment_gateway"):
			payment_gateway = reference_doc.payment_gateway
		else:
			# Try to find from Payment Request
			payment_gateway = frappe.db.get_value(
				"Payment Request",
				{"reference_doctype": doctype, "reference_name": docname},
				"payment_gateway",
			)

	if payment_gateway:
		return frappe.db.get_value("Payment Gateway", payment_gateway, "gateway_controller")

	return None


# nosemgrep: guest-whitelisted-method - public Stripe webhook endpoint; payload authenticity enforced via signature verification inside the handler
@frappe.whitelist(allow_guest=True, methods=["POST"])
def stripe_webhook():
	"""
	Handle Stripe webhook events.

	This endpoint should be configured in your Stripe Dashboard:
	https://dashboard.stripe.com/webhooks

	Endpoint URL: https://your-site.com/api/method/payments.payment_gateways.doctype.stripe_settings.stripe_settings.stripe_webhook
	"""
	payload = frappe.request.data
	sig_header = frappe.request.headers.get("Stripe-Signature")

	if not payload or not sig_header:
		frappe.throw(_("Missing payload or signature"), frappe.AuthenticationError)

	# Get the first Stripe Settings (or could be configured per-webhook)
	stripe_settings = frappe.get_all("Stripe Settings", limit=1)
	if not stripe_settings:
		frappe.throw(_("Stripe Settings not configured"), frappe.AuthenticationError)

	settings = frappe.get_doc("Stripe Settings", stripe_settings[0].name)
	webhook_secret = settings.get_webhook_secret()

	if not webhook_secret:
		frappe.log_error(
			title="Stripe Webhook Error", message="Webhook secret not configured in Stripe Settings"
		)
		frappe.throw(_("Webhook secret not configured"), frappe.AuthenticationError)

	try:
		event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
	except ValueError:
		frappe.throw(_("Invalid payload"), frappe.AuthenticationError)
	except stripe.error.SignatureVerificationError:
		frappe.throw(_("Invalid signature"), frappe.AuthenticationError)

	# Handle the event
	result = settings.handle_webhook_event(event)
	# nosemgrep: frappe-manual-commit - Required: webhook must commit before returning to payment provider
	frappe.db.commit()

	return result
