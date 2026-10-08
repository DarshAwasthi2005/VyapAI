"""
Decides *when* and at what tone-level to send a payment reminder. Kept as
pure logic (no WhatsApp/DB calls) so escalation timing is independently
testable — the actual send happens via integrations/whatsapp.py, invoked
from a scheduled job (see infra/k8s for the CronJob).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

# Hours after order-confirmation before each escalation tier fires.
ESCALATION_SCHEDULE_HOURS = {
    0: 24,   # gentle nudge, 1 day after an unpaid confirmed order
    1: 72,   # firmer reminder, 3 days
    2: 168,  # final notice, 7 days
}
MAX_ESCALATION_LEVEL = max(ESCALATION_SCHEDULE_HOURS)


@dataclass
class ReminderDecision:
    should_send: bool
    next_escalation_level: int | None
    reason: str


def decide_next_reminder(
    *,
    order_confirmed_at: datetime,
    current_escalation_level: int,
    last_reminder_sent_at: datetime | None,
    now: datetime | None = None,
) -> ReminderDecision:
    now = now or datetime.utcnow()

    if current_escalation_level > MAX_ESCALATION_LEVEL:
        return ReminderDecision(False, None, "Max escalation reached — hand off to human collections")

    next_level = current_escalation_level
    threshold_hours = ESCALATION_SCHEDULE_HOURS[next_level]
    due_at = order_confirmed_at + timedelta(hours=threshold_hours)

    if now < due_at:
        return ReminderDecision(False, None, f"Not due until {due_at.isoformat()}")

    # Don't re-send the same tier within 12h of the last message even if
    # somehow called again (idempotency guard for the scheduler).
    if last_reminder_sent_at and (now - last_reminder_sent_at) < timedelta(hours=12):
        return ReminderDecision(False, None, "Reminder already sent recently at this tier")

    return ReminderDecision(True, next_level, f"Due for escalation level {next_level}")
