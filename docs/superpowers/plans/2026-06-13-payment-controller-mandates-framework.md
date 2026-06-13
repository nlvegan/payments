# PaymentController Recurring/Mandate Framework — Implementation Plan (Phases A + B)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Each task follows superpowers:test-driven-development (red → green → refactor). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generalize PaymentController v2 beyond the hard-coded `charge` flow and add PSP-mediated recurring payments via reusable mandates (first charge saves a mandate; renewals charged off-session by the backend), proven by a self-contained reference gateway. DRY/KISS-clean the existing controller along the way.

**Architecture:** Two logical phases, stacked as commits on `feat/payment-controller-mandates`. **Phase A** establishes a self-contained reference/demo gateway as the framework's test vehicle (the existing lifecycle tests currently depend on the off-branch Stripe v2 controller), then applies the behavior-preserving DRY/KISS cleanup against that green safety net. **Phase B** adds the `mandated_charge` flow type, a `PaymentMandate` base, a flow-type dispatch table replacing the `"charge"` hard-coding, and a server-side `charge_mandate()` entry point. Stripe (Phase C) is a separate later plan.

**Tech Stack:** Frappe Framework (Python 3.10+, tabs for indentation, `_()` for i18n), `frappe.tests.IntegrationTestCase` + stdlib `unittest`, dataclasses in `payments/types.py`.

**Spec:** `docs/superpowers/specs/2026-06-13-payment-controller-recurring-mandates-design.md`

**Naming contract (use these exact identifiers across all tasks):**
- DocType: `Payment Demo Settings` (Single) → controller class `PaymentDemoSettings(PaymentController)`, module `Payment Gateways`.
- New `SessionType` member: `mandated_charge = "mandated_charge"`.
- `TxData` fields: `mandate: str | None`, `save_mandate: bool`.
- Base class: `PaymentMandate(Document)` in `payments/controllers/payment_mandate.py`.
- PSL helpers: `set_mandate(mandate)`, `get_mandate()`.
- Controller contracts: `_initiate_mandated_charge()`, `_process_response_for_mandated_charge()`.
- Controller entry point: `PaymentController.charge_mandate(mandate, tx_data, gateway=None)`.
- Extracted helpers: `_redirect_on_initiation_error(psl, error, extra=None)` (module-level), `make_error_processed(error, message)` (local closure in `process_response`), `_build_compensatory_action(self, psl, error_log)` (method).
- Dataclass: `GatewayRef(gateway_settings, gateway_controller)` in `payments/types.py` with `to_json()` / `from_json()`.
- Dispatch map: `PaymentController._FLOW_DISPATCH`.

**Conventions:**
- All `bench` commands run from `~/frappe-bench`. Site: `veg11.veganisme.org`.
- Run a single test module: `bench --site veg11.veganisme.org run-tests --app payments --module payments.controllers.test_payment_controller`
- After any DocType `.json` add/change: `bench --site veg11.veganisme.org migrate` then `bench --site veg11.veganisme.org clear-cache`.
- Indent with **tabs** (ruff config: tab indent, line length 110). Run `ruff format .` and `ruff check .` before each commit.

---

## PHASE A — Reference gateway + DRY/KISS cleanup

### Task A1: Create the reference/demo gateway DocType (test vehicle)

A minimal, SDK-free `PaymentController` subclass so the framework is testable on this branch without Stripe. It is deterministic: charge status is read from the response payload.

**Files:**
- Create: `payments/payment_gateways/doctype/payment_demo_settings/__init__.py` (empty)
- Create: `payments/payment_gateways/doctype/payment_demo_settings/payment_demo_settings.json`
- Create: `payments/payment_gateways/doctype/payment_demo_settings/payment_demo_settings.py`

- [ ] **Step 1: Create the empty package init**

```python
# payments/payment_gateways/doctype/payment_demo_settings/__init__.py
```
(empty file)

- [ ] **Step 2: Create the DocType JSON (Single, minimal)**

`payments/payment_gateways/doctype/payment_demo_settings/payment_demo_settings.json`:
```json
{
 "actions": [],
 "creation": "2026-06-13 00:00:00.000000",
 "doctype": "DocType",
 "engine": "InnoDB",
 "field_order": ["gateway_name"],
 "fields": [
  {
   "fieldname": "gateway_name",
   "fieldtype": "Data",
   "label": "Gateway Name",
   "unique": 1
  }
 ],
 "issingle": 1,
 "links": [],
 "modified": "2026-06-13 00:00:00.000000",
 "module": "Payment Gateways",
 "name": "Payment Demo Settings",
 "owner": "Administrator",
 "permissions": [
  {
   "create": 1,
   "delete": 1,
   "email": 1,
   "print": 1,
   "read": 1,
   "role": "System Manager",
   "share": 1,
   "write": 1
  }
 ],
 "sort_field": "modified",
 "sort_order": "DESC",
 "track_changes": 1
}
```

- [ ] **Step 3: Create the controller implementing the v2 contracts**

`payments/payment_gateways/doctype/payment_demo_settings/payment_demo_settings.py`:
```python
# Copyright (c) 2026, Frappe and contributors
# For license information, please see LICENSE
"""A minimal, dependency-free PaymentController used as a reference
implementation and as the test vehicle for the framework. It performs no
network I/O: the charge outcome is taken from the response payload's
``status`` field, mapped through ``flowstates``.
"""

import frappe

from payments.controllers import PaymentController
from payments.types import FrontendDefaults, Initiated, RemoteServerInitiationPayload, SessionStates


class PaymentDemoSettings(PaymentController):
	flowstates = SessionStates(
		success=["succeeded"],
		pre_authorized=["authorized"],
		processing=["pending"],
		declined=["failed"],
	)
	frontend_defaults = FrontendDefaults(
		gateway_css="",
		gateway_js="",
		gateway_wrapper="<div id='demo-gateway'></div>",
	)

	# -- contracts --

	def validate_tx_data(self, tx_data) -> None:
		if tx_data.amount is None or tx_data.amount <= 0:
			frappe.throw(frappe._("Amount must be positive"))

	def _initiate_charge(self) -> Initiated:
		psl = self.state.psl
		return Initiated(
			correlation_id=f"demo-{psl.name}",
			payload=RemoteServerInitiationPayload({"demo": True, "psl": psl.name}),
		)

	def _validate_response(self) -> None:
		return None

	def _process_response_for_charge(self):
		payload = self.state.response.payload
		self.flags.status_changed_to = payload.get("status", "succeeded")
		return None

	def _render_failure_message(self) -> str:
		return self.state.response.payload.get("decline_reason", "Demo payment failed")

	def _is_server_to_server(self) -> bool:
		return bool(self.state.response.payload.get("s2s"))
```

- [ ] **Step 4: Migrate and clear cache so the doctype loads**

Run:
```bash
cd ~/frappe-bench && bench --site veg11.veganisme.org migrate && bench --site veg11.veganisme.org clear-cache
```
Expected: migrate completes; `Payment Demo Settings` table/single created.

- [ ] **Step 5: Lint**

Run: `cd ~/frappe-bench/apps/payments && ruff format . && ruff check .`
Expected: no errors on the new files.

- [ ] **Step 6: Commit**

```bash
cd ~/frappe-bench/apps/payments
git add payments/payment_gateways/doctype/payment_demo_settings/
git commit -m "test(payments): add SDK-free reference/demo PaymentController gateway"
```

---

### Task A2: Retarget the lifecycle tests to the reference gateway

Make `TestPaymentControllerLifecycle` self-contained (no Stripe), establishing the green safety net for the Phase A refactors. Replace the Stripe SDK mocking with the deterministic demo gateway.

**Files:**
- Modify: `payments/controllers/test_payment_controller.py` (rewrite `TestPaymentControllerLifecycle` setup + the lifecycle tests)

- [ ] **Step 1: Replace setUpClass and helpers to use the demo gateway**

Replace the `STRIPE_MOCK_PATH` constant and `TestPaymentControllerLifecycle.setUpClass` / `_mock_intent` (lines 82–131) with a demo-based fixture:
```python
class TestPaymentControllerLifecycle(IntegrationTestCase):
	"""Integration tests for the PaymentController lifecycle.

	Uses the SDK-free Payment Demo Settings gateway. Tests the orchestration
	layer: initiate -> proceed -> process_response.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("Payment Demo Settings", "Payment Demo Settings"):
			demo = frappe.get_doc({"doctype": "Payment Demo Settings", "gateway_name": "Demo"})
			demo.flags.ignore_mandatory = True
			demo.insert(ignore_permissions=True)

		gateway_name = "Demo-Gateway"
		if not frappe.db.exists("Payment Gateway", gateway_name):
			frappe.get_doc(
				{
					"doctype": "Payment Gateway",
					"gateway": gateway_name,
					"gateway_settings": "Payment Demo Settings",
					"gateway_controller": "Payment Demo Settings",
				}
			).insert(ignore_permissions=True)

		cls.gateway_name = gateway_name
		frappe.db.commit()
```

- [ ] **Step 2: Rewrite the lifecycle tests to drive the demo gateway**

Replace the `@patch(STRIPE_MOCK_PATH)` tests (lines 133–306) with payload-driven equivalents. Full replacement:
```python
	# -- initiate --

	def test_initiate_creates_psl(self):
		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)
		psl = frappe.get_doc("Payment Session Log", psl_name)
		self.assertEqual(psl.status, "Created")
		stored = json.loads(psl.tx_data)
		self.assertEqual(stored["amount"], 25.00)

	def test_initiate_returns_controller_instance(self):
		tx_data = _make_tx_data()
		controller, _psl_name = PaymentController.initiate(tx_data, self.gateway_name)
		self.assertIsInstance(controller, PaymentController)

	# -- proceed --

	def test_proceed_initiates_charge(self):
		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)
		proceeded = PaymentController.proceed(psl_name)
		self.assertEqual(proceeded.integration, "Payment Demo Settings")
		self.assertTrue(proceeded.payload["demo"])
		psl = frappe.get_doc("Payment Session Log", psl_name)
		self.assertEqual(psl.status, "Initiated")
		self.assertEqual(psl.correlation_id, f"demo-{psl_name}")

	def test_proceed_is_idempotent(self):
		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)
		first = PaymentController.proceed(psl_name)
		second = PaymentController.proceed(psl_name)
		self.assertEqual(first.payload, second.payload)

	# -- process_response: success --

	def test_process_response_success(self):
		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)
		PaymentController.proceed(psl_name)
		response = GatewayProcessingResponse(
			hash=None, message=None, payload={"status": "succeeded"}
		)
		result = PaymentController.process_response(psl_name, response)
		self.assertEqual(result.indicator_color, "green")
		self.assertEqual(frappe.get_doc("Payment Session Log", psl_name).status, "Paid")

	# -- process_response: declined --

	def test_process_response_declined(self):
		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)
		PaymentController.proceed(psl_name)
		response = GatewayProcessingResponse(
			hash=None, message=None,
			payload={"status": "failed", "decline_reason": "Demo declined"},
		)
		result = PaymentController.process_response(psl_name, response)
		self.assertEqual(result.indicator_color, "red")
		self.assertEqual(frappe.get_doc("Payment Session Log", psl_name).status, "Declined")

	# -- process_response: ref doc hook error --

	def test_process_response_ref_doc_hook_error(self):
		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)
		PaymentController.proceed(psl_name)

		def exploding_hook(*args, **kwargs):
			raise ValueError("hook exploded")

		response = GatewayProcessingResponse(
			hash=None, message=None, payload={"status": "succeeded"}
		)
		ref_doc_class = frappe.get_doc("User", "Administrator").__class__
		with patch.object(
			ref_doc_class, "on_payment_charge_processed", create=True, new=exploding_hook
		):
			PaymentController.process_response(psl_name, response)
		self.assertEqual(
			frappe.get_doc("Payment Session Log", psl_name).status, "Error - RefDoc"
		)

	# -- pre_data_capture_hook --

	def test_pre_data_capture_hook_stores_state(self):
		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)
		data = PaymentController.pre_data_capture_hook(psl_name)
		self.assertIsInstance(data, dict)
		self.assertEqual(frappe.get_doc("Payment Session Log", psl_name).status, "Data Capture")
```
Also remove the now-unused `MagicMock` import if no longer referenced (keep `patch`).

- [ ] **Step 3: Run the full module; expect green**

Run: `cd ~/frappe-bench && bench --site veg11.veganisme.org run-tests --app payments --module payments.controllers.test_payment_controller`
Expected: PASS (unit classes + the retargeted lifecycle class). This is the safety net for Phase A.

- [ ] **Step 4: Commit**

```bash
cd ~/frappe-bench/apps/payments
git add payments/controllers/test_payment_controller.py
git commit -m "test(payments): retarget lifecycle tests to demo gateway (Stripe-independent)"
```

---

### Task A3: Extract `_error_processed` helper (audit #2)

Collapse the three near-identical `Processed(...)` error returns in `process_response()` (controller lines ~512–545).

**Files:**
- Modify: `payments/controllers/payment_controller.py`

- [ ] **Step 1: Confirm the safety-net test covers the error path**

The existing `test_process_response_ref_doc_hook_error` exercises one error branch. Add a characterization test pinning the shape of a processing-error return. In `test_payment_controller.py`, add to `TestPaymentControllerLifecycle`:
```python
	def test_process_response_processing_error_shape(self):
		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)
		PaymentController.proceed(psl_name)
		response = GatewayProcessingResponse(hash=None, message=None, payload={"status": "succeeded"})
		# Force a processing error by making the charge processor raise
		with patch.object(
			__import__(
				"payments.payment_gateways.doctype.payment_demo_settings.payment_demo_settings",
				fromlist=["PaymentDemoSettings"],
			).PaymentDemoSettings,
			"_process_response_for_charge",
			side_effect=ValueError("boom"),
		):
			result = PaymentController.process_response(psl_name, response)
		self.assertEqual(result.indicator_color, "red")
		self.assertEqual(result.status_changed_to, frappe._("Server Error"))
		self.assertEqual(result.payload, {})
```

- [ ] **Step 2: Run it (green before refactor)**

Run: `bench --site veg11.veganisme.org run-tests --app payments --module payments.controllers.test_payment_controller`
Expected: PASS (pins current behavior).

- [ ] **Step 3: Add the helper and use it**

In `process_response`, just below `get_compensatory_action` (before the `try:` at ~line 500), add a local helper and replace the three `Processed(...)` returns in the `except` blocks (lines ~515–545):
```python
		def make_error_processed(error, message):
			return Processed(
				message=message,
				action=get_compensatory_action(error),
				status_changed_to=_("Server Error"),
				indicator_color="red",
				payload={},
			)
```
Then the three branches become:
```python
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
```
(Note: the `PayloadIntegrityError` branch previously used `status_changed_to=_("Server Error")` too — confirm the message wording matches the original; the helper preserves color/status/payload exactly.)

- [ ] **Step 4: Run tests (green after refactor)**

Run: `bench --site veg11.veganisme.org run-tests --app payments --module payments.controllers.test_payment_controller`
Expected: PASS, unchanged behavior.

- [ ] **Step 5: Lint + commit**

```bash
cd ~/frappe-bench/apps/payments && ruff format . && ruff check .
git add payments/controllers/payment_controller.py payments/controllers/test_payment_controller.py
git commit -m "refactor(payments): extract make_error_processed helper in process_response (DRY)"
```

---

### Task A4: Extract `_redirect_on_initiation_error` helper (audit #1)

Collapse the three near-identical redirect blocks in `proceed()` (controller lines ~257–289).

**Files:**
- Modify: `payments/controllers/payment_controller.py`

- [ ] **Step 1: Add a characterization test for the redirect path**

`proceed()` raises `frappe.Redirect` on initiation failure. Add to `TestPaymentControllerLifecycle`:
```python
	def test_proceed_redirects_on_initiation_failure(self):
		from payments.exceptions import FailedToInitiateFlowError

		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)
		demo_cls = __import__(
			"payments.payment_gateways.doctype.payment_demo_settings.payment_demo_settings",
			fromlist=["PaymentDemoSettings"],
		).PaymentDemoSettings
		with patch.object(
			demo_cls, "_initiate_charge",
			side_effect=FailedToInitiateFlowError("nope", {"err": 1}),
		):
			with self.assertRaises(frappe.Redirect):
				PaymentController.proceed(psl_name)
		self.assertEqual(frappe.get_doc("Payment Session Log", psl_name).status, "Error")
```

- [ ] **Step 2: Run it (green before refactor)**

Run: `bench --site veg11.veganisme.org run-tests --app payments --module payments.controllers.test_payment_controller`
Expected: PASS.

- [ ] **Step 3: Add the helper (module-level) and use it**

Add near `_error_value` (top of `payment_controller.py`, ~line 47):
```python
def _redirect_on_initiation_error(psl, error, extra=None):
	if extra is not None:
		body = _("Please contact customer care mentioning: {0} and {1}").format(psl, error)
	else:
		body = _("Please contact customer care mentioning: {0}").format(error)
	frappe.redirect_to_message(
		_("Payment Gateway Error"),
		body,
		http_status_code=401,
		indicator_color="yellow",
	)
	raise frappe.Redirect
```
Replace the three `except` blocks in `proceed()` (lines ~257–289):
```python
		except FailedToInitiateFlowError as err:
			psl.set_initiation_payload(err.data, "Error")
			error = psl.log_error(title=err.message)
			_redirect_on_initiation_error(psl, error, extra=True)

		except HTTPError:
			data = frappe.flags.integration_request.json()
			psl.set_initiation_payload(data, "Error")
			error = frappe.get_last_doc("Error Log")
			_redirect_on_initiation_error(psl, error, extra=True)

		except Exception:
			error = psl.log_error(title="Unknown Initialization Failure")
			_redirect_on_initiation_error(psl, error)
```
(The first two preserve the original two-arg message `{0} and {1}` = `psl` and `error`; the last preserves the one-arg form. Behavior identical.)

- [ ] **Step 4: Run tests (green after)**

Run: `bench --site veg11.veganisme.org run-tests --app payments --module payments.controllers.test_payment_controller`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
cd ~/frappe-bench/apps/payments && ruff format . && ruff check .
git add payments/controllers/payment_controller.py payments/controllers/test_payment_controller.py
git commit -m "refactor(payments): extract _redirect_on_initiation_error helper in proceed (DRY)"
```

---

### Task A5: Promote `get_compensatory_action` closure to a method (audit #5)

**Files:**
- Modify: `payments/controllers/payment_controller.py`

- [ ] **Step 1: Add the method**

Add a private method on `PaymentController` (near `_build_support_action`, ~line 302):
```python
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
```

- [ ] **Step 2: Replace the closure in `process_response`**

Delete the nested `def get_compensatory_action(error_log):` (lines ~485–498). In the helper from Task A3, change `action=get_compensatory_action(error)` to `action=self._build_compensatory_action(psl, error)`.

- [ ] **Step 3: Run tests (green)**

Run: `bench --site veg11.veganisme.org run-tests --app payments --module payments.controllers.test_payment_controller`
Expected: PASS.

- [ ] **Step 4: Lint + commit**

```bash
cd ~/frappe-bench/apps/payments && ruff format . && ruff check .
git add payments/controllers/payment_controller.py
git commit -m "refactor(payments): promote compensatory-action closure to method (DRY)"
```

---

### Task A6: Introduce `GatewayRef` dataclass (audit #3)

Replace the four hand-rolled `{gateway_settings, gateway_controller}` JSON encode/decode sites.

**Files:**
- Modify: `payments/types.py`
- Modify: `payments/payments/doctype/payment_session_log/payment_session_log.py`

- [ ] **Step 1: Write a unit test for the dataclass**

Add to `test_payment_controller.py` (unit section, no DB):
```python
class TestGatewayRef(unittest.TestCase):
	def test_roundtrip(self):
		from payments.types import GatewayRef

		ref = GatewayRef(gateway_settings="Stripe Settings", gateway_controller="acme")
		restored = GatewayRef.from_json(ref.to_json())
		self.assertEqual(restored.gateway_settings, "Stripe Settings")
		self.assertEqual(restored.gateway_controller, "acme")
```

- [ ] **Step 2: Run it — expect failure (not defined)**

Run: `bench --site veg11.veganisme.org run-tests --app payments --module payments.controllers.test_payment_controller`
Expected: FAIL (`ImportError: cannot import name 'GatewayRef'`).

- [ ] **Step 3: Implement `GatewayRef` in `types.py`**

Append to `payments/types.py`:
```python
import json as _json
from dataclasses import asdict


@dataclass
class GatewayRef:
	"""Identifies the concrete gateway settings doctype + controller record.

	Stored as JSON on Payment Session Log (and the gateway filter), kept as a
	dataclass so the field names live in exactly one place.
	"""

	gateway_settings: str
	gateway_controller: str

	def to_json(self) -> str:
		return _json.dumps(asdict(self))

	@staticmethod
	def from_json(s: str) -> "GatewayRef":
		return GatewayRef(**_json.loads(s))
```

- [ ] **Step 4: Run the unit test — expect pass**

Run: `bench --site veg11.veganisme.org run-tests --app payments --module payments.controllers.test_payment_controller`
Expected: `TestGatewayRef` PASS.

- [ ] **Step 5: Use it in `create_log` and `get_controller`**

In `payment_session_log.py`:
- `create_log` (lines ~241–246): replace the `json.dumps({...})` with `GatewayRef(controller.doctype, controller.name).to_json()`.
- `get_controller` (lines ~126–128): replace `d = json.loads(self.gateway); doctype, docname = d["gateway_settings"], d["gateway_controller"]` with:
```python
		ref = GatewayRef.from_json(self.gateway)
		return frappe.get_cached_doc(ref.gateway_settings, ref.gateway_controller)
```
- `select_button` (lines ~215–225): replace the inline `json.dumps({...})` with `GatewayRef(btn.gateway_settings, btn.gateway_controller).to_json()`.
- Add `from payments.types import GatewayRef` to the imports.
- Leave the `select_button` *filter comparison* (lines ~188–213) reading `gateway_filter = json.loads(psl.gateway)` as-is for now (it compares optional keys); it can also use `GatewayRef.from_json` but that is optional polish — keep this task minimal.

- [ ] **Step 6: Run full module — expect green**

Run: `bench --site veg11.veganisme.org run-tests --app payments --module payments.controllers.test_payment_controller`
Expected: PASS (the lifecycle tests exercise `create_log` + `get_controller`).

- [ ] **Step 7: Lint + commit**

```bash
cd ~/frappe-bench/apps/payments && ruff format . && ruff check .
git add payments/types.py payments/payments/doctype/payment_session_log/payment_session_log.py payments/controllers/test_payment_controller.py
git commit -m "refactor(payments): add GatewayRef dataclass for gateway identity (DRY)"
```

---

### Task A7: Settle the `response` param vs `self.state.response` (audit #6) + cheap lows (#8)

**Files:**
- Modify: `payments/controllers/payment_controller.py`

- [ ] **Step 1: Read `response.payload` from `self.state.response`**

In `_process_response` (line ~357), `ret["payload"] = response.payload` — `response` is the param while `self.state.response` is already set in `process_response` (line ~476). Change the signature to drop the redundant param OR read consistently. Minimal change: in `_process_response`, replace `response.payload` usages with `self.state.response.payload`, and at the two `psl.set_processing_payload(response, ...)` call sites keep passing `self.state.response`. Then change the signature `def _process_response(self, psl, ref_doc):` and the caller (line ~501) to `self._process_response(psl, ref_doc)`.

- [ ] **Step 2: Remove the duplicate `from typing import TYPE_CHECKING` inside the class body**

Delete the in-class `from typing import TYPE_CHECKING` (lines ~56–59 in `PaymentController`); the module-level import (line 4) already covers the `if TYPE_CHECKING:` block.

> Audit #10 (twin PSL setters `update_gateway_specific_state` / `set_initiation_payload`) and #9 (`_STATUS_MAP` dict-copy) are **deliberately skipped** here: #10's two callers carry different semantics worth keeping distinct, and #9 is cosmetic. Neither is touched by the mandate generalization; revisit only if Phase C needs it.

- [ ] **Step 3: Run tests (green)**

Run: `bench --site veg11.veganisme.org run-tests --app payments --module payments.controllers.test_payment_controller`
Expected: PASS.

- [ ] **Step 4: Lint + commit**

```bash
cd ~/frappe-bench/apps/payments && ruff format . && ruff check .
git add payments/controllers/payment_controller.py
git commit -m "refactor(payments): use self.state.response consistently; drop dup TYPE_CHECKING import"
```

---

## PHASE B — Mandate framework

### Task B1: Add `mandated_charge` SessionType + `TxData` mandate fields

**Files:**
- Modify: `payments/types.py`

- [ ] **Step 1: Write unit tests**

Add to `test_payment_controller.py` (unit section):
```python
class TestMandateTypes(unittest.TestCase):
	def test_session_type_has_mandated_charge(self):
		from payments.types import SessionType

		self.assertEqual(SessionType.mandated_charge.value, "mandated_charge")

	def test_txdata_mandate_fields_default(self):
		tx = _make_tx_data()
		self.assertIsNone(tx.mandate)
		self.assertFalse(tx.save_mandate)
```

- [ ] **Step 2: Run — expect failure**

Run: `bench --site veg11.veganisme.org run-tests --app payments --module payments.controllers.test_payment_controller`
Expected: FAIL (`AttributeError`/`TypeError`).

- [ ] **Step 3: Implement**

In `payments/types.py`:
- Add to `SessionType`: `mandated_charge = "mandated_charge"` (leave a comment: `# mandate_acquisition (€0 standalone setup) intentionally deferred`).
- In `TxData`, replace the `# TODO: tx data for subscriptions...` line (line 79) with:
```python
	mandate: str | None = None  # reference to a PaymentMandate, used by off-session charges
	save_mandate: bool = False  # signal the first charge to persist a reusable mandate
```
(These have defaults so existing `TxData(**...)` calls and stored JSON without the keys still construct. Confirm `load_state()` in PSL builds `TxData(**json.loads(...))` — extra-safe because old rows lack the keys and the defaults apply.)

- [ ] **Step 4: Run — expect pass**

Run: `bench --site veg11.veganisme.org run-tests --app payments --module payments.controllers.test_payment_controller`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd ~/frappe-bench/apps/payments && ruff format . && ruff check .
git add payments/types.py payments/controllers/test_payment_controller.py
git commit -m "feat(payments): add mandated_charge SessionType and TxData mandate fields"
```

---

### Task B2: `PaymentMandate` base class

**Files:**
- Create: `payments/controllers/payment_mandate.py`
- Modify: `payments/controllers/__init__.py` (export)

- [ ] **Step 1: Write a unit test**

Add to `test_payment_controller.py`:
```python
class TestPaymentMandateBase(unittest.TestCase):
	def test_base_declares_contract(self):
		from payments.controllers.payment_mandate import PaymentMandate

		self.assertTrue(hasattr(PaymentMandate, "is_usable"))
		self.assertTrue(hasattr(PaymentMandate, "revoke"))
```

- [ ] **Step 2: Run — expect failure (ImportError)**

Run: `bench --site veg11.veganisme.org run-tests --app payments --module payments.controllers.test_payment_controller`
Expected: FAIL.

- [ ] **Step 3: Implement the base**

`payments/controllers/payment_mandate.py`:
```python
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
```
Export from `payments/controllers/__init__.py` (which currently re-exports `PaymentController`): add `from payments.controllers.payment_mandate import PaymentMandate`.

- [ ] **Step 4: Run — expect pass**

Run: `bench --site veg11.veganisme.org run-tests --app payments --module payments.controllers.test_payment_controller`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd ~/frappe-bench/apps/payments && ruff format . && ruff check .
git add payments/controllers/payment_mandate.py payments/controllers/__init__.py payments/controllers/test_payment_controller.py
git commit -m "feat(payments): add PaymentMandate base class"
```

---

### Task B3: PSL `set_mandate` / `get_mandate` helpers (un-vestigialize)

**Files:**
- Modify: `payments/payments/doctype/payment_session_log/payment_session_log.py`

- [ ] **Step 1: Write an integration test**

Add to `TestPaymentControllerLifecycle`:
```python
	def test_psl_set_and_get_mandate(self):
		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)
		psl = frappe.get_doc("Payment Session Log", psl_name)
		psl.set_mandate({"doctype": "Payment Demo Settings", "name": "Payment Demo Settings"})
		ref = psl.get_mandate()
		self.assertEqual(ref["doctype"], "Payment Demo Settings")
		self.assertEqual(ref["name"], "Payment Demo Settings")
```

- [ ] **Step 2: Run — expect failure**

Run: `bench --site veg11.veganisme.org run-tests --app payments --module payments.controllers.test_payment_controller`
Expected: FAIL (`AttributeError: set_mandate`).

- [ ] **Step 3: Implement helpers + drop the TODO**

In `payment_session_log.py`: remove the vestigial-`mandate` TODO comment (lines 29–31). Add methods to `PaymentSessionLog`:
```python
	def set_mandate(self, mandate) -> None:
		"""Store a reference to a PaymentMandate as {doctype, name} JSON."""
		if hasattr(mandate, "doctype"):
			ref = {"doctype": mandate.doctype, "name": mandate.name}
		else:
			ref = {"doctype": mandate["doctype"], "name": mandate["name"]}
		self.db_set("mandate", json.dumps(ref))

	def get_mandate(self) -> dict | None:
		"""Return the stored mandate ref as a dict, or None."""
		if not self.mandate:
			return None
		return json.loads(self.mandate)
```

- [ ] **Step 4: Run — expect pass**

Run: `bench --site veg11.veganisme.org run-tests --app payments --module payments.controllers.test_payment_controller`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd ~/frappe-bench/apps/payments && ruff format . && ruff check .
git add payments/payments/doctype/payment_session_log/payment_session_log.py payments/controllers/test_payment_controller.py
git commit -m "feat(payments): add PSL set_mandate/get_mandate helpers"
```

---

### Task B4: Flow-type dispatch table (generalize the `charge` hard-coding — audit #4)

Replace the bare `"charge"` literals in `_process_response` with a `_FLOW_DISPATCH` table keyed on `psl.flow_type`, defaulting to `charge`. Behavior for the charge flow stays identical.

**Files:**
- Modify: `payments/controllers/payment_controller.py`

- [ ] **Step 1: Confirm safety net (charge path still green)**

Run: `bench --site veg11.veganisme.org run-tests --app payments --module payments.controllers.test_payment_controller`
Expected: PASS (existing success/declined/hook-error tests cover the charge path).

- [ ] **Step 2: Add the dispatch table and use it**

Add a class attribute on `PaymentController` (the methods are resolved by name at call time to stay overridable):
```python
	# flow_type -> (process_method_name, refdoc_hook_name, human_label)
	_FLOW_DISPATCH: ClassVar[dict] = {
		SessionType.charge: ("_process_response_for_charge", "on_payment_charge_processed", "charge"),
		SessionType.mandated_charge: (
			"_process_response_for_mandated_charge",
			"on_payment_mandated_charge_processed",
			"mandated charge",
		),
	}
```
In `_process_response`, near the top, resolve the active flow:
```python
		flow = self.state.psl.flow_type or SessionType.charge
		process_method_name, hookmethod, flow_label = self._FLOW_DISPATCH.get(
			flow, self._FLOW_DISPATCH[SessionType.charge]
		)
```
Then replace:
- line ~336 `processed = self._process_response_for_charge()` → `processed = getattr(self, process_method_name)()`
- line ~339 `f"{self._process_response_for_charge} failed", "charge"` → `f"{process_method_name} failed", flow_label`
- lines ~370, ~403 `"charge".title()` / `_("{} declined").format("charge".title())` → `.format(flow_label.title())`
- line ~409 `hookmethod = "on_payment_charge_processed"` → `hookmethod = hookmethod` (use the resolved variable; delete the literal assignment)
- line ~441 `RefDocHookProcessingError("RefDoc hook processing failed", "charge")` → `RefDocHookProcessingError("RefDoc hook processing failed", flow_label)`

- [ ] **Step 3: Run — expect green (charge unchanged)**

Run: `bench --site veg11.veganisme.org run-tests --app payments --module payments.controllers.test_payment_controller`
Expected: PASS (the demo gateway has `flow_type` unset → defaults to charge).

- [ ] **Step 4: Lint + commit**

```bash
cd ~/frappe-bench/apps/payments && ruff format . && ruff check .
git add payments/controllers/payment_controller.py
git commit -m "refactor(payments): dispatch process_response on flow_type (generalize charge)"
```

---

### Task B5: Mandated-charge contracts + demo gateway support

Add the abstract contracts and implement them in the demo gateway so the dispatch can be exercised.

**Files:**
- Modify: `payments/controllers/payment_controller.py` (add contract stubs)
- Modify: `payments/payment_gateways/doctype/payment_demo_settings/payment_demo_settings.py`

- [ ] **Step 1: Add contract stubs to `PaymentController`**

Near the other concrete-controller contracts (after `_process_response_for_charge`, ~line 619):
```python
	def _initiate_mandated_charge(self) -> Initiated:
		"""Invoked by charge_mandate to initiate an off-session charge against a stored mandate.

		Implementations can read self.state.psl, self.state.tx_data (tx_data.mandate
		holds the PaymentMandate ref). Should confirm synchronously where the gateway
		supports it.
		"""
		raise NotImplementedError

	def _process_response_for_mandated_charge(self) -> "Processed | None":
		"""Process an off-session mandated-charge response. Needs to be idempotent."""
		raise NotImplementedError
```

- [ ] **Step 2: Implement them in the demo gateway (deterministic via mandate sentinel)**

Append to `PaymentDemoSettings`:
```python
	def _initiate_mandated_charge(self) -> Initiated:
		psl = self.state.psl
		mandate_ref = self.state.tx_data.mandate
		# Deterministic simulation: the mandate ref string encodes the outcome.
		# "revoked" -> raise (treated as initiation failure); otherwise succeed/require action.
		if mandate_ref == "revoked":
			from payments.exceptions import FailedToInitiateFlowError

			raise FailedToInitiateFlowError("Mandate not usable", {"mandate": mandate_ref})
		status = "pending" if mandate_ref == "requires_action" else "succeeded"
		return Initiated(
			correlation_id=f"demo-mc-{psl.name}",
			payload=RemoteServerInitiationPayload({"status": status, "s2s": True, "psl": psl.name}),
		)

	def _process_response_for_mandated_charge(self):
		payload = self.state.response.payload
		self.flags.status_changed_to = payload.get("status", "succeeded")
		return None
```

- [ ] **Step 3: Run (no behavior change yet; contracts unused until B6)**

Run: `bench --site veg11.veganisme.org run-tests --app payments --module payments.controllers.test_payment_controller`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
cd ~/frappe-bench/apps/payments && ruff format . && ruff check .
git add payments/controllers/payment_controller.py payments/payment_gateways/doctype/payment_demo_settings/payment_demo_settings.py
git commit -m "feat(payments): add mandated-charge contracts + demo gateway impl"
```

---

### Task B6: `charge_mandate()` server-side entry point

The off-session driver: create a PSL with `flow_type=mandated_charge`, initiate, and process the synchronous result through the existing `process_response`. Handle `requires_action` (→ payment URL) and initiation failure (→ Declined/redirect).

**Files:**
- Modify: `payments/controllers/payment_controller.py`

- [ ] **Step 1: Write tests for the three outcomes**

Add to `TestPaymentControllerLifecycle`:
```python
	def _mandated_tx(self, mandate):
		return _make_tx_data(mandate=mandate, amount=10.0)

	def test_charge_mandate_success(self):
		result = PaymentController.charge_mandate(
			mandate={"doctype": "Payment Demo Settings", "name": "Payment Demo Settings"},
			tx_data=self._mandated_tx("pm_ok"),
			gateway=self.gateway_name,
		)
		self.assertEqual(result.status_changed_to, "succeeded")
		self.assertEqual(result.indicator_color, "green")

	def test_charge_mandate_requires_action_returns_url(self):
		result = PaymentController.charge_mandate(
			mandate={"doctype": "Payment Demo Settings", "name": "Payment Demo Settings"},
			tx_data=self._mandated_tx("requires_action"),
			gateway=self.gateway_name,
		)
		self.assertIn("/pay", result.action["href"])

	def test_charge_mandate_revoked_declined(self):
		result = PaymentController.charge_mandate(
			mandate={"doctype": "Payment Demo Settings", "name": "Payment Demo Settings"},
			tx_data=self._mandated_tx("revoked"),
			gateway=self.gateway_name,
		)
		self.assertEqual(result.indicator_color, "red")
```
> Note: `charge_mandate` takes `gateway` because the demo flow has no Ref Doc preselection; production callers may resolve it from the mandate. Keep the `gateway` parameter optional and documented.

- [ ] **Step 2: Run — expect failure (no such method)**

Run: `bench --site veg11.veganisme.org run-tests --app payments --module payments.controllers.test_payment_controller`
Expected: FAIL (`AttributeError: charge_mandate`).

- [ ] **Step 3: Implement `charge_mandate`**

Add as a `@staticmethod` on `PaymentController` (near `proceed`/`process_response`):
```python
	@staticmethod
	def charge_mandate(mandate, tx_data: TxData, gateway=None) -> Processed:
		"""Charge a stored mandate off-session, server-side (no /pay page).

		Creates a mandated_charge session, initiates the charge (which for
		supporting gateways confirms synchronously), and processes the result
		through the normal pipeline.

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
		psl.db_set("flow_type", SessionType.mandated_charge, commit=True)
		if hasattr(mandate, "name") or isinstance(mandate, dict):
			psl.set_mandate(mandate)

		self.state = psl.load_state()
		self.state.tx_data = self._patch_tx_data(self.state.tx_data)

		try:
			initiated = self._initiate_mandated_charge()
		except FailedToInitiateFlowError as err:
			psl.set_initiation_payload(err.data, "Declined")
			return Processed(
				message=_("The mandate could not be charged. A new authorization may be required."),
				action=dict(href=PaymentController.get_payment_url(psl.name), label=_("Re-authorize")),
				status_changed_to="Declined",
				indicator_color="red",
				payload={},
			)

		psl.db_set({"correlation_id": initiated.correlation_id}, commit=True)
		psl.set_initiation_payload(initiated.payload, "Initiated")

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
```
Ensure imports at top include `SessionType`, `GatewayProcessingResponse`, `FailedToInitiateFlowError`, `Processed` (all already imported in this module — confirm).

- [ ] **Step 4: Run — expect pass (all three outcomes)**

Run: `bench --site veg11.veganisme.org run-tests --app payments --module payments.controllers.test_payment_controller`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
cd ~/frappe-bench/apps/payments && ruff format . && ruff check .
git add payments/controllers/payment_controller.py payments/controllers/test_payment_controller.py
git commit -m "feat(payments): add charge_mandate off-session entry point"
```

---

### Task B7: Idempotency of `charge_mandate` re-entry

Re-charging the same logical mandate operation should not double-charge. The demo gateway's `_initiate_mandated_charge` is deterministic; verify that `process_response` terminal-state guarding holds when a result is processed twice.

**Files:**
- Modify: `payments/controllers/test_payment_controller.py`

- [ ] **Step 1: Write the test**

```python
	def test_charge_mandate_then_reprocess_is_terminal(self):
		# First charge succeeds and reaches Paid (terminal).
		result = PaymentController.charge_mandate(
			mandate={"doctype": "Payment Demo Settings", "name": "Payment Demo Settings"},
			tx_data=self._mandated_tx("pm_ok"),
			gateway=self.gateway_name,
		)
		self.assertEqual(result.indicator_color, "green")
		# Re-processing the same PSL is a no-op terminal return (guarded by is_terminal()).
		# Find the PSL just created for this mandate flow:
		psl_name = frappe.get_all(
			"Payment Session Log",
			filters={"flow_type": "mandated_charge", "status": "Paid"},
			order_by="creation desc",
			limit=1,
		)[0].name
		again = PaymentController.process_response(
			psl_name, GatewayProcessingResponse(hash=None, message=None, payload={"status": "succeeded"})
		)
		self.assertEqual(again.status_changed_to, "Paid")
```

- [ ] **Step 2: Run — expect pass (guarding already exists in `process_response`)**

Run: `bench --site veg11.veganisme.org run-tests --app payments --module payments.controllers.test_payment_controller`
Expected: PASS (the lock + `is_terminal()` check at the top of `process_response` returns the terminal status).

- [ ] **Step 3: Commit**

```bash
cd ~/frappe-bench/apps/payments
git add payments/controllers/test_payment_controller.py
git commit -m "test(payments): assert mandated-charge reprocessing is terminal (idempotency)"
```

---

### Task B8: Full-suite run + spec cross-check

**Files:** none (verification)

- [ ] **Step 1: Run the whole payments app test suite**

Run: `cd ~/frappe-bench && bench --site veg11.veganisme.org run-tests --app payments`
Expected: PASS. If any pre-existing gateway test fails for reasons unrelated to this work, note it but do not silently skip.

- [ ] **Step 2: Lint the whole app**

Run: `cd ~/frappe-bench/apps/payments && ruff format --check . && ruff check .`
Expected: clean.

- [ ] **Step 3: Update the spec status**

Edit `docs/superpowers/specs/2026-06-13-payment-controller-recurring-mandates-design.md`: under PR slicing, mark Phases A and B implemented; note Phase C (Stripe) is the next plan. Commit:
```bash
git add docs/superpowers/specs/2026-06-13-payment-controller-recurring-mandates-design.md
git commit -m "docs: mark mandate framework Phases A+B implemented"
```

---

## Deferred to the Phase C plan (Stripe)

`setup_future_usage` on the first charge; mandate persistence on charge success; `_initiate_mandated_charge` / `_process_response_for_mandated_charge` for Stripe (off-session `confirm=True`); `Stripe Mandate` doctype (subclass of `PaymentMandate`); off-session webhook handling; `_map_intent_status` extraction. Phase C stacks on `feat/stripe-payment-intent` (or that branch is merged into this line first) — resolve at the B→C boundary.

## Out of scope (named)

€0 standalone `mandate_acquisition` flow; GoCardless/Mollie mandate impls; any change to Verenigingen's offline-SDD subsystem; mandate-registry bridging.
