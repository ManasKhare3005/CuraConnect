import { useEffect, useState } from "react";
import { createRefill, logMedicationEvent } from "./api";
import {
  formatDisplayDate,
  getDoseWindow,
  getMedicationDisplayName,
  getTitrationProgress,
  getTitrationSchedule,
} from "./glp1Protocols";

function formatTitle(value) {
  return String(value || "")
    .split("_")
    .filter(Boolean)
    .map(function (part) {
      return part.charAt(0).toUpperCase() + part.slice(1);
    })
    .join(" ");
}

function getSeveritySummary(sideEffects) {
  const counts = { mild: 0, moderate: 0, severe: 0 };
  (Array.isArray(sideEffects) ? sideEffects : []).forEach(function (entry) {
    const severity = String(entry.severity || "").toLowerCase();
    if (Object.prototype.hasOwnProperty.call(counts, severity)) {
      counts[severity] += 1;
    }
  });
  return counts;
}

function getStatusClass(status) {
  const normalized = String(status || "active").toLowerCase();
  if (normalized === "paused") {
    return "is-paused";
  }
  if (normalized === "discontinued") {
    return "is-discontinued";
  }
  return "is-active";
}

export default function MedicationCard({ token, medication, refills = [], onRefresh }) {
  const [submittingDose, setSubmittingDose] = useState(false);
  const [requestingRefill, setRequestingRefill] = useState(false);
  const [statusMessage, setStatusMessage] = useState("");
  const [error, setError] = useState("");

  useEffect(
    function resetMessages() {
      setStatusMessage("");
      setError("");
    },
    [medication && medication.id]
  );

  if (!medication) {
    return (
      <section className="mc-card">
        <div className="cc-section-head">
          <h3>Medication</h3>
          <p>No active GLP-1 medication is on file yet.</p>
        </div>
        <div className="cc-empty-state">
          <p>Add a medication from the clinician workflow to unlock titration and refill tracking.</p>
        </div>
      </section>
    );
  }

  const schedule = getTitrationSchedule(medication.drug_name);
  const progress = getTitrationProgress(medication);
  const doseWindow = getDoseWindow(medication);
  const sideEffects = Array.isArray(medication.side_effects) ? medication.side_effects : [];
  const severitySummary = getSeveritySummary(sideEffects);
  const totalSteps = schedule ? schedule.steps.length : medication.titration_step || 1;
  const activeRefill =
    (Array.isArray(refills) ? refills : []).find(function (entry) {
      return Number(entry.medication_id) === Number(medication.id);
    }) || null;
  const refillLabel = activeRefill ? formatTitle(activeRefill.status) : "Request Refill";

  async function handleLogDose() {
    setSubmittingDose(true);
    setError("");
    setStatusMessage("");
    try {
      await logMedicationEvent(token, medication.id, {
        event_type: "dose_taken",
        dose_mg: medication.current_dose_mg,
        notes: "Logged from the dashboard.",
      });
      setStatusMessage("Dose logged successfully.");
      if (typeof onRefresh === "function") {
        await onRefresh();
      }
    } catch (err) {
      setError(err.message || "Unable to log the dose right now.");
    } finally {
      setSubmittingDose(false);
    }
  }

  async function handleRequestRefill() {
    setRequestingRefill(true);
    setError("");
    setStatusMessage("");
    try {
      if (activeRefill) {
        setStatusMessage(`Current refill status: ${refillLabel}.`);
      } else {
        await createRefill(token, medication.id);
        setStatusMessage("Refill request flagged for the care team.");
      }
      if (typeof onRefresh === "function") {
        await onRefresh();
      }
    } catch (err) {
      setError(err.message || "Unable to request a refill right now.");
    } finally {
      setRequestingRefill(false);
    }
  }

  return (
    <section className="mc-card">
      <div className="mc-head">
        <div>
          <span className="mc-eyebrow">Active Medication</span>
          <h3>{getMedicationDisplayName(medication)}</h3>
        </div>
        <span className={`mc-status ${getStatusClass(medication.status)}`}>
          {formatTitle(medication.status)}
        </span>
      </div>

      <div className="mc-dose-line">
        <strong>{`${medication.current_dose_mg}mg`}</strong>
        <span>{`Step ${medication.titration_step || 1} of ${totalSteps}`}</span>
      </div>

      <div className="mc-progress">
        <div className="mc-progress-track">
          <div className="mc-progress-fill" style={{ width: `${progress}%` }}></div>
        </div>
        <div className="mc-progress-meta">
          <span>{schedule && doseWindow.currentStep ? doseWindow.currentStep.label : "Custom schedule"}</span>
          <span>{`${Math.round(progress)}%`}</span>
        </div>
      </div>

      <div className="mc-grid">
        <div className="mc-metric">
          <span className="mc-metric-label">Days on current dose</span>
          <strong>
            {doseWindow.daysOnCurrentDose == null ? "Not available" : `${doseWindow.daysOnCurrentDose} days`}
          </strong>
        </div>
        <div className="mc-metric">
          <span className="mc-metric-label">Next titration date</span>
          <strong>
            {doseWindow.nextTitrationDate ? formatDisplayDate(doseWindow.nextTitrationDate) : "Maintenance dose"}
          </strong>
        </div>
        <div className="mc-metric">
          <span className="mc-metric-label">Route</span>
          <strong>{medication.route || "Subcutaneous injection"}</strong>
        </div>
        <div className="mc-metric">
          <span className="mc-metric-label">Frequency</span>
          <strong>{formatTitle(medication.frequency || "weekly")}</strong>
        </div>
      </div>

      <div className="mc-actions">
        <button
          type="button"
          className="pc-btn pc-btn-primary"
          onClick={handleLogDose}
          disabled={submittingDose}
        >
          {submittingDose ? "Logging..." : "Log Dose"}
        </button>
        <button
          type="button"
          className="pc-btn pc-btn-secondary"
          onClick={handleRequestRefill}
          disabled={requestingRefill}
        >
          {requestingRefill ? "Submitting..." : refillLabel}
        </button>
      </div>

      <div className="mc-side-effects">
        <div className="mc-side-effects-head">
          <strong>Side Effect Summary</strong>
          <span>{`${sideEffects.length} logged`}</span>
        </div>
        {sideEffects.length === 0 ? (
          <p className="mc-side-effects-empty">No side effects reported for this medication yet.</p>
        ) : (
          <div className="mc-side-effects-row">
            <span className="mc-chip is-mild">{`Mild ${severitySummary.mild}`}</span>
            <span className="mc-chip is-moderate">{`Moderate ${severitySummary.moderate}`}</span>
            <span className="mc-chip is-severe">{`Severe ${severitySummary.severe}`}</span>
          </div>
        )}
      </div>

      {statusMessage ? <p className="mc-feedback is-success">{statusMessage}</p> : null}
      {error ? <p className="pc-error">{error}</p> : null}
    </section>
  );
}
