# Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE

import json

import frappe
from frappe import _
from frappe.utils import fmt_money

from payments.payment_gateways.doctype.mollie_settings.mollie_settings import (
	get_gateway_controller,
)

no_cache = 1

# Doctypes that are allowed to be used with Mollie payments.
# This prevents arbitrary document manipulation via the guest-accessible endpoint.
# Add additional doctypes here as needed for your use case.
ALLOWED_PAYMENT_DOCTYPES = (
	"Payment Request",
)

expected_keys = (
	"amount",
	"title",
	"description",
	"reference_doctype",
	"reference_docname",
	"payer_name",
	"payer_email",
	"order_id",
	"currency",
)


def get_context(context):
	context.no_cache = 1

	# all these keys exist in form_dict
	if not (set(expected_keys) - set(list(frappe.form_dict))):
		for key in expected_keys:
			context[key] = frappe.form_dict[key]

		gateway_controller = get_gateway_controller(context.reference_doctype, context.reference_docname)
		context.profile_id = get_profile_id(context.reference_docname, gateway_controller)
		context.image = get_header_image(context.reference_docname, gateway_controller)

		context["amount"] = fmt_money(amount=context["amount"], currency=context["currency"])

	else:
		frappe.log_error("Data to complete the payment is missing", frappe.form_dict)
		frappe.redirect_to_message(
			_("Some information is missing"),
			_("Looks like someone sent you to an incomplete URL. Please ask them to look into it."),
		)
		frappe.local.flags.redirect_location = frappe.local.response.location
		raise frappe.Redirect
		


def get_profile_id(doc, gateway_controller):
	"""Get the appropriate Mollie profile ID (sandbox or live)."""
	mollie_settings = frappe.get_doc("Mollie Settings", gateway_controller)
	return mollie_settings.get_active_profile_id()


def get_header_image(doc, gateway_controller):
	return frappe.db.get_value("Mollie Settings", gateway_controller, "header_img")


@frappe.whitelist(allow_guest=True)
def make_payment(data, reference_doctype, reference_docname):
	# Validate reference_doctype to prevent arbitrary document manipulation
	if reference_doctype not in ALLOWED_PAYMENT_DOCTYPES:
		frappe.throw(
			_("Invalid reference doctype for Mollie payment: {0}").format(reference_doctype),
			frappe.PermissionError,
		)

	data = json.loads(data)
	gateway_controller = get_gateway_controller(reference_doctype, reference_docname)
	paymentID = frappe.db.get_value(reference_doctype, reference_docname, "payment_id")

	if not paymentID:
		data = frappe.get_doc("Mollie Settings", gateway_controller).create_request(data)
		paymentID = data["paymentID"]

	status = frappe.get_doc("Mollie Settings", gateway_controller).check_request(data, paymentID)
	data["paymentUrl"] = status["paymentUrl"]

	# Check if payment was already completed (stored locally)
	try:
		status_field = frappe.db.get_value(reference_doctype, reference_docname, "payment_status")
		if status_field == "Completed":
			status["status"] = status_field
	except frappe.exceptions.DoesNotExistError:
		pass  # Field doesn't exist on this doctype

	if status["status"] == "Cancelled":
		data = frappe.get_doc("Mollie Settings", gateway_controller).create_request(data)
		paymentID = data["paymentID"]
		status = "Open"
		data["status"] = status
	else:
		status = status["status"]
		data["status"] = status

	# Update payment status on reference document if field exists
	if frappe.get_meta(reference_doctype).has_field("payment_status"):
		frappe.db.set_value(reference_doctype, reference_docname, "payment_status", status)

	frappe.db.commit()
	return data
