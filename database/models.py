from datetime import date, datetime, timezone
from sqlalchemy import Column, Date, Float, Integer, String, DateTime, ForeignKey, Text
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    email = Column(String(255), unique=True, nullable=True, index=True)
    password_hash = Column(String(255), nullable=True)
    dob = Column(Date, nullable=True)
    age = Column(Integer, nullable=True)
    weight_kg = Column(Float, nullable=True)
    height_cm = Column(Float, nullable=True)
    address = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    vitals = relationship("Vital", back_populates="user", order_by="Vital.recorded_at")
    sessions = relationship("Session", back_populates="user")
    conversation_messages = relationship("ConversationMessage", back_populates="user")
    medications = relationship("Medication", back_populates="user", order_by="Medication.created_at")
    medication_events = relationship("MedicationEvent", back_populates="user", order_by="MedicationEvent.recorded_at")
    side_effects = relationship("SideEffect", back_populates="user", order_by="SideEffect.reported_at")
    scheduled_outreach = relationship(
        "ScheduledOutreach",
        back_populates="user",
        order_by="ScheduledOutreach.scheduled_at",
    )
    escalations = relationship(
        "Escalation",
        back_populates="user",
        order_by="Escalation.created_at",
    )
    refill_requests = relationship(
        "RefillRequest",
        back_populates="user",
        order_by="RefillRequest.created_at",
    )


class Vital(Base):
    __tablename__ = "vitals"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    vital_type = Column(String(50), nullable=False)
    value = Column(String(100), nullable=False)
    unit = Column(String(20), nullable=True)
    notes = Column(Text, nullable=True)
    recorded_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="vitals")


class Session(Base):
    __tablename__ = "sessions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    started_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    ended_at = Column(DateTime, nullable=True)
    summary = Column(Text, nullable=True)
    timezone_name = Column(String(80), nullable=True)
    utc_offset_minutes = Column(Integer, nullable=True)

    user = relationship("User", back_populates="sessions")
    messages = relationship("ConversationMessage", back_populates="session", order_by="ConversationMessage.created_at")
    outreach_items = relationship(
        "ScheduledOutreach",
        back_populates="session",
        order_by="ScheduledOutreach.scheduled_at",
    )
    escalations = relationship(
        "Escalation",
        back_populates="session",
        order_by="Escalation.created_at",
    )


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("sessions.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    role = Column(String(20), nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)

    session = relationship("Session", back_populates="messages")
    user = relationship("User", back_populates="conversation_messages")


class Medication(Base):
    __tablename__ = "medications"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    drug_name = Column(String(100), nullable=False)
    brand_name = Column(String(100), nullable=True)
    current_dose_mg = Column(Float, nullable=False)
    frequency = Column(String(50), nullable=False, default="weekly")
    route = Column(String(50), nullable=False, default="subcutaneous injection")
    start_date = Column(Date, nullable=False)
    titration_step = Column(Integer, nullable=False, default=1)
    status = Column(String(20), nullable=False, default="active")
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    user = relationship("User", back_populates="medications")
    medication_events = relationship(
        "MedicationEvent",
        back_populates="medication",
        order_by="MedicationEvent.recorded_at",
    )
    side_effects = relationship(
        "SideEffect",
        back_populates="medication",
        order_by="SideEffect.reported_at",
    )
    scheduled_outreach = relationship(
        "ScheduledOutreach",
        back_populates="medication",
        order_by="ScheduledOutreach.scheduled_at",
    )
    escalations = relationship(
        "Escalation",
        back_populates="medication",
        order_by="Escalation.created_at",
    )
    refill_requests = relationship(
        "RefillRequest",
        back_populates="medication",
        order_by="RefillRequest.created_at",
    )


class MedicationEvent(Base):
    __tablename__ = "medication_events"

    id = Column(Integer, primary_key=True, index=True)
    medication_id = Column(Integer, ForeignKey("medications.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    event_type = Column(String(30), nullable=False)
    dose_mg = Column(Float, nullable=True)
    scheduled_at = Column(DateTime, nullable=True)
    recorded_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    notes = Column(Text, nullable=True)

    medication = relationship("Medication", back_populates="medication_events")
    user = relationship("User", back_populates="medication_events")


class SideEffect(Base):
    __tablename__ = "side_effects"

    id = Column(Integer, primary_key=True, index=True)
    medication_id = Column(Integer, ForeignKey("medications.id"), nullable=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    symptom = Column(String(100), nullable=False)
    severity = Column(String(20), nullable=False)
    titration_step = Column(Integer, nullable=True)
    reported_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    resolved_at = Column(DateTime, nullable=True)
    notes = Column(Text, nullable=True)

    medication = relationship("Medication", back_populates="side_effects")
    user = relationship("User", back_populates="side_effects")


class ScheduledOutreach(Base):
    __tablename__ = "scheduled_outreach"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    medication_id = Column(Integer, ForeignKey("medications.id"), nullable=True, index=True)
    outreach_type = Column(String(50), nullable=False)
    channel = Column(String(20), nullable=False, default="call")
    status = Column(String(20), nullable=False, default="pending")
    scheduled_at = Column(DateTime, nullable=False, index=True)
    attempted_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    attempt_count = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=3)
    priority = Column(Integer, nullable=False, default=5)
    context_json = Column(Text, nullable=True)
    outcome_summary = Column(Text, nullable=True)
    session_id = Column(Integer, ForeignKey("sessions.id"), nullable=True, index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="scheduled_outreach")
    medication = relationship("Medication", back_populates="scheduled_outreach")
    session = relationship("Session", back_populates="outreach_items")


class Escalation(Base):
    __tablename__ = "escalations"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    session_id = Column(Integer, ForeignKey("sessions.id"), nullable=True, index=True)
    medication_id = Column(Integer, ForeignKey("medications.id"), nullable=True, index=True)
    severity = Column(String(20), nullable=False)
    trigger_reason = Column(String(200), nullable=False)
    status = Column(String(20), nullable=False, default="open")
    brief_json = Column(Text, nullable=False)
    assigned_to = Column(String(100), nullable=True)
    acknowledged_at = Column(DateTime, nullable=True)
    resolved_at = Column(DateTime, nullable=True)
    resolution_notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="escalations")
    session = relationship("Session", back_populates="escalations")
    medication = relationship("Medication", back_populates="escalations")


class RefillRequest(Base):
    __tablename__ = "refill_requests"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    medication_id = Column(Integer, ForeignKey("medications.id"), nullable=False, index=True)
    status = Column(String(20), nullable=False, default="due")
    due_date = Column(Date, nullable=True)
    requested_at = Column(DateTime, nullable=True)
    flagged_at = Column(DateTime, nullable=True)
    confirmed_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    pharmacy_name = Column(String(200), nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="refill_requests")
    medication = relationship("Medication", back_populates="refill_requests")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    actor_id = Column(Integer, nullable=True)
    actor_type = Column(String(20), nullable=False, default="user")
    action = Column(String(50), nullable=False)
    resource_type = Column(String(50), nullable=False)
    resource_id = Column(Integer, nullable=True)
    ip_address = Column(String(45), nullable=True)
    details = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
