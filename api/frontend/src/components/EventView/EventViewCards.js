import React, { useEffect, useState, useRef, useMemo, useCallback } from 'react';
import metricConfig from '../metric_config.json';
import { Cpu, Play, ChevronLeft, ChevronRight, Pause, Maximize2, Download, Trash2, Loader } from 'lucide-react';
import { toast } from 'react-hot-toast';
import { api } from '../../api';
import {
  TimeFrameBadge,
  FrameStepButtons,
  RecordingMetaInfo,
  formatDuration,
  formatPlaybackTime,
  getRecordingPlaybackViewData,
  buildDateMetricsData,
} from './EventViewUtils';

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
    chartMaxPerson,
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
              <input type="checkbox" checked={chartToggles.person} onChange={(e) => setChartToggles(prev => ({ ...prev, person: e.target.checked }))} style={{ accentColor: MOTION_COLORS.person, transform: 'scale(0.8)' }} />
              <span className="legend-dot" style={{ backgroundColor: MOTION_COLORS.person }} />
              {metricConfig.labels.person}
            </label>
            <label style={{ display: 'flex', alignItems: 'center', gap: '4px', fontSize: '10px', cursor: 'pointer' }}>
              <input type="checkbox" checked={chartToggles.duration} onChange={(e) => setChartToggles(prev => ({ ...prev, duration: e.target.checked }))} style={{ accentColor: MOTION_COLORS.duration, transform: 'scale(0.8)' }} />
              <span className="legend-dot" style={{ backgroundColor: MOTION_COLORS.duration }} />
              {metricConfig.labels.duration}
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
          const personHeight = chartToggles.person ? Math.max(2, (metric.person / chartMaxPerson) * 100) : 0;
          const durationHeight = chartToggles.duration ? Math.max(2, (metric.duration / chartMaxDuration) * 100) : 0;
          const bgDiffHeight = chartToggles.bgDiff ? Math.max(2, (metric.bgDiff / chartMaxBgDiff) * 100) : 0;
          const loudnessHeight = chartToggles.loudness ? Math.max(2, (metric.loudness / chartMaxLoudness) * 100) : 0;
          const markerHeight = Math.max(velocityHeight, personHeight, durationHeight, bgDiffHeight, loudnessHeight);
          return (
            <React.Fragment key={metric.id}>
              <div className="metrics-bar-group" style={{ left: `${metric.xPercent}%` }}>
                {chartToggles.velocity && (
                  <span className="metrics-bar velocity" style={{ height: `${velocityHeight}%`, backgroundColor: MOTION_COLORS.velocity }} title={`Velocity: ${metric.velocity.toFixed(3)}`} />
                )}
                {chartToggles.person && (
                  <span className="metrics-bar person" style={{ height: `${personHeight}%`, backgroundColor: MOTION_COLORS.person }} title={`person: ${metric.person}`} />
                )}
                {chartToggles.duration && (
                  <span className="metrics-bar duration" style={{ height: `${durationHeight}%`, backgroundColor: MOTION_COLORS.duration }} title={`duration: ${metric.duration.toFixed(1)}s`} />
                )}
                {chartToggles.bgDiff && (
                  <span className="metrics-bar bgdiff" style={{ height: `${bgDiffHeight}%`, backgroundColor: MOTION_COLORS.bgDiff }} title={`bg_diff: ${metric.bgDiff.toFixed(0)}`} />
                )}
                {chartToggles.loudness && (
                  <span className="metrics-bar loudness" style={{ height: `${loudnessHeight}%`, backgroundColor: MOTION_COLORS.loudness }} title={`loudness: ${metric.loudness.toFixed(2)}`} />
                )}
              </div>
              <button
                type="button"
                className={`motion-point-btn${highlightedRecordingId === metric.id ? ' active' : ''}`}
                style={{ left: `${metric.xPercent}%`, bottom: `${Math.min(96, markerHeight + 2)}%` }}
                onClick={() => handleTimelinePointClick(row.cameraId, metric.id)}
                title={`${metric.timeLabel} | vel: ${metric.velocity.toFixed(3)} | person: ${metric.person} | dur: ${metric.duration.toFixed(1)}s | diff: ${metric.bgDiff.toFixed(0)} | loud: ${metric.loudness.toFixed(2)}`}
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
      title={isGenerating ? `Generating overlay… ${progress}%` : isActive ? 'Disable overlay' : 'Predict object/motion with AI'}
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
      {isGenerating ? <Loader size={12} className="spin-icon" /> : <Cpu size={12} />}
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
  const [videoLoading, setVideoLoading] = useState(false);
  const prevShouldLoad = useRef(false);

  // Track when video src is set (loading starts) vs when it becomes playable
  useEffect(() => {
    if (shouldLoadVideo && !prevShouldLoad.current && playbackMode === 'play') {
      setVideoLoading(true);
    }
    if (!shouldLoadVideo) {
      setVideoLoading(false);
    }
    prevShouldLoad.current = shouldLoadVideo;
  }, [shouldLoadVideo, playbackMode]);

  const {
    timestampParts,
    durationValue,
    velValue,
    diffValue,
    loudnessValue,
    personValue,
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
            onCanPlay={() => setVideoLoading(false)}
            onError={(e) => { console.error('Video error:', recording.id, e); setVideoLoading(false); }}
          />
        )}

        {isPlaying && (
          <div className="pause-indicator">
            <Pause size={24} />
          </div>
        )}

        {videoLoading && playbackMode === 'play' && (
          <div className="transcode-overlay">
            <Loader size={22} className="spin-icon" />
            <span>Converting…</span>
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
            personValue={personValue}
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

export { SimpleMotionPlot, ChartCard, OverlayButton, ReelCard };
