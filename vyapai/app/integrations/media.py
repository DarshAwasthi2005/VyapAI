"""
Media storage adapter — where merchant voice notes and bill photos live.

The rest of the codebase never touches buckets or paths directly; it calls
`save(bytes, ext)` and gets back a key it can hand to `open_object()` later.
Today nothing was storing these at all — they passed through memory and
vanished, which made audit, re-extraction, and debugging impossible.

Backends, chosen by MEDIA_BACKEND:

  1. "local" (default, zero-config): plain filesystem under MEDIA_LOCAL_DIR.
     Everything (tests, local dev) works with no external service at all.

  2. "r2": Cloudflare R2 through the S3-compatible API, using its 10-GB free
     tier. Requires the optional `boto3` dependency and the four R2_* env
     vars; raises NotConfiguredError loudly otherwise (this repo never
     silently fakes a store).

Keys are namespaced by merchant and date: merchants/{id}/{yyyy}/{mm}/{uuid}.{ext}
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings
from app.integrations.errors import NotConfiguredError, UpstreamError


def save(content: bytes, *, ext: str, merchant_id: str | None = None) -> str:
    """Persist media bytes and return the storage key (fully opaque)."""
    if settings.media_backend == "r2":
        return _save_r2(content, ext=ext, merchant_id=merchant_id)
    return _save_local(content, ext=ext, merchant_id=merchant_id)


def open_object(key: str) -> bytes:
    """Read media back by key. Used by review screens and re-extraction."""
    if key.startswith("r2://"):
        return _open_r2(key)
    return _open_local(key)


# --- local filesystem backend -------------------------------------------


def _local_path(key: str) -> Path:
    rel = key.removeprefix("local://")
    return Path(settings.media_local_dir) / rel


def _save_local(content: bytes, *, ext: str, merchant_id: str | None) -> str:
    now = datetime.now(timezone.utc)
    rel = "unattributed" if not merchant_id else f"merchants/{merchant_id}"
    key = (
        f"local://{rel}/{now:%Y/%m}/{uuid.uuid4().hex}"
        f".{ext.lstrip('.') or 'bin'}"
    )
    path = _local_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return key


def _open_local(key: str) -> bytes:
    path = _local_path(key)
    try:
        return path.read_bytes()
    except OSError as e:
        raise UpstreamError(f"Stored media missing at {key}: {e}") from e


# --- Cloudflare R2 backend (optional dependency, loaded lazily) ----------


def _r2_client():
    if not (
        settings.r2_account_id
        and settings.r2_access_key_id
        and settings.r2_secret_access_key
        and settings.r2_bucket
    ):
        raise NotConfiguredError(
            "R2 media store",
            ["R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"],
        )
    try:
        import boto3  # optional dependency — see requirements.txt
    except ImportError as e:
        raise UpstreamError(
            "MEDIA_BACKEND=r2 requires boto3: pip install boto3 "
            "(kept optional so the default install stays dependency-light)."
        ) from e
    endpoint = f"https://{settings.r2_account_id}.r2.cloudflarestorage.com"
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        region_name="auto",
    )


def _save_r2(content: bytes, *, ext: str, merchant_id: str | None) -> str:
    now = datetime.now(timezone.utc)
    rel = "unattributed" if not merchant_id else f"merchants/{merchant_id}"
    key = f"{rel}/{now:%Y/%m}/{uuid.uuid4().hex}.{ext.lstrip('.') or 'bin'}"
    client = _r2_client()
    try:
        client.put_object(Bucket=settings.r2_bucket, Key=key, Body=content)
    except Exception as e:  # boto3 raises a wide exception zoo
        raise UpstreamError(f"R2 put_object failed for {key}: {e}") from e
    return f"r2://{settings.r2_bucket}/{key}"


def _open_r2(key: str) -> bytes:
    bucket, _, object_key = key.removeprefix("r2://").partition("/")
    client = _r2_client()
    try:
        resp = client.get_object(Bucket=bucket, Key=object_key)
        return resp["Body"].read()
    except Exception as e:
        raise UpstreamError(f"R2 get_object failed for {key}: {e}") from e


def delete(key: str) -> None:
    """Best-effort removal (e.g., retention policy). Local: unlink if present."""
    try:
        if key.startswith("r2://"):
            bucket, _, object_key = key.removeprefix("r2://").partition("/")
            _r2_client().delete_object(Bucket=bucket, Key=object_key)
        else:
            path = _local_path(key)
            if path.exists():
                os.unlink(path)
    except Exception as e:
        raise UpstreamError(f"Media delete failed for {key}: {e}") from e
