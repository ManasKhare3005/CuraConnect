"""Optional local speech-to-text backend for noisy voice capture fallback.

This layer is intentionally optional. If `faster-whisper` is not installed,
the service reports itself as unavailable and the app falls back to browser STT.
"""

from __future__ import annotations

import math
import os
import tempfile
import time
from typing import Any


class LocalTranscriber:
    def __init__(self):
        self.backend = "unavailable"
        self.model_name = os.getenv("LOCAL_STT_MODEL", "base")
        self.device = os.getenv("LOCAL_STT_DEVICE", "auto")
        self.compute_type = os.getenv("LOCAL_STT_COMPUTE_TYPE", "int8")
        self._load_error: str | None = None
        self._model: Any = None
        self._load_backend()

    @property
    def is_available(self) -> bool:
        return self.backend != "unavailable" and self._model is not None

    @property
    def load_error(self) -> str | None:
        return self._load_error

    def _load_backend(self):
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except Exception as exc:  # pragma: no cover - optional dependency
            self._load_error = f"faster-whisper not installed: {exc}"
            return

        try:
            self._model = WhisperModel(
                self.model_name,
                device=self.device,
                compute_type=self.compute_type,
            )
            self.backend = "faster-whisper"
        except Exception as exc:  # pragma: no cover - runtime environment dependent
            self._load_error = f"failed to initialize faster-whisper model: {exc}"
            self._model = None
            self.backend = "unavailable"

    def transcribe_bytes(
        self,
        audio_bytes: bytes,
        *,
        suffix: str = ".webm",
        language: str = "en",
    ) -> dict[str, Any]:
        start = time.perf_counter()
        if not audio_bytes:
            return {
                "status": "error",
                "text": "",
                "confidence": 0.0,
                "backend": self.backend,
                "latency_ms": int((time.perf_counter() - start) * 1000),
                "error": "empty audio payload",
            }

        if not self.is_available:
            return {
                "status": "unavailable",
                "text": "",
                "confidence": 0.0,
                "backend": self.backend,
                "latency_ms": int((time.perf_counter() - start) * 1000),
                "error": self._load_error or "local transcriber unavailable",
            }

        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix or ".webm", delete=False) as temp:
                temp.write(audio_bytes)
                temp_path = temp.name

            segments, _ = self._model.transcribe(
                temp_path,
                language=language or "en",
                beam_size=5,
                best_of=5,
                vad_filter=True,
                condition_on_previous_text=False,
                temperature=0.0,
            )

            collected_text: list[str] = []
            confidences: list[float] = []
            for segment in segments:
                chunk = str(getattr(segment, "text", "") or "").strip()
                if chunk:
                    collected_text.append(chunk)

                avg_logprob = getattr(segment, "avg_logprob", None)
                if isinstance(avg_logprob, (float, int)):
                    try:
                        confidences.append(max(0.0, min(1.0, math.exp(float(avg_logprob)))))
                    except OverflowError:
                        pass

            transcript = " ".join(collected_text).strip()
            confidence = round(sum(confidences) / len(confidences), 3) if confidences else 0.0

            return {
                "status": "ok",
                "text": transcript,
                "confidence": confidence,
                "backend": self.backend,
                "latency_ms": int((time.perf_counter() - start) * 1000),
                "error": None,
            }
        except Exception as exc:  # pragma: no cover - runtime model/audio errors
            return {
                "status": "error",
                "text": "",
                "confidence": 0.0,
                "backend": self.backend,
                "latency_ms": int((time.perf_counter() - start) * 1000),
                "error": str(exc),
            }
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
