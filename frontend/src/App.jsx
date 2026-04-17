import { useEffect, useRef, useState } from "react";
import {
  checkBackendHealth,
  checkSttHealth,
  enhanceTranscript,
  fetchConversationHistory,
  fetchVitalsHistory,
  resolveWebSocketUrl,
  transcribeAudioBlob,
} from "./api";

function groupVitalsByDate(vitals) {
  const groups = {};
  for (const vital of vitals) {
    const dateKey = vital.recorded_at ? vital.recorded_at.split("T")[0] : "Unknown";
    if (!groups[dateKey]) groups[dateKey] = [];
    groups[dateKey].push(vital);
  }
  return Object.entries(groups).sort((a, b) => b[0].localeCompare(a[0]));
}

function formatDateLabel(dateStr, timeZone = null) {
  const now = new Date();
  const today = normalizeIsoToDateKey(now.toISOString(), timeZone || undefined);
  const yesterday = normalizeIsoToDateKey(
    new Date(now.getTime() - 86400000).toISOString(),
    timeZone || undefined
  );
  if (dateStr === today) return "Today";
  if (dateStr === yesterday) return "Yesterday";
  return dateStr;
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
import ProfileCard from "./ProfileCard";

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
  const [isListening, setIsListening] = useState(false);
  const [vitals, setVitals] = useState([]);
  const [conversationHistory, setConversationHistory] = useState([]);
  const [doctors, setDoctors] = useState([]);
  const pendingMessageRef = useRef(null);
  const micDeniedRef = useRef(false);
  const recognitionResultHandledRef = useRef(false);
  const lastSubmittedMessageRef = useRef({ normalized: "", timestamp: 0, source: "" });
  const autoListenArmedRef = useRef(false);
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
    setMessages([]);
    autoListenArmedRef.current = false;
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
      });
    });
  }

  function disableInteraction() {
    setMicEnabled(false);
    setMicHint("Session ended. Type a message to start a new one.");
  }

  function syncInteractionState() {
    const isConnected = wsRef.current && wsRef.current.readyState === WebSocket.OPEN;

    if (!isConnected) {
      setMicEnabled(false);
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
      autoListenArmedRef.current &&
      !isSpeakingRef.current &&
      !isListeningRef.current &&
      !awaitingAgentReplyRef.current;
    setMicHint(
      isSpeakingRef.current
        ? "Assistant is speaking..."
        : autoListenReady
          ? "Auto-listen is ready for the next reply."
          : "Tap the mic button when you want to speak."
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
        autoListenArmedRef.current = true;
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

    if (data.type === "doctors_found") {
      setDoctors(Array.isArray(data.doctors) ? data.doctors : []);
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

    if (!recognitionRef.current) {
      return;
    }

    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
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

  return (
    <>
      <div className="bg-noise" aria-hidden="true"></div>

      <main className="app-shell">
        <header className="topbar">
          <div className="brand">
            <div className="brand-badge">CC</div>
            <div>
              <h1>CuraConnect</h1>
              <p>Simple health check-ins with voice and text</p>
            </div>
          </div>

          <div className="topbar-right">
            <div className={status.mode ? "status-dot " + status.mode : "status-dot"}>
              <span className="dot"></span>
              <span>{status.text}</span>
            </div>
            <div className="user-info">
              <span>{authUser.name}</span>
              <button type="button" className="btn-logout" onClick={handleLogout}>Logout</button>
            </div>
          </div>
        </header>

        <section className="layout">
          <section className="chat-card">
            <div className="card-title-row">
              <h2>Conversation</h2>
              <span className="pill">Live</span>
            </div>

            <div className="mode-row">
              <label className="mode-toggle">
                <input
                  type="checkbox"
                  checked={canSpeak}
                  onChange={function (event) {
                    setCanSpeak(event.target.checked);
                  }}
                />
                <span>I can speak</span>
              </label>

              <label className="mode-toggle">
                <input
                  type="checkbox"
                  checked={canListen}
                  onChange={function (event) {
                    setCanListen(event.target.checked);
                  }}
                />
                <span>I can listen</span>
              </label>

              <span className="mode-pill">{getModeLabel()}</span>
            </div>

            <div className="transcript">
              {messages.length === 0 && !typing ? (
                <p className="empty-state">Type a message or tap the mic to start your health check-in.</p>
              ) : null}

              {messages.map(function (msg) {
                return (
                  <div className={"message " + msg.role} key={msg.id}>
                    <div className="avatar">{msg.role === "agent" ? "AI" : "YOU"}</div>
                    <div className="bubble">{msg.text}</div>
                  </div>
                );
              })}

              {typing ? (
                <div className="message agent" id="typingIndicator">
                  <div className="avatar">AI</div>
                  <div className="bubble">
                    <div className="typing">
                      <span></span>
                      <span></span>
                      <span></span>
                    </div>
                  </div>
                </div>
              ) : null}

              <div ref={transcriptBottomRef}></div>
            </div>

            <form className="text-form" onSubmit={onSubmit} autoComplete="off">
              <input
                type="text"
                value={inputValue}
                disabled={false}
                onChange={function (event) {
                  setInputValue(event.target.value);
                }}
                placeholder="Type your update (e.g. My heart rate is 85)"
              />
              <button className="send-btn" type="submit" disabled={false} title="Send message">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <line x1="22" y1="2" x2="11" y2="13"></line>
                  <polygon points="22 2 15 22 11 13 2 9 22 2"></polygon>
                </svg>
              </button>
              <button
                className={isListening ? "mic-btn listening" : "mic-btn"}
                type="button"
                onClick={toggleVoice}
                disabled={!micEnabled}
                title={micHint}
              >
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"></path>
                  <path d="M19 10v2a7 7 0 0 1-14 0v-2"></path>
                  <line x1="12" y1="19" x2="12" y2="23"></line>
                  <line x1="8" y1="23" x2="16" y2="23"></line>
                </svg>
              </button>
            </form>

            <p className="controls-hint">{micHint}</p>
          </section>

          <aside className="sidebar">
            <section className="side-card">
              <ProfileCard user={authUser} token={authToken} onUpdate={handleProfileUpdate} onDeleteAccount={handleLogout} />
            </section>

            <section className="side-card">
              <h3>Vitals History</h3>
              <div className="vitals-list">
                {vitals.length === 0 ? (
                  <p className="empty-state">No vitals yet. Share readings in chat to start tracking.</p>
                ) : (
                  groupVitalsByDate(vitals).map(function ([dateKey, dateVitals]) {
                    return (
                      <div className="vitals-date-group" key={dateKey}>
                        <div className="vitals-date-label">{formatDateLabel(dateKey)}</div>
                        {dateVitals.map(function (vital, index) {
                          return (
                            <div className="vital-card" key={(vital.id || "vital") + "-" + index}>
                              <div className="vital-meta">
                                <div className="vital-label">
                                  {VITAL_LABELS[vital.vital_type] || vital.vital_type || "Vital"}
                                </div>
                                <div className="vital-value">
                                  {vital.value}
                                  {vital.unit ? " " + vital.unit : ""}
                                </div>
                              </div>
                              {vital.notes ? <div className="vital-note">{vital.notes}</div> : null}
                            </div>
                          );
                        })}
                      </div>
                    );
                  })
                )}
              </div>
            </section>

            <section className="side-card">
              <h3>Conversation History</h3>
              <div className="conversation-history-list">
                {conversationHistory.length === 0 ? (
                  <p className="empty-state">No saved conversations yet.</p>
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
                    return (
                      <details className="conversation-session" key={(session.session_id || "session") + "-" + index}>
                        <summary className="conversation-session-summary">
                          <span className="conversation-session-title">{dateLabel} • {timeLabel}</span>
                          <span className="conversation-session-meta">
                            {messageCount} message{messageCount === 1 ? "" : "s"} • {timezoneName}
                          </span>
                        </summary>
                        <div className="conversation-session-body">
                          {sessionMessages.length === 0 ? (
                            <p className="empty-state">No messages saved in this session.</p>
                          ) : (
                            sessionMessages.map(function (message, msgIndex) {
                              const role = message.role === "assistant" ? "assistant" : "user";
                              return (
                                <div className={"conversation-line " + role} key={(message.id || "msg") + "-" + msgIndex}>
                                  <div className="conversation-line-header">
                                    <span className="conversation-line-role">{role === "assistant" ? "AI" : "YOU"}</span>
                                    <span className="conversation-line-time">
                                      {formatClockTime(message.created_at, timezoneName)}
                                    </span>
                                  </div>
                                  <div className="conversation-line-text">{message.content || ""}</div>
                                </div>
                              );
                            })
                          )}
                        </div>
                      </details>
                    );
                  })
                )}
              </div>
            </section>

            <section className="side-card">
              <h3>Nearby Doctors</h3>
              <div className="doctors-list">
                {doctors.length === 0 ? (
                  <p className="empty-state">No recommendations yet.</p>
                ) : (
                  doctors.map(function (doc, index) {
                    return (
                      <div className="doctor-card" key={(doc.name || "doctor") + "-" + index}>
                        <div className="doctor-name">{doc.name || "Doctor"}</div>
                        <div className="doctor-address">{doc.address || "Address unavailable"}</div>
                        <div className="doctor-meta">
                          {doc.rating !== undefined && doc.rating !== null ? (
                            <span className="rating">Rating {doc.rating}</span>
                          ) : null}

                          {doc.open_now === true ? <span className="open">Open now</span> : null}
                          {doc.open_now === false ? <span className="closed">Closed</span> : null}

                          {doc.maps_url ? (
                            <a
                              className="map-link"
                              href={doc.maps_url}
                              target="_blank"
                              rel="noopener noreferrer"
                            >
                              View map
                            </a>
                          ) : null}
                        </div>
                      </div>
                    );
                  })
                )}
              </div>
            </section>
          </aside>
        </section>
      </main>
    </>
  );
}

export default App;
