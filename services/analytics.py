"""
Pure analytics computations for retention, engagement, and operational impact.
No external dependencies - all analytics are derived from the existing database.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from sqlalchemy import func

from database import db as database
from database.models import (
    ConversationMessage,
    Escalation,
    Medication,
    MedicationEvent,
    RefillRequest,
    ScheduledOutreach,
    Session,
    User,
    Vital,
)


_INDUSTRY_BASELINE = [
    {"day": 0, "retention_pct": 100.0},
    {"day": 7, "retention_pct": 75.0},
    {"day": 14, "retention_pct": 60.0},
    {"day": 21, "retention_pct": 55.0},
    {"day": 30, "retention_pct": 50.0},
]
_ACTIVE_REFILL_STATUSES = ("due", "reminded", "requested", "flagged_for_team", "confirmed")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(value: datetime | None = None) -> str:
    normalized = value.astimezone(timezone.utc) if value is not None else _utc_now()
    return normalized.isoformat().replace("+00:00", "Z")


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _as_utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _safe_pct(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round((float(numerator) / float(denominator)) * 100.0, 1)


def _real_sessions_subquery(db):
    return (
        db.query(
            Session.id.label("session_id"),
            Session.user_id.label("user_id"),
            Session.started_at.label("started_at"),
            Session.ended_at.label("ended_at"),
            func.max(ConversationMessage.created_at).label("last_message_at"),
        )
        .join(ConversationMessage, ConversationMessage.session_id == Session.id)
        .filter(Session.user_id.isnot(None))
        .group_by(
            Session.id,
            Session.user_id,
            Session.started_at,
            Session.ended_at,
        )
        .subquery()
    )


def _count_active_users_in_range(start_dt: datetime, end_dt: datetime) -> int:
    with database.SessionLocal() as db:
        real_sessions = _real_sessions_subquery(db)
        session_users = (
            db.query(real_sessions.c.user_id.label("user_id"))
            .filter(real_sessions.c.started_at >= start_dt, real_sessions.c.started_at < end_dt)
        )
        vital_users = (
            db.query(Vital.user_id.label("user_id"))
            .filter(Vital.user_id.isnot(None), Vital.recorded_at >= start_dt, Vital.recorded_at < end_dt)
        )
        active_users = session_users.union(vital_users).subquery()
        total = db.query(func.count()).select_from(active_users).scalar()
        return int(total or 0)


def compute_retention_curve(days: int = 30) -> dict:
    bounded_days = max(0, min(int(days), 365))
    cohort_users = database.get_all_users_with_activity(days_back=90)
    total_patients = len(cohort_users)

    curve: list[dict] = []
    for day in range(bounded_days + 1):
        active_patients = 0
        for user in cohort_users:
            first_session_at = _parse_datetime(user.get("first_session_date"))
            last_activity_at = _parse_datetime(user.get("last_activity_date"))
            if first_session_at is None or last_activity_at is None:
                continue
            active_offset = max((last_activity_at.date() - first_session_at.date()).days, 0)
            if active_offset >= day:
                active_patients += 1

        curve.append(
            {
                "day": day,
                "retention_pct": _safe_pct(active_patients, total_patients),
                "active_patients": active_patients,
            }
        )

    return {
        "curve": curve,
        "total_patients": total_patients,
        "industry_baseline": list(_INDUSTRY_BASELINE),
        "computed_at": _utc_iso(),
    }


def compute_engagement_metrics() -> dict:
    now = _utc_now()
    cutoff_7 = now - timedelta(days=7)
    cutoff_10 = now - timedelta(days=10)
    cutoff_30 = now - timedelta(days=30)

    with database.SessionLocal() as db:
        total_patients = int(db.query(func.count(User.id)).scalar() or 0)
        real_sessions = _real_sessions_subquery(db)
        total_sessions = int(db.query(func.count(real_sessions.c.session_id)).scalar() or 0)
        total_vitals = int(db.query(func.count(Vital.id)).filter(Vital.user_id.isnot(None)).scalar() or 0)

        patients_with_medication = int(
            db.query(func.count(func.distinct(Medication.user_id)))
            .filter(Medication.status == "active")
            .scalar()
            or 0
        )
        patients_on_track = int(
            db.query(func.count(func.distinct(MedicationEvent.user_id)))
            .join(Medication, Medication.id == MedicationEvent.medication_id)
            .filter(
                Medication.status == "active",
                MedicationEvent.event_type == "dose_taken",
                MedicationEvent.recorded_at >= cutoff_7,
            )
            .scalar()
            or 0
        )

        last_session_summary = (
            db.query(
                real_sessions.c.user_id.label("user_id"),
                func.max(real_sessions.c.started_at).label("last_session_at"),
            )
            .group_by(real_sessions.c.user_id)
            .subquery()
        )
        last_vital_summary = (
            db.query(
                Vital.user_id.label("user_id"),
                func.max(Vital.recorded_at).label("last_vital_at"),
            )
            .group_by(Vital.user_id)
            .subquery()
        )
        active_medication_users = (
            db.query(
                Medication.user_id.label("user_id"),
                func.min(Medication.start_date).label("earliest_start_date"),
            )
            .filter(Medication.status == "active")
            .group_by(Medication.user_id)
            .subquery()
        )
        risk_rows = (
            db.query(
                active_medication_users.c.user_id,
                active_medication_users.c.earliest_start_date,
                last_session_summary.c.last_session_at,
                last_vital_summary.c.last_vital_at,
            )
            .outerjoin(last_session_summary, last_session_summary.c.user_id == active_medication_users.c.user_id)
            .outerjoin(last_vital_summary, last_vital_summary.c.user_id == active_medication_users.c.user_id)
            .all()
        )

        patients_at_risk = 0
        for row in risk_rows:
            last_session_at = _as_utc_datetime(row.last_session_at)
            last_vital_at = _as_utc_datetime(row.last_vital_at)
            candidates = [candidate for candidate in (last_session_at, last_vital_at) if candidate is not None]
            latest_activity = max(candidates) if candidates else None
            if latest_activity is None and row.earliest_start_date is not None:
                latest_activity = datetime.combine(row.earliest_start_date, time.min, tzinfo=timezone.utc)
            if latest_activity is not None and latest_activity <= cutoff_10:
                patients_at_risk += 1

        escalations_resolved_last_7_days = int(
            db.query(func.count(Escalation.id))
            .filter(
                Escalation.status == "resolved",
                Escalation.resolved_at.isnot(None),
                Escalation.resolved_at >= cutoff_7,
            )
            .scalar()
            or 0
        )
        outreach_completed_last_7_days = int(
            db.query(func.count(ScheduledOutreach.id))
            .filter(
                ScheduledOutreach.status == "completed",
                ScheduledOutreach.completed_at.isnot(None),
                ScheduledOutreach.completed_at >= cutoff_7,
            )
            .scalar()
            or 0
        )
        outreach_pending = int(
            db.query(func.count(ScheduledOutreach.id))
            .filter(ScheduledOutreach.status == "pending")
            .scalar()
            or 0
        )
        refills_pending = int(
            db.query(func.count(RefillRequest.id))
            .filter(RefillRequest.status.in_(_ACTIVE_REFILL_STATUSES))
            .scalar()
            or 0
        )

    escalation_counts = database.count_escalations_by_status()
    avg_sessions_per_patient = round((float(total_sessions) / float(total_patients)), 1) if total_patients else 0.0
    avg_vitals_per_patient = round((float(total_vitals) / float(total_patients)), 1) if total_patients else 0.0

    return {
        "total_patients": total_patients,
        "active_last_7_days": _count_active_users_in_range(cutoff_7, now),
        "active_last_30_days": _count_active_users_in_range(cutoff_30, now),
        "avg_sessions_per_patient": avg_sessions_per_patient,
        "avg_vitals_per_patient": avg_vitals_per_patient,
        "patients_with_medication": patients_with_medication,
        "patients_on_track": patients_on_track,
        "patients_at_risk": patients_at_risk,
        "escalations_open": int(escalation_counts.get("open", 0)),
        "escalations_resolved_last_7_days": escalations_resolved_last_7_days,
        "outreach_completed_last_7_days": outreach_completed_last_7_days,
        "outreach_pending": outreach_pending,
        "refills_pending": refills_pending,
        "computed_at": _utc_iso(),
    }


def compute_revenue_impact(
    total_patients: int,
    avg_revenue_per_patient: float,
    retention_lift_pct: float = 10.0,
) -> dict:
    baseline_retention_30day = 50.0
    improved_retention_30day = min(max(baseline_retention_30day + float(retention_lift_pct), 0.0), 100.0)
    monthly_recovered_revenue = float(total_patients) * float(avg_revenue_per_patient) * (
        (improved_retention_30day - baseline_retention_30day) / 100.0
    )

    return {
        "total_patients": int(total_patients),
        "avg_revenue_per_patient": round(float(avg_revenue_per_patient), 2),
        "baseline_retention_30day": baseline_retention_30day,
        "improved_retention_30day": round(improved_retention_30day, 1),
        "retention_lift_pct": round(float(retention_lift_pct), 1),
        "monthly_recovered_revenue": round(monthly_recovered_revenue, 2),
        "annual_recovered_revenue": round(monthly_recovered_revenue * 12.0, 2),
    }


def compute_support_deflection() -> dict:
    now = _utc_now()
    cutoff_30 = now - timedelta(days=30)

    with database.SessionLocal() as db:
        real_sessions = _real_sessions_subquery(db)
        session_rows = (
            db.query(
                real_sessions.c.session_id,
                real_sessions.c.started_at,
                real_sessions.c.ended_at,
                real_sessions.c.last_message_at,
            )
            .filter(real_sessions.c.started_at >= cutoff_30, real_sessions.c.started_at < now)
            .all()
        )
        escalated_to_clinician = int(
            db.query(func.count(func.distinct(real_sessions.c.session_id)))
            .join(Escalation, Escalation.session_id == real_sessions.c.session_id)
            .filter(real_sessions.c.started_at >= cutoff_30, real_sessions.c.started_at < now)
            .scalar()
            or 0
        )

    total_sessions_last_30_days = database.count_sessions_in_range(cutoff_30, now)
    resolved_without_escalation = max(total_sessions_last_30_days - escalated_to_clinician, 0)

    durations: list[float] = []
    for row in session_rows:
        started_at = _as_utc_datetime(row.started_at)
        if started_at is None:
            continue
        completed_at = _as_utc_datetime(row.ended_at) or _as_utc_datetime(row.last_message_at)
        if completed_at is None or completed_at <= started_at:
            continue
        durations.append((completed_at - started_at).total_seconds())

    avg_session_duration_seconds = int(round(sum(durations) / len(durations))) if durations else 0

    return {
        "total_sessions_last_30_days": total_sessions_last_30_days,
        "resolved_without_escalation": resolved_without_escalation,
        "escalated_to_clinician": escalated_to_clinician,
        "deflection_rate_pct": _safe_pct(resolved_without_escalation, total_sessions_last_30_days),
        "avg_session_duration_seconds": avg_session_duration_seconds,
        "top_resolved_topics": database.get_session_topics_summary(cutoff_30, now)[:3],
        "computed_at": _utc_iso(),
    }
