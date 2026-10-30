# VyapAI — Master Build Prompt

A single, copy-paste-ready prompt that encodes everything from
[DEEP_RESEARCH.md](DEEP_RESEARCH.md) and [PRODUCT_SPEC_V2.md](PRODUCT_SPEC_V2.md).
Give it to any capable coding agent (or yourself) to build VyapAI v2 to
production quality in stages.

---

## The one-prompt master build

```text
You are building VYAPAI v2 — a vernacular (Indian-language) voice-first commerce
agent for kirana/wholesale merchants, backend in the existing FastAPI +
SQLAlchemy + LangGraph repo. The repo already contains a tested scaffold
(app/, tests/, migrations/). Your job is to finish the product in the staged
order below, keeping the existing architecture invariants intact. Do not
bypass them:

INVARIANTS (never violate)
1. "LLMs interpret, code decides": any ASR/OCR/text-LLM output only extracts;
   all arithmetic, GST-slab validation, and posting decisions happen in
   deterministic Python (app/agents/guardrails.py + app/services/accounting.py).
2. requires_human_approval_for_action() always gates e_way_bill,
   loan_application, gst_filing. Never auto-submit these.
3. Both B2B and B2C engines write through app/services/accounting.py into one
   shared ledger. No side-channel writes.
4. Integrations fail loudly: NotConfiguredError until configured, UpstreamError
   on failure. Never fabricate financial data or mock money movement.
5. All INR amounts are integer paise in Python and Numeric(14,2) in Postgres.
   All datetimes are timezone-aware; business day boundary is Asia/Kolkata.

STAGE R0 — THE SPINE
R0.1 Auth: add api_keys table (merchant-scoped bearer keys, hashed at rest),
a require_merchant dependency, and apply it to every /b2b and /b2c route.
Remove any use of body-supplied merchant_id for authorization. In
APP_ENV=production, WHATSAPP_APP_SECRET must be set or startup fails.
R0.2 Money/time repairs: migrate every Float money column to Numeric(14,2);
replace datetime.utcnow() with timezone-aware now(TZ_IST) everywhere; add a
Money dataclass (paise int) and use it in accounting.py, orders.py, collections.
R0.3 Catalog: add products, product_aliases, stock_movements tables (see
docs/PRODUCT_SPEC_V2.md §6). Implement resolve_item(name, lang): exact alias →
pg_trgm fuzzy → confidence score; decrement stock via stock_movements on order
confirmation. Seed aliases from a starter vernacular map (chini/shakkar→sugar,
namkeen, aata, chai patti, etc.) + merchant-confirmed corrections append rows.
R0.4 WhatsApp router: in api/webhooks.py unwrap
entry[0].changes[0].value.messages[0]; enforce signature verification; dispatch:
audio→B2B voice graph, image→B2B photo graph, text→B2C order graph when the
sender is a buyer, or sender-merchant flows (confirmation replies YES/NO,
"balance of <buyer>", "aaj ka hisab"). Store whatsapp_events with unique
message_id for idempotency (Meta retries).
R0.5 Resume-on-approval: decide_approval(approve=True) must re-hydrate
payload_json into the B2B state, run node_post_ledger, persist ledger rows,
and write an audit_events row naming the actor. approve=False archives.
R0.6 Add audit_events table and write an entry for: every ledger post, every
approval decision, every callback that moves Payment status.

STAGE R1 — CONFIRMATION LOOP (the core UX)
R1.1 New tables: conversation_threads, pending_confirmations. Any extraction
with parse_confidence=low, unresolvable item names, or guardrail "needs human
review" becomes a WhatsApp message to the merchant with the parsed entry
rendered in their preferred_language and YES/CORRECT/CANCEL buttons.
R1.2 On YES: re-run the guardrail node and post normally. On CORRECT: parse the
merchant's correction text (or voice), update the pending entry, re-confirm.
R1.3 Text structuring for the B2B voice path: after speech.transcribe(), add
services/text_structuring.py that calls a text LLM (Sarvam/Gemini — reuse
vision.py's adapter pattern and EXTRACTION_PROMPT discipline; no key →
NotConfiguredError) to convert raw_text into the same BillExtraction JSON
shape, then the existing guardrails run unchanged.
R1.4 PII minimization: before ANY hosted-model call, tokenize phone numbers
and known person names ([CUST_n]) and de-tokenize after; add a merchant-facing
AI-usage disclosure string in onboarding.

STAGE R2 — MONEY LOOP
R2.1 Reconciliation: services/reconciliation.py consumes a signed PSP webhook
(Razorpay scheme by default; signature required); matches transaction_ref →
Payment → Order; writes the khata-settlement ledger pair through
accounting.py; closes open buyer balance.Webhook idempotency enforced.
R2.2 Khata queries: on WhatsApp text "RAMESH KA BALANCE" (and vernacular
variants) return the buyer's open balance, last activity, and a collect-link
(UPI deep link, existing upi.py).
R2.3 Reminders: keep the escalation schedule; move sends to WhatsApp utility
templates with a whatsapp_spend cost ledger row per send; honor the 24h
window (free replies inside it).
R2.4 Replace the fake loan-readiness endpoint: compute monthly inflows/
outflows/receivables/payables from real LedgerEntry rows, or return 501
"requires at least 30 days of ledger history" rather than a zero-filled packet.

STAGE R3 — GST + CREDIT PACKET
R3.1 Monthly GST essentials job: per merchant, build gstr1/gstr3b/ITC summary
JSON (reuse reconcile_input_tax_credit) into gst_periods, render a clean PDF,
and auto-send as a WhatsApp document on the 3rd of each month.
R3.2 Export: Tally-compatible XML + CSV endpoints with expiring tokenized
public links (for the merchant's CA).
R3.3 Lender Trial Pack: consent-based, one-page cash-flow card from the REAL
ledger (services/credit.py), consumable by one contracted NBFC; keep ocen.py
parked behind the approval gate. Document that going past one lender requires
an LSP contract under RBI Digital Lending Directions 2025.

CONSTRAINTS
- Modify the existing files; migrations via alembic revision per stage; keep
  tests green (add tests per feature; target every new service has ≥1 unit
  test; accounting/guardrail tests stay untouched in spirit).
- Every stage ends with: `pytest` passing, a 5-line STAGE_REPORT summarizing
  what changed, which tests cover it, and what remains stubbed.
- Do not introduce: new paid third-party dependencies without a config flag,
  any auto-approval path for gated actions, any raw-PII gateway to a model
  API, or a fake/mock payment success path.
- Vernacular UX strings (Hindi romanized + Devanagari, and en fallback) live
  in a single app/i18n.py module so merchants' language choice is one setting.
```

---

## How to use this prompt

1. **Run it stage by stage.** Capable agents do better with
   `"Execute STAGE R0 only, then stop for review"` appended. R0 is security
   + correctness; review it manually before letting an agent touch R1+.
2. **Rejection criteria per stage** (test the agent, not just the code):
   - R0: hitting any /b2b route without a key must return 401; a webhook
     replayed twice must post exactly once; approving an ApprovalRequest must
     actually create ledger rows.
   - R1: send a slow/noisy voice note → get a WhatsApp confirmation card →
     reply "sahi hai" → see the posted group id. Send gibberish → get a
     graceful "please type or correct" message, never a 500.
   - R2: a PSP webhook marking a payment paid creates the khata-settlement
     ledger pair exactly once even if the webhook is replayed.
   - R3: the monthly PDF opens, the ITC line ties to the ledger sum, the
     Tally XML imports into TallyPrime without errors.
3. **Then paste the stubs one at a time** once credentials exist: Sarvam ASR
   key → speech.py; PA onboarding (Razorpay/Cashfree) → PSP webhook config;
   WhatsApp Business verification → templates; GSP sandbox → gst.py. Each is a
   1-2 file change because of the adapter pattern — that was the point.

---

## Non-goals (write these on the wall)

- **No auto-posting above ₹50,000, ever** (guardrail threshold, already coded).
- **No auto-submission of e-way bills, GST filings, or loan applications** —
  human confirmation always, "not a config toggle" (already coded).
- **No pooled merchant funds** — settlement flows through an RBI-authorized PA
  to the merchant's own account, never through a VyapAI account.
- **No credit distribution without a regulated lender contract** — one NBFC
  pilot before any talk of OCEN scale.
- **No web dashboard before R1 ships** — the WhatsApp agent IS the product;
  the PWA exists for day-close + catalog editing only.
