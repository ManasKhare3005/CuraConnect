import json
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
from services.glp1_protocols import days_until_next_titration, get_current_step, get_next_step, identify_drug
from services.llm import generate_response
from services.response_builder import build_response_context
from services import outreach_scheduler

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
        self.awaiting_more_help_confirmation: bool = False
        self.conversation_tone: str = "neutral"
        self.vitals_on_cooldown: bool = False
        self.timezone_name: str | None = None
        self.utc_offset_minutes: int | None = None
        self.active_medication: dict | None = None
        self.medication_context_loaded: bool = False
        self.medication_dose_reported_this_session: bool = False
        self.highest_escalation_severity: str | None = None
        self.refill_prompted_this_session: bool = False
        self.unresolved_side_effects: list[dict] = []
        self.last_activity_at: datetime = datetime.now(timezone.utc)
        self.consecutive_vital_declines: int = 0

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
        if self.active_medication and self.user_id:
            self._load_unresolved_side_effects()
        else:
            self.unresolved_side_effects = []
        if self.unresolved_side_effects:
            symptom_names: list[str] = []
            for effect in self.unresolved_side_effects[:3]:
                symptom = str(effect.get("symptom") or "").strip()
                if symptom and symptom not in symptom_names:
                    symptom_names.append(symptom)
            symptoms_text = self._format_specialty_list(symptom_names) if symptom_names else "those side effects"
            greeting = self._pick(
                f"Hey {name}, welcome back! Last time you mentioned {symptoms_text}. How's that been going?",
                f"Hi {name}! Good to see you. Any update on the {symptoms_text} you reported?",
            )
        else:
            greeting = self._pick(
                f"Hey {name}, welcome back! How are you feeling today?",
                f"Hi {name}! Good to see you again. Any symptoms or updates to share?",
                f"Welcome back, {name}! What can I help you with today?",
            )
        audio = await synthesize_speech(greeting)
        return greeting, audio

    async def process_message(self, user_text: str, location: dict | None = None) -> dict:
        elapsed = datetime.now(timezone.utc) - self.last_activity_at
        if elapsed > timedelta(minutes=30):
            self.end_session()
            return {
                "text": "Your session has timed out for security. Please start a new conversation.",
                "audio": None,
                "events": [{"type": "session_timeout"}],
                "session_complete": True,
            }
        self.last_activity_at = datetime.now(timezone.utc)

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

    def _first_name(self) -> str | None:
        full = self.profile.get("name") or self.user_name
        if not isinstance(full, str) or not full.strip():
            return None
        return full.strip().split()[0]

    def _tone_preface_for_follow_up(self, tone: str) -> str | None:
        name = self._first_name()
        if tone == "warm":
            if name:
                return self._pick(
                    f"Hi {name}, good to hear from you.",
                    f"Hey {name}, glad you checked in.",
                )
            return self._pick(
                "Hi there, good to hear from you.",
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

    async def _handle_active_medication_turn(
        self,
        entities: dict[str, Any],
        user_text: str,
        refill_parts: list[str] | None = None,
        refill_info: str | None = None,
        refill_actions: list[str] | None = None,
    ) -> str | None:
        if not self._has_active_medication_context():
            return None

        response_parts: list[str] = list(refill_parts or [])
        actions_taken: list[str] = list(refill_actions or [])
        handled = False
        display_name = self._medication_display_name()
        scheduled_at = entities.get("recorded_at")
        logged_side_effects: list[str] = []
        logged_medication_vitals = False
        severity: str | None = None
        remedy_text: str | None = None
        escalation_created = False
        next_question_field: str | None = None
        updates: dict[str, Any] = {
            "actions_taken": actions_taken,
            "fallback_parts": response_parts,
            "symptoms_added": [],
            "side_effects_logged": [],
            "resolved_side_effects": [],
            "blood_pressure": None,
            "heart_rate": None,
            "time_label": entities.get("time_label"),
            "refill_info": refill_info,
        }
        resolved_side_effects = list(entities.get("resolved_side_effects") or [])
        if not resolved_side_effects:
            resolved_side_effects = self._check_side_effect_resolution(user_text)
            if resolved_side_effects:
                entities["resolved_side_effects"] = list(resolved_side_effects)
        if resolved_side_effects:
            entities["symptoms"] = [
                symptom
                for symptom in (entities.get("symptoms") or [])
                if symptom not in resolved_side_effects
            ]
            updates["resolved_side_effects"] = list(resolved_side_effects)
            readable_resolved = self._format_specialty_list(resolved_side_effects)
            actions_taken.append(f"Marked {readable_resolved} as resolved.")
            response_parts.append(
                self._pick(
                    f"Great news - I've marked your {readable_resolved} as resolved.",
                    f"Glad to hear that. I marked your {readable_resolved} as resolved.",
                )
            )

        if entities.get("dose_missed"):
            await self._tool_log_medication_event(
                event_type="dose_missed",
                dose_mg=self.active_medication.get("current_dose_mg") if self.active_medication else None,
                scheduled_at=scheduled_at,
                notes="User reported a missed dose during chat.",
            )
            handled = True
            actions_taken.append(f"Logged a missed {display_name} dose.")
            response_parts.append(self._pick(
                f"I am sorry that happened. I logged the missed {display_name} dose for you.",
                f"That is okay. I logged the missed {display_name} dose.",
            ))
            remedy_text = self._pick(
                "Try to get back on schedule with guidance from your prescriber.",
                "Getting back to your regular schedule is usually the next step, but follow your prescriber's plan.",
            )
            response_parts.append(remedy_text)
        elif entities.get("dose_taken"):
            await self._tool_log_medication_event(
                event_type="dose_taken",
                dose_mg=self.active_medication.get("current_dose_mg") if self.active_medication else None,
                scheduled_at=scheduled_at,
                notes="User reported taking a dose during chat.",
            )
            handled = True
            actions_taken.append(f"Logged a {display_name} dose as taken.")
            response_parts.append(self._pick(
                f"Nice work staying on top of your {display_name}. I logged today's dose.",
                f"Great, I logged your {display_name} dose.",
            ))

        if entities.get("dose_question"):
            handled = True
            dose_summary = self._build_dose_question_response()
            actions_taken.append(f"Reviewed the current {display_name} dosing schedule.")
            response_parts.append(dose_summary)

        if handled:
            if entities.get("symptoms"):
                self.symptom_status = "present"
                active_side_effect_names = {
                    str(effect.get("symptom") or "").strip().lower()
                    for effect in self.unresolved_side_effects
                }
                for symptom in entities.get("symptoms", []):
                    if symptom in self.symptoms or symptom in active_side_effect_names:
                        continue
                    self.symptoms.add(symptom)
                    await self._tool_log_side_effect(symptom, user_text)
                    logged_side_effects.append(symptom)

            if logged_side_effects:
                readable = ", ".join(logged_side_effects)
                updates["symptoms_added"] = list(logged_side_effects)
                updates["side_effects_logged"] = list(logged_side_effects)
                actions_taken.append(f"Logged {readable} as side effects linked to {display_name}.")
                response_parts.append(self._pick(
                    f"I also logged {readable} as a side effect linked to your {display_name}.",
                    f"I noted {readable} as a medication side effect for your {display_name}.",
                ))

            if entities.get("blood_pressure"):
                await self._tool_log_vital(
                    {
                        "vital_type": "blood_pressure",
                        "value": entities["blood_pressure"],
                        "unit": "mmHg",
                    },
                    recorded_at=scheduled_at,
                )
                response_parts.append(f"Blood pressure {entities['blood_pressure']} mmHg, noted.")
                actions_taken.append(f"Logged blood pressure {entities['blood_pressure']} mmHg.")
                updates["blood_pressure"] = entities["blood_pressure"]
                logged_medication_vitals = True

            if entities.get("heart_rate") is not None:
                await self._tool_log_vital(
                    {
                        "vital_type": "heart_rate",
                        "value": str(entities["heart_rate"]),
                        "unit": "bpm",
                    },
                    recorded_at=scheduled_at,
                )
                response_parts.append(f"Heart rate {entities['heart_rate']} bpm, got it.")
                actions_taken.append(f"Logged heart rate {entities['heart_rate']} bpm.")
                updates["heart_rate"] = entities["heart_rate"]
                logged_medication_vitals = True

            if entities.get("severe_flag"):
                response_parts.append(self._pick(
                    "If that gets worse, please call 911 or seek urgent care right away.",
                    "Please seek urgent care right away if that symptom worsens.",
                ))

            if entities.get("symptoms") or entities.get("severe_flag") or logged_medication_vitals:
                severity = self._assess_severity()
                actions_taken.append(f"Assessed severity as {severity}.")
                remedy_text = self._suggest_remedies(severity)
                if remedy_text:
                    response_parts.append(remedy_text)
                escalation = self._maybe_create_escalation(severity)
                escalation_created = bool(escalation)
                if escalation_created:
                    actions_taken.append("Created a clinical escalation for the care team.")
                    response_parts.append(
                        "I've flagged this for your care team. They'll have your full history and can follow up with you directly."
                    )

            if entities.get("dose_taken") and not logged_side_effects and not escalation_created:
                next_question_field = "medication_side_effects"
                response_parts.append(self._pick(
                    "Have you noticed any side effects since the injection, like nausea, constipation, diarrhea, or injection site irritation?",
                    "Any side effects after the injection, such as nausea, constipation, diarrhea, or an injection site reaction?",
                ))

            return await self._render_structured_response(
                updates=updates,
                entities=entities,
                severity=severity,
                escalation_created=escalation_created,
                remedy_text=remedy_text,
                next_question_field=next_question_field,
                refill_parts=refill_parts,
            )

        return None

    async def _run_scripted_assistant(self, user_text: str) -> str:
        # If this is the first message and it's just a conversational closer, reply briefly
        if len(self.messages) <= 1 and self._is_conversational_closer(user_text):
            self.session_complete = True
            self.awaiting_more_help_confirmation = False
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
                self.awaiting_more_help_confirmation = False
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
        resolved_side_effects = self._check_side_effect_resolution(user_text)
        if resolved_side_effects:
            entities["resolved_side_effects"] = list(resolved_side_effects)
            entities["symptoms"] = [
                symptom
                for symptom in (entities.get("symptoms") or [])
                if symptom not in resolved_side_effects
            ]
        detected_tone = self._detect_conversation_tone(user_text)
        self.conversation_tone = detected_tone
        stop_requested = bool(entities.get("stop_requested"))
        skip_response = bool(entities.get("skip_response"))
        blood_pressure_mentioned = bool(entities.get("blood_pressure_mentioned"))
        heart_rate_mentioned = bool(entities.get("heart_rate_mentioned"))
        medication_action_requested = bool(
            entities.get("dose_taken")
            or entities.get("dose_missed")
            or entities.get("refill_request")
            or entities.get("dose_question")
        )
        if (
            "medication_dose" in self.prompted_missing_fields
            and not self.medication_dose_reported_this_session
            and entities.get("negative")
        ):
            entities["dose_missed"] = True
            medication_action_requested = True
        if "refill_check" in self.prompted_missing_fields:
            if entities.get("affirmative") and self._has_active_medication_context():
                entities["refill_request"] = True
                medication_action_requested = True
                self.prompted_missing_fields.discard("refill_check")
            elif entities.get("negative") or skip_response:
                self.prompted_missing_fields.discard("refill_check")
        if entities.get("refill_request"):
            self.prompted_missing_fields.discard("refill_check")

        # "nope"/"no"/"skip" with no data — mark last prompted vital as done and move on
        has_any_data = (
            entities.get("blood_pressure")
            or entities.get("heart_rate") is not None
            or entities.get("symptoms")
            or resolved_side_effects
            or entities.get("name")
            or entities.get("age") is not None
            or medication_action_requested
        )

        # After the soft-close prompt ("anything else?"), let the user either continue or end.
        if self.awaiting_more_help_confirmation:
            if self._is_gratitude_or_goodbye(user_text) or stop_requested or entities.get("negative") or skip_response:
                self.awaiting_more_help_confirmation = False
                self.session_complete = True
                return self._final_signoff_message()

            if entities.get("affirmative") and not has_any_data:
                self.awaiting_more_help_confirmation = False
                return self._pick(
                    "Absolutely. What else can I help you with?",
                    "Of course. Tell me what you would like to update next.",
                )

            if (
                has_any_data
                or entities.get("doctor_request")
                or entities.get("doctor_decline")
                or entities.get("no_symptoms")
                or medication_action_requested
            ):
                self.awaiting_more_help_confirmation = False

        pending_vital_type = self._next_pending_vital_type()
        pending_vital_skipped = (
            not has_any_data
            and pending_vital_type is not None
            and (skip_response or entities.get("negative"))
        )

        if pending_vital_skipped:
            self.logged_vital_types.add(pending_vital_type)
            self.consecutive_vital_declines += 1
            # After 2 consecutive declines, stop asking for vitals and wrap up
            if self.consecutive_vital_declines >= 2:
                self.awaiting_more_help_confirmation = True
                # Mark remaining vitals as skipped so they aren't re-asked
                for vt in ("blood_pressure", "heart_rate", "temperature", "oxygen_saturation"):
                    self.prompted_missing_fields.add(vt)
                wrap_text = self._pick(
                    "No problem. Is there anything else I can help you with?",
                    "Got it. Anything else on your mind?",
                )
                return await self._render_structured_response(
                    updates={"fallback_parts": [wrap_text]},
                    entities=entities,
                    next_question_field="more_help_confirmation",
                )
            follow_up_field, follow_up = self._next_scripted_prompt()
            if follow_up:
                return await self._render_structured_response(
                    updates={
                        "actions_taken": [f"They do not have a {pending_vital_type.replace('_', ' ')} reading right now."],
                        "fallback_parts": [follow_up],
                    },
                    entities=entities,
                    next_question_field=follow_up_field,
                )
            fallback_text = self._pick(
                "No problem! Anything else you want to share?",
                "Got it. Let me know if there is anything else.",
            )
            return await self._render_structured_response(
                updates={"fallback_parts": [fallback_text]},
                entities=entities,
            )
        recorded_at = entities.get("recorded_at")
        time_label = entities.get("time_label")
        updates: dict[str, Any] = {
            "profile_changed": {},
            "symptoms_added": [],
            "side_effects_logged": [],
            "resolved_side_effects": list(resolved_side_effects),
            "vitals_logged": [],
            "blood_pressure": None,
            "heart_rate": None,
            "existing_details": [],
            "symptom_status": None,
            "doctor_preference": None,
            "time_label": time_label,
            "actions_taken": [],
        }
        if resolved_side_effects:
            readable_resolved = self._format_specialty_list(resolved_side_effects)
            updates["actions_taken"].append(f"Marked {readable_resolved} as resolved.")

        # Profile updates (name/age only — weight, height, address are in the profile UI)
        profile_inputs: dict[str, Any] = {}
        if entities.get("name"):
            profile_inputs["name"] = entities["name"]
        if entities.get("age") is not None:
            profile_inputs["age"] = entities["age"]

        has_structured_data = (
            bool(profile_inputs)
            or bool(entities.get("symptoms"))
            or bool(resolved_side_effects)
            or medication_action_requested
        )

        refill_response_parts: list[str] = []
        refill_info: str | None = None
        refill_actions: list[str] = []
        refill_handled = False
        if entities.get("refill_request") and self._has_active_medication_context():
            refill_result = await self._handle_refill_request_turn(entities, user_text)
            if isinstance(refill_result, dict):
                refill_response_parts.extend(refill_result.get("parts") or [])
                refill_info = str(refill_result.get("info") or "").strip() or None
                refill_actions = list(refill_result.get("actions_taken") or [])
                refill_handled = bool(refill_result.get("handled"))

        has_non_refill_actions = bool(
            profile_inputs
            or entities.get("symptoms")
            or resolved_side_effects
            or entities.get("blood_pressure")
            or entities.get("heart_rate") is not None
            or entities.get("dose_taken")
            or entities.get("dose_missed")
            or entities.get("dose_question")
            or entities.get("doctor_request")
            or entities.get("doctor_decline")
        )
        if refill_handled and not has_non_refill_actions:
            return await self._render_structured_response(
                updates={
                    "actions_taken": refill_actions,
                    "fallback_parts": refill_response_parts,
                    "refill_info": refill_info,
                },
                entities=entities,
                refill_parts=refill_response_parts,
            )

        medication_entities = dict(entities)
        if refill_handled:
            medication_entities["refill_request"] = False
        medication_response = await self._handle_active_medication_turn(
            medication_entities,
            user_text,
            refill_parts=refill_response_parts,
            refill_info=refill_info,
            refill_actions=refill_actions,
        )
        if medication_response:
            return medication_response

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

        active_side_effect_names = {
            str(effect.get("symptom") or "").strip().lower()
            for effect in self.unresolved_side_effects
        }
        for symptom in entities.get("symptoms", []):
            if symptom in self.symptoms or symptom in active_side_effect_names:
                continue
            self.symptoms.add(symptom)
            updates["symptoms_added"].append(symptom)
            if self._has_active_medication_context():
                side_effect_result = await self._tool_log_side_effect(symptom, user_text)
                updates["side_effects_logged"].append(side_effect_result)
            else:
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
            known = ", ".join(sorted({*self.symptoms, *active_side_effect_names}))
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
        clarification_fields: list[str] = []
        if blood_pressure_mentioned and not blood_pressure:
            self.prompted_missing_fields.add("blood_pressure")
            clarification_fields.append("blood_pressure")
            clarifications.append(
                "I could not read that blood pressure clearly. Please share it as two numbers, like 122 over 78."
            )
        if heart_rate_mentioned and heart_rate is None:
            self.prompted_missing_fields.add("heart_rate")
            clarification_fields.append("heart_rate")
            clarifications.append(
                "I could not read the heart rate clearly. Please share one number in bpm, like 72."
            )

        # "no thanks" at a doctor prompt should decline doctors, not end session
        if stop_requested and "doctor_preference" in self.prompted_missing_fields and self.wants_doctor_suggestions is None:
            self.wants_doctor_suggestions = False
            updates["doctor_preference"] = False
            stop_requested = False

        if stop_requested and not has_structured_data and not clarifications:
            self.awaiting_more_help_confirmation = False
            self.session_complete = True
            return self._final_signoff_message()

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
        updates["fallback_parts"] = response_parts
        if refill_info:
            updates["refill_info"] = refill_info
        response_parts.extend(refill_response_parts)
        response_parts.extend(self._build_acknowledgements(updates))

        if entities.get("severe_flag"):
            response_parts.append(self._pick(
                "That sounds serious. If it gets worse, please call 911 or your local emergency line immediately.",
                "I want to flag that. If the symptoms worsen, do not hesitate to call 911 or go to the nearest ER.",
                "Please take this seriously. Call 911 right away if you experience worsening chest pain or difficulty breathing.",
            ))

        if clarifications:
            response_parts.extend(clarifications)
            updates["clarification_fields"] = clarification_fields
            next_question_field = clarification_fields[0] if len(clarification_fields) == 1 else None
            return await self._render_structured_response(
                updates=updates,
                entities=entities,
                next_question_field=next_question_field,
                refill_parts=refill_response_parts,
            )

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
            self.awaiting_more_help_confirmation = False
            response_parts.append(self._final_signoff_message())
            self.session_complete = True
            merged = " ".join(part.strip() for part in response_parts if part and part.strip())
            return merged

        # Severity assessment + remedy suggestions after new data is logged
        new_data_logged = bool(updates.get("symptoms_added")) or bool(updates.get("blood_pressure")) or updates.get("heart_rate") is not None
        severity: str | None = None
        remedy_text: str | None = None
        escalation_created = False
        if new_data_logged and (self.symptoms or self.logged_vital_types):
            severity = self._assess_severity()
            updates["actions_taken"].append(f"Assessed severity as {severity}.")
            escalation = self._maybe_create_escalation(severity)
            escalation_created = bool(escalation)
            remedy_text = self._suggest_remedies(severity)
            if remedy_text:
                response_parts.append(remedy_text)
            if escalation_created:
                updates["actions_taken"].append("Created a clinical escalation for the care team.")
                response_parts.append(
                    "I've flagged this for your care team. They'll have your full history and can follow up with you directly."
                )

        next_question_field: str | None = None
        follow_up = None
        freshly_added_symptoms = updates.get("symptoms_added") or []
        vitals_just_logged = bool(
            updates.get("blood_pressure")
            or updates.get("heart_rate") is not None
            or any(v.get("vital_type") not in ("symptom", None) for v in (updates.get("vitals_logged") or []))
        )
        # When the patient just mentioned symptoms with no vitals, let the LLM
        # ask a brief follow-up about the symptom rather than immediately pivoting to BP.
        symptom_pause = (
            bool(freshly_added_symptoms)
            and not vitals_just_logged
            and not escalation_created
            and not self.vitals_on_cooldown
        )
        if not escalation_created and not symptom_pause:
            next_question_field, follow_up = self._next_scripted_prompt()
        if follow_up:
            if not response_parts and not has_structured_data and not stop_requested and not clarifications:
                tone_preface = self._tone_preface_for_follow_up(detected_tone)
                if tone_preface:
                    response_parts.append(tone_preface)
            response_parts.append(follow_up)

        if symptom_pause and freshly_added_symptoms:
            symptom_names = ", ".join(str(s) for s in freshly_added_symptoms[:3])
            updates.setdefault("actions_taken", [])
            updates["actions_taken"].append(f"Logged symptom(s): {symptom_names}.")
            updates.setdefault("fallback_parts", [])
            updates["fallback_parts"].append(
                f"Logged {symptom_names}. Can you tell me more about how that started?"
            )
            updates["_symptom_pause"] = symptom_names

        return await self._render_structured_response(
            updates=updates,
            entities=entities,
            severity=severity,
            escalation_created=escalation_created,
            remedy_text=remedy_text,
            next_question_field=next_question_field,
            doctor_result=doctor_result,
            refill_parts=refill_response_parts,
        )

    @staticmethod
    def _pick(*options: str) -> str:
        return random.choice(options)

    def _final_signoff_message(self) -> str:
        name = self.profile.get("name")
        if name:
            return self._pick(
                f"No problem, {name}. You are all set for now. If anything changes, I am here to help.",
                f"All set, {name}. I am here whenever you need another check-in.",
            )
        return self._pick(
            "No problem. You are all set for now. If anything changes, I am here to help.",
            "All set for now. I am here whenever you need another check-in.",
        )

    async def _render_structured_response(
        self,
        updates: dict[str, Any],
        entities: dict[str, Any],
        severity: str | None = None,
        escalation_created: bool = False,
        remedy_text: str | None = None,
        next_question_field: str | None = None,
        doctor_result: dict | None = None,
        refill_parts: list[str] | None = None,
    ) -> str:
        context = build_response_context(
            session=self,
            updates=updates,
            entities=entities,
            severity=severity,
            escalation_created=bool(escalation_created),
            remedy_text=remedy_text,
            next_question_field=next_question_field,
            doctor_result=doctor_result,
            refill_parts=refill_parts,
        )
        return await generate_response(context)

    def _build_acknowledgements(self, updates: dict[str, Any]) -> list[str]:
        parts: list[str] = []
        changed = updates.get("profile_changed") or {}
        name = self._first_name()

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

        resolved_side_effects = updates.get("resolved_side_effects") or []
        if resolved_side_effects:
            readable_resolved = self._format_specialty_list(resolved_side_effects)
            parts.append(self._pick(
                f"Great news - I've marked your {readable_resolved} as resolved.",
                f"Glad to hear that. I marked your {readable_resolved} as resolved.",
            ))

        symptoms_added = updates.get("symptoms_added") or []
        side_effects_logged = updates.get("side_effects_logged") or []
        if symptoms_added:
            readable = ", ".join(symptoms_added)
            if side_effects_logged and self.active_medication:
                medication_name = self._medication_display_name()
                if len(symptoms_added) == 1:
                    parts.append(self._pick(
                        f"I logged the {readable} as a side effect linked to your {medication_name}.",
                        f"Noted the {readable} as a medication side effect for your {medication_name}.",
                    ))
                else:
                    parts.append(self._pick(
                        f"I logged {readable} as side effects linked to your {medication_name}.",
                        f"Noted {readable} as medication side effects for your {medication_name}.",
                    ))
            elif len(symptoms_added) == 1:
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

    def _next_scripted_prompt(self) -> tuple[str | None, str]:
        name = self._first_name()

        if not name:
            return None, self._pick(
                "What name should I use for you?",
                "What is your name?",
                "First, what should I call you?",
            )

        if self.profile.get("age") is None:
            return "age", self._pick(
                f"And how old are you, {name}?",
                f"What is your age, {name}?",
            )

        pending_vital_field, pending_vital_question = self._next_pending_vital_prompt()
        if pending_vital_question:
            return pending_vital_field, pending_vital_question

        if self._has_active_medication_context() and not self.medication_dose_reported_this_session:
            if "medication_dose" not in self.prompted_missing_fields:
                self.prompted_missing_fields.add("medication_dose")
                drug_name = str(self.active_medication.get("drug_name") or "medication")
                return "medication_dose", f"Have you taken your {drug_name} injection this week?"

        should_prompt_refill, refill_message = self._should_prompt_refill()
        if should_prompt_refill and refill_message:
            self.refill_prompted_this_session = True
            self.prompted_missing_fields.add("refill_check")
            return "refill_check", f"{refill_message} Want me to flag it for your care team?"

        if self.symptom_status is None and not self.symptoms:
            if "symptoms" not in self.prompted_missing_fields:
                self.prompted_missing_fields.add("symptoms")
                return "symptoms", self._pick(
                    f"How are you feeling today, {name}?",
                    "What brings you in today? Any symptoms?",
                    "How have you been feeling lately?",
                )

        if self.vitals_on_cooldown and not self.symptoms:
            self.awaiting_more_help_confirmation = True
            return "more_help_confirmation", self._pick(
                "You are all caught up. Is there anything else I can help with?",
                "Vitals look up to date. Anything else you need?",
            )

        has_fever = "fever" in self.symptoms
        has_breathing = "breathlessness" in self.symptoms

        if has_fever and "temperature" not in self.logged_vital_types:
            if "temperature" not in self.prompted_missing_fields:
                self.prompted_missing_fields.add("temperature")
                return "temperature", self._pick(
                    "Since you mentioned a fever, have you checked your temperature?",
                    "Do you know what your temperature is? That would help track the fever.",
                )

        if has_breathing and "oxygen_saturation" not in self.logged_vital_types:
            if "oxygen_saturation" not in self.prompted_missing_fields:
                self.prompted_missing_fields.add("oxygen_saturation")
                return "oxygen_saturation", self._pick(
                    "With the breathing trouble, do you have an oxygen reading? Like from a pulse oximeter?",
                    "If you have a pulse oximeter, your oxygen level would be helpful to log.",
                )

        if "blood_pressure" not in self.logged_vital_types:
            if "blood_pressure" not in self.prompted_missing_fields:
                self.prompted_missing_fields.add("blood_pressure")
                return "blood_pressure", self._pick(
                    "Have you checked your blood pressure recently?",
                    "Do you know your blood pressure? Something like 120 over 80.",
                )

        if "heart_rate" not in self.logged_vital_types:
            if "heart_rate" not in self.prompted_missing_fields:
                self.prompted_missing_fields.add("heart_rate")
                return "heart_rate", self._pick(
                    "What about your heart rate or pulse?",
                    "Do you know your resting heart rate?",
                )

        if not has_fever and "temperature" not in self.logged_vital_types:
            if "temperature" not in self.prompted_missing_fields:
                self.prompted_missing_fields.add("temperature")
                return "temperature", self._pick(
                    "Do you have a temperature reading to log?",
                    "Anything else? I can also note your temperature if you have it.",
                )

        self.awaiting_more_help_confirmation = True
        return "more_help_confirmation", self._pick(
            "That covers everything for now. Is there anything else I can help you with?",
            "We are all caught up. Is there anything else you would like to add?",
            "You are all set for now. Is there anything else I can help you with today?",
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
        latest_user_text = self._latest_user_text().lower()

        if self.active_medication:
            glp1_serious = {"pancreatitis", "gallbladder pain"}
            if self.symptoms & glp1_serious:
                return "serious"

            glp1_concerning = {"nausea", "stomach pain"}
            if self.symptoms & glp1_concerning:
                gi_symptoms = {
                    "nausea",
                    "constipation",
                    "diarrhea",
                    "bloating",
                    "acid reflux",
                    "stomach pain",
                    "sulfur burps",
                }
                active_gi = self.symptoms & gi_symptoms
                if len(active_gi) >= 3:
                    serious = True
                elif len(active_gi) >= 2:
                    moderate_flags += 1

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

        severe_medication_patterns = (
            r"\bcan'?t keep (?:anything|food|water) down\b",
            r"\bcant keep (?:anything|food|water) down\b",
            r"\bthrowing up all day\b",
            r"\bvomiting all day\b",
            r"\bpersistent vomiting\b",
            r"\bsevere nausea\b",
            r"\bdehydrated\b",
        )
        if any(re.search(pattern, latest_user_text) for pattern in severe_medication_patterns):
            serious = True

        moderate_symptoms = {
            "fever",
            "hives",
            "rash",
            "dizziness",
            "nausea",
            "diarrhea",
            "constipation",
            "appetite loss",
            "injection site reaction",
        }
        moderate_flags += len(self.symptoms & moderate_symptoms)

        if serious:
            return "serious"
        if moderate_flags >= 1:
            return "moderate"
        return "mild"

    def _severity_rank(self, severity: str | None) -> int:
        return {
            "mild": 0,
            "moderate": 1,
            "serious": 2,
            "urgent": 3,
        }.get(str(severity or "").strip().lower(), -1)

    def _build_trigger_reason(self, severity: str) -> str:
        medication = self.active_medication or {}
        medication_bits = [str(medication.get("drug_name") or self._medication_display_name())]
        if medication.get("current_dose_mg") is not None:
            medication_bits.append(f"{medication['current_dose_mg']}mg")
        if medication.get("titration_step") is not None:
            medication_bits.append(f"(titration step {medication['titration_step']})")

        symptom_list = sorted(self.symptoms)
        symptom_phrase = "clinically concerning symptoms"
        if symptom_list:
            if len(symptom_list) == 1:
                base_symptom = symptom_list[0]
                if severity == "serious" and not base_symptom.startswith("severe "):
                    symptom_phrase = f"severe {base_symptom}"
                else:
                    symptom_phrase = base_symptom
            else:
                symptom_phrase = ", ".join(symptom_list)

        parts = [f"Patient reported {symptom_phrase} on {' '.join(bit for bit in medication_bits if bit).strip()}."]
        vital_labels = {
            "blood_pressure": "Blood pressure",
            "heart_rate": "Heart rate",
            "temperature": "Temperature",
            "oxygen_saturation": "Oxygen saturation",
        }
        for vital_type, label in vital_labels.items():
            value = self.logged_vital_values.get(vital_type)
            if value:
                parts.append(f"{label} {value}.")

        reason = " ".join(part.strip() for part in parts if part and part.strip()).strip()
        if not reason:
            reason = f"{severity.title()} clinical escalation triggered during chat."
        if len(reason) > 200:
            reason = reason[:197].rstrip(" ,.;") + "..."
        return reason

    def _maybe_create_escalation(self, severity: str) -> dict | None:
        if severity not in {"moderate", "serious"}:
            return None
        if self.user_id is None or not self._has_active_medication_context():
            return None
        if self.highest_escalation_severity is not None:
            if self._severity_rank(severity) <= self._severity_rank(self.highest_escalation_severity):
                return None

        from services.escalation_brief import generate_brief

        trigger_reason = self._build_trigger_reason(severity)
        brief = generate_brief(
            user_id=self.user_id,
            session_id=self.session_id,
            trigger_reason=trigger_reason,
            severity=severity,
            medication_id=int(self.active_medication["id"]) if self.active_medication else None,
        )
        escalation = database.create_escalation(
            user_id=self.user_id,
            session_id=self.session_id,
            severity=severity,
            trigger_reason=str(brief.get("trigger", {}).get("reason") or trigger_reason),
            brief_json=json.dumps(brief),
            medication_id=int(self.active_medication["id"]) if self.active_medication else None,
        )
        self.highest_escalation_severity = severity
        self.events.append({"type": "escalation_created", "escalation": escalation})
        return escalation

    _SYMPTOM_TIPS: dict[str, list[str]] = {
        "cold": [
            "Stay hydrated, rest well, and warm fluids like soup or tea can help.",
            "Plenty of fluids and rest should help. Honey in warm water can soothe things.",
            "Steam inhalation before bed can ease congestion. Try adding a drop of eucalyptus oil.",
            "Vitamin C-rich foods like oranges or bell peppers may help your body fight it off faster.",
            "A warm bowl of chicken soup is actually backed by science for helping with colds.",
            "Try to get extra sleep tonight. Your immune system does most of its work while you rest.",
        ],
        "cough": [
            "Honey in warm water or herbal tea can soothe a cough naturally.",
            "Try sleeping with your head slightly elevated to reduce nighttime coughing.",
            "Stay away from cold drinks for now. Warm liquids will help more.",
            "A spoonful of honey before bed can reduce cough frequency overnight.",
            "Keeping the air moist with a humidifier can help ease a dry cough.",
        ],
        "fever": [
            "Paracetamol (acetaminophen) can help bring down a mild fever. Stay hydrated.",
            "Light clothing and cool compresses on the forehead can provide relief.",
            "Drink more water than usual. Fever causes your body to lose fluids faster.",
            "Rest is key. Avoid strenuous activity until the fever breaks.",
            "A lukewarm bath can help lower body temperature naturally.",
            "Avoid bundling up in heavy blankets even if you feel cold. Let your body regulate.",
        ],
        "allergies": [
            "An OTC antihistamine like cetirizine or loratadine should help.",
            "Try to identify and avoid your triggers. Keep windows closed during high pollen days.",
            "Washing your face and hands after being outside can reduce allergen exposure.",
            "Nasal saline rinses can flush out irritants and provide quick relief.",
            "If you have been outside, changing clothes when you get home can reduce lingering allergens.",
            "Local honey is sometimes said to help build tolerance to local pollen over time.",
        ],
        "hives": [
            "A cool compress can soothe hives. Avoid hot showers for now.",
            "An antihistamine like cetirizine can help reduce the itching and swelling.",
            "Wear loose, soft clothing to avoid irritating the affected areas.",
        ],
        "rash": [
            "Keep the area clean and dry. A mild hydrocortisone cream may help with itching.",
            "Avoid scratching even if it itches. Try a cool compress instead.",
            "Fragrance-free moisturizer can help if the rash is from dry skin.",
        ],
        "itching": [
            "Calamine lotion or a cool compress can provide relief.",
            "An OTC antihistamine can help reduce itching from the inside out.",
            "Avoid hot water on itchy areas. Cool or lukewarm water is better.",
        ],
        "sneezing": [
            "An antihistamine can help if the sneezing is allergy-related.",
            "Keep tissues handy and try a saline nasal spray for relief.",
            "Peppermint tea can sometimes help open up nasal passages.",
        ],
        "headache": [
            "Paracetamol or ibuprofen can help. Make sure you are drinking enough water.",
            "Rest in a quiet, dark room if possible. Screen time can make headaches worse.",
            "Gentle pressure on your temples or the base of your skull can provide some relief.",
            "Dehydration is one of the most common headache causes. Try drinking a full glass of water.",
            "Caffeine in small amounts can sometimes help a headache, but too much can make it worse.",
            "A cold pack on your forehead for 15 minutes can numb the pain.",
        ],
        "sore throat": [
            "Warm salt water gargles and lozenges can help. Stay hydrated.",
            "Gargling with warm salt water 3-4 times a day is one of the most effective remedies.",
            "Cold foods like ice pops can actually numb a sore throat and reduce inflammation.",
            "Avoid whispering. It actually strains your throat more than talking normally.",
            "Marshmallow root tea has been used for centuries to soothe sore throats.",
        ],
        "nausea": [
            "Small sips of water or an ORS solution can help. Avoid heavy or spicy food.",
            "Ginger tea or ginger chews are a natural way to ease nausea.",
            "Try eating plain crackers or dry toast in small amounts.",
            "Fresh air and deep, slow breaths can help calm nausea.",
            "Peppermint tea or even just the scent of peppermint can help settle your stomach.",
        ],
        "diarrhea": [
            "Keep up your fluids. Bland foods like toast, rice, or bananas are easier on the stomach.",
            "An ORS solution is important to replace lost electrolytes.",
            "Avoid dairy, caffeine, and greasy foods until things settle.",
            "Probiotics like yogurt (once you can tolerate it) can help restore gut balance.",
        ],
            "body ache": [
            "Rest and paracetamol or ibuprofen should help with body aches.",
            "A warm bath or heating pad on sore areas can provide relief.",
            "Gentle stretching can help if the aches are muscular.",
            "Make sure you are staying hydrated. Dehydration can make aches worse.",
            "Magnesium-rich foods like bananas or dark chocolate may help with muscle soreness.",
        ],
        "dizziness": [
            "Sit or lie down until it passes. Stay hydrated and avoid sudden movements.",
            "If the dizziness is frequent, a doctor visit would be wise.",
            "Low blood sugar can cause dizziness. Try eating something small.",
            "Stand up slowly, especially after sitting or lying down for a while.",
        ],
        "fatigue": [
            "Good sleep, hydration, and balanced meals can make a big difference.",
            "Make sure you are getting enough rest. Your body may need recovery time.",
            "Even a short 15-minute walk can boost your energy when you are feeling tired.",
            "Check your iron intake. Low iron is a common cause of fatigue.",
            "Reducing screen time before bed can improve your sleep quality significantly.",
            "Try to keep a consistent sleep schedule, even on weekends.",
        ],
        "injection site reaction": [
            "Rotate your injection sites between your stomach, thigh, and upper arm. Avoid injecting in the same spot twice in a row.",
            "A small amount of redness or bruising at the injection site is normal and should resolve in a day or two.",
            "Make sure the medication is at room temperature before injecting. Cold injections tend to cause more discomfort.",
            "Ice the area for a minute before injecting. It can reduce both pain and bruising.",
            "If you notice a hard lump that does not go away after a week, mention it to your care team.",
        ],
        "constipation": [
            "GLP-1 medications slow gastric emptying, which can cause constipation. Increasing your water and fiber intake usually helps.",
            "Try to drink at least 8 glasses of water daily. GLP-1 medications can be dehydrating.",
            "Psyllium husk or a gentle fiber supplement can help keep things moving. Start with a small amount.",
            "Light physical activity like a daily walk can help with constipation.",
            "If it persists beyond two weeks, your care team may suggest a mild stool softener.",
        ],
        "bloating": [
            "Eat smaller, more frequent meals. GLP-1 medications slow digestion, so large meals can make bloating worse.",
            "Avoid carbonated drinks and chewing gum. Both introduce extra air into your digestive system.",
            "Peppermint tea after meals can help ease bloating.",
            "Try eating slowly and chewing thoroughly. It makes a bigger difference than most people expect.",
        ],
        "acid reflux": [
            "Avoid lying down for at least 2-3 hours after eating. Elevate the head of your bed if nighttime reflux is an issue.",
            "Smaller meals throughout the day put less pressure on your stomach. Avoid spicy and fatty foods.",
            "An OTC antacid can help in the short term. If reflux continues for more than a week, let your care team know.",
        ],
        "appetite change": [
            "Reduced appetite is actually one of the intended effects of GLP-1 medications. It is how the medication helps with weight management.",
            "Even if you are not hungry, try to eat small, protein-rich meals to maintain nutrition and energy.",
            "Focus on nutrient-dense foods: protein, vegetables, and healthy fats. You are eating less volume, so quality matters more.",
            "If you are struggling to eat anything at all for more than 2-3 days, let your care team know.",
        ],
        "stomach pain": [
            "Mild stomach discomfort is common with GLP-1 medications, especially in the first few weeks or after a dose increase.",
            "Eating smaller portions and avoiding high-fat meals can reduce stomach pain.",
            "If the pain is severe, sharp, or radiates to your back, contact your care team immediately. This could indicate a more serious issue.",
        ],
        "hair loss": [
            "Some patients notice mild hair thinning during rapid weight loss. This is usually temporary and related to the weight change, not the medication directly.",
            "Make sure you are getting enough protein: at least 60-80 grams per day. Protein deficiency during weight loss can contribute to hair thinning.",
            "A biotin supplement may help. Talk to your care team if the hair loss feels excessive.",
        ],
        "sulfur burps": [
            "Sulfur burps are a known side effect of GLP-1 medications, especially semaglutide. They are unpleasant but not dangerous.",
            "Avoid high-sulfur foods like eggs, broccoli, and garlic for a few weeks to see if it helps.",
            "Eating smaller meals and staying upright after eating can reduce sulfur burps.",
            "An OTC antacid or simethicone (Gas-X) can sometimes help. This side effect usually improves with time.",
        ],
        "pancreatitis": [
            "Severe stomach pain that radiates to your back could be a sign of pancreatitis. This is rare but serious. Please contact your care team or go to urgent care immediately.",
            "Do not wait on this. If the pain is severe and persistent, call 911 or go to the nearest ER.",
        ],
        "gallbladder pain": [
            "Rapid weight loss can increase the risk of gallstones. If you have sharp pain in your upper right abdomen, especially after eating, let your care team know.",
            "This needs medical evaluation. If the pain is severe, go to urgent care or the ER.",
        ],
    }

    def _suggest_remedies(self, severity: str) -> str | None:
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

        # ~40% chance of showing tips for mild cases — avoids being repetitive
        if random.random() > 0.4:
            return None

        tips: list[str] = []
        for symptom in self.symptoms:
            pool = self._SYMPTOM_TIPS.get(symptom)
            if not pool:
                # Check alias groups
                if symptom in ("hives", "rash", "itching", "sneezing"):
                    pool = self._SYMPTOM_TIPS.get(symptom) or self._SYMPTOM_TIPS.get("allergies")
                if not pool:
                    continue
            tips.append(self._pick(*pool))
            if len(tips) >= 2:
                break

        if not tips:
            return None

        advice = " ".join(tips)
        if self.active_medication and severity == "mild":
            glp1_expected = {
                "nausea",
                "constipation",
                "diarrhea",
                "bloating",
                "acid reflux",
                "appetite change",
                "injection site reaction",
                "sulfur burps",
            }
            if self.symptoms & glp1_expected:
                medication_name = self.active_medication.get("brand_name") or "your medication"
                normalizer = self._pick(
                    f"These are common side effects of {medication_name} and usually improve over time.",
                    f"Many patients on {medication_name} experience this, especially in the first few weeks.",
                    "This is a typical adjustment period. Most patients see improvement within a week or two.",
                )
                advice = f"{normalizer} {advice}"
        disclaimer = self._pick(
            "Of course, if things get worse, please see a doctor.",
            "These are just general tips. Consult a doctor if it persists.",
            "Keep an eye on how you feel over the next day or two.",
            "Just some pointers. You know your body best.",
            "Hope that helps. Let me know if you need anything else.",
        )
        return f"{advice} {disclaimer}"

    def _extract_entities_from_user_text(self, text: str) -> dict[str, Any]:
        recorded_at, time_label = self._extract_datetime(text)
        medication_drug_name, medication_brand_name = identify_drug(text)
        medication_mentioned = bool(
            medication_drug_name
            or medication_brand_name
            or re.search(r"\bglp[\s-]?1\b", text.lower())
        )
        symptoms = self._extract_symptoms(text)
        return {
            "name": self._extract_name(text),
            "age": self._extract_age(text),
            "weight_kg": self._extract_weight_kg(text),
            "height_cm": self._extract_height_cm(text),
            "address": self._extract_address(text),
            "symptoms": symptoms,
            "glp1_concern_level": self._assess_glp1_concern(symptoms),
            "medication_mention": {
                "drug_name": medication_drug_name,
                "brand_name": medication_brand_name,
            } if medication_mentioned else None,
            "dose_taken": self._reports_dose_taken(text),
            "dose_missed": self._reports_dose_missed(text),
            "refill_request": self._is_refill_request(text),
            "pharmacy_mention": self._extract_pharmacy(text),
            "dose_question": self._is_dose_question(text),
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

    def _assess_glp1_concern(self, symptoms: list[str]) -> str | None:
        """Classify GLP-1-specific symptom concern level."""
        if not self.active_medication or not symptoms:
            return None

        red_flags = {"pancreatitis", "gallbladder pain", "chest pain", "breathlessness"}
        expected_side_effects = {
            "nausea",
            "constipation",
            "diarrhea",
            "bloating",
            "acid reflux",
            "appetite change",
            "injection site reaction",
            "sulfur burps",
            "hair loss",
            "stomach pain",
        }

        symptom_set = set(symptoms)
        if symptom_set & red_flags:
            return "red_flag"
        if symptom_set & expected_side_effects:
            return "expected"
        return "general"

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
            "throwing up": "nausea",
            "throw up": "nausea",
            "can't keep anything down": "nausea",
            "cant keep anything down": "nausea",
            "cannot keep anything down": "nausea",
            "diarrhea": "diarrhea",
            "constipation": "constipation",
            "constipated": "constipation",
            "sore throat": "sore throat",
            "throat pain": "sore throat",
            "body ache": "body ache",
            "body pain": "body ache",
            "fatigue": "fatigue",
            "tiredness": "fatigue",
            "dizziness": "dizziness",
            "dizzy": "dizziness",
            "injection site": "injection site reaction",
            "injection site reaction": "injection site reaction",
            "injection site pain": "injection site reaction",
            "injection site redness": "injection site reaction",
            "injection site swelling": "injection site reaction",
            "injection site bruise": "injection site reaction",
            "injection site bruising": "injection site reaction",
            "bloating": "bloating",
            "bloated": "bloating",
            "gas": "bloating",
            "acid reflux": "acid reflux",
            "heartburn": "acid reflux",
            "reflux": "acid reflux",
            "gerd": "acid reflux",
            "loss of appetite": "appetite change",
            "no appetite": "appetite change",
            "not hungry": "appetite change",
            "can't eat": "appetite change",
            "food aversion": "appetite change",
            "reduced appetite": "appetite change",
            "decreased appetite": "appetite change",
            "appetite loss": "appetite loss",
            "stomach pain": "stomach pain",
            "abdominal pain": "stomach pain",
            "stomach cramps": "stomach pain",
            "stomach ache": "stomach pain",
            "hair loss": "hair loss",
            "hair thinning": "hair loss",
            "hair falling out": "hair loss",
            "sulfur burps": "sulfur burps",
            "egg burps": "sulfur burps",
            "sulphur burps": "sulfur burps",
            "pancreatitis": "pancreatitis",
            "severe stomach pain": "pancreatitis",
            "gallbladder": "gallbladder pain",
            "gallstones": "gallbladder pain",
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
        if "pancreatitis" in detected and "stomach pain" in detected:
            detected.remove("stomach pain")
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

        segment = re.sub(r"\b(\d)\s*[:]\s*(\d{2})\b", r"\1\2", segment)
        segment = re.sub(r"\b(\d{2})\s*[:]\s*(\d)\b", r"\1\2", segment)

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
            r"^(?:"
            r"no"
            r"|nope"
            r"|nah"
            r"|skip"
            r"|pass"
            r"|not really"
            r"|no i don'?t"
            r"|don'?t (?:know|have)(?: (?:it|that|one))?"
            r"|i don'?t"
            r"|i do not"
            r"|i don'?t have(?: [a-z0-9%/\-: ]+)?(?: right now)?"
            r"|i do not have(?: [a-z0-9%/\-: ]+)?(?: right now)?"
            r"|i haven'?t checked(?: yet| it)?"
            r"|haven'?t checked(?: yet| it)?"
            r"|not right now"
            r")[\s.!]*$",
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

    def _reports_dose_taken(self, text: str) -> bool:
        lowered = text.lower()
        patterns = (
            r"\btook my shot\b",
            r"\bdid my injection\b",
            r"\btook my dose\b",
            r"\binjected today\b",
            r"\bdid my shot\b",
            r"\bhad my injection\b",
            r"\btook (?:my )?(?:ozempic|wegovy|mounjaro|zepbound|semaglutide|tirzepatide)\b",
        )
        return any(re.search(pattern, lowered) for pattern in patterns)

    def _reports_dose_missed(self, text: str) -> bool:
        lowered = text.lower()
        patterns = (
            r"\bmissed my dose\b",
            r"\bmissed my shot\b",
            r"\bforgot my injection\b",
            r"\bforgot my dose\b",
            r"\bskipped this week\b",
            r"\bskipped my injection\b",
            r"\bmissed this week(?:'s)? shot\b",
        )
        return any(re.search(pattern, lowered) for pattern in patterns)

    def _is_refill_request(self, text: str) -> bool:
        lowered = text.lower()
        patterns = (
            r"\bneed a refill\b",
            r"\brunning out\b",
            r"\brunning low\b",
            r"\bneed more\b",
            r"\bprescription\b",
            r"\bpharmacy\b",
            r"\brenew\b",
            r"\brenewal\b",
            r"\bout of medication\b",
            r"\balmost out\b",
            r"\bneed my next\b",
            r"\bpen is almost empty\b",
            r"\bout of (?:medication|ozempic|wegovy|mounjaro|zepbound|semaglutide|tirzepatide)\b",
        )
        return any(re.search(pattern, lowered) for pattern in patterns)

    def _extract_pharmacy(self, text: str) -> str | None:
        known_pharmacies = {
            "cvs": "CVS",
            "walgreens": "Walgreens",
            "walmart": "Walmart",
            "rite aid": "Rite Aid",
            "costco": "Costco",
        }
        lowered = text.lower()
        for key, label in known_pharmacies.items():
            if re.search(rf"\b{re.escape(key)}\b", lowered):
                return label

        patterns = (
            r"\b(?:at|from)\s+([A-Za-z0-9&' .-]{2,60})",
            r"\bpharmacy(?:\s+is|\s+called|\s+name is)?\s+([A-Za-z0-9&' .-]{2,60})",
        )
        stop_markers = (" because ", " but ", " and ", ",", ".", " for ")
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            candidate = match.group(1).strip()
            lowered_candidate = candidate.lower()
            for marker in stop_markers:
                if marker in lowered_candidate:
                    candidate = candidate[:lowered_candidate.index(marker)].strip()
                    lowered_candidate = candidate.lower()
            if not candidate:
                continue
            if lowered_candidate in {"the pharmacy", "my pharmacy", "pharmacy"}:
                continue
            if lowered_candidate.endswith(" pharmacy") or "pharmacy" in lowered_candidate:
                return " ".join(part.capitalize() for part in candidate.split())
        return None

    def _is_dose_question(self, text: str) -> bool:
        lowered = text.lower()
        patterns = (
            r"\bwhat'?s my dose\b",
            r"\bwhat is my dose\b",
            r"\bwhen is my next dose\b",
            r"\bhow much should i take\b",
            r"\bwhen should i inject\b",
            r"\bwhat dose am i on\b",
            r"\bwhat should i take next\b",
        )
        return any(re.search(pattern, lowered) for pattern in patterns)

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

    def _has_active_medication_context(self) -> bool:
        return bool(
            self.authenticated
            and self.active_medication
            and self.active_medication.get("status") == "active"
        )

    def _medication_display_name(self) -> str:
        medication = self.active_medication or {}
        return str(
            medication.get("brand_name")
            or medication.get("drug_name")
            or "medication"
        )

    def _load_unresolved_side_effects(self) -> list[dict]:
        if self.user_id is None:
            self.unresolved_side_effects = []
            return self.unresolved_side_effects

        medication_id = None
        if self.active_medication and self.active_medication.get("id") is not None:
            medication_id = int(self.active_medication["id"])

        self.unresolved_side_effects = database.get_unresolved_side_effects(
            self.user_id,
            medication_id=medication_id,
        )
        return self.unresolved_side_effects

    def _side_effect_resolution_aliases(self, symptom: str) -> list[str]:
        normalized = str(symptom or "").strip().lower()
        aliases = {
            "nausea": ["nausea", "nauseous"],
            "diarrhea": ["diarrhea"],
            "constipation": ["constipation", "constipated"],
            "bloating": ["bloating", "bloated", "gas"],
            "acid reflux": ["acid reflux", "reflux", "heartburn", "gerd"],
            "appetite change": ["appetite change", "appetite", "loss of appetite", "no appetite", "not hungry"],
            "injection site reaction": [
                "injection site reaction",
                "injection site",
                "injection site pain",
                "injection site redness",
                "injection site swelling",
                "injection site bruise",
                "injection site bruising",
            ],
            "stomach pain": ["stomach pain", "abdominal pain", "stomach cramps", "stomach ache"],
            "hair loss": ["hair loss", "hair thinning", "hair falling out"],
            "sulfur burps": ["sulfur burps", "sulphur burps", "egg burps"],
            "gallbladder pain": ["gallbladder pain", "gallbladder", "gallstones"],
            "pancreatitis": ["pancreatitis", "severe stomach pain"],
        }.get(normalized, [normalized])

        deduped: list[str] = []
        for alias in aliases:
            clean_alias = str(alias or "").strip().lower()
            if clean_alias and clean_alias not in deduped:
                deduped.append(clean_alias)
        return deduped

    def _has_specific_resolution_phrase(self, lowered_text: str, symptom: str) -> bool:
        for alias in self._side_effect_resolution_aliases(symptom):
            escaped = re.escape(alias)
            persistence_patterns = (
                rf"\bstill\s+{escaped}\b",
                rf"\bstill\s+(?:have|having|got)\s+{escaped}\b",
                rf"\b{escaped}\s+(?:is\s+)?(?:still\s+there|not\s+better|worse|getting\s+worse)\b",
            )
            if any(re.search(pattern, lowered_text) for pattern in persistence_patterns):
                continue

            resolution_patterns = (
                rf"\b{escaped}\s+(?:is\s+)?(?:better|gone|resolved|improved)\b",
                rf"\b{escaped}\s+went\s+away\b",
                rf"\b{escaped}\s+stopped\b",
                rf"\b{escaped}\s+got\s+better\b",
                rf"\bno\s+more\s+{escaped}\b",
                rf"\bnot\s+{escaped}\s+anymore\b",
            )
            if any(re.search(pattern, lowered_text) for pattern in resolution_patterns):
                return True

        return False

    def _check_side_effect_resolution(self, user_text: str) -> list[str]:
        if self.user_id is None:
            return []

        unresolved = self._load_unresolved_side_effects()
        if not unresolved:
            return []

        lowered = user_text.lower()
        medication_id = int(self.active_medication["id"]) if self.active_medication and self.active_medication.get("id") is not None else None

        resolve_all_patterns = (
            r"\ball better\b",
            r"\beverything is fine now\b",
            r"\bside effects? are gone\b",
            r"\ball the side effects? are gone\b",
        )
        if any(re.search(pattern, lowered) for pattern in resolve_all_patterns):
            resolved_names = []
            for effect in unresolved:
                symptom = str(effect.get("symptom") or "").strip().lower()
                if symptom and symptom not in resolved_names:
                    resolved_names.append(symptom)
                    self.symptoms.discard(symptom)
            database.resolve_all_side_effects(self.user_id, medication_id=medication_id)
            self._load_unresolved_side_effects()
            return resolved_names

        resolved_symptoms: list[str] = []
        for effect in unresolved:
            symptom = str(effect.get("symptom") or "").strip().lower()
            if not symptom:
                continue
            if not self._has_specific_resolution_phrase(lowered, symptom):
                continue
            resolved = database.resolve_side_effect(int(effect["id"]), self.user_id)
            if resolved and symptom not in resolved_symptoms:
                resolved_symptoms.append(symptom)
                self.symptoms.discard(symptom)

        generic_better = bool(re.search(r"\bfeeling better\b", lowered))
        if generic_better and not resolved_symptoms:
            current_symptoms = set(self._extract_symptoms(user_text))
            if not current_symptoms:
                recent_effect = unresolved[0]
                recent_symptom = str(recent_effect.get("symptom") or "").strip().lower()
                resolved = database.resolve_side_effect(int(recent_effect["id"]), self.user_id)
                if resolved and recent_symptom:
                    resolved_symptoms.append(recent_symptom)
                    self.symptoms.discard(recent_symptom)

        if resolved_symptoms:
            self._load_unresolved_side_effects()
        return resolved_symptoms

    def _estimate_side_effect_severity(self, symptom: str, text: str) -> str:
        lowered = text.lower()
        severe_patterns = (
            r"\bsevere\b",
            r"\bterrible\b",
            r"\bawful\b",
            r"\bworst\b",
            r"\bcan'?t keep (?:anything|food|water) down\b",
            r"\bunable to\b",
        )
        moderate_patterns = (
            r"\bmoderate\b",
            r"\bpersistent\b",
            r"\bpretty bad\b",
            r"\bbad enough\b",
            r"\bkeeps coming back\b",
        )
        severe_symptoms = {"breathlessness", "chest pain"}
        if symptom in severe_symptoms or any(re.search(pattern, lowered) for pattern in severe_patterns):
            return "severe"
        if any(re.search(pattern, lowered) for pattern in moderate_patterns):
            return "moderate"
        return "mild"

    def _medication_interval_days(self) -> int | None:
        if not self.active_medication:
            return None
        frequency = str(self.active_medication.get("frequency") or "").strip().lower()
        if frequency == "weekly":
            return 7
        if frequency == "daily":
            return 1
        return None

    def _next_medication_due_date(self) -> datetime | None:
        if self.user_id is None or not self.active_medication:
            return None

        interval_days = self._medication_interval_days()
        if interval_days is None:
            return None

        medication_id = self.active_medication.get("id")
        if medication_id is None:
            return None

        events = database.get_medication_events(self.user_id, medication_id=medication_id, limit=25)
        for event in events:
            if event.get("event_type") != "dose_taken":
                continue
            recorded_at = event.get("recorded_at")
            if not recorded_at:
                continue
            try:
                anchor = datetime.fromisoformat(str(recorded_at))
            except ValueError:
                continue
            if anchor.tzinfo is None:
                anchor = anchor.replace(tzinfo=timezone.utc)
            next_due = anchor + timedelta(days=interval_days)
            while next_due < datetime.now(timezone.utc):
                next_due += timedelta(days=interval_days)
            return next_due

        start_date = self.active_medication.get("start_date")
        if not start_date:
            return None
        try:
            anchor = datetime.fromisoformat(f"{start_date}T00:00:00+00:00")
        except ValueError:
            return None

        next_due = anchor
        while next_due < datetime.now(timezone.utc):
            next_due += timedelta(days=interval_days)
        return next_due

    def _humanize_refill_status(self, status: str | None) -> str:
        normalized = str(status or "").strip().lower()
        return {
            "due": "due soon",
            "reminded": "already on our reminder list",
            "requested": "already requested",
            "flagged_for_team": "already flagged for your care team",
            "confirmed": "already confirmed and in progress",
            "completed": "already completed",
            "cancelled": "cancelled",
        }.get(normalized, normalized or "in progress")

    def _should_prompt_refill(self) -> tuple[bool, str | None]:
        if not self._has_active_medication_context() or self.user_id is None or self.refill_prompted_this_session:
            return False, None

        medication_id = self.active_medication.get("id") if self.active_medication else None
        if medication_id is None:
            return False, None

        active_refills = [
            refill for refill in database.get_active_refills(self.user_id)
            if int(refill.get("medication_id") or 0) == int(medication_id)
        ]
        if active_refills:
            return False, None

        due_date = database.calculate_next_refill_date(int(medication_id), self.user_id)
        if due_date is None:
            return False, None

        days_until_due = (due_date - datetime.now(timezone.utc).date()).days
        if days_until_due > 7:
            return False, None

        brand_name = self._medication_display_name()
        date_label = f"{due_date.strftime('%b')} {due_date.day}"
        return True, f"Your {brand_name} refill should be coming up around {date_label}."

    def _should_prompt_refill_help(self) -> bool:
        should_prompt, _ = self._should_prompt_refill()
        return should_prompt

    async def _handle_refill_request_turn(self, entities: dict[str, Any], user_text: str) -> dict[str, Any] | None:
        if self.user_id is None or not self.active_medication:
            return None

        self.prompted_missing_fields.discard("refill_check")
        medication_id = int(self.active_medication["id"])
        display_name = self._medication_display_name()
        patient_name = self.profile.get("name")
        pharmacy_name = entities.get("pharmacy_mention")
        active_refills = [
            refill for refill in database.get_active_refills(self.user_id)
            if int(refill.get("medication_id") or 0) == medication_id
        ]

        if active_refills:
            refill = active_refills[0]
            current_status = str(refill.get("status") or "due").strip().lower()
            if current_status in {"due", "reminded"}:
                refill_note = "User requested a refill during chat."
                refill = database.update_refill_status(
                    int(refill["id"]),
                    self.user_id,
                    "requested",
                    notes=refill_note,
                    pharmacy_name=pharmacy_name,
                )
                refill = database.update_refill_status(
                    int(refill["id"]),
                    self.user_id,
                    "flagged_for_team",
                    notes=refill_note,
                    pharmacy_name=pharmacy_name,
                )
                await self._tool_log_medication_event(
                    event_type="refill_due",
                    dose_mg=self.active_medication.get("current_dose_mg"),
                    scheduled_at=None,
                    notes=refill_note,
                )
                self.events.append({"type": "refill_flagged", "refill": refill})
                self.refill_prompted_this_session = True

                pharmacy_context = ""
                if pharmacy_name:
                    pharmacy_context = f" I noted {pharmacy_name} as your pharmacy."
                if patient_name:
                    base = (
                        f"Got it, {patient_name}. I've flagged your {display_name} refill for your care team. "
                        "They'll follow up to confirm."
                    )
                else:
                    base = (
                        f"I've flagged your {display_name} refill for your care team. "
                        "They'll follow up to confirm."
                    )
                return {
                    "handled": True,
                    "parts": [f"{base}{pharmacy_context}"],
                    "info": f"{display_name} refill flagged for the care team.",
                    "actions_taken": [
                        f"Flagged the {display_name} refill for the care team.",
                        *( [f"Noted {pharmacy_name} as the pharmacy."] if pharmacy_name else [] ),
                    ],
                }

            if pharmacy_name:
                try:
                    refill = database.update_refill_status(
                        int(refill["id"]),
                        self.user_id,
                        str(refill.get("status") or "due"),
                        notes=refill.get("notes"),
                        pharmacy_name=pharmacy_name,
                    ) or refill
                except ValueError:
                    pass

            status_phrase = self._humanize_refill_status(refill.get("status"))
            pharmacy_context = ""
            if refill.get("pharmacy_name"):
                pharmacy_context = f" I have {refill['pharmacy_name']} noted as the pharmacy."
            self.refill_prompted_this_session = True
            return {
                "handled": True,
                "parts": [f"Your {display_name} refill is {status_phrase}.{pharmacy_context}"],
                "info": f"Their {display_name} refill is currently {status_phrase}.",
                "actions_taken": [
                    f"Reviewed the current refill status for {display_name}: {status_phrase}.",
                    *( [f"Confirmed {refill['pharmacy_name']} as the pharmacy."] if refill.get("pharmacy_name") else [] ),
                ],
            }

        due_date = database.calculate_next_refill_date(medication_id, self.user_id)
        refill = database.create_refill_request(
            user_id=self.user_id,
            medication_id=medication_id,
            due_date=due_date,
        )
        refill_note = "User requested a refill during chat."
        refill = database.update_refill_status(
            int(refill["id"]),
            self.user_id,
            "requested",
            notes=refill_note,
            pharmacy_name=pharmacy_name,
        )
        refill = database.update_refill_status(
            int(refill["id"]),
            self.user_id,
            "flagged_for_team",
            notes=refill_note,
            pharmacy_name=pharmacy_name,
        )
        await self._tool_log_medication_event(
            event_type="refill_due",
            dose_mg=self.active_medication.get("current_dose_mg"),
            scheduled_at=None,
            notes=refill_note,
        )
        self.events.append({"type": "refill_flagged", "refill": refill})
        self.refill_prompted_this_session = True

        pharmacy_context = ""
        if pharmacy_name:
            pharmacy_context = f" I noted {pharmacy_name} as your pharmacy."
        if patient_name:
            base = (
                f"Got it, {patient_name}. I've flagged your {display_name} refill for your care team. "
                "They'll follow up to confirm."
            )
        else:
            base = (
                f"I've flagged your {display_name} refill for your care team. "
                "They'll follow up to confirm."
            )
        return {
            "handled": True,
            "parts": [f"{base}{pharmacy_context}"],
            "info": f"{display_name} refill flagged for the care team.",
            "actions_taken": [
                f"Created and flagged a new {display_name} refill request for the care team.",
                *( [f"Noted {pharmacy_name} as the pharmacy."] if pharmacy_name else [] ),
            ],
        }

    def _build_dose_question_response(self) -> str:
        if not self.active_medication:
            return self._pick(
                "I do not have an active medication on file for you yet.",
                "I am not seeing an active GLP-1 medication in your profile yet.",
            )

        drug_name = str(self.active_medication.get("drug_name") or "medication")
        display_name = self._medication_display_name()
        current_step_number = int(self.active_medication.get("titration_step") or 1)
        current_dose = self.active_medication.get("current_dose_mg")
        current_step = get_current_step(drug_name, current_step_number)
        next_step = get_next_step(drug_name, current_step_number)
        days_to_titration = days_until_next_titration(
            self.active_medication.get("start_date"),
            drug_name,
            current_step_number,
        )
        next_due = self._next_medication_due_date()

        parts = [
            f"You are currently on {display_name} {current_dose} mg at titration step {current_step_number}."
        ]

        if current_step and current_step.get("label"):
            parts.append(f"That is the {current_step['label'].lower()} in your schedule.")

        if next_due:
            days_until_due = max((next_due.date() - datetime.now(timezone.utc).date()).days, 0)
            if days_until_due == 0:
                parts.append("Your next dose is due today.")
            elif days_until_due == 1:
                parts.append("Your next dose is due in 1 day.")
            else:
                parts.append(f"Your next dose is due in {days_until_due} days.")

        if next_step and days_to_titration is not None:
            if days_to_titration == 0:
                parts.append(
                    f"Your next titration step is due now and would move you to {next_step['dose_mg']} mg."
                )
            elif days_to_titration == 1:
                parts.append(
                    f"Your next titration step is in 1 day, moving to {next_step['dose_mg']} mg."
                )
            else:
                parts.append(
                    f"Your next titration step is in {days_to_titration} days, moving to {next_step['dose_mg']} mg."
                )
        elif next_step:
            parts.append(f"The next step in the schedule is {next_step['dose_mg']} mg.")
        else:
            parts.append("You are at the maintenance step in the standard titration schedule.")

        return " ".join(parts)

    def _mentions_severe_symptom(self, text: str) -> bool:
        lowered = text.lower()
        severe_terms = ("chest pain", "shortness of breath", "breathlessness", "difficulty breathing")
        return any(term in lowered for term in severe_terms)

    def _next_pending_vital_question(self) -> str | None:
        vital_type = self._next_pending_vital_type()
        if vital_type is None:
            return None

        if vital_type == "blood_pressure":
            return self._pick(
                "I still need your blood pressure reading. Please share it as two numbers, like 122 over 78.",
                "Whenever you are ready, send your blood pressure as two numbers, like 122 over 78.",
            )
        if vital_type == "heart_rate":
            return self._pick(
                "I still need your heart rate. Please share one number in bpm, like 72.",
                "Whenever you are ready, send your heart rate as one number in bpm, like 72.",
            )
        if vital_type == "temperature":
            return self._pick(
                "If you have it, please share your temperature reading.",
                "Whenever you are ready, send your temperature reading.",
            )
        if vital_type == "oxygen_saturation":
            return self._pick(
                "If you have it, please share your oxygen saturation reading.",
                "Whenever you are ready, send your oxygen level, like 97 percent.",
            )

        return None

    def _next_pending_vital_prompt(self) -> tuple[str | None, str | None]:
        vital_type = self._next_pending_vital_type()
        if vital_type is None:
            return None, None

        if vital_type == "blood_pressure":
            return vital_type, self._pick(
                "I still need your blood pressure reading. Please share it as two numbers, like 122 over 78.",
                "Whenever you are ready, send your blood pressure as two numbers, like 122 over 78.",
            )
        if vital_type == "heart_rate":
            return vital_type, self._pick(
                "I still need your heart rate. Please share one number in bpm, like 72.",
                "Whenever you are ready, send your heart rate as one number in bpm, like 72.",
            )
        if vital_type == "temperature":
            return vital_type, self._pick(
                "If you have it, please share your temperature reading.",
                "Whenever you are ready, send your temperature reading.",
            )
        if vital_type == "oxygen_saturation":
            return vital_type, self._pick(
                "If you have it, please share your oxygen saturation reading.",
                "Whenever you are ready, send your oxygen level, like 97 percent.",
            )

        return None, None

    def _next_pending_vital_type(self) -> str | None:
        pending_order = (
            "blood_pressure",
            "heart_rate",
            "temperature",
            "oxygen_saturation",
        )
        for vital_type in pending_order:
            if vital_type not in self.prompted_missing_fields or vital_type in self.logged_vital_types:
                continue
            return vital_type

        return None

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
        self.consecutive_vital_declines = 0

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

    async def _tool_log_medication_event(
        self,
        event_type: str,
        dose_mg: float | None = None,
        scheduled_at: datetime | None = None,
        notes: str | None = None,
    ) -> dict:
        if self.user_id is None or not self.active_medication:
            return {"status": "error", "message": "Active medication context is required."}

        event = database.log_medication_event(
            medication_id=int(self.active_medication["id"]),
            user_id=self.user_id,
            event_type=event_type,
            dose_mg=dose_mg,
            scheduled_at=scheduled_at,
            notes=notes,
        )
        if event_type in {"dose_taken", "dose_missed", "dose_skipped"}:
            self.medication_dose_reported_this_session = True
        if event_type == "dose_missed":
            outreach_scheduler.schedule_missed_dose_followup(self.user_id, int(self.active_medication["id"]))
        elif event_type == "dose_adjusted":
            outreach_scheduler.schedule_titration_followup(
                self.user_id,
                int(self.active_medication["id"]),
                int(self.active_medication.get("titration_step") or 1),
                float(dose_mg if dose_mg is not None else self.active_medication.get("current_dose_mg") or 0),
            )
        if event_type in {"dose_missed", "dose_skipped"}:
            at_risk, reason = outreach_scheduler.detect_churn_risk(self.user_id)
            if at_risk:
                outreach_scheduler.schedule_retention_risk_outreach(
                    self.user_id,
                    int(self.active_medication["id"]),
                    reason,
                )
        self.events.append({"type": "medication_event_logged", "event": event})
        return {"status": "logged", "event": event}

    async def _tool_log_side_effect(self, symptom: str, raw_text: str) -> dict:
        if self.user_id is None:
            return {"status": "error", "message": "User context is required to log a side effect."}

        severity = self._estimate_side_effect_severity(symptom, raw_text)
        side_effect = database.log_side_effect(
            user_id=self.user_id,
            medication_id=int(self.active_medication["id"]) if self.active_medication else None,
            symptom=symptom,
            severity=severity,
            titration_step=(
                int(self.active_medication["titration_step"])
                if self.active_medication and self.active_medication.get("titration_step") is not None
                else None
            ),
            notes="User-reported during chat.",
        )
        self._load_unresolved_side_effects()
        self.events.append({"type": "side_effect_logged", "side_effect": side_effect})
        return {"status": "logged", "side_effect": side_effect}

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
        if self.user_id is not None and doctors:
            saved = database.save_doctor_recommendations(
                user_id=self.user_id,
                session_id=self.session_id,
                doctors=doctors,
            )
            self.events.append({"type": "doctors_saved", "count": len(saved)})

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

    def identify_user(self, name: str, age: int | None = None, from_auth: bool = False, user_id: int | None = None):
        if user_id is not None:
            user = database.get_user_by_id(user_id)
            if user is None:
                return
        else:
            user = database.get_or_create_user(name, age)
        self.user_id = user.id
        self.user_name = user.name
        self.profile["name"] = user.name
        if age is not None:
            self.profile["age"] = age
        if from_auth:
            self.authenticated = True
            self._check_vitals_cooldown()
            active_medications = database.get_active_medications(self.user_id)
            self.active_medication = active_medications[0] if active_medications else None
            self.medication_context_loaded = True
            if self.active_medication:
                self._load_unresolved_side_effects()
            else:
                self.unresolved_side_effects = []
        database.update_session_user(self.session_id, self.user_id)
        database.attach_session_messages_to_user(self.session_id, self.user_id)

    def _check_vitals_cooldown(self):
        COOLDOWN_HOURS = 6
        if self.user_id is None:
            return
        last_ts = database.get_latest_vital_timestamp(self.user_id)
        if last_ts is None:
            return
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=timezone.utc)
        elapsed = datetime.now(timezone.utc) - last_ts
        if elapsed < timedelta(hours=COOLDOWN_HOURS):
            self.vitals_on_cooldown = True

    def end_session(self):
        database.close_session(self.session_id)
