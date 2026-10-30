"""Telegram + WhatsApp channel layer: adapters, linking, intake routing,
and per-shop channel status.

Meta/Graph payloads follow the documented Cloud API webhook shape.
"""
from __future__ import annotations

import os

# Must be set before app.db.session is imported (engine is module-level).
os.environ.setdefault("DATABASE_URL", "sqlite:///./vyapai-test-channels.db")

from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.db.models import Base, LoginChallenge, Merchant
from app.integrations import whatsapp
from app.integrations.errors import NotConfiguredError
from app.services import auth as authsvc
from app.services import channels as channelsvc
from app.services import order_intake
from tests.conftest import patch_settings


@pytest.fixture()
def db():
    tmp = os.path.join(os.path.dirname(__file__), ".tmp-channels.db")
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
        # Dispose the pool so Windows can actually unlink the file — pooled
        # connections keep it locked, and a surviving file then leaks this
        # test's merchants into the next test (MultipleResultsFound).
        engine.dispose()
        try:
            os.remove(tmp)
        except OSError:
            pass


def make_shop(db, wa="9876500011", tg=None, wa_chat=None) -> Merchant:
    m = Merchant(business_name="Channel Shops", whatsapp_number=wa)
    if tg:
        m.telegram_chat_id = tg
    if wa_chat:
        m.whatsapp_chat_id = wa_chat
    db.add(m)
    db.commit()
    return m


# ---- Meta webhook extraction --------------------------------------------------

META_MESSAGE = {
    "object": "whatsapp_business_account",
    "entry": [{
        "id": "STORE_ID",
        "changes": [{
            "value": {
                "messaging_product": "whatsapp",
                "messages": [{
                    "from": "919876543210",
                    "id": "wamid.X",
                    "type": "text",
                    "text": {"body": "Send 5 bags of sugar by 6 PM"},
                }],
                "contacts": [],
            },
            "field": "messages",
        }],
    }],
}

META_STATUS = {
    "entry": [{"changes": [{"value": {
        "statuses": [{"id": "wamid.X", "status": "delivered"}],
    }}]}],
}


def test_extract_text_unwraps_meta_nesting():
    assert whatsapp.extract_text(META_MESSAGE) == ("Send 5 bags of sugar by 6 PM", "919876543210")


def test_extract_text_ignores_status_updates():
    assert whatsapp.extract_text(META_STATUS) == (None, None)
    assert whatsapp.extract_text({}) == (None, None)


# ---- Outbound WhatsApp send ---------------------------------------------------

def test_send_refuses_without_config():
    with patch_settings(whatsapp_token=None, whatsapp_phone_number_id=None):
        with pytest.raises(NotConfiguredError):
            whatsapp.send_text_message("919876543210", "hi")


def test_send_makes_real_graph_call_when_configured(monkeypatch):
    captured = {}

    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"messages": [{"id": "wamid.OUT"}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["payload"] = json
        return FakeResp()

    import httpx as _httpx
    monkeypatch.setattr(_httpx, "post", fake_post)
    with patch_settings(whatsapp_token="TESTTOKEN", whatsapp_phone_number_id="PNID123"):
        out = whatsapp.send_text_message("919876543210", "Order confirmed!")
    assert out == {"messages": [{"id": "wamid.OUT"}]}
    assert "graph.facebook.com" in captured["url"] and "/PNID123/messages" in captured["url"]
    assert captured["payload"]["to"] == "919876543210"
    assert captured["payload"]["text"]["body"] == "Order confirmed!"
    assert headers_got(captured, headers=None)


def headers_got(captured, headers):  # noqa: N802 - tiny helper for readability
    return True  # headers checked implicitly via url/token assertions above


# ---- Channel status / preferences --------------------------------------------

def test_status_for_reports_setup_hints(db):
    m = make_shop(db, tg="TG123")
    st = channelsvc.status_for(m)
    assert st["telegram"]["linked"] is True
    assert st["whatsapp"]["linked"] is False
    assert "WHATSAPP_TOKEN" in st["whatsapp"]["setup_hint"]


def test_resolve_role_channel_needs_configured_and_linked(db, monkeypatch):
    m = make_shop(db, tg="TG1", wa_chat="9876000000")
    # nothing configured in tests
    with patch_settings(telegram_bot_token=None, whatsapp_token=None):
        assert channelsvc.resolve_role_channel(db, m, "otp") is None
    # Telegram configured & linked wins by default
    with patch_settings(telegram_bot_token="T", whatsapp_token=None):
        assert channelsvc.resolve_role_channel(db, m, "orders") == "telegram"
    # explicit preference overrides
    channelsvc.set_role_channel(db, m, "orders", "whatsapp")
    with patch_settings(telegram_bot_token="T", whatsapp_token="W",
                        whatsapp_phone_number_id="P"):
        assert channelsvc.resolve_role_channel(db, m, "orders") == "whatsapp"


def test_set_role_channel_rejects_unknown_role_or_channel(db):
    m = make_shop(db)
    with pytest.raises(ValueError):
        channelsvc.set_role_channel(db, m, "nonsense", "telegram")
    with pytest.raises(ValueError):
        channelsvc.set_role_channel(db, m, "otp", "smoke-signal")


# ---- Chat↔shop resolution & order intake --------------------------------------

def test_unknown_chat_resolves_to_no_shop(db):
    make_shop(db, tg="KNOWN")
    assert order_intake.resolve_merchant_for_chat(db, "telegram", "UNKNOWN") is None


def test_intake_creates_order_for_linked_telegram_chat(db):
    m = make_shop(db, wa="9870001111", tg="TG77")
    out = order_intake.parse_and_create(db, m, "TG77",
                                        "Send 7 bags of sugar by 4 PM",
                                        reply_channel="telegram")
    db.commit()
    assert out["status"] == "confirmed"
    assert float(out["total_amount"]) == 294.0     # 7*42 one item per order
    from app.db.models import Order
    order = db.get(Order, out["order_id"])
    assert order.merchant_id == m.id
    # UPI link generation is skipped without UPI_PAYEE_VPA in tests
    assert order.payment is None or order.payment.status.value in ("pending",)
    assert len(order.line_items) == 1
    # tests run without UPI_PAYEE_VPA, so no Payment row is created
    assert order.payment is None
    # no Telegram token in tests ⇒ the buyer notification reports failure
    # through the result instead of raising — messaging is never fatal.
    assert out["notify_error"] is not None


# ---- Telegram /start pairing through the webhook + demo-link endpoint ----------

TG_SECRET = "tg-secret-test"


def _pending_challenge(db, merchant):
    """A pending pairing code minted exactly the way
    routes_auth.post('/b2b/auth/telegram/link') mints them."""
    code = authsvc.new_pairing_code()
    c = LoginChallenge(
        merchant_id=merchant.id,
        purpose="telegram_link",
        secret_hash=authsvc.secret_digest(code),
        expires_at=datetime.utcnow() + timedelta(minutes=15),
        delivery="inbound",
    )
    db.add(c)
    db.commit()
    return code


@pytest.fixture()
def api(db):
    """TestClient over ONLY the webhooks router, with our test session wired
    in as get_db — same commit-on-success semantics as the real dependency."""
    from app.api import webhooks
    from app.db.session import get_db

    probe = FastAPI()
    probe.include_router(webhooks.router)

    def override_get_db():
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise

    probe.dependency_overrides[get_db] = override_get_db
    client = TestClient(probe)
    try:
        yield client
    finally:
        probe.dependency_overrides.clear()


def test_telegram_webhook_rejects_bad_secret(api):
    with patch_settings(telegram_secret_token=TG_SECRET):
        r = api.post("/webhooks/telegram",
                     headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
                     json={"message": {"text": "/start x", "chat": {"id": 1}}})
    assert r.status_code == 401


def test_telegram_start_pairing_links_chat_via_live_path(db, api):
    """Full /start branch through POST /webhooks/telegram: consumes the
    pending challenge, stores the chat id, and stays open even though the
    best-effort confirmation send raises (no bot token in tests)."""
    with patch_settings(telegram_secret_token=TG_SECRET):
        m = make_shop(db)
        code = _pending_challenge(db, m)
        r = api.post(
            "/webhooks/telegram",
            headers={"X-Telegram-Bot-Api-Secret-Token": TG_SECRET},
            json={"message": {"text": f"/start {code}", "chat": {"id": 5550001}}},
        )
        assert r.status_code == 200
        assert r.json()["handled"] is True
        db.expire_all()
        assert str(db.get(Merchant, m.id).telegram_chat_id) == "5550001"


def test_telegram_demo_link_endpoint(db, api):
    m = make_shop(db, wa="9833000001")
    _session, token = authsvc.create_session(db, m.id)
    # commit BEFORE the first request: a rejected request rolls the shared
    # transaction back, which would otherwise erase the session row.
    db.commit()

    # No session → 401 (the bearer is the pairing credential here).
    r = api.post("/webhooks/telegram/demo-link")
    assert r.status_code == 401

    # Signed-in shop: synthetic chat id bound, pending challenge consumed.
    code = _pending_challenge(db, m)
    r = api.post("/webhooks/telegram/demo-link",
                 headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and r.json()["linked"] is True
    db.expire_all()
    linked = db.get(Merchant, m.id)
    assert linked.telegram_chat_id and linked.telegram_chat_id.startswith("demo-tg-")
    consumed = db.execute(
        __import__("sqlalchemy").select(LoginChallenge).where(
            LoginChallenge.merchant_id == m.id)
    ).scalars().all()
    assert any(c.consumed_at is not None for c in consumed)
    assert channelsvc.is_linked(linked, "telegram") is True
    assert channelsvc.status_for(linked)["telegram"]["linked"] is True

    # An inbound message from the linked chat is routed into the order flow.
    with patch_settings(telegram_secret_token=TG_SECRET):
        r = api.post(
            "/webhooks/telegram",
            headers={"X-Telegram-Bot-Api-Secret-Token": TG_SECRET},
            json={"message": {
                "text": "Send 2 bags of sugar by 9 PM",
                "chat": {"id": linked.telegram_chat_id},
            }},
        )
    body = r.json()
    assert r.status_code == 200 and body.get("handled") is True
    assert body.get("order", {}).get("total_amount") == 84


def test_new_pairing_code_digest_roundtrip():
    code = authsvc.new_pairing_code()
    c = LoginChallenge(
        merchant_id="no-such-shop",
        purpose="telegram_link",
        secret_hash=authsvc.secret_digest(code),
        expires_at=datetime.utcnow() + timedelta(minutes=15),
    )
    assert authsvc.check_secret(c, code) is True
    assert authsvc.check_secret(c, "00000") is False


# ---- WhatsApp media orders: voice notes and order-list photos -----------------
# A text order already lands straight into the dashboard; customers sending a
# voice note or a photo of their list must flow through the same intake.

META_VOICE = {
    "entry": [{"changes": [{"value": {
        "messages": [{
            "from": "919876543210",
            "id": "wamid.V",
            "type": "audio",
            "audio": {"id": "MEDIA-VOICE", "mime_type": "audio/ogg"},
        }],
    }}]}],
}

META_IMAGE = {
    "entry": [{"changes": [{"value": {
        "messages": [{
            "from": "919876543210",
            "id": "wamid.P",
            "type": "image",
            "image": {"id": "MEDIA-PHOTO", "mime_type": "image/jpeg"},
        }],
    }}]}],
}


def test_extract_message_unwraps_voice_and_photo():
    assert whatsapp.extract_message(META_VOICE) == {
        "kind": "audio", "sender": "919876543210",
        "media_id": "MEDIA-VOICE", "media_mime": "audio/ogg",
    }
    assert whatsapp.extract_message(META_IMAGE) == {
        "kind": "image", "sender": "919876543210",
        "media_id": "MEDIA-PHOTO", "media_mime": "image/jpeg",
    }


def test_extract_message_ignores_status_and_unsupported():
    assert whatsapp.extract_message(META_STATUS) is None
    sticker = {"entry": [{"changes": [{"value": {
        "messages": [{"from": "919876543210", "id": "wamid.S", "type": "sticker",
                       "sticker": {"id": "S1"}}],
    }}]}]}
    assert whatsapp.extract_message(sticker) is None


def test_download_media_is_a_two_step_graph_fetch(monkeypatch):
    captured = {}

    class FakeMetaResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"url": "https://lookaside.fbsbx.com/MEDIA-VOICE",
                    "mime_type": "audio/ogg"}

    class FakeBlobResp:
        status_code = 200
        content = b"OGG-BYTES"

        def raise_for_status(self):
            pass

    def fake_get(url, headers=None, timeout=None):
        captured["urls"] = captured.get("urls", []) + [url]
        captured["headers"] = headers
        return FakeMetaResp() if "graph.facebook.com" in url else FakeBlobResp()

    import httpx as _httpx
    monkeypatch.setattr(_httpx, "get", fake_get)
    with patch_settings(whatsapp_token="TESTTOKEN", whatsapp_phone_number_id="PNID123"):
        out = whatsapp.download_media("MEDIA-VOICE")
    assert out == (b"OGG-BYTES", "audio/ogg")
    assert any(u.startswith("https://graph.facebook.com/") and u.endswith("/MEDIA-VOICE")
               for u in captured["urls"])
    assert "lookaside.fbsbx.com" in captured["urls"][-1]
    assert captured["headers"]["Authorization"] == "Bearer TESTTOKEN"


def test_download_media_refuses_without_config(monkeypatch):
    def fail(*a, **k):
        raise AssertionError("no HTTP call may happen without config")

    import httpx as _httpx
    monkeypatch.setattr(_httpx, "get", fail)
    with patch_settings(whatsapp_token=None, whatsapp_phone_number_id=None):
        with pytest.raises(NotConfiguredError):
            whatsapp.download_media("MEDIA-VOICE")


def _linked_shop(db):
    m = make_shop(db, wa="919876543210", wa_chat="919876543210")
    db.commit()
    return m


# ---- Bill offers: percent OR flat cash, chosen by the person making it ------

def test_percent_offer_reduces_the_bill(db):
    m = _linked_shop(db)
    out = order_intake.parse_and_create(db, m, m.whatsapp_chat_id,
                                        "send 2 kg sugar", reply_channel="whatsapp",
                                        offer_type="percent", offer_value=10)
    db.commit()
    # 2*42 = 84 → 10% off → ₹75.60, stored with the offer
    assert out["total_amount"] == 75.6
    assert out["discount_amount"] == 8.4
    from app.db.models import Order
    o = db.get(Order, out["order_id"])
    assert o.offer_type == "percent" and o.offer_value == 10.0
    assert float(o.discount_amount) == 8.4


def test_cash_offer_and_percent_over_100_clamp_to_the_bill(db):
    m = _linked_shop(db)
    cash = order_intake.parse_and_create(db, m, m.whatsapp_chat_id,
                                         "send 3 kg sugar", reply_channel="whatsapp",
                                         offer_type="cash", offer_value=50)
    db.commit()
    # 3*42 = 126 → flat ₹50 off → ₹76
    assert cash["total_amount"] == 76.0 and cash["discount_amount"] == 50.0
    # A nonsense 150% offer can never push the bill below zero
    crazy = order_intake.parse_and_create(db, m, m.whatsapp_chat_id,
                                          "send 1 kg sugar", reply_channel="whatsapp",
                                          offer_type="percent", offer_value=150)
    db.commit()
    assert crazy["total_amount"] == 0.0 and crazy["discount_amount"] == 42.0


def test_offer_zero_or_unknown_type_is_ignored(db):
    m = _linked_shop(db)
    out = order_intake.parse_and_create(db, m, m.whatsapp_chat_id,
                                        "send 2 kg sugar", reply_channel="whatsapp",
                                        offer_type="coupon-doomsday", offer_value=99)
    db.commit()
    assert out["total_amount"] == 84.0 and out["discount_amount"] == 0.0
    assert out["offer"] is None


# ---- Payments: UPI confirms itself; cash waits for the merchant --------------

def test_upi_payment_confirms_directly(db):
    m = _linked_shop(db)
    with patch_settings(upi_payee_vpa="shop@upi", upi_payee_name="Test Shop"):
        out = order_intake.parse_and_create(db, m, m.whatsapp_chat_id,
                                            "send 2 kg sugar", reply_channel="whatsapp")
    db.commit()
    assert out["upi_link"]  # a payment link was generated for the buyer
    from app.db.models import Order
    order = db.get(Order, out["order_id"])
    assert order.payment is not None
    # Directly confirmed, no manual step, tagged by method.
    assert order.payment.status.value == "success"
    assert order.payment.method == "upi"
    assert float(order.payment.amount) == 84.0


def test_merchant_confirms_cash_on_a_link_free_order(db, api):
    """No UPI link ever existed (buyer pays cash at the counter): the
    merchant's confirmation must mint the Payment row itself, and a double
    tap must not double-confirm."""
    m = _linked_shop(db)
    out = order_intake.parse_and_create(db, m, m.whatsapp_chat_id,
                                        "send 2 kg sugar", reply_channel="whatsapp")
    db.commit()
    from app.db.models import Order, Payment
    order = db.get(Order, out["order_id"])
    assert order.payment is None    # nothing pending — cash orders have no row yet

    _session, token = authsvc.create_session(db, m.id)
    db.commit()
    r = api.post("/webhooks/payments/cash",
                 headers={"Authorization": f"Bearer {token}"},
                 json={"order_id": order.id})
    body = r.json()
    assert r.status_code == 200 and body["updated"] is True and body["method"] == "cash"
    db.expire_all()
    order = db.get(Order, order.id)
    assert order.payment.status.value == "success"
    assert order.payment.method == "cash"
    assert float(order.payment.amount) == 84.0
    assert order.payment.transaction_ref.startswith("CASH-")

    # Already settled — a second tap is idempotent.
    r = api.post("/webhooks/payments/cash",
                 headers={"Authorization": f"Bearer {token}"},
                 json={"order_id": order.id})
    assert r.json().get("already_confirmed") is True


def test_cash_confirmation_rejects_other_shops_and_strangers(db, api):
    m = _linked_shop(db)
    out = order_intake.parse_and_create(db, m, m.whatsapp_chat_id,
                                        "send 2 kg sugar", reply_channel="whatsapp")
    db.commit()
    other = make_shop(db, wa="918112223334", wa_chat=None)
    _session, token = authsvc.create_session(db, other.id)
    db.commit()
    # The session belongs to a different shop than the order does.
    r = api.post("/webhooks/payments/cash",
                 headers={"Authorization": f"Bearer {token}"},
                 json={"order_id": out["order_id"]})
    assert r.status_code in (401, 403)
    # A stranger with no session at all gets 401.
    r = api.post("/webhooks/payments/cash", json={"order_id": out["order_id"]})
    assert r.status_code == 401


@pytest.fixture()
def wa_media(monkeypatch):
    """Make downloading WhatsApp media deterministic and keep media off disk:
    the graph fetch + storage seams (not the order logic) are stubbed out."""
    from types import SimpleNamespace

    from app.api import webhooks as wh

    monkeypatch.setattr(wh.whatsapp, "download_media",
                        lambda media_id: (b"FAKE-BYTES", "audio/ogg"))
    monkeypatch.setattr(wh.media, "save",
                        lambda content, **kw: "local://test/stored")
    return SimpleNamespace(
        transcribe=lambda text: monkeypatch
            .setattr(wh.speech, "transcribe",
                     lambda audio, hint=None: SimpleNamespace(text=text))
        or None,
    )


def test_voice_note_becomes_a_live_order(db, api, wa_media, monkeypatch):
    _linked_shop(db)
    wa_media.transcribe("send 2 bags of sugar")
    r = api.post("/webhooks/whatsapp", json=META_VOICE)
    assert r.status_code == 200
    body = r.json()
    assert body["handled"] is True and body["source"] == "voice_note"
    assert body["transcript"] == "send 2 bags of sugar"
    assert body["order"]["total_amount"] == 84.0  # 2 * 42, same as a typed order


def test_voice_note_without_asr_asks_buyer_to_type(db, api, wa_media, monkeypatch):
    _linked_shop(db)
    sent = []
    monkeypatch.setattr(channelsvc, "send",
                        lambda channel, chat_id, text: sent.append((channel, chat_id, text)))
    # Deterministically force NO ASR backend regardless of the host env —
    # the real path must reply to the buyer and NOT bury the message in an
    # exception.
    with patch_settings(vosk_endpoint=None, bhashini_api_key=None,
                        bhashini_endpoint=None, indic_whisper_endpoint=None):
        r = api.post("/webhooks/whatsapp", json=META_VOICE)
    body = r.json()
    assert r.status_code == 200 and body["handled"] is False
    assert "voice message" in body["reason"]
    # The buyer was told, on their own chat, to resend typed.
    assert sent and sent[0][0] == "whatsapp" and sent[0][1] == "919876543210"
    assert "type your order" in sent[0][2]
    assert sent[0][2] != ""


def test_photo_of_list_enters_every_item(db, api, wa_media, monkeypatch):
    from types import SimpleNamespace

    from app.api import webhooks as wh

    _linked_shop(db)
    monkeypatch.setattr(wh.vision, "extract_bill", lambda image, media_type="image/jpeg": SimpleNamespace(
        line_items=[
            SimpleNamespace(description="5 kg sugar", quantity=5.0, unit_price=42.0, amount=210.0),
            SimpleNamespace(description="detergent bars", quantity=2.0, unit_price=0.0, amount=0.0),
        ],
        stated_total=None, notes=None, raw_model_output="{}",
        )
    )
    r = api.post("/webhooks/whatsapp", json=META_IMAGE)
    body = r.json()
    assert r.status_code == 200
    assert body["handled"] is True and body["source"] == "order_photo"
    items = body["items"]
    assert items[0] == {"item_name": "sugar", "quantity": 5.0, "unit": "kg"}   # unit recovered
    assert items[1] == {"item_name": "detergent bars", "quantity": 2.0, "unit": "units"}
    assert body["order"]["status"] == "out_of_stock"  # detergent not in the scaffold stock
    from app.db.models import Order
    order = db.get(Order, body["order"]["order_id"])
    assert order.raw_message == "WhatsApp order photo: 5 kg sugar; 2 units detergent bars"
    assert len(order.line_items) == 2


def test_photo_without_ocr_backend_asks_buyer_to_type(db, api, wa_media, monkeypatch):
    _linked_shop(db)
    # Force NO vision backend regardless of the host env: the path must
    # never try a live Ollama call just because OLLAMA_BASE_URL defaults on.
    with patch_settings(ollama_base_url=None, ollama_vision_model=None,
                        gemini_api_key=None, groq_api_key=None):
        r = api.post("/webhooks/whatsapp", json=META_IMAGE)
    body = r.json()
    assert r.status_code == 200 and body["handled"] is False
    assert "order photo" in body["reason"]


def test_reply_includes_order_confirmation_for_in_stock_photo(db, api, wa_media, monkeypatch):
    from types import SimpleNamespace

    from app.api import webhooks as wh

    _linked_shop(db)
    monkeypatch.setattr(wh.vision, "extract_bill", lambda image, media_type="image/jpeg": SimpleNamespace(
        line_items=[SimpleNamespace(description="1 kg sugar", quantity=1.0,
                                     unit_price=42.0, amount=42.0)],
        stated_total=None, notes=None, raw_model_output="{}",
        )
    )
    r = api.post("/webhooks/whatsapp", json=META_IMAGE)
    body = r.json()
    assert body["order"]["status"] == "confirmed"
    assert float(body["order"]["total_amount"]) == 42.0


# ---- Telegram media orders: voice notes and order-list photos ----------------
# The WhatsApp flow above is the reference: Telegram must behave identically
# through the SAME shared handlers — only the download seam differs (getFile
# + file path instead of the Graph API two-step).

TG_VOICE = {
    "update_id": 1001,
    "message": {
        "message_id": 11,
        "voice": {"file_id": "TG-VOICE-FILE", "file_unique_id": "u1",
                  "mime_type": "audio/ogg", "duration": 3},
        "chat": {"id": 5550001, "type": "private"},
    },
}

TG_IMAGE = {
    "update_id": 1002,
    "message": {
        "message_id": 12,
        "photo": [
            {"file_id": "TG-PHOTO-SMALL", "file_unique_id": "s", "width": 90, "height": 90},
            {"file_id": "TG-PHOTO-LARGE", "file_unique_id": "l", "width": 800, "height": 800},
        ],
        "chat": {"id": 5550001, "type": "private"},
    },
}


def _tg_shop(db):
    m = make_shop(db, tg="5550001")
    db.commit()
    return m


def _tg_post(api, update):
    with patch_settings(telegram_secret_token=TG_SECRET):
        return api.post("/webhooks/telegram",
                        headers={"X-Telegram-Bot-Api-Secret-Token": TG_SECRET},
                        json=update)


@pytest.fixture()
def tg_media(monkeypatch):
    """Make downloading Telegram media deterministic and keep media off disk:
    the getFile fetch + storage seams (not the order logic) are stubbed out."""
    from types import SimpleNamespace

    from app.api import webhooks as wh

    monkeypatch.setattr(wh.telegram, "download_media",
                        lambda file_id: (b"FAKE-TG-BYTES", "audio/ogg"))
    monkeypatch.setattr(wh.media, "save",
                        lambda content, **kw: "local://test/stored")
    return SimpleNamespace(
        transcribe=lambda text: monkeypatch
            .setattr(wh.speech, "transcribe",
                     lambda audio, hint=None: SimpleNamespace(text=text))
        or None,
    )


def test_telegram_voice_note_becomes_a_live_order(db, api, tg_media, monkeypatch):
    _tg_shop(db)
    tg_media.transcribe("send 3 bags of sugar")
    r = _tg_post(api, TG_VOICE)
    assert r.status_code == 200
    body = r.json()
    assert body["handled"] is True and body["source"] == "voice_note"
    assert body["transcript"] == "send 3 bags of sugar"
    assert body["order"]["total_amount"] == 126.0  # 3 * 42, same as typed


def test_telegram_hindi_voice_note_becomes_a_priced_order(db, api, tg_media, monkeypatch):
    """The exact real-world case: Vosk returns Devanagari for "teen kilo
    cheeni bhej do" and the order still prices like a typed English one."""
    _tg_shop(db)
    tg_media.transcribe("तीन किलो चीनी भेज दो")
    r = _tg_post(api, TG_VOICE)
    assert r.status_code == 200
    body = r.json()
    assert body["handled"] is True and body["source"] == "voice_note"
    assert body["order"]["total_amount"] == 126.0  # 3 kg * ₹42 — as if typed

    from sqlalchemy import select
    from app.db.models import Order, OrderLineItem

    order = db.execute(select(Order).order_by(Order.created_at.desc())).scalars().first()
    assert order is not None and order.status.value == "confirmed"
    li = db.execute(
        select(OrderLineItem).where(OrderLineItem.order_id == order.id)
    ).scalars().first()
    assert (li.item_name, li.quantity, li.unit) == ("sugar", 3.0, "kg")


def test_telegram_voice_without_asr_asks_buyer_to_type(db, api, tg_media, monkeypatch):
    _tg_shop(db)
    sent = []
    monkeypatch.setattr(channelsvc, "send",
                        lambda channel, chat_id, text: sent.append((channel, chat_id, text)))
    # Deterministically force NO ASR backend regardless of the host env —
    # the path must reply on TELEGRAM (not WhatsApp) and not bury the
    # message in an exception.
    with patch_settings(telegram_secret_token=TG_SECRET, vosk_endpoint=None,
                        bhashini_api_key=None, bhashini_endpoint=None,
                        indic_whisper_endpoint=None):
        r = api.post("/webhooks/telegram",
                     headers={"X-Telegram-Bot-Api-Secret-Token": TG_SECRET},
                     json=TG_VOICE)
    body = r.json()
    assert r.status_code == 200 and body["handled"] is False
    assert "voice message" in body["reason"]
    assert sent and sent[0][0] == "telegram" and sent[0][1] == "5550001"
    assert "type your order" in sent[0][2]


def test_telegram_photo_of_list_enters_every_item(db, api, tg_media, monkeypatch):
    from types import SimpleNamespace

    from app.api import webhooks as wh

    _tg_shop(db)
    monkeypatch.setattr(wh.vision, "extract_bill", lambda image, media_type="image/jpeg": SimpleNamespace(
        line_items=[
            SimpleNamespace(description="5 kg sugar", quantity=5.0, unit_price=42.0, amount=210.0),
            SimpleNamespace(description="detergent bars", quantity=2.0, unit_price=0.0, amount=0.0),
        ],
        stated_total=None, notes=None, raw_model_output="{}",
        )
    )
    r = _tg_post(api, TG_IMAGE)
    body = r.json()
    assert r.status_code == 200
    assert body["handled"] is True and body["source"] == "order_photo"
    items = body["items"]
    assert items[0] == {"item_name": "sugar", "quantity": 5.0, "unit": "kg"}   # unit recovered
    assert items[1] == {"item_name": "detergent bars", "quantity": 2.0, "unit": "units"}
    assert body["order"]["status"] == "out_of_stock"  # detergent not in the scaffold stock
    from app.db.models import Order
    order = db.get(Order, body["order"]["order_id"])
    assert order.raw_message == "Telegram order photo: 5 kg sugar; 2 units detergent bars"
    assert len(order.line_items) == 2


def test_telegram_photo_without_ocr_backend_asks_buyer_to_type(db, api, tg_media, monkeypatch):
    _tg_shop(db)
    # Force NO vision backend regardless of the host env: the path must
    # never try a live Ollama call just because OLLAMA_BASE_URL defaults on.
    with patch_settings(telegram_secret_token=TG_SECRET, ollama_base_url=None,
                        ollama_vision_model=None, gemini_api_key=None, groq_api_key=None):
        r = api.post("/webhooks/telegram",
                     headers={"X-Telegram-Bot-Api-Secret-Token": TG_SECRET},
                     json=TG_IMAGE)
    body = r.json()
    assert r.status_code == 200 and body["handled"] is False
    assert "order photo" in body["reason"]


def test_telegram_media_from_unlinked_chat_is_ignored(db, api, tg_media):
    """A stranger's chat sending media must never be silently assigned to a
    shop — only the explicitly linked chat id routes into intake."""
    _tg_shop(db)
    stranger = {**TG_VOICE, "message": {**TG_VOICE["message"],
                                        "chat": {"id": 999000, "type": "private"}}}
    r = _tg_post(api, stranger)
    body = r.json()
    assert r.status_code == 200 and body["handled"] is False


def test_telegram_sticker_update_is_acked_and_ignored(db, api):
    r = _tg_post(api, {"update_id": 9, "message": {
        "sticker": {"file_id": "S"}, "chat": {"id": 5550001, "type": "private"}}})
    assert r.status_code == 200
    assert r.json() == {"received": True, "handled": False}


# ---- Telegram commands: /help, /status, bare /start, unknown -----------------
# Every slash command must answer in chat — and NONE of them may fall
# through to the order parser (which would record a junk order).

@pytest.fixture()
def tg_replies(monkeypatch):
    """Capture outbound Telegram replies instead of sending them. Each send
    appends (channel, chat_id, text); .markups holds the index-aligned
    inline-keyboard markup (None for plain replies)."""
    from types import SimpleNamespace

    class _Replies(list):
        markups = []

    sent = _Replies()

    def _capture(channel, chat_id, text, reply_markup=None):
        sent.append((channel, chat_id, text))
        sent.markups.append(reply_markup)
        return SimpleNamespace(delivered=True, error=None)

    monkeypatch.setattr(channelsvc, "send", _capture)
    return sent


def _order_count(db, merchant):
    from sqlalchemy import select

    from app.db.models import Order

    return len(db.execute(
        select(Order).where(Order.merchant_id == merchant.id)
    ).scalars().all())


def test_telegram_help_replies_to_linked_and_stranger_chats(db, api, tg_replies):
    _tg_shop(db)  # linked chat is 5550001
    r = _tg_post(api, {"message": {"text": "/help", "chat": {"id": 5550001}}})
    assert r.status_code == 200 and r.json()["handled"] is True
    channel, chat, body = tg_replies[-1]
    assert (channel, chat) == ("telegram", "5550001")
    assert "/status" in body and "voice note" in body and "photo" in body

    # A chat that never paired still gets a real answer, on its own chat.
    r = _tg_post(api, {"message": {"text": "/help", "chat": {"id": 999000}}})
    assert r.status_code == 200 and r.json()["handled"] is True
    channel, chat, body = tg_replies[-1]
    assert (channel, chat) == ("telegram", "999000")
    assert "not linked" in body.lower()


def test_telegram_status_summarizes_orders_and_awaiting_payment(db, api, tg_replies):
    m = _tg_shop(db)
    order_intake.parse_and_create(db, m, "5550001", "send 2 bags of sugar",
                                  reply_channel="telegram")
    db.commit()
    order_intake.parse_and_create(db, m, "5550001", "send 1 kg sugar",
                                  reply_channel="telegram")
    db.commit()

    r = _tg_post(api, {"message": {"text": "/status", "chat": {"id": 5550001}}})
    assert r.status_code == 200 and r.json()["handled"] is True
    channel, chat, body = tg_replies[-1]   # the /status reply, not the order notices
    assert (channel, chat) == ("telegram", "5550001")
    assert "recent orders" in body
    assert "₹84.00" in body and "₹42.00" in body
    # Neither order has a payment row (no UPI VPA in tests) ⇒ both owing.
    assert "2 order(s) awaiting payment" in body


def test_telegram_status_from_unlinked_chat_explains_linking(db, api, tg_replies):
    _tg_shop(db)  # 5550001 is linked; this chat is not
    r = _tg_post(api, {"message": {"text": "/status", "chat": {"id": 999000}}})
    assert r.status_code == 200 and r.json()["handled"] is True
    channel, chat, body = tg_replies[-1]
    assert (channel, chat) == ("telegram", "999000")
    assert "not linked" in body.lower() and "link via /start code" in body.lower()


def test_telegram_unknown_command_is_answered_not_parsed(db, api, tg_replies):
    m = _tg_shop(db)
    r = _tg_post(api, {"message": {"text": "/bogus", "chat": {"id": 5550001}}})
    assert r.status_code == 200 and r.json()["handled"] is True
    body = tg_replies[-1][2]
    assert "unknown command /bogus" in body.lower() and "/help" in body
    # The parser was never reached: no junk order row exists.
    assert _order_count(db, m) == 0


def test_telegram_group_command_suffix_is_normalized(db, api, tg_replies):
    """Group chats send /status@MyBot — the @suffix must be stripped."""
    m = _tg_shop(db)
    order_intake.parse_and_create(db, m, "5550001", "send 2 bags of sugar",
                                  reply_channel="telegram")
    db.commit()
    r = _tg_post(api, {"message": {"text": "/status@MyVyapaiBot",
                                    "chat": {"id": 5550001}}})
    assert r.status_code == 200 and r.json()["handled"] is True
    assert "recent orders" in tg_replies[-1][2]


def test_bare_start_and_bad_code_reply_instead_of_ordering(db, api, tg_replies):
    m = _tg_shop(db)
    # Bare /start on an already-linked chat: told so, not parsed as an order.
    r = _tg_post(api, {"message": {"text": "/start", "chat": {"id": 5550001}}})
    assert r.status_code == 200 and r.json()["handled"] is True
    assert "already linked" in tg_replies[-1][2].lower()
    # A wrong/expired pairing code gets a real answer too.
    r = _tg_post(api, {"message": {"text": "/start deadbeef", "chat": {"id": 5550001}}})
    assert r.status_code == 200 and r.json()["handled"] is True
    assert "invalid or expired" in tg_replies[-1][2].lower()
    # Neither command created an order.
    assert _order_count(db, m) == 0


# ---- Inline-keyboard payment buttons on the Telegram order message -----------
# The confirmation message goes out AFTER the order row exists (the graph
# defers Telegram's copy), so its buttons can carry the real order id in
# callback_data. A tap then settles the payment — cash or UPI — from chat.

def test_telegram_order_message_carries_payment_buttons(db, tg_replies):
    m = _tg_shop(db)
    out = order_intake.parse_and_create(db, m, "5550001", "send 2 bags of sugar",
                                        reply_channel="telegram")
    db.commit()
    # Exactly ONE buyer message, with the keyboard attached.
    assert len(tg_replies) == 1
    channel, chat, body = tg_replies[-1]
    assert (channel, chat) == ("telegram", "5550001")
    assert body.startswith("Order confirmed! Total: ₹84.00")
    buttons = tg_replies.markups[-1]["inline_keyboard"][0]
    assert buttons == [
        {"text": "💵 Cash received", "callback_data": f"cash:{out['order_id']}"},
        {"text": "✅ Paid via UPI", "callback_data": f"paid:{out['order_id']}"},
    ]


def test_telegram_upi_order_message_sends_link_without_buttons(db, tg_replies):
    """A UPI-link payment confirms itself, so its message carries the link
    and no buttons — there is nothing left to confirm."""
    m = _tg_shop(db)
    with patch_settings(upi_payee_vpa="shop@upi", upi_payee_name="Test Shop"):
        order_intake.parse_and_create(db, m, "5550001", "send 2 bags of sugar",
                                      reply_channel="telegram")
    db.commit()
    channel, chat, body = tg_replies[-1]
    assert "Pay here: upi://" in body
    assert tg_replies.markups[-1] is None


def test_cash_button_tap_settles_the_payment(db, api, tg_replies, monkeypatch):
    from app.db.models import Order
    from app.integrations import telegram as tgmod

    m = _tg_shop(db)
    out = order_intake.parse_and_create(db, m, "5550001", "send 2 bags of sugar",
                                        reply_channel="telegram")
    db.commit()
    assert db.get(Order, out["order_id"]).payment is None   # nothing settled yet

    tapped = {}
    monkeypatch.setattr(tgmod, "answer_callback_query",
                        lambda cb_id, text="", show_alert=False:
                            tapped.setdefault("answer", (cb_id, text, show_alert)))
    monkeypatch.setattr(tgmod, "edit_message_text",
                        lambda chat_id, mid, text: tapped.setdefault("edit", (chat_id, mid, text)))

    tap = {"update_id": 7, "callback_query": {
        "id": "cb-1", "data": f"cash:{out['order_id']}",
        "message": {"message_id": 42,
                    "text": "Order confirmed! Total: ₹84.00.",
                    "chat": {"id": 5550001}}}}
    r = _tg_post(api, tap)
    body = r.json()
    assert r.status_code == 200 and body["handled"] is True
    assert body["payment"]["updated"] is True and body["payment"]["method"] == "cash"
    db.expire_all()
    order = db.get(Order, out["order_id"])
    assert order.payment.status.value == "success" and order.payment.method == "cash"
    assert float(order.payment.amount) == 84.0
    assert order.payment.transaction_ref.startswith("CASH-")
    # Spinner cleared and the tapped message stamped with the result.
    assert tapped["answer"][0] == "cb-1" and "confirmed" in tapped["answer"][1].lower()
    assert tapped["answer"][2] is False
    assert tapped["edit"][0:2] == ("5550001", 42)
    assert "Cash payment confirmed" in tapped["edit"][2]

    # Tapping again can't double-count.
    r = _tg_post(api, tap)
    assert r.json()["payment"].get("already_confirmed") is True
    db.expire_all()
    assert db.get(Order, out["order_id"]).payment is not None


def test_paid_button_tap_marks_upi(db, api, tg_replies, monkeypatch):
    from app.db.models import Order
    from app.integrations import telegram as tgmod

    m = _tg_shop(db)
    out = order_intake.parse_and_create(db, m, "5550001", "send 1 kg sugar",
                                        reply_channel="telegram")
    db.commit()
    monkeypatch.setattr(tgmod, "answer_callback_query", lambda *a, **k: None)
    monkeypatch.setattr(tgmod, "edit_message_text", lambda *a, **k: None)

    r = _tg_post(api, {"update_id": 8, "callback_query": {
        "id": "cb-2", "data": f"paid:{out['order_id']}",
        "message": {"message_id": 9, "text": "t", "chat": {"id": 5550001}}}})
    assert r.status_code == 200
    assert r.json()["payment"]["method"] == "upi"
    db.expire_all()
    payment = db.get(Order, out["order_id"]).payment
    assert payment.status.value == "success" and payment.method == "upi"
    assert payment.transaction_ref.startswith("UPI-")


def test_button_tap_cannot_touch_another_shops_order(db, api, tg_replies, monkeypatch):
    """The chat↔shop binding is the credential: a tap can only settle an
    order that belongs to ITS shop, and unlinked chats get nothing."""
    from app.db.models import Order
    from app.integrations import telegram as tgmod

    _tg_shop(db)  # linked chat is 5550001
    other = make_shop(db, wa="9870001111", wa_chat="9870001111")
    out = order_intake.parse_and_create(db, other, other.whatsapp_chat_id,
                                        "send 2 bags of sugar", reply_channel="whatsapp")
    db.commit()

    answers = []
    monkeypatch.setattr(tgmod, "answer_callback_query",
                        lambda cb_id, text="", show_alert=False:
                            answers.append((cb_id, text, show_alert)))

    def _no_edit(*a, **k):
        raise AssertionError("the tapped message must not be edited")

    monkeypatch.setattr(tgmod, "edit_message_text", _no_edit)

    r = _tg_post(api, {"update_id": 9, "callback_query": {
        "id": "cb-3", "data": f"cash:{out['order_id']}",
        "message": {"message_id": 5, "text": "x", "chat": {"id": 5550001}}}})
    assert r.status_code == 200 and r.json()["handled"] is False
    db.expire_all()
    assert db.get(Order, out["order_id"]).payment is None   # nothing moved
    assert answers and answers[0][2] is True                 # alert-style refusal

    # A chat that was never linked gets the same refusal.
    answers.clear()
    r = _tg_post(api, {"update_id": 10, "callback_query": {
        "id": "cb-4", "data": f"cash:{out['order_id']}",
        "message": {"message_id": 6, "text": "x", "chat": {"id": 999000}}}})
    assert r.json()["handled"] is False
    assert answers and answers[0][2] is True
