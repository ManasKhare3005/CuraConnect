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

export default function SideEffectTimeline({ medication, sideEffects = [] }) {
  const [selectedEffectId, setSelectedEffectId] = useState(null);
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
  const lastEventDate = effects.reduce(function (latest, entry) {
    const parsed = parseIsoDate(entry.reported_at);
    if (!parsed) {
      return latest;
    }
    return !latest || parsed > latest ? parsed : latest;
  }, null);
  const totalWeeks = Math.max(
    4,
    Math.ceil(getWeeksSinceStart(medication, lastEventDate || new Date())) + 1
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

  return (
    <section className="cc-card-shell">
      <div className="cc-section-head">
        <h3>Side Effect Timeline</h3>
        <p>Track symptoms against titration steps and treatment weeks.</p>
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

          {effects.map(function (entry) {
            const symptomLabel = formatSymptom(entry.symptom);
            const severity = normalizeSeverity(entry.severity);
            const x = resolveX(getWeeksSinceStart(medication, entry.reported_at));
            const y = resolveY(symptomLabel);
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
                <circle
                  cx={x}
                  cy={y}
                  r={isActive ? 8 : 6}
                  fill={SEVERITY_COLORS[severity]}
                  className={isActive ? "set-point is-active" : "set-point"}
                >
                  <title>{`${symptomLabel} (${severity})`}</title>
                </circle>
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
          </div>
          {selectedEffect.notes ? <p>{selectedEffect.notes}</p> : null}
        </div>
      ) : null}
    </section>
  );
}
