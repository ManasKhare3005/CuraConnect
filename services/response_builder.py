"""
Builds structured context for LLM response generation from agent state.
This module translates the agent's internal state into a clean context dict
that the LLM service can use to generate natural responses.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any

from database import db as database
from services.audit import sanitize_for_log


def _first_name(name: str | None) -> str | None:
    if not isinstance(name, str):
        return None
    stripped = name.strip()
    if not stripped:
        return None
    return stripped.split()[0]


def _weeks_since(start_date: date | str | None) -> int | None:
    if start_date is None:
        return None
    if isinstance(start_date, str):
        try:
            start_date = date.fromisoformat(start_date)
        except ValueError:
            return None
    if not isinstance(start_date, date):
        return None
    return max((datetime.now(timezone.utc).date() - start_date).days // 7, 0)


def _sanitize_history_text(text: str, session, first_name: str | None) -> str:
    cleaned = sanitize_for_log(str(text or ""))

    full_name = None
    if isinstance(session.profile.get("name"), str) and session.profile.get("name").strip():
        full_name = session.profile.get("name").strip()
    elif isinstance(getattr(session, "user_name", None), str) and getattr(session, "user_name").strip():
        full_name = getattr(session, "user_name").strip()

    if full_name:
        replacement = first_name or "[NAME]"
        cleaned = re.sub(re.escape(full_name), replacement, cleaned, flags=re.IGNORECASE)

    address = session.profile.get("address")
    if isinstance(address, str) and address.strip():
        cleaned = re.sub(re.escape(address.strip()), "[ADDRESS]", cleaned, flags=re.IGNORECASE)

    cleaned = re.sub(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", "[DATE]", cleaned)
    cleaned = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", "[DATE]", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > 280:
        cleaned = cleaned[:277].rstrip() + "..."
    return cleaned


def _build_patient_context(session, first_name: str | None) -> str:
    parts: list[str] = []
    if first_name:
        parts.append(first_name)

    age = session.profile.get("age")
    if isinstance(age, int) and age > 0:
        parts.append(f"age {age}")

    weight_kg = session.profile.get("weight_kg")
    if isinstance(weight_kg, (int, float)) and weight_kg > 0:
        parts.append(f"{float(weight_kg):g} kg")

    height_cm = session.profile.get("height_cm")
    if isinstance(height_cm, (int, float)) and height_cm > 0:
        parts.append(f"{float(height_cm):g} cm")

    medication = getattr(session, "active_medication", None) or {}
    if medication:
        med_bits: list[str] = []
        brand_name = str(medication.get("brand_name") or "").strip()
        drug_name = str(medication.get("drug_name") or "medication").strip()
        current_dose = medication.get("current_dose_mg")
        titration_step = medication.get("titration_step")
        med_bits.append(f"on {brand_name or drug_name}")
        if current_dose is not None:
            med_bits.append(f"{current_dose}mg")
        if titration_step:
            med_bits.append(f"step {titration_step}")
        weeks_on_treatment = _weeks_since(medication.get("start_date"))
        if weeks_on_treatment is not None:
            if weeks_on_treatment == 0:
                med_bits.append("started this week")
            elif weeks_on_treatment == 1:
                med_bits.append("started 1 week ago")
            else:
                med_bits.append(f"started {weeks_on_treatment} weeks ago")
        if med_bits:
            parts.append(", ".join(med_bits))

    return ", ".join(part for part in parts if part) or "Patient context unavailable"


def _build_recent_vitals_summary(session) -> str | None:
    user_id = getattr(session, "user_id", None)
    if not user_id:
        return None
    try:
        recent = database.get_recent_vitals(user_id, limit=6)
    except Exception:
        return None
    if not recent:
        return None

    # Deduplicate by vital type — keep most recent of each
    seen: dict[str, dict] = {}
    for v in recent:
        vtype = str(v.get("vital_type") or "")
        if vtype and vtype not in seen:
            seen[vtype] = v

    def _age_label(iso_ts: str | None) -> str:
        if not iso_ts:
            return ""
        try:
            ts = datetime.fromisoformat(iso_ts)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            days = (datetime.now(timezone.utc) - ts).days
            if days == 0:
                return "today"
            if days == 1:
                return "yesterday"
            return f"{days}d ago"
        except Exception:
            return ""

    labels = {
        "blood_pressure": "BP",
        "heart_rate": "HR",
        "temperature": "temp",
        "oxygen_saturation": "O2",
        "weight": "weight",
    }
    parts: list[str] = []
    for vtype, label in labels.items():
        v = seen.get(vtype)
        if not v:
            continue
        value = str(v.get("value") or "").strip()
        unit = str(v.get("unit") or "").strip()
        age = _age_label(v.get("recorded_at"))
        entry = f"{label} {value}"
        if unit and unit not in value:
            entry += f" {unit}"
        if age:
            entry += f" ({age})"
        parts.append(entry)

    return ", ".join(parts) if parts else None


def _derive_actions(session, updates: dict, doctor_result: dict | None) -> list[str]:
    derived_actions: list[str] = []
    changed = updates.get("profile_changed") or {}

    if "name" in changed and "age" in changed:
        derived_actions.append(f"Updated their profile with the right name and age {session.profile.get('age')}.")
    elif "name" in changed:
        derived_actions.append("Updated their profile with the right name.")
    elif "age" in changed:
        derived_actions.append(f"Logged their age as {session.profile.get('age')}.")

    time_label = updates.get("time_label")
    time_suffix = f" from {time_label}" if time_label and time_label != "just now" else ""

    blood_pressure = updates.get("blood_pressure")
    if blood_pressure:
        derived_actions.append(f"Logged blood pressure {blood_pressure} mmHg{time_suffix}.")

    heart_rate = updates.get("heart_rate")
    if heart_rate is not None:
        derived_actions.append(f"Logged heart rate {heart_rate} bpm{time_suffix}.")

    symptoms_added = updates.get("symptoms_added") or []
    if symptoms_added:
        readable = ", ".join(str(symptom) for symptom in symptoms_added)
        if updates.get("side_effects_logged") and getattr(session, "active_medication", None):
            medication_name = session._medication_display_name()
            derived_actions.append(f"Logged {readable} as side effects linked to {medication_name}.")
        else:
            derived_actions.append(f"Logged symptoms: {readable}.")

    if updates.get("doctor_preference") is False:
        derived_actions.append("Confirmed they do not want doctor recommendations right now.")

    if isinstance(doctor_result, dict):
        status = str(doctor_result.get("status") or "")
        if status == "found":
            derived_actions.append(
                f"Found {int(doctor_result.get('count') or 0)} nearby doctor options and shared them on screen."
            )
        elif status == "no_results":
            derived_actions.append("Ran a doctor search but did not find good matches yet.")

    return derived_actions


def _summarize_doctor_result(doctor_result: dict | None) -> dict | None:
    if not isinstance(doctor_result, dict):
        return None
    status = str(doctor_result.get("status") or "").strip()
    if not status:
        return None
    specialties = doctor_result.get("specialties") or []
    count = int(doctor_result.get("count") or 0)
    summary = None
    if status == "found":
        if specialties:
            readable = ", ".join(str(item) for item in specialties)
            summary = f"Found {count} nearby doctors for {readable}."
        else:
            summary = f"Found {count} nearby doctor options."
    elif status == "no_results":
        summary = "Doctor search returned no strong matches yet."
    elif status == "error":
        summary = str(doctor_result.get("message") or "Doctor search needs a usable location.")
    return {
        "status": status,
        "count": count,
        "specialties": specialties,
        "summary": summary,
    }


def _map_tone(session, severity: str | None) -> str:
    raw_tone = str(getattr(session, "conversation_tone", "neutral") or "neutral").lower()
    if severity == "serious":
        return "distressed"
    if raw_tone in {"concerned", "anxious"}:
        return "anxious"
    if raw_tone == "casual":
        return "casual"
    return "neutral"


def _infer_session_stage(session, updates: dict, severity: str | None) -> str:
    if getattr(session, "session_complete", False):
        return "wrapup"
    if not session.profile.get("name"):
        return "greeting"
    if session.profile.get("age") is None:
        return "intake"
    if updates.get("blood_pressure") or updates.get("heart_rate") is not None:
        return "intake"
    if severity or updates.get("symptoms_added") or session.symptoms:
        return "assessment"
    return "intake"


def _map_next_question(next_question_field: str | None, clarification_fields: list[str] | None = None) -> str | None:
    if clarification_fields:
        fields = [field for field in clarification_fields if field]
        if fields == ["blood_pressure", "heart_rate"]:
            return "ask for their blood pressure and heart rate"
        if len(fields) == 1:
            return _map_next_question(fields[0])

    mapping = {
        "name": "ask for their name",
        "age": "ask for their age",
        "blood_pressure": "ask for their blood pressure",
        "heart_rate": "ask for their heart rate",
        "temperature": "ask for their temperature",
        "oxygen_saturation": "ask for their oxygen level",
        "symptoms": "ask if they have any symptoms",
        "doctor_preference": "ask if they would like doctor recommendations nearby",
        "medication_dose": "ask if they have taken their injection this week",
        "medication_side_effects": "ask whether they have noticed side effects since the injection",
        "refill_help": "ask if their prescription refill is on track",
        "refill_check": "ask if they want the refill flagged for their care team",
        "more_help_confirmation": "ask if they need anything else today",
    }
    return mapping.get(next_question_field)


def _build_special_instructions(
    session,
    updates: dict,
    entities: dict,
    severity: str | None,
    escalation_created: bool,
    refill_info: str | None,
    doctor_result: dict | None,
) -> list[str]:
    instructions: list[str] = []
    if severity == "serious":
        instructions.append("patient reported a serious symptom - express genuine concern and urgency")
    elif severity == "moderate":
        instructions.append("patient needs a more careful but calm tone")

    if escalation_created:
        instructions.append("tell the patient their care team has been flagged and can follow up directly")

    if getattr(session, "active_medication", None) and entities.get("glp1_concern_level") == "expected":
        instructions.append("normalize common GLP-1 side effects without minimizing how they feel")
    elif entities.get("glp1_concern_level") == "red_flag":
        instructions.append("treat this as a red-flag medication concern")

    if entities.get("severe_flag"):
        instructions.append("the patient described a high-risk symptom, so lead with empathy before action")

    symptom_pause = updates.get("_symptom_pause")
    if symptom_pause:
        instructions.append(
            f"the patient just mentioned {symptom_pause} — acknowledge it naturally and ask one brief follow-up "
            f"about it (how long, how bad, new or recurring?) before moving to anything else"
        )

    if refill_info:
        instructions.append("confirm the refill status briefly and confidently")

    if isinstance(doctor_result, dict) and str(doctor_result.get("status") or "") == "found":
        instructions.append("mention that nearby doctor options were shared on screen")

    return instructions


def build_response_context(
    session,
    updates: dict,
    entities: dict,
    severity: str | None,
    escalation_created: bool,
    remedy_text: str | None,
    next_question_field: str | None,
    doctor_result: dict | None,
    refill_parts: list[str] | None,
) -> dict:
    """Build the context dict for generate_response()."""
    patient_name = _first_name(session.profile.get("name") or getattr(session, "user_name", None))
    conversation_history = []
    for message in (session.messages or [])[-6:]:
        conversation_history.append(
            {
                "role": str(message.get("role") or "user"),
                "content": _sanitize_history_text(str(message.get("content") or ""), session, patient_name),
            }
        )

    refill_info = None
    if isinstance(updates.get("refill_info"), str) and updates.get("refill_info").strip():
        refill_info = updates.get("refill_info").strip()
    elif refill_parts:
        refill_info = " ".join(str(part).strip() for part in refill_parts if str(part or "").strip()).strip() or None

    clarification_fields = updates.get("clarification_fields") or []
    actions_taken = list(updates.get("actions_taken") or [])
    for derived_action in _derive_actions(session, updates, doctor_result):
        if derived_action not in actions_taken:
            actions_taken.append(derived_action)

    context = {
        "patient_name": patient_name,
        "patient_context": _build_patient_context(session, patient_name),
        "recent_vitals": _build_recent_vitals_summary(session),
        "conversation_history": conversation_history,
        "actions_taken": actions_taken,
        "clinical_decision": severity,
        "remedy_text": remedy_text,
        "escalation_created": bool(escalation_created),
        "next_question": _map_next_question(next_question_field, clarification_fields),
        "next_question_field": next_question_field,
        "refill_info": refill_info,
        "doctor_results": _summarize_doctor_result(doctor_result),
        "conversation_tone": _map_tone(session, severity),
        "session_stage": _infer_session_stage(session, updates, severity),
        "is_medication_patient": bool(getattr(session, "active_medication", None)),
        "special_instructions": _build_special_instructions(
            session=session,
            updates=updates,
            entities=entities,
            severity=severity,
            escalation_created=escalation_created,
            refill_info=refill_info,
            doctor_result=doctor_result,
        ),
        "_template_parts": list(updates.get("fallback_parts") or []),
    }
    return context
