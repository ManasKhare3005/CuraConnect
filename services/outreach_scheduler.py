"""
Outreach scheduling engine for GLP-1 patient care.

Generates scheduled outreach items based on medication events and treatment milestones.
Does NOT send messages itself - it creates ScheduledOutreach records that a separate
worker process would pick up and execute.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from database import db as database
from database.models import MedicationEvent, Session, SideEffect


def _coerce_datetime_utc(value: date | datetime | str) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, date):
        now = datetime.now(timezone.utc)
        return datetime.combine(
            value,
            time(hour=now.hour, minute=now.minute, second=0, microsecond=0, tzinfo=timezone.utc),
        )
    parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _schedule_items(
    user_id: int,
    medication_id: int | None,
    items: list[dict],
) -> list[dict]:
    scheduled: list[dict] = []
    for item in items:
        scheduled.append(
            database.create_outreach(
                user_id=user_id,
                medication_id=medication_id,
                outreach_type=item["outreach_type"],
                scheduled_at=item["scheduled_at"],
                channel=item.get("channel", "call"),
                priority=item.get("priority", 5),
                context_json=item.get("context_json"),
            )
        )
    return scheduled


def schedule_new_patient_outreach(user_id: int, medication_id: int, start_date: date | datetime | str) -> list[dict]:
    anchor = _coerce_datetime_utc(start_date)
    return _schedule_items(
        user_id,
        medication_id,
        [
            {
                "outreach_type": "day_3_checkin",
                "scheduled_at": anchor + timedelta(days=3),
                "priority": 3,
                "context_json": {
                    "reason": "new_patient_onboarding",
                    "message_hint": "How's your first injection going? Any questions about injection technique?",
                },
            },
            {
                "outreach_type": "week_1_checkin",
                "scheduled_at": anchor + timedelta(days=7),
                "priority": 3,
                "context_json": {
                    "reason": "new_patient_onboarding",
                    "message_hint": "One week in! How are you feeling? Any side effects?",
                },
            },
            {
                "outreach_type": "week_2_side_effect_check",
                "scheduled_at": anchor + timedelta(days=14),
                "priority": 2,
                "context_json": {
                    "reason": "new_patient_onboarding",
                    "message_hint": (
                        "Checking in on week 2. Nausea and GI symptoms are very common at this stage "
                        "and usually improve. How are you doing?"
                    ),
                },
            },
            {
                "outreach_type": "week_1_checkin",
                "scheduled_at": anchor + timedelta(days=21),
                "priority": 4,
                "context_json": {
                    "reason": "new_patient_onboarding",
                    "message_hint": "Three weeks in. Any updates on how you're feeling?",
                },
            },
            {
                "outreach_type": "refill_reminder",
                "scheduled_at": anchor + timedelta(days=28),
                "priority": 3,
                "context_json": {
                    "reason": "new_patient_onboarding",
                    "message_hint": "Your first month is almost up. Is your refill on track?",
                },
            },
        ],
    )


def schedule_titration_followup(user_id: int, medication_id: int, new_step: int, dose_mg: float) -> list[dict]:
    now = datetime.now(timezone.utc)
    return _schedule_items(
        user_id,
        medication_id,
        [
            {
                "outreach_type": "titration_step_followup",
                "scheduled_at": now + timedelta(days=2),
                "priority": 2,
                "context_json": {
                    "new_step": int(new_step),
                    "dose_mg": float(dose_mg),
                    "message_hint": "Side effects often spike after dose increases.",
                },
            },
            {
                "outreach_type": "week_1_checkin",
                "scheduled_at": now + timedelta(days=7),
                "priority": 3,
                "context_json": {
                    "new_step": int(new_step),
                    "dose_mg": float(dose_mg),
                    "message_hint": "Checking in one week after the dose increase.",
                },
            },
        ],
    )


def schedule_missed_dose_followup(user_id: int, medication_id: int) -> dict:
    return database.create_outreach(
        user_id=user_id,
        medication_id=medication_id,
        outreach_type="missed_dose_followup",
        scheduled_at=datetime.now(timezone.utc) + timedelta(hours=24),
        priority=2,
        context_json={
            "reason": "missed_dose_report",
            "message_hint": "Check in after a missed dose and help the patient get back on schedule.",
        },
    )


def schedule_refill_reminder(
    user_id: int,
    medication_id: int,
    refill_due_date: date | datetime | str,
) -> list[dict]:
    refill_due_at = _coerce_datetime_utc(refill_due_date)
    return _schedule_items(
        user_id,
        medication_id,
        [
            {
                "outreach_type": "refill_reminder",
                "scheduled_at": refill_due_at - timedelta(days=5),
                "priority": 3,
                "context_json": {"reason": "refill_due", "days_before_due": 5},
            },
            {
                "outreach_type": "refill_reminder",
                "scheduled_at": refill_due_at - timedelta(days=1),
                "priority": 2,
                "context_json": {"reason": "refill_due", "days_before_due": 1},
            },
        ],
    )


def schedule_retention_risk_outreach(user_id: int, medication_id: int | None, reason: str) -> dict:
    return database.create_outreach(
        user_id=user_id,
        medication_id=medication_id,
        outreach_type="retention_risk",
        scheduled_at=datetime.now(timezone.utc),
        priority=1,
        context_json={"reason": reason},
    )


def detect_churn_risk(user_id: int) -> tuple[bool, str]:
    active_medications = database.get_active_medications(user_id)
    if not active_medications:
        return False, ""

    now = datetime.now(timezone.utc)
    cutoff_10_days = now - timedelta(days=10)
    cutoff_14_days = now - timedelta(days=14)

    with database.SessionLocal() as db:
        latest_session = (
            db.query(Session)
            .filter(Session.user_id == user_id)
            .order_by(Session.started_at.desc(), Session.id.desc())
            .first()
        )

        if latest_session is None:
            oldest_active_start = None
            for medication in active_medications:
                start_date = medication.get("start_date")
                if not start_date:
                    continue
                candidate = _coerce_datetime_utc(start_date)
                if oldest_active_start is None or candidate < oldest_active_start:
                    oldest_active_start = candidate
            if oldest_active_start and oldest_active_start <= cutoff_10_days:
                return True, "no_activity"
        else:
            latest_activity = latest_session.ended_at or latest_session.started_at
            if latest_activity is not None:
                if latest_activity.tzinfo is None:
                    latest_activity = latest_activity.replace(tzinfo=timezone.utc)
                else:
                    latest_activity = latest_activity.astimezone(timezone.utc)
                if latest_activity <= cutoff_10_days:
                    return True, "no_activity"

        missed_dose_count = (
            db.query(MedicationEvent)
            .filter(
                MedicationEvent.user_id == user_id,
                MedicationEvent.event_type == "dose_missed",
                MedicationEvent.recorded_at >= cutoff_14_days,
            )
            .count()
        )
        if missed_dose_count >= 2:
            return True, "missed_doses"

        severe_side_effect = (
            db.query(SideEffect)
            .filter(SideEffect.user_id == user_id, SideEffect.severity == "severe")
            .order_by(SideEffect.reported_at.desc(), SideEffect.id.desc())
            .first()
        )
        if severe_side_effect and severe_side_effect.reported_at is not None:
            reported_at = severe_side_effect.reported_at
            if reported_at.tzinfo is None:
                reported_at = reported_at.replace(tzinfo=timezone.utc)
            else:
                reported_at = reported_at.astimezone(timezone.utc)

            follow_up_session = (
                db.query(Session)
                .filter(Session.user_id == user_id, Session.started_at > reported_at)
                .order_by(Session.started_at.asc(), Session.id.asc())
                .first()
            )
            if follow_up_session is None:
                return True, "severe_side_effect"

    return False, ""
