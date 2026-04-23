import {
  LineChart,
  Line,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
  ReferenceLine,
} from "recharts";

const HEALTH_RANGES = {
  heart_rate: {
    label: "Heart Rate",
    unit: "bpm",
    normal: [60, 100],
    warning: [50, 130],
    color: "#e11d48",
    lines: [
      { y: 60, label: "Min Normal", stroke: "#22c55e" },
      { y: 100, label: "Max Normal", stroke: "#f97316" },
    ],
  },
  temperature: {
    label: "Temperature",
    unit: "°",
    normal: [36.1, 37.5],
    warning: [35, 39],
    color: "#f97316",
    lines: [
      { y: 36.1, label: "Low Normal", stroke: "#22c55e" },
      { y: 37.5, label: "High Normal", stroke: "#f97316" },
    ],
  },
  oxygen_saturation: {
    label: "SpO₂",
    unit: "%",
    normal: [95, 100],
    warning: [90, 100],
    color: "#0ea5e9",
    lines: [{ y: 95, label: "Min Normal", stroke: "#22c55e" }],
  },
  blood_glucose: {
    label: "Blood Glucose",
    unit: "mg/dL",
    normal: [70, 140],
    warning: [60, 200],
    color: "#a855f7",
    lines: [
      { y: 70, label: "Min Normal", stroke: "#22c55e" },
      { y: 140, label: "Max Normal", stroke: "#f97316" },
    ],
  },
  weight: {
    label: "Weight",
    unit: "kg",
    normal: null,
    warning: null,
    color: "#0f766e",
    lines: [],
  },
};

function getStatus(vitalType, value) {
  const cfg = HEALTH_RANGES[vitalType];
  if (!cfg || cfg.normal === null) return "neutral";
  if (value >= cfg.normal[0] && value <= cfg.normal[1]) return "normal";
  if (cfg.warning && value >= cfg.warning[0] && value <= cfg.warning[1]) return "warning";
  return "critical";
}

function getBPStatus(systolic, diastolic) {
  if (systolic < 90 || diastolic < 60) return "warning";
  if (systolic <= 120 && diastolic <= 80) return "normal";
  if (systolic <= 129 && diastolic < 80) return "elevated";
  if (systolic <= 139 || diastolic <= 89) return "warning";
  return "critical";
}

function parseNumericValue(valueStr) {
  if (!valueStr) return null;
  const n = parseFloat(String(valueStr).replace(/[^\d.]/g, ""));
  return Number.isFinite(n) ? n : null;
}

function parseBP(valueStr) {
  if (!valueStr) return null;
  const m = String(valueStr).match(/(\d+)\s*[\/\-]\s*(\d+)/);
  if (!m) return null;
  return { systolic: parseFloat(m[1]), diastolic: parseFloat(m[2]) };
}

function formatDate(isoStr) {
  if (!isoStr) return "";
  const d = new Date(isoStr);
  if (Number.isNaN(d.getTime())) return isoStr;
  return `${d.getMonth() + 1}/${d.getDate()}`;
}

function buildChartData(vitals, vitalType) {
  return vitals
    .filter((v) => v.vital_type === vitalType)
    .map((v) => ({ date: formatDate(v.recorded_at), value: parseNumericValue(v.value), raw: v.value }))
    .filter((d) => d.value !== null)
    .reverse();
}

function buildBPData(vitals) {
  return vitals
    .filter((v) => v.vital_type === "blood_pressure")
    .map((v) => {
      const bp = parseBP(v.value);
      if (!bp) return null;
      return { date: formatDate(v.recorded_at), systolic: bp.systolic, diastolic: bp.diastolic };
    })
    .filter(Boolean)
    .reverse();
}

function buildSymptomData(vitals) {
  const counts = {};
  vitals.filter((v) => v.vital_type === "symptom").forEach((v) => {
    const key = v.value || "Unknown";
    counts[key] = (counts[key] || 0) + 1;
  });
  return Object.entries(counts)
    .map(([name, count]) => ({ name, count }))
    .sort((a, b) => b.count - a.count)
    .slice(0, 8);
}

function StatusBadge({ status }) {
  const map = {
    normal: { cls: "cc-badge-normal", label: "Normal" },
    elevated: { cls: "cc-badge-warning", label: "Elevated" },
    warning: { cls: "cc-badge-warning", label: "Check Needed" },
    critical: { cls: "cc-badge-critical", label: "See Doctor" },
    neutral: { cls: "cc-badge-neutral", label: "Tracked" },
  };
  const info = map[status] || map.neutral;
  return <span className={`cc-health-badge ${info.cls}`}>{info.label}</span>;
}

function DoctorAdvice({ vitals }) {
  const issues = [];

  const latestByType = {};
  [...vitals].reverse().forEach((v) => {
    if (!latestByType[v.vital_type]) latestByType[v.vital_type] = v;
  });

  const hr = latestByType.heart_rate;
  if (hr) {
    const v = parseNumericValue(hr.value);
    if (v !== null && (v < 50 || v > 130)) issues.push("abnormal heart rate");
  }

  const bp = latestByType.blood_pressure;
  if (bp) {
    const parsed = parseBP(bp.value);
    if (parsed) {
      const st = getBPStatus(parsed.systolic, parsed.diastolic);
      if (st === "critical" || st === "warning") issues.push("elevated blood pressure");
    }
  }

  const temp = latestByType.temperature;
  if (temp) {
    const v = parseNumericValue(temp.value);
    if (v !== null && v > 39) issues.push("high fever");
    else if (v !== null && v < 35) issues.push("low body temperature");
  }

  const spo2 = latestByType.oxygen_saturation;
  if (spo2) {
    const v = parseNumericValue(spo2.value);
    if (v !== null && v < 90) issues.push("low oxygen saturation");
  }

  const glucose = latestByType.blood_glucose;
  if (glucose) {
    const v = parseNumericValue(glucose.value);
    if (v !== null && (v > 200 || v < 60)) issues.push("abnormal blood glucose");
  }

  if (issues.length === 0) {
    return (
      <div className="cc-doctor-advice cc-advice-ok">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M22 11.1V12a10 10 0 1 1-5.9-9.1"></path><polyline points="22 4 12 14.1 9 11.1"></polyline></svg>
        <div>
          <strong>Looking good!</strong>
          <p>Your recent vitals are within normal ranges. Keep up the healthy habits.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="cc-doctor-advice cc-advice-warn">
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M10.3 3.3L2 20h20L13.7 3.3a2 2 0 0 0-3.4 0z"></path><line x1="12" y1="9" x2="12" y2="13"></line><line x1="12" y1="17" x2="12.01" y2="17"></line></svg>
      <div>
        <strong>Consider seeing a doctor</strong>
        <p>Flagged: {issues.join(", ")}. Please consult a healthcare professional.</p>
      </div>
    </div>
  );
}

function VitalLineChart({ title, data, color, lines, unit }) {
  if (data.length === 0) return null;
  const latest = data[data.length - 1]?.value;
  return (
    <div className="cc-graph-card">
      <div className="cc-graph-card-header">
        <span className="cc-graph-title">{title}</span>
        {latest !== null && latest !== undefined && (
          <span className="cc-graph-latest">{latest}{unit}</span>
        )}
      </div>
      <ResponsiveContainer width="100%" height={180}>
        <LineChart data={data} margin={{ top: 8, right: 12, left: -10, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
          <XAxis dataKey="date" tick={{ fontSize: 11 }} stroke="#94a3b8" />
          <YAxis tick={{ fontSize: 11 }} stroke="#94a3b8" />
          <Tooltip
            contentStyle={{ fontSize: 12, borderRadius: 8, border: "1px solid #e2e8f0" }}
            formatter={(v) => [`${v}${unit}`, title]}
          />
          {lines.map((l) => (
            <ReferenceLine key={l.y} y={l.y} stroke={l.stroke} strokeDasharray="4 2" label={{ value: l.label, fontSize: 10, fill: l.stroke }} />
          ))}
          <Line type="monotone" dataKey="value" stroke={color} strokeWidth={2} dot={{ r: 3 }} activeDot={{ r: 5 }} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

function BPChart({ data }) {
  if (data.length === 0) return null;
  const latest = data[data.length - 1];
  return (
    <div className="cc-graph-card">
      <div className="cc-graph-card-header">
        <span className="cc-graph-title">Blood Pressure</span>
        {latest && (
          <span className="cc-graph-latest">{latest.systolic}/{latest.diastolic} mmHg</span>
        )}
      </div>
      <ResponsiveContainer width="100%" height={180}>
        <LineChart data={data} margin={{ top: 8, right: 12, left: -10, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
          <XAxis dataKey="date" tick={{ fontSize: 11 }} stroke="#94a3b8" />
          <YAxis tick={{ fontSize: 11 }} stroke="#94a3b8" />
          <Tooltip contentStyle={{ fontSize: 12, borderRadius: 8, border: "1px solid #e2e8f0" }} />
          <Legend wrapperStyle={{ fontSize: 11 }} />
          <ReferenceLine y={120} stroke="#22c55e" strokeDasharray="4 2" />
          <ReferenceLine y={80} stroke="#22c55e" strokeDasharray="4 2" />
          <Line type="monotone" dataKey="systolic" stroke="#e11d48" strokeWidth={2} dot={{ r: 3 }} />
          <Line type="monotone" dataKey="diastolic" stroke="#0ea5e9" strokeWidth={2} dot={{ r: 3 }} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

function SymptomChart({ data }) {
  if (data.length === 0) return null;
  return (
    <div className="cc-graph-card">
      <div className="cc-graph-card-header">
        <span className="cc-graph-title">Symptoms Frequency</span>
      </div>
      <ResponsiveContainer width="100%" height={180}>
        <BarChart data={data} margin={{ top: 8, right: 12, left: -10, bottom: 0 }} layout="vertical">
          <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
          <XAxis type="number" tick={{ fontSize: 11 }} stroke="#94a3b8" allowDecimals={false} />
          <YAxis type="category" dataKey="name" tick={{ fontSize: 10 }} stroke="#94a3b8" width={80} />
          <Tooltip contentStyle={{ fontSize: 12, borderRadius: 8, border: "1px solid #e2e8f0" }} />
          <Bar dataKey="count" fill="#0f766e" radius={[0, 4, 4, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

function StatusRow({ vitals }) {
  const latestByType = {};
  [...vitals].reverse().forEach((v) => {
    if (!latestByType[v.vital_type]) latestByType[v.vital_type] = v;
  });

  const items = [
    { key: "heart_rate", label: "Heart Rate" },
    { key: "blood_pressure", label: "Blood Pressure" },
    { key: "temperature", label: "Temp" },
    { key: "oxygen_saturation", label: "SpO₂" },
    { key: "blood_glucose", label: "Glucose" },
  ].filter(({ key }) => latestByType[key]);

  if (items.length === 0) return null;

  return (
    <div className="cc-status-chips">
      {items.map(({ key, label }) => {
        const vital = latestByType[key];
        let status;
        if (key === "blood_pressure") {
          const bp = parseBP(vital.value);
          status = bp ? getBPStatus(bp.systolic, bp.diastolic) : "neutral";
        } else {
          const v = parseNumericValue(vital.value);
          status = v !== null ? getStatus(key, v) : "neutral";
        }
        return (
          <div key={key} className="cc-status-chip">
            <span className="cc-chip-label">{label}</span>
            <span className="cc-chip-value">{vital.value}{vital.unit ? ` ${vital.unit}` : ""}</span>
            <StatusBadge status={status} />
          </div>
        );
      })}
    </div>
  );
}

export default function VitalsGraph({ vitals }) {
  if (!vitals || vitals.length === 0) {
    return (
      <div className="cc-panel-list">
        <p className="cc-empty-panel">No vitals yet. Share your readings in chat to see graphs here.</p>
      </div>
    );
  }

  const bpData = buildBPData(vitals);
  const symptomData = buildSymptomData(vitals);

  const numericCharts = Object.entries(HEALTH_RANGES).map(([type, cfg]) => {
    const data = buildChartData(vitals, type);
    return { type, cfg, data };
  }).filter(({ data }) => data.length > 0);

  return (
    <div className="cc-graphs-panel">
      <DoctorAdvice vitals={vitals} />
      <StatusRow vitals={vitals} />

      {bpData.length > 0 && <BPChart data={bpData} />}

      {numericCharts.map(({ type, cfg, data }) => (
        <VitalLineChart
          key={type}
          title={cfg.label}
          data={data}
          color={cfg.color}
          lines={cfg.lines}
          unit={cfg.unit}
        />
      ))}

      {symptomData.length > 0 && <SymptomChart data={symptomData} />}
    </div>
  );
}
