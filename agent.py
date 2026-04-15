import ast
import asyncio
import json
import os
import re
from math import isclose
from typing import Any

from groq import Groq
from dotenv import load_dotenv

from database import db as database
from services.tts import synthesize_speech
from services.doctor_finder import find_nearby_doctors
from services.geocoding import geocode_address

load_dotenv()

client = Groq(api_key=os.getenv("GROQ_API_KEY", ""))
MODEL = "llama-3.3-70b-versatile"
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
        loop_messages = [{"role": "system", "content": self._build_system_prompt()}] + list(self.messages)

        while True:
            loop_messages[0]["content"] = self._build_system_prompt()

            try:
                response = await asyncio.to_thread(
                    client.chat.completions.create,
                    model=MODEL,
                    messages=loop_messages,
                    tools=TOOLS,
                    tool_choice="auto",
                    max_tokens=512,
                )
            except Exception as exc:
                recovered_tool = self._extract_failed_tool_call(exc)
                if recovered_tool:
                    fn_name, fn_args = recovered_tool
                    result = await self._execute_tool(fn_name, fn_args)
                    return self._tool_recovery_response(fn_name, fn_args, result)
                if self._is_rate_limit_error(exc):
                    return (
                        "I'm temporarily hitting a service limit right now. "
                        "Please retry in a minute, and we can continue from where we left off."
                    )
                print(f"Agent loop error: {exc}")
                return "I ran into a temporary issue processing that. Please try your last message again."

            message = response.choices[0].message

            if not message.tool_calls:
                content = message.content or ""
                cleaned_text, inline_calls = self._extract_inline_tool_calls(content)

                if inline_calls:
                    last_result = {}
                    for fn_name, fn_args in inline_calls:
                        last_result = await self._execute_tool(fn_name, fn_args)

                    if cleaned_text:
                        return cleaned_text

                    last_fn_name, last_fn_args = inline_calls[-1]
                    return self._tool_recovery_response(last_fn_name, last_fn_args, last_result)

                return content

            loop_messages.append(message)

            for tool_call in message.tool_calls:
                fn_name = tool_call.function.name
                fn_args = json.loads(tool_call.function.arguments)

                result = await self._execute_tool(fn_name, fn_args)

                loop_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(result),
                    }
                )

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

        doctors = await find_nearby_doctors(float(lat), float(lng), specialty, int(float(radius_km) * 1000))
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
