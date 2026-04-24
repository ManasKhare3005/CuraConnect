"""
LLM service for natural language response generation.

The LLM is used ONLY for conversational phrasing. It receives a structured
context object describing what the agent has decided to do, and returns
a natural-sounding message. It NEVER makes clinical decisions.

Priority order:
  1. Gemini API via ADC Bearer token — project-level rate limits, set VERTEX_PROJECT_ID to enable
  2. Gemini API via API key (GEMINI_API_KEY) — free tier, 15 RPM limit
  3. Groq (GROQ_API_KEY) — free tier, very fast, recommended fallback
  4. Anthropic Claude (ANTHROPIC_API_KEY) — reliable fallback, pay-as-you-go
  5. OpenAI-compatible (OPENAI_API_KEY) — optional fallback
  6. Template fallback — always works, robotic but functional
"""

from __future__ import annotations

import logging
import os
import re
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
VERTEX_PROJECT_ID = os.getenv("VERTEX_PROJECT_ID", "").strip()
VERTEX_LOCATION = os.getenv("VERTEX_LOCATION", "us-central1").strip()
VERTEX_MODEL = os.getenv("VERTEX_MODEL", "gemini-2.0-flash")

# Cached ADC credentials — refreshed automatically when they expire
_vertex_credentials = None


def _get_vertex_token() -> str | None:
    global _vertex_credentials
    try:
        import google.auth
        import google.auth.transport.requests

        if _vertex_credentials is None or not _vertex_credentials.valid:
            credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
                quota_project_id=VERTEX_PROJECT_ID or None,
            )
            _vertex_credentials = credentials

        if not _vertex_credentials.valid:
            _vertex_credentials.refresh(google.auth.transport.requests.Request())

        return _vertex_credentials.token
    except Exception as exc:
        logger.warning("[llm] Failed to get Vertex AI token: %s", exc)
        return None

SYSTEM_PROMPT = """
You are Aria, a healthcare assistant at CuraConnect who supports patients on GLP-1 weight loss medications (Ozempic, Wegovy, Mounjaro, Zepbound).

HOW YOU TALK:
- Like a warm, experienced nurse — not a chatbot filling out a form.
- 2-3 sentences max. Short is always better. Patients are tired and anxious.
- Use the patient's name occasionally — not every sentence.
- Acknowledge what they said, then move forward. Do not parrot their exact words back ("Sorry to hear about the allergies" is lazy — just acknowledge naturally and move on).
- Logging confirmation: "Logged your BP at 138/88" — never "I have successfully recorded your blood pressure reading."
- Asking for vitals: "Know your blood pressure offhand?" — never "Please provide your blood pressure reading."
- GLP-1 side effects: normalize them. "Super common in the first couple weeks" or "Your body's still adjusting — this usually settles down."
- Serious symptoms: be direct and caring. "That concerns me — I'm flagging this for your care team right now." Not clinical process language.
- Never say "I'm just an AI" or "I cannot provide medical advice."
- Never use emojis.
- Match the patient's energy. Casual → casual. Formal → professional. Scared → gentle and reassuring.

RULES:
- If "next_question" is provided, weave it naturally into the END of your response. Do not bolt it on. Do not skip it.
- If the patient gave multiple data points, acknowledge them fluidly — not as a list.
- If escalation was created, make the patient feel heard and safe, not alarmed by process language.
- Only convey information that is in the context. Do NOT invent medical details or medication specifics.
- Under 80 words. If you go over, cut.
""".strip()

http_client: httpx.AsyncClient | None = None


def _build_user_prompt(context: dict) -> str:
    parts: list[str] = []

    patient_name = context.get("patient_name") or "Unknown"
    parts.append(f"Patient: {patient_name}")

    if context.get("patient_context"):
        parts.append(f"Background: {context['patient_context']}")

    if context.get("recent_vitals"):
        parts.append(f"Recent vitals on file: {context['recent_vitals']}")

    tone = context.get("conversation_tone", "neutral")
    parts.append(f"Patient's tone: {tone}")

    history = context.get("conversation_history") or []
    if history:
        parts.append("\nRecent conversation:")
        for msg in history[-4:]:
            role = "Patient" if msg.get("role") == "user" else "Aria"
            parts.append(f"  {role}: {msg.get('content', '')}")

    actions = context.get("actions_taken") or []
    if actions:
        parts.append("\nWhat Aria just did:")
        for action in actions:
            parts.append(f"  - {action}")

    severity = context.get("clinical_decision")
    if severity:
        parts.append(f"\nClinical assessment: severity is {severity}")

    remedy = context.get("remedy_text")
    if remedy:
        parts.append(f"\nAdvice to convey (use your own words, don't read verbatim): {remedy}")

    if context.get("escalation_created"):
        parts.append("\nYou just flagged this for the clinical care team. Let the patient know their care team will follow up.")

    doctor_results = context.get("doctor_results")
    if isinstance(doctor_results, dict) and doctor_results.get("summary"):
        parts.append(f"\nDoctor search: {doctor_results['summary']}")

    refill_info = context.get("refill_info")
    if refill_info:
        parts.append(f"\nRefill update: {refill_info}")

    next_question = context.get("next_question")
    if next_question:
        parts.append(f"\nYou also need to: {next_question}")

    special = context.get("special_instructions") or []
    if special:
        parts.append("\nSpecial instructions:")
        for inst in special:
            parts.append(f"  - {inst}")

    parts.append("\nRespond as Aria. Natural, concise, under 80 words.")
    return "\n".join(parts)


def _clean_response_text(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    return cleaned.strip("\"' ")


def _contains_disallowed_medication_advice(text: str) -> bool:
    lowered = text.lower()
    blocked_patterns = (
        r"\bincrease your dose\b",
        r"\bdecrease your dose\b",
        r"\blower your dose\b",
        r"\braise your dose\b",
        r"\bchange your dose\b",
        r"\bskip your next dose\b",
        r"\bstop taking\b",
        r"\brestart\b.*\bmedication\b",
        r"\btake\s+\d+(?:\.\d+)?\s*mg\b",
    )
    return any(re.search(pattern, lowered) for pattern in blocked_patterns)


def _looks_valid_response(text: str, context: dict) -> bool:
    cleaned = _clean_response_text(text)
    if not cleaned:
        return False
    if len(cleaned.split()) > 120:
        return False
    lowered = cleaned.lower()
    if "i'm just an ai" in lowered or "i am just an ai" in lowered:
        return False
    if "i can't provide medical advice" in lowered or "i cannot provide medical advice" in lowered:
        return False
    if _contains_disallowed_medication_advice(cleaned):
        return False
    return True


def _question_from_field(field: str | None) -> str | None:
    if not field:
        return None
    mapping = {
        "name": "What name should I use for you?",
        "age": "How old are you?",
        "symptoms": "How are you feeling today?",
        "blood_pressure": "Do you know your blood pressure offhand?",
        "heart_rate": "Do you know your heart rate or pulse?",
        "temperature": "Do you have a temperature reading to share?",
        "oxygen_saturation": "If you have it, what is your oxygen level?",
        "doctor_preference": "Would you like nearby doctor recommendations?",
        "medication_dose": "Have you taken your injection this week?",
        "medication_side_effects": "Have you noticed any side effects since the injection?",
        "refill_help": "Is your prescription on track, or do you need help with the refill?",
        "refill_check": "Want me to flag that refill for your care team?",
        "more_help_confirmation": "Is there anything else I can help with?",
    }
    return mapping.get(field)


def _template_fallback(context: dict) -> str:
    """Generate a response without LLM - matches the original concatenation style."""
    fallback_parts = context.get("_template_parts")
    if isinstance(fallback_parts, list):
        merged = " ".join(str(part).strip() for part in fallback_parts if str(part or "").strip())
        if merged:
            return merged

    parts: list[str] = []

    for action in context.get("actions_taken", []):
        clean_action = str(action or "").strip()
        if clean_action:
            parts.append(clean_action)

    remedy_text = str(context.get("remedy_text") or "").strip()
    if remedy_text:
        parts.append(remedy_text)

    if context.get("escalation_created"):
        parts.append(
            "I've flagged this for your care team. They'll have your full history and can follow up with you directly."
        )

    refill_info = str(context.get("refill_info") or "").strip()
    if refill_info:
        parts.append(refill_info)

    doctor_results = context.get("doctor_results")
    if isinstance(doctor_results, dict):
        summary = str(doctor_results.get("summary") or "").strip()
        if summary:
            parts.append(summary)

    next_question = _question_from_field(context.get("next_question_field"))
    if next_question:
        parts.append(next_question)

    merged = " ".join(part.strip() for part in parts if part and part.strip())
    return merged or "I'm here whenever you need me."


async def _post_json(url: str, headers: dict[str, str], payload: dict) -> dict:
    client = http_client or httpx.AsyncClient(timeout=8.0)
    close_after = http_client is None
    try:
        response = await client.post(url, headers=headers, json=payload, timeout=8.0)
        if not response.is_success:
            logger.error("[llm] HTTP %s from %s — body: %s", response.status_code, url, response.text[:400])
        response.raise_for_status()
        return response.json()
    finally:
        if close_after:
            await client.aclose()


def _extract_gemini_text(data: dict) -> str:
    return (
        data.get("candidates", [{}])[0]
        .get("content", {})
        .get("parts", [{}])[0]
        .get("text", "")
    )


async def _call_vertex(context: dict) -> str | None:
    token = _get_vertex_token()
    if not token:
        return None
    url = (
        f"https://{VERTEX_LOCATION}-aiplatform.googleapis.com/v1"
        f"/projects/{VERTEX_PROJECT_ID}/locations/{VERTEX_LOCATION}"
        f"/publishers/google/models/{VERTEX_MODEL}:generateContent"
    )
    payload = {
        "contents": [{"parts": [{"text": _build_user_prompt(context)}]}],
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "generationConfig": {
            "temperature": 0.75,
            "maxOutputTokens": 180,
        },
    }
    start = time.perf_counter()
    data = await _post_json(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        payload=payload,
    )
    elapsed = time.perf_counter() - start
    logger.info("[llm] Vertex AI responded in %.2fs", elapsed)
    return _clean_response_text(_extract_gemini_text(data))


async def _call_gemini(context: dict, api_key: str) -> str | None:
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={api_key}"
    )
    payload = {
        "contents": [{"parts": [{"text": _build_user_prompt(context)}]}],
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "generationConfig": {
            "temperature": 0.75,
            "maxOutputTokens": 180,
        },
    }
    start = time.perf_counter()
    data = await _post_json(url, headers={"Content-Type": "application/json"}, payload=payload)
    elapsed = time.perf_counter() - start
    logger.info("[llm] Gemini AI Studio responded in %.2fs", elapsed)
    return _clean_response_text(_extract_gemini_text(data))


async def _call_openai_compatible(context: dict, api_key: str) -> str | None:
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/")
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"
    payload = {
        "model": model,
        "temperature": 0.7,
        "max_tokens": 200,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(context)},
        ],
    }
    start = time.perf_counter()
    data = await _post_json(
        f"{base_url}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        payload=payload,
    )
    elapsed = time.perf_counter() - start
    logger.info("[llm] OpenAI-compatible responded in %.2fs", elapsed)
    message = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    if isinstance(message, list):
        message = " ".join(
            str(part.get("text") or "")
            for part in message
            if isinstance(part, dict)
        )
    return _clean_response_text(message)


async def _call_groq(context: dict, api_key: str) -> str | None:
    model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip() or "llama-3.3-70b-versatile"
    payload = {
        "model": model,
        "temperature": 0.75,
        "max_tokens": 200,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(context)},
        ],
    }
    start = time.perf_counter()
    data = await _post_json(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        payload=payload,
    )
    elapsed = time.perf_counter() - start
    logger.info("[llm] Groq responded in %.2fs", elapsed)
    message = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    return _clean_response_text(message)


async def _call_anthropic(context: dict, api_key: str) -> str | None:
    model = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001").strip() or "claude-haiku-4-5-20251001"
    payload = {
        "model": model,
        "max_tokens": 200,
        "temperature": 0.75,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": _build_user_prompt(context)}],
    }
    start = time.perf_counter()
    data = await _post_json(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        payload=payload,
    )
    elapsed = time.perf_counter() - start
    logger.info("[llm] Anthropic responded in %.2fs", elapsed)
    content = data.get("content", [{}])
    text = content[0].get("text", "") if isinstance(content, list) and content else ""
    return _clean_response_text(text)


async def generate_response(context: dict) -> str:
    fallback = _template_fallback(context)

    if VERTEX_PROJECT_ID:
        try:
            response = await _call_vertex(context)
            cleaned = _clean_response_text(response or "")
            if _looks_valid_response(cleaned, context):
                logger.debug("[llm] Vertex accepted: %s", cleaned[:80])
                return cleaned
            logger.warning("[llm] Vertex response rejected by validator: %r", cleaned[:120])
        except Exception as exc:
            logger.warning("[llm] Vertex call failed: %s", exc)

    gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if gemini_api_key:
        try:
            response = await _call_gemini(context, gemini_api_key)
            cleaned = _clean_response_text(response or "")
            if _looks_valid_response(cleaned, context):
                logger.debug("[llm] Gemini accepted: %s", cleaned[:80])
                return cleaned
            logger.warning("[llm] Gemini response rejected by validator: %r", cleaned[:120])
        except Exception as exc:
            logger.warning("[llm] Gemini call failed: %s", exc)

    groq_api_key = os.getenv("GROQ_API_KEY", "").strip()
    if groq_api_key:
        try:
            response = await _call_groq(context, groq_api_key)
            cleaned = _clean_response_text(response or "")
            if _looks_valid_response(cleaned, context):
                logger.debug("[llm] Groq accepted: %s", cleaned[:80])
                return cleaned
            logger.warning("[llm] Groq response rejected by validator: %r", cleaned[:120])
        except Exception as exc:
            logger.warning("[llm] Groq call failed: %s", exc)

    anthropic_api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if anthropic_api_key:
        try:
            response = await _call_anthropic(context, anthropic_api_key)
            cleaned = _clean_response_text(response or "")
            if _looks_valid_response(cleaned, context):
                logger.debug("[llm] Anthropic accepted: %s", cleaned[:80])
                return cleaned
            logger.warning("[llm] Anthropic response rejected by validator: %r", cleaned[:120])
        except Exception as exc:
            logger.warning("[llm] Anthropic call failed: %s", exc)

    openai_api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if openai_api_key:
        try:
            response = await _call_openai_compatible(context, openai_api_key)
            cleaned = _clean_response_text(response or "")
            if _looks_valid_response(cleaned, context):
                logger.debug("[llm] OpenAI accepted: %s", cleaned[:80])
                return cleaned
            logger.warning("[llm] OpenAI response rejected by validator: %r", cleaned[:120])
        except Exception as exc:
            logger.warning("[llm] OpenAI call failed: %s", exc)

    logger.info("[llm] Using template fallback")
    return fallback
