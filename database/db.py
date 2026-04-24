import json
import os
from datetime import date, datetime, time, timedelta, timezone
from sqlalchemy import create_engine, func, inspect, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import joinedload, sessionmaker, Session as DBSession
from dotenv import load_dotenv

from database.models import (
    AuditLog,
    Base,
    ConversationMessage,
    DoctorRecommendation,
    Escalation,
    Medication,
    MedicationEvent,
    RefillRequest,
    ScheduledOutreach,
    Session,
    SideEffect,
    User,
    Vital,
)

load_dotenv()

SQLITE_FALLBACK_URL = "sqlite:///./health_assistant.db"
DATABASE_URL = os.getenv("DATABASE_URL", SQLITE_FALLBACK_URL)


def _create_engine(url: str):
    if url.startswith("sqlite"):
        return create_engine(url, connect_args={"check_same_thread": False})
    return create_engine(url)


engine = _create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)

_ACTIVE_REFILL_STATUSES = {"due", "reminded", "requested", "flagged_for_team", "confirmed"}
_REFILL_TRANSITIONS = {
    "due": {"reminded", "requested"},
    "reminded": {"requested"},
    "requested": {"flagged_for_team"},
    "flagged_for_team": {"confirmed"},
    "confirmed": {"completed"},
    "completed": set(),
    "cancelled": set(),
}


def _ensure_users_auth_columns():
    """Lightweight in-place migration for legacy user tables.

    Older project versions created `users` without auth fields.
    This adds missing nullable columns so auth routes can work without
    requiring manual DB resets.
    """
    inspector = inspect(engine)
    if "users" not in inspector.get_table_names():
        return

    existing_columns = {column["name"] for column in inspector.get_columns("users")}
    statements: list[str] = []

    if "email" not in existing_columns:
        statements.append("ALTER TABLE users ADD COLUMN email VARCHAR(255)")
    if "password_hash" not in existing_columns:
        statements.append("ALTER TABLE users ADD COLUMN password_hash VARCHAR(255)")
    if "dob" not in existing_columns:
        statements.append("ALTER TABLE users ADD COLUMN dob DATE")
    if "weight_kg" not in existing_columns:
        statements.append("ALTER TABLE users ADD COLUMN weight_kg FLOAT")
    if "height_cm" not in existing_columns:
        statements.append("ALTER TABLE users ADD COLUMN height_cm FLOAT")
    if "address" not in existing_columns:
        statements.append("ALTER TABLE users ADD COLUMN address VARCHAR(500)")

    if not statements:
        # Ensure the email index exists even if columns already existed.
        statements.append("CREATE INDEX IF NOT EXISTS ix_users_email ON users (email)")

    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))

        # Index creation is safe to retry and helps lookup performance.
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_users_email ON users (email)"))


def _ensure_sessions_metadata_columns():
    """Add missing timezone metadata columns to legacy `sessions` tables."""
    inspector = inspect(engine)
    if "sessions" not in inspector.get_table_names():
        return

    existing_columns = {column["name"] for column in inspector.get_columns("sessions")}
    statements: list[str] = []

    if "timezone_name" not in existing_columns:
        statements.append("ALTER TABLE sessions ADD COLUMN timezone_name VARCHAR(80)")
    if "utc_offset_minutes" not in existing_columns:
        statements.append("ALTER TABLE sessions ADD COLUMN utc_offset_minutes INTEGER")

    if not statements:
        return

    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def _ensure_medication_tables():
    """Add missing medication tracking columns to legacy tables."""
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())

    medication_columns = {
        "user_id": "ALTER TABLE medications ADD COLUMN user_id INTEGER",
        "drug_name": "ALTER TABLE medications ADD COLUMN drug_name VARCHAR(100)",
        "brand_name": "ALTER TABLE medications ADD COLUMN brand_name VARCHAR(100)",
        "current_dose_mg": "ALTER TABLE medications ADD COLUMN current_dose_mg FLOAT",
        "frequency": "ALTER TABLE medications ADD COLUMN frequency VARCHAR(50)",
        "route": "ALTER TABLE medications ADD COLUMN route VARCHAR(50)",
        "start_date": "ALTER TABLE medications ADD COLUMN start_date DATE",
        "titration_step": "ALTER TABLE medications ADD COLUMN titration_step INTEGER",
        "status": "ALTER TABLE medications ADD COLUMN status VARCHAR(20)",
        "notes": "ALTER TABLE medications ADD COLUMN notes TEXT",
        "created_at": "ALTER TABLE medications ADD COLUMN created_at DATETIME",
        "updated_at": "ALTER TABLE medications ADD COLUMN updated_at DATETIME",
    }
    medication_event_columns = {
        "medication_id": "ALTER TABLE medication_events ADD COLUMN medication_id INTEGER",
        "user_id": "ALTER TABLE medication_events ADD COLUMN user_id INTEGER",
        "event_type": "ALTER TABLE medication_events ADD COLUMN event_type VARCHAR(30)",
        "dose_mg": "ALTER TABLE medication_events ADD COLUMN dose_mg FLOAT",
        "scheduled_at": "ALTER TABLE medication_events ADD COLUMN scheduled_at DATETIME",
        "recorded_at": "ALTER TABLE medication_events ADD COLUMN recorded_at DATETIME",
        "notes": "ALTER TABLE medication_events ADD COLUMN notes TEXT",
    }
    side_effect_columns = {
        "medication_id": "ALTER TABLE side_effects ADD COLUMN medication_id INTEGER",
        "user_id": "ALTER TABLE side_effects ADD COLUMN user_id INTEGER",
        "symptom": "ALTER TABLE side_effects ADD COLUMN symptom VARCHAR(100)",
        "severity": "ALTER TABLE side_effects ADD COLUMN severity VARCHAR(20)",
        "titration_step": "ALTER TABLE side_effects ADD COLUMN titration_step INTEGER",
        "reported_at": "ALTER TABLE side_effects ADD COLUMN reported_at DATETIME",
        "resolved_at": "ALTER TABLE side_effects ADD COLUMN resolved_at DATETIME",
        "notes": "ALTER TABLE side_effects ADD COLUMN notes TEXT",
    }

    def apply_missing_columns(table_name: str, columns: dict[str, str]):
        if table_name not in table_names:
            return
        existing_columns = {column["name"] for column in inspector.get_columns(table_name)}
        statements = [statement for name, statement in columns.items() if name not in existing_columns]
        if not statements:
            return
        with engine.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))

    apply_missing_columns("medications", medication_columns)
    apply_missing_columns("medication_events", medication_event_columns)
    apply_missing_columns("side_effects", side_effect_columns)

    with engine.begin() as connection:
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_medications_user_id ON medications (user_id)"))
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_medication_events_medication_id ON medication_events (medication_id)")
        )
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_medication_events_user_id ON medication_events (user_id)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_side_effects_medication_id ON side_effects (medication_id)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_side_effects_user_id ON side_effects (user_id)"))


def _ensure_outreach_table():
    """Add missing outreach scheduling columns to legacy tables."""
    inspector = inspect(engine)
    if "scheduled_outreach" not in inspector.get_table_names():
        return

    outreach_columns = {
        "user_id": "ALTER TABLE scheduled_outreach ADD COLUMN user_id INTEGER",
        "medication_id": "ALTER TABLE scheduled_outreach ADD COLUMN medication_id INTEGER",
        "outreach_type": "ALTER TABLE scheduled_outreach ADD COLUMN outreach_type VARCHAR(50)",
        "channel": "ALTER TABLE scheduled_outreach ADD COLUMN channel VARCHAR(20)",
        "status": "ALTER TABLE scheduled_outreach ADD COLUMN status VARCHAR(20)",
        "scheduled_at": "ALTER TABLE scheduled_outreach ADD COLUMN scheduled_at DATETIME",
        "attempted_at": "ALTER TABLE scheduled_outreach ADD COLUMN attempted_at DATETIME",
        "completed_at": "ALTER TABLE scheduled_outreach ADD COLUMN completed_at DATETIME",
        "attempt_count": "ALTER TABLE scheduled_outreach ADD COLUMN attempt_count INTEGER",
        "max_attempts": "ALTER TABLE scheduled_outreach ADD COLUMN max_attempts INTEGER",
        "priority": "ALTER TABLE scheduled_outreach ADD COLUMN priority INTEGER",
        "context_json": "ALTER TABLE scheduled_outreach ADD COLUMN context_json TEXT",
        "outcome_summary": "ALTER TABLE scheduled_outreach ADD COLUMN outcome_summary TEXT",
        "session_id": "ALTER TABLE scheduled_outreach ADD COLUMN session_id INTEGER",
        "created_at": "ALTER TABLE scheduled_outreach ADD COLUMN created_at DATETIME",
    }

    existing_columns = {column["name"] for column in inspector.get_columns("scheduled_outreach")}
    statements = [statement for name, statement in outreach_columns.items() if name not in existing_columns]
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_scheduled_outreach_user_id ON scheduled_outreach (user_id)"))
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_scheduled_outreach_medication_id ON scheduled_outreach (medication_id)")
        )
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_scheduled_outreach_status ON scheduled_outreach (status)"))
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_scheduled_outreach_scheduled_at ON scheduled_outreach (scheduled_at)")
        )
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_scheduled_outreach_session_id ON scheduled_outreach (session_id)")
        )


def _ensure_escalation_table():
    """Add missing escalation columns to legacy tables."""
    inspector = inspect(engine)
    if "escalations" not in inspector.get_table_names():
        return

    escalation_columns = {
        "user_id": "ALTER TABLE escalations ADD COLUMN user_id INTEGER",
        "session_id": "ALTER TABLE escalations ADD COLUMN session_id INTEGER",
        "medication_id": "ALTER TABLE escalations ADD COLUMN medication_id INTEGER",
        "severity": "ALTER TABLE escalations ADD COLUMN severity VARCHAR(20)",
        "trigger_reason": "ALTER TABLE escalations ADD COLUMN trigger_reason VARCHAR(200)",
        "status": "ALTER TABLE escalations ADD COLUMN status VARCHAR(20)",
        "brief_json": "ALTER TABLE escalations ADD COLUMN brief_json TEXT",
        "assigned_to": "ALTER TABLE escalations ADD COLUMN assigned_to VARCHAR(100)",
        "acknowledged_at": "ALTER TABLE escalations ADD COLUMN acknowledged_at DATETIME",
        "resolved_at": "ALTER TABLE escalations ADD COLUMN resolved_at DATETIME",
        "resolution_notes": "ALTER TABLE escalations ADD COLUMN resolution_notes TEXT",
        "created_at": "ALTER TABLE escalations ADD COLUMN created_at DATETIME",
    }

    existing_columns = {column["name"] for column in inspector.get_columns("escalations")}
    statements = [statement for name, statement in escalation_columns.items() if name not in existing_columns]
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_escalations_user_id ON escalations (user_id)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_escalations_session_id ON escalations (session_id)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_escalations_medication_id ON escalations (medication_id)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_escalations_status ON escalations (status)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_escalations_created_at ON escalations (created_at)"))


def _ensure_refill_table():
    """Add missing refill tracking columns to legacy tables."""
    inspector = inspect(engine)
    if "refill_requests" not in inspector.get_table_names():
        return

    refill_columns = {
        "user_id": "ALTER TABLE refill_requests ADD COLUMN user_id INTEGER",
        "medication_id": "ALTER TABLE refill_requests ADD COLUMN medication_id INTEGER",
        "status": "ALTER TABLE refill_requests ADD COLUMN status VARCHAR(20)",
        "due_date": "ALTER TABLE refill_requests ADD COLUMN due_date DATE",
        "requested_at": "ALTER TABLE refill_requests ADD COLUMN requested_at DATETIME",
        "flagged_at": "ALTER TABLE refill_requests ADD COLUMN flagged_at DATETIME",
        "confirmed_at": "ALTER TABLE refill_requests ADD COLUMN confirmed_at DATETIME",
        "completed_at": "ALTER TABLE refill_requests ADD COLUMN completed_at DATETIME",
        "pharmacy_name": "ALTER TABLE refill_requests ADD COLUMN pharmacy_name VARCHAR(200)",
        "notes": "ALTER TABLE refill_requests ADD COLUMN notes TEXT",
        "created_at": "ALTER TABLE refill_requests ADD COLUMN created_at DATETIME",
    }

    existing_columns = {column["name"] for column in inspector.get_columns("refill_requests")}
    statements = [statement for name, statement in refill_columns.items() if name not in existing_columns]
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_refill_requests_user_id ON refill_requests (user_id)"))
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_refill_requests_medication_id ON refill_requests (medication_id)")
        )
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_refill_requests_status ON refill_requests (status)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_refill_requests_due_date ON refill_requests (due_date)"))


def _ensure_audit_table():
    """Add missing audit log columns to legacy tables."""
    inspector = inspect(engine)
    if "audit_log" not in inspector.get_table_names():
        return

    audit_columns = {
        "user_id": "ALTER TABLE audit_log ADD COLUMN user_id INTEGER",
        "actor_id": "ALTER TABLE audit_log ADD COLUMN actor_id INTEGER",
        "actor_type": "ALTER TABLE audit_log ADD COLUMN actor_type VARCHAR(20)",
        "action": "ALTER TABLE audit_log ADD COLUMN action VARCHAR(50)",
        "resource_type": "ALTER TABLE audit_log ADD COLUMN resource_type VARCHAR(50)",
        "resource_id": "ALTER TABLE audit_log ADD COLUMN resource_id INTEGER",
        "ip_address": "ALTER TABLE audit_log ADD COLUMN ip_address VARCHAR(45)",
        "details": "ALTER TABLE audit_log ADD COLUMN details TEXT",
        "created_at": "ALTER TABLE audit_log ADD COLUMN created_at DATETIME",
    }

    existing_columns = {column["name"] for column in inspector.get_columns("audit_log")}
    statements = [statement for name, statement in audit_columns.items() if name not in existing_columns]
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_audit_log_created_at ON audit_log (created_at)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_audit_log_user_id ON audit_log (user_id)"))


def _ensure_doctor_recommendations_table():
    """Add missing doctor recommendation columns to legacy tables."""
    inspector = inspect(engine)
    if "doctor_recommendations" not in inspector.get_table_names():
        return

    recommendation_columns = {
        "user_id": "ALTER TABLE doctor_recommendations ADD COLUMN user_id INTEGER",
        "session_id": "ALTER TABLE doctor_recommendations ADD COLUMN session_id INTEGER",
        "doctor_name": "ALTER TABLE doctor_recommendations ADD COLUMN doctor_name VARCHAR(200)",
        "specialty": "ALTER TABLE doctor_recommendations ADD COLUMN specialty VARCHAR(100)",
        "recommended_for": "ALTER TABLE doctor_recommendations ADD COLUMN recommended_for VARCHAR(100)",
        "address": "ALTER TABLE doctor_recommendations ADD COLUMN address VARCHAR(500)",
        "phone": "ALTER TABLE doctor_recommendations ADD COLUMN phone VARCHAR(50)",
        "place_id": "ALTER TABLE doctor_recommendations ADD COLUMN place_id VARCHAR(200)",
        "latitude": "ALTER TABLE doctor_recommendations ADD COLUMN latitude FLOAT",
        "longitude": "ALTER TABLE doctor_recommendations ADD COLUMN longitude FLOAT",
        "rating": "ALTER TABLE doctor_recommendations ADD COLUMN rating FLOAT",
        "distance_text": "ALTER TABLE doctor_recommendations ADD COLUMN distance_text VARCHAR(50)",
        "photo_url": "ALTER TABLE doctor_recommendations ADD COLUMN photo_url VARCHAR(500)",
        "is_open": "ALTER TABLE doctor_recommendations ADD COLUMN is_open VARCHAR(20)",
        "created_at": "ALTER TABLE doctor_recommendations ADD COLUMN created_at DATETIME",
    }

    existing_columns = {column["name"] for column in inspector.get_columns("doctor_recommendations")}
    statements = [statement for name, statement in recommendation_columns.items() if name not in existing_columns]
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_doctor_recommendations_user_id ON doctor_recommendations (user_id)")
        )
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_doctor_recommendations_session_id ON doctor_recommendations (session_id)")
        )
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_doctor_recommendations_place_id ON doctor_recommendations (place_id)")
        )
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_doctor_recommendations_created_at ON doctor_recommendations (created_at)")
        )


def _parse_json_blob(raw_value: str | None):
    if not isinstance(raw_value, str) or not raw_value.strip():
        return None
    try:
        return json.loads(raw_value)
    except json.JSONDecodeError:
        return None


def _serialize_medication(medication: Medication) -> dict:
    return {
        "id": medication.id,
        "user_id": medication.user_id,
        "drug_name": medication.drug_name,
        "brand_name": medication.brand_name,
        "current_dose_mg": medication.current_dose_mg,
        "frequency": medication.frequency,
        "route": medication.route,
        "start_date": medication.start_date.isoformat() if medication.start_date else None,
        "titration_step": medication.titration_step,
        "status": medication.status,
        "notes": medication.notes,
        "created_at": medication.created_at.isoformat() if medication.created_at else None,
        "updated_at": medication.updated_at.isoformat() if medication.updated_at else None,
    }


def _serialize_medication_event(event: MedicationEvent) -> dict:
    return {
        "id": event.id,
        "medication_id": event.medication_id,
        "user_id": event.user_id,
        "event_type": event.event_type,
        "dose_mg": event.dose_mg,
        "scheduled_at": event.scheduled_at.isoformat() if event.scheduled_at else None,
        "recorded_at": event.recorded_at.isoformat() if event.recorded_at else None,
        "notes": event.notes,
    }


def _serialize_side_effect(side_effect: SideEffect) -> dict:
    return {
        "id": side_effect.id,
        "medication_id": side_effect.medication_id,
        "user_id": side_effect.user_id,
        "symptom": side_effect.symptom,
        "severity": side_effect.severity,
        "titration_step": side_effect.titration_step,
        "reported_at": side_effect.reported_at.isoformat() if side_effect.reported_at else None,
        "resolved_at": side_effect.resolved_at.isoformat() if side_effect.resolved_at else None,
        "notes": side_effect.notes,
    }


def _serialize_outreach(outreach: ScheduledOutreach) -> dict:
    parsed_context = _parse_json_blob(outreach.context_json)

    payload = {
        "id": outreach.id,
        "user_id": outreach.user_id,
        "medication_id": outreach.medication_id,
        "outreach_type": outreach.outreach_type,
        "channel": outreach.channel,
        "status": outreach.status,
        "scheduled_at": outreach.scheduled_at.isoformat() if outreach.scheduled_at else None,
        "attempted_at": outreach.attempted_at.isoformat() if outreach.attempted_at else None,
        "completed_at": outreach.completed_at.isoformat() if outreach.completed_at else None,
        "attempt_count": outreach.attempt_count,
        "max_attempts": outreach.max_attempts,
        "priority": outreach.priority,
        "context_json": outreach.context_json,
        "context": parsed_context,
        "outcome_summary": outreach.outcome_summary,
        "session_id": outreach.session_id,
        "created_at": outreach.created_at.isoformat() if outreach.created_at else None,
    }

    if getattr(outreach, "user", None) is not None:
        payload["user_name"] = outreach.user.name
        payload["user_email"] = outreach.user.email
    if getattr(outreach, "medication", None) is not None:
        payload["medication"] = {
            "id": outreach.medication.id,
            "drug_name": outreach.medication.drug_name,
            "brand_name": outreach.medication.brand_name,
            "current_dose_mg": outreach.medication.current_dose_mg,
            "titration_step": outreach.medication.titration_step,
            "status": outreach.medication.status,
        }
    if getattr(outreach, "session", None) is not None:
        payload["session"] = {
            "id": outreach.session.id,
            "started_at": outreach.session.started_at.isoformat() if outreach.session.started_at else None,
            "ended_at": outreach.session.ended_at.isoformat() if outreach.session.ended_at else None,
        }

    return payload


def _serialize_escalation(escalation: Escalation) -> dict:
    parsed_brief = _parse_json_blob(escalation.brief_json)
    payload = {
        "id": escalation.id,
        "user_id": escalation.user_id,
        "session_id": escalation.session_id,
        "medication_id": escalation.medication_id,
        "severity": escalation.severity,
        "trigger_reason": escalation.trigger_reason,
        "status": escalation.status,
        "brief_json": escalation.brief_json,
        "brief": parsed_brief,
        "assigned_to": escalation.assigned_to,
        "acknowledged_at": escalation.acknowledged_at.isoformat() if escalation.acknowledged_at else None,
        "resolved_at": escalation.resolved_at.isoformat() if escalation.resolved_at else None,
        "resolution_notes": escalation.resolution_notes,
        "created_at": escalation.created_at.isoformat() if escalation.created_at else None,
    }

    if getattr(escalation, "user", None) is not None:
        payload["user"] = {
            "id": escalation.user.id,
            "name": escalation.user.name,
            "email": escalation.user.email,
            "age": escalation.user.age,
            "weight_kg": escalation.user.weight_kg,
            "height_cm": escalation.user.height_cm,
        }
    if getattr(escalation, "medication", None) is not None:
        payload["medication"] = {
            "id": escalation.medication.id,
            "drug_name": escalation.medication.drug_name,
            "brand_name": escalation.medication.brand_name,
            "current_dose_mg": escalation.medication.current_dose_mg,
            "titration_step": escalation.medication.titration_step,
            "status": escalation.medication.status,
        }
    if getattr(escalation, "session", None) is not None:
        payload["session"] = {
            "id": escalation.session.id,
            "started_at": escalation.session.started_at.isoformat() if escalation.session.started_at else None,
            "ended_at": escalation.session.ended_at.isoformat() if escalation.session.ended_at else None,
        }

    return payload


def _serialize_refill_request(refill: RefillRequest) -> dict:
    payload = {
        "id": refill.id,
        "user_id": refill.user_id,
        "medication_id": refill.medication_id,
        "status": refill.status,
        "due_date": refill.due_date.isoformat() if refill.due_date else None,
        "requested_at": refill.requested_at.isoformat() if refill.requested_at else None,
        "flagged_at": refill.flagged_at.isoformat() if refill.flagged_at else None,
        "confirmed_at": refill.confirmed_at.isoformat() if refill.confirmed_at else None,
        "completed_at": refill.completed_at.isoformat() if refill.completed_at else None,
        "pharmacy_name": refill.pharmacy_name,
        "notes": refill.notes,
        "created_at": refill.created_at.isoformat() if refill.created_at else None,
    }

    if getattr(refill, "user", None) is not None:
        payload["user_name"] = refill.user.name
        payload["user_email"] = refill.user.email
    if getattr(refill, "medication", None) is not None:
        payload["medication"] = {
            "id": refill.medication.id,
            "drug_name": refill.medication.drug_name,
            "brand_name": refill.medication.brand_name,
            "current_dose_mg": refill.medication.current_dose_mg,
            "titration_step": refill.medication.titration_step,
            "status": refill.medication.status,
        }

    return payload


def _serialize_audit_log(entry: AuditLog) -> dict:
    return {
        "id": entry.id,
        "user_id": entry.user_id,
        "actor_id": entry.actor_id,
        "actor_type": entry.actor_type,
        "action": entry.action,
        "resource_type": entry.resource_type,
        "resource_id": entry.resource_id,
        "ip_address": entry.ip_address,
        "details": entry.details,
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
    }


def _serialize_doctor_recommendation(recommendation: DoctorRecommendation) -> dict:
    return {
        "id": recommendation.id,
        "user_id": recommendation.user_id,
        "session_id": recommendation.session_id,
        "doctor_name": recommendation.doctor_name,
        "specialty": recommendation.specialty,
        "recommended_for": recommendation.recommended_for,
        "address": recommendation.address,
        "phone": recommendation.phone,
        "place_id": recommendation.place_id,
        "latitude": recommendation.latitude,
        "longitude": recommendation.longitude,
        "rating": recommendation.rating,
        "distance_text": recommendation.distance_text,
        "photo_url": recommendation.photo_url,
        "is_open": recommendation.is_open,
        "created_at": recommendation.created_at.isoformat() if recommendation.created_at else None,
    }


def _normalize_doctor_open_status(value) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"yes", "open", "true"}:
            return "yes"
        if normalized in {"no", "closed", "false"}:
            return "no"
    return "unknown"


def _doctor_recommendation_lookup_query(db: DBSession, user_id: int, doctor: dict):
    place_id = str(doctor.get("place_id") or "").strip()
    if place_id:
        return (
            db.query(DoctorRecommendation)
            .filter(
                DoctorRecommendation.user_id == user_id,
                DoctorRecommendation.place_id == place_id,
            )
        )

    doctor_name = str(doctor.get("name") or doctor.get("doctor_name") or "").strip()
    address = str(doctor.get("address") or "").strip()
    if doctor_name and address:
        return (
            db.query(DoctorRecommendation)
            .filter(
                DoctorRecommendation.user_id == user_id,
                DoctorRecommendation.place_id.is_(None),
                func.lower(DoctorRecommendation.doctor_name) == doctor_name.lower(),
                func.lower(DoctorRecommendation.address) == address.lower(),
            )
        )
    return None


def _as_utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _coerce_analytics_datetime(value: date | datetime) -> datetime:
    if isinstance(value, datetime):
        normalized = _as_utc_datetime(value)
        return normalized if normalized is not None else datetime.now(timezone.utc)
    return datetime.combine(value, time.min, tzinfo=timezone.utc)


def _normalize_analytics_range(
    start_date: date | datetime,
    end_date: date | datetime,
) -> tuple[datetime, datetime]:
    start_dt = _coerce_analytics_datetime(start_date)
    if isinstance(end_date, datetime):
        end_dt = _coerce_analytics_datetime(end_date)
    else:
        end_dt = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=timezone.utc)

    if end_dt < start_dt:
        start_dt, end_dt = end_dt, start_dt
    return start_dt, end_dt


def _real_sessions_subquery(db: DBSession):
    return (
        db.query(
            Session.id.label("session_id"),
            Session.user_id.label("user_id"),
            Session.started_at.label("started_at"),
            Session.ended_at.label("ended_at"),
            func.max(ConversationMessage.created_at).label("last_message_at"),
            func.count(ConversationMessage.id).label("message_count"),
        )
        .join(ConversationMessage, ConversationMessage.session_id == Session.id)
        .filter(Session.user_id.isnot(None))
        .group_by(Session.id, Session.user_id, Session.started_at, Session.ended_at)
        .subquery()
    )


def _session_window_end(
    started_at: datetime | None,
    ended_at: datetime | None,
    last_message_at: datetime | None,
) -> datetime:
    start = _as_utc_datetime(started_at) or datetime.now(timezone.utc)
    candidates = [start]
    for candidate in (ended_at, last_message_at):
        normalized = _as_utc_datetime(candidate)
        if normalized is not None and normalized > start:
            candidates.append(normalized)

    return max(candidates) + timedelta(minutes=5)


def init_db():
    global engine
    global DATABASE_URL

    try:
        Base.metadata.create_all(bind=engine)
        _ensure_users_auth_columns()
        _ensure_sessions_metadata_columns()
        _ensure_medication_tables()
        _ensure_outreach_table()
        _ensure_escalation_table()
        _ensure_refill_table()
        _ensure_audit_table()
        _ensure_doctor_recommendations_table()
    except OperationalError:
        if DATABASE_URL.startswith("sqlite"):
            raise
        DATABASE_URL = SQLITE_FALLBACK_URL
        engine = _create_engine(DATABASE_URL)
        SessionLocal.configure(bind=engine)
        Base.metadata.create_all(bind=engine)
        _ensure_users_auth_columns()
        _ensure_sessions_metadata_columns()
        _ensure_medication_tables()
        _ensure_outreach_table()
        _ensure_escalation_table()
        _ensure_refill_table()
        _ensure_audit_table()
        _ensure_doctor_recommendations_table()


def get_or_create_user(name: str, age: int | None = None) -> User:
    with SessionLocal(expire_on_commit=False) as db:
        user = db.query(User).filter(User.name.ilike(name)).first()
        if not user:
            user = User(name=name, age=age)
            db.add(user)
            db.commit()
        elif age and not user.age:
            user.age = age
            db.commit()
        return user


def log_vital(
    user_id: int,
    vital_type: str,
    value: str,
    unit: str | None,
    notes: str | None,
    recorded_at: datetime | None = None,
) -> dict:
    with SessionLocal() as db:
        vital = Vital(
            user_id=user_id,
            vital_type=vital_type,
            value=value,
            unit=unit,
            notes=notes,
        )
        if recorded_at is not None:
            vital.recorded_at = recorded_at
        db.add(vital)
        db.commit()
        db.refresh(vital)
        return {
            "id": vital.id,
            "vital_type": vital.vital_type,
            "value": vital.value,
            "unit": vital.unit,
            "notes": vital.notes,
            "recorded_at": vital.recorded_at.isoformat(),
        }


def get_recent_vitals(user_id: int, limit: int = 10) -> list[dict]:
    with SessionLocal() as db:
        vitals = (
            db.query(Vital)
            .filter(Vital.user_id == user_id)
            .order_by(Vital.recorded_at.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "vital_type": v.vital_type,
                "value": v.value,
                "unit": v.unit,
                "notes": v.notes,
                "recorded_at": v.recorded_at.isoformat(),
            }
            for v in vitals
        ]


def create_session(user_id: int | None = None) -> int:
    with SessionLocal() as db:
        session = Session(user_id=user_id)
        db.add(session)
        db.commit()
        db.refresh(session)
        return session.id


def close_session(session_id: int, summary: str | None = None):
    with SessionLocal() as db:
        session = db.query(Session).filter(Session.id == session_id).first()
        if session:
            session.ended_at = datetime.now(timezone.utc)
            session.summary = summary
            db.commit()


def update_session_user(session_id: int, user_id: int):
    with SessionLocal() as db:
        session = db.query(Session).filter(Session.id == session_id).first()
        if session:
            session.user_id = user_id
            db.commit()


def update_session_timezone(
    session_id: int,
    timezone_name: str | None = None,
    utc_offset_minutes: int | None = None,
):
    with SessionLocal() as db:
        session = db.query(Session).filter(Session.id == session_id).first()
        if not session:
            return

        changed = False
        if isinstance(timezone_name, str) and timezone_name.strip():
            clean_tz = timezone_name.strip()[:80]
            if session.timezone_name != clean_tz:
                session.timezone_name = clean_tz
                changed = True

        if isinstance(utc_offset_minutes, int):
            if session.utc_offset_minutes != utc_offset_minutes:
                session.utc_offset_minutes = utc_offset_minutes
                changed = True

        if changed:
            db.commit()


def log_conversation_message(
    session_id: int,
    role: str,
    content: str,
    user_id: int | None = None,
    created_at: datetime | None = None,
) -> dict:
    with SessionLocal() as db:
        message = ConversationMessage(
            session_id=session_id,
            user_id=user_id,
            role=(role or "assistant").strip().lower()[:20],
            content=(content or "").strip(),
        )
        if created_at is not None:
            message.created_at = created_at

        db.add(message)
        db.commit()
        db.refresh(message)
        return {
            "id": message.id,
            "session_id": message.session_id,
            "user_id": message.user_id,
            "role": message.role,
            "content": message.content,
            "created_at": message.created_at.isoformat() if message.created_at else None,
        }


def attach_session_messages_to_user(session_id: int, user_id: int):
    with SessionLocal() as db:
        (
            db.query(ConversationMessage)
            .filter(ConversationMessage.session_id == session_id, ConversationMessage.user_id.is_(None))
            .update({"user_id": user_id}, synchronize_session=False)
        )
        db.commit()


def update_user_profile(user_id: int, name: str | None = None, age: int | None = None):
    with SessionLocal() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return

        changed = False
        if isinstance(name, str) and name.strip() and user.name != name.strip():
            user.name = name.strip()
            changed = True

        if isinstance(age, int) and not isinstance(age, bool) and age > 0 and user.age != age:
            user.age = age
            changed = True

        if changed:
            db.commit()


def update_user_full(
    user_id: int,
    name: str | None = None,
    dob: date | None = None,
    update_dob: bool = False,
    weight_kg: float | None = None,
    update_weight: bool = False,
    height_cm: float | None = None,
    update_height: bool = False,
    address: str | None = None,
    update_address: bool = False,
) -> User:
    with SessionLocal(expire_on_commit=False) as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise ValueError(f"User {user_id} not found")

        if isinstance(name, str) and name.strip():
            user.name = name.strip()

        if update_dob:
            user.dob = dob
            user.age = age_from_dob(dob) if dob else None

        if update_weight:
            user.weight_kg = weight_kg

        if update_height:
            user.height_cm = height_cm

        if update_address:
            user.address = address.strip() if address else None

        db.commit()
        return user


def get_vitals_history(user_id: int, limit: int = 50) -> list[dict]:
    with SessionLocal() as db:
        vitals = (
            db.query(Vital)
            .filter(Vital.user_id == user_id)
            .order_by(Vital.recorded_at.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "id": v.id,
                "vital_type": v.vital_type,
                "value": v.value,
                "unit": v.unit,
                "notes": v.notes,
                "recorded_at": v.recorded_at.isoformat(),
            }
            for v in vitals
        ]


def create_medication(
    user_id: int,
    drug_name: str,
    brand_name: str | None,
    current_dose_mg: float,
    frequency: str,
    start_date: date,
    route: str = "subcutaneous injection",
    titration_step: int = 1,
    status: str = "active",
    notes: str | None = None,
) -> dict:
    with SessionLocal(expire_on_commit=False) as db:
        medication = Medication(
            user_id=user_id,
            drug_name=drug_name.strip().lower(),
            brand_name=brand_name.strip() if isinstance(brand_name, str) and brand_name.strip() else None,
            current_dose_mg=float(current_dose_mg),
            frequency=(frequency or "weekly").strip().lower() or "weekly",
            route=(route or "subcutaneous injection").strip() or "subcutaneous injection",
            start_date=start_date,
            titration_step=int(titration_step),
            status=(status or "active").strip().lower() or "active",
            notes=notes.strip() if isinstance(notes, str) and notes.strip() else None,
        )
        db.add(medication)
        db.commit()
        db.refresh(medication)
        return _serialize_medication(medication)


def get_active_medications(user_id: int) -> list[dict]:
    with SessionLocal() as db:
        medications = (
            db.query(Medication)
            .filter(Medication.user_id == user_id, Medication.status == "active")
            .order_by(Medication.created_at.desc(), Medication.id.desc())
            .all()
        )
        return [_serialize_medication(medication) for medication in medications]


def get_medication_by_id(medication_id: int, user_id: int) -> dict | None:
    with SessionLocal() as db:
        medication = (
            db.query(Medication)
            .filter(Medication.id == medication_id, Medication.user_id == user_id)
            .first()
        )
        if not medication:
            return None
        return _serialize_medication(medication)


def update_medication(medication_id: int, user_id: int, **fields) -> dict | None:
    with SessionLocal(expire_on_commit=False) as db:
        medication = (
            db.query(Medication)
            .filter(Medication.id == medication_id, Medication.user_id == user_id)
            .first()
        )
        if not medication:
            return None

        changed = False
        for field_name, value in fields.items():
            if value is None or not hasattr(medication, field_name):
                continue

            if field_name == "drug_name":
                normalized = str(value).strip().lower()
            elif field_name in {"brand_name", "notes"}:
                normalized = str(value).strip() or None
            elif field_name in {"frequency", "route", "status"}:
                normalized = str(value).strip()
                if field_name in {"frequency", "status"}:
                    normalized = normalized.lower()
            elif field_name == "current_dose_mg":
                normalized = float(value)
            elif field_name == "titration_step":
                normalized = int(value)
            elif field_name == "start_date":
                normalized = value
            else:
                normalized = value

            if getattr(medication, field_name) != normalized:
                setattr(medication, field_name, normalized)
                changed = True

        if not changed:
            return _serialize_medication(medication)

        medication.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(medication)
        return _serialize_medication(medication)


def log_medication_event(
    medication_id: int,
    user_id: int,
    event_type: str,
    dose_mg: float | None,
    scheduled_at: datetime | None,
    notes: str | None,
) -> dict:
    with SessionLocal() as db:
        event = MedicationEvent(
            medication_id=medication_id,
            user_id=user_id,
            event_type=event_type.strip().lower(),
            dose_mg=float(dose_mg) if dose_mg is not None else None,
            scheduled_at=scheduled_at,
            notes=notes.strip() if isinstance(notes, str) and notes.strip() else None,
        )
        db.add(event)
        db.commit()
        db.refresh(event)
        return _serialize_medication_event(event)


def get_medication_events(user_id: int, medication_id: int | None = None, limit: int = 50) -> list[dict]:
    with SessionLocal() as db:
        query = db.query(MedicationEvent).filter(MedicationEvent.user_id == user_id)
        if medication_id is not None:
            query = query.filter(MedicationEvent.medication_id == medication_id)
        events = query.order_by(MedicationEvent.recorded_at.desc(), MedicationEvent.id.desc()).limit(limit).all()
        return [_serialize_medication_event(event) for event in events]


def log_side_effect(
    user_id: int,
    symptom: str,
    severity: str,
    medication_id: int | None = None,
    titration_step: int | None = None,
    notes: str | None = None,
) -> dict:
    with SessionLocal() as db:
        side_effect = SideEffect(
            user_id=user_id,
            medication_id=medication_id,
            symptom=symptom.strip().lower(),
            severity=severity.strip().lower(),
            titration_step=int(titration_step) if titration_step is not None else None,
            notes=notes.strip() if isinstance(notes, str) and notes.strip() else None,
        )
        db.add(side_effect)
        db.commit()
        db.refresh(side_effect)
        return _serialize_side_effect(side_effect)


def get_side_effects(user_id: int, medication_id: int | None = None) -> list[dict]:
    with SessionLocal() as db:
        query = db.query(SideEffect).filter(SideEffect.user_id == user_id)
        if medication_id is not None:
            query = query.filter(SideEffect.medication_id == medication_id)
        side_effects = query.order_by(SideEffect.reported_at.desc(), SideEffect.id.desc()).all()
        return [_serialize_side_effect(side_effect) for side_effect in side_effects]


def resolve_side_effect(side_effect_id: int, user_id: int) -> dict | None:
    """Mark a side effect as resolved (set resolved_at to now)."""
    with SessionLocal() as db:
        effect = (
            db.query(SideEffect)
            .filter(
                SideEffect.id == side_effect_id,
                SideEffect.user_id == user_id,
                SideEffect.resolved_at.is_(None),
            )
            .first()
        )
        if not effect:
            return None
        effect.resolved_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(effect)
        return _serialize_side_effect(effect)


def resolve_all_side_effects(user_id: int, medication_id: int | None = None) -> int:
    """Mark all unresolved side effects as resolved. Returns count resolved."""
    with SessionLocal() as db:
        query = db.query(SideEffect).filter(
            SideEffect.user_id == user_id,
            SideEffect.resolved_at.is_(None),
        )
        if medication_id is not None:
            query = query.filter(SideEffect.medication_id == medication_id)
        effects = query.all()
        count = 0
        now = datetime.now(timezone.utc)
        for effect in effects:
            effect.resolved_at = now
            count += 1
        if count > 0:
            db.commit()
        return count


def get_unresolved_side_effects(user_id: int, medication_id: int | None = None) -> list[dict]:
    """Get all unresolved (active) side effects for a user."""
    with SessionLocal() as db:
        query = db.query(SideEffect).filter(
            SideEffect.user_id == user_id,
            SideEffect.resolved_at.is_(None),
        )
        if medication_id is not None:
            query = query.filter(SideEffect.medication_id == medication_id)
        effects = query.order_by(SideEffect.reported_at.desc(), SideEffect.id.desc()).all()
        return [_serialize_side_effect(effect) for effect in effects]


def get_side_effect_timeline(user_id: int) -> list[dict]:
    with SessionLocal() as db:
        timeline = (
            db.query(SideEffect, Medication)
            .outerjoin(Medication, SideEffect.medication_id == Medication.id)
            .filter(SideEffect.user_id == user_id)
            .order_by(SideEffect.reported_at.asc(), SideEffect.id.asc())
            .all()
        )
        results: list[dict] = []
        for side_effect, medication in timeline:
            entry = _serialize_side_effect(side_effect)
            entry["medication"] = None
            if medication:
                entry["medication"] = {
                    "id": medication.id,
                    "drug_name": medication.drug_name,
                    "brand_name": medication.brand_name,
                    "current_dose_mg": medication.current_dose_mg,
                    "titration_step": medication.titration_step,
                    "status": medication.status,
                }
            results.append(entry)
        return results


def _refill_cycle_days(frequency: str | None) -> int | None:
    normalized = str(frequency or "").strip().lower()
    if not normalized:
        return None
    if normalized == "weekly":
        return 28
    if normalized == "daily":
        return 30
    if normalized == "monthly":
        return 30
    if normalized == "biweekly":
        return 28
    return 30


def calculate_next_refill_date(medication_id: int, user_id: int) -> date | None:
    with SessionLocal() as db:
        medication = (
            db.query(Medication)
            .filter(Medication.id == medication_id, Medication.user_id == user_id)
            .first()
        )
        if not medication:
            return None

        cycle_days = _refill_cycle_days(medication.frequency)
        if cycle_days is None:
            return None

        refill_requests = (
            db.query(RefillRequest)
            .filter(RefillRequest.user_id == user_id, RefillRequest.medication_id == medication_id)
            .order_by(RefillRequest.created_at.desc(), RefillRequest.id.desc())
            .all()
        )

        anchor_date = medication.start_date
        for refill in refill_requests:
            for timestamp in (
                refill.completed_at,
                refill.confirmed_at,
                refill.flagged_at,
                refill.requested_at,
                refill.created_at,
            ):
                if timestamp is not None:
                    anchor_date = timestamp.date()
                    break
            if anchor_date is not None and anchor_date != medication.start_date:
                break

        if anchor_date is None:
            return None
        return anchor_date + timedelta(days=cycle_days)


def create_refill_request(user_id: int, medication_id: int, due_date: date | None = None) -> dict:
    with SessionLocal(expire_on_commit=False) as db:
        refill = RefillRequest(
            user_id=user_id,
            medication_id=medication_id,
            status="due",
            due_date=due_date,
        )
        db.add(refill)
        db.commit()
        db.refresh(refill)
        return _serialize_refill_request(refill)


def get_active_refills(user_id: int) -> list[dict]:
    with SessionLocal() as db:
        refills = (
            db.query(RefillRequest)
            .filter(RefillRequest.user_id == user_id, RefillRequest.status.in_(tuple(_ACTIVE_REFILL_STATUSES)))
            .order_by(RefillRequest.created_at.desc(), RefillRequest.id.desc())
            .all()
        )
        return [_serialize_refill_request(refill) for refill in refills]


def get_pending_refills(limit: int = 50) -> list[dict]:
    status_order = {
        "flagged_for_team": 0,
        "requested": 1,
        "reminded": 2,
        "due": 3,
        "confirmed": 4,
    }
    fallback_due = date.max
    with SessionLocal() as db:
        refills = (
            db.query(RefillRequest)
            .filter(RefillRequest.status.in_(tuple(_ACTIVE_REFILL_STATUSES)))
            .all()
        )
        refills.sort(
            key=lambda refill: (
                status_order.get(str(refill.status or "").lower(), 9),
                refill.due_date or fallback_due,
                refill.created_at or datetime.now(timezone.utc),
                refill.id or 0,
            )
        )
        return [_serialize_refill_request(refill) for refill in refills[:limit]]


def get_refill_by_id(refill_id: int, user_id: int) -> dict | None:
    with SessionLocal() as db:
        refill = (
            db.query(RefillRequest)
            .filter(RefillRequest.id == refill_id, RefillRequest.user_id == user_id)
            .first()
        )
        if not refill:
            return None
        return _serialize_refill_request(refill)


def update_refill_status(
    refill_id: int,
    user_id: int,
    new_status: str,
    notes: str | None = None,
    pharmacy_name: str | None = None,
) -> dict | None:
    normalized_status = (new_status or "").strip().lower()
    now = datetime.now(timezone.utc)
    with SessionLocal(expire_on_commit=False) as db:
        refill = (
            db.query(RefillRequest)
            .filter(RefillRequest.id == refill_id, RefillRequest.user_id == user_id)
            .first()
        )
        if not refill:
            return None

        current_status = str(refill.status or "due").strip().lower()
        if normalized_status != current_status:
            if normalized_status != "cancelled":
                allowed_next = _REFILL_TRANSITIONS.get(current_status, set())
                if normalized_status not in allowed_next:
                    raise ValueError(f"Invalid refill status transition: {current_status} -> {normalized_status}")
            refill.status = normalized_status

        if notes is not None:
            refill.notes = notes.strip() if notes.strip() else None
        if pharmacy_name is not None:
            refill.pharmacy_name = pharmacy_name.strip() if pharmacy_name.strip() else None

        if normalized_status == "requested" and refill.requested_at is None:
            refill.requested_at = now
        if normalized_status == "flagged_for_team" and refill.flagged_at is None:
            refill.flagged_at = now
        if normalized_status == "confirmed" and refill.confirmed_at is None:
            refill.confirmed_at = now
        if normalized_status == "completed" and refill.completed_at is None:
            refill.completed_at = now

        db.commit()
        db.refresh(refill)
        return _serialize_refill_request(refill)


def create_outreach(
    user_id: int,
    outreach_type: str,
    scheduled_at: datetime,
    medication_id: int | None = None,
    channel: str = "call",
    priority: int = 5,
    context_json: str | dict | None = None,
) -> dict:
    if scheduled_at.tzinfo is None:
        normalized_scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)
    else:
        normalized_scheduled_at = scheduled_at.astimezone(timezone.utc)

    serialized_context = context_json
    if isinstance(context_json, dict):
        serialized_context = json.dumps(context_json)

    with SessionLocal(expire_on_commit=False) as db:
        outreach = ScheduledOutreach(
            user_id=user_id,
            medication_id=medication_id,
            outreach_type=outreach_type.strip(),
            channel=(channel or "call").strip().lower() or "call",
            status="pending",
            scheduled_at=normalized_scheduled_at,
            priority=int(priority),
            context_json=serialized_context.strip() if isinstance(serialized_context, str) and serialized_context.strip() else None,
        )
        db.add(outreach)
        db.commit()
        db.refresh(outreach)
        return _serialize_outreach(outreach)


def get_pending_outreach(limit: int = 50) -> list[dict]:
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        outreach_items = (
            db.query(ScheduledOutreach)
            .filter(
                ScheduledOutreach.status == "pending",
                ScheduledOutreach.scheduled_at <= now,
            )
            .order_by(ScheduledOutreach.priority.asc(), ScheduledOutreach.scheduled_at.asc(), ScheduledOutreach.id.asc())
            .limit(limit)
            .all()
        )
        return [_serialize_outreach(outreach) for outreach in outreach_items]


def get_outreach_for_user(user_id: int, status: str | None = None, limit: int = 20) -> list[dict]:
    with SessionLocal() as db:
        query = db.query(ScheduledOutreach).filter(ScheduledOutreach.user_id == user_id)
        if isinstance(status, str) and status.strip():
            query = query.filter(ScheduledOutreach.status == status.strip().lower())
        outreach_items = (
            query.order_by(ScheduledOutreach.scheduled_at.desc(), ScheduledOutreach.id.desc())
            .limit(limit)
            .all()
        )
        return [_serialize_outreach(outreach) for outreach in outreach_items]


def update_outreach_status(
    outreach_id: int,
    status: str,
    outcome_summary: str | None = None,
    session_id: int | None = None,
) -> dict | None:
    with SessionLocal(expire_on_commit=False) as db:
        outreach = db.query(ScheduledOutreach).filter(ScheduledOutreach.id == outreach_id).first()
        if not outreach:
            return None

        outreach.status = (status or outreach.status).strip().lower()
        if outcome_summary is not None:
            outreach.outcome_summary = outcome_summary.strip() if outcome_summary.strip() else None
        if session_id is not None:
            outreach.session_id = session_id
        if outreach.status == "completed":
            outreach.completed_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(outreach)
        return _serialize_outreach(outreach)


def increment_outreach_attempt(outreach_id: int) -> dict | None:
    with SessionLocal(expire_on_commit=False) as db:
        outreach = db.query(ScheduledOutreach).filter(ScheduledOutreach.id == outreach_id).first()
        if not outreach:
            return None

        outreach.attempt_count = int(outreach.attempt_count or 0) + 1
        outreach.attempted_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(outreach)
        return _serialize_outreach(outreach)


def cancel_outreach(outreach_id: int) -> dict | None:
    return update_outreach_status(outreach_id, "cancelled")


def get_outreach_stats() -> dict:
    with SessionLocal() as db:
        outreach_items = db.query(ScheduledOutreach).all()
        by_status: dict[str, int] = {}
        by_outreach_type: dict[str, int] = {}

        for outreach in outreach_items:
            by_status[outreach.status] = by_status.get(outreach.status, 0) + 1
            by_outreach_type[outreach.outreach_type] = by_outreach_type.get(outreach.outreach_type, 0) + 1

        return {
            "total": len(outreach_items),
            "by_status": by_status,
            "by_outreach_type": by_outreach_type,
        }


def delete_vital(user_id: int, vital_id: int) -> bool:
    with SessionLocal() as db:
        vital = db.query(Vital).filter(Vital.id == vital_id, Vital.user_id == user_id).first()
        if not vital:
            return False
        db.delete(vital)
        db.commit()
        return True


def get_latest_vital_timestamp(user_id: int) -> datetime | None:
    with SessionLocal() as db:
        vital = (
            db.query(Vital)
            .filter(Vital.user_id == user_id, Vital.vital_type != "symptom")
            .order_by(Vital.recorded_at.desc())
            .first()
        )
        return vital.recorded_at if vital else None


def get_conversation_history(
    user_id: int,
    limit_sessions: int = 25,
    limit_messages_per_session: int = 0,
) -> list[dict]:
    with SessionLocal() as db:
        real_session_rows = (
            db.query(
                Session.id.label("session_id"),
                func.count(ConversationMessage.id).label("message_count"),
            )
            .join(ConversationMessage, ConversationMessage.session_id == Session.id)
            .filter(Session.user_id == user_id)
            .group_by(Session.id)
            .order_by(Session.started_at.desc(), Session.id.desc())
            .limit(limit_sessions)
            .all()
        )
        session_ids = [int(row.session_id) for row in real_session_rows]
        if not session_ids:
            return []

        sessions = (
            db.query(Session)
            .filter(Session.id.in_(session_ids))
            .order_by(Session.started_at.desc(), Session.id.desc())
            .all()
        )
        message_count_by_session = {
            int(row.session_id): int(row.message_count or 0)
            for row in real_session_rows
        }
        history: list[dict] = []
        for session in sessions:
            messages_query = (
                db.query(ConversationMessage)
                .filter(ConversationMessage.session_id == session.id)
                .order_by(ConversationMessage.created_at.asc())
            )
            if limit_messages_per_session > 0:
                messages = messages_query.limit(limit_messages_per_session).all()
            else:
                messages = messages_query.all()

            history.append(
                {
                    "session_id": session.id,
                    "started_at": session.started_at.isoformat() if session.started_at else None,
                    "ended_at": session.ended_at.isoformat() if session.ended_at else None,
                    "timezone_name": session.timezone_name,
                    "utc_offset_minutes": session.utc_offset_minutes,
                    "message_count": message_count_by_session.get(session.id, len(messages)),
                    "messages": [
                        {
                            "id": message.id,
                            "role": message.role,
                            "content": message.content,
                            "created_at": message.created_at.isoformat() if message.created_at else None,
                        }
                        for message in messages
                    ],
                }
            )
        return history


def get_session_messages(session_id: int, limit: int = 10) -> list[dict]:
    with SessionLocal() as db:
        messages = (
            db.query(ConversationMessage)
            .filter(ConversationMessage.session_id == session_id)
            .order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc())
            .limit(limit)
            .all()
        )
        results = [
            {
                "id": message.id,
                "session_id": message.session_id,
                "user_id": message.user_id,
                "role": message.role,
                "content": message.content,
                "created_at": message.created_at.isoformat() if message.created_at else None,
            }
            for message in reversed(messages)
        ]
        return results


def session_belongs_to_user(user_id: int, session_id: int) -> bool:
    with SessionLocal() as db:
        session = db.query(Session.id).filter(Session.id == session_id, Session.user_id == user_id).first()
        return session is not None


def save_doctor_recommendations(user_id: int, session_id: int | None, doctors: list[dict]) -> list[dict]:
    now = datetime.now(timezone.utc)
    saved_items: list[DoctorRecommendation] = []

    with SessionLocal(expire_on_commit=False) as db:
        for doctor in doctors or []:
            if not isinstance(doctor, dict):
                continue

            doctor_name = str(doctor.get("name") or doctor.get("doctor_name") or "").strip()
            if not doctor_name:
                continue

            types = doctor.get("types")
            primary_type = None
            if isinstance(types, list) and types:
                primary_type = str(types[0]).strip() or None

            existing_query = _doctor_recommendation_lookup_query(db, user_id, doctor)
            existing = existing_query.first() if existing_query is not None else None

            target = existing or DoctorRecommendation(user_id=user_id)
            target.session_id = session_id
            target.doctor_name = doctor_name[:200]
            target.specialty = (
                str(doctor.get("recommended_for") or primary_type or "").strip()[:100] or None
            )
            target.recommended_for = str(doctor.get("recommended_for") or "").strip()[:100] or None
            target.address = str(doctor.get("address") or "").strip()[:500] or None
            target.phone = str(doctor.get("phone") or "").strip()[:50] or None
            target.place_id = str(doctor.get("place_id") or "").strip()[:200] or None
            target.latitude = doctor.get("latitude")
            target.longitude = doctor.get("longitude")
            target.rating = doctor.get("rating")
            target.distance_text = (
                str(doctor.get("distance_text") or "").strip()[:50] or None
            )
            target.photo_url = str(doctor.get("photo_url") or "").strip()[:500] or None
            target.is_open = _normalize_doctor_open_status(
                doctor.get("is_open", doctor.get("open_now"))
            )
            target.created_at = now

            if existing is None:
                db.add(target)
            saved_items.append(target)

        db.commit()
        return [_serialize_doctor_recommendation(item) for item in saved_items]


def get_doctor_recommendations(user_id: int, limit: int = 20) -> list[dict]:
    bounded_limit = max(1, min(int(limit), 200))
    with SessionLocal() as db:
        recommendations = (
            db.query(DoctorRecommendation)
            .filter(DoctorRecommendation.user_id == user_id)
            .order_by(DoctorRecommendation.created_at.desc(), DoctorRecommendation.id.desc())
            .limit(bounded_limit)
            .all()
        )
        return [_serialize_doctor_recommendation(recommendation) for recommendation in recommendations]


def get_doctor_recommendations_for_session(session_id: int) -> list[dict]:
    with SessionLocal() as db:
        recommendations = (
            db.query(DoctorRecommendation)
            .filter(DoctorRecommendation.session_id == session_id)
            .order_by(DoctorRecommendation.created_at.desc(), DoctorRecommendation.id.desc())
            .all()
        )
        return [_serialize_doctor_recommendation(recommendation) for recommendation in recommendations]


def delete_doctor_recommendation(user_id: int, recommendation_id: int) -> bool:
    with SessionLocal() as db:
        recommendation = (
            db.query(DoctorRecommendation)
            .filter(
                DoctorRecommendation.id == recommendation_id,
                DoctorRecommendation.user_id == user_id,
            )
            .first()
        )
        if not recommendation:
            return False
        db.delete(recommendation)
        db.commit()
        return True


def delete_conversation_session(user_id: int, session_id: int) -> bool:
    with SessionLocal() as db:
        session = (
            db.query(Session)
            .filter(Session.id == session_id, Session.user_id == user_id)
            .first()
        )
        if not session:
            return False

        (
            db.query(ScheduledOutreach)
            .filter(ScheduledOutreach.session_id == session_id)
            .update({"session_id": None}, synchronize_session=False)
        )
        (
            db.query(Escalation)
            .filter(Escalation.session_id == session_id)
            .update({"session_id": None}, synchronize_session=False)
        )
        (
            db.query(DoctorRecommendation)
            .filter(DoctorRecommendation.session_id == session_id)
            .update({"session_id": None}, synchronize_session=False)
        )
        (
            db.query(ConversationMessage)
            .filter(ConversationMessage.session_id == session_id)
            .delete(synchronize_session=False)
        )
        db.delete(session)
        db.commit()
        return True


def create_escalation(
    user_id: int,
    session_id: int | None,
    severity: str,
    trigger_reason: str,
    brief_json: str | dict,
    medication_id: int | None = None,
) -> dict:
    serialized_brief = brief_json
    if isinstance(brief_json, dict):
        serialized_brief = json.dumps(brief_json)

    with SessionLocal(expire_on_commit=False) as db:
        escalation = Escalation(
            user_id=user_id,
            session_id=session_id,
            medication_id=medication_id,
            severity=(severity or "moderate").strip().lower(),
            trigger_reason=(trigger_reason or "").strip()[:200],
            status="open",
            brief_json=str(serialized_brief).strip(),
        )
        db.add(escalation)
        db.commit()
        db.refresh(escalation)
        return _serialize_escalation(escalation)


def get_open_escalations(limit: int = 50) -> list[dict]:
    severity_order = {"urgent": 0, "serious": 1, "moderate": 2}
    fallback_time = datetime.now(timezone.utc)
    with SessionLocal() as db:
        escalations = db.query(Escalation).filter(Escalation.status == "open").all()
        escalations.sort(
            key=lambda escalation: (
                severity_order.get(str(escalation.severity or "").lower(), 9),
                escalation.created_at or fallback_time,
                escalation.id or 0,
            )
        )
        return [_serialize_escalation(escalation) for escalation in escalations[:limit]]


def get_escalations_for_user(user_id: int, status: str | None = None) -> list[dict]:
    with SessionLocal() as db:
        query = db.query(Escalation).filter(Escalation.user_id == user_id)
        if isinstance(status, str) and status.strip():
            query = query.filter(Escalation.status == status.strip().lower())
        escalations = query.order_by(Escalation.created_at.desc(), Escalation.id.desc()).all()
        return [_serialize_escalation(escalation) for escalation in escalations]


def update_escalation_status(
    escalation_id: int,
    status: str,
    resolution_notes: str | None = None,
) -> dict | None:
    normalized_status = (status or "").strip().lower()
    now = datetime.now(timezone.utc)
    with SessionLocal(expire_on_commit=False) as db:
        escalation = db.query(Escalation).filter(Escalation.id == escalation_id).first()
        if not escalation:
            return None

        escalation.status = normalized_status or escalation.status
        if escalation.status in {"acknowledged", "in_review"} and escalation.acknowledged_at is None:
            escalation.acknowledged_at = now
        if escalation.status in {"resolved", "dismissed"}:
            escalation.resolved_at = now
        if resolution_notes is not None:
            escalation.resolution_notes = resolution_notes.strip() if resolution_notes.strip() else None
        db.commit()
        db.refresh(escalation)
        return _serialize_escalation(escalation)


def get_escalation_by_id(escalation_id: int) -> dict | None:
    with SessionLocal() as db:
        escalation = db.query(Escalation).filter(Escalation.id == escalation_id).first()
        if not escalation:
            return None
        return _serialize_escalation(escalation)


def delete_user(user_id: int):
    with SessionLocal() as db:
        session_ids = [row[0] for row in db.query(Session.id).filter(Session.user_id == user_id).all()]
        medication_ids = [row[0] for row in db.query(Medication.id).filter(Medication.user_id == user_id).all()]
        if medication_ids:
            db.query(MedicationEvent).filter(MedicationEvent.medication_id.in_(medication_ids)).delete(
                synchronize_session=False
            )
            db.query(SideEffect).filter(SideEffect.medication_id.in_(medication_ids)).delete(
                synchronize_session=False
            )
        if session_ids:
            db.query(ConversationMessage).filter(ConversationMessage.session_id.in_(session_ids)).delete(
                synchronize_session=False
            )
        db.query(Vital).filter(Vital.user_id == user_id).delete()
        db.query(RefillRequest).filter(RefillRequest.user_id == user_id).delete(synchronize_session=False)
        db.query(Escalation).filter(Escalation.user_id == user_id).delete(synchronize_session=False)
        db.query(ScheduledOutreach).filter(ScheduledOutreach.user_id == user_id).delete(synchronize_session=False)
        db.query(DoctorRecommendation).filter(DoctorRecommendation.user_id == user_id).delete(synchronize_session=False)
        db.query(MedicationEvent).filter(MedicationEvent.user_id == user_id).delete(synchronize_session=False)
        db.query(SideEffect).filter(SideEffect.user_id == user_id).delete(synchronize_session=False)
        db.query(Medication).filter(Medication.user_id == user_id).delete(synchronize_session=False)
        db.query(ConversationMessage).filter(ConversationMessage.user_id == user_id).delete(synchronize_session=False)
        db.query(Session).filter(Session.user_id == user_id).delete()
        db.query(User).filter(User.id == user_id).delete()
        db.commit()


def get_user_by_email(email: str) -> User | None:
    with SessionLocal(expire_on_commit=False) as db:
        return db.query(User).filter(User.email.ilike(email.strip())).first()


def get_user_by_id(user_id: int) -> User | None:
    with SessionLocal(expire_on_commit=False) as db:
        return db.query(User).filter(User.id == user_id).first()


def create_audit_log(
    action: str,
    resource_type: str,
    resource_id: int | None = None,
    user_id: int | None = None,
    actor_id: int | None = None,
    actor_type: str = "user",
    ip_address: str | None = None,
    details: str | None = None,
) -> dict:
    with SessionLocal(expire_on_commit=False) as db:
        entry = AuditLog(
            user_id=user_id,
            actor_id=actor_id,
            actor_type=(actor_type or "user").strip().lower()[:20] or "user",
            action=(action or "").strip()[:50],
            resource_type=(resource_type or "").strip()[:50],
            resource_id=resource_id,
            ip_address=(ip_address or "").strip()[:45] or None,
            details=(details or "").strip() or None,
        )
        db.add(entry)
        db.commit()
        db.refresh(entry)
        return _serialize_audit_log(entry)


def get_audit_log(
    limit: int = 100,
    action: str | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
) -> list[dict]:
    bounded_limit = max(1, min(int(limit), 1000))
    with SessionLocal() as db:
        query = db.query(AuditLog)
        if isinstance(action, str) and action.strip():
            query = query.filter(AuditLog.action == action.strip())
        if start_date is not None:
            query = query.filter(AuditLog.created_at >= _as_utc_datetime(start_date))
        if end_date is not None:
            query = query.filter(AuditLog.created_at <= _as_utc_datetime(end_date))
        entries = (
            query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(bounded_limit)
            .all()
        )
        return [_serialize_audit_log(entry) for entry in entries]


def get_audit_events_for_user(user_id: int) -> list[dict]:
    with SessionLocal() as db:
        entries = (
            db.query(AuditLog)
            .filter(AuditLog.user_id == user_id)
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .all()
        )
        return [_serialize_audit_log(entry) for entry in entries]


def get_recent_activity(limit: int = 20) -> list[dict]:
    bounded_limit = max(1, min(int(limit), 200))
    with SessionLocal() as db:
        medication_events = (
            db.query(MedicationEvent)
            .options(joinedload(MedicationEvent.user), joinedload(MedicationEvent.medication))
            .filter(MedicationEvent.event_type.in_(("dose_taken", "dose_missed", "dose_skipped", "dose_adjusted")))
            .order_by(MedicationEvent.recorded_at.desc(), MedicationEvent.id.desc())
            .limit(bounded_limit)
            .all()
        )
        escalations = (
            db.query(Escalation)
            .options(joinedload(Escalation.user), joinedload(Escalation.medication))
            .order_by(Escalation.created_at.desc(), Escalation.id.desc())
            .limit(bounded_limit)
            .all()
        )
        completed_outreach = (
            db.query(ScheduledOutreach)
            .options(joinedload(ScheduledOutreach.user), joinedload(ScheduledOutreach.medication))
            .filter(
                ScheduledOutreach.status == "completed",
                ScheduledOutreach.completed_at.isnot(None),
            )
            .order_by(ScheduledOutreach.completed_at.desc(), ScheduledOutreach.id.desc())
            .limit(bounded_limit)
            .all()
        )
        flagged_refills = (
            db.query(RefillRequest)
            .options(joinedload(RefillRequest.user), joinedload(RefillRequest.medication))
            .filter(RefillRequest.flagged_at.isnot(None))
            .order_by(RefillRequest.flagged_at.desc(), RefillRequest.id.desc())
            .limit(bounded_limit)
            .all()
        )

        activities: list[dict] = []

        for event in medication_events:
            medication_name = (
                event.medication.brand_name
                if event.medication and event.medication.brand_name
                else (event.medication.drug_name if event.medication else "medication")
            )
            activity_type = "dose_logged"
            if event.event_type in {"dose_missed", "dose_skipped"}:
                activity_type = "dose_missed"
            elif event.event_type == "dose_adjusted":
                activity_type = "dose_adjusted"

            dose_text = f"{event.dose_mg}mg " if event.dose_mg is not None else ""
            if event.event_type == "dose_taken":
                summary = f"Logged a {dose_text}{medication_name} dose".replace("  ", " ").strip()
            elif event.event_type == "dose_adjusted":
                summary = (
                    f"Dose adjusted to {dose_text}for {medication_name}"
                    if dose_text
                    else f"Dose adjusted for {medication_name}"
                ).replace("  ", " ").strip()
            else:
                summary = f"Reported a missed {medication_name} dose"

            activities.append(
                {
                    "activity_id": event.id,
                    "activity_type": activity_type,
                    "user_id": event.user_id,
                    "user_name": event.user.name if event.user else None,
                    "occurred_at": event.recorded_at.isoformat() if event.recorded_at else None,
                    "summary": summary,
                    "resource_type": "medication_event",
                    "resource_id": event.id,
                }
            )

        for escalation in escalations:
            activities.append(
                {
                    "activity_id": escalation.id,
                    "activity_type": "escalation_created",
                    "user_id": escalation.user_id,
                    "user_name": escalation.user.name if escalation.user else None,
                    "occurred_at": escalation.created_at.isoformat() if escalation.created_at else None,
                    "summary": escalation.trigger_reason,
                    "resource_type": "escalation",
                    "resource_id": escalation.id,
                }
            )

        for outreach in completed_outreach:
            activities.append(
                {
                    "activity_id": outreach.id,
                    "activity_type": "outreach_completed",
                    "user_id": outreach.user_id,
                    "user_name": outreach.user.name if outreach.user else None,
                    "occurred_at": outreach.completed_at.isoformat() if outreach.completed_at else None,
                    "summary": f"Completed {str(outreach.outreach_type or 'outreach').replace('_', ' ')} outreach",
                    "resource_type": "outreach",
                    "resource_id": outreach.id,
                }
            )

        for refill in flagged_refills:
            medication_name = (
                refill.medication.brand_name
                if refill.medication and refill.medication.brand_name
                else (refill.medication.drug_name if refill.medication else "medication")
            )
            activities.append(
                {
                    "activity_id": refill.id,
                    "activity_type": "refill_flagged",
                    "user_id": refill.user_id,
                    "user_name": refill.user.name if refill.user else None,
                    "occurred_at": refill.flagged_at.isoformat() if refill.flagged_at else None,
                    "summary": f"Flagged a {medication_name} refill for the care team",
                    "resource_type": "refill",
                    "resource_id": refill.id,
                }
            )

        activities.sort(
            key=lambda item: (
                _as_utc_datetime(datetime.fromisoformat(str(item["occurred_at"]).replace("Z", "+00:00")))
                if item.get("occurred_at")
                else datetime.min.replace(tzinfo=timezone.utc),
                int(item.get("activity_id") or 0),
            ),
            reverse=True,
        )
        return activities[:bounded_limit]


def get_all_users_with_activity(days_back: int = 90) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(int(days_back), 0))
    with SessionLocal() as db:
        real_sessions = _real_sessions_subquery(db)
        session_summary = (
            db.query(
                real_sessions.c.user_id.label("user_id"),
                func.min(real_sessions.c.started_at).label("first_session_at"),
                func.max(real_sessions.c.started_at).label("last_session_at"),
                func.count(real_sessions.c.session_id).label("session_count"),
            )
            .group_by(real_sessions.c.user_id)
            .subquery()
        )
        vital_summary = (
            db.query(
                Vital.user_id.label("user_id"),
                func.max(Vital.recorded_at).label("last_vital_at"),
                func.count(Vital.id).label("vital_count"),
            )
            .group_by(Vital.user_id)
            .subquery()
        )

        rows = (
            db.query(
                User.id.label("user_id"),
                User.created_at.label("created_at"),
                session_summary.c.first_session_at,
                session_summary.c.last_session_at,
                session_summary.c.session_count,
                vital_summary.c.last_vital_at,
                vital_summary.c.vital_count,
            )
            .join(session_summary, session_summary.c.user_id == User.id)
            .outerjoin(vital_summary, vital_summary.c.user_id == User.id)
            .filter(User.created_at >= cutoff)
            .order_by(session_summary.c.first_session_at.asc(), User.id.asc())
            .all()
        )

        results: list[dict] = []
        for row in rows:
            first_session_at = _as_utc_datetime(row.first_session_at)
            last_session_at = _as_utc_datetime(row.last_session_at)
            last_vital_at = _as_utc_datetime(row.last_vital_at)
            activity_candidates = [candidate for candidate in (last_session_at, last_vital_at) if candidate is not None]
            last_activity_at = max(activity_candidates) if activity_candidates else None
            results.append(
                {
                    "user_id": row.user_id,
                    "created_at": _as_utc_datetime(row.created_at).isoformat() if row.created_at else None,
                    "first_session_date": first_session_at.isoformat() if first_session_at else None,
                    "last_activity_date": last_activity_at.isoformat() if last_activity_at else None,
                    "session_count": int(row.session_count or 0),
                    "vital_count": int(row.vital_count or 0),
                }
            )

        return results


def get_daily_active_users(start_date: date | datetime, end_date: date | datetime) -> list[dict]:
    start_dt, end_dt = _normalize_analytics_range(start_date, end_date)
    with SessionLocal() as db:
        real_sessions = _real_sessions_subquery(db)
        session_activity = (
            db.query(
                func.date(real_sessions.c.started_at).label("activity_date"),
                real_sessions.c.user_id.label("user_id"),
            )
            .filter(real_sessions.c.started_at >= start_dt, real_sessions.c.started_at < end_dt)
        )
        vital_activity = (
            db.query(
                func.date(Vital.recorded_at).label("activity_date"),
                Vital.user_id.label("user_id"),
            )
            .filter(Vital.user_id.isnot(None), Vital.recorded_at >= start_dt, Vital.recorded_at < end_dt)
        )
        activity = session_activity.union_all(vital_activity).subquery()
        rows = (
            db.query(
                activity.c.activity_date,
                func.count(func.distinct(activity.c.user_id)).label("active_users"),
            )
            .group_by(activity.c.activity_date)
            .order_by(activity.c.activity_date.asc())
            .all()
        )
        counts = {str(row.activity_date): int(row.active_users or 0) for row in rows}

    results: list[dict] = []
    current_day = start_dt.date()
    final_day = (end_dt - timedelta(days=1)).date()
    while current_day <= final_day:
        day_key = current_day.isoformat()
        results.append({"date": day_key, "active_users": counts.get(day_key, 0)})
        current_day += timedelta(days=1)
    return results


def count_sessions_in_range(start_date: date | datetime, end_date: date | datetime) -> int:
    start_dt, end_dt = _normalize_analytics_range(start_date, end_date)
    with SessionLocal() as db:
        real_sessions = _real_sessions_subquery(db)
        total = (
            db.query(func.count(real_sessions.c.session_id))
            .filter(real_sessions.c.started_at >= start_dt, real_sessions.c.started_at < end_dt)
            .scalar()
        )
        return int(total or 0)


def count_escalations_by_status(
    start_date: date | datetime | None = None,
    end_date: date | datetime | None = None,
) -> dict:
    with SessionLocal() as db:
        query = db.query(Escalation.status, func.count(Escalation.id).label("count"))
        if start_date is not None:
            query = query.filter(Escalation.created_at >= _coerce_analytics_datetime(start_date))
        if end_date is not None:
            if isinstance(end_date, datetime):
                end_dt = _coerce_analytics_datetime(end_date)
            else:
                end_dt = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=timezone.utc)
            query = query.filter(Escalation.created_at < end_dt)

        rows = query.group_by(Escalation.status).all()
        return {str(status or "unknown"): int(count or 0) for status, count in rows}


def get_session_topics_summary(start_date: date | datetime, end_date: date | datetime) -> list[dict]:
    start_dt, end_dt = _normalize_analytics_range(start_date, end_date)
    refill_keywords = (
        "refill",
        "running out",
        "running low",
        "need more",
        "prescription",
        "pharmacy",
        "renew",
        "renewal",
        "almost out",
        "pen is almost empty",
    )
    dose_keywords = (
        "dose",
        "shot",
        "injection",
        "inject",
        "missed dose",
        "skipped",
        "next dose",
        "when should i take",
        "when should i inject",
        "titration",
    )
    side_effect_keywords = (
        "side effect",
        "symptom",
        "nausea",
        "vomit",
        "throwing up",
        "constipat",
        "diarrhea",
        "bloat",
        "reflux",
        "heartburn",
        "stomach pain",
        "appetite",
        "injection site",
        "sulfur burp",
        "hair loss",
        "pancreatitis",
        "gallbladder",
    )
    vital_keywords = (
        "blood pressure",
        "heart rate",
        "pulse",
        "oxygen",
        "temperature",
        "fever",
        "glucose",
        "blood sugar",
        "weight",
    )
    dose_event_types = {"dose_taken", "dose_missed", "dose_skipped", "dose_adjusted"}
    refill_event_types = {"refill_due", "refill_confirmed"}

    with SessionLocal() as db:
        real_sessions = _real_sessions_subquery(db)
        escalated_sessions = (
            db.query(Escalation.session_id.label("session_id"))
            .filter(Escalation.session_id.isnot(None))
            .distinct()
            .subquery()
        )
        session_rows = (
            db.query(
                real_sessions.c.session_id,
                real_sessions.c.user_id,
                real_sessions.c.started_at,
                real_sessions.c.ended_at,
                real_sessions.c.last_message_at,
            )
            .outerjoin(escalated_sessions, escalated_sessions.c.session_id == real_sessions.c.session_id)
            .filter(
                real_sessions.c.started_at >= start_dt,
                real_sessions.c.started_at < end_dt,
                escalated_sessions.c.session_id.is_(None),
            )
            .order_by(real_sessions.c.started_at.asc(), real_sessions.c.session_id.asc())
            .all()
        )
        if not session_rows:
            return []

        session_windows: dict[int, dict] = {}
        session_ids: list[int] = []
        user_ids: set[int] = set()
        earliest_start: datetime | None = None
        latest_end: datetime | None = None
        for row in session_rows:
            session_id = int(row.session_id)
            user_id = int(row.user_id)
            started_at = _as_utc_datetime(row.started_at) or start_dt
            window_end = _session_window_end(row.started_at, row.ended_at, row.last_message_at)
            session_windows[session_id] = {
                "user_id": user_id,
                "started_at": started_at,
                "ended_at": window_end,
            }
            session_ids.append(session_id)
            user_ids.add(user_id)
            if earliest_start is None or started_at < earliest_start:
                earliest_start = started_at
            if latest_end is None or window_end > latest_end:
                latest_end = window_end

        messages_by_session: dict[int, list[str]] = {}
        message_rows = (
            db.query(ConversationMessage.session_id, ConversationMessage.content)
            .filter(
                ConversationMessage.session_id.in_(session_ids),
                ConversationMessage.role == "user",
            )
            .all()
        )
        for row in message_rows:
            messages_by_session.setdefault(int(row.session_id), []).append(str(row.content or "").strip().lower())

        earliest_query = (earliest_start or start_dt) - timedelta(minutes=5)
        latest_query = (latest_end or end_dt) + timedelta(minutes=5)

        vitals_by_user: dict[int, list[dict]] = {}
        vital_rows = (
            db.query(Vital.user_id, Vital.vital_type, Vital.recorded_at)
            .filter(
                Vital.user_id.in_(tuple(user_ids)),
                Vital.recorded_at >= earliest_query,
                Vital.recorded_at < latest_query,
            )
            .all()
        )
        for row in vital_rows:
            vitals_by_user.setdefault(int(row.user_id), []).append(
                {
                    "vital_type": str(row.vital_type or "").strip().lower(),
                    "recorded_at": _as_utc_datetime(row.recorded_at),
                }
            )

        side_effects_by_user: dict[int, list[dict]] = {}
        side_effect_rows = (
            db.query(SideEffect.user_id, SideEffect.symptom, SideEffect.reported_at)
            .filter(
                SideEffect.user_id.in_(tuple(user_ids)),
                SideEffect.reported_at >= earliest_query,
                SideEffect.reported_at < latest_query,
            )
            .all()
        )
        for row in side_effect_rows:
            side_effects_by_user.setdefault(int(row.user_id), []).append(
                {
                    "symptom": str(row.symptom or "").strip().lower(),
                    "reported_at": _as_utc_datetime(row.reported_at),
                }
            )

        medication_events_by_user: dict[int, list[dict]] = {}
        medication_event_rows = (
            db.query(MedicationEvent.user_id, MedicationEvent.event_type, MedicationEvent.recorded_at)
            .filter(
                MedicationEvent.user_id.in_(tuple(user_ids)),
                MedicationEvent.recorded_at >= earliest_query,
                MedicationEvent.recorded_at < latest_query,
            )
            .all()
        )
        for row in medication_event_rows:
            medication_events_by_user.setdefault(int(row.user_id), []).append(
                {
                    "event_type": str(row.event_type or "").strip().lower(),
                    "recorded_at": _as_utc_datetime(row.recorded_at),
                }
            )

        def in_window(timestamp: datetime | None, window_start: datetime, window_end: datetime) -> bool:
            if timestamp is None:
                return False
            return window_start <= timestamp <= window_end

        topic_counts: dict[str, int] = {}
        for session_id in session_ids:
            window = session_windows[session_id]
            user_id = int(window["user_id"])
            window_start = window["started_at"]
            window_end = window["ended_at"]
            user_messages = " ".join(messages_by_session.get(session_id, []))

            has_refill_keyword = any(keyword in user_messages for keyword in refill_keywords)
            has_dose_keyword = any(keyword in user_messages for keyword in dose_keywords)
            has_side_effect_keyword = any(keyword in user_messages for keyword in side_effect_keywords)
            has_vital_keyword = any(keyword in user_messages for keyword in vital_keywords)

            side_effect_logged = any(
                in_window(effect.get("reported_at"), window_start, window_end)
                for effect in side_effects_by_user.get(user_id, [])
            )
            symptom_vital_logged = any(
                vital.get("vital_type") == "symptom" and in_window(vital.get("recorded_at"), window_start, window_end)
                for vital in vitals_by_user.get(user_id, [])
            )
            vital_logged = any(
                vital.get("vital_type") != "symptom" and in_window(vital.get("recorded_at"), window_start, window_end)
                for vital in vitals_by_user.get(user_id, [])
            )
            refill_event_logged = any(
                event.get("event_type") in refill_event_types and in_window(event.get("recorded_at"), window_start, window_end)
                for event in medication_events_by_user.get(user_id, [])
            )
            dose_event_logged = any(
                event.get("event_type") in dose_event_types and in_window(event.get("recorded_at"), window_start, window_end)
                for event in medication_events_by_user.get(user_id, [])
            )

            if side_effect_logged or symptom_vital_logged or has_side_effect_keyword:
                topic = "side_effect_question"
            elif refill_event_logged or has_refill_keyword:
                topic = "refill_request"
            elif dose_event_logged or has_dose_keyword:
                topic = "dose_timing"
            elif vital_logged or has_vital_keyword:
                topic = "vital_checkin"
            else:
                topic = "general_checkin"

            topic_counts[topic] = topic_counts.get(topic, 0) + 1

        return [
            {"topic": topic, "count": count}
            for topic, count in sorted(topic_counts.items(), key=lambda item: (-item[1], item[0]))
        ]


def age_from_dob(dob: date) -> int:
    today = date.today()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def create_registered_user(
    name: str, email: str, password_hash: str, dob: date | None = None
) -> User:
    age = age_from_dob(dob) if dob else None
    with SessionLocal(expire_on_commit=False) as db:
        user = User(
            name=name.strip(),
            email=email.strip().lower(),
            password_hash=password_hash,
            dob=dob,
            age=age,
        )
        db.add(user)
        db.commit()
        return user


def get_user_by_name(name: str) -> User | None:
    with SessionLocal(expire_on_commit=False) as db:
        return db.query(User).filter(User.name.ilike(name.strip())).first()


def set_session_timestamps(
    session_id: int,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
    summary: str | None = None,
) -> bool:
    with SessionLocal() as db:
        session = db.query(Session).filter(Session.id == session_id).first()
        if not session:
            return False

        if started_at is not None:
            session.started_at = _as_utc_datetime(started_at) or started_at
        if ended_at is not None:
            session.ended_at = _as_utc_datetime(ended_at) or ended_at
        if summary is not None:
            session.summary = summary
        db.commit()
        return True


def set_medication_event_timestamps(
    event_id: int,
    recorded_at: datetime | None = None,
    scheduled_at: datetime | None = None,
) -> dict | None:
    with SessionLocal(expire_on_commit=False) as db:
        event = db.query(MedicationEvent).filter(MedicationEvent.id == event_id).first()
        if not event:
            return None

        if recorded_at is not None:
            event.recorded_at = _as_utc_datetime(recorded_at) or recorded_at
        if scheduled_at is not None:
            event.scheduled_at = _as_utc_datetime(scheduled_at) or scheduled_at
        db.commit()
        db.refresh(event)
        return _serialize_medication_event(event)


def set_side_effect_timestamps(
    side_effect_id: int,
    reported_at: datetime | None = None,
    resolved_at: datetime | None = None,
    update_resolved_at: bool = False,
) -> dict | None:
    with SessionLocal(expire_on_commit=False) as db:
        side_effect = db.query(SideEffect).filter(SideEffect.id == side_effect_id).first()
        if not side_effect:
            return None

        if reported_at is not None:
            side_effect.reported_at = _as_utc_datetime(reported_at) or reported_at
        if update_resolved_at:
            side_effect.resolved_at = _as_utc_datetime(resolved_at) if resolved_at is not None else None
        db.commit()
        db.refresh(side_effect)
        return _serialize_side_effect(side_effect)


def set_refill_timestamps(
    refill_id: int,
    created_at: datetime | None = None,
    requested_at: datetime | None = None,
    flagged_at: datetime | None = None,
    confirmed_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> dict | None:
    with SessionLocal(expire_on_commit=False) as db:
        refill = db.query(RefillRequest).filter(RefillRequest.id == refill_id).first()
        if not refill:
            return None

        if created_at is not None:
            refill.created_at = _as_utc_datetime(created_at) or created_at
        if requested_at is not None:
            refill.requested_at = _as_utc_datetime(requested_at) or requested_at
        if flagged_at is not None:
            refill.flagged_at = _as_utc_datetime(flagged_at) or flagged_at
        if confirmed_at is not None:
            refill.confirmed_at = _as_utc_datetime(confirmed_at) or confirmed_at
        if completed_at is not None:
            refill.completed_at = _as_utc_datetime(completed_at) or completed_at
        db.commit()
        db.refresh(refill)
        return _serialize_refill_request(refill)


def set_outreach_timestamps(
    outreach_id: int,
    created_at: datetime | None = None,
    scheduled_at: datetime | None = None,
    attempted_at: datetime | None = None,
    completed_at: datetime | None = None,
    attempt_count: int | None = None,
    status: str | None = None,
    session_id: int | None = None,
) -> dict | None:
    with SessionLocal(expire_on_commit=False) as db:
        outreach = db.query(ScheduledOutreach).filter(ScheduledOutreach.id == outreach_id).first()
        if not outreach:
            return None

        if created_at is not None:
            outreach.created_at = _as_utc_datetime(created_at) or created_at
        if scheduled_at is not None:
            outreach.scheduled_at = _as_utc_datetime(scheduled_at) or scheduled_at
        if attempted_at is not None:
            outreach.attempted_at = _as_utc_datetime(attempted_at) or attempted_at
        if completed_at is not None:
            outreach.completed_at = _as_utc_datetime(completed_at) or completed_at
        if attempt_count is not None:
            outreach.attempt_count = int(attempt_count)
        if status is not None:
            outreach.status = status.strip().lower()
        if session_id is not None:
            outreach.session_id = session_id
        db.commit()
        db.refresh(outreach)
        return _serialize_outreach(outreach)


def set_escalation_timestamps(
    escalation_id: int,
    created_at: datetime | None = None,
    acknowledged_at: datetime | None = None,
    resolved_at: datetime | None = None,
    status: str | None = None,
) -> dict | None:
    with SessionLocal(expire_on_commit=False) as db:
        escalation = db.query(Escalation).filter(Escalation.id == escalation_id).first()
        if not escalation:
            return None

        if created_at is not None:
            escalation.created_at = _as_utc_datetime(created_at) or created_at
        if acknowledged_at is not None:
            escalation.acknowledged_at = _as_utc_datetime(acknowledged_at) or acknowledged_at
        if resolved_at is not None:
            escalation.resolved_at = _as_utc_datetime(resolved_at) or resolved_at
        if status is not None:
            escalation.status = status.strip().lower()
        db.commit()
        db.refresh(escalation)
        return _serialize_escalation(escalation)


def set_doctor_recommendation_created_at(
    recommendation_id: int,
    created_at: datetime,
) -> dict | None:
    with SessionLocal(expire_on_commit=False) as db:
        recommendation = db.query(DoctorRecommendation).filter(DoctorRecommendation.id == recommendation_id).first()
        if not recommendation:
            return None

        recommendation.created_at = _as_utc_datetime(created_at) or created_at
        db.commit()
        db.refresh(recommendation)
        return _serialize_doctor_recommendation(recommendation)
