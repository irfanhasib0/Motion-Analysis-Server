import React from 'react';
import { Clock, Activity, BarChart3, Volume2, Users, ChevronLeft, ChevronRight, Image } from 'lucide-react';

export const TimeFrameBadge = ({ timeText, frame }) => (
  <span className="reel-time-frame-badge">
    <span>{timeText}</span>
    <span className="reel-time-frame-divider" />
    <span className="reel-frame-inline">
      <Image size={10} />
      <span>{frame}</span>
    </span>
  </span>
);

export const FrameStepButtons = ({ disabled, onStepBack, onStepForward }) => (
  <div className="reel-frame-controls">
    <button
      type="button"
      className="frame-step-btn"
      disabled={disabled}
      onClick={onStepBack}
    >
      <ChevronLeft size={12} />
    </button>
    <button
      type="button"
      className="frame-step-btn"
      disabled={disabled}
      onClick={onStepForward}
    >
      <ChevronRight size={12} />
    </button>
  </div>
);

export const RecordingMetaInfo = ({ durationText, velValue, diffValue, loudnessValue, personValue, MOTION_COLORS = {} }) => (
  <div className="recording-meta">
    <div className="meta-item">
      <Clock size={12} />
      <span>{durationText}</span>
    </div>
    <div className="meta-item">
      <Activity size={12} style={{ color: MOTION_COLORS.velocity || '#009688' }} />
      <span style={{ color: MOTION_COLORS.velocity || '#009688' }}>{velValue ?? 'N/A'}</span>
    </div>
    <div className="meta-item">
      <Users size={12} style={{ color: MOTION_COLORS.person || '#8e24aa' }} />
      <span style={{ color: MOTION_COLORS.person || '#8e24aa' }}>{personValue != null ? personValue : 'N/A'}</span>
    </div>
    <div className="meta-item">
      <Clock size={12} />
      <span>{durationText}</span>
    </div>
    <div className="meta-item">
      <BarChart3 size={12} style={{ color: MOTION_COLORS.bgDiff || '#5c6bc0' }} />
      <span style={{ color: MOTION_COLORS.bgDiff || '#5c6bc0' }}>{diffValue ?? 'N/A'}</span>
    </div>
    <div className="meta-item">
      <Volume2 size={12} style={{ color: MOTION_COLORS.loudness || '#ff9800' }} />
      <span style={{ color: MOTION_COLORS.loudness || '#ff9800' }}>{typeof loudnessValue === 'number' ? loudnessValue.toFixed(2) : 'N/A'}</span>
    </div>
  </div>
);

export const getRecordingMetadata = (recording) => {
  const metadata = recording?.metadata;
  if (!metadata) return {};

  if (typeof metadata === 'string') {
    try {
      return JSON.parse(metadata);
    } catch (_error) {
      return {};
    }
  }

  return typeof metadata === 'object' ? metadata : {};
};

export const formatTimestampParts = (value) => {
  if (!value) {
    return { date: 'N/A', time: '' };
  }

  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return { date: String(value), time: '' };
  }

  return {
    date: parsed.toLocaleDateString(undefined, { day: '2-digit', month: 'short' }),
    time: parsed.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' }),
  };
};

export const formatDuration = (value) => {
  const numericValue = Number(value);
  if (Number.isFinite(numericValue)) {
    const totalSeconds = Math.max(0, Math.round(numericValue));
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = totalSeconds % 60;
    return `${minutes}m ${seconds}s`;
  }
  return 'N/A';
};

export const formatPlaybackTime = (value) => {
  const numericValue = Number(value);
  if (!Number.isFinite(numericValue) || numericValue < 0) {
    return '00:00.00';
  }
  const totalSeconds = Math.floor(numericValue);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  const centiseconds = Math.floor((numericValue - totalSeconds) * 100);
  return `${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}.${centiseconds.toString().padStart(2, '0')}`;
};

export const resolvePlaybackFps = (recording, metadata) => {
  const fpsFromRecording = Number(recording?.fps);
  if (Number.isFinite(fpsFromRecording) && fpsFromRecording > 0) {
    return fpsFromRecording;
  }

  const fpsFromMetadata = Number(metadata?.fps);
  if (Number.isFinite(fpsFromMetadata) && fpsFromMetadata > 0) {
    return fpsFromMetadata;
  }

  return 30;
};

export const getRecordingTimestampValue = (recording, metadata) => (
  metadata?.time_stamp
  || metadata?.timestamp
  || recording?.start_time
  || recording?.started_at
  || recording?.created_at
  || null
);

export const getRecordingPlaybackViewData = (recording, playbackStatsInput) => {
  const metadata = getRecordingMetadata(recording);
  const timestampValue = getRecordingTimestampValue(recording, metadata);
  const timestampParts = formatTimestampParts(timestampValue);
  const durationValue = metadata.duration ?? recording?.duration;
  const velValue = metadata.vel;
  const diffValue = metadata.diff;
  const loudnessValue = metadata.loudness ?? null;
  const personValue = metadata.max_person_count ?? null;

  const playbackStats = playbackStatsInput || { currentTime: 0, duration: 0 };
  const playbackDuration = playbackStats.duration > 0
    ? playbackStats.duration
    : (Number(recording?.duration) || 0);
  const playbackProgress = playbackDuration > 0
    ? Math.min(100, Math.max(0, (playbackStats.currentTime / playbackDuration) * 100))
    : 0;
  const playbackFps = resolvePlaybackFps(recording, metadata);
  const playbackFrame = Math.max(0, Math.floor(playbackStats.currentTime * playbackFps));
  const totalFrames = Math.max(0, Math.floor(playbackDuration * playbackFps));

  return {
    metadata,
    timestampParts,
    durationValue,
    velValue,
    diffValue,
    loudnessValue,
    personValue,
    playbackStats,
    playbackDuration,
    playbackProgress,
    playbackFps,
    playbackFrame,
    totalFrames,
  };
};

export const buildRecordingsByCamera = (completedRecordings = []) => completedRecordings
  .slice()
  .sort((a, b) => new Date(b.start_time) - new Date(a.start_time))
  .reduce((acc, recording) => {
    const key = recording.camera_id || 'unknown_camera';
    if (!acc[key]) {
      acc[key] = [];
    }
    acc[key].push(recording);
    return acc;
  }, {});

export const buildCameraRows = (recordingsByCamera = {}, validCameras = []) => Object.keys(recordingsByCamera)
  .map((cameraId) => {
    const cameraInfo = validCameras.find((cam) => cam.id === cameraId) || { id: cameraId, name: 'Unknown Camera' };
    return {
      cameraId,
      cameraName: cameraInfo.name,
      recordings: recordingsByCamera[cameraId],
    };
  })
  .sort((a, b) => a.cameraName.localeCompare(b.cameraName));

export const buildRowMetricsData = (recordings = [], zoomHours = 24, scrollOffsetHours = 0) => {
  const rowMetrics = recordings.map((recording, index) => {
    const metadata = getRecordingMetadata(recording);
    const timestampValue = getRecordingTimestampValue(recording, metadata);
    const parsedTimestamp = timestampValue ? new Date(timestampValue) : null;
    const hasValidTime = parsedTimestamp && !Number.isNaN(parsedTimestamp.getTime());
    const velocity = Number(metadata.vel);
    const bgDiff = Number(metadata.diff);
    const loudness = Number(metadata.loudness);
    const duration = Number(recording.duration);
    const person = Number(metadata.max_person_count);

    return {
      id: recording.id,
      index,
      timestampMs: hasValidTime ? parsedTimestamp.getTime() : null,
      timeLabel: hasValidTime
        ? parsedTimestamp.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
        : '--:--',
      velocity: Number.isFinite(velocity) ? Math.max(0, velocity) : 0,
      bgDiff: Number.isFinite(bgDiff) ? Math.max(0, bgDiff) : 0,
      loudness: Number.isFinite(loudness) ? Math.max(0, loudness) : 0,
      duration: Number.isFinite(duration) ? Math.max(0, duration) : 0,
      person: Number.isFinite(person) ? Math.max(0, person) : 0,
    };
  });

  const validTimestamps = rowMetrics
    .map((metric) => metric.timestampMs)
    .filter((value) => Number.isFinite(value));

  const latestTimestampMs = validTimestamps.length > 0 ? Math.max(...validTimestamps) : Date.now();
  const earliestTimestampMs = validTimestamps.length > 0 ? Math.min(...validTimestamps) : Date.now();
  const totalDataSpanHours = Math.max(zoomHours, (latestTimestampMs - earliestTimestampMs) / (60 * 60 * 1000));
  const chartWindowMs = zoomHours * 60 * 60 * 1000;
  const chartEndMs = latestTimestampMs - (scrollOffsetHours * 60 * 60 * 1000);
  const chartStartMs = chartEndMs - chartWindowMs;

  const chartMetrics = rowMetrics
    .map((metric) => {
      const ts = Number(metric.timestampMs);
      if (!Number.isFinite(ts) || ts < chartStartMs || ts > chartEndMs) {
        return { ...metric, xPercent: null };
      }

      const normalized = ((chartEndMs - ts) / chartWindowMs) * 100;
      const xPercent = Math.max(0, Math.min(100, normalized));
      return { ...metric, xPercent };
    })
    .filter((metric) => metric.xPercent !== null);

  const chartMaxVelocity = Math.max(1, ...chartMetrics.map((metric) => metric.velocity));
  const chartMaxBgDiff = Math.max(1, ...chartMetrics.map((metric) => metric.bgDiff));
  const chartMaxLoudness = Math.max(1, ...chartMetrics.map((metric) => metric.loudness));
  const chartMaxDuration = Math.max(1, ...chartMetrics.map((metric) => metric.duration));
  const chartMaxPerson = Math.max(1, ...chartMetrics.map((metric) => metric.person));

  const tickInterval = Math.max(1, Math.floor(zoomHours / 8)); // Show ~8 ticks max
  const tickCount = Math.floor(zoomHours / tickInterval) + 1;
  
  const axisTicks = Array.from({ length: tickCount }, (_, tickIndex) => {
    const hourOffset = tickIndex * tickInterval;
    const tickTs = chartEndMs - (hourOffset * 60 * 60 * 1000);
    return {
      xPercent: (hourOffset / zoomHours) * 100,
      label: new Date(tickTs).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' }),
      showLabel: tickIndex % Math.max(1, Math.floor(tickCount / 4)) === 0, // Show ~4 labels max
    };
  });

  return {
    chartMetrics,
    chartMaxVelocity,
    chartMaxBgDiff,
    chartMaxLoudness,
    chartMaxDuration,
    chartMaxPerson,
    axisTicks,
    totalDataSpanHours,
    latestTimestampMs,
    earliestTimestampMs,
  };
};

/**
 * Build metrics data for a single date (00:00–23:59), x-axis = time of day.
 * Returns metrics positioned as percentage of the 24h window (0% = 00:00, 100% = 24:00).
 */
export const buildDateMetricsData = (recordings = [], dateStr) => {
  const dayStart = new Date(`${dateStr}T00:00:00`).getTime();
  const dayEnd = dayStart + 24 * 60 * 60 * 1000;
  const dayMs = 24 * 60 * 60 * 1000;

  const rowMetrics = recordings.map((recording, index) => {
    const metadata = getRecordingMetadata(recording);
    const timestampValue = getRecordingTimestampValue(recording, metadata);
    const parsedTimestamp = timestampValue ? new Date(timestampValue) : null;
    const hasValidTime = parsedTimestamp && !Number.isNaN(parsedTimestamp.getTime());
    const velocity = Number(metadata.vel);
    const bgDiff = Number(metadata.diff);
    const loudness = Number(metadata.loudness);
    const duration = Number(recording.duration);
    const person = Number(metadata.max_person_count);

    return {
      id: recording.id,
      index,
      timestampMs: hasValidTime ? parsedTimestamp.getTime() : null,
      timeLabel: hasValidTime
        ? parsedTimestamp.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
        : '--:--',
      velocity: Number.isFinite(velocity) ? Math.max(0, velocity) : 0,
      bgDiff: Number.isFinite(bgDiff) ? Math.max(0, bgDiff) : 0,
      loudness: Number.isFinite(loudness) ? Math.max(0, loudness) : 0,
      duration: Number.isFinite(duration) ? Math.max(0, duration) : 0,
      person: Number.isFinite(person) ? Math.max(0, person) : 0,
    };
  });

  const chartMetrics = rowMetrics
    .filter(m => m.timestampMs != null && m.timestampMs >= dayStart && m.timestampMs < dayEnd)
    .map(metric => {
      const xPercent = ((dayEnd - metric.timestampMs) / dayMs) * 100;
      return { ...metric, xPercent: Math.max(0, Math.min(100, xPercent)) };
    });

  const chartMaxVelocity = Math.max(1, ...chartMetrics.map(m => m.velocity));
  const chartMaxBgDiff = Math.max(1, ...chartMetrics.map(m => m.bgDiff));
  const chartMaxLoudness = Math.max(1, ...chartMetrics.map(m => m.loudness));
  const chartMaxDuration = Math.max(1, ...chartMetrics.map(m => m.duration));
  const chartMaxPerson = Math.max(1, ...chartMetrics.map(m => m.person));

  // Fixed 24h axis ticks every 3 hours (newest=left, oldest=right)
  const axisTicks = Array.from({ length: 9 }, (_, i) => {
    const hour = i * 3;
    return {
      xPercent: (hour / 24) * 100,
      label: `${String(24 - hour).padStart(2, '0')}:00`,
      showLabel: true,
    };
  });

  // Current time indicator (percentage of day elapsed)
  const now = Date.now();
  const nowPercent = now >= dayStart && now < dayEnd ? ((dayEnd - now) / dayMs) * 100 : null;

  return {
    chartMetrics,
    chartMaxVelocity,
    chartMaxBgDiff,
    chartMaxLoudness,
    chartMaxDuration,
    chartMaxPerson,
    axisTicks,
    nowPercent,
  };
};
