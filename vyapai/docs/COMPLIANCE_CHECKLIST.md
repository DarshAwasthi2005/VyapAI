# Compliance Checklist

This system touches tax filings, credit data, and payment collection for
real merchants. None of the items below are optional extras — they're
what "industry ready" actually requires beyond working code. This is not
legal advice; confirm specifics with your own counsel/compliance team.

## Before processing real GST data

- [ ] Registered with a licensed **GST Suvidha Provider (GSP)** — you
      cannot call GSTN's e-way bill / GST filing APIs directly as an
      unregistered application.
- [ ] `MAX_AUTO_APPROVE_AMOUNT_INR` and the `requires_human_approval_for_action`
      always-gated list reviewed and signed off by whoever is accountable
      for the merchant's tax filings — this code defaults to
      **human approval required for every e-way bill and GST filing**,
      full stop. Don't remove that gate to "streamline" the flow.
- [ ] GST rate/HSN-code mapping comes from a maintained rate master, not a
      hardcoded table — rates change by government notification.
- [ ] Retention policy for bill photos/voice notes containing merchant
      financial data — how long they're kept, where, and who can access
      them (relevant for India's Digital Personal Data Protection (DPDP)
      Act, 2023).

## Before submitting real OCEN loan applications

- [ ] Formal empanelment as a **Loan Service Provider (LSP)** with an OCEN
      network operator (e.g. Setu, Yubi, Sahamati-aligned networks).
      `integrations/ocen.py` assumes this exists — it will not work
      without it, by design (it raises `NotConfiguredError`).
- [ ] Legal basis / merchant consent captured for sharing their cash-flow
      data with lenders — OCEN packets contain sensitive financial data.
- [ ] `requires_human_approval_for_action("loan_application")` stays
      `True` — never auto-submit a merchant's loan application.
- [ ] A designated human reviews `loan_readiness_score()` flags before
      submission, not just a merchant tapping "confirm" on a WhatsApp
      message with no visibility into the underlying numbers.

## Before going live on WhatsApp

- [ ] Meta **WhatsApp Business Platform** approval, a verified business,
      and message-template approval for any templated messages (payment
      reminders sent outside the 24-hour customer-service window need an
      approved template, not free-form text — check Meta's current
      messaging policy, since this changes).
- [ ] Webhook signature verification (`integrations/whatsapp.verify_signature`)
      actually wired to a real `WHATSAPP_APP_SECRET` and enforced — the
      scaffold's webhook handler currently *skips* the check if the secret
      isn't set; make sure it's set before deploying.
- [ ] A documented escalation tone/policy for payment reminders that your
      legal team has reviewed — `services/collections.py`'s message
      templates are polite by design; don't let a later edit turn them
      into anything that could read as harassment. India's payment
      collection practices are subject to RBI fair-practice expectations
      for lenders/BNPL-adjacent flows if this ever extends to credit
      collection, not just trade receivables.

## Before handling real UPI payments

- [ ] A tie-up with a licensed **Payment Aggregator/PSP** (Razorpay,
      Cashfree, PayU, etc.) for actual settlement and payment-status
      webhooks — `integrations/upi.py` generates valid deep links with no
      API needed, but confirming a payment actually landed requires this.
- [ ] PCI-DSS scope review with your PSP — even though this system never
      touches card numbers or UPI PINs directly, reconciliation data
      (transaction refs, amounts) still needs secure handling.
- [ ] Refund/dispute flow — not in this scaffold at all. Add before
      accepting real payments.

## General

- [ ] Secrets management: `.env` files and the example Kubernetes
      `secret.yaml.example` are for structure, not production secret
      storage. Use a real secrets manager.
- [ ] Audit logging: every `ApprovalRequest` decision, every ledger entry's
      `source` field, and every OCEN/GST submission should be immutable
      and attributable to a specific human or system actor for at least
      as long as GST/financial record-keeping requirements demand.
- [ ] Data residency: confirm where merchant financial data is
      stored/processed against DPDP Act requirements if you're using
      cloud infrastructure outside India for any part of the stack.
- [ ] Independent security review before handling real money — this
      scaffold has not been penetration-tested.
