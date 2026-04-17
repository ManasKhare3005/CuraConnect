"""Text-based STT enhancement pipeline used by the frontend transcript flow."""

from __future__ import annotations

import time
from typing import Any

from .numeric_normalizer import normalize_numerics
from .phonetic_matcher import correct_terms


class TranscriptEnhancer:
    """Enhance browser transcripts for better downstream entity extraction."""

    def enhance(
        self,
        transcript: str,
        expected_keywords: list[str] | None = None,
        custom_terms: list[str] | None = None,
    ) -> dict[str, Any]:
        raw = (transcript or "").strip()
        start_time = time.perf_counter()
        if not raw:
            return {
                "transcript": "",
                "raw_transcript": "",
                "numeric_corrections": [],
                "phonetic_corrections": [],
                "confusable_flags": [],
                "latency_ms": 0,
            }

        numeric_result = normalize_numerics(raw)
        phonetic_result = correct_terms(
            numeric_result["text"],
            expected_keywords=expected_keywords,
            custom_terms=custom_terms,
        )

        latency_ms = int((time.perf_counter() - start_time) * 1000)
        return {
            "transcript": phonetic_result["text"],
            "raw_transcript": raw,
            "numeric_corrections": numeric_result["corrections"],
            "phonetic_corrections": phonetic_result["corrections"],
            "confusable_flags": numeric_result["confusable_flags"],
            "latency_ms": latency_ms,
        }
