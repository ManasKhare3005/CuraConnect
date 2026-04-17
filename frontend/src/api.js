const configuredApiBase = (import.meta.env.VITE_API_URL || "").trim();
const configuredSttBase = (import.meta.env.VITE_STT_URL || "").trim();
const configuredWsUrl = (import.meta.env.VITE_WS_URL || "").trim();

function stripTrailingSlash(value) {
  return value.replace(/\/+$/, "");
}

function inferApiBase() {
  if (configuredApiBase) {
    return stripTrailingSlash(configuredApiBase);
  }

  // In dev the frontend runs on a different port than the backend (8000).
  // Rewrite whenever the port isn't 8000 (covers Vite on any port).
  const host =
    window.location.port && window.location.port !== "8000"
      ? `${window.location.hostname}:8000`
      : window.location.host;

  return `${window.location.protocol}//${host}`;
}

const API_BASE = inferApiBase();
const STT_BASE = configuredSttBase ? stripTrailingSlash(configuredSttBase) : API_BASE;

export function resolveWebSocketUrl(token) {
  let base;
  if (configuredWsUrl) {
    base = configuredWsUrl;
  } else {
    const wsProtocol = window.location.protocol === "https:" ? "wss" : "ws";
    const apiUrl = new URL(API_BASE);
    base = `${wsProtocol}://${apiUrl.host}/ws`;
  }
  return token ? `${base}?token=${encodeURIComponent(token)}` : base;
}

async function request(baseUrl, path, init = {}) {
  const headers = new Headers(init.headers || {});
  if (!(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(`${baseUrl}${path}`, {
    ...init,
    headers,
  });

  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      if (body && typeof body.detail === "string") {
        message = body.detail;
      } else if (body && typeof body.message === "string") {
        message = body.message;
      }
    } catch (error) {
      // Keep default message.
    }
    throw new Error(message);
  }

  return response.json();
}

export function checkBackendHealth() {
  return request(API_BASE, "/health");
}

export function checkSttHealth() {
  return request(STT_BASE, "/api/stt/health");
}

export function enhanceTranscript(transcript, options = {}) {
  return request(STT_BASE, "/api/stt/enhance", {
    method: "POST",
    body: JSON.stringify({
      transcript,
      expected_keywords: options.expectedKeywords || [],
      custom_terms: options.customTerms || [],
    }),
  });
}

export function transcribeAudioBlob(audioBlob, options = {}) {
  const formData = new FormData();
  formData.append("audio", audioBlob, options.filename || "voice.webm");
  formData.append("language", options.language || "en");

  if (options.expectedKeywords && options.expectedKeywords.length > 0) {
    formData.append("expected_keywords", options.expectedKeywords.join(","));
  }
  if (options.customTerms && options.customTerms.length > 0) {
    formData.append("custom_terms", options.customTerms.join(","));
  }

  return request(STT_BASE, "/api/stt/transcribe", {
    method: "POST",
    body: formData,
  });
}

export function registerUser(name, email, password, dob) {
  return request(API_BASE, "/api/auth/register", {
    method: "POST",
    body: JSON.stringify({ name, email, password, dob: dob || null }),
  });
}

export function loginUser(email, password) {
  return request(API_BASE, "/api/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
}

export function fetchVitalsHistory(token) {
  return request(API_BASE, `/api/vitals/history?token=${encodeURIComponent(token)}`);
}

export function fetchConversationHistory(token, options = {}) {
  const limitSessions = Number.isFinite(options.limitSessions) ? Number(options.limitSessions) : 25;
  return request(
    API_BASE,
    `/api/conversations/history?token=${encodeURIComponent(token)}&limit_sessions=${encodeURIComponent(limitSessions)}`
  );
}

export function deleteAccount(token) {
  return request(API_BASE, `/api/auth/account?token=${encodeURIComponent(token)}`, {
    method: "DELETE",
  });
}

export function updateProfile(token, updates) {
  return request(API_BASE, `/api/auth/profile?token=${encodeURIComponent(token)}`, {
    method: "PATCH",
    body: JSON.stringify(updates),
  });
}
