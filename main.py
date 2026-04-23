import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import date, datetime

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv

import re as _re

from database.db import init_db
from database import db as database
from agent import ConversationSession
from auth import hash_password, verify_password, create_access_token, decode_access_token
from services import tts, geocoding, doctor_finder
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


@app.post("/api/auth/register", response_model=AuthResponse)
async def register(payload: RegisterRequest):
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
    token = create_access_token(user.id, user.name, user.email)
    return {"token": token, "user": _serialize_user(user)}


@app.post("/api/auth/login", response_model=AuthResponse)
async def login(payload: LoginRequest):
    email = payload.email.strip().lower()
    user = database.get_user_by_email(email)
    if not user or not user.password_hash:
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    token = create_access_token(user.id, user.name, user.email)
    return {"token": token, "user": _serialize_user(user)}


class ProfileUpdateRequest(BaseModel):
    name: str | None = None
    dob: str | None = None
    weight_kg: float | None = None
    height_cm: float | None = None
    address: str | None = None


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


@app.get("/api/auth/me")
async def get_me(token: str):
    payload = decode_access_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token.")
    user = database.get_user_by_id(payload["sub"])
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    return _serialize_user(user)


@app.patch("/api/auth/profile")
async def update_profile(payload: ProfileUpdateRequest, token: str):
    decoded = decode_access_token(token)
    if not decoded:
        raise HTTPException(status_code=401, detail="Invalid or expired token.")
    user = database.get_user_by_id(decoded["sub"])
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

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
async def vitals_history(token: str):
    decoded = decode_access_token(token)
    if not decoded:
        raise HTTPException(status_code=401, detail="Invalid or expired token.")
    return database.get_vitals_history(decoded["sub"])


@app.delete("/api/vitals/{vital_id}")
async def delete_vital(vital_id: int, token: str):
    decoded = decode_access_token(token)
    if not decoded:
        raise HTTPException(status_code=401, detail="Invalid or expired token.")
    deleted = database.delete_vital(decoded["sub"], vital_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Vital not found.")
    return {"ok": True}


@app.get("/api/conversations/history")
async def conversation_history(token: str, limit_sessions: int = 25):
    decoded = decode_access_token(token)
    if not decoded:
        raise HTTPException(status_code=401, detail="Invalid or expired token.")
    bounded_limit = max(1, min(int(limit_sessions), 100))
    return database.get_conversation_history(decoded["sub"], limit_sessions=bounded_limit)


@app.delete("/api/auth/account")
async def delete_account(token: str):
    decoded = decode_access_token(token)
    if not decoded:
        raise HTTPException(status_code=401, detail="Invalid or expired token.")
    user = database.get_user_by_id(decoded["sub"])
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    database.delete_user(user.id)
    return {"ok": True}


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
                for event in result["events"]:
                    await websocket.send_json(event)

                # Send the agent's spoken response
                await websocket.send_json({
                    "type": "agent_message",
                    "text": result["text"],
                    "audio": result["audio"],
                    "session_complete": result.get("session_complete", False),
                })

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
