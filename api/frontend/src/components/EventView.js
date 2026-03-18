import React, { useEffect, useState, useRef, useMemo, useCallback } from 'react';
import metricConfig from './metric_config.json';
import { Camera, Play, ChevronLeft, ChevronRight, Pause, Maximize2, X, Download, Trash2, Loader } from 'lucide-react';
import { toast } from 'react-hot-toast';
import { api } from '../api';
import {
  TimeFrameBadge,
  FrameStepButtons,
  RecordingMetaInfo,
  getRecordingMetadata,
  formatDuration,
  formatPlaybackTime,
  resolvePlaybackFps,
  getRecordingPlaybackViewData,
  buildRecordingsByCamera,
  buildCameraRows,
  buildRowMetricsData,
  buildDateMetricsData,
} from './EventViewUtils';
import './EventView.css';

// Module-level cache: persists for the lifetime of the browser session
const MOTION_CACHE_MAX = 100;
const motionDataCache = {};
const motionCacheKeys = [];

function cacheMotionData(recordingId, data) {
  if (motionCacheKeys.length >= MOTION_CACHE_MAX && !motionDataCache[recordingId]) {
    const oldest = motionCacheKeys.shift();
    delete motionDataCache[oldest];
  }
  motionDataCache[recordingId] = data;
  if (!motionCacheKeys.includes(recordingId)) {
    motionCacheKeys.push(recordingId);
  }
}

/**
 * Simple motion plot component for individual recordings
 * Displays vel, bg_diff, loudness as line chart below progress bar
 */
const SimpleMotionPlot = ({ recording, MOTION_COLORS }) => {
  const [motionData, setMotionData] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!recording?.id) return;

    // Use cached data immediately without re-fetching
    if (motionDataCache[recording.id]) {
      setMotionData(motionDataCache[recording.id]);
      setLoading(false);
      return;
    }

    const loadMotionData = async () => {
      try {
        setLoading(true);
        const response = await api.getMotionData(recording.id);
        const data = response.data?.data || [];
        cacheMotionData(recording.id, data);
        setMotionData(data);
      } catch (err) {
        console.error('Failed to load motion data:', err);
        setMotionData([]);
      } finally {
        setLoading(false);
      }
    };

    loadMotionData();
  }, [recording?.id]);

  // Calculate max values for scaling
  const maxValues = useMemo(() => {
    if (motionData.length === 0) return { vel: 1, bg_diff: 1, loudness: 1 };
    
    return {
      vel: Math.max(0.1, ...motionData.map(d => d.vel)),
      bg_diff: Math.max(1, ...motionData.map(d => d.bg_diff)), 
      loudness: Math.max(0.1, ...motionData.map(d => d.loudness))
    };
  }, [motionData]);

  if (loading || motionData.length === 0) {
    return null;
  }

  // Generate SVG path data
  const generatePath = (data, key, maxVal) => {
    if (data.length === 0) return '';
    
    const points = data.map((point, index) => {
      const x = (index / (data.length - 1)) * 100;
      const y = 30 - ((point[key] / maxVal) * 25); // Flip Y and scale to 25px height
      return `${x},${y}`;
    });
    
    return `M${points.join(' L')}`;
  };

  const velPath = generatePath(motionData, 'vel', maxValues.vel);
  const bgDiffPath = generatePath(motionData, 'bg_diff', maxValues.bg_diff);
  const loudnessPath = generatePath(motionData, 'loudness', maxValues.loudness);

  return (
    <div style={{ 
      width: '100%', 
      height: '30px', 
      marginTop: '4px', 
      position: 'relative',
      background: 'rgba(255,255,255,0.1)',
      borderRadius: '2px',
      border: '1px solid rgba(255,255,255,0.1)'
    }}>
      <svg 
        width="100%" 
        height="30" 
        viewBox="0 0 100 30" 
        preserveAspectRatio="none"
        style={{ display: 'block' }}
      >
        <path 
          d={velPath} 
          fill="none" 
          stroke={MOTION_COLORS?.velocity || '#009688'} 
          strokeWidth="0.6" 
          opacity="0.5"
        />
        <path 
          d={bgDiffPath} 
          fill="none" 
          stroke={MOTION_COLORS?.bgDiff || '#5c6bc0'} 
          strokeWidth="0.6" 
          opacity="0.5"
        />
        <path 
          d={loudnessPath} 
          fill="none" 
          stroke={MOTION_COLORS?.loudness || '#ff9800'} 
          strokeWidth="0.6" 
          opacity="0.5"
        />
      </svg>
    </div>
  );
};

/**
 * Chart visualization component for timeline metrics
 * Displays velocity and bg_diff data with interactive controls
 */
const ChartCard = ({ 
  row, 
  chartDate,
  setChartDate,
  handleTimelinePointClick,
  highlightedRecordingId,
  chartToggles,
  setChartToggles,
  MOTION_COLORS
 }) => {
  const {
    chartMetrics,
    chartMaxVelocity,
    chartMaxBgDiff,
    chartMaxLoudness,
    chartMaxDuration,
    axisTicks,
    nowPercent,
  } = buildDateMetricsData(row.recordings, chartDate);

  const shiftDate = (delta) => {
    const [y, m, d] = chartDate.split('-').map(Number);
    const date = new Date(y, m - 1, d + delta);
    const yyyy = date.getFullYear();
    const mm = String(date.getMonth() + 1).padStart(2, '0');
    const dd = String(date.getDate()).padStart(2, '0');
    setChartDate(`${yyyy}-${mm}-${dd}`);
  };

  return (
    <div className="camera-row-metrics-card">
      {/* Chart header with date nav and legend */}
      <div className="camera-row-metrics-header">
        {/* Date navigation */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <button
            className="chart-nav-button prev"
            onClick={() => shiftDate(1)}
            title="Next day"
            disabled={chartDate >= new Date().toISOString().slice(0, 10)}
          >
            <ChevronLeft size={16} />
          </button>
          <span style={{ fontWeight: 600, fontSize: 13, minWidth: 90, textAlign: 'center' }}>{chartDate}</span>
          <button className="chart-nav-button next" onClick={() => shiftDate(-1)} title="Previous day">
            <ChevronRight size={16} />
          </button>
        </div>

        {/* Legend toggles */}
        <div className="metrics-chart-controls">
          <div className="metrics-legend">
            <label style={{ display: 'flex', alignItems: 'center', gap: '4px', fontSize: '10px', cursor: 'pointer' }}>
              <input type="checkbox" checked={chartToggles.velocity} onChange={(e) => setChartToggles(prev => ({ ...prev, velocity: e.target.checked }))} style={{ accentColor: MOTION_COLORS.velocity, transform: 'scale(0.8)' }} />
              <span className="legend-dot" style={{ backgroundColor: MOTION_COLORS.velocity }} />
              {metricConfig.labels.vel}
            </label>
            <label style={{ display: 'flex', alignItems: 'center', gap: '4px', fontSize: '10px', cursor: 'pointer' }}>
              <input type="checkbox" checked={chartToggles.bgDiff} onChange={(e) => setChartToggles(prev => ({ ...prev, bgDiff: e.target.checked }))} style={{ accentColor: MOTION_COLORS.bgDiff, transform: 'scale(0.8)' }} />
              <span className="legend-dot" style={{ backgroundColor: MOTION_COLORS.bgDiff }} />
              {metricConfig.labels.diff}
            </label>
            <label style={{ display: 'flex', alignItems: 'center', gap: '4px', fontSize: '10px', cursor: 'pointer' }}>
              <input type="checkbox" checked={chartToggles.loudness} onChange={(e) => setChartToggles(prev => ({ ...prev, loudness: e.target.checked }))} style={{ accentColor: MOTION_COLORS.loudness, transform: 'scale(0.8)' }} />
              <span className="legend-dot" style={{ backgroundColor: MOTION_COLORS.loudness }} />
              {metricConfig.labels.loudness}
            </label>
            <label style={{ display: 'flex', alignItems: 'center', gap: '4px', fontSize: '10px', cursor: 'pointer' }}>
              <input type="checkbox" checked={chartToggles.duration} onChange={(e) => setChartToggles(prev => ({ ...prev, duration: e.target.checked }))} style={{ accentColor: MOTION_COLORS.duration, transform: 'scale(0.8)' }} />
              <span className="legend-dot" style={{ backgroundColor: MOTION_COLORS.duration }} />
              {metricConfig.labels.duration}
            </label>
          </div>
        </div>
      </div>

      {/* Bar chart area */}
      <div 
        className="metrics-bar-chart" 
        role="img" 
        aria-label="Motion metrics by time of day"
        style={{ cursor: 'default', position: 'relative' }}
      >
        {/* Current time vertical red line */}
        {nowPercent != null && (
          <div style={{
            position: 'absolute', left: `${nowPercent}%`, top: 0, bottom: 0,
            width: 1, background: '#f44336', zIndex: 3, pointerEvents: 'none',
          }}>
            <div style={{ position: 'absolute', top: -2, left: -3, width: 7, height: 7, borderRadius: '50%', background: '#f44336' }} />
          </div>
        )}

        {chartMetrics.map((metric) => {
          const velocityHeight = chartToggles.velocity ? Math.max(2, (metric.velocity / chartMaxVelocity) * 100) : 0;
          const bgDiffHeight = chartToggles.bgDiff ? Math.max(2, (metric.bgDiff / chartMaxBgDiff) * 100) : 0;
          const loudnessHeight = chartToggles.loudness ? Math.max(2, (metric.loudness / chartMaxLoudness) * 100) : 0;
          const durationHeight = chartToggles.duration ? Math.max(2, (metric.duration / chartMaxDuration) * 100) : 0;
          const markerHeight = Math.max(velocityHeight, bgDiffHeight, loudnessHeight, durationHeight);
          return (
            <React.Fragment key={metric.id}>
              <div className="metrics-bar-group" style={{ left: `${metric.xPercent}%` }}>
                {chartToggles.velocity && (
                  <span className="metrics-bar velocity" style={{ height: `${velocityHeight}%`, backgroundColor: MOTION_COLORS.velocity }} title={`Velocity: ${metric.velocity.toFixed(3)}`} />
                )}
                {chartToggles.bgDiff && (
                  <span className="metrics-bar bgdiff" style={{ height: `${bgDiffHeight}%`, backgroundColor: MOTION_COLORS.bgDiff }} title={`bg_diff: ${metric.bgDiff.toFixed(0)}`} />
                )}
                {chartToggles.loudness && (
                  <span className="metrics-bar loudness" style={{ height: `${loudnessHeight}%`, backgroundColor: MOTION_COLORS.loudness }} title={`loudness: ${metric.loudness.toFixed(2)}`} />
                )}
                {chartToggles.duration && (
                  <span className="metrics-bar duration" style={{ height: `${durationHeight}%`, backgroundColor: MOTION_COLORS.duration }} title={`duration: ${metric.duration.toFixed(1)}s`} />
                )}
              </div>
              <button
                type="button"
                className={`motion-point-btn${highlightedRecordingId === metric.id ? ' active' : ''}`}
                style={{ left: `${metric.xPercent}%`, bottom: `${Math.min(96, markerHeight + 2)}%` }}
                onClick={() => handleTimelinePointClick(row.cameraId, metric.id)}
                title={`${metric.timeLabel} | vel: ${metric.velocity.toFixed(3)} | diff: ${metric.bgDiff.toFixed(0)} | loud: ${metric.loudness.toFixed(2)} | dur: ${metric.duration.toFixed(1)}s`}
              >
                <span className="point-dot" />
              </button>
            </React.Fragment>
          );
        })}
      </div>
      
      {/* Time axis: 00:00 to 24:00 */}
      <div className="metrics-time-axis">
        {axisTicks.map((tick, index) => (
          <div key={`tick-${index}`} className="metrics-axis-tick" style={{ left: `${tick.xPercent}%` }}>
            <span className="metrics-axis-mark" />
            {tick.showLabel && <span className={index === 0 ? 'metrics-axis-label-first' : 'metrics-axis-label'}>{tick.label}</span>}
          </div>
        ))}
      </div>
    </div>
  );
};

/**
 * Overlay toggle button with progress bar during generation.
 * Triggers background generation, polls status, loads overlay video when ready.
 */
const OverlayButton = ({ recording, overlayIds, setOverlayIds, videoRefs }) => {
  const [progress, setProgress] = useState(null); // null = idle, 0-100 = generating
  const pollRef = useRef(null);

  // Clean up polling on unmount
  useEffect(() => () => { if (pollRef.current) clearInterval(pollRef.current); }, []);

  const isActive = !!overlayIds[recording.id];

  const handleClick = useCallback(async () => {
    // Toggle off
    if (isActive) {
      setOverlayIds((prev) => ({ ...prev, [recording.id]: false }));
      setProgress(null);
      if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
      setTimeout(() => { const v = videoRefs.current[recording.id]; if (v?.load) v.load(); }, 0);
      return;
    }

    // Check if overlay is already cached (instant)
    try {
      const { data: status } = await api.getOverlayStatus(recording.id);
      if (status.status === 'ready') {
        setOverlayIds((prev) => ({ ...prev, [recording.id]: true }));
        setTimeout(() => { const v = videoRefs.current[recording.id]; if (v?.load) v.load(); }, 0);
        return;
      }
    } catch (_) { /* proceed to generate */ }

    // Start generation
    setProgress(0);
    try { await api.generateOverlay(recording.id); } catch (_) {}

    // Poll progress
    pollRef.current = setInterval(async () => {
      try {
        const { data: st } = await api.getOverlayStatus(recording.id);
        setProgress(st.progress ?? 0);
        if (st.status === 'ready') {
          clearInterval(pollRef.current);
          pollRef.current = null;
          setProgress(null);
          setOverlayIds((prev) => ({ ...prev, [recording.id]: true }));
          setTimeout(() => { const v = videoRefs.current[recording.id]; if (v?.load) v.load(); }, 0);
        } else if (st.status === 'error') {
          clearInterval(pollRef.current);
          pollRef.current = null;
          setProgress(null);
          toast.error('Overlay generation failed');
        }
      } catch (_) {}
    }, 800);
  }, [isActive, recording.id, setOverlayIds, videoRefs]);

  const isGenerating = progress !== null;

  return (
    <button
      type="button"
      className={`reel-action-btn${isActive ? ' active' : ''}`}
      title={isGenerating ? `Generating overlay… ${progress}%` : isActive ? 'Disable overlay' : 'Show optical flow overlay'}
      onClick={handleClick}
      disabled={isGenerating}
      style={{
        position: 'relative',
        overflow: 'hidden',
        ...(isActive ? { background: '#1976d2', color: '#fff' } : {}),
      }}
    >
      {isGenerating && (
        <span style={{
          position: 'absolute', left: 0, bottom: 0, height: 3,
          width: `${progress}%`, background: '#1976d2',
          transition: 'width 0.3s ease',
        }} />
      )}
      {isGenerating ? <Loader size={12} className="spin-icon" /> : <Camera size={12} />}
    </button>
  );
};

/**
 * Individual recording card component
 * Handles video playback, controls, and metadata display
 */
const ReelCard = ({
  recording,
  recordingIndex,
  row, 
  playingId, 
  hoveredId, 
  expandedContext,
  playbackStatsById,
  playbackMode,
  highlightedRecordingId,
  labelMap,
  notePanelId,
  noteDraft,
  overlayIds,
  setOverlayIds,
  ALERT_LABELS,
  MOTION_COLORS,
  videoRefs,
  reelCardRefs,
  handleMouseEnter,
  handleMouseLeave,
  handleClick,
  handleOpenExpanded,
  handleVideoTimeUpdate,
  handleVideoLoadedMetadata,
  handleSeekStart,
  handleSeekEnd,
  handleSeekChange,
  stepFrame,
  handleDownloadRecording,
  handleDeleteRecording,
  handleSetLabel,
  setNotePanelId,
  setNoteDraft,
  handleSaveNote
}) => {
  // Extract playback data for this recording
  const isPlaying = playingId === recording.id;
  const isHovered = hoveredId === recording.id;
  const shouldLoadVideo = !expandedContext && (isPlaying || isHovered);
  const {
    timestampParts,
    durationValue,
    velValue,
    diffValue,
    loudnessValue,
    playbackStats,
    playbackDuration,
    playbackProgress,
    playbackFrame,
    totalFrames,
  } = getRecordingPlaybackViewData(recording, playbackStatsById[recording.id]);

  return (
    <div
      key={recording.id}
      ref={(el) => {
        reelCardRefs.current[recording.id] = el;
      }}
      className={`reel-card${highlightedRecordingId === recording.id ? ' reel-card-highlighted' : ''}`}
      onMouseEnter={() => handleMouseEnter(recording.id)}
      onMouseLeave={() => handleMouseLeave(recording.id)}
      onClick={() => handleClick(recording.id)}
    >
      {/* Recording timestamp display */}
      <div className="reel-timestamp">
        <span className="reel-date">{timestampParts.date}</span>
        {timestampParts.time && <span className="reel-time">{timestampParts.time}</span>}
      </div>

      {/* Video thumbnail/player with controls */}
      <div className="reel-thumbnail">
        <button
          type="button"
          className="enlarge-btn"
          title="Enlarge playback"
          onClick={(event) => {
            event.stopPropagation();
            handleOpenExpanded(recording, row.cameraId, recordingIndex);
          }}
        >
          <Maximize2 size={14} />
        </button>

        {/* Video/Stream player */}
        {playbackMode === 'stream' ? (
          <img
            ref={(el) => (videoRefs.current[recording.id] = el)}
            className="reel-video"
            src={!expandedContext ? api.appendQueryParams(api.getRecordingStreamUrl(recording.id, 'stream'), {
              ts: Date.now(),
            }) : undefined}
            alt={`Recording ${recording.id}`}
            onLoad={() => console.log('Stream loaded:', recording.id)}
            onError={(e) => console.error('Stream error:', recording.id, e)}
          />
        ) : (
          <video
            ref={(el) => (videoRefs.current[recording.id] = el)}
            className="reel-video"
            src={shouldLoadVideo ? api.getRecordingStreamUrl(recording.id, 'play', !!overlayIds[recording.id]) : undefined}
            poster={api.getRecordingThumbnailUrl(recording.id)}
            muted
            loop
            playsInline
            preload="none"
            autoPlay={shouldLoadVideo}
            onLoadedMetadata={(event) => handleVideoLoadedMetadata(recording.id, event)}
            onTimeUpdate={(event) => handleVideoTimeUpdate(recording.id, event)}
            onLoadedData={() => console.log('Video loaded:', recording.id)}
            onError={(e) => console.error('Video error:', recording.id, e)}
          />
        )}

        {/* Play/Pause overlay indicators */}
        {!isPlaying && (
          <div className="play-overlay">
            <Play size={48} />
          </div>
        )}

        {isPlaying && (
          <div className="pause-indicator">
            <Pause size={24} />
          </div>
        )}
      </div>

      {/* Recording info panel with controls and metadata */}
      <div className="reel-info">
        {/* Playback controls */}
        <div className="reel-playback-controls" onClick={(event) => event.stopPropagation()}>
          <div className="reel-progress-header">
            <div className="reel-progress-left">
              <TimeFrameBadge timeText={formatPlaybackTime(playbackStats.currentTime)} frame={playbackFrame} />
            </div>
            <FrameStepButtons
              disabled={playbackMode !== 'play'}
              onStepBack={(event) => {
                event.stopPropagation();
                stepFrame(recording, -1);
              }}
              onStepForward={(event) => {
                event.stopPropagation();
                stepFrame(recording, 1);
              }}
            />
            <div className="reel-progress-right">
              <TimeFrameBadge timeText={formatPlaybackTime(playbackDuration)} frame={totalFrames} />
            </div>
          </div>
          <input
            type="range"
            min={0}
            max={Math.max(playbackDuration, 0.01)}
            step={0.01}
            value={Math.min(playbackStats.currentTime, Math.max(playbackDuration, 0.01))}
            disabled={playbackMode !== 'play'}
            onMouseDown={() => {
              handleMouseEnter(recording.id);
              handleSeekStart(recording.id, false);
            }}
            onMouseUp={() => handleSeekEnd(recording.id)}
            onTouchStart={() => {
              handleMouseEnter(recording.id);
              handleSeekStart(recording.id, false);
            }}
            onTouchEnd={() => handleSeekEnd(recording.id)}
            onChange={(event) => handleSeekChange(recording.id, event.target.value)}
            className="reel-progress-slider"
            style={{ '--progress': `${playbackProgress}%` }}
          />
          <SimpleMotionPlot recording={recording} MOTION_COLORS={MOTION_COLORS} />
        </div>
        
        {/* Recording metadata and actions */}
        <div className="reel-meta-row">
          <RecordingMetaInfo 
            durationText={formatDuration(durationValue)}
            velValue={velValue} 
            diffValue={diffValue} 
            loudnessValue={loudnessValue}
            MOTION_COLORS={MOTION_COLORS}
          />
          <div className="reel-card-actions" onClick={(event) => event.stopPropagation()}>
            <OverlayButton
              recording={recording}
              overlayIds={overlayIds}
              setOverlayIds={setOverlayIds}
              videoRefs={videoRefs}
            />
            <button
              type="button"
              className="reel-action-btn"
              title="Download"
              onClick={() => handleDownloadRecording(recording.id)}
            >
              <Download size={12} />
            </button>
            <button
              type="button"
              className="reel-action-btn danger"
              title="Delete"
              onClick={() => handleDeleteRecording(recording.id)}
            >
              <Trash2 size={12} />
            </button>
          </div>
        </div>

        {/* Labels and notes management */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 4, marginTop: 5, marginBottom: 5, flexWrap: 'wrap' }} onClick={(e) => e.stopPropagation()}>
          {ALERT_LABELS.map(({ id, short, color }) => {
            const isActive = (labelMap[recording.id]?.label) === id;
            return (
              <button
                key={id}
                type="button"
                title={id + ' alert'}
                onClick={() => handleSetLabel(recording.id, id)}
                style={{ padding: '0px 6px', fontSize: 10, borderRadius: 10, border: `1.5px solid ${color}`, background: isActive ? color : 'transparent', color: isActive ? '#fff' : color, cursor: 'pointer', fontWeight: 700, lineHeight: 1 }}
              >{short}</button>
            );
          })}
          <button
            type="button"
            title={notePanelId === recording.id ? 'Close note' : 'Add / edit note'}
            onClick={() => { setNotePanelId((v) => v === recording.id ? null : recording.id); setNoteDraft((d) => ({ ...d, [recording.id]: labelMap[recording.id]?.note || '' })); }}
            style={{ padding: '0px 6px', fontSize: 10, borderRadius: 10, border: '1.5px solid #78909c', background: 'transparent', color: '#78909c', cursor: 'pointer', fontWeight: 700, lineHeight: 1 }}
          >✏</button>
          {labelMap[recording.id]?.note && notePanelId !== recording.id && (
            <span style={{ fontSize: 10, color: '#546e7a', maxWidth: 110, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={labelMap[recording.id].note}>
              {labelMap[recording.id].note}
            </span>
          )}
        </div>
        
        {/* Note editing panel */}
        {notePanelId === recording.id && (
          <div style={{ marginTop: 4 }} onClick={(e) => e.stopPropagation()}>
            <textarea
              value={noteDraft[recording.id] ?? ''}
              onChange={(e) => setNoteDraft((d) => ({ ...d, [recording.id]: e.target.value }))}
              rows={2}
              style={{ width: '100%', fontSize: 11, borderRadius: 6, border: '1px solid #b0bec5', padding: '4px 6px', resize: 'none', boxSizing: 'border-box' }}
              placeholder="Add a note..."
            />
            <button type="button" className="btn btn-primary" style={{ padding: '3px 10px', fontSize: 11, marginTop: 2 }} onClick={() => handleSaveNote(recording.id)}>Save</button>
          </div>
        )}
      </div>
    </div>
  );
};

/**
 * Main EventView component for displaying camera recordings with interactive timeline
 * Provides video playback, timeline navigation, and recording management
 */
const EventView = ({ recordings = [], cameras = [] }) => {
  // ===== Configuration constants =====
  // Motion data visualization colors
  const MOTION_COLORS = {
    velocity: '#009688',
    bgDiff: '#5c6bc0', 
    loudness: '#ff9800',
    duration: '#e91e63'
  };
  
  // Sensitivity settings for mouse and touch interactions
  const MOUSE_DRAG_SENSITIVITY = 2.2;
  const TOUCH_DRAG_SENSITIVITY = 3.0;
  
  // Alert label configuration for recording categorization
  const ALERT_LABELS = [
    { id: 'high',   short: 'H', label: 'High alert',   color: '#c62828' },
    { id: 'medium', short: 'M', label: 'Medium alert', color: '#e65100' },
    { id: 'low',    short: 'L', label: 'Low alert',    color: '#2e7d32' },
  ];
  const ALERT_COLOR = { high: '#c62828', medium: '#e65100', low: '#2e7d32' };

  // ===== Data processing and state management =====
  // Process and filter recording data
  const validRecordings = Array.isArray(recordings) ? recordings : [];
  const [removedRecordingIds, setRemovedRecordingIds] = useState({});
  const [labelMap, setLabelMap] = useState({});
  const [showLabeledOnly, setShowLabeledOnly] = useState(false);
  
  // Filter recordings based on completion status and user preferences
  const completedRecordings = validRecordings.filter(
    (recording) => {
      if ((recording?.status || '').toLowerCase() !== 'completed') return false;
      if (removedRecordingIds[recording.id]) return false;
      if (showLabeledOnly) {
        const lbl = labelMap[recording.id]?.label || (recording.metadata?.label);
        if (!lbl) return false;
      }
      return true;
    }
  );
  const validCameras = Array.isArray(cameras) ? cameras : [];

  // Sync labelMap when recordings change to maintain label state
  useEffect(() => {
    setLabelMap((prev) => {
      const next = { ...prev };
      validRecordings.forEach((rec) => {
        if (!next[rec.id] && (rec.metadata?.label || rec.metadata?.note)) {
          next[rec.id] = { label: rec.metadata.label || null, note: rec.metadata.note || '' };
        }
      });
      return next;
    });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [recordings]);

  // ===== UI State Management =====
  const [playingId, setPlayingId] = useState(null);
  const [hoveredId, setHoveredId] = useState(null);
  const [highlightedRecordingId, setHighlightedRecordingId] = useState(null);
  const [playbackMode, setPlaybackMode] = useState(api.getRecordingPlaybackMode());
  const [playbackStatsById, setPlaybackStatsById] = useState({});
  const [expandedContext, setExpandedContext] = useState(null);
  const [overlayIds, setOverlayIds] = useState({});  // {recordingId: true} for overlay-enabled recordings

  const [notePanelId, setNotePanelId] = useState(null);
  const [noteDraft, setNoteDraft] = useState({});

  // ===== Chart zoom/scroll state =====
  const [chartZoomHours, setChartZoomHours] = useState(12); // Default 12-hour view
  const [chartScrollOffsetHours, setChartScrollOffsetHours] = useState(0); // Hours from latest timestamp
  const [chartToggles, setChartToggles] = useState({ velocity: true, bgDiff: false, loudness: false, duration: true }); // Chart visibility toggles
  const [chartDate, setChartDate] = useState(() => {
    // If navigated from Dashboard with a specific date, use it
    const navDate = window.history.state?.usr?.date;
    return navDate || new Date().toISOString().slice(0, 10);
  });

  // Auto-highlight recording when navigated from Dashboard
  useEffect(() => {
    const navState = window.history.state?.usr;
    if (navState?.recordingId && navState?.cameraId) {
      // Small delay to let the reel cards render
      const timer = setTimeout(() => {
        handleTimelinePointClick(navState.cameraId, navState.recordingId);
      }, 300);
      return () => clearTimeout(timer);
    }
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // ===== Event handlers for mouse and keyboard interactions =====
  // Chart navigation and timeline scrub control
  const handleChartWheel = (event) => {
    event.preventDefault();
    
    if (event.ctrlKey || event.metaKey) {
      // Zoom with Ctrl/Cmd + wheel
      const zoomLevels = [1, 3, 6, 12, 24, 48, 72];
      const currentIndex = zoomLevels.indexOf(chartZoomHours);
      
      if (event.deltaY < 0) {
        // Zoom in (smaller time window)
        if (currentIndex > 0) {
          setChartZoomHours(zoomLevels[currentIndex - 1]);
        }
      } else {
        // Zoom out (larger time window) 
        if (currentIndex < zoomLevels.length - 1) {
          setChartZoomHours(zoomLevels[currentIndex + 1]);
        }
      }
    } else {
      // Pan/scroll with wheel only - use consistent small steps
      const scrollStep = Math.min(1, chartZoomHours * 0.02); // 2% of zoom window, max 1 hour
      
      setChartScrollOffsetHours(prev => {
        if (event.deltaY > 0) {
          // Scroll to older data (positive deltaY = wheel down)
          return prev + scrollStep;
        } else {
          // Scroll to more recent data (negative deltaY = wheel up)
          return Math.max(0, prev - scrollStep);
        }
      });
    }
  };

  // ===== Mutable refs for media, drag, and seeking =====
  const videoRefs = useRef({});
  const expandedVideoRefs = useRef({});
  const reelCardRefs = useRef({});
  const rowScrollRefs = useRef({});
  const seekStateRef = useRef({});
  const rowDragStateRef = useRef({
    isDragging: false,
    cameraId: null,
    pointerId: null,
    startX: 0,
    startScrollLeft: 0,
    moved: false,
  });
  const touchDragStateRef = useRef({
    active: false,
    cameraId: null,
    startX: 0,
    startY: 0,
    startScrollLeft: 0,
    horizontalLocked: false,
    moved: false,
    lastDx: 0,
  });
  const suppressClickUntilRef = useRef(0);

  // ===== Utility: snap row scroll to closest card =====
  const snapRowToNearestCard = (cameraId) => {
    const rowElement = rowScrollRefs.current[cameraId];
    if (!rowElement) {
      return;
    }

    const cards = Array.from(rowElement.querySelectorAll('.reel-card'));
    if (cards.length === 0) {
      return;
    }

    const targetScrollLeft = rowElement.scrollLeft;
    let nearestCard = cards[0];
    let nearestDistance = Math.abs(cards[0].offsetLeft - targetScrollLeft);

    for (let idx = 1; idx < cards.length; idx += 1) {
      const distance = Math.abs(cards[idx].offsetLeft - targetScrollLeft);
      if (distance < nearestDistance) {
        nearestDistance = distance;
        nearestCard = cards[idx];
      }
    }

    rowElement.scrollTo({
      left: nearestCard.offsetLeft,
      behavior: 'smooth',
    });
  };

  // ===== Utility: stop/unload inline media when expanded modal is active =====
  const stopInlineMediaPlayback = () => {
    Object.values(videoRefs.current).forEach((element) => {
      if (!element) {
        return;
      }

      const tag = String(element.tagName || '').toUpperCase();

      if (tag === 'VIDEO') {
        try {
          element.pause();
          element.removeAttribute('src');
          element.load();
        } catch (_error) {
        }
      }

      if (tag === 'IMG') {
        try {
          element.src = '';
        } catch (_error) {
        }
      }
    });
  };



  // ===== Label / note handlers =====
  const handleSetLabel = async (recordingId, labelId) => {
    const current = labelMap[recordingId]?.label;
    const next = current === labelId ? null : labelId; // toggle off if same
    setLabelMap((m) => ({ ...m, [recordingId]: { ...(m[recordingId] || {}), label: next } }));
    try {
      await api.updateRecordingMeta(recordingId, { label: next || '' });
    } catch (err) {
      setLabelMap((m) => ({ ...m, [recordingId]: { ...(m[recordingId] || {}), label: current } }));
      toast.error('Failed to save label');
    }
  };

  const handleSaveNote = async (recordingId) => {
    const note = noteDraft[recordingId] ?? (labelMap[recordingId]?.note || '');
    const previousNote = labelMap[recordingId]?.note;
    setLabelMap((m) => ({ ...m, [recordingId]: { ...(m[recordingId] || {}), note } }));
    try {
      await api.updateRecordingMeta(recordingId, { note });
      toast.success('Note saved');
      setNotePanelId(null);
    } catch (err) {
      setLabelMap((m) => ({ ...m, [recordingId]: { ...(m[recordingId] || {}), note: previousNote } }));
      toast.error('Failed to save note');
    }
  };

  // ===== Derived structure: recordings grouped by camera =====
  const recordingsByCamera = buildRecordingsByCamera(completedRecordings);
  const cameraRows = buildCameraRows(recordingsByCamera, validCameras);

  // ===== Playback stat and seek handlers =====
  const updatePlaybackStats = (recordingId, patch) => {
    setPlaybackStatsById((current) => ({
      ...current,
      [recordingId]: {
        ...(current[recordingId] || { currentTime: 0, duration: 0 }),
        ...patch,
      },
    }));
  };

  const handleVideoTimeUpdate = (recordingId, event) => {
    const seekState = seekStateRef.current[recordingId];
    if (seekState?.isSeeking) {
      return;
    }
    const currentTime = Number(event?.target?.currentTime) || 0;
    const duration = Number(event?.target?.duration) || 0;
    updatePlaybackStats(recordingId, { currentTime, duration });
  };

  const handleVideoLoadedMetadata = (recordingId, event) => {
    const currentTime = Number(event?.target?.currentTime) || 0;
    const duration = Number(event?.target?.duration) || 0;
    updatePlaybackStats(recordingId, { currentTime, duration });
  };

  const handleSeekChange = (recordingId, value, useExpandedRef = false) => {
    setHoveredId(recordingId);
    const refMap = useExpandedRef ? expandedVideoRefs.current : videoRefs.current;
    const video = refMap[recordingId];
    if (!video) {
      return;
    }
    const nextTime = Number(value);
    if (!Number.isFinite(nextTime)) {
      return;
    }
    video.currentTime = nextTime;
    updatePlaybackStats(recordingId, {
      currentTime: nextTime,
      duration: Number(video.duration) || 0,
    });
  };

  const handleSeekStart = (recordingId, useExpandedRef = false) => {
    const refMap = useExpandedRef ? expandedVideoRefs.current : videoRefs.current;
    const video = refMap[recordingId];

    const wasPlaying = !!(video && !video.paused);
    if (wasPlaying) {
      video.pause();
    }

    seekStateRef.current[recordingId] = {
      isSeeking: true,
      wasPlaying,
      useExpandedRef,
    };
  };

  const handleSeekEnd = (recordingId) => {
    const seekState = seekStateRef.current[recordingId];
    if (!seekState) {
      return;
    }

    seekStateRef.current[recordingId] = {
      ...seekState,
      isSeeking: false,
    };

    const refMap = seekState.useExpandedRef ? expandedVideoRefs.current : videoRefs.current;
    const video = refMap[recordingId];
    if (seekState.wasPlaying && video && typeof video.play === 'function') {
      video.play().catch(() => {});
      setPlayingId(recordingId);
    }
  };

  const stepFrame = (recording, direction, useExpandedRef = false) => {
    if (playbackMode !== 'play') {
      return;
    }

    const recordingId = recording.id;
    setHoveredId(recordingId);

    const refMap = useExpandedRef ? expandedVideoRefs.current : videoRefs.current;
    const video = refMap[recordingId];
    if (!video) {
      return;
    }

    if (!video.paused) {
      video.pause();
      setPlayingId(null);
    }

    const metadata = getRecordingMetadata(recording);
    const fps = resolvePlaybackFps(recording, metadata);

    const frameSeconds = 1 / fps;
    const duration = Number(video.duration) || Number(playbackStatsById[recordingId]?.duration) || 0;
    const maxTime = Math.max(0, duration - frameSeconds);
    const nextTime = Math.min(maxTime, Math.max(0, (Number(video.currentTime) || 0) + (direction * frameSeconds)));

    video.currentTime = nextTime;
    updatePlaybackStats(recordingId, {
      currentTime: nextTime,
      duration,
    });
  };

  // ===== Row and card interactions =====
  const scrollRow = (cameraId, direction) => {
    const rowElement = rowScrollRefs.current[cameraId];
    if (!rowElement) return;

    const firstCard = rowElement.querySelector('.reel-card');
    const cardWidth = firstCard ? firstCard.offsetWidth : 320;
    const scrollAmount = (cardWidth + 20) * 2;

    rowElement.scrollBy({
      left: direction * scrollAmount,
      behavior: 'smooth',
    });
  };

  const handleMouseEnter = (recordingId) => {
    setHoveredId(recordingId);
    const video = videoRefs.current[recordingId];
    if (video && typeof video.play === 'function') {
      video.play().catch(() => {});
    }
  };

  const handleMouseLeave = (recordingId) => {
    setHoveredId((current) => (current === recordingId ? null : current));
    const video = videoRefs.current[recordingId];
    if (recordingId !== playingId && video && typeof video.pause === 'function') {
      video.pause();
    }
  };

  const handleClick = (recordingId) => {
    if (Date.now() < suppressClickUntilRef.current) {
      return;
    }

    if (playbackMode === 'stream') {
      setPlayingId((current) => (current === recordingId ? null : recordingId));
      return;
    }

    const video = videoRefs.current[recordingId];
    if (!video) {
      setPlayingId((current) => (current === recordingId ? null : recordingId));
      return;
    }

    if (video.paused) {
      video.play().catch(() => {});
      setPlayingId(recordingId);
    } else {
      video.pause();
      setPlayingId(null);
    }
  };

  const handlePlaybackModeChange = (event) => {
    const mode = event.target.value === 'stream' ? 'stream' : 'play';
    api.setRecordingPlaybackMode(mode);
    setPlaybackMode(mode);
    setPlayingId(null);
    setHoveredId(null);
  };

  const handleTimelinePointClick = (cameraId, recordingId) => {
    setHighlightedRecordingId(recordingId);
    setHoveredId(recordingId);

    const rowElement = rowScrollRefs.current[cameraId];
    const cardElement = reelCardRefs.current[recordingId];
    if (rowElement && cardElement) {
      const targetLeft = Math.max(0, cardElement.offsetLeft - 8);
      rowElement.scrollTo({
        left: targetLeft,
        behavior: 'smooth',
      });
    }
  };

  const handleDownloadRecording = (recordingId) => {
    const url = api.downloadRecording(recordingId);
    const link = document.createElement('a');
    link.href = url;
    link.download = '';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  const handleDeleteRecording = async (recordingId) => {
    const confirmed = window.confirm('Delete this recording?');
    if (!confirmed) {
      return;
    }

    try {
      await api.deleteRecording(recordingId);
      setRemovedRecordingIds((current) => ({ ...current, [recordingId]: true }));
      if (highlightedRecordingId === recordingId) {
        setHighlightedRecordingId(null);
      }
      if (hoveredId === recordingId) {
        setHoveredId(null);
      }
      if (playingId === recordingId) {
        setPlayingId(null);
      }
    } catch (error) {
      console.error('Failed to delete recording:', error);
      window.alert(error?.response?.data?.detail || 'Failed to delete recording');
    }
  };

  // ===== Desktop drag-to-scroll handlers =====
  const handleRowPointerDown = (cameraId, event) => {
    if (event.pointerType !== 'mouse') {
      return;
    }

    const target = event.target;
    if (
      target instanceof Element
      && target.closest('button, input, select, textarea')
    ) {
      return;
    }

    if (event.pointerType === 'mouse' && event.button !== 0) {
      return;
    }

    const rowElement = rowScrollRefs.current[cameraId];
    if (!rowElement) {
      return;
    }

    rowDragStateRef.current = {
      isDragging: true,
      cameraId,
      pointerId: event.pointerId,
      startX: event.clientX,
      startScrollLeft: rowElement.scrollLeft,
      moved: false,
    };

    rowElement.classList.add('dragging');
    if (rowElement.setPointerCapture) {
      rowElement.setPointerCapture(event.pointerId);
    }
  };

  const handleRowPointerMove = (cameraId, event) => {
    const dragState = rowDragStateRef.current;
    if (!dragState.isDragging || dragState.cameraId !== cameraId) {
      return;
    }

    const rowElement = rowScrollRefs.current[cameraId];
    if (!rowElement) {
      return;
    }

    const deltaX = event.clientX - dragState.startX;
    if (Math.abs(deltaX) > 4) {
      rowDragStateRef.current.moved = true;
      event.preventDefault();
    }
    rowElement.scrollLeft = dragState.startScrollLeft - (deltaX * MOUSE_DRAG_SENSITIVITY);
  };

  const endRowDrag = (cameraId, event) => {
    const dragState = rowDragStateRef.current;
    if (!dragState.isDragging || dragState.cameraId !== cameraId) {
      return;
    }

    const rowElement = rowScrollRefs.current[cameraId];
    if (rowElement) {
      rowElement.classList.remove('dragging');
      if (rowElement.releasePointerCapture && dragState.pointerId !== null) {
        try {
          rowElement.releasePointerCapture(dragState.pointerId);
        } catch (_error) {
        }
      }
    }

    if (dragState.moved) {
      suppressClickUntilRef.current = Date.now() + 180;
      snapRowToNearestCard(cameraId);
    }

    rowDragStateRef.current = {
      isDragging: false,
      cameraId: null,
      pointerId: null,
      startX: 0,
      startScrollLeft: 0,
      moved: false,
    };
  };

  // ===== Touch drag-to-scroll handlers =====
  const handleRowTouchStart = (cameraId, event) => {
    if (!event.touches || event.touches.length === 0) {
      return;
    }

    const target = event.target;
    if (
      target instanceof Element
      && target.closest('button, input, select, textarea')
    ) {
      return;
    }

    const rowElement = rowScrollRefs.current[cameraId];
    if (!rowElement) {
      return;
    }

    const touch = event.touches[0];
    touchDragStateRef.current = {
      active: true,
      cameraId,
      startX: touch.clientX,
      startY: touch.clientY,
      startScrollLeft: rowElement.scrollLeft,
      horizontalLocked: false,
      moved: false,
      lastDx: 0,
    };
  };

  const handleRowTouchMove = (cameraId, event) => {
    const dragState = touchDragStateRef.current;
    if (!dragState.active || dragState.cameraId !== cameraId || !event.touches || event.touches.length === 0) {
      return;
    }

    const rowElement = rowScrollRefs.current[cameraId];
    if (!rowElement) {
      return;
    }

    const touch = event.touches[0];
    const dx = touch.clientX - dragState.startX;
    const dy = touch.clientY - dragState.startY;

    if (!dragState.horizontalLocked) {
      if (Math.abs(dx) > 6 && Math.abs(dx) > Math.abs(dy)) {
        dragState.horizontalLocked = true;
      } else if (Math.abs(dy) > 6 && Math.abs(dy) >= Math.abs(dx)) {
        dragState.active = false;
        return;
      }
    }

    if (!dragState.horizontalLocked) {
      return;
    }

    if (Math.abs(dx) > 3) {
      dragState.moved = true;
    }

    dragState.lastDx = dx;

    event.preventDefault();
    rowElement.scrollLeft = dragState.startScrollLeft - (dx * TOUCH_DRAG_SENSITIVITY);
  };

  const handleRowTouchEnd = (cameraId) => {
    const dragState = touchDragStateRef.current;
    const rowElement = rowScrollRefs.current[cameraId];
    if (!dragState.active || dragState.cameraId !== cameraId) {
      touchDragStateRef.current = {
        active: false,
        cameraId: null,
        startX: 0,
        startY: 0,
        startScrollLeft: 0,
        horizontalLocked: false,
        moved: false,
        lastDx: 0,
      };
      return;
    }

    if (dragState.moved && rowElement) {
      suppressClickUntilRef.current = Date.now() + 220;

      const firstCard = rowElement.querySelector('.reel-card');
      const gap = parseFloat(window.getComputedStyle(rowElement).columnGap || '20') || 20;
      const stepWidth = (firstCard ? firstCard.offsetWidth : 320) + gap;
      const flickThreshold = 20;

      if (Math.abs(dragState.lastDx) >= flickThreshold) {
        const direction = dragState.lastDx < 0 ? 1 : -1;
        const nextLeft = Math.max(0, dragState.startScrollLeft + (direction * stepWidth));
        rowElement.scrollTo({ left: nextLeft, behavior: 'smooth' });
      } else {
        snapRowToNearestCard(cameraId);
      }
    }

    touchDragStateRef.current = {
      active: false,
      cameraId: null,
      startX: 0,
      startY: 0,
      startScrollLeft: 0,
      horizontalLocked: false,
      moved: false,
      lastDx: 0,
    };
  };

  // ===== Expanded card modal handlers =====
  const handleOpenExpanded = (recording, cameraId, index) => {
    stopInlineMediaPlayback();
    setExpandedContext({ cameraId, index });
    setHoveredId(recording.id);
    setPlayingId(null);
  };

  const handleCloseExpanded = () => {
    setExpandedContext(null);
  };

  const handleExpandedNavigate = (direction) => {
    setExpandedContext((current) => {
      if (!current) {
        return current;
      }
      const rowRecordings = recordingsByCamera[current.cameraId] || [];
      if (rowRecordings.length === 0) {
        return current;
      }

      const nextIndex = Math.min(
        rowRecordings.length - 1,
        Math.max(0, current.index + direction)
      );
      const nextRecording = rowRecordings[nextIndex];
      if (nextRecording) {
        stopInlineMediaPlayback();
        setHoveredId(nextRecording.id);
        setPlayingId(null);
      }
      return { ...current, index: nextIndex };
    });
  };

  // Keep inline card playback stopped while expanded view is open.
  useEffect(() => {
    if (expandedContext) {
      stopInlineMediaPlayback();
      setPlayingId(null);
    }
  }, [expandedContext]);

  const expandedRowRecordings = expandedContext
    ? (recordingsByCamera[expandedContext.cameraId] || [])
    : [];
  const expandedRecording = expandedContext
    ? expandedRowRecordings[expandedContext.index] || null
    : null;
  const canNavigatePrev = !!expandedContext && expandedContext.index > 0;
  const canNavigateNext = !!expandedContext && expandedContext.index < (expandedRowRecordings.length - 1);



  // ===== Main UI render logic =====
  // Early return for empty state
  if (completedRecordings.length === 0) {
    return (
      <div className="reels-container">
        <div className="empty-reels-state">
          <Camera size={64} />
          <h3>No Completed Recordings</h3>
          <p>Completed recordings will appear here.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="reels-container">
      <div className="reels-header">
        <h2>Recordings</h2>
        <div className="header-controls">
          <select
            className="form-control form-select"
            value={playbackMode}
            onChange={handlePlaybackModeChange}
            style={{ width: '170px' }}
          >
            <option value="play">File Playback</option>
            <option value="stream">Legacy Stream (Video only)</option>
          </select>
          <span className="recordings-count">{completedRecordings.length} videos</span>
          <span className="page-indicator">{cameraRows.length} camera rows</span>
          <label style={{ display: 'inline-flex', alignItems: 'center', gap: 5, cursor: 'pointer', fontSize: 13, userSelect: 'none', color: showLabeledOnly ? '#c62828' : undefined }}>
            <input type="checkbox" checked={showLabeledOnly} onChange={(e) => setShowLabeledOnly(e.target.checked)} style={{ accentColor: '#c62828', cursor: 'pointer' }} />
            Labeled only
          </label>
        </div>
      </div>

      <div className="camera-rows">
        {/* === Camera Row Rendering === 
            Each row contains chart + video carousel for one camera */}
        {cameraRows.map((row) => {
          const {
            chartMetrics,
            chartMaxVelocity,
            chartMaxBgDiff,
            chartMaxDuration,
            axisTicks,
            totalDataSpanHours,
          } = buildRowMetricsData(row.recordings, chartZoomHours, chartScrollOffsetHours);

          return (
            <div key={row.cameraId} className="camera-row-card">
              <div className="camera-row-header">
                <h3>{row.cameraName}</h3>
                <div className="camera-row-meta">
                  <span>{row.recordings.length} videos</span>
                </div>
              </div>

              {/* === Chart Card Component === */}
              <ChartCard 
                row={row}
                chartDate={chartDate}
                setChartDate={setChartDate}
                handleTimelinePointClick={handleTimelinePointClick}
                highlightedRecordingId={highlightedRecordingId}
                chartToggles={chartToggles}
                setChartToggles={setChartToggles}
                MOTION_COLORS={MOTION_COLORS}
              />

              {/* === Video Carousel Section === */}
              {/* Carousel navigation wrapper with left/right scroll buttons */}
              <div className="reels-carousel-wrapper">
                <button
                  className="carousel-nav-button prev"
                  onClick={() => scrollRow(row.cameraId, -1)}
                  title="Scroll left"
                >
                  <ChevronLeft size={24} />
                </button>

                <div
                  className="reels-grid row-grid"
                  ref={(el) => {
                    rowScrollRefs.current[row.cameraId] = el;
                  }}
                  onPointerDown={(event) => handleRowPointerDown(row.cameraId, event)}
                  onPointerMove={(event) => handleRowPointerMove(row.cameraId, event)}
                  onPointerUp={(event) => endRowDrag(row.cameraId, event)}
                  onPointerCancel={(event) => endRowDrag(row.cameraId, event)}
                  onTouchStart={(event) => handleRowTouchStart(row.cameraId, event)}
                  onTouchMove={(event) => handleRowTouchMove(row.cameraId, event)}
                  onTouchEnd={() => handleRowTouchEnd(row.cameraId)}
                  onTouchCancel={() => handleRowTouchEnd(row.cameraId)}
                >
                  {/* === Reel Cards Component === */}
                  {row.recordings.map((recording, recordingIndex) => (
                    <ReelCard
                      key={recording.id}
                      recording={recording}
                      recordingIndex={recordingIndex}
                      row={row}
                      playingId={playingId}
                      hoveredId={hoveredId}
                      expandedContext={expandedContext}
                      playbackStatsById={playbackStatsById}
                      playbackMode={playbackMode}
                      highlightedRecordingId={highlightedRecordingId}
                      labelMap={labelMap}
                      notePanelId={notePanelId}
                      noteDraft={noteDraft}
                      overlayIds={overlayIds}
                      setOverlayIds={setOverlayIds}
                      ALERT_LABELS={ALERT_LABELS}
                      MOTION_COLORS={MOTION_COLORS}
                      videoRefs={videoRefs}
                      reelCardRefs={reelCardRefs}
                      handleMouseEnter={handleMouseEnter}
                      handleMouseLeave={handleMouseLeave}
                      handleClick={handleClick}
                      handleOpenExpanded={handleOpenExpanded}
                      handleVideoTimeUpdate={handleVideoTimeUpdate}
                      handleVideoLoadedMetadata={handleVideoLoadedMetadata}
                      handleSeekStart={handleSeekStart}
                      handleSeekEnd={handleSeekEnd}
                      handleSeekChange={handleSeekChange}
                      stepFrame={stepFrame}
                      handleDownloadRecording={handleDownloadRecording}
                      handleDeleteRecording={handleDeleteRecording}
                      handleSetLabel={handleSetLabel}
                      setNotePanelId={setNotePanelId}
                      setNoteDraft={setNoteDraft}
                      handleSaveNote={handleSaveNote}
                    />
                  ))}
                </div>

                <button
                  className="carousel-nav-button next"
                  onClick={() => scrollRow(row.cameraId, 1)}
                  title="Scroll right"
                >
                  <ChevronRight size={24} />
                </button>
              </div>
            </div>
          );
        })}
      </div>

      {expandedRecording && (
        /* === Expanded Modal View === 
           Full-screen modal for focused reel playback with navigation */
        <div className="enlarged-overlay" onClick={handleCloseExpanded}>
          <div className="enlarged-content" onClick={(event) => event.stopPropagation()}>
            <div className="enlarged-card-stage">
              <div className="reel-card enlarged-reel-card">
                <div className="enlarged-reel-header">
                  <span className="enlarged-title">{expandedRecording.filename || expandedRecording.id}</span>
                  <button
                    type="button"
                    className="enlarged-close"
                    onClick={handleCloseExpanded}
                    title="Close enlarged playback"
                  >
                    <X size={16} />
                  </button>
                </div>
                {(() => {
                  const {
                    timestampParts,
                    durationValue,
                    velValue,
                    diffValue,
                    loudnessValue,
                    playbackStats,
                    playbackDuration,
                    playbackProgress,
                    playbackFrame,
                    totalFrames,
                  } = getRecordingPlaybackViewData(expandedRecording, playbackStatsById[expandedRecording.id]);

                  return (
                    <>
                      <div className="reel-timestamp">
                        <span className="reel-date">{timestampParts.date}</span>
                        {timestampParts.time && <span className="reel-time">{timestampParts.time}</span>}
                      </div>

                      <div className="reel-thumbnail">
                        <button
                          type="button"
                          className="enlarged-inline-nav prev"
                          disabled={!canNavigatePrev}
                          onClick={(event) => {
                            event.stopPropagation();
                            handleExpandedNavigate(-1);
                          }}
                          title="Previous"
                        >
                          <ChevronLeft size={20} />
                        </button>

                        {playbackMode === 'stream' ? (
                          <img
                            ref={(el) => (expandedVideoRefs.current[expandedRecording.id] = el)}
                            className="reel-video"
                            src={api.appendQueryParams(api.getRecordingStreamUrl(expandedRecording.id, 'stream'), {
                              ts: Date.now(),
                            })}
                            alt={`Recording ${expandedRecording.id}`}
                          />
                        ) : (
                          <video
                            ref={(el) => (expandedVideoRefs.current[expandedRecording.id] = el)}
                            className="reel-video"
                            src={api.getRecordingStreamUrl(expandedRecording.id, 'play')}
                            loop={false}
                            playsInline
                            controls
                            autoPlay
                            onLoadedMetadata={(event) => handleVideoLoadedMetadata(expandedRecording.id, event)}
                            onTimeUpdate={(event) => handleVideoTimeUpdate(expandedRecording.id, event)}
                          />
                        )}

                        <button
                          type="button"
                          className="enlarged-inline-nav next"
                          disabled={!canNavigateNext}
                          onClick={(event) => {
                            event.stopPropagation();
                            handleExpandedNavigate(1);
                          }}
                          title="Next"
                        >
                          <ChevronRight size={20} />
                        </button>
                      </div>

                      <div className="reel-info">
                        <div className="reel-playback-controls">
                          <div className="reel-progress-header">
                            <div className="reel-progress-left">
                              <TimeFrameBadge timeText={formatPlaybackTime(playbackStats.currentTime)} frame={playbackFrame} />
                            </div>
                            <FrameStepButtons
                              disabled={playbackMode !== 'play'}
                              onStepBack={(event) => {
                                event.stopPropagation();
                                stepFrame(expandedRecording, -1, true);
                              }}
                              onStepForward={(event) => {
                                event.stopPropagation();
                                stepFrame(expandedRecording, 1, true);
                              }}
                            />
                            <div className="reel-progress-right">
                              <TimeFrameBadge timeText={formatPlaybackTime(playbackDuration)} frame={totalFrames} />
                            </div>
                          </div>
                          <input
                            type="range"
                            min={0}
                            max={Math.max(playbackDuration, 0.01)}
                            step={0.01}
                            value={Math.min(playbackStats.currentTime, Math.max(playbackDuration, 0.01))}
                            disabled={playbackMode !== 'play'}
                            onMouseDown={() => handleSeekStart(expandedRecording.id, true)}
                            onMouseUp={() => handleSeekEnd(expandedRecording.id)}
                            onTouchStart={() => handleSeekStart(expandedRecording.id, true)}
                            onTouchEnd={() => handleSeekEnd(expandedRecording.id)}
                            onChange={(event) => handleSeekChange(expandedRecording.id, event.target.value, true)}
                            className="reel-progress-slider"
                            style={{ '--progress': `${playbackProgress}%` }}
                          />
                          <SimpleMotionPlot recording={expandedRecording} MOTION_COLORS={MOTION_COLORS} />
                        </div>
                        <RecordingMetaInfo durationText={formatDuration(durationValue)} velValue={velValue} diffValue={diffValue} loudnessValue={loudnessValue} MOTION_COLORS={MOTION_COLORS} />
                      </div>
                    </>
                  );
                })()}
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default EventView;
