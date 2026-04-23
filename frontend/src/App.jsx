import { useEffect, useRef, useState } from "react";
import {
  checkBackendHealth,
  checkSttHealth,
  deleteVital,
  enhanceTranscript,
  fetchConversationHistory,
  fetchMedication,
  fetchMedications,
  fetchRefills,
  fetchSideEffectTimeline,
  fetchVitalsHistory,
  resolveWebSocketUrl,
  transcribeAudioBlob,
} from "./api";

function groupVitalsByDate(vitals, timeZone = null) {
  const groups = {};
  for (const vital of vitals) {
    const dateKey = normalizeIsoToDateKey(vital.recorded_at, timeZone || undefined);
    if (!groups[dateKey]) groups[dateKey] = [];
    groups[dateKey].push(vital);
  }
  return Object.entries(groups).sort((a, b) => b[0].localeCompare(a[0]));
}

function formatDateLabel(dateStr, timeZone = null) {
  if (!dateStr || dateStr === "Unknown") {
    return "Unknown";
  }

  const keyMatch = /^(\d{4})-(\d{2})-(\d{2})$/.exec(dateStr);
  if (keyMatch) {
    return `${keyMatch[2]}/${keyMatch[3]}/${keyMatch[1]}`;
  }

  const parsed = new Date(dateStr);
  if (Number.isNaN(parsed.getTime())) {
    return dateStr;
  }
  try {
    return new Intl.DateTimeFormat("en-US", {
      timeZone: timeZone || Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).format(parsed);
  } catch (_) {
    return parsed.toLocaleDateString("en-US", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    });
  }
}

function normalizeIsoToDateKey(isoValue, timeZone) {
  if (!isoValue) {
    return "Unknown";
  }
  const parsed = new Date(isoValue);
  if (Number.isNaN(parsed.getTime())) {
    return "Unknown";
  }
  try {
    const formatter = new Intl.DateTimeFormat("en-CA", {
      timeZone: timeZone || Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    });
    const parts = formatter.formatToParts(parsed);
    const year = parts.find((part) => part.type === "year")?.value;
    const month = parts.find((part) => part.type === "month")?.value;
    const day = parts.find((part) => part.type === "day")?.value;
    if (year && month && day) {
      return `${year}-${month}-${day}`;
    }
  } catch (_) {
    // fallback below
  }
  return parsed.toISOString().split("T")[0];
}

function formatClockTime(isoValue, timeZone) {
  if (!isoValue) {
    return "--:--";
  }
  const parsed = new Date(isoValue);
  if (Number.isNaN(parsed.getTime())) {
    return "--:--";
  }
  try {
    return new Intl.DateTimeFormat("en-US", {
      timeZone: timeZone || Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
      hour: "numeric",
      minute: "2-digit",
      hour12: true,
    }).format(parsed);
  } catch (_) {
    return parsed.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  }
}
import AuthPage from "./AuthPage";
import ClinicDashboard from "./ClinicDashboard";
import MedicationCard from "./MedicationCard";
import ProfileCard from "./ProfileCard";
import SideEffectTimeline from "./SideEffectTimeline";
import VitalsGraph from "./VitalsGraph";

const VITAL_LABELS = {
  blood_pressure: "Blood Pressure",
  heart_rate: "Heart Rate",
  temperature: "Temperature",
  blood_glucose: "Blood Glucose",
  oxygen_saturation: "SpO2",
  weight: "Weight",
  bmi: "BMI",
  symptom: "Symptom",
  other: "Other",
};

const NAV_ITEMS = [
  { key: "profile", label: "Profile", icon: "user" },
  { key: "vitals", label: "Vitals", icon: "vitals" },
  { key: "graphs", label: "Graphs", icon: "graph" },
  { key: "conversations", label: "Conversations", icon: "history" },
  { key: "doctors", label: "Doctors", icon: "stethoscope" },
];

const PANEL_TITLES = {
  profile: "Profile",
  vitals: "Vitals History",
  graphs: "Health Graphs",
  conversations: "Conversations",
  doctors: "Nearby Doctors",
};

const PRIMARY_VIEWS = [
  { key: "chat", label: "Chat" },
  { key: "dashboard", label: "Dashboard" },
  { key: "health", label: "My Health" },
];

const MIN_CONFIDENCE_FOR_BACKEND_FALLBACK = 0.58;
const MIN_AUDIO_BYTES_FOR_BACKEND_TRANSCRIBE = 1500;
const STT_EXPECTED_KEYWORDS = [
  "name",
  "age",
  "symptoms",
  "allergies",
  "hives",
  "weight",
  "height",
  "address",
  "blood pressure",
  "heart rate",
  "temperature",
];
const STT_CUSTOM_TERMS = ["Playa", "Norte", "Tempe", "Arizona"];

function App() {
  const [authToken, setAuthToken] = useState(localStorage.getItem("cc_token"));
  const [authUser, setAuthUser] = useState(() => {
    try { return JSON.parse(localStorage.getItem("cc_user")); } catch { return null; }
  });

  const [status, setStatus] = useState({ mode: "", text: "Connecting" });
  const [messages, setMessages] = useState([]);
  const [typing, setTyping] = useState(false);
  const [inputValue, setInputValue] = useState("");
  const [micEnabled, setMicEnabled] = useState(false);
  const [micHint, setMicHint] = useState("Connect first to enable voice input.");
  const [canSpeak, setCanSpeak] = useState(true);
  const [canListen, setCanListen] = useState(true);
  const [autoListenEnabled, setAutoListenEnabled] = useState(true);
  const [isListening, setIsListening] = useState(false);
  const [vitals, setVitals] = useState([]);
  const [conversationHistory, setConversationHistory] = useState([]);
  const [doctors, setDoctors] = useState([]);
  const [activeView, setActiveView] = useState("chat");
  const [activePanel, setActivePanel] = useState(null);
  const [selectedConversationSession, setSelectedConversationSession] = useState(null);
  const [activeMedication, setActiveMedication] = useState(null);
  const [activeRefills, setActiveRefills] = useState([]);
  const [sideEffectTimeline, setSideEffectTimeline] = useState([]);
  const [healthLoading, setHealthLoading] = useState(false);
  const [healthError, setHealthError] = useState("");
  const pendingMessageRef = useRef(null);
  const micDeniedRef = useRef(false);
  const recognitionResultHandledRef = useRef(false);
  const lastSubmittedMessageRef = useRef({ normalized: "", timestamp: 0, source: "" });
  const autoListenArmedRef = useRef(false);
  const autoListenEnabledRef = useRef(true);
  const recognitionStartInFlightRef = useRef(false);

  function handleAuth(token, user) {
    localStorage.setItem("cc_token", token);
    localStorage.setItem("cc_user", JSON.stringify(user));
    setAuthToken(token);
    setAuthUser(user);
  }

  function handleLogout() {
    localStorage.removeItem("cc_token");
    localStorage.removeItem("cc_user");
    setAuthToken(null);
    setAuthUser(null);
    setConversationHistory([]);
    setVitals([]);
    setDoctors([]);
    setActiveMedication(null);
    setActiveRefills([]);
    setSideEffectTimeline([]);
    setHealthLoading(false);
    setHealthError("");
    setActiveView("chat");
    setSelectedConversationSession(null);
    setMessages([]);
    autoListenArmedRef.current = false;
    setActivePanel(null);
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      try {
        wsRef.current.send(JSON.stringify({ type: "end_session" }));
      } catch (_) {}
      wsRef.current.close();
    }
  }

  function handleProfileUpdate(updatedUser) {
    const merged = { ...authUser, ...updatedUser };
    localStorage.setItem("cc_user", JSON.stringify(merged));
    setAuthUser(merged);
  }

  const wsRef = useRef(null);
  const recognitionRef = useRef(null);
  const userLocationRef = useRef(null);
  const audioRef = useRef(null);
  const transcriptBottomRef = useRef(null);
  const isListeningRef = useRef(false);
  const isSpeakingRef = useRef(false);
  const canSpeakRef = useRef(true);
  const canListenRef = useRef(true);
  const awaitingAgentReplyRef = useRef(false);
  const micStreamRef = useRef(null);
  const audioContextRef = useRef(null);
  const micSourceRef = useRef(null);
  const micAnalyserRef = useRef(null);
  const micLevelTimerRef = useRef(null);
  const micReleaseTimerRef = useRef(null);
  const ambientNoiseLevelRef = useRef(0);
  const mediaRecorderRef = useRef(null);
  const recordingChunksRef = useRef([]);
  const pendingRecordingResolverRef = useRef(null);
  const sttTranscribeAvailableRef = useRef(false);

  useEffect(() => {
    if (transcriptBottomRef.current) {
      transcriptBottomRef.current.scrollIntoView({ behavior: "smooth" });
    }
  }, [messages, typing]);

  useEffect(() => {
    function onBeforeUnload() {
      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
        try {
          wsRef.current.send(JSON.stringify({ type: "end_session" }));
        } catch (error) {
          // ignore network shutdown errors
        }
        wsRef.current.close();
      }
    }

    window.addEventListener("beforeunload", onBeforeUnload);
    return function cleanup() {
      window.removeEventListener("beforeunload", onBeforeUnload);
      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
        wsRef.current.close();
      }
      stopMicEnhancementPipeline();
    };
  }, []);

  useEffect(() => {
    if (authToken) {
      connectSession();
    }
  }, [authToken]);

  useEffect(() => {
    if (authToken) {
      void refreshHealthData(authToken, { silent: true });
    }
  }, [authToken]);

  useEffect(() => {
    if (activeView !== "chat") {
      setActivePanel(null);
    }
  }, [activeView]);

  useEffect(
    function bindConversationModalEscape() {
      if (!selectedConversationSession) {
        return undefined;
      }

      function onKeyDown(event) {
        if (event.key === "Escape") {
          setSelectedConversationSession(null);
        }
      }

      window.addEventListener("keydown", onKeyDown);
      return function cleanup() {
        window.removeEventListener("keydown", onKeyDown);
      };
    },
    [selectedConversationSession]
  );

  useEffect(() => {
    canSpeakRef.current = canSpeak;
    canListenRef.current = canListen;

    if (!canSpeak && isListeningRef.current && recognitionRef.current) {
      recognitionRef.current.stop();
    }

    if (!canListen && audioRef.current) {
      audioRef.current.pause();
      audioRef.current = null;
      isSpeakingRef.current = false;
      updateStatus("connected", "Connected");
    }

    syncInteractionState();
  }, [canSpeak, canListen]);

  useEffect(() => {
    autoListenEnabledRef.current = autoListenEnabled;
    if (!autoListenEnabled) {
      autoListenArmedRef.current = false;
    }
    syncInteractionState();
  }, [autoListenEnabled]);

  function updateStatus(mode, text) {
    setStatus({ mode, text });
  }

  function buildEnhancedAudioConstraints() {
    const supported = navigator.mediaDevices && navigator.mediaDevices.getSupportedConstraints
      ? navigator.mediaDevices.getSupportedConstraints()
      : {};
    const audio = {};

    if (supported.echoCancellation) {
      audio.echoCancellation = true;
    }
    if (supported.noiseSuppression) {
      audio.noiseSuppression = true;
    }
    if (supported.autoGainControl) {
      audio.autoGainControl = true;
    }
    if (supported.channelCount) {
      audio.channelCount = 1;
    }
    if (supported.sampleRate) {
      audio.sampleRate = 48000;
    }
    if (supported.sampleSize) {
      audio.sampleSize = 16;
    }

    return { audio };
  }

  function resolvePreferredRecorderMimeType() {
    if (typeof MediaRecorder === "undefined" || !MediaRecorder.isTypeSupported) {
      return "";
    }
    const candidates = [
      "audio/webm;codecs=opus",
      "audio/webm",
      "audio/mp4",
    ];
    for (let i = 0; i < candidates.length; i += 1) {
      if (MediaRecorder.isTypeSupported(candidates[i])) {
        return candidates[i];
      }
    }
    return "";
  }

  function resolvePendingVoiceCapture(blob) {
    if (pendingRecordingResolverRef.current) {
      const resolver = pendingRecordingResolverRef.current;
      pendingRecordingResolverRef.current = null;
      resolver(blob);
    }
  }

  function startVoiceSegmentCapture() {
    if (!sttTranscribeAvailableRef.current) {
      return;
    }
    if (!micStreamRef.current || typeof MediaRecorder === "undefined") {
      return;
    }
    if (mediaRecorderRef.current && mediaRecorderRef.current.state === "recording") {
      return;
    }

    const mimeType = resolvePreferredRecorderMimeType();
    recordingChunksRef.current = [];

    try {
      const recorder = mimeType
        ? new MediaRecorder(micStreamRef.current, { mimeType, audioBitsPerSecond: 128000 })
        : new MediaRecorder(micStreamRef.current);

      recorder.ondataavailable = function (event) {
        if (event.data && event.data.size > 0) {
          recordingChunksRef.current.push(event.data);
        }
      };

      recorder.onstop = function () {
        const blob = recordingChunksRef.current.length
          ? new Blob(recordingChunksRef.current, {
              type: recorder.mimeType || "audio/webm",
            })
          : null;
        recordingChunksRef.current = [];
        mediaRecorderRef.current = null;
        resolvePendingVoiceCapture(blob);
      };

      recorder.onerror = function () {
        recordingChunksRef.current = [];
        mediaRecorderRef.current = null;
        resolvePendingVoiceCapture(null);
      };

      mediaRecorderRef.current = recorder;
      recorder.start(250);
    } catch (error) {
      mediaRecorderRef.current = null;
      recordingChunksRef.current = [];
    }
  }

  async function stopVoiceSegmentCapture() {
    if (!mediaRecorderRef.current) {
      return null;
    }

    const recorder = mediaRecorderRef.current;
    if (recorder.state !== "recording") {
      const blob = recordingChunksRef.current.length
        ? new Blob(recordingChunksRef.current, {
            type: recorder.mimeType || "audio/webm",
          })
        : null;
      recordingChunksRef.current = [];
      mediaRecorderRef.current = null;
      return blob;
    }

    return new Promise(function (resolve) {
      let resolved = false;
      const timer = window.setTimeout(function () {
        if (resolved) {
          return;
        }
        resolved = true;
        pendingRecordingResolverRef.current = null;
        recordingChunksRef.current = [];
        resolve(null);
      }, 1800);

      pendingRecordingResolverRef.current = function (blob) {
        if (resolved) {
          return;
        }
        resolved = true;
        clearTimeout(timer);
        resolve(blob);
      };

      try {
        recorder.stop();
      } catch (error) {
        clearTimeout(timer);
        pendingRecordingResolverRef.current = null;
        recordingChunksRef.current = [];
        resolve(null);
      }
    });
  }

  function clearMicReleaseTimer() {
    if (micReleaseTimerRef.current) {
      clearTimeout(micReleaseTimerRef.current);
      micReleaseTimerRef.current = null;
    }
  }

  function scheduleMicRelease(delayMs = 800) {
    clearMicReleaseTimer();
    micReleaseTimerRef.current = window.setTimeout(function () {
      micReleaseTimerRef.current = null;
      if (isListeningRef.current || isSpeakingRef.current || awaitingAgentReplyRef.current) {
        return;
      }
      stopMicEnhancementPipeline();
      syncInteractionState();
    }, delayMs);
  }

  function stopMicEnhancementPipeline() {
    clearMicReleaseTimer();
    if (mediaRecorderRef.current) {
      try {
        if (mediaRecorderRef.current.state === "recording") {
          mediaRecorderRef.current.stop();
        }
      } catch (error) {
        // ignore recorder stop errors
      }
      mediaRecorderRef.current = null;
    }
    recordingChunksRef.current = [];
    pendingRecordingResolverRef.current = null;

    if (micLevelTimerRef.current) {
      clearInterval(micLevelTimerRef.current);
      micLevelTimerRef.current = null;
    }

    if (micSourceRef.current) {
      try {
        micSourceRef.current.disconnect();
      } catch (error) {
        // ignore disconnect errors
      }
      micSourceRef.current = null;
    }

    if (micAnalyserRef.current) {
      try {
        micAnalyserRef.current.disconnect();
      } catch (error) {
        // ignore disconnect errors
      }
      micAnalyserRef.current = null;
    }

    if (audioContextRef.current) {
      audioContextRef.current.close().catch(function () {
        // ignore close errors
      });
      audioContextRef.current = null;
    }

    if (micStreamRef.current) {
      micStreamRef.current.getTracks().forEach(function (track) {
        track.stop();
      });
      micStreamRef.current = null;
    }

    ambientNoiseLevelRef.current = 0;
  }

  function startMicNoiseMonitor(stream) {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass || !stream) {
      return;
    }

    if (micLevelTimerRef.current) {
      clearInterval(micLevelTimerRef.current);
      micLevelTimerRef.current = null;
    }

    if (audioContextRef.current) {
      audioContextRef.current.close().catch(function () {
        // ignore close errors
      });
      audioContextRef.current = null;
    }

    const context = new AudioContextClass();
    const source = context.createMediaStreamSource(stream);
    const analyser = context.createAnalyser();

    analyser.fftSize = 1024;
    analyser.smoothingTimeConstant = 0.86;

    source.connect(analyser);

    const buffer = new Uint8Array(analyser.fftSize);

    micSourceRef.current = source;
    micAnalyserRef.current = analyser;
    audioContextRef.current = context;

    micLevelTimerRef.current = window.setInterval(function () {
      if (!micAnalyserRef.current) {
        return;
      }

      micAnalyserRef.current.getByteTimeDomainData(buffer);
      let sum = 0;
      for (let i = 0; i < buffer.length; i += 1) {
        const normalized = (buffer[i] - 128) / 128;
        sum += normalized * normalized;
      }

      const rms = Math.sqrt(sum / buffer.length);
      ambientNoiseLevelRef.current = (ambientNoiseLevelRef.current * 0.85) + (rms * 0.15);
    }, 120);
  }

  async function configureEnhancedMicrophone() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      return "unsupported";
    }

    try {
      clearMicReleaseTimer();
      const constraints = buildEnhancedAudioConstraints();

      if (!micStreamRef.current) {
        micStreamRef.current = await navigator.mediaDevices.getUserMedia(constraints);
      }

      const track = micStreamRef.current.getAudioTracks()[0];
      if (track && track.applyConstraints) {
        try {
          await track.applyConstraints(constraints.audio);
        } catch (error) {
          // Some browsers partially support constraints.
        }
      }

      startMicNoiseMonitor(micStreamRef.current);
      return "enhanced";
    } catch (error) {
      stopMicEnhancementPipeline();
      return "failed";
    }
  }

  function pickBestAlternative(result) {
    let best = result[0];
    for (let i = 1; i < result.length; i += 1) {
      const candidate = result[i];
      const bestConfidence = typeof best.confidence === "number" ? best.confidence : 0;
      const candidateConfidence = typeof candidate.confidence === "number" ? candidate.confidence : 0;
      if (candidateConfidence > bestConfidence) {
        best = candidate;
      }
    }
    return best;
  }

  function getModeLabel() {
    if (canSpeak && canListen) {
      return "Voice <-> Voice";
    }
    if (canSpeak && !canListen) {
      return "Voice -> Chat";
    }
    if (!canSpeak && canListen) {
      return "Chat -> Voice";
    }
    return "Chat <-> Chat";
  }

  function appendMessage(role, text) {
    setMessages(function (prev) {
      return prev.concat({
        id: String(Date.now()) + Math.random().toString(16).slice(2),
        role,
        text,
        createdAt: new Date().toISOString(),
      });
    });
  }

  function disableInteraction() {
    setMicEnabled(canSpeakRef.current);
    setMicHint("Session ended. Tap mic or type to start a new one.");
  }

  function syncInteractionState() {
    const isConnected = wsRef.current && wsRef.current.readyState === WebSocket.OPEN;

    if (!isConnected) {
      setMicEnabled(canSpeakRef.current);
      setMicHint("Tap mic or type to start.");
      return;
    }

    if (!canSpeakRef.current) {
      setMicEnabled(false);
      setMicHint("Voice input is off. Use chat input.");
      return;
    }

    if (!recognitionRef.current) {
      setMicEnabled(false);
      setMicHint("Voice input is unavailable in this browser.");
      return;
    }

    setMicEnabled(!isSpeakingRef.current);
    const autoListenReady =
      autoListenEnabledRef.current &&
      autoListenArmedRef.current &&
      !isSpeakingRef.current &&
      !isListeningRef.current &&
      !awaitingAgentReplyRef.current;
    setMicHint(
      isSpeakingRef.current
        ? "Assistant is speaking..."
        : autoListenReady
          ? "Auto-listen is ready for the next reply."
          : autoListenEnabledRef.current
            ? "Tap the mic button when you want to speak."
            : "Auto-listen is off. Tap the mic button when ready."
    );
  }

  function requestLocation() {
    if (!navigator.geolocation) {
      return;
    }

    navigator.geolocation.getCurrentPosition(
      function (pos) {
        userLocationRef.current = {
          latitude: pos.coords.latitude,
          longitude: pos.coords.longitude,
        };

        if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
          wsRef.current.send(
            JSON.stringify({
              type: "location",
              latitude: userLocationRef.current.latitude,
              longitude: userLocationRef.current.longitude,
            })
          );
        }
      },
      function () {
        userLocationRef.current = null;
      }
    );
  }

  function getClientTimezoneContext() {
    const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
    const utcOffsetMinutes = -new Date().getTimezoneOffset();
    return {
      timezone,
      utc_offset_minutes: utcOffsetMinutes,
    };
  }

  async function refreshConversationHistory(tokenOverride = null) {
    const activeToken = tokenOverride || authToken;
    if (!activeToken) {
      return;
    }
    try {
      const history = await fetchConversationHistory(activeToken, { limitSessions: 30 });
      if (Array.isArray(history)) {
        setConversationHistory(history);
      }
    } catch (_) {
      // keep previous history on transient failures
    }
  }

  async function refreshHealthData(tokenOverride = null, options = {}) {
    const activeToken = tokenOverride || authToken;
    if (!activeToken) {
      return;
    }

    if (!options.silent) {
      setHealthLoading(true);
    }
    setHealthError("");

    const [medicationsResult, sideEffectsResult, refillsResult] = await Promise.allSettled([
      fetchMedications(activeToken),
      fetchSideEffectTimeline(activeToken),
      fetchRefills(activeToken),
    ]);

    const tokenExpired =
      (medicationsResult.status === "rejected" && medicationsResult.reason?.status === 401) ||
      (sideEffectsResult.status === "rejected" && sideEffectsResult.reason?.status === 401) ||
      (refillsResult.status === "rejected" && refillsResult.reason?.status === 401);

    if (tokenExpired) {
      handleLogout();
      return;
    }

    let nextMedication = null;
    let nextError = "";

    if (medicationsResult.status === "fulfilled" && Array.isArray(medicationsResult.value) && medicationsResult.value.length > 0) {
      const summaryMedication = medicationsResult.value[0];
      try {
        nextMedication = await fetchMedication(activeToken, summaryMedication.id);
      } catch (err) {
        if (err && err.status === 401) {
          handleLogout();
          return;
        }
        nextMedication = summaryMedication;
        nextError = err?.message || "";
      }
    }

    if (sideEffectsResult.status === "fulfilled" && Array.isArray(sideEffectsResult.value)) {
      setSideEffectTimeline(sideEffectsResult.value);
    } else if (!nextError) {
      nextError = sideEffectsResult.reason?.message || "";
    }

    if (refillsResult.status === "fulfilled" && Array.isArray(refillsResult.value)) {
      setActiveRefills(refillsResult.value);
    } else if (!nextError) {
      nextError = refillsResult.reason?.message || "";
    }

    setActiveMedication(nextMedication);
    if (nextError) {
      setHealthError(nextError);
    }
    setHealthLoading(false);
  }

  function sendUserMessage(text) {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
      return false;
    }

    const timezoneContext = getClientTimezoneContext();
    awaitingAgentReplyRef.current = true;
    wsRef.current.send(
      JSON.stringify({
        type: "user_message",
        text,
        location: userLocationRef.current,
        timezone: timezoneContext.timezone,
        utc_offset_minutes: timezoneContext.utc_offset_minutes,
      })
    );
    return true;
  }

  function normalizeForDuplicateCheck(text) {
    return (text || "")
      .toLowerCase()
      .replace(/[^a-z0-9\s]/g, " ")
      .replace(/\s+/g, " ")
      .trim();
  }

  function shouldSkipDuplicateMessage(text, source) {
    const normalized = normalizeForDuplicateCheck(text);
    if (!normalized) {
      return false;
    }

    const last = lastSubmittedMessageRef.current;
    if (!last || last.normalized !== normalized) {
      return false;
    }

    const elapsedMs = Date.now() - (last.timestamp || 0);
    const shortCloser = /^(thanks|thank you|thx|ok|okay|sure|bye|goodbye|see you|take care)$/.test(normalized);
    if (source === "voice" && elapsedMs < 5000) {
      return true;
    }
    if (shortCloser && elapsedMs < 2500) {
      return true;
    }
    return false;
  }

  async function enhanceVoiceTranscript(rawText) {
    try {
      const result = await enhanceTranscript(rawText, {
        expectedKeywords: STT_EXPECTED_KEYWORDS,
        customTerms: STT_CUSTOM_TERMS,
      });
      const enhanced = (result && result.transcript ? result.transcript : "").trim();
      if (enhanced) {
        return enhanced;
      }
    } catch (error) {
      // Use the original transcript if STT enhancement is unavailable.
    }
    return rawText;
  }

  function collectDynamicCustomTerms(seedTranscript) {
    const dynamic = [];
    const combined = [seedTranscript || ""];
    for (let i = messages.length - 1; i >= 0 && combined.length < 6; i -= 1) {
      const msg = messages[i];
      if (msg.role === "user" && msg.text) {
        combined.push(msg.text);
      }
    }

    combined.forEach(function (value) {
      const matches = value.match(/\b[A-Z][a-zA-Z]{2,}\b/g) || [];
      matches.forEach(function (token) {
        if (dynamic.length < 20 && !dynamic.includes(token)) {
          dynamic.push(token);
        }
      });
    });

    return STT_CUSTOM_TERMS.concat(dynamic);
  }

  async function transcribeCapturedVoice(audioBlob, seedTranscript) {
    if (!audioBlob || audioBlob.size < MIN_AUDIO_BYTES_FOR_BACKEND_TRANSCRIBE) {
      return "";
    }
    if (!sttTranscribeAvailableRef.current) {
      return "";
    }

    try {
      const result = await transcribeAudioBlob(audioBlob, {
        expectedKeywords: STT_EXPECTED_KEYWORDS,
        customTerms: collectDynamicCustomTerms(seedTranscript),
        filename: "voice.webm",
        language: "en",
      });
      return (result && result.transcript ? result.transcript : "").trim();
    } catch (error) {
      return "";
    }
  }

  async function submitUserMessage(rawText, source) {
    const text = (rawText || "").trim();
    if (!text) {
      return;
    }

    const isRetry = source === "retry";
    setTyping(true);

    let outboundText = text;
    if (source === "voice") {
      outboundText = await enhanceVoiceTranscript(text);
    }

    if (!isRetry && shouldSkipDuplicateMessage(outboundText, source)) {
      setTyping(false);
      return;
    }

    if (!isRetry) {
      appendMessage("user", outboundText);
      lastSubmittedMessageRef.current = {
        normalized: normalizeForDuplicateCheck(outboundText),
        timestamp: Date.now(),
        source,
      };
    }

    // If WS is still connecting, queue the message for when it opens
    if (wsRef.current && wsRef.current.readyState === WebSocket.CONNECTING) {
      pendingMessageRef.current = { text: outboundText, source: "retry" };
      return;
    }

    const sent = sendUserMessage(outboundText);
    if (!sent) {
      awaitingAgentReplyRef.current = false;
      setTyping(false);
      // Try reconnecting
      pendingMessageRef.current = { text: outboundText, source: "retry" };
      connectSession();
      return;
    }
  }

  function stopListeningUI() {
    isListeningRef.current = false;
    setIsListening(false);
    updateStatus("connected", "Connected");
  }

  async function startRecognitionSession(source = "manual") {
    if (recognitionStartInFlightRef.current) {
      return;
    }
    if (!recognitionRef.current || !canSpeakRef.current) {
      return;
    }
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
      return;
    }
    if (isSpeakingRef.current || isListeningRef.current || awaitingAgentReplyRef.current) {
      return;
    }

    recognitionStartInFlightRef.current = true;
    try {
      const micSetupResult = await configureEnhancedMicrophone();
      if (micSetupResult === "failed") {
        setMicEnabled(false);
        setMicHint("Mic access denied. Enable it in browser settings to use voice.");
        setCanSpeak(false);
        canSpeakRef.current = false;
        return;
      }

      if (source === "auto") {
        autoListenArmedRef.current = false;
      }

      recognitionRef.current.start();
    } catch (error) {
      scheduleMicRelease(200);
      if (source === "manual") {
        appendMessage("agent", "Voice input is busy. Try again in a second.");
      }
    } finally {
      recognitionStartInFlightRef.current = false;
    }
  }

  function maybeAutoStartListening() {
    if (!autoListenEnabledRef.current) {
      return;
    }

    if (!autoListenArmedRef.current) {
      return;
    }

    if (!canSpeakRef.current) {
      return;
    }

    if (!recognitionRef.current) {
      return;
    }

    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
      return;
    }

    if (isSpeakingRef.current || isListeningRef.current || awaitingAgentReplyRef.current) {
      return;
    }

    window.setTimeout(function () {
      if (isSpeakingRef.current || isListeningRef.current || awaitingAgentReplyRef.current) {
        return;
      }
      void startRecognitionSession("auto");
    }, 160);
  }

  async function handleVoiceRecognitionResult(finalTranscript, bestConfidence) {
    const confidenceTooLow = bestConfidence !== null && bestConfidence < MIN_CONFIDENCE_FOR_BACKEND_FALLBACK;
    const likelyNoiseBurst = finalTranscript.length < 3 && ambientNoiseLevelRef.current > 0.1;
    const shouldTryBackendTranscribe = sttTranscribeAvailableRef.current && (confidenceTooLow || likelyNoiseBurst || !finalTranscript);

    let selectedTranscript = finalTranscript;
    let improvedByBackend = false;
    const capturedAudio = await stopVoiceSegmentCapture();

    if (shouldTryBackendTranscribe && capturedAudio) {
      setMicHint("Improving voice capture...");
      const backendTranscript = await transcribeCapturedVoice(capturedAudio, finalTranscript);
      if (backendTranscript) {
        selectedTranscript = backendTranscript;
        improvedByBackend = true;
      }
    }

    const transcriptTooWeak = !selectedTranscript || selectedTranscript.trim().length < 3;
    const shouldBlockAsNoise = (likelyNoiseBurst || transcriptTooWeak) && !improvedByBackend;

    if (shouldBlockAsNoise) {
      appendMessage(
        "agent",
        "I heard too much background noise. Please speak a bit closer to the mic and try again."
      );
      syncInteractionState();
      maybeAutoStartListening();
      return;
    }

    if (!selectedTranscript) {
      appendMessage(
        "agent",
        "I heard too much background noise. Please speak a bit closer to the mic and try again."
      );
      syncInteractionState();
      maybeAutoStartListening();
      return;
    }

    await submitUserMessage(selectedTranscript, "voice");
  }

  function initRecognition() {
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;

    if (!SpeechRecognition) {
      return null;
    }

    const recognition = new SpeechRecognition();
    recognition.continuous = false;
    recognition.interimResults = true;
    recognition.maxAlternatives = 4;
    recognition.lang = "en-US";

    recognition.onstart = function () {
      recognitionResultHandledRef.current = false;
      clearMicReleaseTimer();
      isListeningRef.current = true;
      setIsListening(true);
      updateStatus("listening", "Listening");
      setMicHint("Listening now...");
      startVoiceSegmentCapture();
    };

    recognition.onresult = function (event) {
      let finalTranscript = "";
      let bestConfidence = null;

      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        const result = event.results[i];
        if (!result.isFinal) {
          continue;
        }

        const bestAlternative = pickBestAlternative(result);
        const chunk = (bestAlternative.transcript || "").trim();
        if (chunk) {
          finalTranscript = (finalTranscript + " " + chunk).trim();
        }

        if (typeof bestAlternative.confidence === "number" && bestAlternative.confidence > 0) {
          if (bestConfidence === null || bestAlternative.confidence > bestConfidence) {
            bestConfidence = bestAlternative.confidence;
          }
        }
      }

      stopListeningUI();

      if (!finalTranscript) {
        scheduleMicRelease();
        syncInteractionState();
        maybeAutoStartListening();
        return;
      }

      if (recognitionResultHandledRef.current) {
        return;
      }
      recognitionResultHandledRef.current = true;
      handleVoiceRecognitionResult(finalTranscript, bestConfidence);
    };

    recognition.onerror = function (event) {
      recognitionResultHandledRef.current = false;
      stopListeningUI();
      stopVoiceSegmentCapture();
      scheduleMicRelease();

      if (event.error === "not-allowed" || event.error === "audio-capture") {
        setMicEnabled(false);
        setMicHint("Mic access denied. Enable it in browser settings to use voice.");
        setCanSpeak(false);
        canSpeakRef.current = false;
        return;
      }

      if (event.error === "no-speech") {
        setMicHint("No clear speech detected. Trying again...");
      } else {
        setMicHint("Could not catch that. Try again.");
      }

      syncInteractionState();
      maybeAutoStartListening();
    };

    recognition.onend = function () {
      recognitionResultHandledRef.current = false;
      stopVoiceSegmentCapture();
      scheduleMicRelease();
      if (isListeningRef.current) {
        stopListeningUI();
      }
    };

    return recognition;
  }

  function afterAudio() {
    isSpeakingRef.current = false;
    audioRef.current = null;
    updateStatus("connected", "Connected");
    syncInteractionState();
    maybeAutoStartListening();
  }

  function playAudio(base64Mp3) {
    if (!base64Mp3 || !canListenRef.current) {
      updateStatus("connected", "Connected");
      syncInteractionState();
      maybeAutoStartListening();
      return;
    }

    isSpeakingRef.current = true;
    updateStatus("speaking", "Speaking");
    setMicEnabled(false);
    setMicHint("Assistant is speaking...");

    if (audioRef.current) {
      audioRef.current.pause();
      audioRef.current = null;
    }

    const audio = new Audio("data:audio/mp3;base64," + base64Mp3);
    audioRef.current = audio;

    audio.onended = afterAudio;
    audio.onerror = afterAudio;

    audio.play().catch(function () {
      afterAudio();
    });
  }

  function endAndCleanup() {
    autoListenArmedRef.current = false;
    if (recognitionRef.current) {
      try { recognitionRef.current.stop(); } catch (_) {}
    }
    stopMicEnhancementPipeline();
    isSpeakingRef.current = false;
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      try {
        wsRef.current.send(JSON.stringify({ type: "end_session" }));
      } catch (_) {}
      wsRef.current.close();
    }
  }

  function handleServerMessage(data) {
    if (!data || !data.type) {
      return;
    }

    if (data.type === "agent_message") {
      awaitingAgentReplyRef.current = false;
      setTyping(false);
      appendMessage("agent", data.text || "");
      refreshConversationHistory();

      // Arm one auto-listen turn after this assistant reply.
      if (!data.session_complete) {
        autoListenArmedRef.current = autoListenEnabledRef.current;
      } else {
        autoListenArmedRef.current = false;
      }

      if (data.session_complete) {
        // Play final audio, then close connection and release mic
        if (data.audio && canListenRef.current) {
          const finalAudio = new Audio("data:audio/mp3;base64," + data.audio);
          finalAudio.onended = function () { endAndCleanup(); };
          finalAudio.onerror = function () { endAndCleanup(); };
          finalAudio.play().catch(function () { endAndCleanup(); });
        } else {
          endAndCleanup();
        }
      } else {
        playAudio(data.audio);
      }
      return;
    }

    if (data.type === "vital_logged") {
      if (data.vital) {
        setVitals(function (prev) {
          return [data.vital].concat(prev).slice(0, 20);
        });
      }
      return;
    }

    if (
      data.type === "medication_event_logged" ||
      data.type === "side_effect_logged" ||
      data.type === "refill_flagged" ||
      data.type === "escalation_created"
    ) {
      void refreshHealthData(null, { silent: true });
      return;
    }

    if (data.type === "doctors_found") {
      setDoctors(Array.isArray(data.doctors) ? data.doctors : []);
      setActivePanel("doctors");
      return;
    }

    if (data.type === "session_timeout") {
      awaitingAgentReplyRef.current = false;
      setTyping(false);
      autoListenArmedRef.current = false;
      syncInteractionState();
      return;
    }

    if (data.type === "error") {
      awaitingAgentReplyRef.current = false;
      setTyping(false);
      autoListenArmedRef.current = false;
      appendMessage("agent", "Something went wrong. Please try again.");
      syncInteractionState();
    }
  }

  async function connectSession() {
    if (
      wsRef.current &&
      (wsRef.current.readyState === WebSocket.OPEN ||
        wsRef.current.readyState === WebSocket.CONNECTING)
    ) {
      return;
    }

    updateStatus("", "Connecting");
    requestLocation();

    if (authToken) {
      const [vitalsResult, conversationsResult] = await Promise.allSettled([
        fetchVitalsHistory(authToken),
        fetchConversationHistory(authToken, { limitSessions: 30 }),
      ]);
      const tokenExpired =
        (vitalsResult.status === "rejected" && vitalsResult.reason?.status === 401) ||
        (conversationsResult.status === "rejected" && conversationsResult.reason?.status === 401);
      if (tokenExpired) {
        handleLogout();
        return;
      }
      if (vitalsResult.status === "fulfilled" && Array.isArray(vitalsResult.value)) {
        setVitals(vitalsResult.value);
      }
      if (conversationsResult.status === "fulfilled" && Array.isArray(conversationsResult.value)) {
        setConversationHistory(conversationsResult.value);
      }
    }
    const healthChecks = await Promise.allSettled([checkBackendHealth(), checkSttHealth()]);
    const sttHealthy = healthChecks[1] && healthChecks[1].status === "fulfilled";
    const sttHealthPayload = sttHealthy ? healthChecks[1].value : null;
    const transcribeEnabled = Boolean(sttHealthPayload && sttHealthPayload.transcribe_enabled);
    sttTranscribeAvailableRef.current = transcribeEnabled;

    const ws = new WebSocket(resolveWebSocketUrl(authToken));
    wsRef.current = ws;

    ws.onopen = function () {
      updateStatus("connected", "Connected");

      if (!recognitionRef.current) {
        recognitionRef.current = initRecognition();
      }

      setMicHint("Type or tap the mic to start.");

      syncInteractionState();

      if (pendingMessageRef.current) {
        const queued = pendingMessageRef.current;
        pendingMessageRef.current = null;
        submitUserMessage(queued.text, queued.source || "retry");
      }
    };

    ws.onmessage = function (event) {
      try {
        const data = JSON.parse(event.data);
        handleServerMessage(data);
      } catch (error) {
        appendMessage("agent", "Invalid server message received.");
      }
    };

    ws.onerror = function () {
      appendMessage("agent", "Connection failed. Please try again.");
    };

    ws.onclose = function () {
      updateStatus("", "Disconnected");
      disableInteraction();
      setMicHint("Session ended.");
      setIsListening(false);
      isListeningRef.current = false;
      isSpeakingRef.current = false;
      awaitingAgentReplyRef.current = false;
      sttTranscribeAvailableRef.current = false;
      autoListenArmedRef.current = false;

      if (recognitionRef.current) {
        try { recognitionRef.current.stop(); } catch (_) {}
      }

      if (audioRef.current) {
        audioRef.current.pause();
        audioRef.current = null;
      }

      stopMicEnhancementPipeline();
      wsRef.current = null;
    };
  }

  function toggleVoice() {
    if (!canSpeakRef.current) {
      return;
    }

    // If disconnected, connect first then start listening once open
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
      connectSession().then(function () {
        if (recognitionRef.current && wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
          void startRecognitionSession("manual");
        }
      });
      return;
    }

    if (!recognitionRef.current) {
      return;
    }

    if (isSpeakingRef.current && audioRef.current) {
      audioRef.current.pause();
      audioRef.current = null;
      isSpeakingRef.current = false;
      updateStatus("connected", "Connected");
      syncInteractionState();
    }

    if (isListeningRef.current) {
      recognitionRef.current.stop();
      return;
    }

    void startRecognitionSession("manual");
  }

  function onSubmit(event) {
    event.preventDefault();

    const text = inputValue.trim();
    if (!text) {
      return;
    }

    submitUserMessage(text, "text");
    setInputValue("");
  }

  if (!authToken || !authUser) {
    return <AuthPage onAuth={handleAuth} />;
  }

  const vitalsTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";

  function formatLiveMessageTime(isoValue) {
    return formatClockTime(isoValue || new Date().toISOString(), vitalsTimezone);
  }

  function togglePanel(panelKey) {
    setActivePanel(function (prev) {
      return prev === panelKey ? null : panelKey;
    });
  }

  function renderRailIcon(iconName) {
    if (iconName === "user") {
      return (
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="M12 12a4.2 4.2 0 1 0 0-8.4 4.2 4.2 0 0 0 0 8.4z"></path>
          <path d="M4 20a8 8 0 0 1 16 0"></path>
        </svg>
      );
    }

    if (iconName === "vitals") {
      return (
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="M3 12h4l2.2-5.2 4.1 10.6L16 12h5"></path>
        </svg>
      );
    }

    if (iconName === "history") {
      return (
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="M3.5 12A8.5 8.5 0 1 0 6 5.8"></path>
          <path d="M3.2 3.8v4.3h4.3"></path>
          <path d="M12 7v5l3.3 2"></path>
        </svg>
      );
    }

    if (iconName === "graph") {
      return (
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"></polyline>
        </svg>
      );
    }

    return (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M10.2 4.3a2.4 2.4 0 1 1 3.6 0l.5 1a8 8 0 0 1 1.8 1l1-.5a2.4 2.4 0 1 1 2.4 4.2l-1 .6a8 8 0 0 1 0 2.1l1 .6a2.4 2.4 0 1 1-2.4 4.1l-1-.5a8 8 0 0 1-1.8 1l-.5 1a2.4 2.4 0 1 1-3.6 0l-.5-1a8 8 0 0 1-1.8-1l-1 .5a2.4 2.4 0 1 1-2.4-4.1l1-.6a8 8 0 0 1 0-2.1l-1-.6a2.4 2.4 0 1 1 2.4-4.2l1 .5a8 8 0 0 1 1.8-1z"></path>
        <circle cx="12" cy="12" r="2.8"></circle>
      </svg>
    );
  }

  function renderVitalIcon(vitalType) {
    const icons = {
      blood_pressure: <svg viewBox="0 0 24 24"><path d="M3 12h4l2.2-5.2 4.1 10.6L16 12h5"></path></svg>,
      heart_rate: <svg viewBox="0 0 24 24"><path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1-1a5.5 5.5 0 0 0-7.8 7.8l1 1L12 21l7.8-7.8 1-1a5.5 5.5 0 0 0 0-7.8z"></path></svg>,
      temperature: <svg viewBox="0 0 24 24"><path d="M14 14.8V5a2 2 0 1 0-4 0v9.8a4 4 0 1 0 4 0z"></path></svg>,
      oxygen_saturation: <svg viewBox="0 0 24 24"><path d="M2 12h4l3-9 4 18 3-9h6"></path></svg>,
      blood_glucose: <svg viewBox="0 0 24 24"><path d="M12 2.7c-5 5-7.5 8.3-7.5 12a7.5 7.5 0 0 0 15 0c0-3.7-2.5-7-7.5-12z"></path></svg>,
      weight: <svg viewBox="0 0 24 24"><path d="M6.5 6.5h11l1 11H5.5z"></path><circle cx="12" cy="4.5" r="2.5"></circle></svg>,
      symptom: <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="8" x2="12" y2="12"></line><line x1="12" y1="16" x2="12.01" y2="16"></line></svg>,
    };
    return <span className={"cc-vital-icon cc-vi-" + vitalType}>{icons[vitalType] || icons.symptom}</span>;
  }

  function summarizeConversation(messagesForSession) {
    if (!Array.isArray(messagesForSession) || messagesForSession.length === 0) {
      return { title: "Conversation", summary: "No messages available." };
    }

    const assistantLine =
      messagesForSession.find(function (entry) {
        return entry.role === "assistant" && entry.content;
      }) || messagesForSession[0];

    const rawText = (assistantLine && assistantLine.content ? assistantLine.content : "Conversation").trim();
    const sentence = rawText.split(/[.!?]/)[0] || rawText;
    const trimmedTitle = sentence.length > 44 ? sentence.slice(0, 44).trim() + "..." : sentence;
    const summary = rawText.length > 110 ? rawText.slice(0, 110).trim() + "..." : rawText;

    return {
      title: trimmedTitle || "Conversation",
      summary: summary || "Conversation summary unavailable.",
    };
  }

  async function handleDeleteVital(vitalId) {
    try {
      await deleteVital(authToken, vitalId);
      setVitals(function (prev) { return prev.filter(function (v) { return v.id !== vitalId; }); });
    } catch (_) {}
  }

  function groupDoctorsBySpecialty(docs) {
    var groups = {};
    docs.forEach(function (doc) {
      var key = doc.recommended_for || "General";
      if (!groups[key]) groups[key] = [];
      groups[key].push(doc);
    });
    return Object.entries(groups);
  }

  function openConversationSession(session) {
    setSelectedConversationSession(session);
  }

  function closeConversationSession() {
    setSelectedConversationSession(null);
  }

  function handleSessionCardKeyDown(event, session) {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openConversationSession(session);
    }
  }

  function renderViewSubtitle() {
    if (activeView === "dashboard") {
      return "Clinician operations";
    }
    if (activeView === "health") {
      return "Medication, vitals, and adherence";
    }
    return "Daily health check-in";
  }

  function renderHealthView() {
    const recentSessions = conversationHistory.slice(0, 4);
    const timelineItems =
      Array.isArray(sideEffectTimeline) && sideEffectTimeline.length > 0
        ? sideEffectTimeline
        : (activeMedication && Array.isArray(activeMedication.side_effects) ? activeMedication.side_effects : []);

    return (
      <div className="cc-view-stage">
        <div className="cc-section-head">
          <h2>My Health</h2>
          <p>Review your GLP-1 treatment, refill workflow, and recent health trends.</p>
        </div>

        {healthError ? <div className="cc-inline-error">{healthError}</div> : null}

        <div className="cc-health-grid">
          <div className="cc-health-stack">
            <MedicationCard
              token={authToken}
              medication={activeMedication}
              refills={activeRefills}
              onRefresh={refreshHealthData}
            />

            <section className="cc-card-shell">
              <ProfileCard
                user={authUser}
                token={authToken}
                onUpdate={handleProfileUpdate}
                onDeleteAccount={handleLogout}
              />
            </section>
          </div>

          <div className="cc-health-stack cc-health-stack-wide">
            <section className="cc-card-shell">
              <div className="cc-section-head cc-section-head-tight">
                <h3>Vitals Trends</h3>
                <p>{healthLoading ? "Refreshing health data..." : "Recent readings saved from chat and structured tracking."}</p>
              </div>
              <VitalsGraph vitals={vitals} />
            </section>
          </div>
        </div>

        <SideEffectTimeline medication={activeMedication} sideEffects={timelineItems} />

        <div className="cc-health-secondary-grid">
          <section className="cc-card-shell">
            <div className="cc-section-head cc-section-head-tight">
              <h3>Refill Status</h3>
              <p>Track active refill requests for your current medication.</p>
            </div>

            {activeRefills.length === 0 ? (
              <div className="cc-empty-state">
                <p>No active refill requests right now.</p>
              </div>
            ) : (
              <div className="cc-panel-list">
                {activeRefills.map(function (refill) {
                  const medicationName =
                    (refill.medication && (refill.medication.brand_name || refill.medication.drug_name)) ||
                    "Medication";
                  return (
                    <article className="cc-refill-item" key={refill.id}>
                      <div className="cc-refill-item-head">
                        <strong>{medicationName}</strong>
                        <span className="cc-refill-status">{String(refill.status || "due").replace(/_/g, " ")}</span>
                      </div>
                      <span>{refill.pharmacy_name || "Care team coordination in progress"}</span>
                      <span>{refill.due_date ? `Due ${formatDateLabel(refill.due_date)}` : "Due date pending"}</span>
                    </article>
                  );
                })}
              </div>
            )}
          </section>

          <section className="cc-card-shell">
            <div className="cc-section-head cc-section-head-tight">
              <h3>Recent Sessions</h3>
              <p>Catch up on the last conversations saved from the chat experience.</p>
            </div>

            {recentSessions.length === 0 ? (
              <div className="cc-empty-state">
                <p>No saved sessions yet.</p>
              </div>
            ) : (
              <div className="cc-panel-list">
                {recentSessions.map(function (session, index) {
                  const sessionMessages = Array.isArray(session.messages) ? session.messages : [];
                  const preview = summarizeConversation(sessionMessages);
                  const anchorTimestamp =
                    session.started_at || (sessionMessages[0] && sessionMessages[0].created_at) || null;
                  const timezoneName =
                    (typeof session.timezone_name === "string" && session.timezone_name.trim()) ||
                    Intl.DateTimeFormat().resolvedOptions().timeZone ||
                    "UTC";
                  return (
                    <article
                      className="cc-session-item"
                      key={(session.session_id || "health-session") + "-" + index}
                      role="button"
                      tabIndex={0}
                      onClick={function () {
                        openConversationSession(session);
                      }}
                      onKeyDown={function (event) {
                        handleSessionCardKeyDown(event, session);
                      }}
                      aria-label={`Open conversation from ${formatDateLabel(normalizeIsoToDateKey(anchorTimestamp, timezoneName), timezoneName)}`}
                    >
                      <div className="cc-session-body">
                        <div className="cc-session-title">{preview.title}</div>
                        <div className="cc-session-summary">{preview.summary}</div>
                        <div className="cc-session-meta">
                          {formatDateLabel(normalizeIsoToDateKey(anchorTimestamp, timezoneName), timezoneName)}
                          {" \u00b7 "}
                          {formatClockTime(anchorTimestamp, timezoneName)}
                        </div>
                      </div>
                    </article>
                  );
                })}
              </div>
            )}
          </section>
        </div>
      </div>
    );
  }

  function renderConversationHistoryModal() {
    if (!selectedConversationSession) {
      return null;
    }

    const sessionMessages = Array.isArray(selectedConversationSession.messages)
      ? selectedConversationSession.messages
      : [];
    const timezoneName =
      (typeof selectedConversationSession.timezone_name === "string" && selectedConversationSession.timezone_name.trim()) ||
      Intl.DateTimeFormat().resolvedOptions().timeZone ||
      "UTC";
    const anchorTimestamp =
      selectedConversationSession.started_at ||
      (sessionMessages[0] && sessionMessages[0].created_at) ||
      null;
    const preview = summarizeConversation(sessionMessages);
    const dateKey = normalizeIsoToDateKey(anchorTimestamp, timezoneName);
    const messageCount = sessionMessages.length || selectedConversationSession.message_count || 0;

    return (
      <div
        className="cc-history-modal-backdrop"
        onClick={function () {
          closeConversationSession();
        }}
      >
        <div
          className="cc-history-modal"
          role="dialog"
          aria-modal="true"
          aria-label="Past conversation transcript"
          onClick={function (event) {
            event.stopPropagation();
          }}
        >
          <div className="cc-history-modal-header">
            <div className="cc-history-modal-copy">
              <h3>{preview.title || "Conversation"}</h3>
              <p>
                {formatDateLabel(dateKey, timezoneName)}
                {" \u00b7 "}
                {formatClockTime(anchorTimestamp, timezoneName)}
                {" \u00b7 "}
                {messageCount} message{messageCount === 1 ? "" : "s"}
              </p>
            </div>
            <button
              type="button"
              className="cc-history-modal-close"
              aria-label="Close conversation transcript"
              onClick={function () {
                closeConversationSession();
              }}
            >
              <svg viewBox="0 0 24 24" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
            </button>
          </div>

          <div className="cc-history-modal-body">
            {sessionMessages.length === 0 ? (
              <p className="cc-empty-panel">No messages were saved for this conversation.</p>
            ) : (
              sessionMessages.map(function (message) {
                const isUser = message.role === "user";
                return (
                  <div
                    className={isUser ? "cc-history-msg-row is-user" : "cc-history-msg-row is-agent"}
                    key={message.id || `${message.role}-${message.created_at}`}
                  >
                    <div className={isUser ? "cc-history-msg-avatar is-user" : "cc-history-msg-avatar"}>
                      {isUser ? "U" : "C"}
                    </div>
                    <div className="cc-history-msg-bubble">
                      <div className="cc-history-msg-text">{message.content || ""}</div>
                      <div className="cc-history-msg-time">
                        {formatClockTime(message.created_at, timezoneName)}
                      </div>
                    </div>
                  </div>
                );
              })
            )}
          </div>
        </div>
      </div>
    );
  }

  function renderDrawerContent() {
    if (activePanel === "profile") {
      return (
        <div className="cc-panel-profile">
          <ProfileCard
            user={authUser}
            token={authToken}
            onUpdate={handleProfileUpdate}
            onDeleteAccount={handleLogout}
          />
        </div>
      );
    }

    if (activePanel === "vitals") {
      return (
        <div className="cc-panel-list">
          {vitals.length === 0 ? (
            <p className="cc-empty-panel">No vitals yet. Share readings in chat to start tracking.</p>
          ) : (
            groupVitalsByDate(vitals, vitalsTimezone).map(function ([dateKey, dateVitals]) {
              const showTime = dateVitals.length > 1;
              return (
                <section className="cc-group" key={dateKey}>
                  <h3 className="cc-group-title">{formatDateLabel(dateKey, vitalsTimezone)}</h3>
                  {dateVitals.map(function (vital, index) {
                    return (
                      <article className="cc-vital-item" key={(vital.id || "vital") + "-" + index}>
                        <div className="cc-vital-icon-wrap">
                          {renderVitalIcon(vital.vital_type)}
                          {vital.id && (
                            <button
                              type="button"
                              className="cc-vital-delete"
                              title="Delete this reading"
                              onClick={function () { handleDeleteVital(vital.id); }}
                            >
                              <svg viewBox="0 0 24 24" aria-hidden="true"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6l-1.5 14a2 2 0 0 1-2 1.8H8.5a2 2 0 0 1-2-1.8L5 6"></path><path d="M10 11v6"></path><path d="M14 11v6"></path></svg>
                            </button>
                          )}
                        </div>
                        <div className="cc-vital-main">
                          <div className="cc-vital-name">{VITAL_LABELS[vital.vital_type] || vital.vital_type || "Vital"}</div>
                          {showTime ? (
                            <div className="cc-vital-time">{formatClockTime(vital.recorded_at, vitalsTimezone)}</div>
                          ) : null}
                        </div>
                        <div className="cc-vital-reading">
                          <div className="cc-vital-value">{vital.value}</div>
                          {vital.unit ? <div className="cc-vital-unit">{vital.unit}</div> : null}
                        </div>
                      </article>
                    );
                  })}
                </section>
              );
            })
          )}
        </div>
      );
    }

    if (activePanel === "graphs") {
      return <VitalsGraph vitals={vitals} />;
    }

    if (activePanel === "conversations") {
      return (
        <div className="cc-panel-list">
          {conversationHistory.length === 0 ? (
            <p className="cc-empty-panel">No saved conversations yet.</p>
          ) : (
            conversationHistory.map(function (session, index) {
              const sessionMessages = Array.isArray(session.messages) ? session.messages : [];
              const timezoneName =
                (typeof session.timezone_name === "string" && session.timezone_name.trim()) ||
                Intl.DateTimeFormat().resolvedOptions().timeZone ||
                "UTC";
              const anchorTimestamp = session.started_at || (sessionMessages[0] && sessionMessages[0].created_at) || null;
              const dateKey = normalizeIsoToDateKey(anchorTimestamp, timezoneName);
              const dateLabel = formatDateLabel(dateKey, timezoneName);
              const timeLabel = formatClockTime(anchorTimestamp, timezoneName);
              const messageCount = sessionMessages.length || session.message_count || 0;
              const preview = summarizeConversation(sessionMessages);
              return (
                <article
                  className="cc-session-item"
                  key={(session.session_id || "session") + "-" + index}
                  role="button"
                  tabIndex={0}
                  onClick={function () {
                    openConversationSession(session);
                  }}
                  onKeyDown={function (event) {
                    handleSessionCardKeyDown(event, session);
                  }}
                  aria-label={`Open conversation from ${dateLabel}`}
                >
                  <span className="cc-session-icon">
                    <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path></svg>
                  </span>
                  <div className="cc-session-body">
                    <div className="cc-session-title">{preview.title}</div>
                    <div className="cc-session-summary">{preview.summary}</div>
                    <div className="cc-session-meta">
                      {dateLabel} &middot; {timeLabel} {timezoneName} &middot; {messageCount} msg{messageCount === 1 ? "" : "s"}
                    </div>
                  </div>
                  <span className="cc-session-chevron">
                    <svg viewBox="0 0 24 24" aria-hidden="true"><polyline points="9 18 15 12 9 6"></polyline></svg>
                  </span>
                </article>
              );
            })
          )}
        </div>
      );
    }

    if (activePanel === "doctors") {
      return (
        <div className="cc-panel-list">
          {doctors.length === 0 ? (
            <p className="cc-empty-panel">No recommendations yet.</p>
          ) : (
            groupDoctorsBySpecialty(doctors).map(function ([specialty, docs]) {
              return (
                <section className="cc-group" key={specialty}>
                  <h3 className="cc-group-title">{specialty}</h3>
                  {docs.map(function (doc, index) {
                    return (
                      <article className="cc-doctor-item" key={(doc.name || "doctor") + "-" + index}>
                        <div className="cc-doctor-head">
                          <div className="cc-doctor-name">{doc.name || "Doctor"}</div>
                          {doc.rating !== undefined && doc.rating !== null ? (
                            <span className="cc-doctor-rating">
                              <svg viewBox="0 0 24 24" aria-hidden="true"><polygon points="12 2 15.1 8.3 22 9.3 17 14.1 18.2 21 12 17.8 5.8 21 7 14.1 2 9.3 8.9 8.3 12 2"></polygon></svg>
                              {doc.rating}
                            </span>
                          ) : null}
                        </div>
                        <div className="cc-doctor-address">{doc.address || "Address unavailable"}</div>
                        {doc.distance_km ? (
                          <div className="cc-doctor-distance">{doc.distance_km} km away</div>
                        ) : null}
                        <div className="cc-doctor-actions">
                          <button type="button" className="cc-btn-outline" title="Call">
                            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M22 16.9v3a2 2 0 0 1-2.2 2A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1.9.4 1.8.7 2.7a2 2 0 0 1-.4 2.1L8 9.9a16 16 0 0 0 6.1 6.1l1.4-1.4a2 2 0 0 1 2.1-.4c.9.3 1.8.5 2.7.7a2 2 0 0 1 1.7 2z"></path></svg>
                            Call
                          </button>
                          {doc.maps_url ? (
                            <a className="cc-btn-outline" href={doc.maps_url} target="_blank" rel="noopener noreferrer">
                              <svg viewBox="0 0 24 24" aria-hidden="true"><polygon points="3 11 22 2 13 21 11 13 3 11"></polygon></svg>
                              Directions
                            </a>
                          ) : null}
                        </div>
                      </article>
                    );
                  })}
                </section>
              );
            })
          )}
        </div>
      );
    }

    return <p className="cc-empty-panel">Choose a panel from the left menu.</p>;
  }

  const isChatView = activeView === "chat";

  return (
    <div className={isChatView ? "cc-shell" : "cc-shell cc-shell-wide"}>
      {isChatView ? (
        <aside className="cc-rail">
          <div className="cc-rail-logo" aria-label="CuraConnect">C</div>
          <div className="cc-rail-nav">
            {NAV_ITEMS.map(function (item) {
              const isActive = activePanel === item.key;
              return (
                <button
                  key={item.key}
                  type="button"
                  className={isActive ? "cc-rail-btn is-active" : "cc-rail-btn"}
                  onClick={function () {
                    togglePanel(item.key);
                  }}
                  title={item.label}
                  aria-label={item.label}
                >
                  {renderRailIcon(item.icon)}
                </button>
              );
            })}
          </div>
        </aside>
      ) : null}

      <main className={isChatView ? "cc-main" : "cc-main cc-main-wide"}>
        <header className="cc-topbar">
          <div className="cc-brand">
            <h1>CuraConnect</h1>
            <p>{renderViewSubtitle()}</p>
          </div>
          <div className="cc-top-actions">
            <span className={status.mode === "connected" ? "cc-status is-connected" : "cc-status"}>
              {status.text || "Disconnected"}
            </span>
            {isChatView ? (
              <>
                <div className="cc-mode-group" role="group" aria-label="Response mode">
                  <button
                    type="button"
                    className={canListen ? "cc-mode-btn is-active" : "cc-mode-btn"}
                    onClick={function () {
                      setCanListen(true);
                    }}
                    title="Voice replies"
                  >
                    <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2.5a3.2 3.2 0 0 0-3.2 3.2v7a3.2 3.2 0 0 0 6.4 0v-7A3.2 3.2 0 0 0 12 2.5z"></path><path d="M18.3 11.8v1.1a6.3 6.3 0 0 1-12.6 0v-1.1"></path></svg>
                    Voice
                  </button>
                  <button
                    type="button"
                    className={!canListen ? "cc-mode-btn is-active" : "cc-mode-btn"}
                    onClick={function () {
                      setCanListen(false);
                    }}
                    title="Chat replies"
                  >
                    <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path></svg>
                    Chat
                  </button>
                </div>
                <label className="cc-auto-listen" title="Auto-listen after each reply">
                  Auto-listen
                  <input
                    type="checkbox"
                    checked={autoListenEnabled}
                    onChange={function (e) {
                      setAutoListenEnabled(e.target.checked);
                      autoListenEnabledRef.current = e.target.checked;
                    }}
                  />
                  <span className="cc-switch-track"><span className="cc-switch-thumb"></span></span>
                </label>
              </>
            ) : null}
          </div>
        </header>

        <nav className="cc-view-tabs" aria-label="Primary navigation">
          {PRIMARY_VIEWS.map(function (item) {
            const isActive = activeView === item.key;
            return (
              <button
                key={item.key}
                type="button"
                className={isActive ? "cc-view-tab is-active" : "cc-view-tab"}
                onClick={function () {
                  setActiveView(item.key);
                }}
              >
                {item.label}
              </button>
            );
          })}
        </nav>

        {isChatView ? (
          <>
            {canListen && (
              <div className="cc-voice-stage">
                <button
                  type="button"
                  className={isListening ? "cc-voice-orb is-listening" : "cc-voice-orb"}
                  onClick={toggleVoice}
                  disabled={!micEnabled}
                  title={micHint}
                >
                  <svg viewBox="0 0 24 24" aria-hidden="true">
                    <path d="M12 2.5a3.2 3.2 0 0 0-3.2 3.2v7a3.2 3.2 0 0 0 6.4 0v-7A3.2 3.2 0 0 0 12 2.5z"></path>
                    <path d="M18.3 11.8v1.1a6.3 6.3 0 0 1-12.6 0v-1.1"></path>
                    <path d="M12 19.5v2"></path>
                  </svg>
                </button>
                <p className="cc-voice-hint">{isListening ? "Listening..." : "Tap to speak"}</p>
              </div>
            )}

            <section className="cc-transcript">
              {messages.length === 0 && !typing ? (
                <p className="cc-empty-chat">Type a message or tap the mic to start your health check-in.</p>
              ) : null}

              {messages.map(function (msg) {
                const isUser = msg.role === "user";
                return (
                  <div className={isUser ? "cc-msg-row is-user" : "cc-msg-row is-agent"} key={msg.id}>
                    {!isUser ? <div className="cc-msg-avatar">C</div> : null}
                    <div className="cc-msg-bubble">
                      <div className="cc-msg-text">{msg.text}</div>
                      <div className="cc-msg-time">{formatLiveMessageTime(msg.createdAt)}</div>
                    </div>
                  </div>
                );
              })}

              {typing ? (
                <div className="cc-msg-row is-agent" id="typingIndicator">
                  <div className="cc-msg-avatar">C</div>
                  <div className="cc-msg-bubble">
                    <div className="typing">
                      <span></span>
                      <span></span>
                      <span></span>
                    </div>
                  </div>
                </div>
              ) : null}

              <div ref={transcriptBottomRef}></div>
            </section>

            <form className="cc-composer" onSubmit={onSubmit} autoComplete="off">
              <input
                type="text"
                value={inputValue}
                onChange={function (event) {
                  setInputValue(event.target.value);
                }}
                placeholder="Type or tap the mic..."
              />
              <button className="cc-send-btn" type="submit" title="Send message">
                <svg viewBox="0 0 24 24" aria-hidden="true">
                  <line x1="22" y1="2" x2="11" y2="13"></line>
                  <polygon points="22 2 15 22 11 13 2 9 22 2"></polygon>
                </svg>
              </button>
              <button
                className={isListening ? "cc-mic-btn is-listening" : "cc-mic-btn"}
                type="button"
                onClick={toggleVoice}
                disabled={!micEnabled}
                title={micHint}
              >
                <svg viewBox="0 0 24 24" aria-hidden="true">
                  <path d="M12 2.5a3.2 3.2 0 0 0-3.2 3.2v7a3.2 3.2 0 0 0 6.4 0v-7A3.2 3.2 0 0 0 12 2.5z"></path>
                  <path d="M18.3 11.8v1.1a6.3 6.3 0 0 1-12.6 0v-1.1"></path>
                  <path d="M12 19.5v2"></path>
                </svg>
              </button>
            </form>
          </>
        ) : activeView === "dashboard" ? (
          <section className="cc-view-content">
            <ClinicDashboard token={authToken} />
          </section>
        ) : (
          <section className="cc-view-content">
            {renderHealthView()}
          </section>
        )}
      </main>

      {isChatView && activePanel ? (
        <button
          type="button"
          className="cc-drawer-backdrop"
          aria-label="Close side panel"
          onClick={function () {
            setActivePanel(null);
          }}
        ></button>
      ) : null}

      {isChatView ? (
        <aside className={activePanel ? "cc-drawer is-open" : "cc-drawer"}>
          <div className="cc-drawer-header">
            <h2>{PANEL_TITLES[activePanel] || "Panel"}</h2>
            <button
              type="button"
              className="cc-drawer-close"
              aria-label="Close panel"
              onClick={function () {
                setActivePanel(null);
              }}
            >
              <svg viewBox="0 0 24 24" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
            </button>
          </div>
          <div className="cc-drawer-body">{activePanel ? renderDrawerContent() : null}</div>
        </aside>
      ) : null}

      {renderConversationHistoryModal()}
    </div>
  );
}
export default App;
