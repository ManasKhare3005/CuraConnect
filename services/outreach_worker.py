"""
Background worker that processes pending outreach items.

In production, this would integrate with a telephony provider (Twilio, etc.)
to make actual calls or send SMS. For now, it processes the queue and logs actions.

Run with: python -m services.outreach_worker
"""

from __future__ import annotations

import asyncio
import logging
import random

from database import db as database

logger = logging.getLogger(__name__)

OUTREACH_MESSAGES = {
    "day_3_checkin": [
        "Hey {name}, it's Aria from CuraConnect. Just checking in - how did your first {brand_name} injection go? Any questions about the process?",
        "Hi {name}! Aria here. It's been a few days since you started {brand_name}. How are you feeling so far?",
    ],
    "week_1_checkin": [
        "Hi {name}, one week on {brand_name}! How's everything going? Any side effects or concerns?",
    ],
    "week_2_side_effect_check": [
        "Hey {name}, Aria from CuraConnect. Week two is when most people notice GI side effects like nausea. This is completely normal and usually improves. How are you doing?",
        "Hi {name}, checking in at the two-week mark. Many patients experience nausea or appetite changes around now - it typically gets better. How are things on your end?",
    ],
    "titration_step_followup": [
        "Hi {name}, you recently moved to {dose_mg}mg of {brand_name}. It is normal to feel some side effects after a dose increase. How are things going?",
    ],
    "refill_reminder": [
        "Hey {name}, just a heads up - your {brand_name} refill should be coming up soon. Want me to flag this for your care team?",
    ],
    "missed_dose_followup": [
        "Hi {name}, I noticed you may have missed a dose. No worries - just try to get back on schedule with your next injection. Any concerns?",
    ],
    "retention_risk": [
        "Hey {name}, it has been a little while since we heard from you. Just checking in to see how your {brand_name} treatment is going. We are here if you need anything.",
    ],
}


def _format_message(outreach: dict) -> str:
    templates = OUTREACH_MESSAGES.get(outreach.get("outreach_type")) or [
        "Hi {name}, this is Aria from CuraConnect checking in about your care plan.",
    ]
    medication = outreach.get("medication") or {}
    context = outreach.get("context") or {}
    name = outreach.get("user_name") or "there"
    brand_name = medication.get("brand_name") or medication.get("drug_name") or "your medication"
    dose_mg = context.get("dose_mg") or medication.get("current_dose_mg") or ""
    template = random.choice(templates)
    return template.format(name=name, brand_name=brand_name, dose_mg=dose_mg)


async def process_pending_outreach() -> None:
    pending_items = database.get_pending_outreach()
    for outreach in pending_items:
        outreach_id = int(outreach["id"])
        attempt_count = int(outreach.get("attempt_count") or 0)
        max_attempts = int(outreach.get("max_attempts") or 3)

        if attempt_count >= max_attempts:
            database.update_outreach_status(
                outreach_id,
                "patient_unreachable",
                outcome_summary="Maximum outreach attempts reached without contact.",
            )
            logger.info("Outreach %s marked patient_unreachable after %s attempts", outreach_id, attempt_count)
            continue

        incremented = database.increment_outreach_attempt(outreach_id)
        in_progress = database.update_outreach_status(outreach_id, "in_progress")
        message = _format_message(in_progress or incremented or outreach)
        logger.info(
            "Processing outreach %s [%s via %s]: %s",
            outreach_id,
            outreach.get("outreach_type"),
            outreach.get("channel"),
            message,
        )

        await asyncio.sleep(1)

        database.update_outreach_status(
            outreach_id,
            "completed",
            outcome_summary=f"Stub worker processed outreach via {outreach.get('channel')}: {message}",
        )
        logger.info("Completed outreach %s", outreach_id)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    database.init_db()
    logger.info("Outreach worker started")
    while True:
        try:
            await process_pending_outreach()
        except Exception:
            logger.exception("Outreach worker loop failed")
        await asyncio.sleep(30)


if __name__ == "__main__":
    asyncio.run(main())
