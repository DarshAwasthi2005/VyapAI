# VyapAI — Deep Research & Product Strategy (October 2026)

> Compiled from a full code audit of this repository **plus** live web research
> (market data, competitor filings, RBI/Meta/Sarvam/OCEN documentation).
> Every external claim carries a source link. Legal/regulatory notes are
> informational, not legal advice.

---

## 1. What this repo is today (code audit verdict)

VyapAI is a **dual B2B/B2C vernacular commerce agent platform** for Indian kirana
and wholesale merchants:

- **B2B engine**: merchant speaks a voice note or photographs a handwritten bill →
  ASR/vision extraction → deterministic guardrails → double-entry ledger →
  GST reconciliation, e-way bill staging, OCEN loan-readiness packet.
- **B2C engine**: buyer orders over WhatsApp in free text → parse → inventory
  check → price → UPI payment link → polite escalating payment reminders.

**State: honest deployable scaffold. 32/32 tests pass.**

### What is REAL and validated

| Component | Verdict |
|---|---|
| [accounting.py](../vyapai/app/services/accounting.py) | Real double-entry math: sale/purchase transaction builders, ITC reconciliation, line-item-vs-total cross-check, paise rounding, balance assertion. |
| [guardrails.py](../vyapai/app/agents/guardrails.py) | Deterministic circuit breaker + `requires_human_approval_for_action()` hard-gates e-way bill/loan/GST filings. This is the repo's best design decision. |
| [upi.py](../vyapai/app/integrations/upi.py) | Fully functional UPI deep links (NPCI standard), transaction refs for reconciliation. |
| [vision.py](../vyapai/app/integrations/vision.py) | Working Gemini + Ollama OCR adapters with strict JSON extraction schema and the crucial prompt instruction "do not compute totals — transcribe." |
| LangGraph graphs | Both B2B/B2C graphs wired with typed state + conditional edges; thin over business services (swappable). |
| DB schema / Alembic | Consistent models ↔ migration ↔ enums. |
| Infra | docker-compose, k8s manifests, hourly reminder CronJob, API docs. |

### What is INCOMPLETE (the "Known gaps" the docs already admit)

1. **Approval resolution never resumes the graph** — approving a paused entry just
   flips a status; the ledger write never happens.
2. **WhatsApp webhook routing is a stub** — payload acknowledged but not unwrapped.
3. **No multi-tenancy / auth** — every endpoint trusts `merchant_id` from the
   request body.
4. **Exact-match inventory** (`"sugar"` in a dict) — no alias catalog, no stock
   decrement on order.
5. **Voice path extracted nothing after transcription** — `node_ingest` transcribes
   but never structures `raw_text` into line items (no text-LLM pass).
6. **No merchant catalog/stock tables** — B2C prices/stock are hardcoded dicts in
   [routes_b2c.py](../vyapai/app/api/routes_b2c.py).
7. No refunds, no multi-user staff roles, no web/app UI whatsoever, no audit log
   containing timestamps+beyond `source` field, no rate limiting, no idempotency on
   webhooks (PSP retries will double-write), no decimals-safe money type (floats!).

### Fonts of risk for the founder

- `Float` for INR amounts (floating point ledger = eventual audit pain; must move
  to `Numeric(12,2)`/paise-int before day one of production).
- `utcnow()` deprecated across the codebase; no timezone-aware datetimes — and
  **Indian Accounting Standard timezones matter** (day boundary at IST, not UTC).
- WebGL of features above means the current repo is a *spine*, not a product.

---

## 2. The market — numbers with sources

| Signal | Figure | Source & caveat |
|---|---|---|
| Kirana stores | ~13M, only 3% tech-enabled (2018), 70% urban ready-to-adopt | [Invest India](https://www.investindia.gov.in/team-india-blogs/modernization-of-kirana-stores-india) — dated census; use as context, not TAM |
| MSMEs (registered) | 6.2 crore by Mar 2025 | [SIDBI report](https://www.sidbi.in/uploads/Understanding_Indian_MSME_sector_Progress_and_Challenges_13_05_25_Final.pdf) (not independently fetched) |
| MSME credit gap | ~₹30 lakh crore addressable | Same SIDBI source |
| UPI (Dec 2025) | 21.63B txns; Jan 2026 ~21.7B / ₹28.33L cr | [NPCI UPI stats](https://www.npci.org.in/product/upi/product-statistics), [BusinessLine](https://www.thehindubusinessline.com/money-and-banking/upi-transactions-hit-record-high-of-2833-lakh-cror-in-january/article70576968.ece) |
| ONDC (Mar 2025) | 1.6 crore orders/month, 20.4 crore cumulative, 7.6L sellers | [ETRetail](https://retail.economictimes.indiatimes.com/news/e-commerce/e-tailing/ondc-crosses-20-4-cr-transactions-in-march-2025/120308007) |
| Merchants on PhonePe Biz | 5+ crore | [business.phonepe.com](https://business.phonepe.com/) |

**Missing: no credible public TAM for kirana accounting SaaS.** Size the tam by
distribution pools (UPI merchant base, khata-app MAU), not store counts alone.

## 3. Competitive landscape (verified)

**Ledger/billing/GST incumbents:** Khatabook (10M+ MAU, loans ₹10k–5L w/ partners,
*no* verified voice-to-ledger), OkCredit (shut its P2P OkNivesh Jan 2025 after RBI
rule changes; FY24 = ₹14.3cr revenue, ₹35cr loss — a cautionary tale on
payments-adjacent monetization; [ET](https://economictimes.indiatimes.com/tech/startups/okcredit-shuts-down-p2p-platform-oknivesh/articleshow/117149754.cms)),
Vyapar / myBillBook / Zoho Books / Tally (GST billing + inventory + reports, all
typing/tap-first, none voice-first), Paytm for Business / PhonePe for Business
(*payments-first*, no books).

**B2B procurement (the real competitive threat to your B2C engine):**
Udaan (FY25 ₹4,561cr revenue, +$114M Series G Jun 2025),
Jumbotail+Solv (500k retailers, 400+ cities, +$120M Jun 2025, explicitly
"investing in AI-native solutions"). They own the wholesaler-supply side; a
kirana-side ordering agent without supply aggregation is a tool, not a moat.

**Direct analogue that just launched:** **Pilloo AI** — Andhra Pradesh, launched
Feb 2, 2026: spoken invoice creation + transaction recording in **5 Indian
languages** + bill/statement upload extraction. ([BusinessLine](https://www.thehindubusinessline.com/info-tech/andhra-pradesh-cm-launches-pilloo-ai/article70583731.ece)).
This validates voice-first accounting demand in India — and proves the window
before someone pairs it with payments + credit is short. **Pilloo has no
payments/credit/engine; pairing those is the whitespace.**

**Invoice finance adjacency:** M1xchange/KredX (TReDS) — requires formal
corporate-buyer invoices; a kirana khata is not eligible. Not a fit for v1.

### Strategic position (where VyapAI can win)

The competitors split into "khata apps" (free ledger w/ weak data quality),
"accounting suites" (powerful, too heavy for voice-first merchants), and
"payment super-apps" (money first, zero books). **Nobody owns the loop:
speak → accurate P&L → get paid → file GST → collateralized by the same
trusted dataset → access cheap trade credit.** That single converging ledger —
GSTR-quality numbers by voice, feeding both GST filings *and* a lender — is the
moat. Khatabook's ledger was never accountant-grade; an accountant-grade
ledger *acquired by voice* is defensible.

---

## 4. Integration-reality check (2026, researched with sources)

| Capability | Verdict (2026) | Recommendation |
|---|---|---|
| **Vernacular ASR** | Bhashini: real but enterprise-paced, no public SLA. **Sarvam Saaras/Saarika V4** ₹30/hr (~₹0.50/min) streaming/batch + key-term prompting; supports Hindi/Tamil/Gujarati | Pilot **Sarvam first** (managed, cheap, vocal-code-mix aware), Bhashini as gov-aligned option, IndicWhisper/AI4Bharat self-hosted for offline/rural. Sources: [Sarvam STT](https://www.sarvam.ai/apis/speech-to-text), [Bhashini](https://bhashini.gov.in) |
| **Handwritten bill OCR** | Sarvam Vision 2.1: 93.52% Hindi / 88.87% Gujarati / 87.70% Tamil OCR vendor-self-reported; Gemini 2.5 class strong generalist | Keep the existing **Gemini adapter as tier-1** (free tier, no card), add **Sarvam Vision as tier-2 for Indic scripts**, Ollama stays the offline fallback. Never trust model math → keep current guardrails. [Sarvam Vision blog](https://www.sarvam.ai/blogs/sarvam-vision-2-1) |
| **WhatsApp Cloud API** | Per-message pricing since **Jul 2025**: marketing ≈ ₹0.7846→₹0.8631 (2026 card), utility ≈ ₹0.115, authentication ≈ ₹0.115; free replies inside 24h customer window. **Meta's 2026 AI-provider terms restrict general-purpose AI assistants**; business-specific commerce flows are documented & legal (Flows, catalogs, carts) | Reminders = **utility templates**, conversational commerce inside the window, use catalogs/Flows not free-form. [Meta pricing](https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing) |
| **UPI** | Deep links free (already built); money movement needs an **RBI-authorized PA** (Razorpay/Cashfree/PayU) acting for each merchant; **you may NOT pool merchant funds** | Keep links for v0.1; wrap a PA per-merchant before live settlement. [RBI PA list](https://www.rbi.org.in/Scripts/bs_viewcontent.aspx?Id=4236) |
| **UPI AutoPay** | ₹15,000/debit AFA cap (₹1,00,000 special cases); use-cased via PA mandate | Not a collections shortcut for repayments; payments must go direct to RE. [NPCI AutoPay](https://www.npci.org.in/product/autopay) |
| **Credit / OCEN** | OCEN 4.0 is live-ish (~70,000 loans, ₹1,600cr across 2025 network-reported), but there is **no self-serve LSP onboarding** and RBI DLG caps at 5% via permitted form. You must have (a) an OCEN network participant AND (b) an RBI-regulated lender AND (c) an LSP contract under the **RBI Digital Lending Directions 2025** (in force; multi-lender provisions effective Nov 1, 2025) | **Do NOT make OCEN the launch dependency.** Ship first with one named NBFC/bank partner + a conventional RE integration; keep `ocen.py` behind the existing human-approval gate. Sources: [RBI DLG Directions 2025](https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=12848) |
| **GST filing / e-way bill / e-invoicing** | e-invoicing mandate ≥ ₹5cr turnover (Aug 2023) — most kiranas *not* mandated; e-way bill ≤₹50,000 consignment exemption; QRMP lets ≤₹5cr file quarterly (GSTR-1 by 13th, GSTR-3B by 22nd/24th). GSTN access via licensed GSP | Ship **GST-ready books + GSP-sync exports** as the wedge; keep "file for me" as a human-in-loop action routed to a CA partner or GSP. Sources: [GST Council](https://gstcouncil.gov.in/node/4365), [CBIC Rule 138](https://taxinformation.cbic.gov.in/...) |
| **DPDP Act 2023 / AI data** | DPDP rules phased to May 2027; no blanket localization, but *you* are a data fiduciary for proprietor/customer voice + phone + balances. Free Gemini tier trains on your data; **OpenAI API tier doesn't by default**, Sarvam enterprise API terms are negotiable. Bhashini gov | Send minimum fields; **never** send raw customer phone numbers or voice to free-tier models; pseudonymize and mask PIIs; disclose AI usage to merchants; check vendor T&Cs. [Gemini terms](https://ai.google.dev/gemini-api/terms), [Sarvam privacy](https://www.sarvam.ai/privacy-policy) |

---

## 5. FEATURE MATRIX: what to ADD, CUT, and KEEP

### 5a. ADD (ordered by revenue-proximity × build-cost)

**Tier A — wire the spine (without these there's no product):**

1. **Merchant catalog + Inventory tables + alias matching** ✅ (currently the #1
   functional gap; the B2C engine literally can't run against real data). Fuzzy
   vernacular match: alias table (`chini`→sugar, `shakkar`→sugar) seeded at
   onboarding + trigram similarity fallback.
2. **WhatsApp webhook inbound router** (currently an acknowledged stub): unwrap
   Meta's `entry[0].changes[0].value.messages[0]` payload, signature-check,
   dispatch AUDIO → B2B voice path, IMAGE → B2C/B2B photo path, TEXT → the
   merchant's active B2C ordering flow. This single step turns "API app" into
   "WhatsApp product."
3. **Resume-on-approval**: rewrite `decide_approval` to re-hydrate
   `payload_json` → invoke graph → post ledger. (Current bug: approving silently
   does nothing.)
4. **Forced auth**: merchant-scoped API key (or WhatsApp-number login) on every
   endpoint. Currently zero.
5. **Money in paise / Numeric(12,2)** and **timezone = IST-day-boundary**.

**Tier B — the differentiators (build these on the spine):**

6. **Voice-text structuring** — after `speech.transcribe()`, run the text-LLM pass
   using the same strict-schema JSON pattern as `vision.py` (`EXTRACTION_PROMPT`
   structure already exists; just chain it). The docstring says "left to a
   text-completion call" — do it; plus a **merchant confirmation loop**
   (WhatsApp message: "I heard: 2 bags sugar ₹42 each. Correct?" — reply YES/NO)
   which converts every ambiguous case into a delightful UX instead of an
   error. This confirmation flow *is* the product for a kirana owner.
7. **Reconciliation as the headline feature**: match inbound PSP/UPI credits
   against open invoices + khata balances and auto-write "customer settled their
   khata" transactions. This is what Soundbox users have and khata apps don't.
8. **Credit-lite readiness ("Lender Trial Pack")**: monthly cash-flow packet PDF/
   WhatsApp card generated from the ledger, shoppable to one NBFC partner first.
   Keep OCEN adapter behind the approval gate as designed, but **don't gate the
   product-wedge on OCEN empanelment** (which the research shows takes a bank
   partner first anyway).
9. **Multi-language voice-UX polish**: Hindi/Tamil/Gujarati/Marathi/Bengali
   onboarding scripts read by a real voice, plus per-language keyword aliases
   harvested during use ("chini", "sakkharai", "cheeni", …).
10. **Offline resilience**: queue voice-notes/photos locally in a PWA when the
    shop has no signal, sync when back; the merchant must never lose a day's
    entries to a network failure.
11. **Multi-staff roles**: owner vs. helper (helpers can add entries, not view
    monthly P&L or take approvals). Khatabook already taught the market that
    staff sharing kills trust.
12. **Data-plane hygiene for LLM calls**: strip/pseudonymize customer phone
    numbers and names before they hit any hosted model. Mask with
    `{{CUSTOMER_A}}` and disclose processing in a merchant notice.

**Tier C — monetization-ready:**

13. **Freemium funnel**: 30 free voice-entries/month → paid tiers (entry
    packs, GST-season pass, multi-staff, credit-trial-pack inclusions). Price at
    ₹199/₹499/₹999/month Anna/Traders/Wholesaler geographies; ground-tested
    before fleetwide rollout.
14. **Referral & CA-evangelist loop**: every ledger summary includes a
    "share with your CA" export link (CSV + Tally-compatible XML) — CAs are the
    de-facto buyers of small-town merchants and the highest-trust reviewers.
15. **GST essentials**: monthly GSTR-1/3B summary + ITC reconciliation (already
    exists in `reconcile_input_tax_credit`) — surface it as a monthly WhatsApp
    report auto-generated per merchant; forward-ready for a filing partner.

### 5b. REMOVE / SIMPLIFY (be ruthless; the current scaffold is over-built for v1)

| Current feature | Verdict | Why |
|---|---|---|
| **OCEN submission engine** | **Deprioritize** to a parked, gated module | Requires LSP empanelment + RE partner + RBI-DLG compliance before a single call is legal. Research shows the practical path is an NBFC/bank partner first. Keep the adapter; don't build flow around it until one lender signs. |
| **e-Way bill generation** | **Deprioritize** | E-way bills only bite above ₹50,000 consignments (CBIC Rule 138) and require GSP registration. A kirana/wholesale merchant's daily flow doesn't need it untilGST-season. Keep the schema; don't polish the adapter. |
| **Bhashini as primary ASR** | **Demote** | Enterprise signup friction, unclear SLAs; Sarvam is the pragmatic tier-1. Keep the adapter as a fallback — it's already written. |
| **Ollama tier-1 vision** | **Demote** | 60–120s/image on CPU and lower accuracy; merchants will abandon a slow flow. Ideal for offline/rural with a GPU box, but not the primary path. |
| **Generic PSP webhook** (`/webhooks/payments/psp` accepting any `transaction_ref`) | **Remove** until a PA is signed | An unauthenticated unverified payment webhook is a security hole (anyone can mark anything paid). Accept it only after a named PSP integration with signature verification. |
| **Loan "check_loan_readiness" endpoint with placeholder 0-inflow packet** | **Fix or cut** | It builds a packet of all zeros and returns flags like "no GST turnover" — which will feel like a broken credit check to a real merchant. Cut the placeholder path until real ledger aggregation exists (Tier B #8). |

### 5c. KEEP (already good, protect as invariants)

- **Deterministic guardrails between LLM and ledger** — never allow a model to
  compute a total, a tax, or an auto-post threshold. This is your licence to
  operate when auditors, the CA community, or RBI ask "who checked the AI?".
- **`requires_human_approval_for_action()` hard-gates** — e-way bill, loan
  application and GST filing always human-gated, "not a config toggle."
- **Adapters fail loudly** (`NotConfiguredError`) — no silent mocks in prod.
- **One ledger shared by both engines** — architecturally your moat; every new
  B2C feature must keep writing through `accounting.py`.
- **Decimal/paise problem awareness** — but fix it now, not at IPO:
  numeric columns, integer paise, FIFO ITC ledgers.

---

## 6. Release strategy (what the research says actually fails)

- **Historical stall pattern**: 13M kiranas exist, but retention collapses when a
  tool needs daily discipline and gives no immediate value. Free khata apps get
  installs; paid conversion struggled (~1.5% for Khatabook's Biz Analyst paying
  cohort, [ET 2022](https://economictimes.indiatimes.com/tech/startups/khatabook-bets-on-lending-saas-for-revenue/articleshow/94948797.cms)).
  **Design rule: every session must produce visible accountant-grade value
  within 60 seconds, or it will churn.**
- **Never monetize credit before you have trustworthy books**: OkCredit's P2P
  product shut down after RBI's P2P rule change; LSPs without an RE contract are
  in violation of the DLG Directions. Books-first → one-lender pilot → scale.
- **Onboarding is in person, not ads**: kirana SaaS wins in one district at a
  time with feet on street / CA-coordinated demos. Plan a 2-city pilot
  (e.g., Indore + Coimbatore) with Hindi + Tamil/Gujarati tracks.

### 12-month roadmap recommendation

- **M0–M2 (the spine)**: auth, catalog/inventory + aliases, webhook router,
  resume-on-approval, paise/IST fixes. Exit: a real merchant can voice-note a
  sale on WhatsApp and a correct two-ledger-line entry appears.
- **M3–M5 (the loop)**: confirmation UX, reconciliation, monthly GST report PDF,
  Tally export, PWA offline. Exit: a merchant "closes the day" by voice and gets
  a one-tap P&L + ITC position.
- **M6–M9 (money)**: signed PA integration (Razorpay or Cashfree),
  utility-template reminders against real PSP webhooks, first NBFC "lender
  trial pack" pilot. Exit: first MRR, first ₹ of credit distributed.
- **M10–M12 (credit + scale)**: DLG-compliant referral structure, second city,
  CA/referral loop, electric monetization at ₹199/₹499/₹999.

## 7. The moat statement (for pitch decks)

> "Merchants' backpacks are full of apps that ring the register. **We plant a
> CA audit trail by voice** — and the faster a merchant speaks and orders, the
> stronger his credit file — a compounding data asset no payment app, khata
> app, or ERP can retrofit without rebuilding their data model."

---

## 8. Source index

- Onboarding & kirana behaviour: [Flourish Ventures shopkeeper study](https://www.prnewswire.com/news-releases/new-research-finds-corner-shops-are-faring-well-against-big-box-stores-in-emerging-markets-but-digital-challenges-remain-301623857.html)
- ASR top speeds: [Sarvam STT API](https://www.sarvam.ai/apis/speech-to-text) · [Bhashini](https://bhashini.gov.in)
- OCR: [Sarvam Vision 2.1](https://www.sarvam.ai/blogs/sarvam-vision-2-1) · existing [vision.py](../vyapai/app/integrations/vision.py)
- WhatsApp pricing & AI-provider policy: [Meta WhatsApp pricing](https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing)
- Payments & credit: [RBI PA authorization list](https://www.rbi.org.in/Scripts/bs_viewcontent.aspx?Id=4236) · [NPCI AutoPay](https://www.npci.org.in/product/autopay) · [RBI Digital Lending Directions 2025](https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=12848)
- OCEN scale: [iSPIRT ProductNation OCEN 2025](https://pn.ispirt.in/ocen-2025/)
- GST: [CBIC Rule 138 e-way bill](https://taxinformation.cbic.gov.in/), [GST Council e-invoicing](https://gstcouncil.gov.in/node/4365), [QRMP FAQs](https://tutorial.gst.gov.in/userguide/returns/FAQs_change_profile.htm)
- DPDP & LLM data: [Gemini API terms](https://ai.google.dev/gemini-api/terms), [Sarvam privacy policy](https://www.sarvam.ai/privacy-policy), [Hogan Lovells DPDP brief](https://www.hlc.com/en/publications/indias-digital-personal-data-protection-act-2023-brought-into-force-)
- Competitor operating signals: ET (Khatabook, OkCredit, Udaan), Entrackr (Khatabook layoffs / MyStore shutdown), The New Indian Express (Jumbotail), BusinessLine (Pilloo AI).
