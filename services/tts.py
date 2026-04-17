import base64
import logging
import os

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
ELEVENLABS_MODEL = "eleven_turbo_v2_5"

http_client: httpx.AsyncClient | None = None


async def synthesize_speech(text: str) -> str | None:
    if not ELEVENLABS_API_KEY:
        return None

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "text": text,
        "model_id": ELEVENLABS_MODEL,
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
            "style": 0.2,
            "use_speaker_boost": True,
        },
    }

    client = http_client or httpx.AsyncClient(timeout=30.0)
    close_after = http_client is None
    try:
        response = await client.post(url, headers=headers, json=payload)
        if response.status_code == 200:
            return base64.b64encode(response.content).decode("utf-8")
        else:
            logger.warning("ElevenLabs error %s: %s", response.status_code, response.text[:200])
            return None
    finally:
        if close_after:
            await client.aclose()
