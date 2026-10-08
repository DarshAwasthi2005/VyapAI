from datetime import datetime, timedelta

from app.services.collections import decide_next_reminder


def test_no_reminder_before_threshold():
    now = datetime.utcnow()
    decision = decide_next_reminder(
        order_confirmed_at=now - timedelta(hours=2),
        current_escalation_level=0,
        last_reminder_sent_at=None,
        now=now,
    )
    assert not decision.should_send


def test_gentle_reminder_after_24h():
    now = datetime.utcnow()
    decision = decide_next_reminder(
        order_confirmed_at=now - timedelta(hours=25),
        current_escalation_level=0,
        last_reminder_sent_at=None,
        now=now,
    )
    assert decision.should_send
    assert decision.next_escalation_level == 0


def test_firm_reminder_after_72h():
    now = datetime.utcnow()
    decision = decide_next_reminder(
        order_confirmed_at=now - timedelta(hours=80),
        current_escalation_level=1,
        last_reminder_sent_at=now - timedelta(hours=20),
        now=now,
    )
    assert decision.should_send
    assert decision.next_escalation_level == 1


def test_no_duplicate_within_12h():
    now = datetime.utcnow()
    decision = decide_next_reminder(
        order_confirmed_at=now - timedelta(hours=30),
        current_escalation_level=0,
        last_reminder_sent_at=now - timedelta(hours=1),
        now=now,
    )
    assert not decision.should_send


def test_max_escalation_hands_off_to_human():
    now = datetime.utcnow()
    decision = decide_next_reminder(
        order_confirmed_at=now - timedelta(hours=300),
        current_escalation_level=3,
        last_reminder_sent_at=None,
        now=now,
    )
    assert not decision.should_send
    assert "human" in decision.reason.lower()
