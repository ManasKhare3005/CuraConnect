from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from auth import hash_password
from database import db as database
from services.escalation_brief import generate_brief
from services.outreach_scheduler import detect_churn_risk


DEMO_PASSWORD = "demo123"
REGISTERED_DEMOS = {
    "Sarah Chen": "sarah.demo@example.com",
    "Marcus Johnson": "marcus.demo@example.com",
    "Emily Rodriguez": "emily.demo@example.com",
    "David Kim": "david.demo@example.com",
    "Priya Patel": "priya.demo@example.com",
}
NON_AUTH_DEMOS = [
    "James Wilson",
    "Aisha Mohammed",
    "Robert Taylor",
]


def utc_days_ago(now: datetime, days_ago: int, hour: int, minute: int = 0) -> datetime:
    target_date = (now - timedelta(days=days_ago)).date()
    return datetime.combine(target_date, time(hour=hour, minute=minute, tzinfo=timezone.utc))


def offset_days(anchor: datetime, day_offset: int, hour: int, minute: int = 0) -> datetime:
    target_date = (anchor + timedelta(days=day_offset)).date()
    return datetime.combine(target_date, time(hour=hour, minute=minute, tzinfo=timezone.utc))


def create_registered_demo_user(
    stats: Counter,
    name: str,
    email: str,
    dob: date,
    weight_kg: float,
    height_cm: float,
) -> int:
    user = database.create_registered_user(
        name=name,
        email=email,
        password_hash=hash_password(DEMO_PASSWORD),
        dob=dob,
    )
    database.update_user_full(
        user_id=user.id,
        weight_kg=weight_kg,
        update_weight=True,
        height_cm=height_cm,
        update_height=True,
    )
    stats["patients"] += 1
    return int(user.id)


def create_basic_demo_user(
    stats: Counter,
    name: str,
    age: int,
    weight_kg: float,
    height_cm: float,
) -> int:
    user = database.get_or_create_user(name, age)
    database.update_user_full(
        user_id=user.id,
        weight_kg=weight_kg,
        update_weight=True,
        height_cm=height_cm,
        update_height=True,
    )
    stats["patients"] += 1
    return int(user.id)


def add_medication(
    stats: Counter,
    user_id: int,
    drug_name: str,
    brand_name: str,
    current_dose_mg: float,
    start_date: date,
    titration_step: int,
    status: str = "active",
    frequency: str = "weekly",
    route: str = "subcutaneous injection",
    notes: str | None = None,
) -> dict:
    medication = database.create_medication(
        user_id=user_id,
        drug_name=drug_name,
        brand_name=brand_name,
        current_dose_mg=current_dose_mg,
        frequency=frequency,
        start_date=start_date,
        route=route,
        titration_step=titration_step,
        status=status,
        notes=notes,
    )
    stats["medications"] += 1
    return medication


def add_session(
    stats: Counter,
    user_id: int,
    session_start: datetime,
    summary: str,
    turns: list[tuple[int, str, str]],
) -> int:
    session_id = database.create_session(user_id)
    stats["sessions"] += 1

    first_message_at = session_start
    last_message_at = session_start
    for minute_offset, role, content in turns:
        created_at = session_start + timedelta(minutes=minute_offset)
        database.log_conversation_message(
            session_id=session_id,
            role=role,
            content=content,
            user_id=user_id,
            created_at=created_at,
        )
        stats["messages"] += 1
        if created_at < first_message_at:
            first_message_at = created_at
        if created_at > last_message_at:
            last_message_at = created_at

    database.close_session(session_id, summary=summary)
    database.set_session_timestamps(
        session_id,
        started_at=first_message_at - timedelta(minutes=1),
        ended_at=last_message_at + timedelta(minutes=3),
        summary=summary,
    )
    return session_id


def add_vital(
    stats: Counter,
    user_id: int,
    vital_type: str,
    value: str,
    unit: str | None,
    recorded_at: datetime,
    notes: str,
) -> dict:
    vital = database.log_vital(
        user_id=user_id,
        vital_type=vital_type,
        value=value,
        unit=unit,
        notes=notes,
        recorded_at=recorded_at,
    )
    stats["vitals"] += 1
    return vital


def add_medication_event(
    stats: Counter,
    medication_id: int,
    user_id: int,
    event_type: str,
    recorded_at: datetime,
    dose_mg: float | None = None,
    scheduled_at: datetime | None = None,
    notes: str | None = None,
) -> dict:
    event = database.log_medication_event(
        medication_id=medication_id,
        user_id=user_id,
        event_type=event_type,
        dose_mg=dose_mg,
        scheduled_at=scheduled_at or recorded_at,
        notes=notes,
    )
    database.set_medication_event_timestamps(
        event_id=int(event["id"]),
        recorded_at=recorded_at,
        scheduled_at=scheduled_at or recorded_at,
    )
    stats["medication_events"] += 1
    return event


def add_side_effect(
    stats: Counter,
    user_id: int,
    symptom: str,
    severity: str,
    reported_at: datetime,
    medication_id: int | None = None,
    titration_step: int | None = None,
    notes: str | None = None,
    resolved_at: datetime | None = None,
) -> dict:
    side_effect = database.log_side_effect(
        user_id=user_id,
        symptom=symptom,
        severity=severity,
        medication_id=medication_id,
        titration_step=titration_step,
        notes=notes,
    )
    database.set_side_effect_timestamps(
        side_effect_id=int(side_effect["id"]),
        reported_at=reported_at,
        resolved_at=resolved_at,
        update_resolved_at=resolved_at is not None,
    )
    stats["side_effects"] += 1
    return side_effect


def add_completed_refill(
    stats: Counter,
    user_id: int,
    medication_id: int,
    due_date: date,
    requested_at: datetime,
    flagged_at: datetime,
    confirmed_at: datetime,
    completed_at: datetime,
    pharmacy_name: str,
    notes: str | None = None,
) -> dict:
    refill = database.create_refill_request(user_id=user_id, medication_id=medication_id, due_date=due_date)
    database.update_refill_status(refill["id"], user_id, "requested", notes=notes, pharmacy_name=pharmacy_name)
    database.update_refill_status(refill["id"], user_id, "flagged_for_team", notes=notes, pharmacy_name=pharmacy_name)
    database.update_refill_status(refill["id"], user_id, "confirmed", notes=notes, pharmacy_name=pharmacy_name)
    database.update_refill_status(refill["id"], user_id, "completed", notes=notes, pharmacy_name=pharmacy_name)
    database.set_refill_timestamps(
        refill_id=int(refill["id"]),
        created_at=requested_at - timedelta(hours=10),
        requested_at=requested_at,
        flagged_at=flagged_at,
        confirmed_at=confirmed_at,
        completed_at=completed_at,
    )
    stats["refills"] += 1
    return database.get_refill_by_id(int(refill["id"]), user_id) or refill


def add_active_refill(
    stats: Counter,
    user_id: int,
    medication_id: int,
    due_date: date,
    final_status: str,
    requested_at: datetime | None = None,
    flagged_at: datetime | None = None,
    pharmacy_name: str | None = None,
    notes: str | None = None,
) -> dict:
    refill = database.create_refill_request(user_id=user_id, medication_id=medication_id, due_date=due_date)
    created_at = requested_at - timedelta(hours=8) if requested_at else datetime.now(timezone.utc)

    if final_status in {"requested", "flagged_for_team"}:
        database.update_refill_status(refill["id"], user_id, "requested", notes=notes, pharmacy_name=pharmacy_name)
    if final_status == "flagged_for_team":
        database.update_refill_status(refill["id"], user_id, "flagged_for_team", notes=notes, pharmacy_name=pharmacy_name)

    database.set_refill_timestamps(
        refill_id=int(refill["id"]),
        created_at=created_at,
        requested_at=requested_at,
        flagged_at=flagged_at,
    )
    stats["refills"] += 1
    return database.get_refill_by_id(int(refill["id"]), user_id) or refill


def add_outreach(
    stats: Counter,
    user_id: int,
    outreach_type: str,
    scheduled_at: datetime,
    medication_id: int | None,
    priority: int,
    status: str = "pending",
    created_at: datetime | None = None,
    attempted_at: datetime | None = None,
    completed_at: datetime | None = None,
    attempt_count: int | None = None,
    session_id: int | None = None,
    outcome_summary: str | None = None,
    context_json: dict | None = None,
) -> dict:
    outreach = database.create_outreach(
        user_id=user_id,
        outreach_type=outreach_type,
        scheduled_at=scheduled_at,
        medication_id=medication_id,
        priority=priority,
        context_json=context_json,
    )

    if status != "pending" or outcome_summary is not None or session_id is not None:
        database.update_outreach_status(
            int(outreach["id"]),
            status=status,
            outcome_summary=outcome_summary,
            session_id=session_id,
        )

    database.set_outreach_timestamps(
        outreach_id=int(outreach["id"]),
        created_at=created_at or (scheduled_at - timedelta(days=1)),
        scheduled_at=scheduled_at,
        attempted_at=attempted_at,
        completed_at=completed_at,
        attempt_count=attempt_count,
        status=status,
        session_id=session_id,
    )
    stats["outreach"] += 1
    return outreach


def minimal_brief(user_id: int, trigger_reason: str, severity: str, medication_id: int | None = None) -> dict:
    user = database.get_user_by_id(user_id)
    medication = database.get_medication_by_id(medication_id, user_id) if medication_id else None
    return {
        "patient": {
            "name": user.name if user else "Patient",
            "age": database.age_from_dob(user.dob) if user and user.dob else (user.age if user else None),
            "weight_kg": user.weight_kg if user else None,
            "height_cm": user.height_cm if user else None,
        },
        "current_medication": medication or None,
        "trigger": {
            "reason": trigger_reason,
            "severity": severity,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        },
        "recent_vitals": database.get_recent_vitals(user_id, limit=5),
        "side_effect_timeline": database.get_side_effect_timeline(user_id),
        "medication_adherence": {},
        "conversation_excerpt": [],
        "recommended_actions": ["Clinical review recommended"],
    }


def add_escalation(
    stats: Counter,
    user_id: int,
    session_id: int,
    severity: str,
    trigger_reason: str,
    medication_id: int | None,
    created_at: datetime,
    status: str = "open",
    resolution_notes: str | None = None,
    resolved_at: datetime | None = None,
) -> dict:
    try:
        brief = generate_brief(
            user_id=user_id,
            session_id=session_id,
            trigger_reason=trigger_reason,
            severity=severity,
            medication_id=medication_id,
        )
    except Exception:
        brief = minimal_brief(user_id, trigger_reason, severity, medication_id=medication_id)

    escalation = database.create_escalation(
        user_id=user_id,
        session_id=session_id,
        severity=severity,
        trigger_reason=trigger_reason,
        brief_json=json.dumps(brief),
        medication_id=medication_id,
    )
    if status != "open":
        database.update_escalation_status(
            escalation_id=int(escalation["id"]),
            status=status,
            resolution_notes=resolution_notes,
        )

    database.set_escalation_timestamps(
        escalation_id=int(escalation["id"]),
        created_at=created_at,
        resolved_at=resolved_at,
        status=status,
    )
    stats["escalations"] += 1
    return database.get_escalation_by_id(int(escalation["id"])) or escalation


def add_doctor_recommendations(
    stats: Counter,
    user_id: int,
    session_id: int,
    created_at: datetime,
    doctors: list[dict],
) -> list[dict]:
    saved = database.save_doctor_recommendations(user_id=user_id, session_id=session_id, doctors=doctors)
    for recommendation in saved:
        database.set_doctor_recommendation_created_at(int(recommendation["id"]), created_at=created_at)
    stats["doctor_recommendations"] += len(saved)
    return saved


def seed_sarah(stats: Counter, now: datetime):
    user_id = create_registered_demo_user(
        stats,
        name="Sarah Chen",
        email=REGISTERED_DEMOS["Sarah Chen"],
        dob=date(1992, 3, 15),
        weight_kg=89.0,
        height_cm=165.0,
    )
    start = utc_days_ago(now, 28, 8, 15)
    medication = add_medication(
        stats,
        user_id=user_id,
        drug_name="semaglutide",
        brand_name="Wegovy",
        current_dose_mg=0.5,
        start_date=start.date(),
        titration_step=2,
        status="active",
        notes="Week 4 demo patient doing well overall.",
    )

    intake_session = add_session(
        stats,
        user_id,
        offset_days(start, 0, 9, 10),
        "Started Wegovy and reviewed onboarding basics.",
        [
            (0, "user", "hi aria, i started wegovy this morning and wanted to make sure im doing this right"),
            (2, "assistant", "Morning, Sarah. Starting week one can feel like a lot, but you're doing all the right things by checking in early."),
            (5, "user", "the injection itself was easier than i expected. im mostly nervous about side effects"),
            (7, "assistant", "That's totally normal. Most people notice the biggest adjustment in the first couple of weeks, and we can keep an eye on it together."),
            (10, "user", "good to know. i took the 0.25 shot and im planning to stay on schedule"),
            (12, "assistant", "Perfect. I'll track today's first dose and your starting weight so we have a clean baseline."),
        ],
    )
    add_vital(stats, user_id, "weight", "89.0", "kg", offset_days(start, 0, 9, 28), "Starting weight at Wegovy intake")
    add_vital(stats, user_id, "heart_rate", "72", "bpm", offset_days(start, 0, 9, 29), "Baseline heart rate at start of treatment")

    nausea_session = add_session(
        stats,
        user_id,
        offset_days(start, 3, 18, 5),
        "Reported mild nausea during first week on Wegovy.",
        [
            (0, "user", "hey aria, just wanted to check in. i started the wegovy 3 days ago and ive been feeling a little nauseous"),
            (2, "assistant", "Hey Sarah! Thanks for checking in. Mild nausea is super common in the first week or so, your body's adjusting to the medication. Have you tried eating smaller meals throughout the day?"),
            (5, "user", "yeah ive been trying that. its not terrible just annoying"),
            (7, "assistant", "Totally get it. The good news is it usually settles down within a week or two. I logged the nausea for tracking. How's the injection site feeling? Any redness or soreness?"),
            (10, "user", "nah the injection was fine actually"),
            (12, "assistant", "Great to hear. Do you know your blood pressure offhand? I just want a baseline while you're settling in."),
            (15, "user", "i think it was like 118 over 76 last time i checked"),
            (17, "assistant", "Perfect, that's right where you want to be. I've got that logged. Anything else, or are you good for now?"),
        ],
    )
    add_vital(stats, user_id, "blood_pressure", "118/76", "mmHg", offset_days(start, 3, 18, 25), "Baseline blood pressure during early nausea check-in")

    improvement_session = add_session(
        stats,
        user_id,
        offset_days(start, 10, 12, 15),
        "Week 2 check-in: nausea resolved and appetite suppression noted.",
        [
            (0, "user", "quick update, the nausea pretty much went away and im actually feeling good this week"),
            (2, "assistant", "Love hearing that. That early adjustment period can be annoying, but it usually passes just like this."),
            (5, "user", "my appetite is definitely lower though. im getting full really fast"),
            (7, "assistant", "That's expected on Wegovy. Just keep aiming for smaller protein-heavy meals so your energy stays steady."),
            (10, "user", "sounds good. my weight was 88.2 this morning"),
            (12, "assistant", "Nice, I logged that. You're trending in the right direction and staying consistent."),
        ],
    )
    add_vital(stats, user_id, "weight", "88.2", "kg", offset_days(start, 10, 12, 28), "Weekly home scale update")
    add_vital(stats, user_id, "heart_rate", "68", "bpm", offset_days(start, 10, 12, 29), "Heart rate during improved week 2 check-in")

    dose_increase_session = add_session(
        stats,
        user_id,
        offset_days(start, 16, 17, 40),
        "Reported nausea returning briefly after step 2 dose increase.",
        [
            (0, "user", "hey, i moved up to the 0.5 dose a couple days ago and the nausea came back"),
            (2, "assistant", "That can definitely happen after a dose increase. Usually it's a short bump while your stomach adjusts again."),
            (5, "user", "it feels stronger than week one but im still able to work"),
            (7, "assistant", "Okay, that's helpful context. Keep portions small, stay hydrated, and avoid heavy meals for a few days."),
            (10, "user", "my blood pressure earlier was 122 over 78 and my weight was 87.5"),
            (12, "assistant", "Thanks, I logged both. Your pressure still looks solid, and we'll keep an eye on the nausea over the next several days."),
        ],
    )
    add_vital(stats, user_id, "blood_pressure", "122/78", "mmHg", offset_days(start, 16, 17, 55), "Blood pressure after Sarah's dose increase")
    add_vital(stats, user_id, "weight", "87.5", "kg", offset_days(start, 16, 17, 56), "Weight after moving to step 2")

    day_25_session = add_session(
        stats,
        user_id,
        offset_days(start, 25, 8, 45),
        "Regular check-in: symptoms improving and refill moving forward.",
        [
            (0, "user", "morning aria. im doing a lot better now, just a little constipated but otherwise fine"),
            (2, "assistant", "Morning, Sarah. Glad the nausea settled down again. Mild constipation is pretty common at this stage."),
            (5, "user", "good because i was worried the higher dose wasnt going to work for me"),
            (7, "assistant", "You're still well within the normal adjustment window. Most people smooth out after a few days, just like you did."),
            (10, "user", "my weight was 87.1, bp 120 over 75, and heart rate 70"),
            (12, "assistant", "Amazing, I logged all three. You're staying right on track, and I can see your refill request is already moving along too."),
        ],
    )
    add_vital(stats, user_id, "weight", "87.1", "kg", offset_days(start, 25, 9, 5), "Week 4 home scale reading")
    add_vital(stats, user_id, "blood_pressure", "120/75", "mmHg", offset_days(start, 25, 9, 6), "Blood pressure during week 4 check-in")
    add_vital(stats, user_id, "heart_rate", "70", "bpm", offset_days(start, 25, 9, 7), "Heart rate during week 4 check-in")

    add_medication_event(stats, medication["id"], user_id, "dose_taken", offset_days(start, 0, 8, 0), dose_mg=0.25, notes="Started Wegovy")
    add_medication_event(stats, medication["id"], user_id, "dose_taken", offset_days(start, 7, 8, 5), dose_mg=0.25, notes="Week 2 dose taken on time")
    add_medication_event(stats, medication["id"], user_id, "dose_taken", offset_days(start, 14, 8, 0), dose_mg=0.25, notes="Final 0.25mg dose before escalation")
    add_medication_event(stats, medication["id"], user_id, "dose_adjusted", offset_days(start, 14, 20, 10), dose_mg=0.5, notes="Escalated to step 2 after four weeks")
    add_medication_event(stats, medication["id"], user_id, "dose_taken", offset_days(start, 21, 8, 10), dose_mg=0.5, notes="First 0.5mg weekly dose")
    add_medication_event(stats, medication["id"], user_id, "dose_taken", offset_days(start, 28, 8, 20), dose_mg=0.5, notes="Current week's dose")

    add_side_effect(
        stats,
        user_id,
        "nausea",
        "mild",
        reported_at=offset_days(start, 2, 11, 20),
        resolved_at=offset_days(start, 8, 9, 0),
        medication_id=medication["id"],
        titration_step=1,
        notes="Mild first-week nausea, improved with smaller meals.",
    )
    add_side_effect(
        stats,
        user_id,
        "appetite change",
        "mild",
        reported_at=offset_days(start, 5, 13, 10),
        medication_id=medication["id"],
        titration_step=1,
        notes="Ongoing appetite suppression but eating regular protein-rich meals.",
    )
    add_side_effect(
        stats,
        user_id,
        "nausea",
        "moderate",
        reported_at=offset_days(start, 16, 8, 35),
        resolved_at=offset_days(start, 22, 10, 45),
        medication_id=medication["id"],
        titration_step=2,
        notes="Nausea returned briefly after stepping up to 0.5mg.",
    )
    add_side_effect(
        stats,
        user_id,
        "constipation",
        "mild",
        reported_at=offset_days(start, 18, 16, 20),
        medication_id=medication["id"],
        titration_step=2,
        notes="Improving with more fluids and walking after dinner.",
    )

    add_completed_refill(
        stats,
        user_id=user_id,
        medication_id=medication["id"],
        due_date=offset_days(start, 28, 12, 0).date(),
        requested_at=offset_days(start, 24, 14, 15),
        flagged_at=offset_days(start, 25, 8, 30),
        confirmed_at=offset_days(start, 25, 15, 20),
        completed_at=offset_days(start, 26, 11, 10),
        pharmacy_name="CVS",
        notes="First-month refill completed without delay.",
    )

    add_outreach(
        stats,
        user_id=user_id,
        outreach_type="day_3_checkin",
        scheduled_at=offset_days(start, 3, 17, 30),
        medication_id=medication["id"],
        priority=3,
        status="completed",
        attempted_at=offset_days(start, 3, 17, 40),
        completed_at=offset_days(start, 3, 17, 43),
        attempt_count=1,
        session_id=nausea_session,
        outcome_summary="Reached Sarah by chat. Mild nausea reviewed and blood pressure logged.",
        context_json={"reason": "new_patient_onboarding"},
    )
    add_outreach(
        stats,
        user_id=user_id,
        outreach_type="week_1_checkin",
        scheduled_at=offset_days(start, 7, 12, 0),
        medication_id=medication["id"],
        priority=3,
        status="completed",
        attempted_at=offset_days(start, 7, 12, 15),
        completed_at=offset_days(start, 7, 12, 18),
        attempt_count=1,
        outcome_summary="Nausea improving. No injection issues reported.",
        context_json={"reason": "new_patient_onboarding"},
    )
    add_outreach(
        stats,
        user_id=user_id,
        outreach_type="week_2_side_effect_check",
        scheduled_at=offset_days(start, 14, 12, 20),
        medication_id=medication["id"],
        priority=2,
        status="completed",
        attempted_at=offset_days(start, 14, 12, 25),
        completed_at=offset_days(start, 14, 12, 29),
        attempt_count=1,
        outcome_summary="Sarah noted appetite suppression but felt stable overall.",
        context_json={"reason": "new_patient_onboarding"},
    )
    add_outreach(
        stats,
        user_id=user_id,
        outreach_type="week_1_checkin",
        scheduled_at=offset_days(start, 21, 11, 50),
        medication_id=medication["id"],
        priority=4,
        status="completed",
        attempted_at=offset_days(start, 21, 12, 0),
        completed_at=offset_days(start, 21, 12, 3),
        attempt_count=1,
        outcome_summary="Dose increase reviewed. Temporary nausea spike discussed.",
        context_json={"reason": "new_patient_onboarding"},
    )
    add_outreach(
        stats,
        user_id=user_id,
        outreach_type="refill_reminder",
        scheduled_at=offset_days(start, 28, 10, 0),
        medication_id=medication["id"],
        priority=3,
        status="pending",
        created_at=offset_days(start, 27, 13, 0),
        context_json={"reason": "new_patient_onboarding"},
    )

    add_doctor_recommendations(
        stats,
        user_id=user_id,
        session_id=nausea_session,
        created_at=offset_days(start, 3, 18, 35),
        doctors=[
            {
                "name": "Desert Digestive Specialists",
                "recommended_for": "gastroenterologist",
                "address": "2210 East Camelback Rd, Phoenix, AZ",
                "phone": "602-555-2101",
                "place_id": "demo-sarah-gastro-1",
                "latitude": 33.5091,
                "longitude": -112.0342,
                "rating": 4.6,
                "distance_text": "2.4 km",
                "photo_url": "https://example.com/demo/sarah-gastro.jpg",
                "is_open": True,
            },
            {
                "name": "Valley Family Medicine",
                "recommended_for": "general practitioner",
                "address": "1455 South Rural Rd, Tempe, AZ",
                "phone": "480-555-2012",
                "place_id": "demo-sarah-gp-1",
                "latitude": 33.4142,
                "longitude": -111.9261,
                "rating": 4.4,
                "distance_text": "3.1 km",
                "photo_url": "https://example.com/demo/sarah-gp.jpg",
                "is_open": True,
            },
        ],
    )


def seed_marcus(stats: Counter, now: datetime):
    user_id = create_registered_demo_user(
        stats,
        name="Marcus Johnson",
        email=REGISTERED_DEMOS["Marcus Johnson"],
        dob=date(1984, 8, 19),
        weight_kg=110.0,
        height_cm=180.0,
    )
    start = utc_days_ago(now, 14, 7, 45)
    medication = add_medication(
        stats,
        user_id=user_id,
        drug_name="tirzepatide",
        brand_name="Mounjaro",
        current_dose_mg=2.5,
        start_date=start.date(),
        titration_step=1,
        status="active",
        notes="Week 2 patient struggling with GI side effects and fading engagement.",
    )

    session_day0 = add_session(
        stats,
        user_id,
        offset_days(start, 0, 10, 0),
        "Started Mounjaro and reviewed first-week expectations.",
        [
            (0, "user", "hi, i started mounjaro today and just wanted a quick rundown of what to expect"),
            (2, "assistant", "Absolutely. The first couple of weeks are mostly about getting comfortable with the routine and watching for stomach side effects."),
            (5, "user", "im hoping it helps because my appetite has been all over the place lately"),
            (7, "assistant", "That's a common reason people start. We'll track your symptoms and your weekly doses so you can see the pattern clearly."),
        ],
    )
    add_vital(stats, user_id, "weight", "110.0", "kg", offset_days(start, 0, 10, 18), "Starting weight at Mounjaro intake")

    session_day3 = add_session(
        stats,
        user_id,
        offset_days(start, 3, 19, 5),
        "Severe nausea reported three days after starting Mounjaro.",
        [
            (0, "user", "aria this nausea is rough. im not throwing up but i feel sick most of the day"),
            (2, "assistant", "I'm sorry, Marcus. That sounds exhausting. Nausea can hit hard early on, but I want to track how intense it feels for you."),
            (5, "user", "it feels more than mild honestly. ive barely wanted dinner the last two nights"),
            (7, "assistant", "Thanks for saying that clearly. I'm logging it as severe so your team can see the pattern. Keep sipping fluids tonight, even small amounts."),
            (10, "user", "okay. ill try"),
            (12, "assistant", "If it tips into vomiting or you can't keep fluids down, please reach out right away."),
        ],
    )

    session_day5 = add_session(
        stats,
        user_id,
        offset_days(start, 5, 14, 10),
        "Persistent severe nausea with elevated blood pressure prompted escalation.",
        [
            (0, "user", "hey i still feel awful. ive been nauseous all week and my blood pressure was 142 over 92 today"),
            (2, "assistant", "Thanks for checking in, Marcus. I'm concerned that it's still this intense. I'll log the blood pressure and flag this for clinical review."),
            (5, "user", "i also had diarrhea again this morning and i barely ate lunch"),
            (7, "assistant", "Got it. I'm adding that too. Focus on fluids for now and keep meals bland and small if you can."),
            (10, "user", "yeah i mostly just want this to calm down"),
            (12, "assistant", "Totally understandable. Your care team should look at the full picture from here, and we may follow up again soon."),
        ],
    )
    add_vital(stats, user_id, "blood_pressure", "142/92", "mmHg", offset_days(start, 5, 14, 25), "Elevated BP during severe side-effect escalation session")
    add_vital(stats, user_id, "heart_rate", "88", "bpm", offset_days(start, 5, 14, 26), "Heart rate during Marcus escalation check-in")

    add_medication_event(stats, medication["id"], user_id, "dose_taken", offset_days(start, 0, 8, 10), dose_mg=2.5, notes="First Mounjaro dose taken")
    add_medication_event(stats, medication["id"], user_id, "dose_missed", offset_days(start, 7, 8, 20), dose_mg=2.5, notes="Forgot weekly injection")
    add_medication_event(stats, medication["id"], user_id, "dose_taken", offset_days(start, 14, 19, 15), dose_mg=2.5, notes="Took the missed weekly dose late")

    add_side_effect(
        stats,
        user_id,
        "nausea",
        "severe",
        reported_at=offset_days(start, 3, 19, 18),
        medication_id=medication["id"],
        titration_step=1,
        notes="Persistent nausea interfering with meals.",
    )
    add_side_effect(
        stats,
        user_id,
        "nausea",
        "severe",
        reported_at=offset_days(start, 5, 18, 35),
        medication_id=medication["id"],
        titration_step=1,
        notes="Still severe after several days, logged again to mark worsening pattern.",
    )
    add_side_effect(
        stats,
        user_id,
        "diarrhea",
        "moderate",
        reported_at=offset_days(start, 5, 14, 30),
        medication_id=medication["id"],
        titration_step=1,
        notes="Ongoing loose stools alongside nausea.",
    )
    add_side_effect(
        stats,
        user_id,
        "sulfur burps",
        "mild",
        reported_at=offset_days(start, 8, 16, 0),
        medication_id=medication["id"],
        titration_step=1,
        notes="Reported unpleasant sulfur burps with meals.",
    )

    add_outreach(
        stats,
        user_id=user_id,
        outreach_type="day_3_checkin",
        scheduled_at=offset_days(start, 3, 18, 0),
        medication_id=medication["id"],
        priority=3,
        status="completed",
        attempted_at=offset_days(start, 3, 18, 8),
        completed_at=offset_days(start, 3, 18, 12),
        attempt_count=1,
        session_id=session_day3,
        outcome_summary="Reached Marcus. Severe nausea discussed and hydration guidance reinforced.",
        context_json={"reason": "new_patient_onboarding"},
    )
    add_outreach(
        stats,
        user_id=user_id,
        outreach_type="week_2_side_effect_check",
        scheduled_at=offset_days(start, 14, 9, 0),
        medication_id=medication["id"],
        priority=2,
        status="pending",
        created_at=offset_days(start, 13, 9, 30),
        context_json={"reason": "critical_week_2_check"},
    )

    add_escalation(
        stats,
        user_id=user_id,
        session_id=session_day5,
        severity="serious",
        trigger_reason="Severe nausea reported on Mounjaro 2.5mg with blood pressure 142/92.",
        medication_id=medication["id"],
        created_at=offset_days(start, 5, 15, 10),
        status="open",
    )

    risk, reason = detect_churn_risk(user_id)
    if not risk or not reason:
        raise RuntimeError("Marcus Johnson should trigger churn risk detection but did not.")


def seed_emily(stats: Counter, now: datetime):
    user_id = create_registered_demo_user(
        stats,
        name="Emily Rodriguez",
        email=REGISTERED_DEMOS["Emily Rodriguez"],
        dob=date(1998, 5, 9),
        weight_kg=76.0,
        height_cm=160.0,
    )
    start = utc_days_ago(now, 3, 9, 20)
    medication = add_medication(
        stats,
        user_id=user_id,
        drug_name="semaglutide",
        brand_name="Ozempic",
        current_dose_mg=0.25,
        start_date=start.date(),
        titration_step=1,
        status="active",
        notes="New patient on day 3 with mild injection-site irritation only.",
    )

    add_session(
        stats,
        user_id,
        offset_days(start, 0, 11, 0),
        "Completed intake and first-dose onboarding for Ozempic.",
        [
            (0, "user", "hi aria, first ozempic shot is done and i just wanted to make sure im set up right"),
            (2, "assistant", "You're off to a great start, Emily. The big things this week are staying hydrated and noticing how your stomach feels."),
            (5, "user", "so far the only thing is a little redness where i injected"),
            (7, "assistant", "A little irritation at the site can happen, especially early on. I'll log that so we can see if it fades over the next few days."),
            (10, "user", "perfect. otherwise im okay"),
            (12, "assistant", "Great. We'll check back in at day three, but if anything changes sooner, message me."),
        ],
    )

    add_vital(stats, user_id, "weight", "76.0", "kg", offset_days(start, 0, 11, 18), "Starting weight for Ozempic treatment")
    add_medication_event(stats, medication["id"], user_id, "dose_taken", offset_days(start, 0, 9, 15), dose_mg=0.25, notes="First Ozempic dose")
    add_side_effect(
        stats,
        user_id,
        "injection site reaction",
        "mild",
        reported_at=offset_days(start, 1, 15, 30),
        medication_id=medication["id"],
        titration_step=1,
        notes="Mild redness and soreness at the injection site.",
    )
    add_outreach(
        stats,
        user_id=user_id,
        outreach_type="day_3_checkin",
        scheduled_at=offset_days(start, 3, 12, 0),
        medication_id=medication["id"],
        priority=3,
        status="pending",
        created_at=offset_days(start, 2, 12, 30),
        context_json={"reason": "new_patient_onboarding"},
    )


def seed_david(stats: Counter, now: datetime):
    user_id = create_registered_demo_user(
        stats,
        name="David Kim",
        email=REGISTERED_DEMOS["David Kim"],
        dob=date(1974, 11, 2),
        weight_kg=95.0,
        height_cm=175.0,
    )
    start = utc_days_ago(now, 21, 8, 0)
    medication = add_medication(
        stats,
        user_id=user_id,
        drug_name="semaglutide",
        brand_name="Wegovy",
        current_dose_mg=0.25,
        start_date=start.date(),
        titration_step=1,
        status="paused",
        notes="Paused after severe GI intolerance in week 1.",
    )

    add_session(
        stats,
        user_id,
        offset_days(start, 0, 10, 0),
        "Initial Wegovy start session.",
        [
            (0, "user", "starting wegovy today and hoping this finally helps with cravings"),
            (2, "assistant", "I'm glad you checked in, David. We'll take it one week at a time and pay close attention to how your stomach responds."),
            (5, "user", "sounds good. ill keep you posted"),
            (7, "assistant", "Perfect. If nausea shows up, start with small bland meals and extra fluids."),
        ],
    )
    session_nausea = add_session(
        stats,
        user_id,
        offset_days(start, 2, 18, 0),
        "Severe nausea reported after Wegovy start.",
        [
            (0, "user", "im pretty nauseous tonight. way more than i expected"),
            (2, "assistant", "I'm sorry, David. That sounds miserable. I want to log exactly how intense it feels and keep a close eye on it."),
            (5, "user", "honestly it feels severe. i can barely look at dinner"),
            (7, "assistant", "Okay, I'll mark it that way. Keep sipping water and hold off on anything greasy for now."),
        ],
    )
    session_vomiting = add_session(
        stats,
        user_id,
        offset_days(start, 3, 11, 10),
        "Vomiting reported and medication pause discussed.",
        [
            (0, "user", "i started vomiting this morning and havent wanted to eat anything"),
            (2, "assistant", "Thanks for telling me right away. I'm concerned by that escalation, and I'm flagging this for your care team."),
            (5, "user", "i dont think i can keep taking the shot like this"),
            (7, "assistant", "That's understandable. Your clinician may want you to pause until they review the full picture."),
        ],
    )
    follow_up = add_session(
        stats,
        user_id,
        offset_days(start, 18, 9, 40),
        "Follow-up after medication pause.",
        [
            (0, "user", "just checking back in. the vomiting stopped after i paused it"),
            (2, "assistant", "I'm relieved to hear that. It sounds like the pause gave your system a chance to calm down."),
            (5, "user", "yeah im still deciding if i want to restart later"),
            (7, "assistant", "That makes sense. Your care team can help decide whether a slower restart or a different plan fits better."),
        ],
    )

    add_vital(stats, user_id, "weight", "95.0", "kg", offset_days(start, 0, 10, 12), "Starting weight before side effects began")
    add_vital(stats, user_id, "blood_pressure", "134/86", "mmHg", offset_days(start, 3, 11, 22), "Blood pressure during severe vomiting escalation")
    add_vital(stats, user_id, "heart_rate", "84", "bpm", offset_days(start, 3, 11, 23), "Heart rate during severe vomiting escalation")

    add_medication_event(stats, medication["id"], user_id, "dose_taken", offset_days(start, 0, 8, 0), dose_mg=0.25, notes="First Wegovy dose")
    add_medication_event(stats, medication["id"], user_id, "dose_taken", offset_days(start, 7, 8, 5), dose_mg=0.25, notes="Second Wegovy dose")
    add_medication_event(stats, medication["id"], user_id, "dose_missed", offset_days(start, 14, 9, 0), dose_mg=0.25, notes="Skipped dose while paused because of symptoms")
    add_medication_event(stats, medication["id"], user_id, "dose_missed", offset_days(start, 21, 9, 15), dose_mg=0.25, notes="Still paused at week 4")

    add_side_effect(
        stats,
        user_id,
        "nausea",
        "severe",
        reported_at=offset_days(start, 2, 18, 20),
        medication_id=medication["id"],
        titration_step=1,
        notes="Marked severe due to poor appetite and persistent queasiness.",
    )
    add_side_effect(
        stats,
        user_id,
        "vomiting",
        "severe",
        reported_at=offset_days(start, 3, 11, 18),
        medication_id=medication["id"],
        titration_step=1,
        notes="Vomiting prompted urgent clinical follow-up.",
    )
    add_side_effect(
        stats,
        user_id,
        "stomach pain",
        "moderate",
        reported_at=offset_days(start, 5, 13, 15),
        medication_id=medication["id"],
        titration_step=1,
        notes="Cramping persisted for a couple of days after vomiting episode.",
    )

    add_escalation(
        stats,
        user_id=user_id,
        session_id=session_vomiting,
        severity="serious",
        trigger_reason="Severe vomiting reported on Wegovy 0.25mg during week 1.",
        medication_id=medication["id"],
        created_at=offset_days(start, 3, 11, 30),
        status="resolved",
        resolution_notes="Patient advised to pause medication. Follow up in 2 weeks.",
        resolved_at=offset_days(start, 4, 9, 10),
    )

    add_outreach(
        stats,
        user_id=user_id,
        outreach_type="retention_risk",
        scheduled_at=utc_days_ago(now, 1, 16, 0),
        medication_id=medication["id"],
        priority=1,
        status="in_progress",
        created_at=utc_days_ago(now, 2, 18, 20),
        attempted_at=utc_days_ago(now, 1, 16, 10),
        attempt_count=1,
        outcome_summary="Reached voicemail. Care team plans another follow-up while David remains paused.",
        context_json={"reason": "paused_medication_and_low_engagement"},
    )


def seed_priya(stats: Counter, now: datetime):
    user_id = create_registered_demo_user(
        stats,
        name="Priya Patel",
        email=REGISTERED_DEMOS["Priya Patel"],
        dob=date(1987, 7, 22),
        weight_kg=72.0,
        height_cm=163.0,
    )
    start = utc_days_ago(now, 90, 8, 0)
    medication = add_medication(
        stats,
        user_id=user_id,
        drug_name="semaglutide",
        brand_name="Wegovy",
        current_dose_mg=2.4,
        start_date=start.date(),
        titration_step=5,
        status="active",
        notes="Maintenance-dose success story with strong adherence and steady weight loss.",
    )

    session_0 = add_session(
        stats,
        user_id,
        offset_days(start, 0, 10, 0),
        "Started Wegovy and set nutrition goals.",
        [
            (0, "user", "i finally picked up wegovy and im excited but also nervous to start"),
            (2, "assistant", "That's a totally normal mix of feelings. The goal this month is consistency, hydration, and noticing how your appetite changes."),
            (5, "user", "if i can get some control over late night eating ill be thrilled"),
            (7, "assistant", "That's a great target. We'll track both symptoms and progress so you can actually see the changes over time."),
        ],
    )
    add_session(
        stats,
        user_id,
        offset_days(start, 14, 9, 20),
        "Early treatment check-in with mild nausea improving.",
        [
            (0, "user", "the first week was a little queasy but its already easing up"),
            (2, "assistant", "That's exactly what we hope to see. Early nausea is common, and it usually fades as your body adjusts."),
            (5, "user", "my portions are definitely smaller now"),
            (7, "assistant", "Great. Keep focusing on protein first and don't let hydration slide while you're eating less."),
        ],
    )
    refill_session = add_session(
        stats,
        user_id,
        offset_days(start, 30, 13, 0),
        "First refill coordination session.",
        [
            (0, "user", "hey, i think my first refill is coming up soon"),
            (2, "assistant", "Good timing. Staying ahead of the refill is the easiest way to keep momentum going."),
            (5, "user", "can you flag it for the team just in case"),
            (7, "assistant", "Absolutely. I'll note it and make sure the refill workflow moves along."),
        ],
    )
    add_session(
        stats,
        user_id,
        offset_days(start, 45, 8, 40),
        "Mid-program check-in showing continued weight loss.",
        [
            (0, "user", "checking in because im down to 77 now and feeling way more in control around food"),
            (2, "assistant", "That is fantastic progress, Priya. More importantly, it sounds sustainable, not rushed."),
            (5, "user", "yeah it finally feels steady"),
            (7, "assistant", "That's the sweet spot. When the routine feels steady, long-term success gets much easier."),
        ],
    )
    add_session(
        stats,
        user_id,
        offset_days(start, 70, 17, 15),
        "Approaching maintenance with minimal side effects.",
        [
            (0, "user", "surprisingly the higher doses havent been bad at all"),
            (2, "assistant", "That's great. A lot of patients worry each titration step will feel rough, so a smooth transition is a big win."),
            (5, "user", "my blood pressure was 116 over 74 this week too"),
            (7, "assistant", "Beautiful. I'll add that in. Your trends are looking really strong across the board."),
        ],
    )
    add_session(
        stats,
        user_id,
        offset_days(start, 88, 9, 10),
        "Month 3 success check-in with maintenance dose established.",
        [
            (0, "user", "im at 72 now and honestly feel better than i have in years"),
            (2, "assistant", "That is such a good update. You've built a really consistent routine, and it shows."),
            (5, "user", "i still want to stay on top of refills so i dont lose momentum"),
            (7, "assistant", "Absolutely. I can already see the next refill queue starting, which is exactly where we want it."),
        ],
    )

    for vital_time, value in [
        (offset_days(start, 0, 10, 20), "85.0"),
        (offset_days(start, 14, 9, 35), "82.0"),
        (offset_days(start, 30, 13, 18), "79.5"),
        (offset_days(start, 45, 8, 55), "77.0"),
        (offset_days(start, 70, 17, 28), "74.5"),
        (offset_days(start, 88, 9, 25), "72.0"),
    ]:
        add_vital(stats, user_id, "weight", value, "kg", vital_time, "Weight trend during Priya's GLP-1 success story")

    add_vital(stats, user_id, "blood_pressure", "116/74", "mmHg", offset_days(start, 70, 17, 29), "Maintenance-phase blood pressure")
    add_vital(stats, user_id, "heart_rate", "66", "bpm", offset_days(start, 70, 17, 30), "Maintenance-phase heart rate")

    dose_days = [0, 7, 14, 21, 28, 35, 44, 49, 56, 63, 70, 84]
    dose_values = [0.25, 0.25, 0.25, 0.25, 0.5, 0.5, 0.75, 0.75, 1.0, 1.0, 1.7, 2.4]
    for day_offset, dose in zip(dose_days, dose_values):
        add_medication_event(
            stats,
            medication["id"],
            user_id,
            "dose_taken",
            offset_days(start, day_offset, 8, 5),
            dose_mg=dose,
            notes="Weekly dose logged during Priya's maintenance journey.",
        )
    add_medication_event(
        stats,
        medication["id"],
        user_id,
        "dose_missed",
        offset_days(start, 42, 8, 0),
        dose_mg=0.75,
        notes="Travel week caused one delayed dose before she got back on schedule.",
    )
    add_medication_event(
        stats,
        medication["id"],
        user_id,
        "dose_adjusted",
        offset_days(start, 28, 18, 0),
        dose_mg=0.5,
        notes="Escalation to 0.5mg",
    )
    add_medication_event(
        stats,
        medication["id"],
        user_id,
        "dose_adjusted",
        offset_days(start, 56, 18, 5),
        dose_mg=1.0,
        notes="Escalation to 1.0mg",
    )
    add_medication_event(
        stats,
        medication["id"],
        user_id,
        "dose_adjusted",
        offset_days(start, 70, 18, 10),
        dose_mg=1.7,
        notes="Escalation to 1.7mg",
    )
    add_medication_event(
        stats,
        medication["id"],
        user_id,
        "dose_adjusted",
        offset_days(start, 84, 18, 15),
        dose_mg=2.4,
        notes="Moved to maintenance dose",
    )

    add_side_effect(
        stats,
        user_id,
        "nausea",
        "mild",
        reported_at=offset_days(start, 3, 12, 10),
        resolved_at=offset_days(start, 10, 9, 0),
        medication_id=medication["id"],
        titration_step=1,
        notes="Mild early nausea that resolved quickly.",
    )
    add_side_effect(
        stats,
        user_id,
        "constipation",
        "mild",
        reported_at=offset_days(start, 32, 14, 10),
        resolved_at=offset_days(start, 40, 10, 5),
        medication_id=medication["id"],
        titration_step=2,
        notes="Short-lived constipation during second month.",
    )

    add_completed_refill(
        stats,
        user_id=user_id,
        medication_id=medication["id"],
        due_date=offset_days(start, 28, 12, 0).date(),
        requested_at=offset_days(start, 27, 16, 10),
        flagged_at=offset_days(start, 28, 9, 30),
        confirmed_at=offset_days(start, 28, 13, 0),
        completed_at=offset_days(start, 29, 11, 40),
        pharmacy_name="Walgreens",
        notes="First refill completed smoothly.",
    )
    add_completed_refill(
        stats,
        user_id=user_id,
        medication_id=medication["id"],
        due_date=offset_days(start, 56, 12, 0).date(),
        requested_at=offset_days(start, 55, 15, 20),
        flagged_at=offset_days(start, 56, 8, 45),
        confirmed_at=offset_days(start, 56, 14, 0),
        completed_at=offset_days(start, 57, 10, 50),
        pharmacy_name="Walgreens",
        notes="Second refill completed on time.",
    )
    add_active_refill(
        stats,
        user_id=user_id,
        medication_id=medication["id"],
        due_date=(now + timedelta(days=4)).date(),
        final_status="flagged_for_team",
        requested_at=utc_days_ago(now, 1, 14, 15),
        flagged_at=utc_days_ago(now, 0, 10, 5),
        pharmacy_name="Walgreens",
        notes="Upcoming maintenance refill already flagged so Priya stays on schedule.",
    )

    for outreach_type, day_offset, priority, linked_session, summary in [
        ("day_3_checkin", 3, 3, session_0, "Mild nausea normalized and nutrition tips reviewed."),
        ("week_1_checkin", 7, 3, None, "Priya reported smaller appetite but good hydration."),
        ("week_2_side_effect_check", 14, 2, None, "Symptoms minimal and trending down."),
        ("week_1_checkin", 21, 4, refill_session, "Stayed engaged and started thinking ahead about refill timing."),
        ("refill_reminder", 28, 3, refill_session, "Refill plan confirmed early."),
    ]:
        scheduled_at = offset_days(start, day_offset, 12, 0)
        add_outreach(
            stats,
            user_id=user_id,
            outreach_type=outreach_type,
            scheduled_at=scheduled_at,
            medication_id=medication["id"],
            priority=priority,
            status="completed",
            attempted_at=scheduled_at + timedelta(minutes=12),
            completed_at=scheduled_at + timedelta(minutes=15),
            attempt_count=1,
            session_id=linked_session,
            outcome_summary=summary,
            context_json={"reason": "new_patient_onboarding"},
        )


def seed_james(stats: Counter, now: datetime):
    user_id = create_basic_demo_user(stats, "James Wilson", 46, 101.0, 178.0)
    start = utc_days_ago(now, 7, 8, 30)
    medication = add_medication(
        stats,
        user_id=user_id,
        drug_name="tirzepatide",
        brand_name="Zepbound",
        current_dose_mg=2.5,
        start_date=start.date(),
        titration_step=1,
        status="active",
        notes="Week 1 tirzepatide patient with no major issues yet.",
    )
    add_session(
        stats,
        user_id,
        offset_days(start, 0, 9, 40),
        "Initial Zepbound onboarding session.",
        [
            (0, "user", "started zepbound today and honestly so far so good"),
            (2, "assistant", "That's a great start, James. The first week is usually about routine more than results."),
            (5, "user", "im mostly just hoping i dont get hit with nausea"),
            (7, "assistant", "Fair concern. We'll keep an eye on it, but for now just stay hydrated and keep meals simple."),
        ],
    )
    add_medication_event(stats, medication["id"], user_id, "dose_taken", offset_days(start, 0, 8, 15), dose_mg=2.5, notes="First Zepbound dose")
    add_vital(stats, user_id, "blood_pressure", "124/80", "mmHg", offset_days(start, 0, 9, 55), "Baseline blood pressure")
    add_outreach(
        stats,
        user_id=user_id,
        outreach_type="week_1_checkin",
        scheduled_at=offset_days(start, 7, 12, 30),
        medication_id=medication["id"],
        priority=3,
        status="pending",
        created_at=offset_days(start, 6, 14, 0),
        context_json={"reason": "new_patient_onboarding"},
    )


def seed_aisha(stats: Counter, now: datetime):
    user_id = create_basic_demo_user(stats, "Aisha Mohammed", 33, 83.0, 168.0)
    start = utc_days_ago(now, 14, 8, 10)
    medication = add_medication(
        stats,
        user_id=user_id,
        drug_name="semaglutide",
        brand_name="Wegovy",
        current_dose_mg=0.25,
        start_date=start.date(),
        titration_step=1,
        status="active",
        notes="Week 2 semaglutide patient with mild nausea but good adherence.",
    )
    session_0 = add_session(
        stats,
        user_id,
        offset_days(start, 0, 10, 20),
        "Initial Wegovy start session.",
        [
            (0, "user", "took my first wegovy shot today"),
            (2, "assistant", "Nice work. The first week is mostly about learning your body's rhythm and staying gentle with food choices."),
            (5, "user", "ill keep an eye on it"),
            (7, "assistant", "Perfect. If nausea shows up, smaller meals and slower eating usually help a lot."),
        ],
    )
    session_1 = add_session(
        stats,
        user_id,
        offset_days(start, 9, 18, 15),
        "Mild nausea but overall steady progress.",
        [
            (0, "user", "i had some mild nausea this week but overall im okay"),
            (2, "assistant", "That's encouraging. Mild nausea at this stage is really common, and it sounds like it's staying manageable."),
            (5, "user", "yeah it usually passes if i eat something small"),
            (7, "assistant", "Great pattern to notice. I'll log it, and we can keep an eye on whether it fades by next week."),
        ],
    )
    add_medication_event(stats, medication["id"], user_id, "dose_taken", offset_days(start, 0, 8, 5), dose_mg=0.25, notes="First Wegovy dose")
    add_medication_event(stats, medication["id"], user_id, "dose_taken", offset_days(start, 7, 8, 10), dose_mg=0.25, notes="Second Wegovy dose")
    add_side_effect(
        stats,
        user_id,
        "nausea",
        "mild",
        reported_at=offset_days(start, 4, 12, 0),
        medication_id=medication["id"],
        titration_step=1,
        notes="Brief mild nausea relieved by smaller snacks.",
    )
    add_vital(stats, user_id, "blood_pressure", "117/76", "mmHg", offset_days(start, 9, 18, 25), "Blood pressure during mild nausea check-in")
    add_vital(stats, user_id, "heart_rate", "71", "bpm", offset_days(start, 9, 18, 26), "Heart rate during mild nausea check-in")
    add_outreach(
        stats,
        user_id=user_id,
        outreach_type="day_3_checkin",
        scheduled_at=offset_days(start, 3, 12, 15),
        medication_id=medication["id"],
        priority=3,
        status="completed",
        attempted_at=offset_days(start, 3, 12, 25),
        completed_at=offset_days(start, 3, 12, 28),
        attempt_count=1,
        session_id=session_0,
        outcome_summary="Aisha doing well. No urgent concerns.",
        context_json={"reason": "new_patient_onboarding"},
    )
    add_outreach(
        stats,
        user_id=user_id,
        outreach_type="week_2_side_effect_check",
        scheduled_at=offset_days(start, 14, 11, 45),
        medication_id=medication["id"],
        priority=2,
        status="completed",
        attempted_at=offset_days(start, 14, 12, 0),
        completed_at=offset_days(start, 14, 12, 4),
        attempt_count=1,
        session_id=session_1,
        outcome_summary="Mild nausea remains manageable. Patient still on track.",
        context_json={"reason": "new_patient_onboarding"},
    )


def seed_robert(stats: Counter, now: datetime):
    user_id = create_basic_demo_user(stats, "Robert Taylor", 49, 108.0, 182.0)
    start = utc_days_ago(now, 28, 7, 50)
    medication = add_medication(
        stats,
        user_id=user_id,
        drug_name="tirzepatide",
        brand_name="Mounjaro",
        current_dose_mg=5.0,
        start_date=start.date(),
        titration_step=2,
        status="active",
        notes="Week 4 tirzepatide patient who just moved to step 2 and is noticing bloating.",
    )

    add_session(
        stats,
        user_id,
        offset_days(start, 0, 10, 5),
        "Initial Mounjaro onboarding session.",
        [
            (0, "user", "starting mounjaro today and hoping i can stick with it"),
            (2, "assistant", "That's a great place to start. Consistency matters more than perfection early on."),
            (5, "user", "im ready for the routine part, i just dont know how ill feel after"),
            (7, "assistant", "Totally fair. We'll track each week and keep dose changes visible so nothing feels random."),
        ],
    )
    session_dose_increase = add_session(
        stats,
        user_id,
        offset_days(start, 22, 16, 10),
        "Dose increase follow-up after moving to 5.0mg.",
        [
            (0, "user", "i moved up to 5 milligrams and im feeling a little bloated"),
            (2, "assistant", "That makes sense after a dose increase. Bloating is one of the more common adjustments right after stepping up."),
            (5, "user", "nothing severe, just uncomfortable after dinner"),
            (7, "assistant", "Got it. Smaller meals and avoiding carbonated drinks usually help while things settle."),
        ],
    )
    add_session(
        stats,
        user_id,
        offset_days(start, 27, 9, 30),
        "Current week check-in with moderate bloating.",
        [
            (0, "user", "checking in again because the bloating is still hanging around"),
            (2, "assistant", "Thanks for the update. Since it's still there, I'll keep it logged and make sure the titration follow-up stays on the queue."),
            (5, "user", "okay. otherwise the dose itself is going fine"),
            (7, "assistant", "That's useful to know. If it stays moderate and not painful, we can usually keep monitoring rather than panic."),
        ],
    )

    add_vital(stats, user_id, "blood_pressure", "128/82", "mmHg", offset_days(start, 27, 9, 42), "Blood pressure after dose increase")
    add_vital(stats, user_id, "heart_rate", "76", "bpm", offset_days(start, 27, 9, 43), "Heart rate after dose increase")
    add_medication_event(stats, medication["id"], user_id, "dose_taken", offset_days(start, 0, 8, 0), dose_mg=2.5, notes="Week 1 dose")
    add_medication_event(stats, medication["id"], user_id, "dose_taken", offset_days(start, 7, 8, 0), dose_mg=2.5, notes="Week 2 dose")
    add_medication_event(stats, medication["id"], user_id, "dose_taken", offset_days(start, 14, 8, 0), dose_mg=2.5, notes="Week 3 dose")
    add_medication_event(stats, medication["id"], user_id, "dose_adjusted", offset_days(start, 21, 18, 0), dose_mg=5.0, notes="Escalated to step 2")
    add_medication_event(stats, medication["id"], user_id, "dose_taken", offset_days(start, 21, 8, 5), dose_mg=5.0, notes="First 5.0mg dose")
    add_medication_event(stats, medication["id"], user_id, "dose_taken", offset_days(start, 28, 8, 10), dose_mg=5.0, notes="Current weekly dose")
    add_side_effect(
        stats,
        user_id,
        "bloating",
        "moderate",
        reported_at=offset_days(start, 23, 18, 20),
        medication_id=medication["id"],
        titration_step=2,
        notes="Moderate bloating after evening meals since the dose increase.",
    )
    add_outreach(
        stats,
        user_id=user_id,
        outreach_type="titration_step_followup",
        scheduled_at=utc_days_ago(now, 1, 11, 0),
        medication_id=medication["id"],
        priority=2,
        status="pending",
        created_at=utc_days_ago(now, 3, 13, 30),
        context_json={"reason": "dose_increase", "dose_mg": 5.0},
    )
    add_outreach(
        stats,
        user_id=user_id,
        outreach_type="week_1_checkin",
        scheduled_at=offset_days(start, 7, 12, 10),
        medication_id=medication["id"],
        priority=3,
        status="completed",
        attempted_at=offset_days(start, 7, 12, 20),
        completed_at=offset_days(start, 7, 12, 24),
        attempt_count=1,
        outcome_summary="Early tirzepatide tolerance looked good before dose increase.",
        context_json={"reason": "new_patient_onboarding"},
    )


def reset_demo_data():
    for email in REGISTERED_DEMOS.values():
        user = database.get_user_by_email(email)
        if user:
            database.delete_user(user.id)

    for name in NON_AUTH_DEMOS:
        user = database.get_user_by_name(name)
        if user and user.email is None:
            database.delete_user(user.id)


def main():
    parser = argparse.ArgumentParser(description="Seed CuraConnect with realistic GLP-1 demo data.")
    parser.add_argument("--reset", action="store_true", help="Delete existing demo data and recreate it.")
    args = parser.parse_args()

    database.init_db()

    existing_sarah = database.get_user_by_email(REGISTERED_DEMOS["Sarah Chen"])
    if existing_sarah and not args.reset:
        print("Demo data already exists. Run with --reset to recreate.")
        return

    if args.reset:
        reset_demo_data()

    now = datetime.now(timezone.utc)
    stats: Counter = Counter()

    seed_sarah(stats, now)
    seed_marcus(stats, now)
    seed_emily(stats, now)
    seed_david(stats, now)
    seed_priya(stats, now)
    seed_james(stats, now)
    seed_aisha(stats, now)
    seed_robert(stats, now)

    print(
        "Created 8 demo patients with "
        f"{stats['sessions']} sessions, "
        f"{stats['messages']} messages, "
        f"{stats['vitals']} vitals, "
        f"{stats['medication_events']} medication events, "
        f"{stats['side_effects']} side effects, "
        f"{stats['refills']} refill requests, "
        f"{stats['outreach']} outreach items, "
        f"{stats['escalations']} escalations, and "
        f"{stats['doctor_recommendations']} doctor recommendations."
    )


if __name__ == "__main__":
    main()
