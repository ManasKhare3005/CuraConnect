from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from difflib import get_close_matches
from typing import Any

TITRATION_SCHEDULES = {
    "semaglutide": {
        "brand_names": ["Ozempic", "Wegovy"],
        "steps": [
            {"step": 1, "dose_mg": 0.25, "duration_weeks": 4, "label": "Starting dose"},
            {"step": 2, "dose_mg": 0.5, "duration_weeks": 4, "label": "Escalation 1"},
            {"step": 3, "dose_mg": 1.0, "duration_weeks": 4, "label": "Escalation 2"},
            {"step": 4, "dose_mg": 1.7, "duration_weeks": 4, "label": "Escalation 3"},
            {"step": 5, "dose_mg": 2.4, "duration_weeks": None, "label": "Maintenance"},
        ],
    },
    "tirzepatide": {
        "brand_names": ["Mounjaro", "Zepbound"],
        "steps": [
            {"step": 1, "dose_mg": 2.5, "duration_weeks": 4, "label": "Starting dose"},
            {"step": 2, "dose_mg": 5.0, "duration_weeks": 4, "label": "Escalation 1"},
            {"step": 3, "dose_mg": 7.5, "duration_weeks": 4, "label": "Escalation 2"},
            {"step": 4, "dose_mg": 10.0, "duration_weeks": 4, "label": "Escalation 3"},
            {"step": 5, "dose_mg": 12.5, "duration_weeks": 4, "label": "Escalation 4"},
            {"step": 6, "dose_mg": 15.0, "duration_weeks": None, "label": "Maintenance"},
        ],
    },
}


def _normalize_drug_name(drug_name: str | None) -> str:
    return (drug_name or "").strip().lower()


def _coerce_start_date(value: date | datetime | str | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    candidate = str(value).strip()
    if not candidate:
        return None
    try:
        return date.fromisoformat(candidate)
    except ValueError:
        return None


def get_current_step(drug_name: str, step_number: int) -> dict[str, Any] | None:
    schedule = TITRATION_SCHEDULES.get(_normalize_drug_name(drug_name))
    if not schedule:
        return None
    for step in schedule["steps"]:
        if step["step"] == int(step_number):
            return dict(step)
    return None


def get_next_step(drug_name: str, step_number: int) -> dict[str, Any] | None:
    schedule = TITRATION_SCHEDULES.get(_normalize_drug_name(drug_name))
    if not schedule:
        return None
    for step in schedule["steps"]:
        if step["step"] == int(step_number) + 1:
            return dict(step)
    return None


def days_until_next_titration(
    start_date: date | datetime | str | None,
    drug_name: str,
    current_step: int | dict[str, Any],
) -> int | None:
    normalized_start = _coerce_start_date(start_date)
    schedule = TITRATION_SCHEDULES.get(_normalize_drug_name(drug_name))
    if not normalized_start or not schedule:
        return None

    if isinstance(current_step, dict):
        step_number = int(current_step.get("step") or 0)
    else:
        step_number = int(current_step)

    if step_number <= 0:
        return None

    elapsed_weeks = 0
    target_step = None
    for step in schedule["steps"]:
        if step["step"] == step_number:
            target_step = step
            break
        elapsed_weeks += int(step.get("duration_weeks") or 0)

    if not target_step:
        return None

    duration_weeks = target_step.get("duration_weeks")
    if duration_weeks is None:
        return None

    next_titration_date = normalized_start + timedelta(weeks=elapsed_weeks + int(duration_weeks))
    return max((next_titration_date - date.today()).days, 0)


def identify_drug(text: str) -> tuple[str | None, str | None]:
    lowered = (text or "").strip().lower()
    if not lowered:
        return None, None

    alias_map = {
        "semaglutide": ("semaglutide", None),
        "ozempic": ("semaglutide", "Ozempic"),
        "wegovy": ("semaglutide", "Wegovy"),
        "tirzepatide": ("tirzepatide", None),
        "mounjaro": ("tirzepatide", "Mounjaro"),
        "zepbound": ("tirzepatide", "Zepbound"),
    }

    for alias, result in alias_map.items():
        if re.search(rf"\b{re.escape(alias)}\b", lowered):
            return result

    tokens = re.findall(r"[a-z0-9-]+", lowered)
    for token in tokens:
        matches = get_close_matches(token, alias_map.keys(), n=1, cutoff=0.82)
        if matches:
            return alias_map[matches[0]]

    return None, None
