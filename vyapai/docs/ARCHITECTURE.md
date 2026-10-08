# Architecture

## Design principles

1. **One ledger, two front doors.** A WhatsApp sale and a voice-note journal
   entry both ultimately write `LedgerEntry` rows. The B2C engine doesn't
   know about GST or OCEN; the B2B engine doesn't know about WhatsApp. They
   meet at `app/services/accounting.py` and the `merchants`/`ledger_entries`
   tables.

2. **LLMs interpret, code decides.** Speech-to-text and vision OCR (both
   LLM-backed) only ever *extract* — they never compute a total, apply a
   tax rate, or decide whether something is safe to post. Every number that
   reaches the ledger passes through `app/agents/guardrails.py`, which is
   plain deterministic Python: arithmetic cross-checks, GST-slab validation,
   amount-threshold checks.

3. **Some actions are never fully autonomous.** `requires_human_approval_for_action()`
   hard-codes that e-way bill submission, loan applications, and GST filings
   always land in the `ApprovalRequest` queue, independent of amount or
   guardrail outcome. This isn't a configurable toggle — it's a design
   decision that submitting a merchant's data to a government or lending API
   should never happen without a human confirming it, at least for v1.

4. **Adapters fail loudly, not silently.** Every integration in
   `app/integrations/` raises `NotConfiguredError` if its credentials are
   missing and `UpstreamError` if the (stubbed or real) call fails. Nothing
   falls back to fabricated data.

## B2B flow (voice/photo -> ledger -> optional GST/OCEN)

```
Merchant sends voice note or bill photo
        │
        ▼
POST /b2b/entries/voice  or  /b2b/entries/photo
        │
        ▼
graph_b2b.py: ingest node
   - voice -> integrations/speech.transcribe()
   - photo -> integrations/vision.extract_bill()
        │
        ▼
graph_b2b.py: guardrail node
   - agents/guardrails.validate_bill_extraction()
     (line-item arithmetic, GST slab validity)
        │
   ┌────┴────┐
   │         │
 passed    failed
   │         │
   ▼         ▼
post_ledger   route_to_human
(services/    (db.models.ApprovalRequest,
 accounting)   resolved via
   │           POST /b2b/approvals/{id}/decide)
   ▼
LedgerEntry rows committed
   │
   ▼ (separate, always-gated action)
GET /b2b/merchants/{id}/loan-readiness
   -> services/credit.loan_readiness_score()
   -> (on merchant confirmation) integrations/ocen.submit_loan_application()
```

## B2C flow (WhatsApp order -> inventory -> UPI -> reminders)

```
Buyer messages merchant's WhatsApp number
        │
        ▼
Webhook: POST /webhooks/whatsapp  (Meta Cloud API)
        │  (signature verified via integrations/whatsapp.verify_signature)
        ▼
routes_b2c.submit_order()  (or a router that dispatches by sender's active flow)
        │
        ▼
graph_b2c.py: parse -> check_inventory -> [in stock?]
                                              │
                          ┌───────────────────┴───────────────────┐
                          │                                       │
                        yes                                       no
                          │                                       │
                          ▼                                       ▼
                 price_order -> generate_payment_link      notify_buyer
                          │                                (out-of-stock msg)
                          ▼
                    notify_buyer
                 (order confirmed + UPI link)
                          │
                          ▼
        Payment row (status=pending) created
                          │
          ┌───────────────┴────────────────┐
          │                                 │
   PSP webhook fires                  CronJob (hourly)
   POST /webhooks/payments/psp        app/jobs/send_reminders.py
   -> status=success                  -> services/collections.decide_next_reminder()
                                       -> integrations/whatsapp.send_payment_reminder()
```

## Why LangGraph, specifically

Both flows are short, mostly-linear state machines with exactly one branch
point each (guardrail pass/fail; in-stock/out-of-stock). LangGraph is used
here for:
- A typed `State` dict that's easy to serialize into `ApprovalRequest.payload_json`
  when a flow needs to pause for a human.
- Conditional edges that make the branch point explicit and testable in isolation
  (`_route_after_guardrail`, `_route_after_inventory`).
- Room to grow: multi-turn WhatsApp conversations (e.g., "did you mean 5kg or
  5 bags?") map naturally onto adding a re-entrant node without restructuring
  the whole flow.

If your team prefers a lighter-weight alternative (plain function
composition, a state machine library you already use), the graphs are a thin
layer over `app/services/*` — swapping orchestration frameworks doesn't touch
business logic.

## Data model

See `app/db/models.py`. Key relationships:
- `Merchant` 1—N `LedgerEntry` (via `merchant_id`)
- `Merchant` 1—N `Order` (via `merchant_id`)
- `Order` 1—N `OrderLineItem`, `Order` 1—1 `Payment`
- `ApprovalRequest` references a `merchant_id` and stores the paused agent
  state as JSON — resolving it (`decide_approval`) doesn't currently
  re-invoke the graph from where it paused (see "Known gaps" below).

## Known gaps / next steps

- **Approval resolution doesn't resume the graph.** `decide_approval()`
  flips the `ApprovalRequest` status but doesn't automatically re-run
  `post_ledger` on approval — wire that up (re-hydrate the saved state,
  call `node_post_ledger` directly) before relying on this in production.
- **WhatsApp inbound routing is a stub.** `webhooks.py` acknowledges Meta's
  payload but doesn't yet unwrap it or decide whether a message is a new
  B2C order, a reply to a B2B voice-note confirmation, etc. That routing
  logic depends on your actual conversation design.
- **Fuzzy item/catalog matching.** `services/orders.py`'s inventory check is
  exact-lowercase match. Real vernacular orders will use local terms
  ("chini" for sugar) — plug in a catalog alias table or an LLM-assisted
  match before `check_inventory`.
- **No multi-tenancy/auth layer yet.** Every endpoint trusts `merchant_id`
  from the request. Add API-key or session auth scoped per merchant before
  exposing this beyond a private pilot.
