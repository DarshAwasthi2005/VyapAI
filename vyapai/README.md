# VyapAI — Dual B2B/B2C Vernacular Commerce Agent Platform

VyapAI is an agentic backend for Indian kirana/wholesale merchants. One platform,
two agent engines, sharing a ledger:

- **B2B Engine** — merchants speak or photograph their day's business (voice notes
  in Hindi/Tamil/Gujarati/etc., or photos of handwritten bills). The system turns
  that into double-entry accounting, GST-reconciled records, e-way bills, and a
  standardized loan-readiness packet submitted over OCEN.
- **B2C Engine** — a retail buyer orders over WhatsApp in their own words, one
  item at a time ("7 bags of sugar by 4 PM"), the agent checks inventory, sends a UPI payment link, and runs a polite, escalating reminder sequence for
  anything left unpaid.

Both engines write to the same merchant ledger, so a WhatsApp sale updates the
same books that feed the GST filing and the loan application.

## Status: deployable scaffold, not a live financial integration

This repository is a complete, working architecture — routing, state machines,
validation, database models, deployment configs, and CI — with **every external
integration built as a clean adapter behind an interface** (`app/integrations/`).
The business logic (accounting, GST arithmetic, UPI link construction, guardrail
validation) is real and tested. The adapters for Bhashini, WhatsApp Cloud API,
GSTN e-way bill APIs, and OCEN are stubbed with the real request/response shapes
and clear `TODO` markers for credentials — because those each require your own
merchant/business registration, API keys, and (for GSTN/OCEN) regulatory
sign-off that no one can provide on your behalf. See
`docs/COMPLIANCE_CHECKLIST.md` before pointing any of this at real money or
real tax filings.

## Architecture

See `docs/ARCHITECTURE.md` for the full diagram and data flow. In short:

```
                 ┌─────────────────────────────┐
 WhatsApp ──────▶│   FastAPI webhook (api/)     │
 Voice note      │                              │
 Bill photo ────▶│   Speech/Vision adapters     │
                 └──────────────┬───────────────┘
                                │
                 ┌──────────────▼───────────────┐
                 │      Agent Orchestrator       │
                 │   (app/agents — LangGraph)    │
                 │  ┌───────────┐ ┌────────────┐ │
                 │  │ B2B graph │ │ B2C graph  │ │
                 │  └─────┬─────┘ └──────┬─────┘ │
                 └────────┼──────────────┼────────┘
                          │              │
              ┌───────────▼───┐   ┌──────▼────────┐
              │ Guardrail      │   │ Order/Payment │
              │ validator      │   │ services      │
              │ (deterministic)│   │               │
              └───────┬────────┘   └──────┬────────┘
                      │                   │
              ┌───────▼───────────────────▼────────┐
              │     Shared Ledger (Postgres)        │
              └───────┬──────────────┬──────────────┘
                      │              │
              ┌───────▼──────┐ ┌─────▼──────────┐
              │ GST / e-way  │ │  OCEN loan      │
              │ bill adapter │ │  packet adapter │
              └──────────────┘ └─────────────────┘
```

## Repo layout

```
app/
  main.py                 FastAPI app entrypoint
  config.py               Settings (env-driven)
  db/                      SQLAlchemy models + session
  agents/                  LangGraph state machines + guardrail validator
  integrations/            External adapters (speech, vision, GST, OCEN, WhatsApp, UPI)
  services/                Core business logic (accounting, credit, orders, collections)
  api/                     HTTP routes + webhooks
tests/                     Pytest suite (business logic + guardrails)
infra/k8s/                 Kubernetes manifests
infra/github/workflows/    CI pipeline
docs/                      Architecture, API reference, compliance checklist
```

## Running it locally

```bash
cp .env.example .env            # fill in what you have; stubs work with placeholders
docker compose up --build       # api + postgres
# API docs at http://localhost:8000/docs
pytest tests/ -v                # 65 tests, no external creds needed
```

## Running it at ₹0 — the zero-cost stack

Every capability below runs on documented free tiers (verified limits and
honest caveats in `docs/ZERO_COST_BLUEPRINT.md`). With no keys at all the app
still boots and all 65 tests pass — adapters fail loud only when actually
called:

| Capability | ₹0 way | Env vars / notes |
|---|---|---|
| Handwritten-bill OCR + transcript structuring | **Groq free tier** (`integrations/vision.py`, `integrations/llm_text.py`) — no card; falls back to Gemini, then local Ollama | `GROQ_API_KEY` from [console.groq.com](https://console.groq.com) |
| Vernacular ASR | **Vosk self-hosted server** (`integrations/speech.py`) — Apache-2.0 models, no per-minute cost | `VOSK_ENDPOINT` (tiny HTTP wrapper around Vosk) |
| Voice notes & bill photos | **Cloudflare R2 10 GB free tier** (`integrations/media.py`), local filesystem by default in dev | `MEDIA_BACKEND=r2` + `R2_*` vars (needs `pip install boto3`) |
| Messaging (pilot channel) | **Telegram bot — free, no per-message cost** (`integrations/telegram.py`, webhook at `/webhooks/telegram`) | `TELEGRAM_BOT_TOKEN` + `TELEGRAM_SECRET_TOKEN` |
| Messaging (later channel) | WhatsApp Cloud API (`integrations/whatsapp.py`) — free inside the 24h window; template messages outside it cost ~₹0.115/utility | `WHATSAPP_*` vars |
| Money correctness | Integer-paise math (`app/utils/money.py`) + `Numeric(14,2)` columns; migration `0002_money_numeric_and_audit_events.py` | automatic — no keys |
| Audit trail | Immutable `audit_events` table (`app/services/audit.py`), wired into ledger posts, approval decisions and payment-status changes | automatic |
| PII discipline | Phone numbers masked to `[PHONE_n]` tokens before any hosted-LLM call (`services/text_structuring.py`) — Gemini/Groq free-tier terms never see raw personal data | automatic |
| Hosting / CI / DB / backups | Oracle Always-Free VM or Render Free · GitHub Actions · Neon free Postgres · nightly `pg_dump` → R2 | see blueprint |

## What you plug in for a real deployment

| Capability | Adapter | You need |
|---|---|---|
| Vernacular speech-to-text | `integrations/speech.py` | Bhashini API key, or Indic-Whisper self-hosted endpoint |
| Handwritten bill OCR | `integrations/vision.py` | Free Gemini API key ([aistudio.google.com](https://aistudio.google.com/apikey), no card), or local Ollama — no key at all |
| WhatsApp messaging | `integrations/whatsapp.py` | Meta WhatsApp Business Cloud API token + verified number |
| UPI collection | `integrations/upi.py` | A licensed PSP/PA (UPI links only; settlement needs a bank/PSP tie-up) |
| GST e-way bill | `integrations/gst.py` | GSTN API sandbox/prod credentials via a licensed GSP |
| Credit/OCEN | `integrations/ocen.py` | Empanelment as an LSP/LA on an OCEN-compliant network |

Each adapter raises a clear `NotConfiguredError` until its env vars are set, so
the system fails loudly instead of silently mocking financial data in
production.
