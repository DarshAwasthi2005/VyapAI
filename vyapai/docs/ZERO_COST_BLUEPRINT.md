# VyapAI — Zero-Cost Blueprint (Industry Grade at ₹0)

How to improve this project — including data storage — with **every external
service on a free tier**, so the whole system can be built and run without
spending money. Written against the actual code in [../app](../app), with
late-2026 limits verified on the web (sources at the bottom).

> **Honest upfront answer:** 100%-free is fully achievable for the *build +
> pilot* stage (~100s of merchants), and this document designs for exactly
> that. Three things cannot stay ₹0 forever and are flagged as such: (1)
> WhatsApp template messages outside promotion windows, (2) real money
> settlement through an RBI-authorized PSP, (3) a privacy-safe LLM tier for
> merchant financial data once you pass demo volumes. Everything else has a
> genuine free path.

---

## 1. Data storage improvements (your main question)

### 1a. What's wrong today
- Postgres lives in a Docker volume — fine locally, but **no backups, no
  durability**, data dies with the machine.
- Merchant **voice notes and bill photos are never stored** — they flow through
  memory and vanish. You can't audit, re-extract, or debug bad extractions
  later. This is the single biggest data gap.
- All amounts are **floats** (`Float` columns) — a ledger must be paise-exact.

### 1b. The free-tier target architecture

| Concern | Free choice | Free limits | Why this one |
|---|---|---|---|
| **Metadata / ledger DB** | **Neon Postgres** (free plan) | 1 GB storage/project, 100 CU-hrs/mo, scales to zero after 5 min idle, 5 GB transfer | Real Postgres (keeps `pg_trgm` fuzzy alias matching, Alembic migrations, and the existing SQLAlchemy unchanged); serverless; no card |
| **Multimedia (voice notes, bill photos)** | **Cloudflare R2** (free plan) | 10 GB storage, 10M Class-B ops/mo, **zero egress fees** | Object storage the code is currently missing entirely; no egress bill when serving images back to the LLM |
| **Cache / rate-limit / queue** | **Upstash Redis** (free plan) | 256 MB, 500k commands/mo | Only if needed; the reminder cron can run without it |
| **Cron (hourly reminders, daily summaries)** | **GitHub Actions scheduled workflows** | 2,000 free minutes/mo on private repos | Replaces the k8s CronJob for a $0 deployment — under a minute of runner time/hour ≈ 720 min/mo, inside the free allowance |
| **Backups (the "industry-level" bit)** | **`pg_dump` nightly via GitHub Actions → R2** | Free | A fintech ledger without backups is not industry-grade; this one costs nothing and is testable |

Note what is deliberately *not* chosen: Render/Koyeb free Postgres (expires in
30 days), Supabase free DB (500 MB cap + can pause after 7 days of inactivity),
Fly.io (no free tier for new accounts).

### 1c. Schema/code changes to make it real

1. **Store the input media.** In [routes_b2b.py](../app/api/routes_b2b.py),
   after reading `audio_bytes`/`image_bytes`, upload to R2 under
   `merchants/{merchant_id}/{yyyy}/{mm}/{uuid}.{ext}` and save
   `media_object_key` on a new `ledger_events` (or `entries`) row. Post   processing, guardrails "needs_human_approval" can then re-render the
   extraction for review.
 Add `MEDIA_STORE` adapter to `integrations/`
   following the existing fail-loud pattern (`NotConfiguredError` if creds
   absent, falls back to "unusable media discarded after processing" — in dev
   with no R2 creds, keep bytes in memory only).
2. **Money as paise.** New `Money` dataclass (int paise) +
   `Numeric(14,2)` columns replacing every `Float` money column. Write this
   migration first — changing money semantics later is painful.
3. **Timezone.** Replace `datetime.utcnow()` (deprecated and wrong for an
   Indian shop's "today") with timezone-aware `Asia/Kolkata` timestamps.
4. **A structured audit trail.** You already have `ApprovalRequest`. Add an
   immutable append-only `audit_events` table (actor, action, object,
   before/after). An auditor (or investor) asks "show me who changed what" —
   a khata app cannot answer this; it's your differentiator and it's free.
5. **Don't PII-leak into free-model APIs.** Gemini's *free tier trains on your
   data* and its terms explicitly say don't submit personal information
   ([terms](https://ai.google.dev/gemini-api/terms)). So: pseudonymize
   customer names/phones before any hosted-model call (mask tokens like
   `[CUST_1]`, re-hydrate after), send only the minimum fields, and keep raw
   audio/images on R2 with short-lived signed URLs. For a merchant fintech
   this isn't gold-plating — it's what makes the "free" path legally sane
   under DPDP.

## 2. Compute/APIs — the ₹0 stack

| Service needed | Free way, and what it costs you in sweat |
|---|---|
| **App hosting** (FastAPI) | **Oracle Cloud Always Free** Arm VM (4 OCPU / 24 GB RAM, 200 GB disk — genuinely free, always-on) is the best free host; or **Render Free** (512 MB, sleeps after 15-min idle, ~60s cold wake) for zero-ops; **Vercel serverless** as an option for request/response endpoints. Pick Oracle if you can babysit a VM; the repo already has Docker/k8s files that deploy cleanly. |
| **LLM / OCR (bill photos, text structuring)** | **Groq free tier** (`llama-3.2-11b-vision` / `openai/gpt-oss-20b`) — vision + text, 30 RPM/1,000 RPD free with *stated* no-default-retention. Keep the existing **Gemini free-tier adapter** for non-sensitive demo data only (its free tier trains on inputs; never send real vendor bills or merchant PII). Both plug into [vision.py](../app/integrations/vision.py)'s existing backend-switch pattern. Writes from local `ollama` remain a $0 offline fallback. |
| **Vernacular ASR (Hindi/Tamil/Gujarati)** | **IndicWhisper self-hosted on your Oracle VM** (fastest CPU route; AI4Bharat license must be checked per-checkpoint) or **Vosk small models** (42 MB Hindi / 100 MB Gujarati, Apache 2.0, ~300 MB RAM — real-time on CPU, but **no Tamil**). **Sarvam** gives ₹100 one-time free credits (≈ ₹30/hr STT) — treat as a trial not a budget. **Bhashini** is free *only for PoC* — its own docs say production/commercial use requires their paid plan. Put a quality-based router in `speech.py`: ingest Vosk locally (JSON), escalate unclear ASR to a hosted LLM turn as a free-tier fallback, and never silently accept bad audio for money entry. |
| **WhatsApp in-app messaging** | WhatsApp Cloud API setup is free, **replies/sends within the 24-h customer-service window are free today**, *but* as of Oct 1, 2026 Meta started metering even service/window messages, and **utility templates outside the window cost ~₹0.115/message** in India. So: design the web app to use agent replies inside the window (free-ish), and make reminders/dunning a template flow whose token cost is a known per-unit marginal cost — not “free.” |
| **Buyer-sent ordering & notifications, fallback channel** | **Telegram Bot — completely free, no per-message cost, unlimited practical volume** for order-taking, confirmations, khata balances, reports. Ship the *same* adapter interface as WhatsApp (`integrations/telegram.py` mirrors `whatsapp.py`) so the messaging channel is pluggable: Telegram becomes the **zero-cost demo/pilot channel**, WhatsApp the later paid channel. This one change keeps the whole product truly runnable at ₹0. |
| **Email (report PDFs, CA exports)** | **Brevo** free 300 emails/day (~9k/mo), or **Resend** 100/day. Later used for monthly GST statements and lender packets. |
| **Auth** | **Supabase Auth free 50k MAU** for the PWA, if you go Supabase; otherwise an API-key or phone-OTP via the messaging adapters (Telegram DM codes = free). Firebase Auth is free for non-SMS flows; **phone-OTP SMS is paid** with every provider — use Telegram/email OTP instead, or defer OTP to the pilot phase. |
| **Payments** | Upi **deep links** are already free and coded ([upi.py](../app/integrations/upi.py)). Knowing *whether it was paid* requires a PSP webhook: Razorpay/Cashfree/PayU all have **zero setup fee, pay only per-transaction %** — so piloting is free until real payments flow, then it's % fees only (a cost of goods, not a fixed bill). |
| **Monitoring** | **Sentry Developer (5k errors/mo)** or Grafana Cloud free — plus open-source structured logging. |
| **CI** | GitHub Actions free (2,000 min/mo private) — schedule + test + build; scheduled cron role above. |

## 3. What genuinely cannot be ₹0 forever (don't kid yourself)

1. **WhatsApp reminder templates outside the 24h/72h windows** — a real cost
   every delivered message, or design flow so merchants are inside the window
   (they usually are, since the buyer is *replying*).
2. **A compliant ASR/LLM tier at volume for financial data** — Gemini-free and
   Groq-free have caps; Groq's is generous (1,000 RPD) but not contractual
   throughput. Past pilot volume you'll need a paid tier or dedicated hosting.
3. **OTP SMS** — every SMS gateway charges. Avoidable via Telegram/WhatsApp
   OTP, which are free.
4. **Real production SLAs/backups** — free Neon + free R2 storage have no
   bounded-RTO guarantee. The GitHub Actions pg_dump → R2 backup closes most
   of this gap for free, but it's chicken-and-egg with real SLAs.

## 4. Ranked list of concrete improvements (what to build next)

1. **Media storage adapter + storing voice/photos on R2** (new module
   `app/integrations/media.py`) — the missing piece that blocks auditability.
2. **Money-as-paise + `Numeric(14,2)` migration + IST timestamps.**
3. **`audit_events` table + events written from every money-touching code
   path.**
4. **PII-masking layer** wrapping every hosted-model call.
5. **Telegram adapter** (`integrations/telegram.py`) as the free default
   channel, architected identically to WhatsApp so both coexist.
6. **Speech router**: Vosk-small (or IndicWhisper) locally on the Oracle VM →
   unclear audio → ask the merchant to re-speak/type (never hallucinated
   entries.
7. **Groq adapter** alongside Gemini in `vision.py` + a new
   `text_structuring.py` (currently missing in the voice path — noted in
   DEEP_RESEARCH).
8. **Nightly `pg_dump` → R2 from GitHub Actions** + a documented restore
   drill. *A backup you've never restored is a rumor.*
9. **Sentry + structured request-ID logging.**
10. **PWA-only UI to stay serverless-friendly:** plus the confirmation loop
    UX (R1 in PRODUCT_SPEC_V2) so low-confidence inputs become "did you
    mean X?" instead of 500/unclean data.

---

## 5. Verified free-tier sources (late 2026)

- Neon plans: <https://neon.com/pricing> and <https://neon.com/docs/introduction/plans>
- Supabase pricing: <https://supabase.com/pricing>
- Cloudflare R2 pricing: <https://developers.cloudflare.com/r2/pricing/>
- Backblaze B2: <https://www.backblaze.com/cloud-storage/pricing>
- Upstash Redis: <https://upstash.com/pricing/redis> · QStash: <https://upstash.com/pricing/qstash>
- Oracle Always Free: <https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm>
- Render Free: <https://render.com/docs/free>
- Koyeb instances: <https://www.koyeb.com/docs/reference/instances>
- Railway free trial: <https://docs.railway.com/pricing/free-trial>
- Vercel Python functions: <https://vercel.com/docs/functions/runtimes/python>
- Fly.io pricing: <https://docs.fly.io/about/pricing>
- GitHub Actions billing: <https://docs.github.com/en/billing/concepts/product-billing/github-actions>
- Sentry pricing: <https://sentry.io/pricing/> · Grafana: <https://grafana.com/pricing/>
- Gemini free limits/terms: <https://ai.google.dev/gemini-api/docs/pricing> · <https://ai.google.dev/gemini-api/terms> · <https://ai.google.dev/gemini-api/docs/rate-limits>
- Groq rate limits/no-retention: <https://console.groq.com/docs/rate-limits> · <https://console.groq.com/docs/your-data>
- OpenRouter free models: <https://openrouter.ai/pricing> · <https://openrouter.ai/docs/api_reference/limits>
- Mistral free mode: <https://docs.mistral.ai/admin/billing-usage/usage-limits>
- Bhashini (PoC-only caveat): <https://dibd-bhashini.gitbook.io/bhashini-apis> · <https://bhashini.gov.in/udbhav>
- Vosk models (Hindi Gupta/Marathi sizes/RAM): <https://alphacephei.com/vosk/models>
- Sarvam credits/free: <https://docs.sarvam.ai/api/getting-started/ratelimits> · <https://docs.sarvam.ai/api/getting-started/pricing>
- AI4Bharat ASR: <https://ai4bharat.iitm.ac.in/areas/asr/>
- Google Cloud STT 60-min free: <https://docs.cloud.google.com/speech-to-text/docs/v1/speech-to-text-requests>
- WhatsApp pricing + Oct 2026 change: <https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing>
- Twilio 1,000 service msgs/mo notice: <https://help.twilio.com/articles/53100480177819>
- Telegram bot limits: <https://core.telegram.org/bots/faq>
- Resend pricing: <https://resend.com/pricing/> · Brevo: <https://www.brevo.com/pricing/>
- Supabase Auth / Firebase / Auth0: <https://supabase.com/pricing> · <https://firebase.google.com/pricing> · <https://auth0.com/pricing/>
