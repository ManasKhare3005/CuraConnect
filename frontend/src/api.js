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
    const err = new Error(message);
    err.status = response.status;
    throw err;
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

export function fetchMedications(token) {
  return request(API_BASE, `/api/medications?token=${encodeURIComponent(token)}`);
}

export function fetchMedication(token, medicationId) {
  return request(API_BASE, `/api/medications/${medicationId}?token=${encodeURIComponent(token)}`);
}

export function createMedication(token, data) {
  return request(API_BASE, `/api/medications?token=${encodeURIComponent(token)}`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export function updateMedication(token, medicationId, updates) {
  return request(API_BASE, `/api/medications/${medicationId}?token=${encodeURIComponent(token)}`, {
    method: "PATCH",
    body: JSON.stringify(updates),
  });
}

export function logMedicationEvent(token, medicationId, eventData) {
  return request(API_BASE, `/api/medications/${medicationId}/events?token=${encodeURIComponent(token)}`, {
    method: "POST",
    body: JSON.stringify(eventData),
  });
}

export function fetchMedicationEvents(token, medicationId) {
  return request(API_BASE, `/api/medications/${medicationId}/events?token=${encodeURIComponent(token)}`);
}

export function logSideEffect(token, data) {
  return request(API_BASE, `/api/side-effects?token=${encodeURIComponent(token)}`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export function fetchSideEffectTimeline(token) {
  return request(API_BASE, `/api/side-effects/timeline?token=${encodeURIComponent(token)}`);
}

export function resolveSideEffect(token, sideEffectId) {
  return request(API_BASE, `/api/side-effects/${sideEffectId}/resolve?token=${encodeURIComponent(token)}`, {
    method: "POST",
  });
}

export function fetchRefills(token) {
  return request(API_BASE, `/api/refills?token=${encodeURIComponent(token)}`);
}

export function createRefill(token, medicationId) {
  return request(API_BASE, `/api/refills?token=${encodeURIComponent(token)}`, {
    method: "POST",
    body: JSON.stringify({ medication_id: medicationId }),
  });
}

export function updateRefillStatus(token, refillId, status, notes) {
  return request(API_BASE, `/api/refills/${refillId}?token=${encodeURIComponent(token)}`, {
    method: "PATCH",
    body: JSON.stringify({
      status,
      notes: notes || null,
    }),
  });
}

export function fetchPendingRefills(token) {
  return request(API_BASE, `/api/refills/pending?token=${encodeURIComponent(token)}`);
}

export function fetchPendingOutreach(token) {
  return request(API_BASE, `/api/outreach/pending?token=${encodeURIComponent(token)}`);
}

export function fetchUserOutreach(token) {
  return request(API_BASE, `/api/outreach/user?token=${encodeURIComponent(token)}`);
}

export function fetchOutreachStats(token) {
  return request(API_BASE, `/api/outreach/stats?token=${encodeURIComponent(token)}`);
}

export function completeOutreach(token, outreachId, outcomeSummary) {
  return request(API_BASE, `/api/outreach/${outreachId}/complete?token=${encodeURIComponent(token)}`, {
    method: "POST",
    body: JSON.stringify({ outcome_summary: outcomeSummary || null }),
  });
}

export function cancelOutreach(token, outreachId) {
  return request(API_BASE, `/api/outreach/${outreachId}/cancel?token=${encodeURIComponent(token)}`, {
    method: "POST",
  });
}

export function fetchOpenEscalations(token) {
  return request(API_BASE, `/api/escalations?token=${encodeURIComponent(token)}`);
}

export function fetchUserEscalations(token) {
  return request(API_BASE, `/api/escalations/user?token=${encodeURIComponent(token)}`);
}

export function fetchEscalation(token, escalationId) {
  return request(API_BASE, `/api/escalations/${escalationId}?token=${encodeURIComponent(token)}`);
}

export function updateEscalationStatus(token, escalationId, status, resolutionNotes) {
  return request(API_BASE, `/api/escalations/${escalationId}?token=${encodeURIComponent(token)}`, {
    method: "PATCH",
    body: JSON.stringify({
      status,
      resolution_notes: resolutionNotes || null,
    }),
  });
}

export function fetchRetentionCurve(token, days = 30) {
  return request(API_BASE, `/api/analytics/retention?token=${encodeURIComponent(token)}&days=${encodeURIComponent(days)}`);
}

export function fetchEngagementMetrics(token) {
  return request(API_BASE, `/api/analytics/engagement?token=${encodeURIComponent(token)}`);
}

export function fetchRevenueImpact(token, patients, revenue, lift = 10) {
  return request(
    API_BASE,
    `/api/analytics/revenue-impact?token=${encodeURIComponent(token)}&patients=${encodeURIComponent(patients)}&revenue=${encodeURIComponent(revenue)}&lift=${encodeURIComponent(lift)}`
  );
}

export function fetchSupportDeflection(token) {
  return request(API_BASE, `/api/analytics/support-deflection?token=${encodeURIComponent(token)}`);
}

export function fetchRecentActivity(token, limit = 20) {
  return request(
    API_BASE,
    `/api/activity/recent?token=${encodeURIComponent(token)}&limit=${encodeURIComponent(limit)}`
  );
}

export function fetchAuditLog(token, filters = {}) {
  const params = new URLSearchParams({ token });
  if (filters.limit != null) {
    params.set("limit", String(filters.limit));
  }
  if (filters.action) {
    params.set("action", String(filters.action));
  }
  if (filters.startDate) {
    params.set("start_date", String(filters.startDate));
  }
  if (filters.endDate) {
    params.set("end_date", String(filters.endDate));
  }
  return request(API_BASE, `/api/audit/log?${params.toString()}`);
}

export function fetchUserAuditLog(token, userId) {
  return request(API_BASE, `/api/audit/user/${userId}?token=${encodeURIComponent(token)}`);
}

export function fetchConversationHistory(token, options = {}) {
  const limitSessions = Number.isFinite(options.limitSessions) ? Number(options.limitSessions) : 25;
  return request(
    API_BASE,
    `/api/conversations/history?token=${encodeURIComponent(token)}&limit_sessions=${encodeURIComponent(limitSessions)}`
  );
}

export function fetchDoctorHistory(token) {
  return request(API_BASE, `/api/doctors/history?token=${encodeURIComponent(token)}`);
}

export function fetchSessionDoctors(token, sessionId) {
  return request(API_BASE, `/api/doctors/session/${sessionId}?token=${encodeURIComponent(token)}`);
}

export function deleteDoctorRecommendation(token, recommendationId) {
  return request(API_BASE, `/api/doctors/${recommendationId}?token=${encodeURIComponent(token)}`, {
    method: "DELETE",
  });
}

export function deleteConversationSession(token, sessionId) {
  return request(API_BASE, `/api/conversations/${sessionId}?token=${encodeURIComponent(token)}`, {
    method: "DELETE",
  });
}

export function deleteVital(token, vitalId) {
  return request(API_BASE, `/api/vitals/${vitalId}?token=${encodeURIComponent(token)}`, {
    method: "DELETE",
  });
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
