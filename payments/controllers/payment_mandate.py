from frappe.model.document import Document


class PaymentMandate(Document):
	"""Base class for gateway-specific reusable payment mandates.

	A mandate represents a stored authorization to charge a payer off-session
	(e.g. a saved card / SEPA mandate at a PSP). Concrete subclasses carry the
	gateway-specific identifiers (customer id, payment-method id, mandate
	reference) and implement the contract below.
	"""

	def is_usable(self) -> bool:
		"""Whether this mandate can currently be charged off-session."""
		raise NotImplementedError

	def revoke(self) -> None:
		"""Revoke the mandate at the gateway and mark it revoked locally."""
		raise NotImplementedError
