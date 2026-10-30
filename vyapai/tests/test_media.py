"""Media adapter: the local backend must round-trip bytes by opaque key,
and the R2 backend must fail loud (never silently fall back) when unconfigured."""
import sys

import pytest

from app.integrations import media
from app.integrations.errors import NotConfiguredError, UpstreamError
from tests.conftest import patch_settings


def test_local_roundtrip(tmp_path):
    with patch_settings(media_backend="local", media_local_dir=str(tmp_path / ".media")):
        key = media.save(b"voice-note-bytes", ext="ogg", merchant_id="m1")
        assert key.startswith("local://merchants/m1/")
        assert media.open_object(key) == b"voice-note-bytes"


def test_local_unattributed_namespace(tmp_path):
    with patch_settings(media_backend="local", media_local_dir=str(tmp_path / ".media")):
        key = media.save(b"x", ext="jpg")
        assert "unattributed" in key


def test_local_delete(tmp_path):
    with patch_settings(media_backend="local", media_local_dir=str(tmp_path / ".media")):
        key = media.save(b"gone", ext="txt")
        media.delete(key)
        with pytest.raises(UpstreamError):
            media.open_object(key)


def test_r2_backend_raises_not_configured_when_env_missing():
    with patch_settings(media_backend="r2", r2_account_id=None):
        with pytest.raises(NotConfiguredError):
            media.save(b"x", ext="ogg", merchant_id="m1")


def test_r2_backend_refuses_without_boto(monkeypatch):
    """boto3 is an optional dependency; MEDIA_BACKEND=r2 without it fails
    loudly instead of pretending to save."""
    monkeypatch.setitem(sys.modules, "boto3", None)
    with patch_settings(
        media_backend="r2",
        r2_account_id="acct",
        r2_access_key_id="key",
        r2_secret_access_key="secret",
        r2_bucket="bucket",
    ):
        with pytest.raises(UpstreamError, match="boto3"):
            media.save(b"x", ext="ogg", merchant_id="m1")
