import ast
import asyncio
import json
import os
import re
from math import isclose
from typing import Any

from dotenv import load_dotenv

from database import db as database
from services.tts import synthesize_speech
from services.doctor_finder import find_nearby_doctors
from services.geocoding import geocode_address

load_dotenv()

INLINE_TOOL_CALL_RE = re.compile(r"<function=(\w+)(.*?)</function>", flags=re.DOTALL)

SYSTEM_PROMPT_BASE = """You are a warm, professional health assistant voice agent named Aria for CuraConnect.
Your job is to help users track practical health check-in information and connect them to nearby doctors when needed.

Conversation style:
- Keep responses short (2-3 sentences max). This is a voice-first interface.
- Be empathetic, conversational, and clear.
- Ask one question at a time.

What to collect first (common and practical):
- Name and age
- Weight (if known)
- How they are feeling and any symptoms
- Blood pressure, temperature, heart rate, or blood glucose only if the user knows them

Rules:
- Always call log_vital when the user shares a health reading.
- Always call set_user_profile when the user shares profile info (name, age, weight, height, address).
- Do not ask again for a vital that is already logged in this session unless the user is giving an updated reading.
- If the user does not know a metric, move on without pressure.

Doctor recommendation rules:
- Before calling find_nearby_doctors, ask for the user's address if no address is known.
- Prefer address-based recommendations over rough device location.
- If only rough location exists, tell the user it may be less accurate and ask for address confirmation.

Clinical reference ranges:
- Blood pressure: <120/80 normal, 120-139/80-89 elevated, >=140/90 high
- Heart rate: 60-100 bpm normal resting
- Temperature: 97-99 F (36.1-37.2 C) normal
- Blood glucose fasting: 70-99 mg/dL normal, 100-125 prediabetic, >=126 high
- Oxygen saturation: 95-100% normal, <90% concerning

If severe symptoms are mentioned (for example chest pain, shortness of breath, severe headache), encourage urgent medical care immediately."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "log_vital",
            "description": "Log a health reading into the database whenever a user shares one.",
            "parameters": {
                "type": "object",
                "properties": {
                    "vital_type": {
                        "type": "string",
                        "enum": [
                            "blood_pressure",
                            "heart_rate",
                            "temperature",
                            "blood_glucose",
                            "oxygen_saturation",
                            "weight",
                            "height",
                            "bmi",
                            "symptom",
                            "other",
                        ],
                        "description": "Type of metric being recorded",
                    },
                    "value": {
                        "type": "string",
                        "description": "Value of the metric, for example '122/78', '95', or '72'",
                    },
                    "unit": {
                        "type": "string",
                        "description": "Unit for the value, for example 'mmHg', 'bpm', 'kg', 'cm', '%'",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Optional context, for example 'after exercise'",
                    },
                },
                "required": ["vital_type", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_user_profile",
            "description": "Save user profile details such as name, age, weight, height, and address.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "User's name"},
                    "age": {"type": "integer", "description": "User age in years"},
                    "weight_kg": {"type": "number", "description": "User weight in kilograms"},
                    "height_cm": {"type": "number", "description": "User height in centimeters"},
                    "address": {
                        "type": "string",
                        "description": "User address or area for doctor recommendations",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_nearby_doctors",
            "description": "Find nearby doctors or clinics. Use address when possible for better accuracy.",
            "parameters": {
                "type": "object",
                "properties": {
                    "latitude": {
                        "type": "number",
                        "description": "Optional latitude coordinate",
                    },
                    "longitude": {
                        "type": "number",
                        "description": "Optional longitude coordinate",
                    },
                    "address": {
                        "type": "string",
                        "description": "Preferred address to search near",
                    },
                    "specialty": {
                        "type": "string",
                        "description": "Doctor specialty, for example 'general practitioner'",
                    },
                    "radius_km": {
                        "type": "number",
                        "description": "Search radius in km (default 5)",
                    },
                },
                "required": [],
            },
        },
    },
]


class ConversationSession:
    def __init__(self):
        self.messages: list[dict] = []
        self.user_id: int | None = None
        self.user_name: str | None = None
        self.session_id: int = database.create_session()
        self.location: dict | None = None
        self.address_location: dict | None = None
        self.events: list[dict] = []
        self.logged_vital_types: set[str] = set()
        self.profile: dict[str, Any] = {
            "name": None,
            "age": None,
            "weight_kg": None,
            "height_cm": None,
            "address": None,
        }
        self.symptoms: set[str] = set()
        self.doctors_shared: bool = False
        self.prompted_missing_fields: set[str] = set()

    def _build_system_prompt(self) -> str:
        profile_lines = []
        for key in ["name", "age", "weight_kg", "height_cm", "address"]:
            value = self.profile.get(key)
            profile_lines.append(f"- {key}: {value if value is not None else 'unknown'}")

        if self.location:
            profile_lines.append(
                f"- device_location: {self.location.get('latitude')}, {self.location.get('longitude')}"
            )
        else:
            profile_lines.append("- device_location: unknown")

        if self.address_location:
            profile_lines.append(
                f"- address_location: {self.address_location.get('latitude')}, {self.address_location.get('longitude')}"
            )
        else:
            profile_lines.append("- address_location: unknown")

        logged = ", ".join(sorted(self.logged_vital_types)) if self.logged_vital_types else "none"

        context = "\n".join(profile_lines)
        return (
            f"{SYSTEM_PROMPT_BASE}\n\n"
            "Session state (must respect):\n"
            f"{context}\n"
            f"- vitals_logged_this_session: {logged}\n"
            "If a vital appears in vitals_logged_this_session, do not ask for it again unless user gives an update."
        )

    def set_location(self, latitude: float, longitude: float):
        self.location = {"latitude": latitude, "longitude": longitude}

    async def set_address(self, address: str) -> dict:
        self.profile["address"] = address
        geocoded = await geocode_address(address)
        if geocoded:
            self.address_location = {
                "latitude": geocoded["latitude"],
                "longitude": geocoded["longitude"],
            }
            self.profile["address"] = geocoded.get("formatted_address", address)
            return {"status": "ok", "address": self.profile["address"], "location": self.address_location}
        return {"status": "unresolved", "address": address}

    async def start(self) -> tuple[str, str | None]:
        greeting = (
            "Hello! I'm Aria from CuraConnect, your personal health assistant. "
            "Could you tell me your name and age to get started?"
        )
        audio = await synthesize_speech(greeting)
        return greeting, audio

    async def process_message(self, user_text: str, location: dict | None = None) -> dict:
        if location:
            self.location = location

        self.events = []
        self.messages.append({"role": "user", "content": user_text})

        response_text = await self._run_agent_loop()
        audio = await synthesize_speech(response_text)

        self.messages.append({"role": "assistant", "content": response_text})

        return {
            "text": response_text,
            "audio": audio,
            "events": self.events,
        }

    async def _run_agent_loop(self) -> str:
        user_text = self._latest_user_text()
        return await self._run_scripted_assistant(user_text)

    async def _run_scripted_assistant(self, user_text: str) -> str:
        entities = self._extract_entities_from_user_text(user_text)
        stop_requested = bool(entities.get("stop_requested"))
        blood_pressure_mentioned = bool(entities.get("blood_pressure_mentioned"))
        heart_rate_mentioned = bool(entities.get("heart_rate_mentioned"))
        updates: dict[str, Any] = {
            "profile_changed": {},
            "symptoms_added": [],
            "vitals_logged": [],
            "address_status": None,
            "blood_pressure": None,
            "heart_rate": None,
            "existing_details": [],
        }

        profile_inputs: dict[str, Any] = {}
        if entities.get("name"):
            profile_inputs["name"] = entities["name"]
        if entities.get("age") is not None:
            profile_inputs["age"] = entities["age"]
        if entities.get("weight_kg") is not None:
            profile_inputs["weight_kg"] = entities["weight_kg"]
        if entities.get("height_cm") is not None:
            profile_inputs["height_cm"] = entities["height_cm"]

        has_structured_data = bool(profile_inputs) or bool(entities.get("address")) or bool(entities.get("symptoms"))

        if profile_inputs:
            profile_result = await self._tool_set_user_profile(profile_inputs)
            if isinstance(profile_result, dict):
                updates["profile_changed"] = profile_result.get("changed") or {}

        changed = updates["profile_changed"]
        for key in ("weight_kg", "height_cm", "address"):
            if key in changed and key in self.prompted_missing_fields:
                self.prompted_missing_fields.remove(key)

        if entities.get("name") and self.profile.get("name") == entities["name"] and "name" not in changed:
            updates["existing_details"].append(f"I already have your name as {self.profile.get('name')}.")
        if entities.get("age") is not None and self.profile.get("age") == entities["age"] and "age" not in changed:
            updates["existing_details"].append(f"I already have your age as {self.profile.get('age')}.")
        if entities.get("weight_kg") is not None and "weight_kg" not in changed:
            current_weight = self.profile.get("weight_kg")
            if isinstance(current_weight, (int, float)) and isclose(
                float(current_weight), float(entities["weight_kg"]), rel_tol=0.0, abs_tol=0.01
            ):
                updates["existing_details"].append(f"I already have your weight as {float(current_weight):g} kg.")
        if entities.get("height_cm") is not None and "height_cm" not in changed:
            current_height = self.profile.get("height_cm")
            if isinstance(current_height, (int, float)) and isclose(
                float(current_height), float(entities["height_cm"]), rel_tol=0.0, abs_tol=0.1
            ):
                updates["existing_details"].append(f"I already have your height as {float(current_height):g} cm.")

        address = entities.get("address")
        if address:
            address_result = await self.set_address(address)
            updates["address_status"] = address_result.get("status")
            updates["profile_changed"]["address"] = self.profile.get("address")
            if updates["address_status"] == "ok" and "address" in self.prompted_missing_fields:
                self.prompted_missing_fields.remove("address")

        for symptom in entities.get("symptoms", []):
            if symptom in self.symptoms:
                continue
            self.symptoms.add(symptom)
            updates["symptoms_added"].append(symptom)
            vital_result = await self._tool_log_vital(
                {
                    "vital_type": "symptom",
                    "value": symptom,
                    "notes": "User-reported symptom",
                }
            )
            updates["vitals_logged"].append(vital_result)

        if entities.get("symptoms") and not updates["symptoms_added"]:
            known = ", ".join(sorted(self.symptoms))
            if known:
                updates["existing_details"].append(f"I still have your symptoms noted: {known}.")

        blood_pressure = entities.get("blood_pressure")
        if blood_pressure:
            await self._tool_log_vital(
                {
                    "vital_type": "blood_pressure",
                    "value": blood_pressure,
                    "unit": "mmHg",
                }
            )
            updates["vitals_logged"].append({"vital_type": "blood_pressure", "value": blood_pressure})
            updates["blood_pressure"] = blood_pressure
            has_structured_data = True

        heart_rate = entities.get("heart_rate")
        if heart_rate is not None:
            await self._tool_log_vital(
                {
                    "vital_type": "heart_rate",
                    "value": str(heart_rate),
                    "unit": "bpm",
                }
            )
            updates["vitals_logged"].append({"vital_type": "heart_rate", "value": str(heart_rate)})
            updates["heart_rate"] = heart_rate
            has_structured_data = True

        clarifications: list[str] = []
        if blood_pressure_mentioned and not blood_pressure:
            clarifications.append(
                "I could not read that blood pressure clearly. Please share it as two numbers, like 122 over 78."
            )
        if heart_rate_mentioned and heart_rate is None:
            clarifications.append(
                "I could not read the heart rate clearly. Please share one number in bpm, like 72."
            )

        if stop_requested and not has_structured_data and not clarifications:
            return "No problem. You are all set for now. If anything changes, I am here to help."

        doctors_requested = bool(entities.get("doctor_request"))
        auto_doctor_from_new_address = (
            bool(address)
            and updates.get("address_status") == "ok"
            and bool(self.symptoms)
            and not self.doctors_shared
        )
        should_find_doctors = doctors_requested or auto_doctor_from_new_address
        doctor_result = None
        if should_find_doctors and (self.profile.get("address") or self.address_location or self.location):
            doctor_input: dict[str, Any] = {}
            if self.profile.get("address"):
                doctor_input["address"] = self.profile["address"]
            specialties = self._recommended_specialties()
            if specialties:
                doctor_input["specialties"] = specialties
                doctor_input["specialty"] = specialties[0]
            doctor_result = await self._tool_find_doctors(doctor_input)
            if isinstance(doctor_result, dict) and doctor_result.get("status") == "found":
                self.doctors_shared = True

        response_parts: list[str] = []
        response_parts.extend(self._build_acknowledgements(updates))

        if entities.get("severe_flag"):
            response_parts.append(
                "Because you mentioned breathing trouble, please seek urgent care immediately if it worsens."
            )

        if clarifications:
            response_parts.extend(clarifications)
            merged = " ".join(part.strip() for part in response_parts if part and part.strip())
            return merged or clarifications[0]

        if isinstance(doctor_result, dict):
            if doctor_result.get("status") == "found":
                specialties = doctor_result.get("specialties") or []
                if specialties:
                    readable = self._format_specialty_list(specialties)
                    response_parts.append(
                        f"I found {doctor_result.get('count', 0)} {readable} options and shared them on your screen."
                    )
                else:
                    response_parts.append(
                        f"I found {doctor_result.get('count', 0)} relevant doctors and shared them on your screen."
                    )
            elif doctor_result.get("status") == "no_results":
                response_parts.append(
                    "I could not find enough symptom-matched doctors from that search yet, but I can refine it."
                )
            elif doctor_result.get("status") == "error":
                response_parts.append("Please share your full address so I can recommend nearby doctors accurately.")
        elif should_find_doctors and not self.profile.get("address"):
            response_parts.append("Please share your full address so I can find nearby doctors for you.")

        if stop_requested:
            response_parts.append("No problem. You are all set for now. If anything changes, I am here to help.")
            merged = " ".join(part.strip() for part in response_parts if part and part.strip())
            return merged

        follow_up = self._next_scripted_question()
        if follow_up:
            response_parts.append(follow_up)

        merged = " ".join(part.strip() for part in response_parts if part and part.strip())
        return merged or "I am here whenever you want to update any health detail."

    def _build_acknowledgements(self, updates: dict[str, Any]) -> list[str]:
        parts: list[str] = []
        changed = updates.get("profile_changed") or {}

        if "name" in changed and "age" in changed:
            parts.append(f"Nice to meet you, {self.profile.get('name')}. I saved your age as {self.profile.get('age')}.")
        elif "name" in changed:
            parts.append(f"Nice to meet you, {self.profile.get('name')}.")
        elif "age" in changed:
            parts.append(f"I saved your age as {self.profile.get('age')}.")

        if "weight_kg" in changed:
            parts.append(f"I logged your weight as {changed['weight_kg']:g} kg.")
        if "height_cm" in changed:
            parts.append(f"I noted your height as {changed['height_cm']:g} cm.")
        if updates.get("blood_pressure"):
            parts.append(f"I logged your blood pressure as {updates['blood_pressure']} mmHg.")
        if updates.get("heart_rate") is not None:
            parts.append(f"I logged your heart rate as {updates['heart_rate']} bpm.")

        symptoms_added = updates.get("symptoms_added") or []
        if symptoms_added:
            readable = ", ".join(symptoms_added)
            parts.append(f"I noted your symptoms: {readable}.")

        if updates.get("address_status") == "ok":
            parts.append("I saved your address.")
        elif updates.get("address_status") == "unresolved":
            parts.append("I could not fully confirm that address yet.")

        parts.extend(updates.get("existing_details") or [])

        return parts

    def _next_scripted_question(self) -> str:
        if not self.profile.get("name"):
            return "What name should I use for you?"
        if self.profile.get("age") is None:
            return f"Thanks {self.profile.get('name')}. What is your age?"
        if not self.symptoms:
            return "How are you feeling today, and do you have any symptoms?"
        if self.profile.get("weight_kg") is None:
            if "weight_kg" not in self.prompted_missing_fields:
                self.prompted_missing_fields.add("weight_kg")
                return "Do you know your current weight in kilograms?"
            return ""
        if self.profile.get("height_cm") is None:
            if "height_cm" not in self.prompted_missing_fields:
                self.prompted_missing_fields.add("height_cm")
                return "If you know it, what is your height in centimeters?"
            return ""
        if not self.profile.get("address"):
            if "address" not in self.prompted_missing_fields:
                self.prompted_missing_fields.add("address")
                return "Can you share your address so I can recommend nearby doctors if needed?"
            return ""
        if self.symptoms and not self.doctors_shared:
            specialties = self._recommended_specialties()
            if specialties:
                readable = self._format_specialty_list(specialties)
                return f"If you want, I can find {readable} based on your saved address."
            return "If you want, I can find doctors based on your saved address."
        if not any(vital in self.logged_vital_types for vital in {"blood_pressure", "heart_rate", "temperature"}):
            return "If you know a reading like blood pressure, heart rate, or temperature, I can log it."
        return "I have your key details saved. If anything changes, just tell me and I will update it."

    def _recommended_specialties(self) -> list[str]:
        specialties: list[str] = []
        allergy_symptoms = {"allergies", "hives", "rash", "itching", "sneezing"}

        if any(symptom in self.symptoms for symptom in allergy_symptoms):
            specialties.append("allergist")
            specialties.append("general physician")
        elif self.symptoms:
            specialties.append("general physician")

        deduped: list[str] = []
        for specialty in specialties:
            if specialty not in deduped:
                deduped.append(specialty)
        return deduped

    def _format_specialty_list(self, specialties: list[str]) -> str:
        if not specialties:
            return "doctor"
        if len(specialties) == 1:
            return specialties[0]
        if len(specialties) == 2:
            return f"{specialties[0]} and {specialties[1]}"
        return ", ".join(specialties[:-1]) + f", and {specialties[-1]}"

    def _extract_entities_from_user_text(self, text: str) -> dict[str, Any]:
        return {
            "name": self._extract_name(text),
            "age": self._extract_age(text),
            "weight_kg": self._extract_weight_kg(text),
            "height_cm": self._extract_height_cm(text),
            "address": self._extract_address(text),
            "symptoms": self._extract_symptoms(text),
            "blood_pressure": self._extract_blood_pressure(text),
            "heart_rate": self._extract_heart_rate(text),
            "blood_pressure_mentioned": self._mentions_blood_pressure(text),
            "heart_rate_mentioned": self._mentions_heart_rate(text),
            "doctor_request": self._is_doctor_request(text),
            "severe_flag": self._mentions_severe_symptom(text),
            "stop_requested": self._is_stop_request(text),
        }

    def _extract_name(self, text: str) -> str | None:
        patterns = [
            r"\bmy name is\s+([a-zA-Z][a-zA-Z' -]{0,40})",
            r"\bthis is\s+([a-zA-Z][a-zA-Z' -]{0,40})",
        ]
        stop_words = {"and", "age", "is", "i", "im", "i'm", "my"}

        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            candidate = match.group(1).strip()
            candidate = re.split(r"\b(?:and|age|i am|i'm|im)\b", candidate, maxsplit=1, flags=re.IGNORECASE)[0]
            words = [w for w in re.findall(r"[a-zA-Z][a-zA-Z'-]*", candidate) if w.lower() not in stop_words]
            if words:
                return " ".join(words[:2]).title()
        return None

    def _extract_age(self, text: str) -> int | None:
        patterns = [
            r"\bage(?:\s*(?:is|=|:|around|about|would be|would be something around))?\s*(\d{1,3})\b",
            r"\bi am\s+(\d{1,3})\b",
            r"\bi'?m\s+(\d{1,3})\b",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            age = int(match.group(1))
            if 1 <= age <= 120:
                return age
        return None

    def _extract_weight_kg(self, text: str) -> float | None:
        patterns = [
            r"\bweight(?:\s*(?:is|=|:|around|about|would be|would be something around))?\s*(\d{2,3}(?:\.\d+)?)\s*(?:kg|kgs|kilograms?)?\b",
            r"\b(\d{2,3}(?:\.\d+)?)\s*(?:kg|kgs|kilograms?)\b",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            value = float(match.group(1))
            if 20 <= value <= 350:
                return value
        return None

    def _extract_height_cm(self, text: str) -> float | None:
        patterns = [
            r"\bheight(?:\s*(?:is|=|:|around|about|would be|would be something around))?\s*(\d{2,3}(?:\.\d+)?)\s*(?:cm|centimeters?)?\b",
            r"\b(\d{2,3}(?:\.\d+)?)\s*(?:cm|centimeters?)\b",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            value = float(match.group(1))
            if 80 <= value <= 260:
                return value
        return None

    def _extract_address(self, text: str) -> str | None:
        patterns = [
            r"\bmy address(?:\s+is|\s+would be|:)?\s+(.+)",
            r"\baddress(?:\s+is|:)?\s+(.+)",
            r"\bit(?:'s| is)\s+(.+)",
            r"^\s*is\s+(.+)",
        ]

        candidates: list[str] = []
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                candidates.append(match.group(1).strip())

        if not candidates and self._looks_like_address(text):
            candidates.append(text.strip())

        for candidate in candidates:
            normalized = self._normalize_address_candidate(candidate)
            if self._looks_like_address(normalized):
                return normalized
        return None

    def _normalize_address_candidate(self, address: str) -> str:
        cleaned = re.sub(
            r"^(?:my address(?: is| would be)?|address(?: is)?|it is|it's|is)\s+",
            "",
            address.strip(),
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(r"^\s*one\s+(0[0-9]{2,4})\b", lambda m: f"1{m.group(1)}", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,\n\t")
        return cleaned

    def _looks_like_address(self, text: str) -> bool:
        lowered = text.lower()
        has_number = bool(re.search(r"\d{2,6}", lowered)) or bool(
            re.search(r"\b(one|two|three|four|five|six|seven|eight|nine)\b", lowered)
        )
        hints = (
            "street",
            "st",
            "road",
            "rd",
            "drive",
            "dr",
            "avenue",
            "ave",
            "lane",
            "ln",
            "boulevard",
            "blvd",
            "way",
            "court",
            "ct",
            "tempe",
            "arizona",
            "az",
            "phoenix",
            "mesa",
            "scottsdale",
        )
        has_hint = any(re.search(rf"\b{re.escape(hint)}\b", lowered) for hint in hints)
        return has_number and has_hint

    def _extract_symptoms(self, text: str) -> list[str]:
        lowered = text.lower()
        symptom_aliases = {
            "allergy": "allergies",
            "allergies": "allergies",
            "hives": "hives",
            "rash": "rash",
            "itching": "itching",
            "sneezing": "sneezing",
            "cough": "cough",
            "fever": "fever",
            "headache": "headache",
            "breathlessness": "breathlessness",
            "breathless": "breathlessness",
            "shortness of breath": "breathlessness",
            "breathing difficulty": "breathlessness",
        }
        detected: list[str] = []
        for key, canonical in symptom_aliases.items():
            if key in lowered and canonical not in detected:
                detected.append(canonical)
        return detected

    def _extract_blood_pressure(self, text: str) -> str | None:
        normalized = text.lower()
        normalized = re.sub(r"\b(\d)\s*[:]\s*(\d{2})\b", r"\1\2", normalized)
        normalized = re.sub(r"\b(\d{2})\s*[:]\s*(\d)\b", r"\1\2", normalized)
        normalized = re.sub(r"\bover\b", "/", normalized)
        normalized = re.sub(r"\bby\b", "/", normalized)

        match = re.search(r"\b(\d{2,3})\s*/\s*(\d{2,3})\b", normalized, flags=re.IGNORECASE)
        if match:
            systolic = int(match.group(1))
            diastolic = int(match.group(2))
            if 60 <= systolic <= 250 and 40 <= diastolic <= 150:
                return f"{systolic}/{diastolic}"

        if self._mentions_blood_pressure(normalized):
            after_bp = normalized.split("blood pressure", 1)[1] if "blood pressure" in normalized else normalized
            numbers = re.findall(r"\d{2,3}", after_bp)
            if len(numbers) >= 2:
                systolic = int(numbers[0])
                diastolic = int(numbers[1])
                if 60 <= systolic <= 250 and 40 <= diastolic <= 150:
                    return f"{systolic}/{diastolic}"
        return None

    def _extract_heart_rate(self, text: str) -> int | None:
        lowered = text.lower()
        segment = lowered

        focused = re.search(r"\b(?:heart rate|pulse)\b(.*)", lowered, flags=re.IGNORECASE)
        if focused:
            segment = focused.group(1)
        elif "bpm" not in lowered:
            return None

        numbers = re.findall(r"\d{2,4}", segment)
        for number in numbers:
            value = int(number)
            if 35 <= value <= 220:
                return value

            if len(number) == 4:
                first = int(number[:2])
                second = int(number[2:])
                if 35 <= first <= 220 and 35 <= second <= 220:
                    return round((first + second) / 2)

        return None

    def _mentions_blood_pressure(self, text: str) -> bool:
        lowered = text.lower()
        return "blood pressure" in lowered or bool(re.search(r"\bbp\b", lowered))

    def _mentions_heart_rate(self, text: str) -> bool:
        lowered = text.lower()
        return "heart rate" in lowered or "pulse" in lowered

    def _is_stop_request(self, text: str) -> bool:
        lowered = text.lower()
        stop_patterns = (
            r"\bno thanks\b",
            r"\bno thank you\b",
            r"\bthat'?s all\b",
            r"\bnothing else\b",
            r"\bi'?m done\b",
            r"\bim done\b",
            r"\bno more\b",
        )
        return any(re.search(pattern, lowered) for pattern in stop_patterns)

    def _is_doctor_request(self, text: str) -> bool:
        lowered = text.lower()
        keywords = ("doctor", "doctors", "clinic", "nearby", "recommend", "hospital", "appointment")
        return any(keyword in lowered for keyword in keywords)

    def _mentions_severe_symptom(self, text: str) -> bool:
        lowered = text.lower()
        severe_terms = ("chest pain", "shortness of breath", "breathlessness", "difficulty breathing")
        return any(term in lowered for term in severe_terms)

    async def _execute_tool(self, tool_name: str, tool_input: dict) -> Any:
        if tool_name == "log_vital":
            return await self._tool_log_vital(tool_input)
        if tool_name == "set_user_profile":
            return await self._tool_set_user_profile(tool_input)
        if tool_name == "find_nearby_doctors":
            return await self._tool_find_doctors(tool_input)
        return {"error": f"Unknown tool: {tool_name}"}

    def _parse_dict(self, raw: str) -> dict | None:
        raw = raw.strip()
        if not raw:
            return None

        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        try:
            parsed = ast.literal_eval(raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return None

        return None

    def _extract_failed_tool_call(self, exc: Exception) -> tuple[str, dict] | None:
        text = str(exc)
        candidates = [text]

        try:
            candidates.append(bytes(text, "utf-8").decode("unicode_escape"))
        except Exception:
            pass

        for candidate in candidates:
            _, calls = self._extract_inline_tool_calls(candidate)
            if calls:
                return calls[0]
        return None

    def _is_rate_limit_error(self, exc: Exception) -> bool:
        text = str(exc).lower()
        markers = (
            "rate_limit_exceeded",
            "rate limit",
            "tokens per day",
            "too many requests",
            "quota",
        )
        return any(marker in text for marker in markers)

    def _latest_user_text(self) -> str:
        for message in reversed(self.messages):
            if message.get("role") == "user":
                return str(message.get("content") or "")
        return ""

    def _address_looks_present_in_latest_user_message(self, address: str) -> bool:
        latest_user_text = self._latest_user_text().lower()
        if not latest_user_text:
            return False

        candidate = address.lower().strip()
        if not candidate:
            return False

        if candidate in latest_user_text:
            return True

        candidate_tokens = {
            token
            for token in re.findall(r"[a-z0-9]+", candidate)
            if len(token) >= 2 or token.isdigit()
        }
        latest_tokens = {
            token
            for token in re.findall(r"[a-z0-9]+", latest_user_text)
            if len(token) >= 2 or token.isdigit()
        }

        if not candidate_tokens or not latest_tokens:
            return False

        overlap = len(candidate_tokens & latest_tokens)
        overlap_ratio = overlap / len(candidate_tokens)

        return overlap >= 3 and overlap_ratio >= 0.45

    def _extract_inline_tool_calls(self, content: str) -> tuple[str, list[tuple[str, dict]]]:
        calls: list[tuple[str, dict]] = []

        def _replace(match: re.Match) -> str:
            fn_name = match.group(1)
            payload = (match.group(2) or "").strip()

            # Handle variants such as:
            # <function=tool({...})</function>
            # <function=tool[]{...}</function>
            # <function=tool{...}</function>
            # <function=tool={...}</function>
            if payload.startswith(">"):
                payload = payload[1:].strip()
            if payload.startswith("[]"):
                payload = payload[2:].strip()
            if payload.startswith("="):
                payload = payload[1:].strip()
            if payload.startswith("(") and payload.endswith(")"):
                payload = payload[1:-1].strip()

            fn_args = self._parse_dict(payload)
            if fn_args is not None:
                calls.append((fn_name, fn_args))
            return ""

        cleaned = INLINE_TOOL_CALL_RE.sub(_replace, content)
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
        return cleaned, calls

    def _tool_recovery_response(self, fn_name: str, fn_args: dict, tool_result: Any) -> str:
        if fn_name == "log_vital":
            vital_type = str(fn_args.get("vital_type", "vital")).replace("_", " ")
            value = str(fn_args.get("value", ""))
            unit = fn_args.get("unit")
            reading = f"{value} {unit}".strip() if unit else value
            return (
                f"Thanks, I logged your {vital_type} as {reading}. "
                "Would you like to share a common check-in item like age, weight, or symptoms next?"
            )

        if fn_name == "set_user_profile":
            changed = {}
            if isinstance(tool_result, dict):
                changed = tool_result.get("changed") or {}

            if changed.get("address_status") == "ok":
                return (
                    "Great, I saved your profile and address for accurate doctor recommendations. "
                    "What health reading or symptom would you like to share next?"
                )
            if changed.get("address_status") == "unresolved":
                return (
                    "I saved your profile details, but I could not confirm that address yet. "
                    "Please share it again with city and state so I can find nearby doctors accurately."
                )
            if changed.get("address_status") == "ignored_not_in_user_turn":
                profile_fields_saved = any(
                    key in changed for key in ("name", "age", "weight_kg", "height_cm")
                )
                if profile_fields_saved:
                    return (
                        "I saved your profile details from that message. "
                        "Please share your full address in your next message for doctor recommendations."
                    )
                return (
                    "I did not save an address from that step because I could not confirm it came from your message. "
                    "Please share your full address in your next message."
                )
            if changed:
                return (
                    "Thanks, I saved that profile information. "
                    "If you want doctor recommendations later, please share your address too."
                )
            return (
                "I did not catch a clear profile update from that message. "
                "Please repeat the detail you want me to save."
            )

        if fn_name == "find_nearby_doctors":
            if isinstance(tool_result, dict) and tool_result.get("status") == "found":
                count = tool_result.get("count", 0)
                return (
                    f"I found {count} nearby options and shared them on your screen. "
                    "Would you like help picking one?"
                )
            return (
                "I could not find nearby doctors from that attempt. "
                "Please share your address so I can search accurately."
            )

        return "I ran into a small processing issue, but we can continue."

    async def _tool_log_vital(self, inputs: dict) -> dict:
        vital_type = inputs["vital_type"]
        value = inputs["value"]
        unit = inputs.get("unit")
        notes = inputs.get("notes")

        if self.user_id is None:
            fallback_name = self.profile.get("name") or "Unknown"
            fallback_age = self.profile.get("age")
            user = database.get_or_create_user(str(fallback_name), fallback_age)
            self.user_id = user.id
            self.user_name = user.name
            database.update_session_user(self.session_id, self.user_id)

        vital = database.log_vital(self.user_id, vital_type, value, unit, notes)
        self.logged_vital_types.add(vital_type)

        if vital_type == "weight":
            try:
                self.profile["weight_kg"] = float(value)
            except (TypeError, ValueError):
                pass
        if vital_type == "height":
            try:
                self.profile["height_cm"] = float(value)
            except (TypeError, ValueError):
                pass

        self.events.append({"type": "vital_logged", "vital": vital})
        return {"status": "logged", "vital": vital}

    async def _tool_set_user_profile(self, inputs: dict) -> dict:
        changed: dict[str, Any] = {}

        name = inputs.get("name")
        age = inputs.get("age")
        weight_kg = inputs.get("weight_kg")
        height_cm = inputs.get("height_cm")
        address = inputs.get("address")

        if isinstance(name, str) and name.strip():
            clean_name = name.strip()
            if self.profile.get("name") != clean_name:
                self.profile["name"] = clean_name
                self.user_name = clean_name
                changed["name"] = clean_name

        if isinstance(age, int) and age > 0:
            if self.profile.get("age") != age:
                self.profile["age"] = age
                changed["age"] = age

        if isinstance(weight_kg, (int, float)) and weight_kg > 0:
            clean_weight = float(weight_kg)
            current_weight = self.profile.get("weight_kg")
            if not isinstance(current_weight, (int, float)) or not isclose(
                float(current_weight), clean_weight, rel_tol=0.0, abs_tol=0.01
            ):
                self.profile["weight_kg"] = clean_weight
                changed["weight_kg"] = clean_weight

        if isinstance(height_cm, (int, float)) and height_cm > 0:
            clean_height = float(height_cm)
            current_height = self.profile.get("height_cm")
            if not isinstance(current_height, (int, float)) or not isclose(
                float(current_height), clean_height, rel_tol=0.0, abs_tol=0.1
            ):
                self.profile["height_cm"] = clean_height
                changed["height_cm"] = clean_height

        if isinstance(address, str) and address.strip():
            clean_address = address.strip()
            if self._address_looks_present_in_latest_user_message(clean_address):
                address_result = await self.set_address(clean_address)
                changed["address"] = self.profile.get("address")
                changed["address_status"] = address_result.get("status")
            else:
                changed["address_status"] = "ignored_not_in_user_turn"

        if self.user_id is None and self.profile.get("name"):
            user = database.get_or_create_user(str(self.profile["name"]), self.profile.get("age"))
            self.user_id = user.id
            self.user_name = user.name
            database.update_session_user(self.session_id, self.user_id)
        elif self.user_id is not None and ("name" in changed or "age" in changed):
            database.update_user_profile(
                self.user_id,
                name=self.profile.get("name"),
                age=self.profile.get("age"),
            )

        if "weight_kg" in changed:
            await self._tool_log_vital(
                {
                    "vital_type": "weight",
                    "value": str(changed["weight_kg"]),
                    "unit": "kg",
                    "notes": "Captured from profile information",
                }
            )

        if "height_cm" in changed:
            await self._tool_log_vital(
                {
                    "vital_type": "height",
                    "value": str(changed["height_cm"]),
                    "unit": "cm",
                    "notes": "Captured from profile information",
                }
            )

        return {"status": "updated", "changed": changed}

    async def _tool_find_doctors(self, inputs: dict) -> dict:
        lat = inputs.get("latitude")
        lng = inputs.get("longitude")
        specialty = inputs.get("specialty")
        specialties_input = inputs.get("specialties")
        radius_km = inputs.get("radius_km", 5)
        address = inputs.get("address")
        location_source = None

        if isinstance(address, str) and address.strip():
            geocoded = await geocode_address(address.strip())
            if geocoded:
                lat = geocoded["latitude"]
                lng = geocoded["longitude"]
                self.address_location = {"latitude": lat, "longitude": lng}
                self.profile["address"] = geocoded.get("formatted_address", address.strip())
                location_source = "address"

        if (lat is None or lng is None) and self.address_location:
            lat = self.address_location.get("latitude")
            lng = self.address_location.get("longitude")
            location_source = location_source or "saved_address"

        if (lat is None or lng is None) and self.profile.get("address"):
            geocoded = await geocode_address(str(self.profile["address"]))
            if geocoded:
                lat = geocoded["latitude"]
                lng = geocoded["longitude"]
                self.address_location = {"latitude": lat, "longitude": lng}
                self.profile["address"] = geocoded.get("formatted_address", str(self.profile["address"]))
                location_source = location_source or "profile_address"

        if (lat is None or lng is None) and self.location:
            lat = self.location.get("latitude")
            lng = self.location.get("longitude")
            location_source = location_source or "device_location"

        if lat is None or lng is None:
            return {
                "status": "error",
                "message": "Address or reliable location is required to recommend nearby doctors.",
            }

        specialties: list[str] = []
        if isinstance(specialties_input, list):
            specialties = [str(item).strip() for item in specialties_input if str(item).strip()]
        if not specialties and isinstance(specialty, str) and specialty.strip():
            specialties = [specialty.strip()]
        if not specialties:
            specialties = [""]

        merged: list[dict] = []
        seen: set[str] = set()
        for current_specialty in specialties:
            query_specialty = current_specialty if current_specialty else None
            results = await find_nearby_doctors(
                float(lat),
                float(lng),
                query_specialty,
                int(float(radius_km) * 1000),
            )
            for doctor in results:
                key = str(doctor.get("place_id") or f"{doctor.get('name')}|{doctor.get('address')}")
                if key in seen:
                    continue
                seen.add(key)
                if query_specialty:
                    doctor["recommended_for"] = query_specialty
                merged.append(doctor)
                if len(merged) >= 8:
                    break
            if len(merged) >= 8:
                break

        doctors = merged
        self.events.append({"type": "doctors_found", "doctors": doctors})

        if not doctors:
            return {
                "status": "no_results",
                "message": "No doctors found nearby.",
                "source": location_source,
            }

        return {
            "status": "found",
            "count": len(doctors),
            "doctors": doctors,
            "source": location_source,
            "specialties": [s for s in specialties if s],
        }

    def identify_user(self, name: str, age: int | None = None):
        user = database.get_or_create_user(name, age)
        self.user_id = user.id
        self.user_name = user.name
        self.profile["name"] = user.name
        if age is not None:
            self.profile["age"] = age
        database.update_session_user(self.session_id, self.user_id)

    def end_session(self):
        database.close_session(self.session_id)
