import random
import re
from datetime import datetime, timedelta, timezone
from math import isclose
from typing import Any

from dotenv import load_dotenv

from database import db as database
from services.tts import synthesize_speech
from services.doctor_finder import find_nearby_doctors
from services.geocoding import geocode_address

load_dotenv()


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
        self.logged_vital_values: dict[str, str] = {}
        self.profile: dict[str, Any] = {
            "name": None,
            "age": None,
            "weight_kg": None,
            "height_cm": None,
            "address": None,
        }
        self.symptoms: set[str] = set()
        self.symptom_status: str | None = None
        self.unknown_fields: set[str] = set()
        self.wants_doctor_suggestions: bool | None = None
        self.address_confirmed: bool = False
        self.doctors_shared: bool = False
        self.prompted_missing_fields: set[str] = set()
        self.authenticated: bool = False
        self.session_complete: bool = False
        self.conversation_tone: str = "neutral"
        self.timezone_name: str | None = None
        self.utc_offset_minutes: int | None = None

    def set_location(self, latitude: float, longitude: float):
        self.location = {"latitude": latitude, "longitude": longitude}

    def set_timezone_context(self, timezone_name: str | None = None, utc_offset_minutes: int | None = None):
        clean_timezone = None
        if isinstance(timezone_name, str):
            stripped = timezone_name.strip()
            if stripped:
                clean_timezone = stripped[:80]

        clean_offset = None
        if isinstance(utc_offset_minutes, int) and -840 <= utc_offset_minutes <= 840:
            clean_offset = utc_offset_minutes

        if clean_timezone is None and clean_offset is None:
            return

        if clean_timezone is not None:
            self.timezone_name = clean_timezone
        if clean_offset is not None:
            self.utc_offset_minutes = clean_offset

        database.update_session_timezone(
            self.session_id,
            timezone_name=self.timezone_name,
            utc_offset_minutes=self.utc_offset_minutes,
        )

    async def set_address(self, address: str) -> dict:
        self.profile["address"] = address
        geocoded = await geocode_address(address)
        if geocoded:
            self.address_location = {
                "latitude": geocoded["latitude"],
                "longitude": geocoded["longitude"],
            }
            self.profile["address"] = geocoded.get("formatted_address", address)
            self.address_confirmed = True
            return {"status": "ok", "address": self.profile["address"], "location": self.address_location}
        self.address_confirmed = False
        self.address_location = None
        return {"status": "unresolved", "address": address}

    async def start(self) -> tuple[str, str | None]:
        greeting = self._pick(
            "Hey there! I'm Aria from CuraConnect. Let's get you set up quickly. What is your name?",
            "Hi! I'm Aria, your health assistant at CuraConnect. What should I call you?",
            "Hello! I'm Aria from CuraConnect. I'll keep this quick. What is your name?",
        )
        audio = await synthesize_speech(greeting)
        return greeting, audio

    async def start_authenticated(self, name: str) -> tuple[str, str | None]:
        greeting = self._pick(
            f"Hey {name}, welcome back! How are you feeling today?",
            f"Hi {name}! Good to see you again. Any symptoms or updates to share?",
            f"Welcome back, {name}! What can I help you with today?",
        )
        audio = await synthesize_speech(greeting)
        return greeting, audio

    async def process_message(self, user_text: str, location: dict | None = None) -> dict:
        if location:
            self.location = location

        self.events = []
        self.messages.append({"role": "user", "content": user_text})
        database.log_conversation_message(
            session_id=self.session_id,
            user_id=self.user_id,
            role="user",
            content=user_text,
        )

        response_text = await self._run_agent_loop()
        audio = await synthesize_speech(response_text)

        self.messages.append({"role": "assistant", "content": response_text})
        database.log_conversation_message(
            session_id=self.session_id,
            user_id=self.user_id,
            role="assistant",
            content=response_text,
        )

        return {
            "text": response_text,
            "audio": audio,
            "events": self.events,
            "session_complete": self.session_complete,
        }

    async def _run_agent_loop(self) -> str:
        user_text = self._latest_user_text()
        return await self._run_scripted_assistant(user_text)

    def _is_conversational_closer(self, text: str) -> bool:
        lowered = text.lower().strip()
        return bool(re.match(
            r"^(?:sure|ok|okay|thanks|thank you|bye|goodbye|see you|take care|got it|cool|great|alright|sounds good|cheers)[\s!.]*$",
            lowered,
        ))

    def _is_gratitude_or_goodbye(self, text: str) -> bool:
        lowered = text.lower().strip()
        normalized = re.sub(r"[,;]+", " ", lowered)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return bool(re.match(
            r"^(?:(?:sure|ok|okay|alright|got it|sounds good|cool|great|cheers)\s+)*"
            r"(?:thanks?|thank you|thanks a lot|thank you so much|thx|bye|goodbye|see you|take care)"
            r"(?:\s+(?:thanks?|thank you|thx|bye|goodbye|see you|take care))*[\s!.]*$",
            normalized,
        ))

    def _is_greeting_message(self, text: str) -> bool:
        lowered = text.lower().strip()
        normalized = re.sub(r"[,;]+", " ", lowered)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return bool(re.match(
            r"^(?:hi|hey|hello|hiya|yo|sup|what'?s up|good morning|good afternoon|good evening)\b",
            normalized,
        ))

    def _detect_conversation_tone(self, text: str) -> str:
        lowered = text.lower().strip()
        if not lowered:
            return "neutral"
        if self._is_greeting_message(lowered):
            return "warm"

        concerned_patterns = (
            r"\bi'?m worried\b",
            r"\bi am worried\b",
            r"\bi'?m anxious\b",
            r"\bi am anxious\b",
            r"\bi'?m scared\b",
            r"\bi am scared\b",
            r"\bpanic\b",
            r"\bgetting worse\b",
            r"\bvery bad\b",
            r"\bterrible\b",
        )
        if any(re.search(pattern, lowered) for pattern in concerned_patterns):
            return "concerned"

        casual_markers = ("bro", "buddy", "dude", "lol", "haha")
        if any(marker in lowered for marker in casual_markers):
            return "casual"

        return "neutral"

    def _tone_preface_for_follow_up(self, tone: str) -> str | None:
        name = self.profile.get("name")
        if tone == "warm":
            if name:
                return self._pick(
                    f"Hi {name}, it is good to hear from you.",
                    f"Hey {name}, glad you checked in.",
                )
            return self._pick(
                "Hi there, it is good to hear from you.",
                "Hey, glad you checked in.",
            )
        if tone == "concerned":
            return self._pick(
                "I hear you. We can go step by step.",
                "I am with you. Let us take this one step at a time.",
            )
        if tone == "casual":
            if name:
                return self._pick(
                    f"Hey {name}, thanks for checking in.",
                    f"Good to have you here, {name}.",
                )
            return self._pick(
                "Hey, thanks for checking in.",
                "Good to have you here.",
            )
        return None

    async def _run_scripted_assistant(self, user_text: str) -> str:
        # If this is the first message and it's just a conversational closer, reply briefly
        if len(self.messages) <= 1 and self._is_conversational_closer(user_text):
            self.session_complete = True
            name = self.profile.get("name") or ""
            if name:
                return self._pick(
                    f"Hey {name}! Let me know whenever you want to log vitals or check in.",
                    f"Hi {name}! I am here whenever you need me.",
                )
            return self._pick(
                "Hey! Let me know whenever you want to log vitals or check in.",
                "Hi! I am here whenever you need me.",
            )

        if self._is_gratitude_or_goodbye(user_text):
            has_session_context = bool(
                self.symptoms
                or self.logged_vital_types
                or self.profile.get("name")
                or self.profile.get("age") is not None
            )
            if self.session_complete or has_session_context:
                self.session_complete = True
                name = self.profile.get("name")
                if name:
                    return self._pick(
                        f"You are welcome, {name}. Take care, and I am here whenever you need me.",
                        f"Anytime, {name}. Glad to help. Take care!",
                    )
                return self._pick(
                    "You are welcome. Take care, and I am here whenever you need me.",
                    "Anytime. Glad to help. Reach out whenever you need.",
                )

        entities = self._extract_entities_from_user_text(user_text)
        detected_tone = self._detect_conversation_tone(user_text)
        self.conversation_tone = detected_tone
        stop_requested = bool(entities.get("stop_requested"))
        skip_response = bool(entities.get("skip_response"))
        blood_pressure_mentioned = bool(entities.get("blood_pressure_mentioned"))
        heart_rate_mentioned = bool(entities.get("heart_rate_mentioned"))

        # "nope"/"no"/"skip" with no data — mark last prompted vital as done and move on
        has_any_data = (entities.get("blood_pressure") or entities.get("heart_rate") is not None
                        or entities.get("symptoms") or entities.get("name") or entities.get("age") is not None)
        if skip_response and not has_any_data:
            vital_prompts = ("blood_pressure", "heart_rate", "temperature", "oxygen_saturation")
            for vp in vital_prompts:
                if vp in self.prompted_missing_fields and vp not in self.logged_vital_types:
                    self.logged_vital_types.add(vp)
                    break
            follow_up = self._next_scripted_question()
            return follow_up or self._pick(
                "No problem! Anything else you want to share?",
                "Got it. Let me know if there is anything else.",
            )
        recorded_at = entities.get("recorded_at")
        time_label = entities.get("time_label")
        updates: dict[str, Any] = {
            "profile_changed": {},
            "symptoms_added": [],
            "vitals_logged": [],
            "blood_pressure": None,
            "heart_rate": None,
            "existing_details": [],
            "symptom_status": None,
            "doctor_preference": None,
            "time_label": time_label,
        }

        # Profile updates (name/age only — weight, height, address are in the profile UI)
        profile_inputs: dict[str, Any] = {}
        if entities.get("name"):
            profile_inputs["name"] = entities["name"]
        if entities.get("age") is not None:
            profile_inputs["age"] = entities["age"]

        has_structured_data = bool(profile_inputs) or bool(entities.get("symptoms"))

        if entities.get("doctor_request"):
            self.wants_doctor_suggestions = True
            updates["doctor_preference"] = True
        elif entities.get("doctor_decline"):
            self.wants_doctor_suggestions = False
            updates["doctor_preference"] = False
        elif "doctor_preference" in self.prompted_missing_fields and self.wants_doctor_suggestions is None:
            if entities.get("affirmative"):
                self.wants_doctor_suggestions = True
                updates["doctor_preference"] = True
            elif entities.get("negative"):
                self.wants_doctor_suggestions = False
                updates["doctor_preference"] = False

        if profile_inputs:
            profile_result = await self._tool_set_user_profile(profile_inputs)
            if isinstance(profile_result, dict):
                updates["profile_changed"] = profile_result.get("changed") or {}

        if entities.get("symptoms"):
            if self.symptom_status != "present":
                updates["symptom_status"] = "present"
            self.symptom_status = "present"
        elif entities.get("no_symptoms"):
            if self.symptom_status != "none":
                updates["symptom_status"] = "none"
            self.symptom_status = "none"

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
                },
                recorded_at=recorded_at,
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
                },
                recorded_at=recorded_at,
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
                },
                recorded_at=recorded_at,
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

        # "no thanks" at a doctor prompt should decline doctors, not end session
        if stop_requested and "doctor_preference" in self.prompted_missing_fields and self.wants_doctor_suggestions is None:
            self.wants_doctor_suggestions = False
            updates["doctor_preference"] = False
            stop_requested = False

        if stop_requested and not has_structured_data and not clarifications:
            return "No problem. You are all set for now. If anything changes, I am here to help."

        doctors_requested = bool(entities.get("doctor_request")) or (
            self.wants_doctor_suggestions is True and not self.doctors_shared
        )
        should_find_doctors = doctors_requested and (
            self.wants_doctor_suggestions is not False
        )
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
            response_parts.append(self._pick(
                "That sounds serious. If it gets worse, please call 911 or your local emergency line immediately.",
                "I want to flag that. If the symptoms worsen, do not hesitate to call 911 or go to the nearest ER.",
                "Please take this seriously. Call 911 right away if you experience worsening chest pain or difficulty breathing.",
            ))

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
            self.session_complete = True
            merged = " ".join(part.strip() for part in response_parts if part and part.strip())
            return merged

        # Severity assessment + remedy suggestions after new data is logged
        new_data_logged = bool(updates.get("symptoms_added")) or bool(updates.get("blood_pressure")) or updates.get("heart_rate") is not None
        if new_data_logged and (self.symptoms or self.logged_vital_types):
            severity = self._assess_severity()
            remedy = self._suggest_remedies(severity)
            if remedy:
                response_parts.append(remedy)

        follow_up = self._next_scripted_question()
        if follow_up:
            if not response_parts and not has_structured_data and not stop_requested and not clarifications:
                tone_preface = self._tone_preface_for_follow_up(detected_tone)
                if tone_preface:
                    response_parts.append(tone_preface)
            response_parts.append(follow_up)

        merged = " ".join(part.strip() for part in response_parts if part and part.strip())
        return merged or "I am here whenever you want to update any health detail."

    @staticmethod
    def _pick(*options: str) -> str:
        return random.choice(options)

    def _build_acknowledgements(self, updates: dict[str, Any]) -> list[str]:
        parts: list[str] = []
        changed = updates.get("profile_changed") or {}
        name = self.profile.get("name")

        if "name" in changed and "age" in changed:
            parts.append(self._pick(
                f"Great to meet you, {name}! Got your age down as {self.profile.get('age')}.",
                f"Hi {name}! Noted, you are {self.profile.get('age')} years old.",
                f"Thanks, {name}. Age {self.profile.get('age')}, got it.",
            ))
        elif "name" in changed:
            parts.append(self._pick(
                f"Hi {name}, great to have you here.",
                f"Got it, {name}!",
                f"Thanks, {name}.",
            ))
        elif "age" in changed:
            parts.append(self._pick(
                f"Got it, age {self.profile.get('age')}.",
                f"Noted, {self.profile.get('age')} years old.",
            ))

        time_tag = ""
        tl = updates.get("time_label")
        if tl and tl != "just now":
            time_tag = f" from {tl}"

        if updates.get("blood_pressure"):
            parts.append(f"Blood pressure {updates['blood_pressure']} mmHg{time_tag}, noted.")
        if updates.get("heart_rate") is not None:
            parts.append(f"Heart rate {updates['heart_rate']} bpm{time_tag}, got it.")

        symptoms_added = updates.get("symptoms_added") or []
        if symptoms_added:
            readable = ", ".join(symptoms_added)
            if len(symptoms_added) == 1:
                parts.append(self._pick(
                    f"Sorry to hear about the {readable}.",
                    f"Noted the {readable}.",
                    f"Got it, {readable}.",
                ))
            else:
                parts.append(self._pick(
                    f"Sorry to hear that. I have noted {readable}.",
                    f"Got it, noted {readable}.",
                ))
        elif updates.get("symptom_status") == "none":
            parts.append(self._pick(
                "Glad to hear you are doing well!",
                "Good to know you are feeling fine.",
                "Great, no symptoms noted.",
            ))

        if updates.get("doctor_preference") is False:
            parts.append(self._pick(
                "Sure, we will skip doctor suggestions.",
                "Got it, no doctor recommendations for now.",
            ))

        parts.extend(updates.get("existing_details") or [])

        return parts

    def _next_scripted_question(self) -> str:
        name = self.profile.get("name")

        if not name:
            return self._pick(
                "What name should I use for you?",
                "What is your name?",
                "First, what should I call you?",
            )

        if self.profile.get("age") is None:
            return self._pick(
                f"And how old are you, {name}?",
                f"What is your age, {name}?",
            )

        if self.symptom_status is None and not self.symptoms:
            if "symptoms" not in self.prompted_missing_fields:
                self.prompted_missing_fields.add("symptoms")
                return self._pick(
                    f"How are you feeling today, {name}?",
                    "What brings you in today? Any symptoms?",
                    "How have you been feeling lately?",
                )
            # User didn't report symptoms after being asked — treat as no symptoms
            self.symptom_status = "none"

        # Symptom-aware vital questions
        has_fever = "fever" in self.symptoms
        has_breathing = "breathlessness" in self.symptoms

        if has_fever and "temperature" not in self.logged_vital_types:
            if "temperature" not in self.prompted_missing_fields:
                self.prompted_missing_fields.add("temperature")
                return self._pick(
                    "Since you mentioned a fever, have you checked your temperature?",
                    "Do you know what your temperature is? That would help track the fever.",
                )

        if has_breathing and "oxygen_saturation" not in self.logged_vital_types:
            if "oxygen_saturation" not in self.prompted_missing_fields:
                self.prompted_missing_fields.add("oxygen_saturation")
                return self._pick(
                    "With the breathing trouble, do you have an oxygen reading? Like from a pulse oximeter?",
                    "If you have a pulse oximeter, your oxygen level would be helpful to log.",
                )

        if "blood_pressure" not in self.logged_vital_types:
            if "blood_pressure" not in self.prompted_missing_fields:
                self.prompted_missing_fields.add("blood_pressure")
                return self._pick(
                    "Have you checked your blood pressure recently?",
                    "Do you know your blood pressure? Something like 120 over 80.",
                )

        if "heart_rate" not in self.logged_vital_types:
            if "heart_rate" not in self.prompted_missing_fields:
                self.prompted_missing_fields.add("heart_rate")
                return self._pick(
                    "What about your heart rate or pulse?",
                    "Do you know your resting heart rate?",
                )

        if not has_fever and "temperature" not in self.logged_vital_types:
            if "temperature" not in self.prompted_missing_fields:
                self.prompted_missing_fields.add("temperature")
                return self._pick(
                    "Do you have a temperature reading to log?",
                    "Anything else? I can also note your temperature if you have it.",
                )

        self.session_complete = True
        return self._pick(
            "That covers everything for now. I am here if anything comes up!",
            "All set! Let me know any time you want to check in again.",
            "Looks like we are all caught up. Reach out whenever you need.",
        )

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

    def _assess_severity(self) -> str:
        """Classify current session data as 'mild', 'moderate', or 'serious'."""
        serious = False
        moderate_flags = 0

        bp = self.logged_vital_values.get("blood_pressure", "")
        if "/" in bp:
            try:
                sys_val, dia_val = int(bp.split("/")[0]), int(bp.split("/")[1])
                if sys_val >= 180 or dia_val >= 120:
                    serious = True
                elif sys_val >= 140 or dia_val >= 90:
                    moderate_flags += 1
            except (ValueError, IndexError):
                pass

        hr = self.logged_vital_values.get("heart_rate", "")
        if hr:
            try:
                hr_val = int(hr)
                if hr_val > 150 or hr_val < 40:
                    serious = True
                elif hr_val > 100 or hr_val < 50:
                    moderate_flags += 1
            except ValueError:
                pass

        temp = self.logged_vital_values.get("temperature", "")
        if temp:
            try:
                temp_val = float(temp)
                # Handle both Fahrenheit (>50) and Celsius (<50)
                if temp_val > 103 or (temp_val > 39.4 and temp_val < 50):
                    serious = True
                elif temp_val > 101 or (temp_val > 38.3 and temp_val < 50):
                    moderate_flags += 1
            except ValueError:
                pass

        spo2 = self.logged_vital_values.get("oxygen_saturation", "")
        if spo2:
            try:
                spo2_val = float(spo2.replace("%", ""))
                if spo2_val < 90:
                    serious = True
                elif spo2_val < 95:
                    moderate_flags += 1
            except ValueError:
                pass

        serious_symptoms = {"breathlessness", "chest pain"}
        if self.symptoms & serious_symptoms:
            serious = True

        moderate_symptoms = {"fever", "hives", "rash", "dizziness", "nausea"}
        moderate_flags += len(self.symptoms & moderate_symptoms)

        if serious:
            return "serious"
        if moderate_flags >= 1:
            return "moderate"
        return "mild"

    def _suggest_remedies(self, severity: str) -> str | None:
        """Return OTC suggestions for mild cases, or an escalation for moderate/serious."""
        if severity == "serious":
            return self._pick(
                "This looks serious. Please call 911 or your local emergency number if you feel it is getting worse. Do not wait.",
                "Based on your readings, this needs immediate attention. If you feel unsafe, call 911 right away. Otherwise, please get to urgent care as soon as possible.",
                "I am concerned about these readings. Please call emergency services (911) if symptoms worsen suddenly, or visit the nearest ER without delay.",
            )

        if severity == "moderate":
            return self._pick(
                "Some of your readings are a bit elevated. Keep monitoring, and if things do not improve in a day or two, please see a doctor.",
                "This looks like it could go either way. Rest up, and if it gets worse or does not improve soon, please consult a doctor.",
            )

        # Mild — suggest OTC remedies based on symptoms
        tips: list[str] = []

        if "cold" in self.symptoms or "cough" in self.symptoms:
            tips.append(self._pick(
                "For the cold, stay hydrated, rest well, and warm fluids like soup or tea can help.",
                "Plenty of fluids and rest should help with the cold. Honey in warm water can soothe a cough.",
            ))

        if "fever" in self.symptoms:
            tips.append(self._pick(
                "For a mild fever, paracetamol (acetaminophen) can help. Stay hydrated and rest.",
                "You can take paracetamol for the fever. Light clothing and cool compresses also help.",
            ))

        if self.symptoms & {"allergies", "hives", "rash", "itching", "sneezing"}:
            tips.append(self._pick(
                "An over-the-counter antihistamine like cetirizine or loratadine should help with the allergy symptoms.",
                "For allergies, try an OTC antihistamine. Avoid known triggers and keep windows closed if it is pollen-related.",
            ))

        if "headache" in self.symptoms:
            tips.append(self._pick(
                "For a headache, paracetamol or ibuprofen can help. Make sure you are drinking enough water.",
                "Try paracetamol or ibuprofen for the headache. Rest in a quiet, dark room if possible.",
            ))

        if "sore throat" in self.symptoms:
            tips.append(self._pick(
                "For the sore throat, warm salt water gargles and lozenges can help. Stay hydrated.",
                "Try gargling with warm salt water. Throat lozenges and warm drinks can ease the pain.",
            ))

        if "nausea" in self.symptoms or "diarrhea" in self.symptoms:
            tips.append(self._pick(
                "Stay hydrated with small sips of water or an ORS solution. Avoid heavy or spicy food for now.",
                "Keep up your fluids. Bland foods like toast or rice are easier on the stomach.",
            ))

        if "body ache" in self.symptoms:
            tips.append(self._pick(
                "For body aches, rest and paracetamol or ibuprofen should help.",
                "Body aches usually ease with rest. Paracetamol can take the edge off.",
            ))

        if "dizziness" in self.symptoms:
            tips.append(self._pick(
                "For dizziness, sit or lie down until it passes. Stay hydrated and avoid sudden movements.",
                "Take it easy and drink water. If the dizziness is frequent, a doctor visit would be wise.",
            ))

        if "fatigue" in self.symptoms:
            tips.append(self._pick(
                "Fatigue often improves with good sleep, hydration, and balanced meals.",
                "Make sure you are getting enough rest and eating well. Fatigue can be a sign your body needs recovery time.",
            ))

        if not tips:
            return None

        advice = " ".join(tips)
        disclaimer = self._pick(
            "Of course, if things get worse, please see a doctor.",
            "These are general suggestions. If your condition worsens, do consult a doctor.",
            "Keep an eye on how you feel. If it does not improve in a couple of days, please visit a doctor.",
        )
        return f"{advice} {disclaimer}"

    def _extract_entities_from_user_text(self, text: str) -> dict[str, Any]:
        recorded_at, time_label = self._extract_datetime(text)
        return {
            "name": self._extract_name(text),
            "age": self._extract_age(text),
            "weight_kg": self._extract_weight_kg(text),
            "height_cm": self._extract_height_cm(text),
            "address": self._extract_address(text),
            "symptoms": self._extract_symptoms(text),
            "no_symptoms": self._reports_no_symptoms(text),
            "weight_unknown": self._reports_unknown_for("weight", text),
            "height_unknown": self._reports_unknown_for("height", text),
            "address_unknown": self._reports_unknown_for("address", text),
            "blood_pressure": self._extract_blood_pressure(text),
            "heart_rate": self._extract_heart_rate(text),
            "blood_pressure_mentioned": self._mentions_blood_pressure(text),
            "heart_rate_mentioned": self._mentions_heart_rate(text),
            "doctor_request": self._is_doctor_request(text),
            "doctor_decline": self._is_doctor_decline(text),
            "affirmative": self._is_affirmative(text),
            "negative": self._is_negative(text),
            "severe_flag": self._mentions_severe_symptom(text),
            "stop_requested": self._is_stop_request(text),
            "skip_response": self._is_skip_response(text),
            "recorded_at": recorded_at,
            "time_label": time_label,
        }

    def _extract_name(self, text: str) -> str | None:
        # Authenticated users manage their name via the profile UI, not chat
        if self.authenticated:
            return None

        # Explicit correction patterns — always checked (even if name is set)
        explicit_patterns = [
            r"\bmy name is\s+([a-zA-Z][a-zA-Z' -]{0,40})",
            r"\bcall me\s+([a-zA-Z][a-zA-Z' -]{0,40})",
        ]
        # Ambiguous patterns — only used when no name is set yet
        ambiguous_patterns = [
            r"\bthis is\s+([a-zA-Z][a-zA-Z' -]{0,40})",
            r"\bi am\s+([a-zA-Z][a-zA-Z' -]{0,40})",
            r"\bi'?m\s+([a-zA-Z][a-zA-Z' -]{0,40})",
            r"\bit'?s\s+([a-zA-Z][a-zA-Z' -]{0,40})",
        ]
        stop_words = {"and", "age", "is", "i", "im", "i'm", "my", "hey", "hi", "hello"}
        # Words that indicate a status/feeling, not a name introduction
        status_words = {
            "feeling", "doing", "fine", "good", "great", "okay", "ok", "well",
            "sick", "not", "bad", "better", "worse", "tired", "having", "got",
        }

        has_name = bool(self.profile.get("name"))
        patterns = explicit_patterns if has_name else explicit_patterns + ambiguous_patterns

        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            candidate = match.group(1).strip()
            candidate = re.split(r"\b(?:and|age|i am|i'm|im)\b", candidate, maxsplit=1, flags=re.IGNORECASE)[0]
            words = [w for w in re.findall(r"[a-zA-Z][a-zA-Z'-]*", candidate) if w.lower() not in stop_words]
            if not words:
                continue
            # Reject if the first word is a status/feeling word
            if words[0].lower() in status_words:
                continue
            return " ".join(words[:2]).title()

        # When we just asked for the name, accept a bare word/phrase as a name
        if not has_name:
            stripped = text.strip()
            words = [w for w in re.findall(r"[a-zA-Z][a-zA-Z'-]*", stripped) if w.lower() not in stop_words]
            if words and len(words) <= 3 and not re.search(r"\d", stripped):
                if words[0].lower() not in status_words:
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
            r"\b(?:i live in|i am from|my area is|i stay in)\s+(.+)",
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
            r"^(?:my address(?: is| would be)?|address(?: is)?|it is|it's|is|i live in|i am from|my area is|i stay in)\s+",
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
        street_suffixes = (
            "street", "st", "road", "rd", "drive", "dr", "avenue", "ave",
            "lane", "ln", "boulevard", "blvd", "way", "court", "ct",
            "place", "pl", "circle", "cir", "highway", "hwy", "parkway", "pkwy",
        )
        has_suffix = any(re.search(rf"\b{re.escape(s)}\b", lowered) for s in street_suffixes)
        # Also accept if it contains a US state abbreviation or zip code
        has_region = bool(re.search(r"\b[A-Z]{2}\b", text)) or bool(re.search(r"\b\d{5}(?:-\d{4})?\b", lowered))
        return has_number and (has_suffix or has_region)

    def _extract_symptoms(self, text: str) -> list[str]:
        lowered = text.lower()
        symptom_aliases = {
            "allergy": "allergies",
            "allergies": "allergies",
            "hives": "hives",
            "rash": "rash",
            "itching": "itching",
            "sneezing": "sneezing",
            "cold": "cold",
            "cough": "cough",
            "fever": "fever",
            "headache": "headache",
            "migraine": "headache",
            "nausea": "nausea",
            "vomiting": "nausea",
            "diarrhea": "diarrhea",
            "sore throat": "sore throat",
            "throat pain": "sore throat",
            "body ache": "body ache",
            "body pain": "body ache",
            "fatigue": "fatigue",
            "tiredness": "fatigue",
            "dizziness": "dizziness",
            "dizzy": "dizziness",
            "chest pain": "chest pain",
            "breathlessness": "breathlessness",
            "breathless": "breathlessness",
            "shortness of breath": "breathlessness",
            "breathing difficulty": "breathlessness",
        }
        detected: list[str] = []
        for key, canonical in symptom_aliases.items():
            pattern = rf"\b{re.escape(key)}\b"
            for match in re.finditer(pattern, lowered):
                if self._is_negated_mention(lowered, match.start()):
                    continue
                if canonical not in detected:
                    detected.append(canonical)
                break
        return detected

    def _is_negated_mention(self, lowered_text: str, start_index: int) -> bool:
        window_start = max(0, start_index - 30)
        context = lowered_text[window_start:start_index]
        negations = (
            "no",
            "not",
            "without",
            "don't",
            "dont",
            "do not",
            "never",
        )
        return any(re.search(rf"\b{re.escape(token)}\b", context) for token in negations)

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

        # Context fallback: user answered right after a BP prompt with only numbers ("122 78").
        if self._is_waiting_for_vital("blood_pressure"):
            compact = re.search(r"\b(\d{4})\b", normalized)
            if compact:
                combined = compact.group(1)
                systolic = int(combined[:2])
                diastolic = int(combined[2:])
                if 60 <= systolic <= 250 and 40 <= diastolic <= 150:
                    return f"{systolic}/{diastolic}"

            numbers = re.findall(r"\d{2,3}", normalized)
            if len(numbers) >= 2:
                for idx in range(len(numbers) - 1):
                    systolic = int(numbers[idx])
                    diastolic = int(numbers[idx + 1])
                    if 60 <= systolic <= 250 and 40 <= diastolic <= 150:
                        return f"{systolic}/{diastolic}"
        return None

    def _extract_heart_rate(self, text: str) -> int | None:
        lowered = text.lower()
        segment = lowered
        waiting_for_heart_rate = self._is_waiting_for_vital("heart_rate")

        focused = re.search(r"\b(?:heart rate|pulse)\b(.*)", lowered, flags=re.IGNORECASE)
        if focused:
            segment = focused.group(1)
        elif "bpm" in lowered:
            segment = lowered
        elif waiting_for_heart_rate:
            # Avoid misreading unrelated profile updates as heart rate values.
            if self._mentions_blood_pressure(lowered) or "/" in lowered:
                return None
            if re.search(r"\b(?:weight|kg|height|cm|age|years?|address|temperature|fever)\b", lowered):
                return None
            segment = lowered
        else:
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

    def _extract_datetime(self, text: str) -> tuple[datetime | None, str | None]:
        """Parse relative/absolute time references from user text.

        Returns (datetime_utc, human_label) or (None, None).
        """
        lowered = text.lower()
        now = datetime.now(timezone.utc)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)

        # Time-of-day offsets
        time_offsets = {
            "this morning": 8,
            "in the morning": 8,
            "this afternoon": 14,
            "in the afternoon": 14,
            "this evening": 19,
            "in the evening": 19,
            "tonight": 21,
            "last night": -3,  # sentinel for yesterday night
        }

        # Relative day phrases
        day_match = None
        label = None

        if re.search(r"\byesterday\b", lowered):
            day_match = today - timedelta(days=1)
            label = "yesterday"
        elif re.search(r"\bday before yesterday\b", lowered):
            day_match = today - timedelta(days=2)
            label = "day before yesterday"
        elif re.search(r"\btoday\b", lowered) or re.search(r"\bjust now\b", lowered):
            return now, "just now"
        elif re.search(r"\bthis morning\b", lowered):
            return today.replace(hour=8), "this morning"
        elif re.search(r"\bthis afternoon\b", lowered):
            return today.replace(hour=14), "this afternoon"
        elif re.search(r"\bthis evening\b", lowered):
            return today.replace(hour=19), "this evening"
        elif re.search(r"\blast night\b", lowered):
            yesterday = today - timedelta(days=1)
            return yesterday.replace(hour=22), "last night"
        elif re.search(r"\btonight\b", lowered):
            return today.replace(hour=21), "tonight"

        # "N days/hours ago"
        ago_match = re.search(r"\b(\d{1,2})\s+(day|days|hour|hours)\s+ago\b", lowered)
        if ago_match:
            amount = int(ago_match.group(1))
            unit = ago_match.group(2)
            if "hour" in unit:
                result = now - timedelta(hours=amount)
                label = f"{amount} hour{'s' if amount != 1 else ''} ago"
            else:
                result = now - timedelta(days=amount)
                label = f"{amount} day{'s' if amount != 1 else ''} ago"
            return result, label

        # "last Monday", "on Tuesday", etc.
        weekdays = {
            "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
            "friday": 4, "saturday": 5, "sunday": 6,
        }
        weekday_match = re.search(
            r"\b(?:last|on|this past)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
            lowered,
        )
        if weekday_match:
            target_day = weekdays[weekday_match.group(1)]
            current_day = now.weekday()
            days_back = (current_day - target_day) % 7
            if days_back == 0:
                days_back = 7
            result = today - timedelta(days=days_back)
            label = weekday_match.group(1).title()
            return result.replace(hour=12), label

        if day_match:
            # Check for time-of-day qualifier on the matched day
            for phrase, hour in time_offsets.items():
                if phrase in lowered and hour >= 0:
                    return day_match.replace(hour=hour), f"{label} {phrase.split()[-1]}"
            return day_match.replace(hour=12), label

        return None, None

    def _mentions_blood_pressure(self, text: str) -> bool:
        lowered = text.lower()
        return "blood pressure" in lowered or bool(re.search(r"\bbp\b", lowered))

    def _mentions_heart_rate(self, text: str) -> bool:
        lowered = text.lower()
        return "heart rate" in lowered or "pulse" in lowered

    def _reports_no_symptoms(self, text: str) -> bool:
        lowered = text.lower()
        patterns = (
            r"\bno symptoms?\b",
            r"\bno major symptoms?\b",
            r"\bnothing (?:serious|major|wrong|really|particular|specific|much)\b",
            r"\bnope\b",
            r"\bi (?:feel|am|'?m) (?:fine|good|okay|ok|great|alright|better|well|pretty good)\b",
            r"\bdoing (?:fine|good|okay|ok|great|alright|well)\b",
            r"\ball good\b",
            r"\bjust (?:here to|want to|wanna) log\b",
            r"\bjust (?:logging|checking in|a check.?in)\b",
            r"\bno (?:issues?|problems?|complaints?|concerns?)\b",
            r"\bnot really\b",
        )
        return any(re.search(pattern, lowered) for pattern in patterns)

    def _reports_unknown_for(self, field: str, text: str) -> bool:
        lowered = text.lower()
        unknown_markers = (
            "don't know",
            "do not know",
            "not sure",
            "no idea",
            "cannot remember",
            "can't remember",
        )
        if not any(marker in lowered for marker in unknown_markers):
            return False

        field_keywords = {
            "weight": ("weight", "kg", "kilogram"),
            "height": ("height", "cm", "centimeter"),
            "address": ("address", "city", "area", "location"),
        }
        keywords = field_keywords.get(field, ())
        return any(keyword in lowered for keyword in keywords)

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
            r"\bmaybe later\b",
        )
        return any(re.search(pattern, lowered) for pattern in stop_patterns)

    def _is_skip_response(self, text: str) -> bool:
        lowered = text.lower().strip()
        return bool(re.match(
            r"^(?:no|nope|nah|skip|pass|not really|don'?t (?:know|have)(?: (?:it|that|one))?|i don'?t)[\s.!]*$",
            lowered,
        ))

    def _is_doctor_request(self, text: str) -> bool:
        lowered = text.lower()
        keywords = ("doctor", "doctors", "clinic", "nearby", "recommend", "hospital", "appointment")
        return any(keyword in lowered for keyword in keywords)

    def _is_doctor_decline(self, text: str) -> bool:
        lowered = text.lower()
        decline_markers = (
            r"\bno doctor\b",
            r"\bno doctors\b",
            r"\bnot now\b",
            r"\bno recommendation\b",
            r"\bno need\b",
            r"\bskip (?:doctor|doctors|recommendations?)\b",
        )
        if any(re.search(pattern, lowered) for pattern in decline_markers):
            return True
        return bool(re.search(r"\b(?:no|don't|do not)\b.*\b(?:doctor|clinic|recommend|suggest)\b", lowered))

    def _is_affirmative(self, text: str) -> bool:
        lowered = text.lower().strip()
        return bool(
            re.search(
                r"\b(?:yes|yeah|yep|sure|please do|go ahead|okay|ok|sounds good)\b",
                lowered,
            )
        )

    def _is_negative(self, text: str) -> bool:
        lowered = text.lower().strip()
        return bool(
            re.search(
                r"\b(?:no|nope|not now|skip|don't|do not|maybe later)\b",
                lowered,
            )
        )

    def _mentions_severe_symptom(self, text: str) -> bool:
        lowered = text.lower()
        severe_terms = ("chest pain", "shortness of breath", "breathlessness", "difficulty breathing")
        return any(term in lowered for term in severe_terms)

    def _is_waiting_for_vital(self, vital_type: str) -> bool:
        return vital_type in self.prompted_missing_fields and vital_type not in self.logged_vital_types

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

    async def _tool_log_vital(self, inputs: dict, recorded_at: datetime | None = None) -> dict:
        vital_type = inputs["vital_type"]
        value = inputs["value"]
        unit = inputs.get("unit")
        notes = inputs.get("notes")

        if self.user_id is None:
            fallback_name = self.profile.get("name")
            if not fallback_name:
                fallback_name = f"anonymous-session-{self.session_id}"
            fallback_age = self.profile.get("age")
            user = database.get_or_create_user(str(fallback_name), fallback_age)
            self.user_id = user.id
            self.user_name = user.name
            database.update_session_user(self.session_id, self.user_id)

        vital = database.log_vital(self.user_id, vital_type, value, unit, notes, recorded_at=recorded_at)
        self.logged_vital_types.add(vital_type)
        self.logged_vital_values[vital_type] = value

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

        if isinstance(age, int) and not isinstance(age, bool) and age > 0:
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

    def identify_user(self, name: str, age: int | None = None, from_auth: bool = False):
        user = database.get_or_create_user(name, age)
        self.user_id = user.id
        self.user_name = user.name
        self.profile["name"] = user.name
        if age is not None:
            self.profile["age"] = age
        if from_auth:
            self.authenticated = True
        database.update_session_user(self.session_id, self.user_id)
        database.attach_session_messages_to_user(self.session_id, self.user_id)

    def end_session(self):
        database.close_session(self.session_id)
