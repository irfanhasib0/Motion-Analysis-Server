import React, { useEffect, useState, useRef } from 'react';
import { Camera, Play, ChevronLeft, ChevronRight, Pause, Maximize2, X, Download, Trash2, Archive, FolderOpen } from 'lucide-react';
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
} from './EventViewUtils';
import './LiveView.css';

const EventView = ({ recordings = [], cameras = [] }) => {
  // ===== Tunable interaction parameters =====
  const MOUSE_DRAG_SENSITIVITY = 2.2;
  const TOUCH_DRAG_SENSITIVITY = 3.0;
  const ALERT_LABELS = [
    { id: 'high',   short: 'H', label: 'High alert',   color: '#c62828' },
    { id: 'medium', short: 'M', label: 'Medium alert', color: '#e65100' },
    { id: 'low',    short: 'L', label: 'Low alert',    color: '#2e7d32' },
  ];
  const ALERT_COLOR = { high: '#c62828', medium: '#e65100', low: '#2e7d32' };

  // ===== Source data normalization =====
  const validRecordings = Array.isArray(recordings) ? recordings : [];
  const [removedRecordingIds, setRemovedRecordingIds] = useState({});
  const [labelMap, setLabelMap] = useState({});
  const [showLabeledOnly, setShowLabeledOnly] = useState(false);
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

  // Sync labelMap when recordings change
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

  // ===== UI state =====
  const [playingId, setPlayingId] = useState(null);
  const [hoveredId, setHoveredId] = useState(null);
  const [highlightedRecordingId, setHighlightedRecordingId] = useState(null);
  const [playbackMode, setPlaybackMode] = useState(api.getRecordingPlaybackMode());
  const [playbackStatsById, setPlaybackStatsById] = useState({});
  const [expandedContext, setExpandedContext] = useState(null);

  // ===== Archive state =====
  const [archivePath, setArchivePath] = useState('');
  const [archiveList, setArchiveList] = useState([]);
  const [archiveBusy, setArchiveBusy] = useState(false);
  const [archivePanelOpen, setArchivePanelOpen] = useState(false);
  const [archiveFilters, setArchiveFilters] = useState(() => { const today = new Date().toISOString().slice(0, 10); const yesterday = new Date(Date.now() - 86400000).toISOString().slice(0, 10); return { date_from: yesterday, date_to: today, min_vel: '', min_diff: '', min_duration: '', label_filter: '' }; });
  const [archiveExportResult, setArchiveExportResult] = useState(null);
  const [deleteAfterArchive, setDeleteAfterArchive] = useState(false);
  const [excludeMode, setExcludeMode] = useState(true);
  const [notePanelId, setNotePanelId] = useState(null);
  const [noteDraft, setNoteDraft] = useState({});

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

  // ===== Archive handlers =====
  const handleExportArchive = async () => {
    setArchiveBusy(true);
    setArchiveExportResult(null);
    try {
      const filters = {};
      if (archiveFilters.date_from) filters.date_from = archiveFilters.date_from;
      if (archiveFilters.date_to) filters.date_to = archiveFilters.date_to;
      if (archiveFilters.min_vel !== '') filters.min_vel = Number(archiveFilters.min_vel);
      if (archiveFilters.min_diff !== '') filters.min_diff = Number(archiveFilters.min_diff);
      if (archiveFilters.min_duration !== '') filters.min_duration = Number(archiveFilters.min_duration);
      if (archiveFilters.label_filter) filters.label_filter = [archiveFilters.label_filter];
      filters.exclude_mode = excludeMode;
      if (deleteAfterArchive) filters.delete_after = true;
      const res = await api.exportArchive(filters);
      setArchiveExportResult(res.data);
      toast.success(`Exported ${res.data.recordings_count} recording(s) → ${res.data.archive_name}`);
      if (deleteAfterArchive && res.data.deleted_count > 0) {
        toast(`${res.data.deleted_count} recording(s) removed from recordings.`, {
          icon: '⚠️',
          style: { background: '#b71c1c', color: '#fff', fontWeight: 600 },
          duration: 5000,
        });
        window.dispatchEvent(new CustomEvent('archive-unloaded', { detail: {} }));
      }
    } catch (err) {
      toast.error('Export failed: ' + (err?.response?.data?.detail || err.message));
    } finally {
      setArchiveBusy(false);
    }
  };

  const handleListArchives = async () => {
    setArchiveBusy(true);
    try {
      const res = await api.listArchives();
      setArchiveList(res.data.archives || []);
      if ((res.data.archives || []).length === 0) {
        toast('No archives found.');
      }
    } catch (err) {
      toast.error('List failed: ' + (err?.response?.data?.detail || err.message));
    } finally {
      setArchiveBusy(false);
    }
  };

  const handleLoadArchive = async (path) => {
    const target = path || archivePath.trim();
    if (!target) {
      toast.error('Please enter an archive directory path.');
      return;
    }
    setArchiveBusy(true);
    try {
      const res = await api.loadArchive(target);
      toast.success(`Loaded ${res.data.loaded_count} recording(s) from archive.`);
      // Merge loaded recordings into parent state if available
      if (res.data.recordings && res.data.recordings.length > 0) {
        // We signal parent via a custom event so App.js can refresh
        window.dispatchEvent(new CustomEvent('archive-loaded', { detail: res.data.recordings }));
      }
    } catch (err) {
      toast.error('Load failed: ' + (err?.response?.data?.detail || err.message));
    } finally {
      setArchiveBusy(false);
    }
  };

  const handleUnloadArchive = async (path) => {
    setArchiveBusy(true);
    try {
      const res = await api.unloadArchive(path);
      toast.success(`Unloaded ${res.data.unloaded_count} recording(s) from view.`);
      setArchiveList((prev) => prev.filter((a) => a.path !== path));
      window.dispatchEvent(new CustomEvent('archive-unloaded', { detail: { path } }));
    } catch (err) {
      toast.error('Unload failed: ' + (err?.response?.data?.detail || err.message));
    } finally {
      setArchiveBusy(false);
    }
  };

  // ===== Label / note handlers =====
  const handleSetLabel = async (recordingId, labelId) => {
    const current = labelMap[recordingId]?.label;
    const next = current === labelId ? null : labelId; // toggle off if same
    setLabelMap((m) => ({ ...m, [recordingId]: { ...(m[recordingId] || {}), label: next } }));
    try {
      await api.updateRecordingMeta(recordingId, { label: next || '' });
    } catch (err) {
      toast.error('Failed to save label');
    }
  };

  const handleSaveNote = async (recordingId) => {
    const note = noteDraft[recordingId] ?? (labelMap[recordingId]?.note || '');
    setLabelMap((m) => ({ ...m, [recordingId]: { ...(m[recordingId] || {}), note } }));
    try {
      await api.updateRecordingMeta(recordingId, { note });
      toast.success('Note saved');
      setNotePanelId(null);
    } catch (err) {
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

  const ArchivePanel = () => (
    <div style={{ background: 'rgba(0,150,136,0.06)', border: '1px solid rgba(0,150,136,0.18)', borderRadius: 14, padding: '14px 18px', marginBottom: 18 }}>
      <div
        style={{ display: 'flex', alignItems: 'center', gap: 10, cursor: 'pointer', userSelect: 'none' }}
        onClick={() => setArchivePanelOpen((o) => !o)}
      >
        <Archive size={18} style={{ color: '#00897b' }} />
        <span style={{ fontWeight: 700, fontSize: 14, color: '#004d40' }}>Recording Archives</span>
        <span style={{ marginLeft: 'auto', fontSize: 12, color: '#546e7a' }}>{archivePanelOpen ? '▲ Hide' : '▼ Show'}</span>
      </div>

      {archivePanelOpen && (
        <div style={{ marginTop: 14, display: 'flex', flexDirection: 'column', gap: 14 }}>

          {/* Export section */}
          <div style={{ background: 'rgba(255,255,255,0.7)', borderRadius: 10, padding: '12px 14px', border: '1px solid rgba(0,150,136,0.15)' }}>
            <div style={{ fontWeight: 600, fontSize: 13, color: '#004d40', marginBottom: 10 }}>Export recordings to archive</div>

            {/* Filter row 1 — dates */}
            <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center', marginBottom: 8 }}>
              <label style={{ fontSize: 12, color: '#546e7a', minWidth: 60 }}>From</label>
              <input
                type="date"
                className="form-control"
                value={archiveFilters.date_from}
                onChange={(e) => setArchiveFilters((f) => ({ ...f, date_from: e.target.value }))}
                style={{ width: 150 }}
              />
              <label style={{ fontSize: 12, color: '#546e7a', minWidth: 20 }}>To</label>
              <input
                type="date"
                className="form-control"
                value={archiveFilters.date_to}
                onChange={(e) => setArchiveFilters((f) => ({ ...f, date_to: e.target.value }))}
                style={{ width: 150 }}
              />
            </div>

            {/* Filter row 2 — thresholds */}
            <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center', marginBottom: 10 }}>
              <label style={{ display: 'inline-flex', alignItems: 'center', gap: 5, cursor: 'pointer', userSelect: 'none', fontSize: 12, fontWeight: 600, color: excludeMode ? '#b71c1c' : '#757575', marginRight: 2 }}>
                <input
                  type="checkbox"
                  checked={excludeMode}
                  onChange={() => setExcludeMode(true)}
                  style={{ accentColor: '#c62828', width: 14, height: 14, cursor: 'pointer' }}
                />
                Exclude ≤
              </label>
              <label style={{ display: 'inline-flex', alignItems: 'center', gap: 5, cursor: 'pointer', userSelect: 'none', fontSize: 12, fontWeight: 600, color: !excludeMode ? '#1565c0' : '#757575', marginRight: 4 }}>
                <input
                  type="checkbox"
                  checked={!excludeMode}
                  onChange={() => setExcludeMode(false)}
                  style={{ accentColor: '#1565c0', width: 14, height: 14, cursor: 'pointer' }}
                />
                Include ≤
              </label>
              <label style={{ fontSize: 12, color: '#546e7a' }}>vel</label>
              <select
                className="form-control form-select"
                value={archiveFilters.min_vel}
                onChange={(e) => setArchiveFilters((f) => ({ ...f, min_vel: e.target.value }))}
                style={{ width: 100 }}
              >
                <option value="">{excludeMode ? 'None' : 'Any'}</option>
                <option value="0.1">0.1</option>
                <option value="0.2">0.2</option>
                <option value="0.5">0.5</option>
                <option value="1.0">1.0</option>
                <option value="1.5">1.5</option>
                <option value="2.0">2.0</option>
              </select>
              <label style={{ fontSize: 12, color: '#546e7a' }}>diff</label>
              <select
                className="form-control form-select"
                value={archiveFilters.min_diff}
                onChange={(e) => setArchiveFilters((f) => ({ ...f, min_diff: e.target.value }))}
                style={{ width: 100 }}
              >
                <option value="">{excludeMode ? 'None' : 'Any'}</option>
                <option value="10">10</option>
                <option value="20">20</option>
                <option value="50">50</option>
                <option value="100">100</option>
                <option value="150">150</option>
                <option value="200">200</option>
              </select>
              <label style={{ fontSize: 12, color: '#546e7a' }}>duration</label>
              <select
                className="form-control form-select"
                value={archiveFilters.min_duration}
                onChange={(e) => setArchiveFilters((f) => ({ ...f, min_duration: e.target.value }))}
                style={{ width: 110 }}
              >
                <option value="">{excludeMode ? 'None' : 'Any'}</option>
                <option value="5">5 s</option>
                <option value="10">10 s</option>
                <option value="15">15 s</option>
                <option value="30">30 s</option>
                <option value="60">60 s</option>
              </select>
            </div>

            {/* Label filter */}
            <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center', marginBottom: 10 }}>
              <label style={{ fontSize: 12, color: '#546e7a' }}>Alert label</label>
              <select
                className="form-control form-select"
                value={archiveFilters.label_filter}
                onChange={(e) => setArchiveFilters((f) => ({ ...f, label_filter: e.target.value }))}
                style={{ width: 150 }}
              >
                <option value="">All</option>
                <option value="high">🔴 High alert</option>
                <option value="medium">🟠 Medium alert</option>
                <option value="low">🟢 Low alert</option>
              </select>
            </div>

            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
              <button
                type="button"
                className="btn btn-primary"
                style={{ padding: '6px 14px', fontSize: 13, whiteSpace: 'nowrap' }}
                disabled={archiveBusy}
                onClick={handleExportArchive}
              >
                <Download size={14} style={{ marginRight: 4 }} />
                Export Archive
              </button>
              <button
                type="button"
                className="btn btn-secondary"
                style={{ padding: '6px 14px', fontSize: 13, whiteSpace: 'nowrap' }}
                disabled={archiveBusy}
                onClick={handleListArchives}
              >
                <FolderOpen size={14} style={{ marginRight: 4 }} />
                List Archives
              </button>
              <label style={{ display: 'inline-flex', alignItems: 'center', gap: 6, cursor: 'pointer', userSelect: 'none', fontSize: 13, color: deleteAfterArchive ? '#c62828' : '#546e7a', fontWeight: deleteAfterArchive ? 600 : 400 }}>
                <input
                  type="checkbox"
                  checked={deleteAfterArchive}
                  onChange={(e) => setDeleteAfterArchive(e.target.checked)}
                  style={{ accentColor: '#c62828', width: 15, height: 15, cursor: 'pointer' }}
                />
                <Trash2 size={13} />
                Remove from recordings
              </label>
            </div>

            {/* Per-camera result */}
            {archiveExportResult && (
              <div style={{ marginTop: 12, padding: '10px 12px', background: 'rgba(0,150,136,0.07)', borderRadius: 8, border: '1px solid rgba(0,150,136,0.18)' }}>
                <div style={{ fontWeight: 600, fontSize: 12, color: '#004d40', marginBottom: 6 }}>
                  {archiveExportResult.archive_name}
                </div>
                {Object.entries(archiveExportResult.per_camera || {}).map(([cam, s]) => (
                  <div key={cam} style={{ fontSize: 12, color: s.archived === s.total ? '#2e7d32' : '#e65100', marginBottom: 2 }}>
                    {s.archived === s.total ? '✓' : '⚠'} {cam}: {s.archived}/{s.total} archived
                  </div>
                ))}
                {Object.keys(archiveExportResult.per_camera || {}).length === 0 && (
                  <div style={{ fontSize: 12, color: '#e65100' }}>No recordings matched the filters.</div>
                )}
              </div>
            )}
          </div>

          {/* Load archive section */}
          <div style={{ background: 'rgba(255,255,255,0.7)', borderRadius: 10, padding: '12px 14px', border: '1px solid rgba(0,150,136,0.15)' }}>
            <div style={{ fontWeight: 600, fontSize: 13, color: '#004d40', marginBottom: 8 }}>Load an archive into the event view</div>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
              <input
                type="text"
                className="form-control"
                placeholder="Archive folder path (e.g. /mnt/backup/recordings/archive_20260301_120000)"
                value={archivePath}
                onChange={(e) => setArchivePath(e.target.value)}
                style={{ flex: '1 1 320px', minWidth: 220 }}
              />
              <button
                type="button"
                className="btn btn-primary"
                style={{ padding: '6px 14px', fontSize: 13, whiteSpace: 'nowrap' }}
                disabled={archiveBusy}
                onClick={() => handleLoadArchive(null)}
              >
                Load Archive
              </button>
            </div>
          </div>

          {/* Archive list */}
          {archiveList.length > 0 && (
            <div style={{ background: 'rgba(255,255,255,0.7)', borderRadius: 10, padding: '12px 14px', border: '1px solid rgba(0,150,136,0.15)' }}>
              <div style={{ fontWeight: 600, fontSize: 13, color: '#004d40', marginBottom: 10 }}>Found archives</div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                {archiveList.map((arc) => (
                  <div key={arc.path} style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', padding: '8px 10px', background: 'rgba(0,150,136,0.05)', borderRadius: 8, border: '1px solid rgba(0,150,136,0.12)' }}>
                    <div style={{ flex: 1, minWidth: 200 }}>
                      <div style={{ fontWeight: 600, fontSize: 13, color: '#004d40' }}>{arc.name}</div>
                      <div style={{ fontSize: 11, color: '#546e7a' }}>{arc.archived_at ? new Date(arc.archived_at).toLocaleString() : ''} · {arc.recordings_count} recording(s)</div>
                      <div style={{ fontSize: 11, color: '#78909c', wordBreak: 'break-all' }}>{arc.path}</div>
                    </div>
                    <button
                      type="button"
                      className="btn btn-secondary"
                      style={{ padding: '4px 12px', fontSize: 12 }}
                      disabled={archiveBusy}
                      onClick={() => handleLoadArchive(arc.path)}
                    >
                      Load
                    </button>
                    <button
                      type="button"
                      className="btn btn-danger"
                      style={{ padding: '4px 12px', fontSize: 12 }}
                      disabled={archiveBusy}
                      onClick={() => handleUnloadArchive(arc.path)}
                    >
                      Unload
                    </button>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );

  if (completedRecordings.length === 0) {
    return (
      <div className="reels-container">
        <ArchivePanel />
        <div className="empty-reels-state">
          <Camera size={64} />
          <h3>No Completed Recordings</h3>
          <p>Completed recordings will appear here, or load an archive above.</p>
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

      <ArchivePanel />

      <div className="camera-rows">
        {cameraRows.map((row) => {
          const {
            chartMetrics,
            chartMaxVelocity,
            chartMaxBgDiff,
            axisTicks,
          } = buildRowMetricsData(row.recordings);

          return (
            <div key={row.cameraId} className="camera-row-card">
              <div className="camera-row-header">
                <h3>{row.cameraName}</h3>
                <div className="camera-row-meta">
                  <span>{row.recordings.length} videos</span>
                </div>
              </div>

              <div className="camera-row-metrics-card">
                {/* Mini analytics card for each camera row */}
                <div className="camera-row-metrics-header">
                  <span>Timestamp vs Velocity / bg_diff</span>
                  <div className="metrics-legend">
                    <span><span className="legend-dot velocity" /> Velocity</span>
                    <span><span className="legend-dot bgdiff" /> bg_diff</span>
                  </div>
                </div>

                <div className="metrics-bar-chart" role="img" aria-label="Velocity and bg_diff bar plot by timestamp">
                  {chartMetrics.map((metric) => {
                    if (metric.xPercent === null) {
                      return null;
                    }
                    const velocityHeight = Math.max(2, (metric.velocity / chartMaxVelocity) * 100);
                    const bgDiffHeight = Math.max(2, (metric.bgDiff / chartMaxBgDiff) * 100);
                    const markerHeight = Math.max(velocityHeight, bgDiffHeight);
                    return (
                      <React.Fragment key={metric.id}>
                        <div
                          className="metrics-bar-group"
                          style={{ left: `${metric.xPercent}%` }}
                        >
                          <span
                            className="metrics-bar velocity"
                            style={{ height: `${velocityHeight}%` }}
                            title={`Velocity: ${metric.velocity.toFixed(3)}`}
                          />
                          <span
                            className="metrics-bar bgdiff"
                            style={{ height: `${bgDiffHeight}%` }}
                            title={`bg_diff: ${metric.bgDiff.toFixed(0)}`}
                          />
                        </div>

                        <button
                          type="button"
                          className={`motion-point-btn${highlightedRecordingId === metric.id ? ' active' : ''}`}
                          style={{
                            left: `${metric.xPercent}%`,
                            bottom: `${Math.min(96, markerHeight + 2)}%`,
                          }}
                          onClick={() => handleTimelinePointClick(row.cameraId, metric.id)}
                          title={`${metric.timeLabel} | vel: ${metric.velocity.toFixed(3)} | bg_diff: ${metric.bgDiff.toFixed(0)}`}
                        >
                          <span className="point-dot" />
                        </button>
                      </React.Fragment>
                    );
                  })}
                </div>
                <div className="metrics-time-axis">
                  {axisTicks.map((tick, index) => (
                    <div key={`${row.cameraId}-tick-${index}`} className="metrics-axis-tick" style={{ left: `${tick.xPercent}%` }}>
                      <span className="metrics-axis-mark" />
                      {tick.showLabel && <span className="metrics-axis-label">{tick.label}</span>}
                    </div>
                  ))}
                </div>
              </div>

              <div className="reels-carousel-wrapper">
                <button
                  className="carousel-nav-button prev"
                  onClick={() => scrollRow(row.cameraId, -1)}
                  title="Scroll left"
                >
                  <ChevronLeft size={32} />
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
                  {row.recordings.map((recording, recordingIndex) => {
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
                        <div className="reel-timestamp">
                          <span className="reel-date">{timestampParts.date}</span>
                          {timestampParts.time && <span className="reel-time">{timestampParts.time}</span>}
                        </div>

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
                              src={shouldLoadVideo ? api.getRecordingStreamUrl(recording.id, 'play') : undefined}
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

                        <div className="reel-info">
                          {/* Per-card playback controls */}
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
                                setHoveredId(recording.id);
                                handleSeekStart(recording.id, false);
                              }}
                              onMouseUp={() => handleSeekEnd(recording.id)}
                              onTouchStart={() => {
                                setHoveredId(recording.id);
                                handleSeekStart(recording.id, false);
                              }}
                              onTouchEnd={() => handleSeekEnd(recording.id)}
                              onChange={(event) => handleSeekChange(recording.id, event.target.value)}
                              className="reel-progress-slider"
                              style={{ '--progress': `${playbackProgress}%` }}
                            />
                          </div>
                          <div className="reel-meta-row">
                            <RecordingMetaInfo durationText={formatDuration(durationValue)} velValue={velValue} diffValue={diffValue} loudnessValue={loudnessValue} />
                            <div className="reel-card-actions" onClick={(event) => event.stopPropagation()}>
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

                          {/* Label + note row */}
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
                  })}
                </div>

                <button
                  className="carousel-nav-button next"
                  onClick={() => scrollRow(row.cameraId, 1)}
                  title="Scroll right"
                >
                  <ChevronRight size={32} />
                </button>
              </div>
            </div>
          );
        })}
      </div>

      {expandedRecording && (
        // Expanded modal view for focused reel playback.
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
                        </div>
                        <RecordingMetaInfo durationText={formatDuration(durationValue)} velValue={velValue} diffValue={diffValue} loudnessValue={loudnessValue} />
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
