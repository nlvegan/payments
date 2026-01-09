# Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE

from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.integrations.utils import create_request_log, make_get_request
from frappe.model.document import Document
from frappe.utils import call_hook_method, cint, get_url

from mollie.api.client import Client
from mollie.api.error import Error as MollieError

from payments.utils import create_payment_gateway
from payments.utils.utils import log_payment_error


class MollieSettings(Document):
	supported_currencies = [
		"AED",
		"AUD",
		"BGN",
		"BRL",
		"CAD",
		"CHF",
		"CZK",
		"DKK",
		"EUR",
		"GBP",
		"HKD",
		"HUF",
		"ILS",
		"ISK",
		"JPY",
		"MXN",
		"MYR",
		"NOK",
		"NZD",
		"PHP",
		"PLN",
		"RON",
		"RUB",
		"SEK",
		"SGD",
		"THB",
		"TWD",
		"USD",
		"ZAR",
	]

	def on_update(self):
		create_payment_gateway(
			"Mollie-" + self.gateway_name,
			settings="Mollie Settings",
			controller=self.gateway_name,
		)
		call_hook_method("payment_gateway_enabled", gateway="Mollie-" + self.gateway_name)
		if not self.flags.ignore_mandatory:
			self.validate_mollie_credentials()

	def validate_mollie_credentials(self):
		"""Validate the currently active API credentials (sandbox or live)."""
		api_key = self.get_api_key()
		if api_key:
			header = {"Authorization": f"Bearer {api_key}"}
			try:
				make_get_request(url="https://api.mollie.com/v2/payments", headers=header)
			except Exception:
				mode = "sandbox" if self.is_sandbox_mode() else "live"
				frappe.throw(
					_("Mollie API validation failed. Please check your {0} credentials.").format(mode)
				)

	def validate_transaction_currency(self, currency):
		if currency not in self.supported_currencies:
			frappe.throw(
				_(
					"Please select another payment method. Mollie does not support transactions in currency '{0}'"
				).format(currency)
			)

	def is_sandbox_mode(self):
		"""Check if sandbox mode is enabled.

		Returns True if:
		- use_sandbox checkbox is checked in the DocType, OR
		- use_sandbox is passed in the request form_dict
		"""
		return cint(self.use_sandbox) or cint(frappe.form_dict.get("use_sandbox"))

	def get_api_key(self):
		"""Get the appropriate API key based on sandbox mode.

		Priority for sandbox mode:
		1. sandbox_secret_key from DocType
		2. sandbox_secret_key from site_config.json

		Priority for live mode:
		1. secret_key from DocType
		"""
		if self.is_sandbox_mode():
			# Try DocType field first, then fall back to site config
			sandbox_key = self.get_password(fieldname="sandbox_secret_key", raise_exception=False)
			if sandbox_key:
				return sandbox_key
			# Fallback to site_config.json
			return frappe.conf.get("sandbox_secret_key") or frappe.conf.get("mollie_sandbox_secret_key")
		else:
			return self.get_password(fieldname="secret_key", raise_exception=False)

	def get_active_profile_id(self):
		"""Get the appropriate profile ID based on sandbox mode.

		Priority for sandbox mode:
		1. sandbox_profile_id from DocType
		2. sandbox_profile_id from site_config.json

		Priority for live mode:
		1. profile_id from DocType
		"""
		if self.is_sandbox_mode():
			# Try DocType field first, then fall back to site config
			if self.sandbox_profile_id:
				return self.sandbox_profile_id
			# Fallback to site_config.json
			return frappe.conf.get("sandbox_profile_id") or frappe.conf.get("mollie_sandbox_profile_id")
		else:
			return self.profile_id

	def get_payment_url(self, **kwargs):
		return get_url(f"mollie_checkout?{urlencode(kwargs)}")

	def get_mollie_client(self):
		"""Create and return a new Mollie client instance with the appropriate API key."""
		client = Client()
		api_key = self.get_api_key()
		if not api_key:
			mode = "sandbox" if self.is_sandbox_mode() else "live"
			frappe.throw(_("Mollie {0} API key is not configured").format(mode))
		client.set_api_key(api_key)
		return client

	def create_request(self, data):
		self.data = frappe._dict(data)
		self.mollie_client = self.get_mollie_client()

		try:
			self.integration_request = create_request_log(self.data, service_name="Mollie")
			return self.create_charge_on_mollie()

		except MollieError as e:
			log_payment_error("Mollie", e, {"method": "create_request", "data": self.data})
			return {
				"redirect_to": frappe.redirect_to_message(
					_("Payment Error"),
					_("Mollie payment failed: {0}").format(str(e)),
				),
				"status": 400,
			}
		except Exception as e:
			log_payment_error("Mollie", e, {"method": "create_request", "data": self.data})
			return {
				"redirect_to": frappe.redirect_to_message(
					_("Server Error"),
					_(
						"It seems that there is an issue with the server's Mollie configuration. In case of failure, the amount will get refunded to your account."
					),
				),
				"status": 401,
			}

	def check_request(self, data, paymentID):
		mollie_client = self.get_mollie_client()
		try:
			payment = mollie_client.payments.get(paymentID)
			paymentUrl = "Unavailable"

			if payment.is_paid():
				status = "Completed"
			elif payment.is_pending():
				status = "Pending"
				if "checkout" in payment["_links"]:
					paymentUrl = payment["_links"]["checkout"]["href"]
				else:
					status = "Cancelled"
			elif payment.is_open():
				status = "Open"
				if "checkout" in payment["_links"]:
					paymentUrl = payment["_links"]["checkout"]["href"]
				else:
					status = "Cancelled"
			else:
				status = "Cancelled"

			return {"paymentUrl": paymentUrl, "status": status}

		except MollieError as e:
			log_payment_error("Mollie", e, {"method": "check_request", "payment_id": paymentID})
			return {"paymentUrl": "Unavailable", "status": "Error", "error": str(e)}
		except Exception as e:
			log_payment_error("Mollie", e, {"method": "check_request", "payment_id": paymentID})
			return {"paymentUrl": "Unavailable", "status": "Error", "error": str(e)}

	def create_charge_on_mollie(self):
		data_details = {
			"amount": self.data.amount,
			"title": f"Payment for {self.data.reference_doctype} {self.data.reference_docname}",
			"description": f"Payment for {self.data.reference_doctype} {self.data.reference_docname}",
			"reference_doctype": self.data.reference_doctype,
			"reference_docname": self.data.reference_docname,
			"payer_email": frappe.session.user,
			"payer_name": frappe.utils.get_fullname(frappe.session.user),
			"order_id": self.data.reference_docname,
			"currency": self.data.currency,
			"redirect_to": self.data.get("redirect_to"),
		}
		redirect_url = self.get_payment_url(**data_details)
		email = frappe.db.get_value(
			self.data.reference_doctype, self.data.reference_docname, "email"
		)
		if email:
			self.data.payer_email = email

		charge_data = {
			"amount": {
				"currency": self.data.currency,
				"value": "{:.2f}".format(float(self.data.amount)),
			},
			"description": self.data.description,
			"redirectUrl": redirect_url,
		}

		if (
			self.data.payer_email
			and self.data.payer_email != "Guest"
			and frappe.utils.validate_email_address(self.data.payer_email)
		):
			charge_data["billingAddress"] = {"email": self.data.payer_email}

		try:
			charge = self.mollie_client.payments.create(charge_data)
			frappe.db.set_value(
				self.data.reference_doctype, self.data.reference_docname, "payment_id", charge.id
			)
		except MollieError as e:
			log_payment_error(
				"Mollie", e, {"method": "create_charge_on_mollie", "charge_data": charge_data}
			)
			raise
		except Exception as e:
			log_payment_error(
				"Mollie", e, {"method": "create_charge_on_mollie", "charge_data": charge_data}
			)
			raise

		data2 = self.finalize_request()
		data2.update(paymentID=charge.id)
		data2.update(paymentUrl=charge.checkout_url)

		return data2

	def finalize_request(self):
		redirect_to = self.data.get("redirect_to") or None
		redirect_message = self.data.get("redirect_message") or None
		status = self.integration_request.status

		if self.flags.status_changed_to == "Completed":
			if self.data.reference_doctype and self.data.reference_docname:
				custom_redirect_to = None
				try:
					custom_redirect_to = frappe.get_doc(
						self.data.reference_doctype, self.data.reference_docname
					).run_method("on_payment_authorized", self.flags.status_changed_to)
				except Exception as e:
					log_payment_error(
						"Mollie",
						e,
						{
							"method": "finalize_request.on_payment_authorized",
							"reference_doctype": self.data.reference_doctype,
							"reference_docname": self.data.reference_docname,
						},
					)

				if custom_redirect_to:
					redirect_to = custom_redirect_to

				redirect_url = "payment-success?doctype={}&docname={}".format(
					self.data.reference_doctype, self.data.reference_docname
				)

			if self.redirect_url:
				redirect_url = self.redirect_url
				redirect_to = None
		else:
			redirect_url = "payment-failed"

		if redirect_to and "?" in redirect_url:
			redirect_url += "&" + urlencode({"redirect_to": redirect_to})
		else:
			redirect_url += "?" + urlencode({"redirect_to": redirect_to})

		if redirect_message:
			redirect_url += "&" + urlencode({"redirect_message": redirect_message})

		return {"redirect_to": redirect_url, "status": status}


def get_gateway_controller(doctype, docname):
	reference_doc = frappe.get_doc(doctype, docname)
	gateway_controller = frappe.db.get_value(
		"Payment Gateway", reference_doc.payment_gateway, "gateway_controller"
	)
	return gateway_controller
