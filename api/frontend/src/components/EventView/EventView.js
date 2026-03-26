import React, { useEffect, useState, useRef } from 'react';
import { Camera, ChevronLeft, ChevronRight, X } from 'lucide-react';
import { toast } from 'react-hot-toast';
import { api } from '../../api';
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
} from './EventViewUtils';
import { SimpleMotionPlot, ChartCard, ReelCard } from './EventViewCards';
import './EventView.css';

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
    duration: '#e91e63',
    person: '#8e24aa'
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
  const [chartToggles, setChartToggles] = useState({ velocity: true, bgDiff: false, loudness: false, duration: true, person: false }); // Chart visibility toggles
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
                    personValue,
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
                        <RecordingMetaInfo durationText={formatDuration(durationValue)} velValue={velValue} diffValue={diffValue} loudnessValue={loudnessValue} personValue={personValue} MOTION_COLORS={MOTION_COLORS} />
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
