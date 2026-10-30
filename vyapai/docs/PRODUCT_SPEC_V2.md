# VyapAI v2 — Complete Product Blueprint

The build order, schema, and screens for turning the current scaffold into the
product described in [DEEP_RESEARCH.md](DEEP_RESEARCH.md). Graded in 4 releases;
each is independently shippable.

---

## R0 — Foundations ("The Spine") · ship in ~4–6 weeks

Without R0 nothing else is real. All items are *repairs*, not new ideas.

### R0.1 Auth & multi-tenancy (security-critical)
- Every route requires a merchant-scoped **API key** (`Authorization: Bearer <key>`
  hashed-at-rest in a new `api_keys` table, one active key per merchant by default).
- Webhooks authenticate differently: WhatsApp via `X-Hub-Signature-256` (already
  coded — enforce `whatsapp_app_secret` as required in `APP_ENV=production`), PSP
  webhooks via the signed PSP's scheme (R1).
- Context object: `current_merchant` injected as a FastAPI dependency; every
  query filtered by `merchant_id = current_merchant.id`. No endpoint trusts a
  body-supplied merchant id, ever.

### R0.2 Money & time correctness
- All `amount` columns → `Numeric(14,2)`; in Python, all money math in **integer
  paise** via a small `Money` dataclass (paise int; `format_inr()` helper).
- `datetime.utcnow()` → timezone-aware `datetime.now(TZ_DEBIAN_IST)`; new
  `TZ_IST` constant; ledger day-boundary = IST midnight for balance-at-date
  queries (as Accountant-general expectations).

### R0.3 Catalog, inventory & vernacular alias resolution
- New tables (see §6 schema): `products` (merchant-scoped SKU), `stock_movements`
  (append-only FIFO ledger of stock), `product_aliases` (vernacular → product).
- `resolve_item(name, lang)` → deterministic alias lookup → trigram fuzzy
  fallback (`pg_trgm`) → low-confidence → merchant-confirmation loop (R1).
- On order confirm: decrement stock via `stock_movements` rows; update
  `products.stock_on_hand` (derived, never trusted directly).
- Seed catalog on merchant onboarding from their most common OCR'd bill items.

### R0.4 WhatsApp inbound router (currently a stub)
- Unwrap `entry[0].changes[0].value.messages[0]`; dispatch by type:
  - `audio` → B2B voice graph,
  - `image` (document/photo) → B2B photo graph,
  - `text` → if sender is unregistered buyer → B2C order graph; if registered
    merchant → entry confirmation / approval decision / row of commands
    (e.g., reply "YES"/"NO" to confirm an entry).
- Reply via outbound WhatsApp with structured confirmation cards (interactivity
  via buttons where supported).

### R0.5 Resume-on-approval (the current silent no-op)
- `decide_approval(approve=True)`: re-hydrate `payload_json` into the B2B graph
  state and call `node_post_ledger` + persist, exactly as if guardrails had
  passed. Log the resolving user in a new `audit_events` table.

### R0.6 Ops
- Webhook **idempotency**: store `webhook_event_id` unique per
  source+message-id; discard duplicates (Meta retries aggressively).
- Structured JSON logging w/ request-id; remove dev CORS `*` in production
  entirely.

---

## R1 — The Merchant Confirmation Loop · the *product* moment (~4 weeks)

**Core UX:** every ambiguous or low-confidence extraction becomes a WhatsApp
question, never a dead error. "Samajh gaya: 2 bag chini ₹42/bag — sahi hai?"
Buttons: ✅ Sahi hai / ✏️ Sudhaar do (correct) / ❌ Cancel.

- New `ConversationThread` and `PendingConfirmation` models. `parse_confidence`
  from the existing B2C parser and the OCR path both route here.
- On "Sahi hai": the confirmed structured entry flows into the normal
  guardrail ledger path (guardrails still run — confirmations validate *intent*,
  not arithmetic).
- B2B voice graph gets its missing text-structuring pass: transcribe → new
  `services/text_structuring.py` → same strict JSON-scheme output shape as
  `vision.py` `BillExtraction`.
- **PII minimization layer before every hosted-model call**: replace phone
  numbers with stable tokens (`[CUST_3]`) and restore them after; zero raw
  customer numbers to model endpoints. Response to DPDP-era expectations.

## R2 — Money Loop (reconciliation + collections QA) (~4–6 weeks)

- **Payment reconciliation engine** `services/reconciliation.py`:
  - PSP webhook (Razorpay or Cashfree signed) matching `tr` / PSP order id →
    Payment row → order → auto-generate a khata-level `LedgerEntry` "Cash /
    Debtors settled by UPI" pair → optionally auto-close the khata balance of
    that buyer.
- Reminders transition to **utility templates** with per-category price
  accounting in a `whatsapp_spend` table (₹0.115/utility outside window, free
  within window — batch sends at send-hour = cheaper).
- **Khata (customer balance) queryable**: merchants ask on WhatsApp
  "Ramesh ka balance?" → agent returns current open balance, last settled, link
  to collect. This single feature out-competes Khatabook's ledger because the
  data is accountant-grade.

## R3 — Money Out: GST & Credit Packets (~6–8 weeks)

- **Monthly GST Essentials**: monthly statement per merchant:
  GSTR-1-ready B2B/ B2C/SUP line summary, GSTR-3B summary, ITC position
  (reuse `reconcile_input_tax_credit`), all from real ledger data. Sent as PDF /
  viewable in a WhatsApp-attached link.
- **Tally-compatible XML + CSV export** ("hand to your CA" button) — this is
  the trust loop into the CA channel for the pilot.
- **Lender Trial Pack**: monthly, consent-based one-page cash-flow card per
  merchant (reuse `credit.build_cash_flow_summary`, but with REAL ledger
  aggregation replacing the zero placeholder) shoppable to ONE
  contracted NBFC in the pilot city. Cap at 1 lender in R3; OCEN adapter stays
  parked behind `requires_human_approval_for_action` until the pilot proves
  demand & data quality.
- Kill/repair `GET /b2b/merchants/{id}/loan-readiness`'s current zero-filled
  placeholder packet end-to-end (it currently outputs a real-feeling but
  fake score).

## R4 — Scale layer (post-revenue)

- Second city rollout, CA referral program, referral rewards.
- Fine-tuned or prompted per-SKU alias learning (every confirmed correction
  becomes a catalog alias row — the dataset compounds).
- Optional: ONDC seller-side listing sync in reverse (buyer demand → order →
  merchant fulfilment) once a named ordering buyer-app channel integrator
  contract is signed. **Not before** — no observable verified success case of
  ONDC kirana voice/chat ordering is publicly documented.

---

## §6 Full target schema (new tables only; existing tables stay, with R0 column fixes)

| Table | Purpose | Key columns |
|---|---|---|
| `api_keys` | Merchant-scoped auth | id, merchant_id, key_hash, label, last_used_at, revoked_at |
| `products` | SKU per merchant | id, merchant_id, name, canonical_unit, sale_price_paise, gst_rate, hsn_code, is_active |
| `product_aliases` | vernacular/typo → product | id, product_id (FK), alias (normalized), language, source ("seed", "merchant_confirmed"), confidence |
| `stock_movements` | append-only inventory ledger | id, product_id, qty_delta (numeric), movement_type (purchase/sale/correction), order_id?, ledger_txn_ref?, created_at |
| `conversation_threads` | one per WhatsApp buyer↔merchant pair | id, merchant_id, buyer_wa_id, last_message_at |
| `pending_confirmations` | staged answer awaiting YES/NO/CORRECT | id, thread_id, kind (entry/order/payment), payload_json, status, expires_at |
| `whatsapp_events` (or `webhook_events`) | dedupe + routing | id, message_id (unique), source, payload_hash, routed_to, processed_at |
| `audit_events` | immutable, attributable log | id, actor_type(merchant/staff/system/ca), actor_id, action, object_type, object_id, before_json, after_json, at |
| `khata_balances` (materialized or derived view) | per-buyer open balance | merchant_id, buyer_contact, open_paise, last_activity_at |
| `whatsapp_spend` | per-message cost ledger | id, merchant_id, template_name, category, price_paise, wam_id |
| `gst_periods` | monthly GST statement staging | id, merchant_id, period, gstr1_json, gstr3b_json, itc_json, status (draft/finalized/exported) |

Column-level migration headline: **every `Float` money column → `Numeric(14,2)`;
every `DateTime` → `timestamptz`; all code moves to integer-paise arithmetic.**

---

## §7 UI surfaces (no web app in the repo today — plan the minimal set)

1. **Merchant WhatsApp surfaces (primary, zero-install UI)**: confirmation
   cards, voice > entry previews, khata queries, monthly GST summary PDF,
   approval buttons for gated actions. The agent *is* the app.
2. **Merchant PWA** (`app/web`): light Flutter/Vite+React PWA, only for:
   day-close dashboard (today's P&L, unpaid khata list), catalog & stock editor,
   settings, staff management. Offline queueing for voice/photos (R0-adjacent).
   Distribution = a link opened on the owner's phone at onboarding.
3. **Ops console** (`app/admin`, internal): merchant search, approval queue
   (ApprovalRequests incl. resume-on-approval buttons), webhook delivery
   replay, spend dashboards. Not public.
4. **CA/partner export link** (public tokenized URLs): read-only ledger + GST
   statement per period; expiring tokens.

---

## §8 The master build sequence (mirrors MASTER_BUILD_PROMPT.md)

1. R0.1–R0.2 in one pass (auth + money/time) — pure code repairs.
2. R0.3–R0.6 in the same PR train (schema + router + resume + ops).
3. R1.0 confirmation loop + text-structuring pass + PII mask layer.
4. R2 reconciliation + khata queries + template reminders.
5. R3 GST monthlies + exports + the real lender packet.
6. PWA + admin console in parallel once R1 stable.

Each stage's exit test: **the merchant-facing demo can run end-to-end with
stubs for everything still unconfigured** (speech `UpstreamError` → graceful
"please type it" fallback, catalog seeded, confirmation loop closed via
WhatsApp simulator in test env).
