import json
import hashlib
import logging
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timezone

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv

import re as _re

from database.db import init_db
from database import db as database
from agent import ConversationSession
from auth import hash_password, verify_password, create_access_token, decode_access_token
from services.glp1_protocols import identify_drug
from services import analytics, audit, doctor_finder, geocoding, outreach_scheduler, tts
from stt_service import LocalTranscriber, TranscriptEnhancer

load_dotenv()

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    shared_client = httpx.AsyncClient(timeout=30.0)
    tts.http_client = shared_client
    geocoding.http_client = shared_client
    doctor_finder.http_client = shared_client
    try:
        yield
    finally:
        await shared_client.aclose()


app = FastAPI(title="CuraConnect Voice Assistant", lifespan=lifespan)
transcript_enhancer = TranscriptEnhancer()
local_transcriber = LocalTranscriber()

cors_origins = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173,http://localhost:8000,http://127.0.0.1:8000",
    ).split(",")
    if origin.strip()
]

# Wildcard origin is incompatible with credentials per the CORS spec, so
# drop credentials when we intentionally fall back to "*".
if cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )


class SttEnhanceRequest(BaseModel):
    transcript: str = Field(..., min_length=1)
    expected_keywords: list[str] = Field(default_factory=list)
    custom_terms: list[str] = Field(default_factory=list)


class SttEnhanceResponse(BaseModel):
    transcript: str
    raw_transcript: str
    numeric_corrections: list[dict]
    phonetic_corrections: list[dict]
    confusable_flags: list[dict]
    latency_ms: int


class SttTranscribeResponse(BaseModel):
    transcript: str
    raw_transcript: str
    confidence: float
    source: str
    numeric_corrections: list[dict]
    phonetic_corrections: list[dict]
    confusable_flags: list[dict]
    latency_ms: int


class RegisterRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., min_length=5, max_length=255)
    password: str = Field(..., min_length=6)
    dob: str | None = None


class LoginRequest(BaseModel):
    email: str
    password: str


class AuthResponse(BaseModel):
    token: str
    user: dict


_EMAIL_RE = _re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MEDICATION_STATUSES = {"active", "paused", "discontinued"}
_MEDICATION_EVENT_TYPES = {
    "dose_taken",
    "dose_missed",
    "dose_skipped",
    "dose_adjusted",
    "refill_due",
    "refill_confirmed",
}
_SIDE_EFFECT_SEVERITIES = {"mild", "moderate", "severe"}
_ESCALATION_STATUSES = {"open", "acknowledged", "in_review", "resolved", "dismissed"}
_REFILL_STATUSES = {"due", "reminded", "requested", "flagged_for_team", "confirmed", "completed", "cancelled"}
_AUDIT_VIEWER_USER_IDS = {
    int(value.strip())
    for value in os.getenv("AUDIT_VIEWER_USER_IDS", "").split(",")
    if value.strip().isdigit()
}


def _parse_csv_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [token.strip() for token in raw.split(",") if token.strip()]


def _parse_user_date(raw_value: str) -> date:
    """Accept MM/DD/YYYY and ISO YYYY-MM-DD date inputs."""
    candidate = raw_value.strip()
    if not candidate:
        raise ValueError("empty date value")

    # Preferred UI format first.
    try:
        return datetime.strptime(candidate, "%m/%d/%Y").date()
    except ValueError:
        pass

    # Keep backward compatibility with existing HTML date input payloads.
    return date.fromisoformat(candidate)


def _parse_datetime_value(raw_value: str) -> datetime:
    candidate = raw_value.strip()
    if not candidate:
        raise ValueError("empty datetime value")
    parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_audit_datetime_query(raw_value: str | None, field_name: str, end_of_day: bool = False) -> datetime | None:
    if raw_value is None or not raw_value.strip():
        return None

    candidate = raw_value.strip()
    try:
        return _parse_datetime_value(candidate)
    except ValueError:
        try:
            parsed_date = _parse_user_date(candidate)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid {field_name}. Use ISO datetime or YYYY-MM-DD.",
            )

    chosen_time = time.max if end_of_day else time.min
    return datetime.combine(parsed_date, chosen_time, tzinfo=timezone.utc)


def _normalize_medication_identity(drug_name: str, brand_name: str | None = None) -> tuple[str, str | None]:
    normalized_drug = drug_name.strip().lower()
    normalized_brand = brand_name.strip() if isinstance(brand_name, str) and brand_name.strip() else None
    matched_drug, matched_brand = identify_drug(" ".join(part for part in [normalized_drug, normalized_brand] if part))
    if matched_drug:
        normalized_drug = matched_drug
    if matched_brand and normalized_brand is None:
        normalized_brand = matched_brand
    return normalized_drug, normalized_brand


def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _hash_for_audit(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()[:16]


class _AuditScope:
    def __init__(
        self,
        request: Request,
        action: str,
        resource_type: str,
        actor_id: int | None = None,
        user_id: int | None = None,
        resource_id: int | None = None,
        actor_type: str = "user",
        details: str | None = None,
    ):
        self.request = request
        self.action = action
        self.resource_type = resource_type
        self.actor_id = actor_id
        self.user_id = user_id
        self.resource_id = resource_id
        self.actor_type = actor_type
        self.details = details

    def __enter__(self):
        return self

    def set_resource_id(self, resource_id: int | None):
        self.resource_id = resource_id

    def set_user_id(self, user_id: int | None):
        self.user_id = user_id

    def set_actor_type(self, actor_type: str):
        self.actor_type = actor_type

    def set_details(self, details: str | None):
        self.details = details

    def __exit__(self, exc_type, exc, tb):
        details = self.details
        if exc is not None and details is None:
            if isinstance(exc, HTTPException):
                details = f"attempt_status={exc.status_code}"
            else:
                details = "attempt_status=error"

        audit.log_audit_event(
            action=self.action,
            resource_type=self.resource_type,
            resource_id=self.resource_id,
            user_id=self.user_id,
            actor_id=self.actor_id,
            actor_type=self.actor_type,
            ip_address=get_client_ip(self.request),
            details=details,
        )
        return False


@app.post("/api/auth/register", response_model=AuthResponse)
async def register(payload: RegisterRequest, request: Request):
    email = payload.email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="Invalid email address.")

    existing = database.get_user_by_email(email)
    if existing:
        raise HTTPException(status_code=409, detail="An account with this email already exists.")

    parsed_dob: date | None = None
    if payload.dob:
        try:
            parsed_dob = _parse_user_date(payload.dob)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date of birth. Use MM/DD/YYYY.")
        if parsed_dob >= date.today():
            raise HTTPException(status_code=400, detail="Date of birth must be in the past.")

    hashed = hash_password(payload.password)
    user = database.create_registered_user(
        name=payload.name.strip(),
        email=email,
        password_hash=hashed,
        dob=parsed_dob,
    )
    audit.audit_auth_event("register", user.id, get_client_ip(request))
    token = create_access_token(user.id, user.name, user.email)
    return {"token": token, "user": _serialize_user(user)}


@app.post("/api/auth/login", response_model=AuthResponse)
async def login(payload: LoginRequest, request: Request):
    email = payload.email.strip().lower()
    user = database.get_user_by_email(email)
    if not user or not user.password_hash:
        audit.audit_auth_event(
            "login_failed",
            None,
            get_client_ip(request),
            f"invalid credentials for: {_hash_for_audit(email)}",
        )
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    if not verify_password(payload.password, user.password_hash):
        audit.audit_auth_event(
            "login_failed",
            None,
            get_client_ip(request),
            f"invalid credentials for: {_hash_for_audit(email)}",
        )
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    audit.audit_auth_event("login", user.id, get_client_ip(request))
    token = create_access_token(user.id, user.name, user.email)
    return {"token": token, "user": _serialize_user(user)}


class ProfileUpdateRequest(BaseModel):
    name: str | None = None
    dob: str | None = None
    weight_kg: float | None = None
    height_cm: float | None = None
    address: str | None = None


class MedicationCreateRequest(BaseModel):
    drug_name: str = Field(..., min_length=1, max_length=100)
    brand_name: str | None = Field(default=None, max_length=100)
    current_dose_mg: float = Field(..., gt=0)
    frequency: str = Field(default="weekly", min_length=1, max_length=50)
    route: str = Field(default="subcutaneous injection", min_length=1, max_length=50)
    start_date: str
    titration_step: int = Field(default=1, ge=1)
    status: str = Field(default="active", min_length=1, max_length=20)
    notes: str | None = None


class MedicationUpdateRequest(BaseModel):
    drug_name: str | None = Field(default=None, min_length=1, max_length=100)
    brand_name: str | None = Field(default=None, max_length=100)
    current_dose_mg: float | None = Field(default=None, gt=0)
    frequency: str | None = Field(default=None, min_length=1, max_length=50)
    route: str | None = Field(default=None, min_length=1, max_length=50)
    start_date: str | None = None
    titration_step: int | None = Field(default=None, ge=1)
    status: str | None = Field(default=None, min_length=1, max_length=20)
    notes: str | None = None


class MedicationEventCreateRequest(BaseModel):
    event_type: str = Field(..., min_length=1, max_length=30)
    dose_mg: float | None = Field(default=None, gt=0)
    scheduled_at: str | None = None
    notes: str | None = None


class SideEffectCreateRequest(BaseModel):
    medication_id: int | None = None
    symptom: str = Field(..., min_length=1, max_length=100)
    severity: str = Field(..., min_length=1, max_length=20)
    titration_step: int | None = Field(default=None, ge=1)
    notes: str | None = None


class OutreachCompleteRequest(BaseModel):
    outcome_summary: str | None = None
    session_id: int | None = None


class EscalationUpdateRequest(BaseModel):
    status: str = Field(..., min_length=1, max_length=20)
    resolution_notes: str | None = None


class RefillCreateRequest(BaseModel):
    medication_id: int = Field(..., ge=1)
    due_date: str | None = None
    pharmacy_name: str | None = Field(default=None, max_length=200)
    notes: str | None = None


class RefillUpdateRequest(BaseModel):
    status: str = Field(..., min_length=1, max_length=20)
    notes: str | None = None
    pharmacy_name: str | None = Field(default=None, max_length=200)


def _serialize_user(user) -> dict:
    age = database.age_from_dob(user.dob) if user.dob else user.age
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "age": age,
        "dob": user.dob.isoformat() if user.dob else None,
        "weight_kg": user.weight_kg,
        "height_cm": user.height_cm,
        "address": user.address,
    }


def _require_authenticated_user(token: str):
    decoded = decode_access_token(token)
    if not decoded:
        raise HTTPException(status_code=401, detail="Invalid or expired token.")
    user = database.get_user_by_id(decoded["sub"])
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    return user


def _require_audit_log_access(user, target_user_id: int | None = None) -> str:
    if target_user_id is not None and user.id == target_user_id:
        return "user"
    if user.id in _AUDIT_VIEWER_USER_IDS:
        return "clinician"
    raise HTTPException(status_code=403, detail="You do not have access to audit logs.")


def _require_clinician_access(user) -> str:
    if user.id in _AUDIT_VIEWER_USER_IDS:
        return "clinician"
    raise HTTPException(status_code=403, detail="You do not have access to this clinic dashboard resource.")


@app.get("/api/auth/me")
async def get_me(token: str, request: Request):
    payload = decode_access_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token.")
    user = database.get_user_by_id(payload["sub"])
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    with _AuditScope(
        request,
        action="view_profile",
        resource_type="user_profile",
        actor_id=user.id,
        user_id=user.id,
        resource_id=user.id,
    ):
        return _serialize_user(user)


@app.patch("/api/auth/profile")
async def update_profile(payload: ProfileUpdateRequest, token: str, request: Request):
    decoded = decode_access_token(token)
    if not decoded:
        raise HTTPException(status_code=401, detail="Invalid or expired token.")
    user = database.get_user_by_id(decoded["sub"])
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    with _AuditScope(
        request,
        action="update_profile",
        resource_type="user_profile",
        actor_id=user.id,
        user_id=user.id,
        resource_id=user.id,
    ):
        parsed_dob: date | None = None
        if payload.dob is not None:
            if payload.dob == "":
                parsed_dob = None
            else:
                try:
                    parsed_dob = _parse_user_date(payload.dob)
                except ValueError:
                    raise HTTPException(status_code=400, detail="Invalid date. Use MM/DD/YYYY.")
                if parsed_dob >= date.today():
                    raise HTTPException(status_code=400, detail="Date of birth must be in the past.")

        updated_user = database.update_user_full(
            user_id=user.id,
            name=payload.name.strip() if payload.name else None,
            dob=parsed_dob if payload.dob is not None else None,
            update_dob=payload.dob is not None,
            weight_kg=payload.weight_kg,
            update_weight=payload.weight_kg is not None,
            height_cm=payload.height_cm,
            update_height=payload.height_cm is not None,
            address=payload.address,
            update_address=payload.address is not None,
        )
        return _serialize_user(updated_user)


@app.get("/api/vitals/history")
async def vitals_history(token: str, request: Request):
    decoded = decode_access_token(token)
    if not decoded:
        raise HTTPException(status_code=401, detail="Invalid or expired token.")
    with _AuditScope(
        request,
        action="view_vitals",
        resource_type="vital",
        actor_id=decoded["sub"],
        user_id=decoded["sub"],
    ):
        return database.get_vitals_history(decoded["sub"])


@app.delete("/api/vitals/{vital_id}")
async def delete_vital(vital_id: int, token: str, request: Request):
    decoded = decode_access_token(token)
    if not decoded:
        raise HTTPException(status_code=401, detail="Invalid or expired token.")
    with _AuditScope(
        request,
        action="delete_vital",
        resource_type="vital",
        actor_id=decoded["sub"],
        user_id=decoded["sub"],
        resource_id=vital_id,
    ):
        deleted = database.delete_vital(decoded["sub"], vital_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Vital not found.")
        return {"ok": True}


@app.post("/api/medications")
async def create_medication(payload: MedicationCreateRequest, token: str, request: Request):
    user = _require_authenticated_user(token)
    with _AuditScope(
        request,
        action="create_medication",
        resource_type="medication",
        actor_id=user.id,
        user_id=user.id,
    ) as audit_scope:
        try:
            parsed_start_date = _parse_user_date(payload.start_date)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid medication start date. Use MM/DD/YYYY or YYYY-MM-DD.")

        status = payload.status.strip().lower()
        if status not in _MEDICATION_STATUSES:
            raise HTTPException(status_code=400, detail="Invalid medication status.")

        drug_name, brand_name = _normalize_medication_identity(payload.drug_name, payload.brand_name)
        medication = database.create_medication(
            user_id=user.id,
            drug_name=drug_name,
            brand_name=brand_name,
            current_dose_mg=payload.current_dose_mg,
            frequency=payload.frequency,
            route=payload.route,
            start_date=parsed_start_date,
            titration_step=payload.titration_step,
            status=status,
            notes=payload.notes,
        )
        audit_scope.set_resource_id(int(medication["id"]))
        if medication.get("status") == "active":
            outreach_scheduler.schedule_new_patient_outreach(user.id, medication["id"], medication["start_date"])
        return medication


@app.get("/api/medications")
async def list_medications(token: str, request: Request):
    user = _require_authenticated_user(token)
    with _AuditScope(
        request,
        action="view_medications",
        resource_type="medication",
        actor_id=user.id,
        user_id=user.id,
    ):
        return database.get_active_medications(user.id)


@app.get("/api/medications/{medication_id}")
async def get_medication(medication_id: int, token: str, request: Request):
    user = _require_authenticated_user(token)
    with _AuditScope(
        request,
        action="view_medication",
        resource_type="medication",
        actor_id=user.id,
        user_id=user.id,
        resource_id=medication_id,
    ):
        medication = database.get_medication_by_id(medication_id, user.id)
        if not medication:
            raise HTTPException(status_code=404, detail="Medication not found.")

        medication["events"] = database.get_medication_events(user.id, medication_id=medication_id)
        medication["side_effects"] = database.get_side_effects(user.id, medication_id=medication_id)
        return medication


@app.patch("/api/medications/{medication_id}")
async def update_medication(medication_id: int, payload: MedicationUpdateRequest, token: str, request: Request):
    user = _require_authenticated_user(token)
    with _AuditScope(
        request,
        action="update_medication",
        resource_type="medication",
        actor_id=user.id,
        user_id=user.id,
        resource_id=medication_id,
    ):
        existing = database.get_medication_by_id(medication_id, user.id)
        if not existing:
            raise HTTPException(status_code=404, detail="Medication not found.")

        updates: dict = {}
        if payload.drug_name is not None:
            normalized_drug, normalized_brand = _normalize_medication_identity(
                payload.drug_name,
                payload.brand_name if payload.brand_name is not None else existing.get("brand_name"),
            )
            updates["drug_name"] = normalized_drug
            if payload.brand_name is not None:
                updates["brand_name"] = normalized_brand or payload.brand_name.strip()
        elif payload.brand_name is not None:
            matched_drug, matched_brand = identify_drug(payload.brand_name)
            updates["brand_name"] = matched_brand or payload.brand_name.strip()
            if matched_drug and not existing.get("drug_name"):
                updates["drug_name"] = matched_drug

        if payload.current_dose_mg is not None:
            updates["current_dose_mg"] = payload.current_dose_mg
        if payload.frequency is not None:
            updates["frequency"] = payload.frequency
        if payload.route is not None:
            updates["route"] = payload.route
        if payload.start_date is not None:
            try:
                updates["start_date"] = _parse_user_date(payload.start_date)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid medication start date. Use MM/DD/YYYY or YYYY-MM-DD.")
        if payload.titration_step is not None:
            updates["titration_step"] = payload.titration_step
        if payload.status is not None:
            status = payload.status.strip().lower()
            if status not in _MEDICATION_STATUSES:
                raise HTTPException(status_code=400, detail="Invalid medication status.")
            updates["status"] = status
        if payload.notes is not None:
            updates["notes"] = payload.notes

        updated = database.update_medication(medication_id, user.id, **updates)
        if not updated:
            raise HTTPException(status_code=404, detail="Medication not found.")

        previous_step = int(existing.get("titration_step") or 0)
        updated_step = int(updated.get("titration_step") or 0)
        previous_dose = float(existing.get("current_dose_mg") or 0)
        updated_dose = float(updated.get("current_dose_mg") or 0)
        if updated.get("status") == "active" and (updated_step > previous_step or updated_dose > previous_dose):
            outreach_scheduler.schedule_titration_followup(
                user.id,
                medication_id,
                updated_step or previous_step or 1,
                updated_dose or previous_dose,
            )

        return updated


@app.post("/api/medications/{medication_id}/events")
async def create_medication_event(medication_id: int, payload: MedicationEventCreateRequest, token: str, request: Request):
    user = _require_authenticated_user(token)
    with _AuditScope(
        request,
        action="log_medication_event",
        resource_type="medication",
        actor_id=user.id,
        user_id=user.id,
        resource_id=medication_id,
    ):
        medication = database.get_medication_by_id(medication_id, user.id)
        if not medication:
            raise HTTPException(status_code=404, detail="Medication not found.")

        event_type = payload.event_type.strip().lower()
        if event_type not in _MEDICATION_EVENT_TYPES:
            raise HTTPException(status_code=400, detail="Invalid medication event type.")

        scheduled_at = None
        if payload.scheduled_at:
            try:
                scheduled_at = _parse_datetime_value(payload.scheduled_at)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid scheduled date/time.")

        event = database.log_medication_event(
            medication_id=medication_id,
            user_id=user.id,
            event_type=event_type,
            dose_mg=payload.dose_mg if payload.dose_mg is not None else medication.get("current_dose_mg"),
            scheduled_at=scheduled_at,
            notes=payload.notes,
        )
        if event_type == "dose_missed":
            outreach_scheduler.schedule_missed_dose_followup(user.id, medication_id)
        elif event_type == "dose_adjusted":
            outreach_scheduler.schedule_titration_followup(
                user.id,
                medication_id,
                int(medication.get("titration_step") or 1),
                float(payload.dose_mg if payload.dose_mg is not None else medication.get("current_dose_mg") or 0),
            )
        if event_type in {"dose_missed", "dose_skipped"}:
            at_risk, reason = outreach_scheduler.detect_churn_risk(user.id)
            if at_risk:
                outreach_scheduler.schedule_retention_risk_outreach(user.id, medication_id, reason)
        return event


@app.get("/api/medications/{medication_id}/events")
async def medication_events(medication_id: int, token: str, request: Request):
    user = _require_authenticated_user(token)
    with _AuditScope(
        request,
        action="view_medication_events",
        resource_type="medication",
        actor_id=user.id,
        user_id=user.id,
        resource_id=medication_id,
    ):
        medication = database.get_medication_by_id(medication_id, user.id)
        if not medication:
            raise HTTPException(status_code=404, detail="Medication not found.")
        return database.get_medication_events(user.id, medication_id=medication_id)


@app.post("/api/side-effects")
async def create_side_effect(payload: SideEffectCreateRequest, token: str, request: Request):
    user = _require_authenticated_user(token)
    with _AuditScope(
        request,
        action="log_side_effect",
        resource_type="side_effect",
        actor_id=user.id,
        user_id=user.id,
        resource_id=payload.medication_id,
    ):
        severity = payload.severity.strip().lower()
        if severity not in _SIDE_EFFECT_SEVERITIES:
            raise HTTPException(status_code=400, detail="Invalid side effect severity.")

        medication = None
        if payload.medication_id is not None:
            medication = database.get_medication_by_id(payload.medication_id, user.id)
            if not medication:
                raise HTTPException(status_code=404, detail="Medication not found.")

        side_effect = database.log_side_effect(
            user_id=user.id,
            medication_id=payload.medication_id,
            symptom=payload.symptom,
            severity=severity,
            titration_step=payload.titration_step if payload.titration_step is not None else (
                medication.get("titration_step") if medication else None
            ),
            notes=payload.notes,
        )
        return side_effect


@app.get("/api/side-effects/timeline")
async def side_effect_timeline(token: str, request: Request):
    user = _require_authenticated_user(token)
    with _AuditScope(
        request,
        action="view_side_effects",
        resource_type="side_effect",
        actor_id=user.id,
        user_id=user.id,
    ):
        return database.get_side_effect_timeline(user.id)


@app.get("/api/refills")
async def list_refills(token: str, request: Request):
    user = _require_authenticated_user(token)
    with _AuditScope(
        request,
        action="view_refills",
        resource_type="refill",
        actor_id=user.id,
        user_id=user.id,
    ):
        return database.get_active_refills(user.id)


@app.get("/api/refills/pending")
async def pending_refills(token: str, request: Request, limit: int = 50):
    user = _require_authenticated_user(token)
    actor_type = _require_clinician_access(user)
    bounded_limit = max(1, min(int(limit), 500))
    with _AuditScope(
        request,
        action="view_pending_refills",
        resource_type="refill",
        actor_id=user.id,
        user_id=None,
        actor_type=actor_type,
    ):
        return database.get_pending_refills(limit=bounded_limit)


@app.post("/api/refills")
async def create_refill(payload: RefillCreateRequest, token: str, request: Request):
    user = _require_authenticated_user(token)
    with _AuditScope(
        request,
        action="create_refill",
        resource_type="refill",
        actor_id=user.id,
        user_id=user.id,
    ) as audit_scope:
        medication = database.get_medication_by_id(payload.medication_id, user.id)
        if not medication:
            raise HTTPException(status_code=404, detail="Medication not found.")

        active_refills = [
            refill for refill in database.get_active_refills(user.id)
            if int(refill.get("medication_id") or 0) == payload.medication_id
        ]
        if active_refills:
            existing = active_refills[0]
            audit_scope.set_resource_id(int(existing["id"]))
            existing_status = str(existing.get("status") or "due").strip().lower()
            if existing_status not in {"due", "reminded"}:
                return existing

            try:
                existing = database.update_refill_status(
                    int(existing["id"]),
                    user.id,
                    "requested",
                    notes=payload.notes,
                    pharmacy_name=payload.pharmacy_name,
                )
                existing = database.update_refill_status(
                    int(existing["id"]),
                    user.id,
                    "flagged_for_team",
                    notes=payload.notes,
                    pharmacy_name=payload.pharmacy_name,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))

            database.log_medication_event(
                medication_id=payload.medication_id,
                user_id=user.id,
                event_type="refill_due",
                dose_mg=medication.get("current_dose_mg"),
                scheduled_at=None,
                notes=payload.notes or "Manual refill request created via API.",
            )
            return existing

        due_date = None
        if payload.due_date:
            try:
                due_date = _parse_user_date(payload.due_date)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid refill due date. Use MM/DD/YYYY or YYYY-MM-DD.")
        if due_date is None:
            due_date = database.calculate_next_refill_date(payload.medication_id, user.id)

        refill = database.create_refill_request(
            user_id=user.id,
            medication_id=payload.medication_id,
            due_date=due_date,
        )
        audit_scope.set_resource_id(int(refill["id"]))
        try:
            refill = database.update_refill_status(
                refill["id"],
                user.id,
                "requested",
                notes=payload.notes,
                pharmacy_name=payload.pharmacy_name,
            )
            refill = database.update_refill_status(
                refill["id"],
                user.id,
                "flagged_for_team",
                notes=payload.notes,
                pharmacy_name=payload.pharmacy_name,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        database.log_medication_event(
            medication_id=payload.medication_id,
            user_id=user.id,
            event_type="refill_due",
            dose_mg=medication.get("current_dose_mg"),
            scheduled_at=None,
            notes=payload.notes or "Manual refill request created via API.",
        )
        return refill


@app.patch("/api/refills/{refill_id}")
async def update_refill(refill_id: int, payload: RefillUpdateRequest, token: str, request: Request):
    user = _require_authenticated_user(token)
    with _AuditScope(
        request,
        action="update_refill",
        resource_type="refill",
        actor_id=user.id,
        user_id=user.id,
        resource_id=refill_id,
    ) as audit_scope:
        normalized_status = payload.status.strip().lower()
        if normalized_status not in _REFILL_STATUSES:
            raise HTTPException(status_code=400, detail="Invalid refill status.")

        refill = database.get_refill_by_id(refill_id, user.id)
        refill_owner_id = user.id if refill else None
        if refill is None:
            pending_match = next(
                (item for item in database.get_pending_refills(limit=500) if int(item.get("id") or 0) == refill_id),
                None,
            )
            if pending_match is not None:
                refill_owner_id = int(pending_match["user_id"])
                audit_scope.set_user_id(refill_owner_id)
                audit_scope.set_actor_type(_require_clinician_access(user))

        if refill_owner_id is None:
            raise HTTPException(status_code=404, detail="Refill request not found.")

        try:
            updated = database.update_refill_status(
                refill_id,
                refill_owner_id,
                normalized_status,
                notes=payload.notes,
                pharmacy_name=payload.pharmacy_name,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        if not updated:
            raise HTTPException(status_code=404, detail="Refill request not found.")
        audit_scope.set_user_id(int(updated.get("user_id") or refill_owner_id))
        return updated


@app.get("/api/outreach/pending")
async def pending_outreach(token: str, request: Request, limit: int = 50):
    user = _require_authenticated_user(token)
    actor_type = _require_clinician_access(user)
    bounded_limit = max(1, min(int(limit), 200))
    with _AuditScope(
        request,
        action="view_pending_outreach",
        resource_type="outreach",
        actor_id=user.id,
        user_id=None,
        actor_type=actor_type,
    ):
        return database.get_pending_outreach(limit=bounded_limit)


@app.get("/api/outreach/user")
async def user_outreach(token: str, request: Request, status: str | None = None, limit: int = 20):
    user = _require_authenticated_user(token)
    bounded_limit = max(1, min(int(limit), 200))
    with _AuditScope(
        request,
        action="view_outreach",
        resource_type="outreach",
        actor_id=user.id,
        user_id=user.id,
    ):
        return database.get_outreach_for_user(user.id, status=status, limit=bounded_limit)


@app.get("/api/outreach/stats")
async def outreach_stats(token: str, request: Request):
    user = _require_authenticated_user(token)
    actor_type = _require_clinician_access(user)
    with _AuditScope(
        request,
        action="view_outreach_stats",
        resource_type="outreach",
        actor_id=user.id,
        user_id=None,
        actor_type=actor_type,
    ):
        return database.get_outreach_stats()


@app.post("/api/outreach/{outreach_id}/complete")
async def complete_outreach(outreach_id: int, payload: OutreachCompleteRequest, token: str, request: Request):
    user = _require_authenticated_user(token)
    actor_type = _require_clinician_access(user)
    with _AuditScope(
        request,
        action="complete_outreach",
        resource_type="outreach",
        actor_id=user.id,
        user_id=None,
        resource_id=outreach_id,
        actor_type=actor_type,
    ) as audit_scope:
        updated = database.update_outreach_status(
            outreach_id,
            "completed",
            outcome_summary=payload.outcome_summary,
            session_id=payload.session_id,
        )
        if not updated:
            raise HTTPException(status_code=404, detail="Outreach item not found.")
        audit_scope.set_user_id(updated.get("user_id"))
        return updated


@app.post("/api/outreach/{outreach_id}/cancel")
async def cancel_outreach(outreach_id: int, token: str, request: Request):
    user = _require_authenticated_user(token)
    actor_type = _require_clinician_access(user)
    with _AuditScope(
        request,
        action="cancel_outreach",
        resource_type="outreach",
        actor_id=user.id,
        user_id=None,
        resource_id=outreach_id,
        actor_type=actor_type,
    ) as audit_scope:
        updated = database.cancel_outreach(outreach_id)
        if not updated:
            raise HTTPException(status_code=404, detail="Outreach item not found.")
        audit_scope.set_user_id(updated.get("user_id"))
        return updated


@app.get("/api/escalations")
async def list_open_escalations(token: str, request: Request, limit: int = 50):
    user = _require_authenticated_user(token)
    actor_type = _require_clinician_access(user)
    bounded_limit = max(1, min(int(limit), 200))
    with _AuditScope(
        request,
        action="view_escalations",
        resource_type="escalation",
        actor_id=user.id,
        user_id=None,
        actor_type=actor_type,
    ):
        return database.get_open_escalations(limit=bounded_limit)


@app.get("/api/escalations/user")
async def user_escalations(token: str, request: Request, status: str | None = None):
    user = _require_authenticated_user(token)
    with _AuditScope(
        request,
        action="view_user_escalations",
        resource_type="escalation",
        actor_id=user.id,
        user_id=user.id,
    ):
        normalized_status = None
        if status is not None:
            normalized_status = status.strip().lower()
            if normalized_status not in _ESCALATION_STATUSES:
                raise HTTPException(status_code=400, detail="Invalid escalation status.")
        return database.get_escalations_for_user(user.id, status=normalized_status)


@app.get("/api/escalations/{escalation_id}")
async def get_escalation(escalation_id: int, token: str, request: Request):
    user = _require_authenticated_user(token)
    with _AuditScope(
        request,
        action="view_escalation",
        resource_type="escalation",
        actor_id=user.id,
        user_id=None,
        resource_id=escalation_id,
    ) as audit_scope:
        escalation = database.get_escalation_by_id(escalation_id)
        if not escalation:
            raise HTTPException(status_code=404, detail="Escalation not found.")
        if int(escalation.get("user_id") or 0) != user.id:
            audit_scope.set_actor_type(_require_clinician_access(user))
        audit_scope.set_user_id(escalation.get("user_id"))
        return escalation


@app.patch("/api/escalations/{escalation_id}")
async def update_escalation(escalation_id: int, payload: EscalationUpdateRequest, token: str, request: Request):
    user = _require_authenticated_user(token)
    actor_type = _require_clinician_access(user)
    with _AuditScope(
        request,
        action="update_escalation",
        resource_type="escalation",
        actor_id=user.id,
        user_id=None,
        resource_id=escalation_id,
        actor_type=actor_type,
    ) as audit_scope:
        normalized_status = payload.status.strip().lower()
        if normalized_status not in _ESCALATION_STATUSES:
            raise HTTPException(status_code=400, detail="Invalid escalation status.")
        updated = database.update_escalation_status(
            escalation_id,
            normalized_status,
            resolution_notes=payload.resolution_notes,
        )
        if not updated:
            raise HTTPException(status_code=404, detail="Escalation not found.")
        audit_scope.set_user_id(updated.get("user_id"))
        return updated


@app.get("/api/analytics/retention")
async def analytics_retention(token: str, request: Request, days: int = 30):
    user = _require_authenticated_user(token)
    bounded_days = max(0, min(int(days), 365))
    with _AuditScope(
        request,
        action="view_retention_analytics",
        resource_type="analytics",
        actor_id=user.id,
        user_id=None,
        details=f"days={bounded_days}",
    ):
        return analytics.compute_retention_curve(days=bounded_days)


@app.get("/api/analytics/engagement")
async def analytics_engagement(token: str, request: Request):
    user = _require_authenticated_user(token)
    with _AuditScope(
        request,
        action="view_engagement_analytics",
        resource_type="analytics",
        actor_id=user.id,
        user_id=None,
    ):
        return analytics.compute_engagement_metrics()


@app.get("/api/analytics/revenue-impact")
async def analytics_revenue_impact(
    token: str,
    request: Request,
    patients: int,
    revenue: float,
    lift: float = 10.0,
):
    user = _require_authenticated_user(token)
    with _AuditScope(
        request,
        action="view_revenue_impact",
        resource_type="analytics",
        actor_id=user.id,
        user_id=None,
        details=f"patients={int(patients)}; lift={float(lift):.1f}",
    ):
        if int(patients) < 0:
            raise HTTPException(status_code=400, detail="Patients must be zero or greater.")
        if float(revenue) < 0:
            raise HTTPException(status_code=400, detail="Revenue must be zero or greater.")
        return analytics.compute_revenue_impact(
            total_patients=int(patients),
            avg_revenue_per_patient=float(revenue),
            retention_lift_pct=float(lift),
        )


@app.get("/api/analytics/support-deflection")
async def analytics_support_deflection(token: str, request: Request):
    user = _require_authenticated_user(token)
    with _AuditScope(
        request,
        action="view_support_deflection",
        resource_type="analytics",
        actor_id=user.id,
        user_id=None,
    ):
        return analytics.compute_support_deflection()


@app.get("/api/activity/recent")
async def recent_activity(token: str, request: Request, limit: int = 20):
    user = _require_authenticated_user(token)
    actor_type = _require_clinician_access(user)
    bounded_limit = max(1, min(int(limit), 200))
    with _AuditScope(
        request,
        action="view_recent_activity",
        resource_type="activity",
        actor_id=user.id,
        user_id=None,
        actor_type=actor_type,
        details=f"limit={bounded_limit}",
    ):
        return database.get_recent_activity(limit=bounded_limit)


@app.get("/api/audit/log")
async def audit_log_query(
    token: str,
    request: Request,
    limit: int = 100,
    action: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
):
    user = _require_authenticated_user(token)
    actor_type = _require_audit_log_access(user)
    parsed_start = _parse_audit_datetime_query(start_date, "start_date")
    parsed_end = _parse_audit_datetime_query(end_date, "end_date", end_of_day=True)
    with _AuditScope(
        request,
        action="view_audit_log",
        resource_type="audit_log",
        actor_id=user.id,
        user_id=None,
        actor_type=actor_type,
        details=f"limit={max(1, min(int(limit), 1000))}",
    ):
        return database.get_audit_log(
            limit=max(1, min(int(limit), 1000)),
            action=action.strip() if isinstance(action, str) and action.strip() else None,
            start_date=parsed_start,
            end_date=parsed_end,
        )


@app.get("/api/audit/user/{user_id}")
async def user_audit_log(user_id: int, token: str, request: Request):
    user = _require_authenticated_user(token)
    actor_type = _require_audit_log_access(user, target_user_id=user_id)
    target_user = database.get_user_by_id(user_id)
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found.")
    with _AuditScope(
        request,
        action="view_user_audit_log",
        resource_type="audit_log",
        actor_id=user.id,
        user_id=user_id,
        resource_id=user_id,
        actor_type=actor_type,
    ):
        return database.get_audit_events_for_user(user_id)


@app.get("/api/conversations/history")
async def conversation_history(token: str, request: Request, limit_sessions: int = 25):
    decoded = decode_access_token(token)
    if not decoded:
        raise HTTPException(status_code=401, detail="Invalid or expired token.")
    bounded_limit = max(1, min(int(limit_sessions), 100))
    with _AuditScope(
        request,
        action="view_conversations",
        resource_type="conversation",
        actor_id=decoded["sub"],
        user_id=decoded["sub"],
    ):
        return database.get_conversation_history(decoded["sub"], limit_sessions=bounded_limit)


@app.delete("/api/auth/account")
async def delete_account(token: str, request: Request):
    decoded = decode_access_token(token)
    if not decoded:
        raise HTTPException(status_code=401, detail="Invalid or expired token.")
    user = database.get_user_by_id(decoded["sub"])
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    try:
        database.delete_user(user.id)
        return {"ok": True}
    finally:
        audit.audit_auth_event("delete_account", user.id, get_client_ip(request))


@app.get("/")
async def root():
    return {
        "app": "CuraConnect Backend",
        "status": "ok",
        "message": "React frontend is now standalone. Run it from the frontend folder.",
    }


@app.get("/health")
async def health():
    return {"ok": True, "service": "curaconnect-backend"}


@app.get("/api/stt/health")
async def stt_health():
    return {
        "ok": True,
        "service": "curaconnect-stt-enhancer",
        "mode": "text-enhancement",
        "transcribe_enabled": local_transcriber.is_available,
        "transcriber_backend": local_transcriber.backend,
        "transcriber_model": local_transcriber.model_name,
        "transcriber_error": local_transcriber.load_error,
    }


@app.post("/api/stt/enhance", response_model=SttEnhanceResponse)
async def stt_enhance(payload: SttEnhanceRequest):
    transcript = payload.transcript.strip()
    if not transcript:
        raise HTTPException(status_code=400, detail="Transcript cannot be empty.")

    result = transcript_enhancer.enhance(
        transcript=transcript,
        expected_keywords=payload.expected_keywords,
        custom_terms=payload.custom_terms,
    )
    return result


@app.post("/api/stt/transcribe", response_model=SttTranscribeResponse)
async def stt_transcribe(
    audio: UploadFile = File(...),
    expected_keywords: str | None = Form(default=None),
    custom_terms: str | None = Form(default=None),
    language: str = Form(default="en"),
):
    audio_bytes = await audio.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Audio payload is empty.")

    suffix = os.path.splitext(audio.filename or "voice.webm")[1] or ".webm"
    transcribed = local_transcriber.transcribe_bytes(
        audio_bytes,
        suffix=suffix,
        language=(language or "en").strip() or "en",
    )

    if transcribed.get("status") != "ok":
        raise HTTPException(
            status_code=503,
            detail=transcribed.get("error") or "Local speech transcriber is unavailable.",
        )

    raw_transcript = str(transcribed.get("text") or "").strip()
    if not raw_transcript:
        raise HTTPException(
            status_code=422,
            detail="Speech was captured but no clear transcript could be produced.",
        )

    enhanced = transcript_enhancer.enhance(
        transcript=raw_transcript,
        expected_keywords=_parse_csv_list(expected_keywords),
        custom_terms=_parse_csv_list(custom_terms),
    )

    return {
        "transcript": enhanced["transcript"],
        "raw_transcript": raw_transcript,
        "confidence": float(transcribed.get("confidence") or 0.0),
        "source": str(transcribed.get("backend") or "unknown"),
        "numeric_corrections": enhanced["numeric_corrections"],
        "phonetic_corrections": enhanced["phonetic_corrections"],
        "confusable_flags": enhanced["confusable_flags"],
        "latency_ms": int(transcribed.get("latency_ms") or 0) + int(enhanced.get("latency_ms") or 0),
    }


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str | None = None):
    await websocket.accept()
    session = ConversationSession()

    if token:
        payload = decode_access_token(token)
        if payload:
            user = database.get_user_by_id(payload["sub"])
            if user:
                age = database.age_from_dob(user.dob) if user.dob else user.age
                session.identify_user(user.name, age, from_auth=True, user_id=user.id)

    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)

            msg_type = data.get("type")

            if msg_type == "user_message":
                user_text = data.get("text", "").strip()
                if not user_text:
                    continue

                location = data.get("location")  # {"latitude": ..., "longitude": ...}
                timezone_name = data.get("timezone")
                utc_offset_minutes = data.get("utc_offset_minutes")
                parsed_offset = None
                if isinstance(utc_offset_minutes, int):
                    parsed_offset = utc_offset_minutes
                elif isinstance(utc_offset_minutes, str):
                    try:
                        parsed_offset = int(utc_offset_minutes)
                    except ValueError:
                        parsed_offset = None

                if timezone_name or parsed_offset is not None:
                    session.set_timezone_context(
                        timezone_name=timezone_name if isinstance(timezone_name, str) else None,
                        utc_offset_minutes=parsed_offset,
                    )

                # Check if this message contains the user's name for the first time
                if session.user_id is None and session.user_name is None:
                    # Pass to agent — it will extract the name naturally
                    pass

                result = await session.process_message(user_text, location)

                # Send any side-effect events first (vital logged, doctors found)
                should_close = False
                for event in result["events"]:
                    await websocket.send_json(event)
                    if event.get("type") == "session_timeout":
                        should_close = True

                # Send the agent's spoken response
                await websocket.send_json({
                    "type": "agent_message",
                    "text": result["text"],
                    "audio": result["audio"],
                    "session_complete": result.get("session_complete", False),
                })
                if should_close:
                    await websocket.close()
                    break

            elif msg_type == "identify_user":
                # Frontend can call this once name is confirmed
                name = data.get("name", "")
                age = data.get("age")
                if name:
                    session.identify_user(name, age)

            elif msg_type == "location":
                lat = data.get("latitude")
                lng = data.get("longitude")
                if lat is not None and lng is not None:
                    session.set_location(lat, lng)

            elif msg_type == "address":
                address = data.get("address", "").strip()
                if address:
                    await session.set_address(address)

            elif msg_type == "end_session":
                session.end_session()
                break

    except WebSocketDisconnect:
        session.end_session()
    except Exception as e:
        logger.exception("WebSocket error")
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
        session.end_session()
