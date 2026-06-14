# Copyright (c) 2026, Frappe and contributors
# For license information, please see LICENSE
"""Stripe-specific reusable payment mandate.

Persists the Stripe customer + payment-method identifiers captured from a first
charge with ``save_mandate``, so renewals can be charged off-session. Subclass
of the framework ``PaymentMandate`` base.
"""

import frappe
from stripe.error import InvalidRequestError

from payments.controllers.payment_mandate import PaymentMandate


class StripeMandate(PaymentMandate):
	def is_usable(self) -> bool:
		"""Whether this mandate can currently be charged off-session."""
		return self.status == "Active" and bool(self.customer_id) and bool(self.payment_method_id)

	def revoke(self) -> None:
		"""Detach the PaymentMethod at Stripe and mark the mandate revoked.

		An already-detached payment method (Stripe InvalidRequestError) is treated
		as success — the goal state is reached. NOTE: db_set does not auto-commit;
		in a background-job context the caller must commit.
		"""
		import stripe

		if self.payment_method_id:
			controller = frappe.get_cached_doc(self.gateway_settings, self.gateway_controller)
			try:
				stripe.PaymentMethod.detach(self.payment_method_id, api_key=controller.get_stripe_api_key())
			except InvalidRequestError:
				# Already detached at Stripe — safe to mark revoked locally.
				pass
		self.db_set("status", "Revoked")
