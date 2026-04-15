import { useEffect, useRef, useState } from "react";

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

const MIN_CONFIDENCE_FOR_AUTO_SEND = 0.45;

function resolveWebSocketUrl() {
  const configured = (import.meta.env.VITE_WS_URL || "").trim();
  if (configured) {
    return configured;
  }

  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const host =
    window.location.port === "5173"
      ? window.location.hostname + ":8000"
      : window.location.host;
  return protocol + "://" + host + "/ws";
}

function App() {
  const [status, setStatus] = useState({ mode: "", text: "Disconnected" });
  const [messages, setMessages] = useState([]);
  const [typing, setTyping] = useState(false);
  const [inputValue, setInputValue] = useState("");
  const [inputEnabled, setInputEnabled] = useState(false);
  const [micEnabled, setMicEnabled] = useState(false);
  const [micHint, setMicHint] = useState("Connect first to enable voice input.");
  const [canSpeak, setCanSpeak] = useState(true);
  const [canListen, setCanListen] = useState(true);
  const [isListening, setIsListening] = useState(false);
  const [vitals, setVitals] = useState([]);
  const [doctors, setDoctors] = useState([]);
  const [startButtonText, setStartButtonText] = useState("Start Session");
  const [startDisabled, setStartDisabled] = useState(false);

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
  const ambientNoiseLevelRef = useRef(0);

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

  function stopMicEnhancementPipeline() {
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
    setInputEnabled(false);
    setMicEnabled(false);
    setMicHint("Connect first to enable voice input.");
  }

  function syncInteractionState() {
    const isConnected = wsRef.current && wsRef.current.readyState === WebSocket.OPEN;
    setInputEnabled(Boolean(isConnected));

    if (!isConnected) {
      setMicEnabled(false);
      setMicHint("Connect first to enable voice input.");
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
    setMicHint(
      isSpeakingRef.current
        ? "Assistant is speaking..."
        : "Auto-listen is ready. You can also press Use Voice."
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

  function sendUserMessage(text) {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
      return;
    }

    awaitingAgentReplyRef.current = true;
    wsRef.current.send(
      JSON.stringify({
        type: "user_message",
        text,
        location: userLocationRef.current,
      })
    );
  }

  function stopListeningUI() {
    isListeningRef.current = false;
    setIsListening(false);
    updateStatus("connected", "Connected");
  }

  function maybeAutoStartListening() {
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
      try {
        recognitionRef.current.start();
      } catch (error) {
        // Ignore rapid re-start errors and keep manual voice button as fallback.
      }
    }, 160);
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
      isListeningRef.current = true;
      setIsListening(true);
      updateStatus("listening", "Listening");
      setMicHint("Listening now...");
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
        syncInteractionState();
        maybeAutoStartListening();
        return;
      }

      const confidenceTooLow = bestConfidence !== null && bestConfidence < MIN_CONFIDENCE_FOR_AUTO_SEND;
      const likelyNoiseBurst = finalTranscript.length < 3 && ambientNoiseLevelRef.current > 0.06;

      if (confidenceTooLow || likelyNoiseBurst) {
        appendMessage(
          "agent",
          "I heard too much background noise. Please speak a bit closer to the mic and try again."
        );
        syncInteractionState();
        maybeAutoStartListening();
        return;
      }

      appendMessage("user", finalTranscript);
      setTyping(true);
      sendUserMessage(finalTranscript);
    };

    recognition.onerror = function (event) {
      stopListeningUI();

      if (event.error === "no-speech") {
        setMicHint("No clear speech detected. Trying again...");
      } else if (event.error === "audio-capture") {
        appendMessage("agent", "I cannot access your microphone right now. Please check permissions.");
      } else if (event.error === "not-allowed") {
        appendMessage("agent", "Microphone permission was denied. You can continue with text chat.");
      } else {
        appendMessage("agent", "I could not catch that. Please try again.");
      }

      syncInteractionState();
      maybeAutoStartListening();
    };

    recognition.onend = function () {
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

  function handleServerMessage(data) {
    if (!data || !data.type) {
      return;
    }

    if (data.type === "agent_message") {
      awaitingAgentReplyRef.current = false;
      setTyping(false);
      appendMessage("agent", data.text || "");
      playAudio(data.audio);
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
      appendMessage("agent", "Something went wrong. Please try again.");
      syncInteractionState();
    }
  }

  async function connectSession() {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      return;
    }

    updateStatus("", "Connecting");
    requestLocation();
    setStartDisabled(true);
    setStartButtonText("Starting...");

    const micSetupResult = await configureEnhancedMicrophone();

    const ws = new WebSocket(resolveWebSocketUrl());
    wsRef.current = ws;

    ws.onopen = function () {
      updateStatus("connected", "Connected");
      setStartButtonText("Session Active");

      if (!recognitionRef.current) {
        recognitionRef.current = initRecognition();
      }

      syncInteractionState();
      if (micSetupResult === "enhanced") {
        setMicHint("Enhanced mic filtering is active. Assistant will auto-listen after each response.");
      } else if (micSetupResult === "unsupported") {
        setMicHint("Auto-listen is active. This browser has limited microphone filtering support.");
      } else {
        setMicHint("Auto-listen is active. For best accuracy, move closer to the mic.");
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
      setStartDisabled(false);
      setStartButtonText("Start Session");
      setMicHint("Connect first to enable voice input.");
      setIsListening(false);
      isListeningRef.current = false;
      isSpeakingRef.current = false;
      awaitingAgentReplyRef.current = false;

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

    try {
      recognitionRef.current.start();
    } catch (error) {
      appendMessage("agent", "Voice input is busy. Try again in a second.");
    }
  }

  function onSubmit(event) {
    event.preventDefault();

    const text = inputValue.trim();
    if (!text) {
      return;
    }

    appendMessage("user", text);
    setTyping(true);
    sendUserMessage(text);
    setInputValue("");
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

          <div className={status.mode ? "status-dot " + status.mode : "status-dot"}>
            <span className="dot"></span>
            <span>{status.text}</span>
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
                <p className="empty-state">Start a session to begin your health conversation.</p>
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
                disabled={!inputEnabled}
                onChange={function (event) {
                  setInputValue(event.target.value);
                }}
                placeholder="Type your update (e.g. My heart rate is 85)"
              />
              <button type="submit" disabled={!inputEnabled}>Send</button>
            </form>

            <div className="controls">
              <button
                className="btn btn-primary"
                type="button"
                onClick={connectSession}
                disabled={startDisabled}
              >
                {startButtonText}
              </button>

              <button
                className={isListening ? "btn btn-secondary listening" : "btn btn-secondary"}
                type="button"
                onClick={toggleVoice}
                disabled={!micEnabled}
              >
                Use Voice
              </button>

              <p className="controls-hint">{micHint}</p>
            </div>
          </section>

          <aside className="sidebar">
            <section className="side-card">
              <h3>Vitals Logged</h3>
              <div className="vitals-list">
                {vitals.length === 0 ? (
                  <p className="empty-state">No vitals yet.</p>
                ) : (
                  vitals.map(function (vital, index) {
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
