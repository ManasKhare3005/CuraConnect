import { useEffect, useState } from "react";
import {
  formatDisplayDate,
  getTitrationMilestones,
  getWeeksSinceStart,
  parseIsoDate,
} from "./glp1Protocols";

const SEVERITY_COLORS = {
  mild: "#198754",
  moderate: "#ffc107",
  severe: "#dc3545",
};

function formatSymptom(symptom) {
  return String(symptom || "Symptom")
    .split(" ")
    .filter(Boolean)
    .map(function (part) {
      return part.charAt(0).toUpperCase() + part.slice(1);
    })
    .join(" ");
}

function normalizeSeverity(value) {
  const normalized = String(value || "mild").toLowerCase();
  return SEVERITY_COLORS[normalized] ? normalized : "mild";
}

function getUniqueSymptoms(sideEffects) {
  const names = [];
  (Array.isArray(sideEffects) ? sideEffects : []).forEach(function (entry) {
    const label = formatSymptom(entry.symptom);
    if (label && !names.includes(label)) {
      names.push(label);
    }
  });
  return names;
}

function getDurationDays(startValue, endValue) {
  const start = parseIsoDate(startValue);
  const end = parseIsoDate(endValue);
  if (!start || !end) {
    return null;
  }
  const msPerDay = 24 * 60 * 60 * 1000;
  return Math.max(1, Math.round((end.getTime() - start.getTime()) / msPerDay));
}

function formatDurationLabel(days) {
  if (!Number.isFinite(days) || days <= 0) {
    return null;
  }
  if (days === 1) {
    return "Lasted 1 day";
  }
  return `Lasted ${days} days`;
}

export default function SideEffectTimeline({ medication, sideEffects = [], onResolve = null }) {
  const [selectedEffectId, setSelectedEffectId] = useState(null);
  const [resolveError, setResolveError] = useState("");
  const [resolvingId, setResolvingId] = useState(null);
  const effects = Array.isArray(sideEffects) ? sideEffects : [];
  const rows = getUniqueSymptoms(effects);

  useEffect(
    function syncSelectedEffect() {
      if (!effects.length) {
        setSelectedEffectId(null);
        return;
      }
      const hasSelected = effects.some(function (entry) {
        return entry.id === selectedEffectId;
      });
      if (!hasSelected) {
        setSelectedEffectId(effects[effects.length - 1].id);
      }
    },
    [effects, selectedEffectId]
  );

  async function handleResolve(effectId) {
    if (!onResolve || !effectId) {
      return;
    }
    setResolveError("");
    setResolvingId(effectId);
    try {
      await onResolve(effectId);
    } catch (error) {
      setResolveError(error?.message || "Could not resolve that side effect right now.");
    } finally {
      setResolvingId(null);
    }
  }

  if (!medication) {
    return (
      <section className="cc-card-shell">
        <div className="cc-section-head">
          <h3>Side Effect Timeline</h3>
          <p>Add an active GLP-1 medication to visualize treatment milestones.</p>
        </div>
        <div className="cc-empty-state">
          <p>No treatment timeline is available yet.</p>
        </div>
      </section>
    );
  }

  if (!effects.length) {
    return (
      <section className="cc-card-shell">
        <div className="cc-section-head">
          <h3>Side Effect Timeline</h3>
          <p>Side effects will appear here as they are logged over the treatment journey.</p>
        </div>
        <div className="cc-empty-state">
          <p>No side effects reported yet.</p>
        </div>
      </section>
    );
  }

  const width = 920;
  const rowHeight = 42;
  const height = Math.max(180, 100 + rows.length * rowHeight);
  const padding = { top: 48, right: 28, bottom: 34, left: 150 };
  const now = new Date();
  const lastEventDate = effects.reduce(function (latest, entry) {
    const parsed = parseIsoDate(entry.resolved_at || entry.reported_at) || (!entry.resolved_at ? now : null);
    if (!parsed) {
      return latest;
    }
    return !latest || parsed > latest ? parsed : latest;
  }, null);
  const totalWeeks = Math.max(
    4,
    Math.ceil(getWeeksSinceStart(medication, lastEventDate || now)) + 1
  );
  const milestones = getTitrationMilestones(medication, totalWeeks);
  const selectedEffect =
    effects.find(function (entry) {
      return entry.id === selectedEffectId;
    }) || effects[effects.length - 1];

  function resolveX(weekValue) {
    return (
      padding.left +
      ((Math.max(0, weekValue) / Math.max(totalWeeks, 1)) * (width - padding.left - padding.right))
    );
  }

  function resolveY(symptomLabel) {
    const rowIndex = Math.max(rows.indexOf(symptomLabel), 0);
    return padding.top + rowIndex * rowHeight + rowHeight / 2;
  }

  const plottedEffects = effects
    .map(function (entry) {
      const symptomLabel = formatSymptom(entry.symptom);
      const severity = normalizeSeverity(entry.severity);
      const reportedAt = parseIsoDate(entry.reported_at);
      const resolvedAt = parseIsoDate(entry.resolved_at);
      const endDate = resolvedAt || now;
      const startWeek = getWeeksSinceStart(medication, reportedAt || medication.start_date);
      const endWeek = getWeeksSinceStart(medication, endDate);
      const durationDays = getDurationDays(entry.reported_at, resolvedAt || now);
      return {
        ...entry,
        symptomLabel,
        severity,
        y: resolveY(symptomLabel),
        startX: resolveX(startWeek),
        endX: resolveX(Math.max(startWeek, endWeek)),
        isResolved: Boolean(entry.resolved_at),
        durationDays,
        durationLabel: resolvedAt ? formatDurationLabel(durationDays) : null,
      };
    })
    .filter(function (entry) {
      return Number.isFinite(entry.startX) && Number.isFinite(entry.endX);
    });

  return (
    <section className="cc-card-shell">
      <div className="cc-section-head">
        <h3>Side Effect Timeline</h3>
        <p>Track symptoms against titration steps and treatment weeks.</p>
      </div>

      <div className="set-legend" aria-label="Side effect legend">
        <span className="set-legend-item">
          <span className="set-legend-dot is-active" aria-hidden="true" />
          Active side effect
        </span>
        <span className="set-legend-item">
          <span className="set-legend-dot is-resolved" aria-hidden="true" />
          Resolved
        </span>
      </div>

      <div className="set-wrap">
        <svg className="set-svg" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Side effect timeline">
          {rows.map(function (symptomLabel, rowIndex) {
            const y = padding.top + rowIndex * rowHeight + rowHeight / 2;
            return (
              <g key={`row-${symptomLabel}`}>
                <line
                  x1={padding.left}
                  x2={width - padding.right}
                  y1={y}
                  y2={y}
                  className="set-row-line"
                />
                <text x={padding.left - 12} y={y + 4} className="set-axis-label set-axis-label-left">
                  {symptomLabel}
                </text>
              </g>
            );
          })}

          {milestones.map(function (milestone) {
            const x = resolveX(milestone.week_offset);
            return (
              <g key={`milestone-${milestone.step}-${milestone.week_offset}`}>
                <line
                  x1={x}
                  x2={x}
                  y1={padding.top - 6}
                  y2={height - padding.bottom}
                  className="set-step-line"
                />
                <text x={x} y={24} className="set-axis-label set-axis-label-top">
                  {`${milestone.dose_mg}mg`}
                </text>
              </g>
            );
          })}

          {Array.from({ length: totalWeeks + 1 }).map(function (_, index) {
            const x = resolveX(index);
            return (
              <text key={`week-${index}`} x={x} y={height - 10} className="set-axis-label set-axis-label-bottom">
                {`W${index}`}
              </text>
            );
          })}

          {plottedEffects.map(function (entry) {
            const isActive = selectedEffect && selectedEffect.id === entry.id;
            return (
              <g
                key={`effect-${entry.id}`}
                onMouseEnter={function () {
                  setSelectedEffectId(entry.id);
                }}
                onClick={function () {
                  setSelectedEffectId(entry.id);
                }}
                className="set-point-group"
              >
                <line
                  x1={entry.startX}
                  x2={entry.endX}
                  y1={entry.y}
                  y2={entry.y}
                  stroke={SEVERITY_COLORS[entry.severity]}
                  className={entry.isResolved ? "set-effect-line is-resolved" : "set-effect-line is-active"}
                />
                <circle
                  cx={entry.startX}
                  cy={entry.y}
                  r={isActive ? 8 : 6}
                  fill={SEVERITY_COLORS[entry.severity]}
                  className={isActive ? "set-point is-active" : "set-point"}
                >
                  <title>{`${entry.symptomLabel} (${entry.severity})`}</title>
                </circle>
                {entry.isResolved ? (
                  <circle
                    cx={entry.endX}
                    cy={entry.y}
                    r={isActive ? 7 : 5.5}
                    className="set-point-resolved"
                  >
                    <title>
                      {entry.durationLabel
                        ? `${entry.symptomLabel} resolved. ${entry.durationLabel}.`
                        : `${entry.symptomLabel} resolved.`}
                    </title>
                  </circle>
                ) : (
                  <circle
                    cx={entry.endX}
                    cy={entry.y}
                    r={isActive ? 8 : 6}
                    stroke={SEVERITY_COLORS[entry.severity]}
                    className="set-point-open"
                  >
                    <title>{`${entry.symptomLabel} is still active.`}</title>
                  </circle>
                )}
              </g>
            );
          })}
        </svg>
      </div>

      {selectedEffect ? (
        <div className="set-detail">
          <div className="set-detail-head">
            <strong>{formatSymptom(selectedEffect.symptom)}</strong>
            <span className={`set-severity-badge is-${normalizeSeverity(selectedEffect.severity)}`}>
              {selectedEffect.severity || "mild"}
            </span>
          </div>
          <div className="set-detail-grid">
            <span>{`Reported ${formatDisplayDate(selectedEffect.reported_at)}`}</span>
            <span>
              {selectedEffect.resolved_at
                ? `Resolved ${formatDisplayDate(selectedEffect.resolved_at)}`
                : "Still active"}
            </span>
            <span>{`Titration step ${selectedEffect.titration_step || "N/A"}`}</span>
            {selectedEffect.resolved_at ? (
              <span>{formatDurationLabel(getDurationDays(selectedEffect.reported_at, selectedEffect.resolved_at))}</span>
            ) : (
              <span>
                {`Active for ${getDurationDays(selectedEffect.reported_at, now) || 1} day${
                  (getDurationDays(selectedEffect.reported_at, now) || 1) === 1 ? "" : "s"
                }`}
              </span>
            )}
          </div>
          {!selectedEffect.resolved_at && onResolve ? (
            <div className="set-detail-actions">
              <button
                type="button"
                className="cc-btn-outline"
                disabled={resolvingId === selectedEffect.id}
                onClick={function () {
                  handleResolve(selectedEffect.id);
                }}
              >
                {resolvingId === selectedEffect.id ? "Marking..." : "Mark Resolved"}
              </button>
              {resolveError ? <span className="set-error">{resolveError}</span> : null}
            </div>
          ) : null}
          {selectedEffect.notes ? <p>{selectedEffect.notes}</p> : null}
        </div>
      ) : null}
    </section>
  );
}
