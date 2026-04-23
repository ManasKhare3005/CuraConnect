const TITRATION_SCHEDULES = {
  semaglutide: {
    brand_names: ["Ozempic", "Wegovy"],
    steps: [
      { step: 1, dose_mg: 0.25, duration_weeks: 4, label: "Starting dose" },
      { step: 2, dose_mg: 0.5, duration_weeks: 4, label: "Escalation 1" },
      { step: 3, dose_mg: 1.0, duration_weeks: 4, label: "Escalation 2" },
      { step: 4, dose_mg: 1.7, duration_weeks: 4, label: "Escalation 3" },
      { step: 5, dose_mg: 2.4, duration_weeks: null, label: "Maintenance" },
    ],
  },
  tirzepatide: {
    brand_names: ["Mounjaro", "Zepbound"],
    steps: [
      { step: 1, dose_mg: 2.5, duration_weeks: 4, label: "Starting dose" },
      { step: 2, dose_mg: 5.0, duration_weeks: 4, label: "Escalation 1" },
      { step: 3, dose_mg: 7.5, duration_weeks: 4, label: "Escalation 2" },
      { step: 4, dose_mg: 10.0, duration_weeks: 4, label: "Escalation 3" },
      { step: 5, dose_mg: 12.5, duration_weeks: 4, label: "Escalation 4" },
      { step: 6, dose_mg: 15.0, duration_weeks: null, label: "Maintenance" },
    ],
  },
};

function normalizeDrugName(drugName) {
  return String(drugName || "").trim().toLowerCase();
}

export function parseIsoDate(value) {
  if (!value) {
    return null;
  }

  if (value instanceof Date) {
    return Number.isNaN(value.getTime()) ? null : value;
  }

  const candidate = String(value).trim();
  if (!candidate) {
    return null;
  }

  if (/^\d{4}-\d{2}-\d{2}$/.test(candidate)) {
    const parts = candidate.split("-").map(Number);
    return new Date(Date.UTC(parts[0], parts[1] - 1, parts[2]));
  }

  const parsed = new Date(candidate);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

export function formatDisplayDate(value, options) {
  const parsed = parseIsoDate(value);
  if (!parsed) {
    return "Not scheduled";
  }
  return parsed.toLocaleDateString(
    "en-US",
    options || { month: "short", day: "numeric", year: "numeric" }
  );
}

export function getTitrationSchedule(drugName) {
  return TITRATION_SCHEDULES[normalizeDrugName(drugName)] || null;
}

export function getScheduleStep(drugName, stepNumber) {
  const schedule = getTitrationSchedule(drugName);
  if (!schedule) {
    return null;
  }

  const normalizedStep = Number(stepNumber) || 1;
  for (let i = 0; i < schedule.steps.length; i += 1) {
    if (Number(schedule.steps[i].step) === normalizedStep) {
      return schedule.steps[i];
    }
  }

  return null;
}

export function getNextScheduleStep(drugName, stepNumber) {
  return getScheduleStep(drugName, (Number(stepNumber) || 1) + 1);
}

function addDays(dateValue, days) {
  const parsed = parseIsoDate(dateValue);
  if (!parsed) {
    return null;
  }
  const copy = new Date(parsed.getTime());
  copy.setUTCDate(copy.getUTCDate() + Number(days || 0));
  return copy;
}

function dayDiff(startDate, endDate) {
  if (!startDate || !endDate) {
    return null;
  }
  const msPerDay = 24 * 60 * 60 * 1000;
  return Math.max(Math.floor((endDate.getTime() - startDate.getTime()) / msPerDay), 0);
}

export function getMedicationDisplayName(medication) {
  if (!medication) {
    return "No active medication";
  }
  const drug = medication.drug_name || "Medication";
  return medication.brand_name ? `${drug} (${medication.brand_name})` : drug;
}

export function getDoseWindow(medication) {
  if (!medication) {
    return {
      schedule: null,
      currentStep: null,
      nextStep: null,
      currentStepStart: null,
      nextTitrationDate: null,
      daysOnCurrentDose: null,
    };
  }

  const schedule = getTitrationSchedule(medication.drug_name);
  const currentStepNumber = Number(medication.titration_step) || 1;
  const currentStep = getScheduleStep(medication.drug_name, currentStepNumber);
  const nextStep = getNextScheduleStep(medication.drug_name, currentStepNumber);
  const startDate = parseIsoDate(medication.start_date);

  if (!schedule || !currentStep || !startDate) {
    return {
      schedule,
      currentStep,
      nextStep,
      currentStepStart: null,
      nextTitrationDate: null,
      daysOnCurrentDose: null,
    };
  }

  let elapsedWeeks = 0;
  for (let index = 0; index < schedule.steps.length; index += 1) {
    const step = schedule.steps[index];
    if (Number(step.step) === currentStepNumber) {
      break;
    }
    elapsedWeeks += Number(step.duration_weeks || 0);
  }

  const currentStepStart = addDays(startDate, elapsedWeeks * 7);
  const nextTitrationDate =
    currentStep.duration_weeks == null
      ? null
      : addDays(currentStepStart, Number(currentStep.duration_weeks || 0) * 7);
  const daysOnCurrentDose = currentStepStart ? dayDiff(currentStepStart, new Date()) : null;

  return {
    schedule,
    currentStep,
    nextStep,
    currentStepStart,
    nextTitrationDate,
    daysOnCurrentDose,
  };
}

export function getTitrationProgress(medication) {
  const schedule = getTitrationSchedule(medication && medication.drug_name);
  if (!schedule || schedule.steps.length === 0) {
    return 0;
  }
  const currentStepNumber = Number((medication && medication.titration_step) || 1);
  return Math.max(
    0,
    Math.min(100, (currentStepNumber / schedule.steps.length) * 100)
  );
}

export function getTitrationMilestones(medication, horizonWeeks = null) {
  const schedule = getTitrationSchedule(medication && medication.drug_name);
  const startDate = parseIsoDate(medication && medication.start_date);
  if (!schedule || !startDate) {
    return [];
  }

  let elapsedWeeks = 0;
  const milestones = [];
  const resolvedHorizonWeeks =
    Number.isFinite(horizonWeeks) && horizonWeeks > 0
      ? horizonWeeks
      : schedule.steps.reduce(function (sum, step) {
          return sum + Number(step.duration_weeks || 0);
        }, 4);

  for (let index = 0; index < schedule.steps.length; index += 1) {
    const step = schedule.steps[index];
    milestones.push({
      step: step.step,
      dose_mg: step.dose_mg,
      label: step.label,
      week_offset: elapsedWeeks,
      date: addDays(startDate, elapsedWeeks * 7),
    });
    elapsedWeeks += Number(step.duration_weeks || 0);
  }

  if (milestones.length > 0 && elapsedWeeks < resolvedHorizonWeeks) {
    milestones.push({
      step: "current",
      dose_mg: medication.current_dose_mg,
      label: "Current dose",
      week_offset: resolvedHorizonWeeks,
      date: addDays(startDate, resolvedHorizonWeeks * 7),
    });
  }

  return milestones;
}

export function getWeeksSinceStart(medication, eventDate) {
  const startDate = parseIsoDate(medication && medication.start_date);
  const occurrence = parseIsoDate(eventDate);
  if (!startDate || !occurrence) {
    return 0;
  }
  return Math.max(dayDiff(startDate, occurrence) / 7, 0);
}
