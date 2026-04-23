import { useEffect, useState } from "react";
import {
  cancelOutreach,
  completeOutreach,
  fetchEngagementMetrics,
  fetchEscalation,
  fetchOpenEscalations,
  fetchPendingOutreach,
  fetchRecentActivity,
  fetchRetentionCurve,
  fetchRevenueImpact,
  fetchSupportDeflection,
  updateEscalationStatus,
} from "./api";
import RetentionChart from "./RetentionChart";

function formatNumber(value, suffix = "") {
  const numeric = Number(value || 0);
  if (!Number.isFinite(numeric)) {
    return `0${suffix}`;
  }
  return `${numeric.toLocaleString("en-US", { maximumFractionDigits: numeric % 1 === 0 ? 0 : 1 })}${suffix}`;
}

function formatDateTime(value) {
  if (!value) {
    return "Not available";
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  return parsed.toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function formatRelativeTime(value) {
  if (!value) {
    return "just now";
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  const diffMs = Date.now() - parsed.getTime();
  const diffMinutes = Math.max(Math.round(diffMs / 60000), 0);
  if (diffMinutes < 60) {
    return `${diffMinutes || 1}m ago`;
  }
  const diffHours = Math.round(diffMinutes / 60);
  if (diffHours < 24) {
    return `${diffHours}h ago`;
  }
  const diffDays = Math.round(diffHours / 24);
  return `${diffDays}d ago`;
}

function formatLabel(value) {
  return String(value || "")
    .split("_")
    .filter(Boolean)
    .map(function (part) {
      return part.charAt(0).toUpperCase() + part.slice(1);
    })
    .join(" ");
}

function SummaryCard({ label, value, hint, tone = "" }) {
  const className = tone ? `cd-summary-card is-${tone}` : "cd-summary-card";
  return (
    <article className={className}>
      <span className="cd-summary-label">{label}</span>
      <strong className="cd-summary-value">{value}</strong>
      <span className="cd-summary-hint">{hint}</span>
    </article>
  );
}

function SeverityBadge({ severity }) {
  const normalized = String(severity || "moderate").toLowerCase();
  return <span className={`cd-badge is-${normalized}`}>{formatLabel(normalized)}</span>;
}

function PriorityBadge({ priority }) {
  const numeric = Number(priority || 5);
  let tone = "normal";
  if (numeric <= 2) {
    tone = "high";
  } else if (numeric <= 4) {
    tone = "medium";
  }
  return <span className={`cd-priority is-${tone}`}>{`Priority ${numeric}`}</span>;
}

function ActivityIcon({ type }) {
  if (type === "escalation_created") {
    return "!";
  }
  if (type === "outreach_completed") {
    return "O";
  }
  if (type === "refill_flagged") {
    return "R";
  }
  return "D";
}

function DetailList({ items = [], emptyText, renderItem }) {
  if (!Array.isArray(items) || items.length === 0) {
    return <p className="cd-empty-copy">{emptyText}</p>;
  }
  return (
    <div className="cd-detail-list">
      {items.map(function (item, index) {
        return (
          <div className="cd-detail-item" key={(item.id || item.recorded_at || item.timestamp || index).toString()}>
            {renderItem(item)}
          </div>
        );
      })}
    </div>
  );
}

export default function ClinicDashboard({ token }) {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [engagement, setEngagement] = useState(null);
  const [retention, setRetention] = useState(null);
  const [supportDeflection, setSupportDeflection] = useState(null);
  const [openEscalations, setOpenEscalations] = useState([]);
  const [pendingOutreach, setPendingOutreach] = useState([]);
  const [recentActivity, setRecentActivity] = useState([]);
  const [restrictedSections, setRestrictedSections] = useState({});
  const [escalationDetails, setEscalationDetails] = useState({});
  const [expandedEscalationId, setExpandedEscalationId] = useState(null);
  const [modalState, setModalState] = useState(null);
  const [revenuePatients, setRevenuePatients] = useState("1000");
  const [revenuePerPatient, setRevenuePerPatient] = useState("300");
  const [revenueImpact, setRevenueImpact] = useState(null);

  async function loadDashboardData() {
    if (!token) {
      return;
    }

    setLoading(true);
    setError("");

    const results = await Promise.allSettled([
      fetchEngagementMetrics(token),
      fetchRetentionCurve(token, 30),
      fetchSupportDeflection(token),
      fetchOpenEscalations(token),
      fetchPendingOutreach(token),
      fetchRecentActivity(token, 20),
    ]);

    const nextRestricted = {};
    const nextErrorMessages = [];
    let sessionExpiredMessage = "";

    if (results[0].status === "fulfilled") {
      setEngagement(results[0].value);
      if (Number(results[0].value.total_patients || 0) > 0 && (revenuePatients === "" || revenuePatients === "1000")) {
        setRevenuePatients(String(results[0].value.total_patients));
      }
    } else if (results[0].reason && results[0].reason.status === 401) {
      sessionExpiredMessage = "Your session expired. Please sign in again.";
      setError(sessionExpiredMessage);
    } else {
      nextErrorMessages.push(results[0].reason?.message || "Unable to load engagement metrics.");
    }

    if (results[1].status === "fulfilled") {
      setRetention(results[1].value);
    } else if (results[1].reason && results[1].reason.status === 401) {
      sessionExpiredMessage = "Your session expired. Please sign in again.";
      setError(sessionExpiredMessage);
    } else {
      nextErrorMessages.push(results[1].reason?.message || "Unable to load retention analytics.");
    }

    if (results[2].status === "fulfilled") {
      setSupportDeflection(results[2].value);
    } else if (results[2].reason && results[2].reason.status !== 401) {
      nextErrorMessages.push(results[2].reason?.message || "Unable to load support deflection data.");
    }

    if (results[3].status === "fulfilled") {
      setOpenEscalations(Array.isArray(results[3].value) ? results[3].value : []);
    } else if (results[3].reason && results[3].reason.status === 403) {
      nextRestricted.escalations = true;
      setOpenEscalations([]);
    } else if (results[3].reason && results[3].reason.status !== 401) {
      nextErrorMessages.push(results[3].reason?.message || "Unable to load escalations.");
    }

    if (results[4].status === "fulfilled") {
      setPendingOutreach(Array.isArray(results[4].value) ? results[4].value : []);
    } else if (results[4].reason && results[4].reason.status === 403) {
      nextRestricted.outreach = true;
      setPendingOutreach([]);
    } else if (results[4].reason && results[4].reason.status !== 401) {
      nextErrorMessages.push(results[4].reason?.message || "Unable to load outreach queue.");
    }

    if (results[5].status === "fulfilled") {
      setRecentActivity(Array.isArray(results[5].value) ? results[5].value : []);
    } else if (results[5].reason && results[5].reason.status === 403) {
      nextRestricted.activity = true;
      setRecentActivity([]);
    } else if (results[5].reason && results[5].reason.status !== 401) {
      nextErrorMessages.push(results[5].reason?.message || "Unable to load recent activity.");
    }

    setRestrictedSections(nextRestricted);
    if (!sessionExpiredMessage && nextErrorMessages.length > 0) {
      setError(nextErrorMessages[0]);
    }
    setLoading(false);
  }

  useEffect(
    function initialLoad() {
      void loadDashboardData();
    },
    [token]
  );

  useEffect(
    function loadRevenueImpact() {
      if (!token) {
        return undefined;
      }

      const patients = Number(revenuePatients || 0);
      const revenue = Number(revenuePerPatient || 0);
      if (!Number.isFinite(patients) || !Number.isFinite(revenue) || patients < 0 || revenue < 0) {
        setRevenueImpact(null);
        return undefined;
      }

      const timer = window.setTimeout(function () {
        fetchRevenueImpact(token, patients, revenue, 10)
          .then(function (payload) {
            setRevenueImpact(payload);
          })
          .catch(function () {
            setRevenueImpact(null);
          });
      }, 140);

      return function cleanup() {
        window.clearTimeout(timer);
      };
    },
    [token, revenuePatients, revenuePerPatient]
  );

  async function toggleEscalation(escalation) {
    const nextId = expandedEscalationId === escalation.id ? null : escalation.id;
    setExpandedEscalationId(nextId);
    if (!nextId || escalationDetails[nextId]) {
      return;
    }
    try {
      const detail = await fetchEscalation(token, escalation.id);
      setEscalationDetails(function (prev) {
        return { ...prev, [escalation.id]: detail };
      });
    } catch (_) {
      setEscalationDetails(function (prev) {
        return { ...prev, [escalation.id]: escalation };
      });
    }
  }

  async function acknowledgeEscalation(escalationId) {
    try {
      await updateEscalationStatus(token, escalationId, "acknowledged", null);
      await loadDashboardData();
    } catch (err) {
      setError(err.message || "Unable to acknowledge escalation.");
    }
  }

  async function submitModalAction() {
    if (!modalState) {
      return;
    }

    setModalState(function (prev) {
      return prev ? { ...prev, saving: true } : prev;
    });

    try {
      if (modalState.kind === "resolve_escalation") {
        await updateEscalationStatus(token, modalState.itemId, "resolved", modalState.text || null);
      } else if (modalState.kind === "complete_outreach") {
        await completeOutreach(token, modalState.itemId, modalState.text || null);
      }
      setModalState(null);
      await loadDashboardData();
    } catch (err) {
      setError(err.message || "Unable to complete that action.");
      setModalState(function (prev) {
        return prev ? { ...prev, saving: false } : prev;
      });
    }
  }

  async function cancelQueueItem(outreachId) {
    try {
      await cancelOutreach(token, outreachId);
      await loadDashboardData();
    } catch (err) {
      setError(err.message || "Unable to cancel outreach.");
    }
  }

  const retentionValue =
    retention && Array.isArray(retention.curve) && retention.curve.length > 0
      ? retention.curve[retention.curve.length - 1].retention_pct
      : 0;

  return (
    <div className="cd">
      <div className="cc-section-head">
        <h2>Clinic Dashboard</h2>
        <p>Monitor retention, escalation risk, outreach operations, and patient engagement.</p>
      </div>

      {error ? <div className="cd-banner-error">{error}</div> : null}

      <section className="cd-summary-grid">
        <SummaryCard
          label="Active Patients"
          value={loading ? "..." : formatNumber(engagement && engagement.active_last_30_days)}
          hint="Patients active in the last 30 days"
        />
        <SummaryCard
          label="Retention Rate"
          value={loading ? "..." : formatNumber(retentionValue, "%")}
          hint="30 day cohort retention"
        />
        <SummaryCard
          label="Open Escalations"
          value={loading ? "..." : formatNumber(engagement && engagement.escalations_open)}
          hint="Requires clinician review"
          tone={Number(engagement && engagement.escalations_open) > 0 ? "critical" : ""}
        />
        <SummaryCard
          label="Pending Outreach"
          value={loading ? "..." : formatNumber(engagement && engagement.outreach_pending)}
          hint="Due to be worked now"
        />
        <SummaryCard
          label="Support Deflection"
          value={loading ? "..." : formatNumber(supportDeflection && supportDeflection.deflection_rate_pct, "%")}
          hint="Resolved without clinician escalation"
        />
      </section>

      <section className="cd-panel cd-panel-chart">
        <div className="cd-panel-head">
          <div>
            <h3>30 Day Retention Curve</h3>
            <p>Compare your clinic retention against the static industry baseline.</p>
          </div>
        </div>
        <RetentionChart
          data={retention && Array.isArray(retention.curve) ? retention.curve : []}
          baseline={retention && Array.isArray(retention.industry_baseline) ? retention.industry_baseline : []}
        />

        <div className="cd-revenue">
          <div className="cd-revenue-inputs">
            <label className="cd-field">
              <span>Patients per month</span>
              <input
                type="number"
                min="0"
                value={revenuePatients}
                onChange={function (event) {
                  setRevenuePatients(event.target.value);
                }}
              />
            </label>
            <label className="cd-field">
              <span>Avg revenue per patient</span>
              <input
                type="number"
                min="0"
                step="0.01"
                value={revenuePerPatient}
                onChange={function (event) {
                  setRevenuePerPatient(event.target.value);
                }}
              />
            </label>
          </div>
          <div className="cd-revenue-output">
            <strong>
              {revenueImpact
                ? `A ${revenueImpact.retention_lift_pct}% retention lift = $${formatNumber(
                    revenueImpact.annual_recovered_revenue,
                    ""
                  )} in recovered annual revenue`
                : "Enter patient and revenue values to estimate recovered annual revenue."}
            </strong>
          </div>
        </div>
      </section>

      <div className="cd-grid">
        <section className="cd-panel">
          <div className="cd-panel-head">
            <div>
              <h3>Open Escalations</h3>
              <p>Expand an escalation to review the pre-charted clinical brief.</p>
            </div>
          </div>

          {loading && openEscalations.length === 0 ? (
            <div className="cc-empty-state">
              <p>Loading escalations...</p>
            </div>
          ) : restrictedSections.escalations ? (
            <div className="cc-empty-state">
              <p>This queue is available to clinician dashboard accounts.</p>
            </div>
          ) : openEscalations.length === 0 ? (
            <div className="cc-empty-state">
              <p>No open escalations right now.</p>
            </div>
          ) : (
            <div className="cd-table">
              {openEscalations.map(function (escalation) {
                const detail = escalationDetails[escalation.id] || escalation;
                const brief = detail.brief || {};
                const isExpanded = expandedEscalationId === escalation.id;
                const patientName =
                  (detail.user && detail.user.name) ||
                  (brief.patient && brief.patient.name) ||
                  `Patient #${detail.user_id}`;

                return (
                  <article className="cd-row-card" key={escalation.id}>
                    <div className="cd-row-main">
                      <div className="cd-row-summary">
                        <strong>{patientName}</strong>
                        <span>{detail.trigger_reason}</span>
                      </div>
                      <div className="cd-row-meta">
                        <SeverityBadge severity={detail.severity} />
                        <span>{formatRelativeTime(detail.created_at)}</span>
                      </div>
                    </div>

                    <div className="cd-row-actions">
                      <button
                        type="button"
                        className="pc-btn pc-btn-secondary"
                        onClick={function () {
                          void toggleEscalation(detail);
                        }}
                      >
                        {isExpanded ? "Hide Brief" : "View Brief"}
                      </button>
                      <button
                        type="button"
                        className="pc-btn pc-btn-secondary"
                        onClick={function () {
                          void acknowledgeEscalation(detail.id);
                        }}
                      >
                        Acknowledge
                      </button>
                      <button
                        type="button"
                        className="pc-btn pc-btn-primary"
                        onClick={function () {
                          setModalState({
                            kind: "resolve_escalation",
                            itemId: detail.id,
                            title: `Resolve escalation for ${patientName}`,
                            text: "",
                            saving: false,
                          });
                        }}
                      >
                        Resolve
                      </button>
                    </div>

                    {isExpanded ? (
                      <div className="cd-brief">
                        <div className="cd-brief-grid">
                          <div className="cd-brief-card">
                            <h4>Patient</h4>
                            <p>{patientName}</p>
                            <p>{`Age ${brief.patient && brief.patient.age != null ? brief.patient.age : "N/A"}`}</p>
                            <p>{`Weight ${brief.patient && brief.patient.weight_kg != null ? brief.patient.weight_kg : "N/A"} kg`}</p>
                            <p>{`Height ${brief.patient && brief.patient.height_cm != null ? brief.patient.height_cm : "N/A"} cm`}</p>
                          </div>
                          <div className="cd-brief-card">
                            <h4>Medication</h4>
                            <p>{detail.medication && detail.medication.brand_name ? detail.medication.brand_name : detail.medication?.drug_name || "No active medication"}</p>
                            <p>{`Dose ${brief.current_medication && brief.current_medication.current_dose_mg != null ? brief.current_medication.current_dose_mg : detail.medication?.current_dose_mg || "N/A"}mg`}</p>
                            <p>{`Step ${brief.current_medication && brief.current_medication.titration_step != null ? brief.current_medication.titration_step : detail.medication?.titration_step || "N/A"}`}</p>
                            <p>{`Triggered ${formatDateTime(detail.created_at)}`}</p>
                          </div>
                        </div>

                        <div className="cd-brief-section">
                          <h4>Recent Vitals</h4>
                          <DetailList
                            items={brief.recent_vitals || []}
                            emptyText="No recent vitals were included in this brief."
                            renderItem={function (item) {
                              return (
                                <>
                                  <strong>{formatLabel(item.type)}</strong>
                                  <span>{String(item.value || "--")}</span>
                                  <span>{formatDateTime(item.recorded_at)}</span>
                                </>
                              );
                            }}
                          />
                        </div>

                        <div className="cd-brief-section">
                          <h4>Side Effect Timeline</h4>
                          <DetailList
                            items={brief.side_effect_timeline || []}
                            emptyText="No side effects were included in this brief."
                            renderItem={function (item) {
                              return (
                                <>
                                  <strong>{item.symptom}</strong>
                                  <span>{`Step ${item.step || "N/A"}`}</span>
                                  <span>{formatDateTime(item.reported_at)}</span>
                                </>
                              );
                            }}
                          />
                        </div>

                        <div className="cd-brief-section">
                          <h4>Conversation Excerpt</h4>
                          <DetailList
                            items={brief.conversation_excerpt || []}
                            emptyText="No conversation excerpt is available."
                            renderItem={function (item) {
                              return (
                                <>
                                  <strong>{formatLabel(item.role)}</strong>
                                  <span>{item.content}</span>
                                  <span>{formatDateTime(item.timestamp)}</span>
                                </>
                              );
                            }}
                          />
                        </div>

                        <div className="cd-brief-section">
                          <h4>Recommended Actions</h4>
                          <DetailList
                            items={brief.recommended_actions || []}
                            emptyText="No recommended actions were generated."
                            renderItem={function (item) {
                              return <span>{typeof item === "string" ? item : JSON.stringify(item)}</span>;
                            }}
                          />
                        </div>
                      </div>
                    ) : null}
                  </article>
                );
              })}
            </div>
          )}
        </section>

        <section className="cd-panel">
          <div className="cd-panel-head">
            <div>
              <h3>Pending Outreach Queue</h3>
              <p>Prioritized by urgency and scheduled execution time.</p>
            </div>
          </div>

          {loading && pendingOutreach.length === 0 ? (
            <div className="cc-empty-state">
              <p>Loading outreach queue...</p>
            </div>
          ) : restrictedSections.outreach ? (
            <div className="cc-empty-state">
              <p>This queue is available to clinician dashboard accounts.</p>
            </div>
          ) : pendingOutreach.length === 0 ? (
            <div className="cc-empty-state">
              <p>No outreach items are pending right now.</p>
            </div>
          ) : (
            <div className="cd-table">
              {pendingOutreach.map(function (item) {
                const patientName = item.user_name || `Patient #${item.user_id}`;
                return (
                  <article className="cd-row-card" key={item.id}>
                    <div className="cd-row-main">
                      <div className="cd-row-summary">
                        <strong>{patientName}</strong>
                        <span>{formatLabel(item.outreach_type)}</span>
                      </div>
                      <div className="cd-row-meta">
                        <PriorityBadge priority={item.priority} />
                        <span>{formatDateTime(item.scheduled_at)}</span>
                      </div>
                    </div>
                    <div className="cd-row-inline">
                      <span>{`Channel: ${formatLabel(item.channel)}`}</span>
                      <span>{`Attempts: ${item.attempt_count || 0}/${item.max_attempts || 3}`}</span>
                    </div>
                    <div className="cd-row-actions">
                      <button
                        type="button"
                        className="pc-btn pc-btn-primary"
                        onClick={function () {
                          setModalState({
                            kind: "complete_outreach",
                            itemId: item.id,
                            title: `Complete outreach for ${patientName}`,
                            text: item.outcome_summary || "",
                            saving: false,
                          });
                        }}
                      >
                        Mark Complete
                      </button>
                      <button
                        type="button"
                        className="pc-btn pc-btn-secondary"
                        onClick={function () {
                          void cancelQueueItem(item.id);
                        }}
                      >
                        Cancel
                      </button>
                    </div>
                  </article>
                );
              })}
            </div>
          )}
        </section>
      </div>

      <section className="cd-panel">
        <div className="cd-panel-head">
          <div>
            <h3>Recent Activity Feed</h3>
            <p>Latest dose, escalation, outreach, and refill events across the clinic.</p>
          </div>
        </div>

        {loading && recentActivity.length === 0 ? (
          <div className="cc-empty-state">
            <p>Loading recent activity...</p>
          </div>
        ) : restrictedSections.activity ? (
          <div className="cc-empty-state">
            <p>This feed is available to clinician dashboard accounts.</p>
          </div>
        ) : recentActivity.length === 0 ? (
          <div className="cc-empty-state">
            <p>No recent activity yet.</p>
          </div>
        ) : (
          <div className="cd-activity-feed">
            {recentActivity.map(function (item) {
              return (
                <article className="cd-activity-item" key={`${item.activity_type}-${item.activity_id}`}>
                  <span className={`cd-activity-icon is-${item.activity_type}`}>
                    <ActivityIcon type={item.activity_type} />
                  </span>
                  <div className="cd-activity-copy">
                    <strong>{item.user_name || `Patient #${item.user_id || "?"}`}</strong>
                    <p>{item.summary || formatLabel(item.activity_type)}</p>
                    <span>{formatRelativeTime(item.occurred_at)}</span>
                  </div>
                </article>
              );
            })}
          </div>
        )}
      </section>

      {modalState ? (
        <div className="cd-modal-backdrop" onClick={function () { setModalState(null); }}>
          <div className="cd-modal" onClick={function (event) { event.stopPropagation(); }}>
            <h3>{modalState.title}</h3>
            <textarea
              value={modalState.text}
              onChange={function (event) {
                const nextValue = event.target.value;
                setModalState(function (prev) {
                  return prev ? { ...prev, text: nextValue } : prev;
                });
              }}
              placeholder="Add notes for the care team"
            ></textarea>
            <div className="cd-modal-actions">
              <button
                type="button"
                className="pc-btn pc-btn-secondary"
                onClick={function () {
                  setModalState(null);
                }}
                disabled={modalState.saving}
              >
                Cancel
              </button>
              <button
                type="button"
                className="pc-btn pc-btn-primary"
                onClick={function () {
                  void submitModalAction();
                }}
                disabled={modalState.saving}
              >
                {modalState.saving ? "Saving..." : "Save"}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
