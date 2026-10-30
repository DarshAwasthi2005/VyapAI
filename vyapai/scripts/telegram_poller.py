"""Telegram long-polling runner — the "always on" local/hosted inbox.

The FastAPI webhook needs a public HTTPS URL; the dashboard wizard only polls
while its link dialog is open. This script is the third option: a long-running
process that holds the getUpdates connection open (server→Telegram, so no
public URL, no tunnel) and feeds every update through the EXACT same handler
the real webhook uses — `/start` pairing, slash commands, text/voice/photo
orders, and inline payment-button taps.

Usage:
    python scripts/telegram_poller.py                 # runs forever
    python scripts/telegram_poller.py --once          # drain, then exit

Requires TELEGRAM_BOT_TOKEN in the environment/.env. If a webhook is set on
the bot, Telegram refuses getUpdates — call deleteWebhook first (the script
explains this if it happens).
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from pathlib import Path

from app.config import settings
from app.db.session import SessionLocal
from app.integrations import telegram
from app.integrations.errors import NotConfiguredError, UpstreamError

# Update ids are monotonically increasing; remembering the last handled one
# means a restart never re-processes (and never re-charges) an old order.
OFFSET_FILE = Path(os.environ.get("TELEGRAM_OFFSET_FILE", ".telegram-offset"))

log = logging.getLogger("telegram-poller")


def _load_offset() -> int:
    try:
        return int(OFFSET_FILE.read_text().strip())
    except (OSError, ValueError):
        return 0


def _save_offset(value: int) -> None:
    try:
        OFFSET_FILE.write_text(str(value))
    except OSError as e:  # read-only fs: keep running, just lose the memory
        log.warning("could not persist offset: %s", e)


def drain_once(*, timeout_seconds: int = 25) -> int:
    """Fetch and handle one batch. Returns how many updates were handled."""
    from app.api.webhooks import _handle_telegram_update

    offset = _load_offset()
    updates = telegram.fetch_recent_updates(offset=offset, timeout_seconds=timeout_seconds)
    handled = 0
    for update in updates:
        uid = int(update.get("update_id", 0))
        db = SessionLocal()
        try:
            result = _handle_telegram_update(db, update)
            db.commit()
            if result.get("handled"):
                handled += 1
                log.info("handled update %s: %s", uid, result)
        except Exception:  # one bad message must not kill the runner
            db.rollback()
            log.exception("failed to handle update %s", uid)
        finally:
            db.close()
        if uid:
            _save_offset(uid + 1)
    return handled


def main() -> None:
    parser = argparse.ArgumentParser(description="VyapAI Telegram long-polling runner")
    parser.add_argument("--once", action="store_true", help="drain pending updates, then exit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not settings.telegram_bot_token:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN is not set. Add it to .env (from @BotFather), then rerun."
        )

    log.info("polling Telegram as @%s", telegram.get_bot_username() or "unknown-bot")
    while True:
        try:
            drain_once()
        except NotConfiguredError as e:
            raise SystemExit(str(e)) from e
        except UpstreamError as e:
            # Typically: a webhook is registered on the bot (getUpdates is
            # mutually exclusive with it), or Telegram is briefly unreachable.
            log.error("%s", e)
            log.error("if a webhook is set, run: (see deleteWebhook in docs/GO_LIVE.md)")
            time.sleep(10)
        except KeyboardInterrupt:
            log.info("stopped by operator")
            return
        if args.once:
            return
        time.sleep(1)  # safety margin when Telegram returns immediately empty


if __name__ == "__main__":
    main()
