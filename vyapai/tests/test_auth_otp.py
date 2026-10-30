"""Unified OTP login: phone OR email → 6-digit code → dashboard (no PIN).

Covers signup with either identity, the /b2b/auth/otp/request + /otp/verify
endpoints, attempt-burning on wrong codes, and the 404/422/409 edges.
"""
from __future__ import annotations

import os

# Must be set before app.db.session is imported (engine is module-level).
os.environ.setdefault("DATABASE_URL", "sqlite:///./vyapai-test-otp.db")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, Merchant
from app.db.session import get_db
from app.api import routes_auth, routes_b2b
from tests.conftest import patch_settings


@pytest.fixture()
def db():
    tmp = os.path.join(os.path.dirname(__file__), ".tmp-otp.db")
    # A crashed prior run leaves the file behind with an out-of-date schema
    # (create_all never alters existing tables) — start fresh instead.
    try:
        os.remove(tmp)
    except OSError:
        pass
    engine = create_engine(f"sqlite:///{tmp}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        try:
            os.remove(tmp)
        except OSError:
            pass


@pytest.fixture()
def client(db):
    """TestClient over the auth + merchant-registration routers, with the
    test session wired in for get_db (commit-on-success semantics)."""
    probe = FastAPI()
    probe.include_router(routes_auth.router)
    probe.include_router(routes_b2b.router)

    def override_get_db():
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise

    probe.dependency_overrides[get_db] = override_get_db
    try:
        yield TestClient(probe)
    finally:
        probe.dependency_overrides.clear()


def _register(client, **kw):
    payload = {"business_name": "OTP Test Stores", "pin": "1234", "preferred_language": "hi"}
    payload.update(kw)
    return client.post("/b2b/merchants", json=payload)


# ---- signup: phone OR email either suffices -----------------------------------

def test_signup_by_email_only(client, db):
    r = _register(client, email="Sharma@Test.IN")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["email"] == "sharma@test.in"          # normalised to lower
    assert body["whatsapp_number"] is None
    # unique identity enforced
    r2 = _register(client, email="sharma@test.in")
    assert r2.status_code == 409, r2.text


def test_signup_by_phone_only(client, db):
    r = _register(client, whatsapp_number="9812345678")
    assert r.status_code == 200, r.text
    assert r.json()["whatsapp_number"] == "9812345678"


def test_signup_requires_an_identity(client):
    r = _register(client, gstin="22AAAAA0000A1Z5")
    assert r.status_code == 422, r.text
    assert "phone number or an email" in r.json()["detail"]


# ---- request OTP on the chosen identity ----------------------------------------

def _request_otp(client, value, channel):
    return client.post("/b2b/auth/otp/request", json={"channel": channel, "value": value})


def test_email_otp_request_echoes_code_in_dev(client, db):
    _register(client, email="login@test.in", pin="9999")
    r = _request_otp(client, "LOGIN@test.in", "email")   # case-insensitive lookup
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["delivery"] == "dev_echo"                # SMTP not configured in tests
    assert body["sent_to"] == "login@test.in"
    assert len(body["otp"]) == 6                         # dev_echo convenience
    return body


# ---- dashboard: change preferred language -----------------------------------


def _signed_in_token(client, whatsapp_number):
    """Full dev-flow sign-in: request the OTP (echoed in dev) and verify it."""
    req = _request_otp(client, whatsapp_number, "phone").json()
    r = client.post("/b2b/auth/otp/verify", json={"challenge_id": req["challenge_id"], "code": req["otp"]})
    return r.json()["token"]


def test_change_language_persists_and_normalizes(client, db):
    _register(client, whatsapp_number="9812300001", preferred_language="hi")
    token = _signed_in_token(client, "9812300001")
    r = client.patch("/b2b/auth/me/language", json={"preferred_language": "TA"},
                     headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert r.json()["preferred_language"] == "ta"          # normalized to lowercase code
    # It really saved: /me reports the new language.
    assert client.get("/b2b/auth/me", headers={"Authorization": f"Bearer {token}"}).json()["preferred_language"] == "ta"


def test_change_language_rejects_unknown_code(client, db):
    _register(client, whatsapp_number="9812300037")
    token = _signed_in_token(client, "9812300037")
    r = client.patch("/b2b/auth/me/language", json={"preferred_language": "xx"},
                     headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 422, r.text
    assert "Unsupported language" in r.json()["detail"]
    # The shop's language was NOT touched by the failed attempt.
    assert client.get("/b2b/auth/me", headers={"Authorization": f"Bearer {token}"}).json()["preferred_language"] == "hi"


def test_change_language_requires_a_session(client, db):
    _register(client, whatsapp_number="9812300091")
    r = client.patch("/b2b/auth/me/language", json={"preferred_language": "ta"})
    assert r.status_code == 401, r.text
    r2 = client.patch("/b2b/auth/me/language", json={"preferred_language": "ta"},
                      headers={"Authorization": "Bearer dead-token"})
    assert r2.status_code == 401, r2.text


def test_real_email_delivery_when_smtp_configured(client, db, monkeypatch):
    _register(client, email="real@test.in", pin="5555")
    sent = {}

    from email.message import EmailMessage

    class FakeSMTP:
        def __init__(self, *a, **k): pass

        def __enter__(self): return self

        def __exit__(self, *exc): return False

        def ehlo(self): pass

        def starttls(self, **k): pass

        def login(self, u, p): pass

        def send_message(self, msg: EmailMessage):
            sent["to"] = msg["To"]
            sent["subject"] = msg["Subject"]
            sent["body"] = msg.get_content()

    import smtplib as _smtplib
    monkeypatch.setattr(_smtplib, "SMTP", FakeSMTP)
    from datetime import datetime, timedelta
    from app.db.models import LoginChallenge
    from app.services import auth as authsvc

    with patch_settings(smtp_host="smtp.test", smtp_port=587, smtp_from="no-reply@vyapai.test"):
        r = _request_otp(client, "real@test.in", "email")
    assert r.status_code == 200, r.text
    assert r.json()["delivery"] == "email"
    assert "otp" not in r.json()  # real send ⇒ never echoed
    assert sent["subject"] == "Your VyapAI login code"
    # the delivered code verifies (sha256 round-trip through the challenge)
    challenge = db.query(LoginChallenge).where(
        LoginChallenge.merchant_id == db.query(Merchant).filter_by(email="real@test.in").one().id
    ).order_by(LoginChallenge.created_at.desc()).first()
    # the OTP in the body must round-trip through the challenge's hash
    import re as _re
    otp_in_body = _re.search(r"\b(\d{6})\b", sent["body"]).group(1)
    assert authsvc.check_secret(challenge, otp_in_body)


def test_real_sms_delivery_when_twilio_configured(client, db, monkeypatch):
    _register(client, whatsapp_number="9840022222", pin="4455")
    sent = {}

    def fake_sms(to_e164, otp):
        sent["to"] = to_e164
        sent["otp"] = otp

    from app.integrations import sms as sms_mod
    monkeypatch.setattr(sms_mod, "send_otp_sms", fake_sms)

    with patch_settings(twilio_account_sid="ACtest", twilio_auth_token="tok",
                        twilio_phone_number="+15550001111"):
        r = _request_otp(client, "9840022222", "phone")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["delivery"] == "sms"
    assert body["sent_to"] == "9840022222"
    assert "otp" not in body  # real send ⇒ never echoed
    assert sent["to"] == "+919840022222"  # stored nationally → E.164 for Twilio
    # the code that "went out" verifies against the persisted challenge
    from datetime import datetime
    from app.db.models import LoginChallenge
    from app.services import auth as authsvc
    m = db.query(Merchant).filter_by(whatsapp_number="9840022222").one()
    challenge = db.query(LoginChallenge).where(
        LoginChallenge.merchant_id == m.id
    ).order_by(LoginChallenge.created_at.desc()).first()
    assert authsvc.check_secret(challenge, sent["otp"])


# ---- verify with the code alone ------------------------------------------------

def _verify(client, challenge_id, code):
    return client.post("/b2b/auth/otp/verify", json={"challenge_id": challenge_id, "code": code})


def test_otp_verify_opens_dashboard_session(client, db):
    req = test_email_otp_request_echoes_code_in_dev(client, db)
    r = _verify(client, req["challenge_id"], req["otp"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["token"]) > 30
    assert body["delivery"] == "dev_echo"
    # the session actually resolves to the shop
    me = client.get("/b2b/auth/me", headers={"Authorization": f"Bearer {body['token']}"})
    assert me.status_code == 200 and me.json()["email"] == "login@test.in"
    # single use: replay burns nothing but is refused
    r2 = _verify(client, req["challenge_id"], req["otp"])
    assert r2.status_code == 401


def test_wrong_code_burns_attempt_then_correct_still_works(client, db):
    req = test_email_otp_request_echoes_code_in_dev(client, db)
    r = _verify(client, req["challenge_id"], "000000")
    assert r.status_code == 401
    assert "Wrong code" in r.json()["detail"]
    r = _verify(client, req["challenge_id"], req["otp"])
    assert r.status_code == 200, r.text


def test_five_wrong_attempts_kill_the_challenge(client, db):
    req = test_email_otp_request_echoes_code_in_dev(client, db)
    for _ in range(5):
        assert _verify(client, req["challenge_id"], "000000").status_code == 401
    r = _verify(client, req["challenge_id"], req["otp"])
    assert r.status_code == 401
    assert "Too many wrong attempts" in r.json()["detail"]
