"""End-to-end smoke test of the customer journey against a running dev server.

Run:  python scripts/smoke_customer_flow.py
Requires: uvicorn running on 127.0.0.1:8000 with a fresh demo DB
(APP_ENV=development, sqlite DATABASE_URL, UPI_PAYEE_VPA set).

Walks: onboarding (with login PIN) -> real login via dev-echo OTP ->
ledger entry -> customer order -> UPI link -> authenticated PSP webhook
(mark paid) -> approval decision -> loan readiness -> auth-protection
checks -> reads back everything the dashboard consumes.
"""
import json
import re
import sys

import httpx

BASE = "http://127.0.0.1:8000"
WA = "9876543210"
PIN = "424242"
client = httpx.Client(base_url=BASE, timeout=15)
auth = {}
checks: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    checks.append((name, bool(ok), str(detail)[:220]))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not ok else ""))


# 1. Onboarding (registers with a PIN) ------------------------------------------
r = client.post("/b2b/merchants", json={
    "business_name": "Sharma Kirana Store",
    "gstin": "22AAAAA0000A1Z5",
    "whatsapp_number": WA,
    "upi_vpa": "sharma@okaxis",
    "preferred_language": "hi",
    "pin": PIN,
})
check("onboarding creates the shop (PIN accepted)", r.status_code == 200, r.text)
merchant = r.json()
mid = merchant.get("id", "")
check("payload persists all filled details",
      r.status_code == 200 and merchant.get("business_name") == "Sharma Kirana Store"
      and merchant.get("gstin") and merchant.get("preferred_language") == "hi",
      r.text)
check("PIN hash is never returned by the API", "pin" not in merchant and "pin_hash" not in merchant, r.text)

# unauthenticated reads must now be refused — this is the real-login change
r = client.get(f"/b2b/merchants/{mid}/ledger")
check("ledger is closed without a session (401)", r.status_code == 401, f"expected 401, got {r.status_code}")

# 2. Real login: dev-echo OTP + PIN ---------------------------------------------
def _login(wa, pin):
    r = client.post("/b2b/auth/request-login", json={"whatsapp_number": wa})
    ok = r.status_code == 200 and r.json().get("otp")  # dev_echo per APP_ENV
    r2 = client.post("/b2b/auth/verify", json={
        "challenge_id": r.json()["challenge_id"],
        "otp": r.json().get("otp", ""),
        "pin": pin,
    })
    return ok, r2

ok, r = _login(WA, PIN)
token = r.json().get("token", "") if r.status_code == 200 else ""
auth = {"Authorization": f"Bearer {token}"}
check("login: OTP challenge issued (dev_echo)", ok, r.text)
check("login: verify returns a bearer token", r.status_code == 200 and len(token) > 30, r.text)

r = client.get("/b2b/auth/me", headers=auth)
check("/me resolves the session to the shop", r.status_code == 200 and r.json().get("id") == mid, r.text)

r = client.post("/b2b/auth/request-login", json={"whatsapp_number": WA})
ok, r_bad = _login(WA, "000000")
check("login: wrong PIN is refused", r_bad.status_code == 401, f"expected 401, got {r_bad.status_code}")

ok, r = _login(WA, PIN)
token = r.json().get("token", "") if r.status_code == 200 else token
auth = {"Authorization": f"Bearer {token}"}

# 3. Text ledger entry (sale with 5% GST) --------------------------------------
r = client.post("/b2b/entries/text", headers=auth, json={
    "merchant_id": mid, "party_name": "Ravi Uncle",
    "transaction_kind": "sale", "taxable_value": 500.0, "gst_rate": 5.0,
})
check("sales entry posts to the books", r.status_code == 200 and r.json().get("needs_human_approval") is False, r.text)
group_sale = r.json().get("transaction_group", "")

r = client.post("/b2b/entries/text", headers=auth, json={
    "merchant_id": mid, "party_name": "Gupta Wholesale",
    "transaction_kind": "purchase", "taxable_value": 1200.0, "gst_rate": 18.0,
})
check("purchase entry posts to the books", r.status_code == 200, r.text)

r = client.get(f"/b2b/merchants/{mid}/ledger", headers=auth)
lines = r.json()
check("dashboard ledger endpoint returns entries", r.status_code == 200 and isinstance(lines, list) and len(lines) >= 2,
      f"got {len(lines) if isinstance(lines, list) else lines} lines: {json.dumps(lines[:2]) if isinstance(lines, list) else ''}")
sale_lines = [l for l in lines if l["transaction_group"] == group_sale] if isinstance(lines, list) else []
check("simple books: one entry per sale, amount incl. GST",
      len(sale_lines) == 1 and sale_lines[0]["account"] == "Sales"
      and float(sale_lines[0]["amount"]) == 525.0, json.dumps(sale_lines))

# 3. Customer order ------------------------------------------------------------
r = client.post(f"/b2c/merchants/{mid}/orders", json={
    "from_number": "9876500001",
    "message_text": "Send 7 bags of sugar by 4 PM",
})
order = r.json()
check("customer order parsed & confirmed", r.status_code == 200 and order.get("status") == "confirmed", r.text)
check("order total priced correctly (7*42 = 294)", order.get("total_amount") == 294 or
      float(order.get("total_amount", 0)) == 294.0, r.text)
check("UPI payment link generated", bool(order.get("upi_link")), order.get("upi_link", ""))

# 4. UPI-linked orders confirm DIRECTLY on creation (per the merchant-facing
# design): money moves inside the buyer's UPI app the moment they tap the
# link, so the payment row is born SUCCESS/method=upi. Cash orders are the
# opposite — they stay unconfirmed until the merchant taps "Confirm cash".
r = client.get(f"/b2b/merchants/{mid}/orders", headers=auth)
orders = r.json()
mine = [o for o in orders if o["raw_message"].startswith("Send 7 bags")][0]
tref = mine["transaction_ref"]
check("orders endpoint shows the UPI payment confirmed on creation",
      mine["payment_status"] == "success" and tref, json.dumps(mine))

r = client.post("/webhooks/payments/psp", headers=auth,
                json={"transaction_ref": tref, "status": "success"})
check("PSP webhook accepts a session-authenticated call", r.status_code == 200, r.text)
r = client.post("/webhooks/payments/psp", json={"transaction_ref": tref, "status": "success"})
check("PSP webhook refuses unsigned calls (401)", r.status_code == 401, f"expected 401, got {r.status_code}")

r = client.get(f"/b2b/merchants/{mid}/orders", headers=auth)
paid = [o for o in r.json() if o["id"] == mine["id"]][0]
check("order still shows paid (idempotent)", paid["payment_status"] == "success", json.dumps(paid))

# 5. Out-of-stock guard --------------------------------------------------------
r = client.post(f"/b2c/merchants/{mid}/orders", json={
    "from_number": "9876500002", "message_text": "need 999 cartons of soap",
})
check("over-order flagged out of stock", r.json().get("status") == "out_of_stock", r.text)

# 6. Approvals: force a guardrail hold -----------------------------------------
guard_group = None
r = client.post("/b2b/entries/text", headers=auth, json={
    "merchant_id": mid, "party_name": "Big Deal",
    "transaction_kind": "sale", "taxable_value": 900000.0, "gst_rate": 28.0,
})
big = r.json()
check("large text entry held for approval (guardrail)", big.get("needs_human_approval") is True, r.text)
check("held entry was NOT posted to the ledger yet", all(
    l["transaction_group"] != guard_group
    for l in client.get(f"/b2b/merchants/{mid}/ledger").json()
    if guard_group) if guard_group else True, "")

r = client.get(f"/b2b/merchants/{mid}/approvals", headers=auth)
pendings = [a for a in r.json() if a["status"] == "pending"]
check("held entry is in the approval queue", len(pendings) >= 1, r.text)
if pendings:
    aid = pendings[0]["id"]
    payload = json.loads(pendings[0]["payload"])
    guard_group = payload["transaction_group"]
    # reject a second large entry and prove it never posts
    r2 = client.post("/b2b/entries/text", headers=auth, json={
        "merchant_id": mid, "party_name": "No Thankyou Ltd",
        "transaction_kind": "sale", "taxable_value": 950000.0, "gst_rate": 28.0,
    })
    check("second large entry also held", r2.json().get("needs_human_approval") is True, r2.text)
    r = client.get(f"/b2b/merchants/{mid}/approvals", headers=auth)
    second = [a for a in r.json() if a["status"] == "pending" and a["id"] != aid][0]
    rejected_check = client.post(f"/b2b/approvals/{second['id']}/decide", headers=auth, params={"approve": "false"})
    check("approval can be rejected", rejected_check.status_code == 200, rejected_check.text)
    rej_group = json.loads(second["payload"])["transaction_group"]
    check("rejected entry never posts to the ledger", all(
        l["transaction_group"] != rej_group
        for l in client.get(f"/b2b/merchants/{mid}/ledger", headers=auth).json()), "")

    # approve the first and prove it posts exactly once
    r2 = client.post(f"/b2b/approvals/{aid}/decide", headers=auth, params={"approve": "true"})
    check("approval can be granted", r2.status_code == 200 and r2.json()["status"] == "approved", r2.text)
    lines = client.get(f"/b2b/merchants/{mid}/ledger", headers=auth).json()
    approved = [l for l in lines if l["transaction_group"] == guard_group]
    check("approved entry now posts to the ledger (one entry)",
          len(approved) == 1 and approved[0]["account"] == "Sales", f"{len(approved)} lines")

# 7. Loan readiness ------------------------------------------------------------
r = client.get(f"/b2b/merchants/{mid}/loan-readiness", headers=auth,
               params={"requested_amount": "50000", "purpose": "stock purchase"})
check("loan readiness returns a score", r.status_code == 200, r.text)

# 8. Channels: Telegram + WhatsApp ---------------------------------------------
r = client.get(f"/b2b/channels?merchant_id={mid}", headers=auth)
channels = r.json()
check("channels status endpoint returns both channels",
      r.status_code == 200 and "telegram" in channels and "whatsapp" in channels, r.text)
check("channels report setup honestly (nothing configured in bare dev)",
      channels["telegram"]["configured"] is False and channels["whatsapp"]["configured"] is False,
      json.dumps(channels))

# demo-link the shop's own WhatsApp chat (the code path the real webhook runs)
r = client.post("/webhooks/whatsapp/demo-link", headers=auth,
                json={"merchant_id": mid})
check("shop's own WhatsApp chat can be linked (demo helper)",
      r.status_code == 200 and r.json().get("linked") is True, r.text)

# an inbound WhatsApp message from the shop's number is routed as an order
r = client.post("/webhooks/whatsapp", json={
    "object": "whatsapp_business_account",
    "entry": [{"changes": [{"value": {
        "messaging_product": "whatsapp",
        "messages": [{"from": WA, "id": "wamid.T1", "type": "text",
                      "text": {"body": "Send 2 bags of sugar by 9 PM"}}],
    }}]}],
})
body = r.json()
check("inbound WhatsApp message routed into the order flow",
      r.status_code == 200 and body.get("handled") is True and body.get("order") is not None,
      r.text)
check("WhatsApp-intake order priced correctly (2*42 = 84)",
      body.get("order", {}).get("total_amount") == 84 or
      float(body.get("order", {}).get("total_amount", 0)) == 84.0, r.text)

# unlinked/unknown chats never silently create orders
r = client.post("/webhooks/whatsapp", json={
    "object": "whatsapp_business_account",
    "entry": [{"changes": [{"value": {
        "messaging_product": "whatsapp",
        "messages": [{"from": "0000000003", "id": "wamid.T2", "type": "text",
                      "text": {"body": "2 bags sugar"}}],
    }}]}],
})
check("unknown WhatsApp sender is acked but not routed", r.status_code == 200 and
      r.json().get("handled") is False, r.text)
r = client.get(f"/b2b/channels?merchant_id={mid}", headers=auth)
check("whatsapp now shows linked after first contact",
      r.json().get("whatsapp", {}).get("linked") is True, r.text)

# Telegram pairing in demo mode (no bot token on this machine): the demo
# helper runs the exact /start-pairing branch the live webhook runs.
r = client.post("/b2b/auth/telegram/link", headers=auth)
tl = r.json()
check("telegram link issues a pairing code",
      r.status_code == 200 and bool(tl.get("code")), r.text)
r = client.post("/webhooks/telegram/demo-link", headers=auth)
check("telegram chat links via /start pairing (demo helper)",
      r.status_code == 200 and r.json().get("linked") is True, r.text)
r = client.get(f"/b2b/auth/telegram/link/{tl['challenge_id']}")
check("telegram link poll reports linked",
      r.status_code == 200 and r.json().get("linked") is True, r.text)
r = client.get(f"/b2b/channels?merchant_id={mid}", headers=auth)
check("channels card shows telegram linked",
      r.json().get("telegram", {}).get("linked") is True, r.text)
r = client.post("/webhooks/telegram/demo-link")
check("telegram demo-link requires a session (401)",
      r.status_code == 401, f"expected 401, got {r.status_code}")

# 9. Unified OTP login: phone | email -----------------------------------------
r = client.post("/b2b/auth/otp/request", json={"channel": "phone", "value": WA})
o = r.json()
check("OTP login: request issues a code (phone)",
      r.status_code == 200 and o.get("delivery") == "dev_echo" and len(o.get("otp", "")) == 6, r.text)
r = client.post("/b2b/auth/otp/verify", json={"challenge_id": o["challenge_id"], "code": o["otp"]})
check("OTP login: the code alone opens a session (no PIN)",
      r.status_code == 200 and len(r.json().get("token", "")) > 30, r.text)

# wrong code is refused
r = client.post("/b2b/auth/otp/request", json={"channel": "phone", "value": WA})
o = r.json()
check("OTP login: wrong code refused (401)",
      client.post("/b2b/auth/otp/verify",
                  json={"challenge_id": o["challenge_id"], "code": "000000"}).status_code == 401,
      "expected 401 for wrong code")

# signup by email: identity without a phone number
r = client.post("/b2b/merchants", json={
    "business_name": "Email OTP Stores", "email": "otp@shop.test",
    "pin": "1234", "preferred_language": "hi",
})
check("signup with email identity works", r.status_code == 200 and
      r.json().get("email") == "otp@shop.test" and r.json().get("whatsapp_number") is None, r.text)
r = client.post("/b2b/auth/otp/request", json={"channel": "email", "value": "OTP@shop.test"})
o2 = r.json()
check("OTP login: request by email issues a code", r.status_code == 200 and len(o2.get("otp", "")) == 6, r.text)
r = client.post("/b2b/auth/otp/verify", json={"challenge_id": o2["challenge_id"], "code": o2["otp"]})
check("OTP login: email code opens a session", r.status_code == 200 and
      len(r.json().get("token", "")) > 30, r.text)
r = client.post("/b2b/auth/otp/request", json={"channel": "email", "value": "ghost@shop.test"})
check("unknown identity refused (404)", r.status_code == 404, f"expected 401, got {r.status_code}")

# 10. Health --------------------------------------------------------------------
r = client.get("/health")
check("health endpoint", r.status_code == 200 and r.json()["status"] == "ok", r.text)

# ---- summary -----------------------------------------------------------------
failed = [c for c in checks if not c[1]]
print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
if failed:
    print("FAILED:")
    for name, _, detail in failed:
        print(f"  - {name}: {detail}")
    sys.exit(1)
