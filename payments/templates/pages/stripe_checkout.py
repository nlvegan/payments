# Copyright (c) 2021, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE

import json

import frappe
from frappe import _
from frappe.utils import flt, fmt_money

from payments.payment_gateways.doctype.stripe_settings.stripe_settings import (
	get_gateway_controller,
)

no_cache = 1

# Keys that should be present in the URL for the checkout page
# Note: amount/currency can be derived from reference document for security
REQUIRED_KEYS = (
	"reference_doctype",
	"reference_docname",
)

OPTIONAL_KEYS = (
	"amount",  # Will be validated/overridden from reference doc
	"currency",  # Will be validated/overridden from reference doc
	"title",
	"description",
	"payer_name",
	"payer_email",
	"payment_gateway",
	"order_id",
)

# Fields to check for amount on reference documents (in order of preference)
AMOUNT_FIELDS = ("grand_total", "total_amount", "amount", "outstanding_amount", "total")
CURRENCY_FIELDS = ("currency", "payment_currency", "transaction_currency")


def get_amount_and_currency_from_reference(reference_doctype, reference_docname):
	"""
	Securely fetch amount and currency from the reference document.
	This prevents URL parameter tampering attacks.

	Returns:
	        tuple: (amount, currency) or (None, None) if not found
	"""
	try:
		# Use ignore_permissions since checkout page is publicly accessible
		# The payment link itself serves as authorization to view payment details
		reference_doc = frappe.get_doc(reference_doctype, reference_docname)
	except frappe.DoesNotExistError:
		return None, None

	# Find amount field
	amount = None
	for field in AMOUNT_FIELDS:
		if hasattr(reference_doc, field):
			value = getattr(reference_doc, field)
			if value is not None:
				amount = flt(value)
				break

	# Find currency field
	currency = None
	for field in CURRENCY_FIELDS:
		if hasattr(reference_doc, field):
			value = getattr(reference_doc, field)
			if value:
				currency = value
				break

	# Default currency if not found
	if not currency:
		currency = (
			frappe.get_cached_value(
				"Company", frappe.defaults.get_global_default("company"), "default_currency"
			)
			or "USD"
		)

	return amount, currency


def validate_and_override_payment_params(data, reference_doctype, reference_docname):
	"""Validate amount/currency against reference document to prevent tampering.

	Overrides data["amount"] and data["currency"] with server-side values.
	Returns (amount, currency) or throws if amount cannot be determined.
	"""
	ref_amount, ref_currency = get_amount_and_currency_from_reference(reference_doctype, reference_docname)

	if ref_amount is not None:
		data["amount"] = ref_amount
	elif not data.get("amount"):
		frappe.throw(_("Could not determine payment amount from reference document"))

	if ref_currency:
		data["currency"] = ref_currency
	elif not data.get("currency"):
		data["currency"] = "USD"

	return data["amount"], data["currency"]


def get_context(context):
	context.no_cache = 1

	# Check for required keys
	missing_keys = set(REQUIRED_KEYS) - set(frappe.form_dict.keys())
	if missing_keys:
		frappe.redirect_to_message(
			_("Missing Information"),
			_("The following required parameters are missing: {0}").format(", ".join(missing_keys)),
		)
		frappe.local.flags.redirect_location = frappe.local.response.location
		raise frappe.Redirect

	# Populate context with form data
	for key in REQUIRED_KEYS + OPTIONAL_KEYS:
		context[key] = frappe.form_dict.get(key, "")

	# SECURITY: Validate and override amount/currency from reference document
	# This prevents URL parameter tampering (Issue #148)
	try:
		validate_and_override_payment_params(context, context.reference_doctype, context.reference_docname)
	except frappe.ValidationError:
		frappe.redirect_to_message(
			_("Invalid Reference"),
			_("Could not determine payment amount from the reference document."),
		)
		frappe.local.flags.redirect_location = frappe.local.response.location
		raise frappe.Redirect

	# Get the gateway controller
	gateway_controller = get_gateway_controller(
		context.reference_doctype, context.reference_docname, context.payment_gateway or None
	)

	if not gateway_controller:
		frappe.redirect_to_message(
			_("Configuration Error"),
			_("Payment gateway is not properly configured. Please contact support."),
		)
		frappe.local.flags.redirect_location = frappe.local.response.location
		raise frappe.Redirect

	# Get Stripe settings
	stripe_settings = frappe.get_doc("Stripe Settings", gateway_controller)
	context.publishable_key = stripe_settings.publishable_key
	context.image = stripe_settings.header_img
	context.gateway_controller = gateway_controller
	context.collect_billing_address = stripe_settings.collect_billing_address

	# Format amount for display
	context.display_amount = fmt_money(amount=context.amount, currency=context.currency)

	# Store data for JavaScript (amount is now validated server-side)
	context.payment_data = json.dumps(
		{
			"amount": context.amount,
			"currency": context.currency,
			"reference_doctype": context.reference_doctype,
			"reference_docname": context.reference_docname,
			"payer_name": context.payer_name,
			"payer_email": context.payer_email,
			"description": context.description or f"Payment for {context.reference_docname}",
			"gateway_controller": gateway_controller,
		}
	)


@frappe.whitelist(allow_guest=True)
def create_payment_intent(data):
	"""
	Create a PaymentIntent on the server side.

	SECURITY: Amount and currency are validated against the reference document
	to prevent URL/API parameter tampering (Issue #148).

	Args:
	        data: JSON string with payment data

	Returns:
	        dict with client_secret and payment_intent_id
	"""
	if isinstance(data, str):
		data = json.loads(data)

	gateway_controller = data.get("gateway_controller")
	if not gateway_controller:
		frappe.throw(_("Gateway controller not specified"))

	reference_doctype = data.get("reference_doctype")
	reference_docname = data.get("reference_docname")

	if not reference_doctype or not reference_docname:
		frappe.throw(_("Reference document not specified"))

	# SECURITY: Validate and override amount/currency from reference document
	validate_and_override_payment_params(data, reference_doctype, reference_docname)

	stripe_settings = frappe.get_doc("Stripe Settings", gateway_controller)
	return stripe_settings.create_payment_intent(data)


@frappe.whitelist(allow_guest=True)
def confirm_payment(payment_intent_id, reference_doctype, reference_docname):
	"""
	Confirm that a payment was successful (called from frontend after Stripe confirms).

	This is a fallback in case the webhook hasn't processed yet.
	The webhook will handle the actual business logic; this just returns the redirect URL.

	Args:
	        payment_intent_id: The Stripe PaymentIntent ID
	        reference_doctype: The reference document type
	        reference_docname: The reference document name

	Returns:
	        dict with redirect_to URL
	"""
	import stripe

	# Resolve the correct Stripe Settings from the gateway_controller passed by the frontend,
	# falling back to the first Stripe Settings if not specified
	gateway_controller = frappe.form_dict.get("gateway_controller")
	if gateway_controller and frappe.db.exists("Stripe Settings", gateway_controller):
		settings = frappe.get_doc("Stripe Settings", gateway_controller)
	else:
		stripe_settings_list = frappe.get_all("Stripe Settings", limit=1)
		if not stripe_settings_list:
			frappe.throw(_("Stripe Settings not configured"))
		settings = frappe.get_doc("Stripe Settings", stripe_settings_list[0].name)

	stripe.api_key = settings.get_stripe_api_key()

	try:
		# Retrieve the PaymentIntent to check its status
		intent = stripe.PaymentIntent.retrieve(payment_intent_id)

		# SECURITY: Verify the client-supplied reference matches the PaymentIntent metadata
		# This prevents an attacker from pairing a valid payment_intent_id with a different document
		intent_ref_doctype = intent.metadata.get("reference_doctype")
		intent_ref_docname = intent.metadata.get("reference_docname")
		if intent_ref_doctype and intent_ref_docname:
			if intent_ref_doctype != reference_doctype or intent_ref_docname != reference_docname:
				frappe.log_error(
					title="Stripe: reference mismatch in confirm_payment",
					message=f"Client sent {reference_doctype}/{reference_docname}, "
					f"but PaymentIntent has {intent_ref_doctype}/{intent_ref_docname}",
				)
				return {
					"status": "error",
					"redirect_to": "/payment-failed",
					"message": _("Payment reference mismatch. Please contact support."),
				}

		if intent.status == "succeeded":
			# Payment successful - update Integration Request status
			integration_request_name = intent.metadata.get("integration_request")
			if integration_request_name:
				frappe.db.set_value(
					"Integration Request",
					integration_request_name,
					{
						"status": "Completed",
						"output": json.dumps(
							{
								"payment_intent_id": intent.id,
								"status": intent.status,
								"amount": intent.amount,
								"currency": intent.currency,
							}
						),
					},
				)

			# Trigger on_payment_authorized as a fallback (webhook should handle this too)
			if reference_doctype and reference_docname:
				try:
					ref_doc = frappe.get_doc(reference_doctype, reference_docname)
					ref_doc.run_method("on_payment_authorized", "Completed")
					frappe.db.commit()
				except Exception:
					# Log but don't fail - webhook will handle it
					frappe.log_error(
						title="Stripe: on_payment_authorized fallback failed", message=frappe.get_traceback()
					)

			redirect_url = (
				settings.redirect_url
				or f"/payment-success?doctype={reference_doctype}&docname={reference_docname}"
			)
			return {
				"status": "success",
				"redirect_to": redirect_url,
			}

		elif intent.status == "requires_action":
			# 3D Secure or other action required
			return {
				"status": "requires_action",
				"message": _("Additional authentication required"),
			}

		elif intent.status == "processing":
			# Payment is still processing
			return {
				"status": "processing",
				"message": _("Payment is being processed. You will receive a confirmation shortly."),
				"redirect_to": f"/payment-success?doctype={reference_doctype}&docname={reference_docname}&status=processing",
			}

		else:
			# Payment failed or was canceled
			return {
				"status": "failed",
				"redirect_to": "/payment-failed",
				"message": _("Payment was not successful. Please try again."),
			}

	except stripe.error.StripeError as e:
		frappe.log_error(title="Stripe Payment Confirmation Error", message=str(e))
		# Return generic message to client - raw Stripe errors may leak internal details
		return {
			"status": "error",
			"redirect_to": "/payment-failed",
			"message": _(
				"An error occurred while processing your payment. Please try again or contact support."
			),
		}
