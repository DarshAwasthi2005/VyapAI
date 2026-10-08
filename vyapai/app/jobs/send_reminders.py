"""
Run hourly via the CronJob in infra/k8s/cronjob-reminders.yaml (or a plain
cron entry / systemd timer outside Kubernetes).

For every order with a pending payment, decides whether a reminder is due
and — if so — sends it and bumps the escalation level. All the *timing*
logic lives in services/collections.py and is unit-tested there; this
script is just the DB-loop + send glue.
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select

from app.db.models import Order, Payment, PaymentStatus
from app.db.session import get_session
from app.integrations import whatsapp
from app.integrations.errors import NotConfiguredError, UpstreamError
from app.services.collections import decide_next_reminder

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vyapai.jobs.send_reminders")


def run() -> None:
    sent, skipped, failed = 0, 0, 0
    with get_session() as db:
        pending = db.execute(
            select(Payment, Order)
            .join(Order, Payment.order_id == Order.id)
            .where(Payment.status == PaymentStatus.PENDING)
        ).all()

        for payment, order in pending:
            decision = decide_next_reminder(
                order_confirmed_at=order.created_at,
                current_escalation_level=payment.reminder_escalation_level,
                last_reminder_sent_at=payment.last_reminder_sent_at,
            )
            if not decision.should_send:
                skipped += 1
                continue

            try:
                whatsapp.send_payment_reminder(
                    to_msisdn=order.buyer_whatsapp_number,
                    amount_inr=payment.amount,
                    upi_link=payment.upi_deep_link,
                    escalation_level=decision.next_escalation_level,
                )
                payment.reminder_escalation_level = decision.next_escalation_level + 1
                payment.last_reminder_sent_at = datetime.utcnow()
                sent += 1
            except (NotConfiguredError, UpstreamError) as e:
                logger.warning("Reminder send failed for order %s: %s", order.id, e)
                failed += 1

    logger.info("Reminder sweep done: sent=%d skipped=%d failed=%d", sent, skipped, failed)


if __name__ == "__main__":
    run()
