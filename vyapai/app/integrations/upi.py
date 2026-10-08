"""
UPI payment link generation.

Building a `upi://pay` deep link is an open, documented standard (NPCI) and
needs no external API call — this is fully functional, not a stub. What
*does* need a real integration is (a) knowing when the payment actually
lands, which requires either a PSP webhook or polling a bank/PA API, and
(b) settlement into the merchant's account, which requires a licensed
Payment Aggregator/PSP tie-up. Both are marked below.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from urllib.parse import quote, urlencode

from app.config import settings
from app.integrations.errors import NotConfiguredError


@dataclass
class UpiLink:
    transaction_ref: str
    deep_link: str
    amount: float
    payee_vpa: str


def generate_payment_link(amount: float, note: str, payee_vpa: str | None = None,
                           payee_name: str | None = None) -> UpiLink:
    """Build a UPI intent deep link for a specific amount.

    Renders as a tappable "Pay via UPI" link/QR on WhatsApp or web. Any UPI
    app (GPay, PhonePe, Paytm, BHIM...) can open it.
    """
    vpa = payee_vpa or settings.upi_payee_vpa
    name = payee_name or settings.upi_payee_name
    if not vpa:
        raise NotConfiguredError("UPI", ["UPI_PAYEE_VPA (or pass payee_vpa explicitly)"])
    if amount <= 0:
        raise ValueError("amount must be positive")

    txn_ref = f"VYAPAI-{uuid.uuid4().hex[:12].upper()}"
    params = {
        "pa": vpa,                      # payee address
        "pn": name or "Merchant",       # payee name
        "am": f"{amount:.2f}",          # amount
        "cu": "INR",
        "tn": note[:50],                # transaction note, UPI caps this short
        "tr": txn_ref,                  # transaction reference, for reconciliation
    }
    deep_link = "upi://pay?" + urlencode(params, quote_via=quote)
    return UpiLink(transaction_ref=txn_ref, deep_link=deep_link, amount=amount, payee_vpa=vpa)


def check_payment_status(transaction_ref: str) -> str:
    """Returns 'pending' | 'success' | 'failed'.

    TODO(deploy): a UPI link alone tells you nothing about whether it was
    paid — you need one of:
      - Your PSP/PA's webhook (e.g. Razorpay, Cashfree, PayU) posting to
        app/api/webhooks.py on payment success, matched by `tr`.
      - Polling your PA's transaction-status API by transaction_ref.
    Wire the chosen path here; until then this always reports 'pending'.
    """
    return "pending"
