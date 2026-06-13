# PaymentController v2 — Recurring / Mandated Payments Extension

**Date:** 2026-06-13
**Status:** Design approved (pending written-spec review)
**Author:** foppe (with Claude)

## Summary

Extend the PaymentController v2 architecture to support **PSP-mediated recurring
payments** via reusable mandates: a first interactive charge saves a mandate,
and subsequent renewals are charged off-session by the backend. **Stripe** is
the reference implementation (SetupIntent-style `setup_future_usage` on the
first charge, then off-session `PaymentIntent` confirmation for renewals).

The work is delivered as **two stacked PRs** to match the maintainers' request
to split logistical/framework work from gateway implementations.

## Background & motivation

The v2 PaymentController (commit `cdf1bbc`, branch
`refactor/payment-controller-architecture`) orchestrates a one-shot payment
lifecycle (`initiate → proceed → process_response`) but only supports the
`charge` flow. Everything in `_process_response` is hard-coded to `"charge"`
(`types.py:79` carries a `# TODO` for subscriptions/mandates).

A previous mandate system existed and was removed in `a7d2b83`
("remove speculative mandate system") because it was **speculative with zero
concrete implementations** — it added 3 flow types and 6 abstract methods with
no gateway using them. That code is preserved on
`feature/mandate-system-scaffolding` for reference. The lesson encoded in this
design: **ship the abstraction together with a real consumer**, and keep the
new surface minimal.

### Scope decisions (settled during brainstorming)

1. **PSP-mediated only; offline SEPA Direct Debit (pain.008 XML) stays out.**
   Verenigingen already owns a mature offline-SDD subsystem (`sepa_mandate`,
   `direct_debit_batch`, pain.008 generation, pain.002 ingestion, return-file
   processing, retries). Modelling offline SDD as a PaymentController "gateway"
   would duplicate that subsystem and misfit the interactive, API-mediated,
   signed-response lifecycle (offline SDD has no API and settles asynchronously
   via batch). No mandate-registry bridge either.
2. **Reference gateway: Stripe.** Its v2 charge impl already exists on
   `feat/stripe-payment-intent` (commit `6ed9225`), so the off-session charge
   half is largely in place; the mandate half (`setup_future_usage` +
   off-session confirm) is a small extension.
3. **Primary acquisition path: first charge saves the mandate.** The initial
   payment is interactive (`/pay`) and saves the payment method for reuse;
   renewals are off-session. A standalone €0 `mandate_acquisition` flow is
   *intentionally deferred* until a consumer needs it.
4. **Approach: mandate as a side-effect of the first charge.** One new
   `mandated_charge` flow type + a `PaymentMandate` base, rather than
   resurrecting the full removed scaffolding. Minimal surface, defensible in
   review, generalizes the `"charge"` hard-coding as a byproduct.

## PR slicing

**Current decision (2026-06-13):** PR1 is not under active review, so we
**simplify the existing v2 code in place** (the DRY/KISS cleanup is not a
separate PR) and **stack all work as commits on `feat/payment-controller-mandates`**
for now. The eventual split into reviewable PRs is **deferred pending the
maintainers' answer** on how they want it chunked. The phases below are still
authored so they *can* be split later along these lines without rework.

Logical phases (each a coherent commit series on this branch):

| Phase | Contents | Eventual PR home |
|-------|----------|------------------|
| **A — DRY/KISS cleanup** | Simplify the existing v2 controller in place (audit findings #1, #2, #3, #5, #6, cheap lows). Behavior-preserving. | folds into PR1, or its own logistical PR if maintainers prefer |
| **B — mandate framework** | Flow-type generalization, `mandated_charge` SessionType, `PaymentMandate` base, off-session `charge_mandate` entry point, un-vestigialize PSL `mandate` field. | logistical PR |
| **C — Stripe mandate impl** | `setup_future_usage` on first charge, mandate persistence, off-session `mandated_charge`, `Stripe Mandate` doctype, webhooks. | gateway PR |

Phase C extends the Stripe v2 controller, which currently lives on
`feat/stripe-payment-intent` (not on this branch). Resolve its base at the
Phase B→C boundary: either rebase Phase C onto `feat/stripe-payment-intent`, or
merge that branch into this line first. Phases A and B do not depend on it.

## Detailed design

### Data model

**`payments/types.py`:**
- `SessionType`: add `mandated_charge = "mandated_charge"`. Comment that
  `mandate_acquisition` (€0 standalone setup) is intentionally deferred.
- `TxData`: replace the stale `# TODO` line with:
  - `mandate: str | None` — reference to a `PaymentMandate` (used by
    off-session charges to select the stored payment method).
  - `save_mandate: bool = False` — signals the first charge to persist a
    mandate. Set by the Ref Doc at `initiate()`; **not** guest-updatable (it is
    not added to `UPDATABLE_TX_DATA_FIELDS`).

**`PaymentMandate` base — `payments/controllers/payment_mandate.py`:**
A thin `Document` base that concrete mandate doctypes subclass. Minimal
contract:
- `is_usable() -> bool` — whether the mandate can be charged now.
- `revoke() -> None` — revoke at the PSP and mark locally revoked.

Identity fields (customer id, payment-method id, mandate reference) are
gateway-specific and live on the subclass; the base stays abstract. Mirrors how
`PaymentController` relates to concrete gateway settings.

**Payment Session Log:**
- Drop the "vestigial `mandate` field" TODO (lines 29–31); the field becomes
  real.
- Add `set_mandate(mandate)` / `get_mandate()` helpers (store as a
  `{doctype, name}` ref, consistent with how `gateway` is stored).
- `flow_type` already exists; no schema change beyond un-vestigializing.

### Control flow (`payments/controllers/payment_controller.py`)

**Generalize the charge-hard-coded dispatch** (this *is* audit finding #4).
Replace the five bare `"charge"` literals in `_process_response` with a table
keyed on `psl.flow_type`:

```python
_FLOW_DISPATCH = {
    SessionType.charge:          (_process_response_for_charge,          "on_payment_charge_processed",          "charge"),
    SessionType.mandated_charge: (_process_response_for_mandated_charge, "on_payment_mandated_charge_processed", "mandated charge"),
}
```

The status-mapping / `_STATUS_MAP` / declined machinery is already
flow-agnostic and is reused unchanged.

**New contracts** (invoked only when a controller opts into mandates — not
speculative dead surface):
- `_initiate_mandated_charge() -> Initiated`
- `_process_response_for_mandated_charge() -> Processed | None`

**Off-session backend entry point** — the one genuinely new control-flow piece.
Renewals have no user at `/pay`, so a static method drives the lifecycle
server-side:

```python
PaymentController.charge_mandate(mandate, tx_data) -> Processed
```

`mandate` is the `PaymentMandate` document (or its `{doctype, name}` ref); the
method writes it onto `tx_data.mandate` before creating the session (the
`tx_data.mandate` field is the serialized form the gateway reads back via
`self.state.tx_data`). It creates a PSL with `flow_type=mandated_charge`, calls
`_initiate_mandated_charge()` (which for Stripe confirms synchronously via
`off_session=True, confirm=True`), then feeds that synchronous result straight
into the **existing** `process_response()`. No new processing machine — it
reuses `initiate → process`, just server-driven instead of page-driven.

**Edge cases baked in:**
- **Off-session `requires_action`** (bank wants SCA / mandate needs re-auth):
  `charge_mandate` detects this and returns `get_payment_url(psl)` so the
  caller (e.g. a dues job) can email the member a link; that same PSL then
  completes through the normal interactive path. `flowstates` is left untouched
  — the branch is in `charge_mandate`, not a global reclassification of
  `requires_action`.
- **Mandate revoked / payment method detached:** `_initiate_mandated_charge`
  fails → `Declined` with a clear message so the caller knows to request a
  fresh mandate (a new first-charge).
- **Idempotency:** mandated charges keyed by `psl-{name}`, same as the existing
  charge path.

### Stripe implementation (`stripe_settings.py`, PR-2b)

**First charge saves the mandate** — extend existing methods, do not fork:
- `_initiate_charge`: when `tx_data.save_mandate`, ensure a Stripe `Customer`
  exists for the payer (create + cache its id) and add
  `setup_future_usage="off_session"` + `customer=<id>` to the PaymentIntent
  params. Idempotency key (`psl-{name}`) and metadata unchanged.
- `_process_response_for_charge`: extract the Stripe-status mapping into
  `_map_intent_status(payload)` (shared with the mandated-charge path — DRY).
  On success, if a payment method was saved (`payment_method` + `customer`
  present), persist a `Stripe Mandate` and link it via `psl.set_mandate(...)`.

**Off-session renewals:**
- `_initiate_mandated_charge`: `PaymentIntent.create(amount, currency,
  customer, payment_method, off_session=True, confirm=True,
  idempotency_key="psl-{name}")`; return `Initiated(correlation_id=intent.id,
  payload=intent)` (already resolved synchronously).
- `_process_response_for_mandated_charge`: `self.flags.status_changed_to =
  self._map_intent_status(...)`; return `None` to use default processing.
  `_is_server_to_server()` returns True for this path (no client redirect), so
  the framework's error-muting applies.

**`Stripe Mandate` doctype** (subclass of `PaymentMandate`): fields `gateway`
(link), `customer_id`, `payment_method_id`, `mandate_reference` (Stripe
`mandate` id for SEPA), `status`, optional `payer` ref. `is_usable()` checks
status; `revoke()` detaches the PaymentMethod at Stripe and marks revoked.

**Webhooks:** `handle_webhook_event` already routes
`payment_intent.succeeded/failed`. For off-session intents it resolves the PSL
via `metadata.psl_name` (already set) and runs them through `process_response`.
`requires_action` on an off-session intent surfaces as the re-auth/payment-link
case above.

### DRY/KISS cleanup folded into PR-1b

From the audit of `cdf1bbc` (these are logistical and the generalization
touches the same code):

- **#1 (high)** — collapse the three near-identical `except` blocks in
  `proceed()` into one `_redirect_on_initiation_error(...)` helper; resolve the
  `HTTPError`-branch inconsistency (it reads `frappe.flags.integration_request`
  rather than `psl`).
- **#2 (high)** — collapse the three near-identical `Processed(...)` error
  returns in `process_response()` into one `_error_processed(error, message)`
  helper.
- **#3 (high)** — introduce a `GatewayRef` dataclass
  (`gateway_settings`/`gateway_controller`) with `to_json`/`from_json`,
  replacing the four hand-rolled `json.dumps/loads` sites with stringly-typed
  keys.
- **#5 (med)** — promote the `get_compensatory_action` closure to a
  `_build_compensatory_action(self, psl)` method (symmetric with
  `_build_support_action`).
- **#6 (med)** — settle the `response` param vs `self.state.response`
  inconsistency in `_process_response` (read from `self.state.response`
  consistently).
- **Low** — include the cheap ones (#8 dup `TYPE_CHECKING` imports, #10 twin
  PSL setters `update_gateway_specific_state`/`set_initiation_payload`); note
  any skipped (#9 `_STATUS_MAP` dict-copy is optional polish).

**Scoping guard:** keep the cleanup to code the mandate generalization already
touches, so PR-1b stays "framework + the cleanup that enables it" rather than a
sprawling refactor.

## Testing

- **PR-1b:** extend `test_payment_controller.py` with a fake controller
  implementing the mandate contracts. Cover: flow-type dispatch selects the
  correct process_fn/hook; `charge_mandate` happy path (synchronous success);
  `requires_action` → returns payment URL; revoked-mandate → `Declined`;
  idempotency on re-entry; and **characterization tests** asserting the
  DRY-refactored error paths behave identically to before (the helpers are
  behavior-preserving).
- **PR-2b:** Stripe tests for `save_mandate` → `setup_future_usage` on the
  intent; mandate persisted on success; off-session confirm
  success/failure/`requires_action`; `Stripe Mandate.is_usable()`/`revoke()`;
  webhook → PSL resolution. The Stripe API is a true external boundary, so
  recorded/test-mode responses are appropriate (consistent with the repo's
  "isolate external I/O, don't mock business logic" stance).

## Out of scope (deliberately named)

- €0 standalone `mandate_acquisition` flow (deferred until a consumer needs it).
- GoCardless / Mollie mandate implementations.
- Any change to Verenigingen's offline-SDD subsystem.
- Mandate-registry bridging between PSP mandates and Verenigingen `sepa_mandate`.

## Risks

- **Off-session SCA divergence** between card and SEPA: cards can trigger
  `requires_action` more often; the payment-link fallback handles both but adds
  a round-trip for the member. Acceptable.
- **Customer lifecycle on Stripe:** creating/caching a Stripe `Customer` per
  payer introduces a dependency on a stable payer identity. The reference impl
  keys on payer contact; revisit if a Ref Doc lacks one.
- **Reviewer load / PR split:** for now everything stacks on
  `feat/payment-controller-mandates` and the existing PR is simplified in place.
  The split into reviewable PRs is deferred pending the maintainers' preference;
  the phase boundaries (A/B/C) are authored so the split is mechanical when
  decided.
