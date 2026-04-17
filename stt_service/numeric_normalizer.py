"""Numeric cleanup helpers for speech transcripts."""

from __future__ import annotations

import re
from typing import Dict, List, Tuple

_DIGITS = {
    "zero": 0,
    "oh": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
}

_TEENS = {
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}

_TENS = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}

_NUMBER_WORDS = set(_DIGITS) | set(_TEENS) | set(_TENS) | {"hundred", "thousand", "and"}

_CONFUSABLE_PAIRS = [
    ("fifteen", "fifty"),
    ("fourteen", "forty"),
    ("thirteen", "thirty"),
    ("sixteen", "sixty"),
    ("seventeen", "seventy"),
    ("eighteen", "eighty"),
    ("nineteen", "ninety"),
    ("nine", "five"),
]

_WORD_PATTERN = r"(?:zero|oh|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|and)"
_NUMBER_SEQUENCE_RE = re.compile(rf"\b{_WORD_PATTERN}(?:[\s-]+{_WORD_PATTERN})*\b", re.IGNORECASE)
_BP_BY_OVER_RE = re.compile(r"\b(\d{2,3})\s*(?:by|over|slash)\s*(\d{2,3})\b", re.IGNORECASE)
_SPOKEN_BP_EDGE_RE = re.compile(r"\b1[:\s](\d{2})\s*(?:by|over|slash|/)\s*(\d{2,3})\b", re.IGNORECASE)


def _parse_under_100(tokens: List[str]) -> int | None:
    if not tokens:
        return None
    if len(tokens) == 1:
        token = tokens[0]
        if token in _DIGITS:
            return _DIGITS[token]
        if token in _TEENS:
            return _TEENS[token]
        if token in _TENS:
            return _TENS[token]
        return None

    if tokens[0] in _TENS and len(tokens) == 2 and tokens[1] in _DIGITS:
        return _TENS[tokens[0]] + _DIGITS[tokens[1]]

    return None


def _parse_number_phrase(raw_tokens: List[str]) -> int | None:
    tokens = [token for token in raw_tokens if token and token != "and"]
    if not tokens:
        return None

    if all(token in _DIGITS for token in tokens):
        if len(tokens) == 1:
            return _DIGITS[tokens[0]]
        joined = "".join(str(_DIGITS[token]) for token in tokens)
        return int(joined)

    # Supports "one twenty two" -> 122 (common speech pattern in vitals).
    if len(tokens) >= 2 and tokens[0] in _DIGITS and tokens[0] not in {"zero", "oh"}:
        remainder = _parse_under_100(tokens[1:])
        if remainder is not None:
            return (_DIGITS[tokens[0]] * 100) + remainder

    total = 0
    current = 0
    saw_number = False

    for token in tokens:
        if token in _DIGITS:
            current += _DIGITS[token]
            saw_number = True
            continue
        if token in _TEENS:
            current += _TEENS[token]
            saw_number = True
            continue
        if token in _TENS:
            current += _TENS[token]
            saw_number = True
            continue
        if token == "hundred":
            current = max(current, 1) * 100
            saw_number = True
            continue
        if token == "thousand":
            total += max(current, 1) * 1000
            current = 0
            saw_number = True
            continue
        return None

    if not saw_number:
        return None
    return total + current


def _replace_number_words(text: str) -> Tuple[str, List[Dict]]:
    corrections: List[Dict] = []

    def replace(match: re.Match) -> str:
        spoken = match.group(0)
        raw_tokens = [token for token in re.split(r"[\s-]+", spoken.lower()) if token]
        if not raw_tokens:
            return spoken

        leading_and = 0
        trailing_and = 0
        while leading_and < len(raw_tokens) and raw_tokens[leading_and] == "and":
            leading_and += 1
        while trailing_and < len(raw_tokens) and raw_tokens[len(raw_tokens) - trailing_and - 1] == "and":
            trailing_and += 1

        if leading_and + trailing_and >= len(raw_tokens):
            return spoken

        start = leading_and
        end = len(raw_tokens) - trailing_and if trailing_and else len(raw_tokens)
        core_tokens = raw_tokens[start:end]

        tokens = [token for token in core_tokens if token in _NUMBER_WORDS]
        if not tokens:
            return spoken

        number_value = _parse_number_phrase(tokens)
        if number_value is None:
            return spoken

        corrected = str(number_value)
        if leading_and:
            corrected = ("and " * leading_and) + corrected
        if trailing_and:
            corrected = corrected + (" and" * trailing_and)

        if corrected.lower() != spoken.lower():
            corrections.append(
                {
                    "heard": " ".join(core_tokens),
                    "suggested": str(number_value),
                    "reason": "spoken number normalized to digits",
                }
            )
        return corrected

    return _NUMBER_SEQUENCE_RE.sub(replace, text), corrections


def _normalize_units(text: str) -> str:
    normalized = text
    normalized = re.sub(r"\b(?:kilograms?|kgs?)\b", "kg", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\b(?:centimeters?|cms?)\b", "cm", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\b(?:beats per minute|beats a minute)\b", "bpm", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\b(?:millimeters of mercury)\b", "mmHg", normalized, flags=re.IGNORECASE)
    return normalized


def _normalize_blood_pressure(text: str) -> str:
    normalized = text
    lowered = normalized.lower()
    has_bp_context = "blood pressure" in lowered or bool(re.search(r"\bbp\b", lowered))

    if has_bp_context:
        normalized = _SPOKEN_BP_EDGE_RE.sub(lambda m: f"1{m.group(1)}/{m.group(2)}", normalized)

    normalized = _BP_BY_OVER_RE.sub(lambda m: f"{m.group(1)}/{m.group(2)}", normalized)
    normalized = re.sub(r"\b(\d{2,3})\s*/\s*(\d{2,3})\b", lambda m: f"{m.group(1)}/{m.group(2)}", normalized)
    return normalized


def _flag_confusable_pairs(text: str) -> List[Dict]:
    lowered = text.lower()
    flags: List[Dict] = []
    for left, right in _CONFUSABLE_PAIRS:
        if re.search(rf"\b{left}\b", lowered):
            flags.append(
                {
                    "found": left,
                    "confusable_with": right,
                    "note": f"'{left}' can be confused with '{right}' in noisy speech.",
                }
            )
    return flags


def normalize_numerics(text: str) -> Dict:
    digitized, number_corrections = _replace_number_words(text)
    unit_normalized = _normalize_units(digitized)
    bp_normalized = _normalize_blood_pressure(unit_normalized)
    collapsed = re.sub(r"\s+", " ", bp_normalized).strip()

    return {
        "text": collapsed,
        "corrections": number_corrections,
        "confusable_flags": _flag_confusable_pairs(text),
    }
