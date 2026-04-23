"""Conservative phonetic correction for common health and address terms."""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

DEFAULT_TERMS = [
    "allergy",
    "allergies",
    "hives",
    "rash",
    "itching",
    "breathlessness",
    "asthma",
    "wheezing",
    "blood",
    "pressure",
    "heart",
    "rate",
    "temperature",
    "weight",
    "height",
    "address",
    "doctor",
    "physician",
    "clinic",
    "avenue",
    "street",
    "drive",
    "road",
    "tempe",
    "arizona",
    "playa",
    "norte",
    "semaglutide",
    "tirzepatide",
    "ozempic",
    "wegovy",
    "mounjaro",
    "zepbound",
    "titration",
    "subcutaneous",
    "injection",
    "nausea",
    "constipation",
    "diarrhea",
    "injection site",
    "appetite",
    "refill",
    "pharmacy",
    "dosage",
]

COMMON_WORDS_TO_SKIP = {
    "have",
    "has",
    "had",
    "feel",
    "feels",
    "felt",
    "live",
    "lives",
    "name",
    "age",
    "with",
    "without",
    "some",
    "slight",
    "just",
    "been",
    "being",
}


def _metaphone(word: str) -> str:
    word = word.upper().strip()
    if not word:
        return ""

    compressed = word[0]
    for ch in word[1:]:
        if ch != compressed[-1] or ch == "C":
            compressed += ch
    word = compressed

    if word[:2] in {"KN", "GN", "PN", "AE", "WR"}:
        word = word[1:]

    result: List[str] = []
    i = 0
    while i < len(word):
        ch = word[i]
        prev_ch = word[i - 1] if i > 0 else ""
        next_ch = word[i + 1] if i + 1 < len(word) else ""

        if ch in "AEIOU":
            if i == 0:
                result.append(ch)
        elif ch == "B":
            if not (i == len(word) - 1 and prev_ch == "M"):
                result.append("B")
        elif ch == "C":
            if next_ch == "H":
                result.append("X")
                i += 1
            elif next_ch in "IEY":
                result.append("S")
            else:
                result.append("K")
        elif ch == "D":
            if next_ch == "G" and i + 2 < len(word) and word[i + 2] in "IEY":
                result.append("J")
                i += 2
            else:
                result.append("T")
        elif ch == "G":
            if next_ch == "H":
                i += 1
            elif next_ch in "IEY":
                result.append("J")
            else:
                result.append("K")
        elif ch == "H":
            if prev_ch not in "AEIOU" or next_ch in "AEIOU":
                result.append("H")
        elif ch in "FJLMNR":
            result.append(ch)
        elif ch == "K":
            if prev_ch != "C":
                result.append("K")
        elif ch == "P":
            if next_ch == "H":
                result.append("F")
                i += 1
            else:
                result.append("P")
        elif ch == "Q":
            result.append("K")
        elif ch == "S":
            if next_ch == "H":
                result.append("X")
                i += 1
            else:
                result.append("S")
        elif ch == "T":
            if next_ch == "H":
                result.append("0")
                i += 1
            else:
                result.append("T")
        elif ch == "V":
            result.append("F")
        elif ch in "WY":
            if next_ch in "AEIOU":
                result.append(ch)
        elif ch == "X":
            result.append("KS")
        elif ch == "Z":
            result.append("S")

        i += 1

    return "".join(result)


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)

    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            current.append(min(current[j - 1] + 1, previous[j] + 1, previous[j - 1] + cost))
        previous = current
    return previous[-1]


def _best_match(token: str, vocabulary: List[Tuple[str, str]]) -> Optional[Tuple[str, int, int]]:
    token_meta = _metaphone(token)
    if not token_meta:
        return None

    best_term: Optional[str] = None
    best_meta = 10**6
    best_char = 10**6
    for term, term_meta in vocabulary:
        meta_distance = _levenshtein(token_meta, term_meta)
        char_distance = _levenshtein(token.lower(), term.lower())

        combined = (meta_distance * 2) + char_distance
        best_combined = (best_meta * 2) + best_char
        if combined < best_combined:
            best_term = term
            best_meta = meta_distance
            best_char = char_distance

    if best_term is None:
        return None
    return best_term, best_meta, best_char


def correct_terms(
    text: str,
    expected_keywords: Optional[List[str]] = None,
    custom_terms: Optional[List[str]] = None,
) -> Dict:
    vocab = list(dict.fromkeys([*(expected_keywords or []), *(custom_terms or []), *DEFAULT_TERMS]))
    lowered_vocab = {term.lower() for term in vocab}
    keyed_vocab = [(term, _metaphone(term)) for term in vocab]

    corrections: List[Dict] = []

    def replace(match: re.Match) -> str:
        token = match.group(0)
        lower = token.lower()

        if lower in lowered_vocab:
            return token
        if len(token) < 4 or re.search(r"\d", token):
            return token
        if lower in COMMON_WORDS_TO_SKIP:
            return token

        result = _best_match(token, keyed_vocab)
        if result is None:
            return token

        suggested, meta_distance, char_distance = result
        if len(token) <= 4 and char_distance > 1:
            return token
        if (
            meta_distance <= 1
            and char_distance <= 2
            and token[0].lower() == suggested[0].lower()
            and lower != suggested.lower()
        ):
            corrections.append(
                {
                    "heard": token,
                    "suggested": suggested,
                    "phonetic_distance": meta_distance,
                    "character_distance": char_distance,
                    "reason": "phonetic correction",
                }
            )
            return suggested
        return token

    corrected = re.sub(r"\b[A-Za-z][A-Za-z'-]{3,}\b", replace, text)
    return {"text": corrected, "corrections": corrections}
