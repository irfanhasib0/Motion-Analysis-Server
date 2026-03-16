import React, { useMemo, useState } from 'react';
import metricConfig from './metric_config.json';
import {
  LineChart, Line, XAxis, YAxis, Tooltip, Legend, ResponsiveContainer, CartesianGrid, ReferenceLine
} from 'recharts';

const METRICS = [
  { key: 'vel', label: metricConfig.labels.vel, defaultOn: false },
  { key: 'diff', label: metricConfig.labels.diff, defaultOn: false },
  { key: 'loudness', label: metricConfig.labels.loudness, defaultOn: false },
  { key: 'duration', label: metricConfig.labels.duration, defaultOn: true },
];

const DAY_COLORS = [
  '#e6194b', '#3cb44b', '#4363d8', '#f58231', '#911eb4',
  '#42d4f4', '#f032e6', '#bfef45', '#fabebe', '#469990',
  '#e6beff', '#9A6324', '#fffac8', '#800000', '#aaffc3',
  '#808000', '#ffd8b1', '#000075', '#a9a9a9', '#dcbeff',
  '#00a86b', '#ff6f61', '#6b5b95', '#88b04b', '#955251',
  '#b565a7', '#009b77', '#dd4124', '#d65076', '#45b8ac',
  '#efc050',
];

const formatMinute = (min) => {
  const h = Math.floor(min / 60);
  const m = Math.floor(min % 60);
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
};

const AVG_WINDOWS = [
  { value: 0, label: 'Raw' },
  { value: 10, label: '10 min' },
  { value: 30, label: '30 min' },
  { value: 60, label: '60 min' },
];

const MotionActivityChart = ({ recordings, cameras = [], onDayClick }) => {
  const [filterCamera, setFilterCamera] = useState('');
  const [avgWindow, setAvgWindow] = useState(10);
  const [activeMetrics, setActiveMetrics] = useState(() => {
    const init = {};
    METRICS.forEach(m => { init[m.key] = m.defaultOn; });
    return init;
  });
  // Load default Y max values from config
  const DEFAULT_YMAX = metricConfig.max;
  const [yMaxOverrides, setYMaxOverrides] = useState({});

  // Filter recordings by selected camera
  const filteredRecs = useMemo(() => {
    if (!filterCamera) return recordings;
    return recordings.filter(r => r.camera_id === filterCamera);
  }, [recordings, filterCamera]);

  // Build merged data array for LineChart with optional averaging and value clipping
  const { chartDataByMetric, dayList } = useMemo(() => {
    const byDate = {};
    for (const rec of filteredRecs) {
      const ts = rec.started_at || rec.created_at;
      if (!ts) continue;
      const d = new Date(ts);
      const dateStr = ts.slice(0, 10);
      const minuteOfDay = Math.round(d.getHours() * 60 + d.getMinutes() + d.getSeconds() / 60);
      if (!byDate[dateStr]) byDate[dateStr] = [];
      const meta = rec.metadata || {};
      byDate[dateStr].push({
        minute: minuteOfDay,
        vel: meta.vel != null ? Math.min(Number(meta.vel), DEFAULT_YMAX.vel) : null,
        diff: meta.diff != null ? Math.min(Number(meta.diff), DEFAULT_YMAX.diff) : null,
        loudness: meta.loudness != null ? Math.min(Number(meta.loudness), DEFAULT_YMAX.loudness) : null,
        duration: rec.duration != null ? Math.min(Number(rec.duration), DEFAULT_YMAX.duration) : null,
        id: rec.id,
        camera_id: rec.camera_id,
      });
    }
    const days = Object.keys(byDate).sort();
    // Average points into buckets if window > 0
    const processedByDate = {};
    for (const date of days) {
      const pts = byDate[date];
      if (avgWindow === 0) {
        processedByDate[date] = pts;
      } else {
        const buckets = {};
        for (const pt of pts) {
          const bucketCenter = Math.floor(pt.minute / avgWindow) * avgWindow + avgWindow / 2;
          if (!buckets[bucketCenter]) buckets[bucketCenter] = [];
          buckets[bucketCenter].push(pt);
        }
        processedByDate[date] = Object.entries(buckets).map(([center, group]) => {
          const c = Number(center);
          const avg = { minute: c, id: null, camera_id: null };
          // Average each metric
          for (const mk of ['vel', 'diff', 'loudness', 'duration']) {
            const vals = group.filter(p => p[mk] != null).map(p => p[mk]);
            avg[mk] = vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null;
          }
          // Pick recording nearest to bucket center for click navigation
          let bestDist = Infinity;
          for (const p of group) {
            const dist = Math.abs(p.minute - c);
            if (dist < bestDist && p.id) { bestDist = dist; avg.id = p.id; avg.camera_id = p.camera_id; }
          }
          return avg;
        });
      }
    }
    // Build per-metric merged data arrays
    const dataByMetric = {};
    for (const mk of ['vel', 'diff', 'loudness', 'duration']) {
      const minuteMap = {};
      for (const date of days) {
        for (const pt of processedByDate[date]) {
          if (pt[mk] == null) continue;
          const key = pt.minute;
          if (!minuteMap[key]) minuteMap[key] = { minute: key };
          minuteMap[key][`${mk}_${date}`] = pt[mk];
          minuteMap[key][`id_${date}`] = pt.id;
          minuteMap[key][`cam_${date}`] = pt.camera_id;
        }
      }
      dataByMetric[mk] = Object.values(minuteMap).sort((a, b) => a.minute - b.minute);
    }
    return { chartDataByMetric: dataByMetric, dayList: days };
  }, [filteredRecs, avgWindow]);

  const toggleMetric = (key) => {
    setActiveMetrics(prev => ({ ...prev, [key]: !prev[key] }));
  };

  // Current time as minute-of-day for the reference line
  const now = new Date();
  const nowMinute = now.getHours() * 60 + now.getMinutes();

  const CustomTooltip = ({ active, payload, label }) => {
    if (!active || !payload?.length) return null;
    return (
      <div style={{
        background: 'rgba(30,30,30,0.92)', border: '1px solid #444', borderRadius: 6,
        padding: '6px 10px', fontSize: 12, color: '#eee',
      }}>
        <div style={{ fontWeight: 700, marginBottom: 2 }}>{formatMinute(label)}</div>
        {payload.map((entry, i) => (
          entry.value != null && (
            <div key={i} style={{ color: entry.color }}>
              {entry.name}: {typeof entry.value === 'number' ? entry.value.toFixed(2) : entry.value}
            </div>
          )
        ))}
      </div>
    );
  };

  if (!dayList.length) {
    return (
      <div style={{ padding: 16, color: '#78909c', textAlign: 'center', fontSize: 13 }}>
        No recordings with metadata available for chart.
      </div>
    );
  }

  return (
    <div>
      {/* Camera select + Metric checkboxes */}
      <div style={{ display: 'flex', gap: 16, marginBottom: 10, flexWrap: 'wrap', alignItems: 'center' }}>
        <select
          value={filterCamera}
          onChange={(e) => setFilterCamera(e.target.value)}
          style={{ padding: '3px 6px', fontSize: 12, borderRadius: 4, border: '1px solid #ccc' }}
        >
          <option value="">All Cameras</option>
          {cameras.map(c => (
            <option key={c.id} value={c.id}>{c.name}</option>
          ))}
        </select>
        <select
          value={avgWindow}
          onChange={(e) => setAvgWindow(Number(e.target.value))}
          style={{ padding: '3px 6px', fontSize: 12, borderRadius: 4, border: '1px solid #ccc' }}
        >
          {AVG_WINDOWS.map(w => (
            <option key={w.value} value={w.value}>{w.label}</option>
          ))}
        </select>
        {METRICS.map(m => (
          <label key={m.key} style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 13, cursor: 'pointer', color: '#333' }}>
            <input
              type="checkbox"
              checked={!!activeMetrics[m.key]}
              onChange={() => toggleMetric(m.key)}
            />
            {m.label}
          </label>
        ))}
      </div>

      {/* One chart per active metric */}
      {METRICS.filter(m => activeMetrics[m.key]).map(metric => {
        const yMax = yMaxOverrides[metric.key] ?? DEFAULT_YMAX[metric.key];
        const data = chartDataByMetric[metric.key] || [];
        return (
        <div key={metric.key} style={{ marginBottom: 16 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
            <span style={{ fontSize: 13, fontWeight: 600, color: '#37474f' }}>
              {metric.label}
            </span>
            <label style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 11, color: '#78909c', marginLeft: 'auto' }}>
              Y max:
              <input
                type="number"
                min="0"
                step="any"
                placeholder="auto"
                value={yMax ?? ''}
                onChange={(e) => {
                  const v = e.target.value;
                  setYMaxOverrides(prev => ({ ...prev, [metric.key]: v === '' ? undefined : Number(v) }));
                }}
                style={{ width: 70, padding: '2px 4px', fontSize: 11, borderRadius: 4, border: '1px solid #ccc' }}
              />
            </label>
          </div>
          <ResponsiveContainer width="100%" height={200}>
            <LineChart data={data} margin={{ top: 5, right: 10, left: 0, bottom: 5 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="rgba(0,0,0,0.06)" />
              <XAxis
                dataKey="minute"
                type="number"
                domain={[0, 1440]}
                ticks={[0, 180, 360, 540, 720, 900, 1080, 1260, 1440]}
                tickFormatter={formatMinute}
                tick={{ fontSize: 10 }}
              />
              <YAxis
                tick={{ fontSize: 10 }}
                width={50}
                domain={[0, yMax != null ? yMax : 'auto']}
                allowDataOverflow={yMax != null}
              />
              <Tooltip content={<CustomTooltip />} />
              <ReferenceLine x={nowMinute} stroke="#f44336" strokeWidth={1} strokeDasharray="3 3" label={{ value: 'Now', position: 'top', fontSize: 9, fill: '#f44336' }} />
              <Legend wrapperStyle={{ fontSize: 11 }} />
              {dayList.map((date, idx) => (
                <Line
                  key={date}
                  name={date}
                  dataKey={`${metric.key}_${date}`}
                  type="monotone"
                  stroke={DAY_COLORS[idx % DAY_COLORS.length]}
                  strokeWidth={1.5}
                  dot={{ r: 2.5 }}
                  activeDot={onDayClick ? (props) => {
                    const { cx, cy, payload, fill } = props;
                    return (
                      <circle
                        cx={cx} cy={cy} r={4} fill={fill} stroke="#fff" strokeWidth={1}
                        style={{ cursor: 'pointer' }}
                        onClick={() => onDayClick(date, payload[`id_${date}`], payload[`cam_${date}`])}
                      />
                    );
                  } : { r: 4 }}
                  connectNulls
                />
              ))}
            </LineChart>
          </ResponsiveContainer>
        </div>
        );
      })}
    </div>
  );
};

export default MotionActivityChart;
