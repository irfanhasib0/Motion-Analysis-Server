import React, { useEffect, useState, useCallback } from 'react';
import {
  LineChart, Line, XAxis, YAxis, Tooltip, Legend, ResponsiveContainer,
  CartesianGrid, ReferenceLine
} from 'recharts';
import api from '../../api';

const CAMERA_COLORS = [
  '#4363d8', '#e6194b', '#3cb44b', '#f58231', '#911eb4',
  '#42d4f4', '#f032e6', '#469990', '#9A6324', '#800000',
];

const LAG_TYPES = [
  { key: 'video', label: 'Video Lag', style: 'solid' },
  { key: 'audio', label: 'Audio Lag', style: '5 5' },
  { key: 'recorder', label: 'Recorder Lag', style: '3 3' },
];

const LAG_CLIP_MAX = 5; // seconds — values above this are clipped for display

const formatTime = (ts) => {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
};

const formatTimeFull = (ts) => {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString();
};

const CustomTooltip = ({ active, payload, label }) => {
  if (!active || !payload || !payload.length) return null;
  return (
    <div style={{ background: '#fff', border: '1px solid #ddd', borderRadius: 6, padding: '8px 12px', fontSize: 12, boxShadow: '0 2px 8px rgba(0,0,0,0.15)' }}>
      <div style={{ color: '#666', marginBottom: 4 }}>{formatTimeFull(label)}</div>
      {payload.map((entry, i) => (
        <div key={i} style={{ color: entry.color, marginBottom: 2 }}>
          {entry.name}: {Number(entry.value).toFixed(1)}s
        </div>
      ))}
    </div>
  );
};

const StreamLagChart = ({ cameras }) => {
  const [lagData, setLagData] = useState({});
  const [selectedCamera, setSelectedCamera] = useState('all');
  const [visibleTypes, setVisibleTypes] = useState({ video: true, audio: true, recorder: true });

  const fetchLagHistory = useCallback(async () => {
    try {
      const res = await api.getLagHistory();
      setLagData(res.data || {});
    } catch (err) {
      // Silently handle — chart just stays empty
    }
  }, []);

  useEffect(() => {
    fetchLagHistory();
    const interval = setInterval(fetchLagHistory, 60000); // Refresh every 60s
    return () => clearInterval(interval);
  }, [fetchLagHistory]);

  const toggleType = (key) => {
    setVisibleTypes(prev => ({ ...prev, [key]: !prev[key] }));
  };

  // Build unified chart data with timestamps as x-axis
  const cameraIds = Object.keys(lagData);
  const activeCameras = selectedCamera === 'all' ? cameraIds : [selectedCamera];

  // Merge all samples by timestamp for unified x-axis
  const chartData = [];
  if (activeCameras.length === 1) {
    // Single camera: show raw samples (clipped)
    const samples = lagData[activeCameras[0]] || [];
    for (const s of samples) {
      chartData.push({
        ts: s.ts,
        video: Math.min(s.video || 0, LAG_CLIP_MAX),
        audio: Math.min(s.audio || 0, LAG_CLIP_MAX),
        recorder: Math.min(s.recorder || 0, LAG_CLIP_MAX),
      });
    }
  } else {
    // Multiple cameras: interleave by timestamp, keyed by camera
    const allSamples = [];
    for (const cid of activeCameras) {
      for (const s of (lagData[cid] || [])) {
        allSamples.push({ ...s, _cid: cid });
      }
    }
    allSamples.sort((a, b) => a.ts - b.ts);

    // Group by approximate timestamp (within 5s window)
    let currentBucket = null;
    for (const s of allSamples) {
      if (!currentBucket || s.ts - currentBucket.ts > 5) {
        currentBucket = { ts: s.ts };
        chartData.push(currentBucket);
      }
      for (const type of LAG_TYPES) {
        currentBucket[`${s._cid}_${type.key}`] = Math.min(s[type.key] || 0, LAG_CLIP_MAX);
      }
    }
  }

  // Compute true max lag (unclipped) across active cameras + visible types
  let maxLagRaw = 0;
  for (const cid of activeCameras) {
    for (const s of (lagData[cid] || [])) {
      for (const t of LAG_TYPES) {
        if (visibleTypes[t.key]) maxLagRaw = Math.max(maxLagRaw, s[t.key] || 0);
      }
    }
  }
  const wasClipped = maxLagRaw > LAG_CLIP_MAX;

  const hasData = chartData.length > 0;

  // Get camera name by id
  const getCameraName = (cid) => {
    const cam = cameras?.find(c => c.id === cid);
    return cam?.name || cid;
  };

  return (
    <div>
      {/* Controls */}
      <div style={{ display: 'flex', gap: 12, marginBottom: 12, flexWrap: 'wrap', alignItems: 'center' }}>
        <select
          value={selectedCamera}
          onChange={(e) => setSelectedCamera(e.target.value)}
          style={{
            background: '#fff', color: '#333', border: '1px solid #ccc',
            borderRadius: 4, padding: '4px 8px', fontSize: 13
          }}
        >
          <option value="all">All Cameras</option>
          {cameraIds.map(cid => (
            <option key={cid} value={cid}>{getCameraName(cid)}</option>
          ))}
        </select>
        {LAG_TYPES.map(t => (
          <label key={t.key} style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 13, color: '#546e7a', cursor: 'pointer' }}>
            <input
              type="checkbox" checked={visibleTypes[t.key]}
              onChange={() => toggleType(t.key)}
            />
            {t.label}
          </label>
        ))}
        {wasClipped && (
          <span style={{ marginLeft: 'auto', fontSize: 11, color: '#e65100', background: 'rgba(255,152,0,0.1)', border: '1px solid rgba(230,81,0,0.3)', borderRadius: 5, padding: '3px 8px', whiteSpace: 'nowrap' }}>
            max {maxLagRaw.toFixed(1)}s — clipping &gt; {LAG_CLIP_MAX}s
          </span>
        )}
      </div>

      {/* Chart */}
      {hasData ? (
        <ResponsiveContainer width="100%" height={196}>
          <LineChart data={chartData} margin={{ top: 5, right: 20, left: 10, bottom: 5 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#e0e0e0" />
            <XAxis
              dataKey="ts" tickFormatter={formatTime} stroke="#999"
              tick={{ fontSize: 11, fill: '#666' }} interval="preserveStartEnd"
            />
            <YAxis
              stroke="#999" tick={{ fontSize: 11, fill: '#666' }}
              domain={[0, LAG_CLIP_MAX]}
              label={{ value: 'Lag (s)', angle: -90, position: 'insideLeft', style: { fill: '#666', fontSize: 12 } }}
            />
            <Tooltip content={<CustomTooltip />} />
            <Legend wrapperStyle={{ fontSize: 12 }} formatter={(value) => <span style={{ color: '#546e7a' }}>{value}</span>} />

            {activeCameras.length === 1 ? (
              // Single camera: one line per lag type
              LAG_TYPES.filter(t => visibleTypes[t.key]).map((t, i) => (
                <Line
                  key={t.key} type="monotone" dataKey={t.key} name={t.label}
                  stroke={CAMERA_COLORS[i]} strokeWidth={1.5} strokeDasharray={t.style === 'solid' ? undefined : t.style}
                  dot={false} isAnimationActive={false}
                />
              ))
            ) : (
              // Multiple cameras: one line per camera+type combo
              activeCameras.flatMap((cid, ci) =>
                LAG_TYPES.filter(t => visibleTypes[t.key]).map(t => (
                  <Line
                    key={`${cid}_${t.key}`} type="monotone" dataKey={`${cid}_${t.key}`}
                    name={`${getCameraName(cid)} ${t.label}`}
                    stroke={CAMERA_COLORS[ci % CAMERA_COLORS.length]} strokeWidth={1.5}
                    strokeDasharray={t.style === 'solid' ? undefined : t.style}
                    dot={false} isAnimationActive={false}
                  />
                ))
              )
            )}
          </LineChart>
        </ResponsiveContainer>
      ) : (
        <div style={{ color: '#999', textAlign: 'center', padding: '40px 0', fontSize: 14 }}>
          No lag data available yet. Data will appear as streams are monitored.
        </div>
      )}
    </div>
  );
};

export default StreamLagChart;
