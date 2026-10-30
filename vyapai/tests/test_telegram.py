"""Telegram adapter — the free messaging channel. Sends are real HTTP calls,
so tests mock httpx.post; the secret-token check must be constant-time and
closed by default."""
import httpx
import pytest

from app.integrations import telegram
from app.integrations.errors import NotConfiguredError, UpstreamError
from tests.conftest import patch_settings


class _FakeResponse:
    def __init__(self, payload=None, status=200):
        self._payload = payload if payload is not None else {"ok": True, "result": {}}
        self.status_code = status
        self._request = httpx.Request("POST", "https://api.telegram.org/x")
        self._response = httpx.Response(status, request=self._request)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=self._request, response=self._response
            )

    def json(self):
        return self._payload


def test_secret_token_mismatch_rejected():
    with patch_settings(telegram_secret_token="s3cret"):
        assert telegram.verify_secret_token("wrong") is False
        assert telegram.verify_secret_token("") is False


def test_secret_token_empty_config_closes_endpoint():
    with patch_settings(telegram_secret_token=""):
        assert telegram.verify_secret_token("s3cret") is False


def test_send_without_token_fails_not_configured():
    with patch_settings(telegram_bot_token=""):
        with pytest.raises(NotConfiguredError):
            telegram.send_text_message(1, "hi")


def test_send_text_message_posts_to_bot_api(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _FakeResponse()

    monkeypatch.setattr(httpx, "post", fake_post)
    with patch_settings(telegram_bot_token="T0KEN"):
        telegram.send_text_message(42, "Namaste")

    assert captured["url"] == "https://api.telegram.org/botT0KEN/sendMessage"
    assert captured["json"] == {"chat_id": 42, "text": "Namaste"}


def test_send_text_message_api_error_is_upstream(monkeypatch):
    monkeypatch.setattr(
        httpx, "post", lambda *a, **kw: _FakeResponse({"ok": False}, status=500)
    )
    with patch_settings(telegram_bot_token="T0KEN"):
        with pytest.raises(UpstreamError):
            telegram.send_text_message(42, "Namaste")


def test_payment_reminder_escalation_levels_reach_the_channel(monkeypatch):
    bodies = []

    def fake_post(url, json=None, timeout=None):
        bodies.append(json["text"])
        return _FakeResponse()

    monkeypatch.setattr(httpx, "post", fake_post)
    with patch_settings(telegram_bot_token="T0KEN"):
        for level in (0, 1, 2):
            telegram.send_payment_reminder(7, 1500.0, "upi://pay", level)

    assert "gentle reminder" in bodies[0]
    assert "₹1,500.00" in bodies[1]
    assert "Final reminder" in bodies[2]


def test_extract_text_passthrough_and_non_text_updates():
    text, chat = telegram.extract_text(
        {"message": {"text": "send 2 kg rice", "chat": {"id": 99}}}
    )
    assert text == "send 2 kg rice"
    assert chat == "99"

    assert telegram.extract_text({"message": {"photo": []}}) == (None, None)
    assert telegram.extract_text({}) == (None, None)


# ---- Media intake: voice notes and order-list photos ------------------------
# Telegram's shape differs from Meta's (file_id + getFile instead of Graph
# ids), but extract_media returns the same dict shape so the shared webhook
# handlers treat both channels identically.

def test_extract_media_unwraps_voice_and_audio():
    assert telegram.extract_media({"message": {
        "voice": {"file_id": "TG-VOICE", "mime_type": "audio/ogg"},
        "chat": {"id": 5550001},
    }}) == {"kind": "audio", "chat_id": "5550001",
           "media_id": "TG-VOICE", "media_mime": "audio/ogg"}
    # Uploaded audio files are spoken orders too ("audio" like Meta).
    assert telegram.extract_media({"message": {
        "audio": {"file_id": "TG-AUDIO", "mime_type": "audio/mpeg"},
        "chat": {"id": 7},
    }}) == {"kind": "audio", "chat_id": "7",
           "media_id": "TG-AUDIO", "media_mime": "audio/mpeg"}


def test_extract_media_photo_picks_the_largest_size():
    # Photos arrive as an array of sizes; the LAST entry is the largest.
    msg = telegram.extract_media({"message": {
        "photo": [{"file_id": "SMALL"}, {"file_id": "LARGE"}],
        "chat": {"id": -100123},
    }})
    assert msg == {"kind": "image", "chat_id": "-100123",
                   "media_id": "LARGE", "media_mime": "image/jpeg"}


def test_extract_media_text_passthrough_and_unsupported_ignored():
    assert telegram.extract_media({"message": {"text": "2 kg rice", "chat": {"id": 9}}}) == {
        "kind": "text", "chat_id": "9", "text": "2 kg rice"}
    assert telegram.extract_media({"message": {"sticker": {"file_id": "S"}}}) is None
    assert telegram.extract_media({"message": {"video": {"file_id": "V"}}}) is None
    assert telegram.extract_media({"edited_message": {"text": "x"}}) is None
    assert telegram.extract_media({"message": {"chat": {"id": 1}}}) is None  # no content
    assert telegram.extract_media({}) is None


def test_download_media_is_getfile_then_file_fetch(monkeypatch):
    captured = {"posts": [], "gets": []}
    file_path = {"value": "voice/file_7.ogg"}

    def fake_post(url, json=None, timeout=None):
        captured["posts"].append((url, json))
        return _FakeResponse({"ok": True, "result": {"file_path": file_path["value"]}})

    class _FakeFileResp:
        status_code = 200
        content = b"FILE-BYTES"

        def raise_for_status(self):
            pass

    def fake_get(url, timeout=None):
        captured["gets"].append(url)
        return _FakeFileResp()

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx, "get", fake_get)
    with patch_settings(telegram_bot_token="T0KEN"):
        out = telegram.download_media("TG-VOICE")
        # MIME comes from the extension of the returned file_path —
        # Telegram's file objects carry no mime_type of their own.
        file_path["value"] = "photos/file_2.jpg"
        photo_out = telegram.download_media("TG-PHOTO")

    assert out == (b"FILE-BYTES", "audio/ogg")
    assert photo_out == (b"FILE-BYTES", "image/jpeg")
    assert captured["posts"] == [
        ("https://api.telegram.org/botT0KEN/getFile", {"file_id": "TG-VOICE"}),
        ("https://api.telegram.org/botT0KEN/getFile", {"file_id": "TG-PHOTO"}),
    ]
    assert captured["gets"] == [
        "https://api.telegram.org/file/botT0KEN/voice/file_7.ogg",
        "https://api.telegram.org/file/botT0KEN/photos/file_2.jpg",
    ]


def test_download_media_refuses_without_token(monkeypatch):
    def fail(*a, **k):
        raise AssertionError("no HTTP call may happen without a token")

    monkeypatch.setattr(httpx, "post", fail)
    monkeypatch.setattr(httpx, "get", fail)
    with patch_settings(telegram_bot_token=""):
        with pytest.raises(NotConfiguredError):
            telegram.download_media("TG-VOICE")


def test_download_media_api_rejection_is_upstream(monkeypatch):
    monkeypatch.setattr(
        httpx, "post", lambda *a, **kw: _FakeResponse({"ok": False}, status=200)
    )
    with patch_settings(telegram_bot_token="T0KEN"):
        with pytest.raises(UpstreamError):
            telegram.download_media("TG-VOICE")


# ---- Inline keyboards: payment buttons on the order message -----------------

def test_send_text_message_can_carry_inline_keyboard(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["json"] = json
        return _FakeResponse()

    monkeypatch.setattr(httpx, "post", fake_post)
    with patch_settings(telegram_bot_token="T0KEN"):
        telegram.send_text_message(7, "Order confirmed!", reply_markup={
            "inline_keyboard": [[{"text": "💵 Cash received",
                                  "callback_data": "cash:order-1"}]]})

    assert captured["json"]["reply_markup"]["inline_keyboard"][0][0] == {
        "text": "💵 Cash received", "callback_data": "cash:order-1"}
    # A plain send (no markup) must not carry the key at all — the
    # no-keyboard path is asserted by test_send_text_message_posts_to_bot_api.


def test_answer_callback_and_edit_message_hit_bot_api(monkeypatch):
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append((url, json))
        return _FakeResponse()

    monkeypatch.setattr(httpx, "post", fake_post)
    with patch_settings(telegram_bot_token="T0KEN"):
        telegram.answer_callback_query("cb-1", "Payment confirmed ✓")
        telegram.edit_message_text(-10055, 42,
                                    "Order confirmed!\n\n✓ Cash payment confirmed.")

    assert calls[0][0].endswith("/answerCallbackQuery")
    assert calls[0][1] == {"callback_query_id": "cb-1",
                           "text": "Payment confirmed ✓", "show_alert": False}
    assert calls[1][0].endswith("/editMessageText")
    assert calls[1][1]["chat_id"] == -10055 and calls[1][1]["message_id"] == 42
    # An empty keyboard REMOVES the buttons so a tap can't be repeated.
    assert calls[1][1]["reply_markup"] == {"inline_keyboard": []}


def test_extract_callback_unwraps_the_tap():
    tap = telegram.extract_callback({"callback_query": {
        "id": "cb9", "data": "cash:order-1",
        "message": {"message_id": 42, "text": "Order confirmed!",
                    "chat": {"id": -1001}}}})
    assert tap == {"callback_id": "cb9", "chat_id": "-1001", "message_id": 42,
                   "text": "Order confirmed!", "data": "cash:order-1"}
    # Ordinary messages and malformed callbacks are ignored.
    assert telegram.extract_callback({"message": {"text": "hi", "chat": {"id": 1}}}) is None
    assert telegram.extract_callback({"callback_query": {"id": "x"}}) is None


def test_polling_subscribes_to_callback_queries(monkeypatch):
    """getUpdates must ask for callback_query too, or button taps never
    arrive on localhost polling (webhook delivery gets them by default)."""
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["json"] = json
        return _FakeResponse({"ok": True, "result": []})

    monkeypatch.setattr(httpx, "post", fake_post)
    with patch_settings(telegram_bot_token="T0KEN"):
        telegram.fetch_recent_updates(offset=0, timeout_seconds=0)

    assert set(captured["json"]["allowed_updates"]) == {"message", "callback_query"}
