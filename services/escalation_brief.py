"""
Generates pre-charted clinical escalation briefs by aggregating patient data.
No LLM calls - all data is pulled from the database and structured deterministically.
"""

from __future__ import annotations

from datetime import date, datetime, timezone, timedelta

from database import db as database


_SIDE_EFFECT_SEVERITY_RANK = {"mild": 0, "moderate": 1, "severe": 2}


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


def _format_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    normalized = value.astimezone(timezone.utc)
    return normalized.isoformat().replace("+00:00", "Z")


def _format_date(value: str | date | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    candidate = str(value).strip()
    if not candidate:
        return None
    if "T" in candidate:
        parsed = _parse_datetime(candidate)
        return parsed.date().isoformat() if parsed else None
    try:
        return date.fromisoformat(candidate).isoformat()
    except ValueError:
        return None


def _normalize_symptom_key(symptom: str) -> str:
    normalized = (symptom or "").strip().lower()
    for prefix in ("mild ", "moderate ", "severe "):
        if normalized.startswith(prefix):
            return normalized[len(prefix):].strip()
    return normalized


def _has_concerning_vitals(vitals: list[dict]) -> bool:
    for vital in vitals:
        vital_type = str(vital.get("type") or "").strip().lower()
        value = str(vital.get("value") or "").strip()
        if not vital_type or not value:
            continue

        if vital_type == "blood_pressure" and "/" in value:
            try:
                systolic, diastolic = value.split("/", 1)
                if int(systolic) >= 180 or int(diastolic) >= 120:
                    return True
            except ValueError:
                continue
        elif vital_type == "heart_rate":
            try:
                heart_rate = float(value)
                if heart_rate > 150 or heart_rate < 40:
                    return True
            except ValueError:
                continue
        elif vital_type == "temperature":
            try:
                temperature = float(value)
                if temperature > 103 or (39.4 < temperature < 50):
                    return True
            except ValueError:
                continue
        elif vital_type == "oxygen_saturation":
            try:
                oxygen = float(value.replace("%", ""))
                if oxygen < 90:
                    return True
            except ValueError:
                continue

    return False


def _is_worsening_same_step(side_effects: list[dict], current_step: int | None) -> bool:
    if current_step is None:
        return False

    grouped: dict[str, list[dict]] = {}
    for effect in side_effects:
        if effect.get("titration_step") != current_step:
            continue
        key = _normalize_symptom_key(str(effect.get("symptom") or ""))
        if not key:
            continue
        grouped.setdefault(key, []).append(effect)

    for entries in grouped.values():
        if len(entries) < 2:
            continue
        ordered = sorted(
            entries,
            key=lambda item: _parse_datetime(item.get("reported_at")) or datetime.min.replace(tzinfo=timezone.utc),
        )
        first_rank = _SIDE_EFFECT_SEVERITY_RANK.get(str(ordered[0].get("severity") or "").lower(), 0)
        last_rank = _SIDE_EFFECT_SEVERITY_RANK.get(str(ordered[-1].get("severity") or "").lower(), 0)
        if last_rank > first_rank:
            return True

    return False


def _days_on_current_dose(medication: dict | None, medication_events: list[dict]) -> int | None:
    if not medication:
        return None

    current_dose = medication.get("current_dose_mg")
    anchor: datetime | None = None
    latest_adjustment: datetime | None = None
    for event in medication_events:
        if event.get("event_type") != "dose_adjusted":
            continue
        recorded_at = _parse_datetime(event.get("recorded_at"))
        if recorded_at is None:
            continue
        if latest_adjustment is None or recorded_at > latest_adjustment:
            latest_adjustment = recorded_at
        if current_dose is None or event.get("dose_mg") is None:
            continue
        try:
            if abs(float(event.get("dose_mg")) - float(current_dose)) < 0.0001:
                if anchor is None or recorded_at > anchor:
                    anchor = recorded_at
        except (TypeError, ValueError):
            continue

    if anchor is None:
        anchor = latest_adjustment

    if anchor is None:
        start_date = _format_date(medication.get("start_date"))
        if start_date:
            anchor = datetime.fromisoformat(f"{start_date}T00:00:00+00:00")

    if anchor is None:
        return None

    return max((datetime.now(timezone.utc).date() - anchor.date()).days, 0)


def _build_recent_vitals(user_id: int) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    recent_vitals = database.get_recent_vitals(user_id, limit=10)
    results: list[dict] = []
    for vital in recent_vitals:
        recorded_at = _parse_datetime(vital.get("recorded_at"))
        if recorded_at is None or recorded_at < cutoff:
            continue
        vital_type = str(vital.get("vital_type") or "").strip().lower()
        if vital_type == "symptom":
            continue
        results.append(
            {
                "type": vital_type,
                "value": str(vital.get("value") or ""),
                "recorded_at": _format_timestamp(recorded_at),
            }
        )
    return results


def _build_side_effect_timeline(side_effects: list[dict]) -> list[dict]:
    ordered = sorted(
        side_effects,
        key=lambda item: _parse_datetime(item.get("reported_at")) or datetime.min.replace(tzinfo=timezone.utc),
    )
    results: list[dict] = []
    for effect in ordered:
        severity = str(effect.get("severity") or "").strip().lower()
        symptom = str(effect.get("symptom") or "").strip()
        display_symptom = symptom
        if severity in {"mild", "severe"} and symptom and not symptom.lower().startswith(f"{severity} "):
            display_symptom = f"{severity} {symptom}"
        results.append(
            {
                "symptom": display_symptom,
                "step": effect.get("titration_step"),
                "reported_at": _format_date(effect.get("reported_at")),
                "resolved_at": _format_date(effect.get("resolved_at")),
            }
        )
    return results


def _build_adherence_summary(medication_events: list[dict]) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    doses_taken = 0
    doses_missed = 0
    last_dose_date: str | None = None

    for event in medication_events:
        recorded_at = _parse_datetime(event.get("recorded_at"))
        if recorded_at is None:
            continue

        event_type = str(event.get("event_type") or "").strip().lower()
        if event_type == "dose_taken":
            if last_dose_date is None:
                last_dose_date = recorded_at.date().isoformat()
            if recorded_at >= cutoff:
                doses_taken += 1
        elif event_type in {"dose_missed", "dose_skipped"} and recorded_at >= cutoff:
            doses_missed += 1

    return {
        "doses_taken_last_30_days": doses_taken,
        "doses_missed_last_30_days": doses_missed,
        "last_dose_date": last_dose_date,
    }


def _build_recommended_actions(
    trigger_reason: str,
    severity: str,
    recent_vitals: list[dict],
    side_effects: list[dict],
    medication: dict | None,
    adherence: dict,
    conversation_excerpt: list[dict],
) -> list[str]:
    actions: list[str] = []
    severity_lower = (severity or "").strip().lower()
    current_step = medication.get("titration_step") if medication else None
    combined_text = " ".join(
        [
            trigger_reason,
            " ".join(str(item.get("symptom") or "") for item in side_effects),
            " ".join(str(item.get("content") or "") for item in conversation_excerpt),
        ]
    ).lower()

    if severity_lower == "urgent" or (severity_lower == "serious" and _has_concerning_vitals(recent_vitals)):
        actions.append("Immediate clinical review recommended")

    if _is_worsening_same_step(side_effects, current_step):
        actions.append("Consider dose reduction or temporary pause")

    if int(adherence.get("doses_missed_last_30_days") or 0) >= 2:
        actions.append("Assess patient engagement and barriers to adherence")

    if any(term in combined_text for term in ("nausea", "vomiting", "throwing up", "can't keep anything down", "cant keep anything down")):
        actions.append("Evaluate for dehydration")

    if severity_lower == "serious":
        actions.append("Schedule follow-up in 48 hours")

    return actions


def generate_brief(
    user_id: int,
    session_id: int | None,
    trigger_reason: str,
    severity: str,
    medication_id: int | None = None,
) -> dict:
    user = database.get_user_by_id(user_id)
    active_medications = database.get_active_medications(user_id)
    medication = None
    if medication_id is not None:
        medication = database.get_medication_by_id(medication_id, user_id)
    if medication is None and active_medications:
        medication = active_medications[0]

    medication_lookup_id = medication.get("id") if medication else medication_id
    medication_events = database.get_medication_events(user_id, medication_id=medication_lookup_id, limit=30)
    side_effects = database.get_side_effects(user_id, medication_id=medication_lookup_id)
    recent_vitals = _build_recent_vitals(user_id)
    conversation_excerpt = []
    if session_id is not None:
        conversation_excerpt = [
            {
                "role": message.get("role"),
                "content": message.get("content"),
                "timestamp": _format_timestamp(_parse_datetime(message.get("created_at"))),
            }
            for message in database.get_session_messages(session_id, limit=10)
        ]

    adherence = _build_adherence_summary(medication_events)
    recommended_actions = _build_recommended_actions(
        trigger_reason=trigger_reason,
        severity=severity,
        recent_vitals=recent_vitals,
        side_effects=side_effects,
        medication=medication,
        adherence=adherence,
        conversation_excerpt=conversation_excerpt,
    )

    age = None
    if user is not None:
        age = database.age_from_dob(user.dob) if user.dob else user.age

    brief = {
        "patient": {
            "name": user.name if user else None,
            "age": age,
            "weight_kg": user.weight_kg if user else None,
            "height_cm": user.height_cm if user else None,
        },
        "current_medication": {
            "drug_name": medication.get("drug_name") if medication else None,
            "brand_name": medication.get("brand_name") if medication else None,
            "current_dose_mg": medication.get("current_dose_mg") if medication else None,
            "titration_step": medication.get("titration_step") if medication else None,
            "days_on_current_dose": _days_on_current_dose(medication, medication_events),
            "treatment_start_date": _format_date(medication.get("start_date") if medication else None),
        },
        "trigger": {
            "reason": trigger_reason,
            "severity": severity,
            "timestamp": _format_timestamp(datetime.now(timezone.utc)),
        },
        "recent_vitals": recent_vitals,
        "side_effect_timeline": _build_side_effect_timeline(side_effects),
        "medication_adherence": adherence,
        "conversation_excerpt": conversation_excerpt,
        "recommended_actions": recommended_actions,
    }
    return brief
