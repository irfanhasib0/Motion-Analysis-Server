import React, { useEffect, useState, useCallback } from 'react';
import {
  LineChart, Line, XAxis, YAxis, Tooltip, Legend, ResponsiveContainer,
  CartesianGrid, ReferenceLine,
} from 'recharts';
import api from '../../api';

const CHART_HEIGHT = 196;

const SERIES = [
  { key: 'cpu',      label: 'System CPU %',     color: '#e6194b', dash: undefined },
  { key: 'mem',      label: 'System RAM %',      color: '#4363d8', dash: undefined },
  { key: 'proc_cpu', label: 'Process CPU %',     color: '#f58231', dash: '5 5' },
  { key: 'proc_mem', label: 'Process RAM %',     color: '#911eb4', dash: '5 5' },
];

const formatTime = (ts) => {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
};

const formatTimeFull = (ts) => {
  if (!ts) return '';
  return new Date(ts * 1000).toLocaleTimeString();
};

const CustomTooltip = ({ active, payload, label }) => {
  if (!active || !payload || !payload.length) return null;
  return (
    <div style={{ background: '#fff', border: '1px solid #ddd', borderRadius: 6, padding: '8px 12px', fontSize: 12, boxShadow: '0 2px 8px rgba(0,0,0,0.15)' }}>
      <div style={{ color: '#666', marginBottom: 4 }}>{formatTimeFull(label)}</div>
      {payload.map((entry, i) => (
        <div key={i} style={{ color: entry.color, marginBottom: 2 }}>
          {entry.name}: {Number(entry.value).toFixed(1)}%
        </div>
      ))}
    </div>
  );
};

const ResourceUsageChart = () => {
  const [chartData, setChartData] = useState([]);
  const [visible, setVisible] = useState({ cpu: true, mem: true, proc_cpu: true, proc_mem: false });

  const fetchHistory = useCallback(async () => {
    try {
      const res = await api.getResourceHistory();
      setChartData(res.data || []);
    } catch {
      // stay empty
    }
  }, []);

  useEffect(() => {
    fetchHistory();
    const id = setInterval(fetchHistory, 60000);
    return () => clearInterval(id);
  }, [fetchHistory]);

  const toggle = (key) => setVisible(prev => ({ ...prev, [key]: !prev[key] }));

  const hasData = chartData.length > 0;

  return (
    <div>
      {/* Controls */}
      <div style={{ display: 'flex', gap: 12, marginBottom: 12, flexWrap: 'wrap', alignItems: 'center' }}>
        {SERIES.map(s => (
          <label key={s.key} style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 13, color: '#546e7a', cursor: 'pointer' }}>
            <input type="checkbox" checked={!!visible[s.key]} onChange={() => toggle(s.key)} />
            <span style={{ color: s.color, fontWeight: 600 }}>{s.label}</span>
          </label>
        ))}
      </div>

      {/* Chart */}
      {hasData ? (
        <ResponsiveContainer width="100%" height={CHART_HEIGHT}>
          <LineChart data={chartData} margin={{ top: 5, right: 20, left: 10, bottom: 5 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#e0e0e0" />
            <XAxis
              dataKey="ts" tickFormatter={formatTime} stroke="#999"
              tick={{ fontSize: 11, fill: '#666' }} interval="preserveStartEnd"
            />
            <YAxis
              stroke="#999" tick={{ fontSize: 11, fill: '#666' }}
              domain={[0, 100]}
              label={{ value: '%', angle: -90, position: 'insideLeft', style: { fill: '#666', fontSize: 12 } }}
            />
            <Tooltip content={<CustomTooltip />} />
            <Legend wrapperStyle={{ fontSize: 12 }} formatter={(value) => <span style={{ color: '#546e7a' }}>{value}</span>} />
            <ReferenceLine y={80} stroke="#ff7043" strokeDasharray="6 3" label={{ value: '80%', fill: '#ff7043', fontSize: 10 }} />
            {SERIES.filter(s => visible[s.key]).map(s => (
              <Line
                key={s.key} type="monotone" dataKey={s.key} name={s.label}
                stroke={s.color} strokeWidth={1.5}
                strokeDasharray={s.dash}
                dot={false} isAnimationActive={false}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      ) : (
        <div style={{ color: '#999', textAlign: 'center', padding: '40px 0', fontSize: 14 }}>
          No resource data yet. Data will appear after ~30 seconds of uptime.
        </div>
      )}
    </div>
  );
};

export default ResourceUsageChart;
