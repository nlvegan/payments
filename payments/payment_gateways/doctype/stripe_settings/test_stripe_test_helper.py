import unittest
from unittest.mock import patch

import frappe

from payments.payment_gateways.doctype.stripe_settings.stripe_test_helper import (
	ensure_stripe_test_credentials,
)


class TestEnsureStripeTestCredentials(unittest.TestCase):
	"""The credential-present path is covered live in test_stripe_settings_live.

	These cover only the deterministic skip-guard: no real Stripe needed.
	"""

	def test_returns_none_when_keys_absent(self):
		with patch.object(frappe, "conf", frappe._dict()):
			self.assertIsNone(ensure_stripe_test_credentials())

	def test_returns_none_when_secret_prefix_wrong(self):
		conf = frappe._dict(
			stripe_test_secret_key="sk_live_nope",
			stripe_test_publishable_key="pk_test_ok",
		)
		with patch.object(frappe, "conf", conf):
			self.assertIsNone(ensure_stripe_test_credentials())

	def test_returns_none_when_publishable_prefix_wrong(self):
		conf = frappe._dict(
			stripe_test_secret_key="sk_test_ok",
			stripe_test_publishable_key="pk_live_nope",
		)
		with patch.object(frappe, "conf", conf):
			self.assertIsNone(ensure_stripe_test_credentials())
