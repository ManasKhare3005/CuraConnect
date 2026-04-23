function buildLinePath(points) {
  if (!points.length) {
    return "";
  }
  if (points.length === 1) {
    return `M ${points[0].x} ${points[0].y}`;
  }

  let path = `M ${points[0].x} ${points[0].y}`;
  for (let index = 0; index < points.length - 1; index += 1) {
    const current = points[index];
    const next = points[index + 1];
    const midpointX = (current.x + next.x) / 2;
    path += ` Q ${midpointX} ${current.y}, ${next.x} ${next.y}`;
  }
  return path;
}

function normalizeSeries(data, maxDay, width, height, padding) {
  const safeData = Array.isArray(data) ? data : [];
  return safeData.map(function (point) {
    const day = Number(point.day || 0);
    const retention = Math.max(0, Math.min(100, Number(point.retention_pct || 0)));
    const x =
      padding.left +
      ((Math.max(day, 0) / Math.max(maxDay, 1)) * (width - padding.left - padding.right));
    const y =
      padding.top +
      ((100 - retention) / 100) * (height - padding.top - padding.bottom);
    return {
      x,
      y,
      day,
      retention,
    };
  });
}

export default function RetentionChart({ data = [], baseline = [] }) {
  const width = 760;
  const height = 280;
  const padding = { top: 20, right: 18, bottom: 34, left: 42 };
  const safeData = Array.isArray(data) ? data : [];
  const safeBaseline = Array.isArray(baseline) ? baseline : [];
  const maxDay = safeData.reduce(function (maxValue, point) {
    return Math.max(maxValue, Number(point.day || 0));
  }, 30);
  const clinicPoints = normalizeSeries(safeData, maxDay, width, height, padding);
  const baselinePoints = normalizeSeries(safeBaseline, maxDay, width, height, padding);
  const yTicks = [0, 25, 50, 75, 100];
  const xTicks = [0, 7, 14, 21, 30];

  return (
    <div className="rc">
      <div className="rc-legend">
        <span className="rc-legend-item">
          <span className="rc-legend-swatch rc-legend-clinic"></span>
          With CuraConnect
        </span>
        <span className="rc-legend-item">
          <span className="rc-legend-swatch rc-legend-baseline"></span>
          Industry Average
        </span>
      </div>

      <div className="rc-svg-wrap">
        <svg className="rc-svg" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="30 day retention chart">
          {yTicks.map(function (tick) {
            const y =
              padding.top +
              ((100 - tick) / 100) * (height - padding.top - padding.bottom);
            return (
              <g key={`y-${tick}`}>
                <line
                  x1={padding.left}
                  x2={width - padding.right}
                  y1={y}
                  y2={y}
                  className="rc-grid-line"
                />
                <text x={padding.left - 8} y={y + 4} className="rc-axis-label rc-axis-label-y">
                  {tick}
                </text>
              </g>
            );
          })}

          {xTicks.map(function (tick) {
            const x =
              padding.left +
              ((tick / Math.max(maxDay, 1)) * (width - padding.left - padding.right));
            return (
              <g key={`x-${tick}`}>
                <line
                  x1={x}
                  x2={x}
                  y1={padding.top}
                  y2={height - padding.bottom}
                  className="rc-grid-line rc-grid-line-vertical"
                />
                <text x={x} y={height - 10} className="rc-axis-label rc-axis-label-x">
                  Day {tick}
                </text>
              </g>
            );
          })}

          <path
            d={buildLinePath(baselinePoints)}
            className="rc-line rc-line-baseline"
          />
          <path
            d={buildLinePath(clinicPoints)}
            className="rc-line rc-line-clinic"
          />

          {clinicPoints.map(function (point) {
            return (
              <circle
                key={`clinic-${point.day}`}
                cx={point.x}
                cy={point.y}
                r="3.5"
                className="rc-dot rc-dot-clinic"
              >
                <title>{`Day ${point.day}: ${point.retention}%`}</title>
              </circle>
            );
          })}

          {baselinePoints.map(function (point) {
            return (
              <circle
                key={`baseline-${point.day}`}
                cx={point.x}
                cy={point.y}
                r="3"
                className="rc-dot rc-dot-baseline"
              >
                <title>{`Industry day ${point.day}: ${point.retention}%`}</title>
              </circle>
            );
          })}
        </svg>
      </div>
    </div>
  );
}
