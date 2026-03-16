import React, { useEffect, useState, useMemo, useCallback } from 'react';
import { Play, Download, Trash2, Clock, HardDrive, Camera, Search, Archive, Calendar, FolderOpen, ChevronDown, ChevronRight } from 'lucide-react';
import { toast } from 'react-hot-toast';
import { api } from '../api';

const RecordingList = ({ recordings, setRecordings, cameras }) => {
  const [selectedRecording, setSelectedRecording] = useState(null);
  const [filterCamera, setFilterCamera] = useState('');
  const [filterStatus, setFilterStatus] = useState('');
  const [sortBy, setSortBy] = useState('created_at');
  const [sortOrder, setSortOrder] = useState('desc');
  const [searchTerm, setSearchTerm] = useState('');
  const [storageInfo, setStorageInfo] = useState(null);
  const [playbackMode, setPlaybackMode] = useState(api.getRecordingPlaybackMode());
  
  // Archive state from EventView
  const [archivePath, setArchivePath] = useState('');
  const [archiveList, setArchiveList] = useState([]);
  const [archiveBusy, setArchiveBusy] = useState(false);
  const [archivePanelOpen, setArchivePanelOpen] = useState(true);
  const [archiveFilters, setArchiveFilters] = useState(() => { 
    const today = new Date().toISOString().slice(0, 10); 
    const yesterday = new Date(Date.now() - 86400000).toISOString().slice(0, 10); 
    return { 
      date_from: yesterday, 
      date_to: today, 
      min_vel: '', 
      min_diff: '', 
      min_duration: '', 
      label_filter: '',
      camera_filter: '' // Added camera selection
    }; 
  });
  const [archiveExportResult, setArchiveExportResult] = useState(null);
  const [deleteAfterArchive, setDeleteAfterArchive] = useState(true);
  const [excludeMode, setExcludeMode] = useState(true);
  const [cleanOverlay, setCleanOverlay] = useState(true);
  const [minFreeStorageGiB, setMinFreeStorageGiB] = useState(1);
  const [autoArchiveDays, setAutoArchiveDays] = useState(7);
  const [storageSaving, setStorageSaving] = useState(false);
  const [collapsedCameras, setCollapsedCameras] = useState({});
  const [collapsedDates, setCollapsedDates] = useState({});
  const [bulkDeleting, setBulkDeleting] = useState(false);

  const loadStorageInfo = async () => {
    try {
      const response = await api.getRecordingStorageInfo();
      setStorageInfo(response.data);
      if (response.data?.min_free_bytes) {
        setMinFreeStorageGiB(Number((response.data.min_free_bytes / (1024 ** 3)).toFixed(2)));
      }
    } catch (error) {
      console.error('Failed to load recording storage info:', error);
    }
    // Load auto_archive_days from system settings
    try {
      const settingsRes = await api.getSystemSettings();
      if (settingsRes.data?.auto_archive_days != null) {
        setAutoArchiveDays(Number(settingsRes.data.auto_archive_days));
      }
    } catch (error) {
      console.error('Failed to load system settings:', error);
    }
  };

  const handleSaveMinFreeStorage = async (gib) => {
    setStorageSaving(true);
    try {
      const res = await api.getSystemSettings();
      const current = res.data || {};
      const payload = { ...current, min_free_storage_bytes: Math.round(gib * 1024 ** 3) };
      delete payload.total_memory_bytes;
      delete payload.active_preset;
      await api.updateSystemSettings(payload);
      setMinFreeStorageGiB(gib);
      toast.success(`Storage threshold updated to ${gib} GiB`);
      loadStorageInfo();
    } catch (err) {
      toast.error('Failed to update storage threshold: ' + (err?.response?.data?.detail || err.message));
    } finally {
      setStorageSaving(false);
    }
  };

  const handleSaveAutoArchiveDays = async (days) => {
    setStorageSaving(true);
    try {
      const res = await api.getSystemSettings();
      const current = res.data || {};
      const payload = { ...current, auto_archive_days: days };
      delete payload.total_memory_bytes;
      delete payload.active_preset;
      await api.updateSystemSettings(payload);
      setAutoArchiveDays(days);
      toast.success(days > 0 ? `Auto-archive set to ${days} days` : 'Auto-archive disabled');
    } catch (err) {
      toast.error('Failed to update auto-archive: ' + (err?.response?.data?.detail || err.message));
    } finally {
      setStorageSaving(false);
    }
  };

  const loadArchives = async () => {
    try {
      const response = await api.get('/recordings/archive/list');
      setArchiveList(response.data.archives || []);
    } catch (error) {
      console.error('Failed to load archives:', error);
    }
  };

  // Fetch recordings from API, optionally filtered by camera
  const fetchRecordings = useCallback(async (cameraId) => {
    try {
      const res = await api.getRecordings(cameraId || null);
      setRecordings(res.data);
    } catch (err) {
      console.error('Failed to fetch recordings:', err);
    }
  }, [setRecordings]);

  useEffect(() => {
    fetchRecordings(filterCamera);
  }, [filterCamera, fetchRecordings]);

  useEffect(() => {
    loadStorageInfo();
    loadArchives();
  }, [recordings.length]);

  const handleDeleteRecording = async (recordingId) => {
    if (!window.confirm('Are you sure you want to delete this recording?')) {
      return;
    }
    
    try {
      await api.deleteRecording(recordingId);
      setRecordings(prev => prev.filter(r => r.id !== recordingId));
      toast.success('Recording deleted successfully');
    } catch (error) {
      toast.error('Failed to delete recording: ' + (error.response?.data?.detail || error.message));
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

  const handleBulkDelete = async (recordingIds, label) => {
    if (!recordingIds.length) return;
    if (!window.confirm(`Delete ${recordingIds.length} recording(s) from ${label}?\n\nThis action cannot be undone.`)) {
      return;
    }
    setBulkDeleting(true);
    let deleted = 0;
    for (const id of recordingIds) {
      try {
        await api.deleteRecording(id);
        deleted++;
      } catch (err) {
        console.error(`Failed to delete ${id}:`, err);
      }
    }
    setRecordings(prev => prev.filter(r => !recordingIds.includes(r.id)));
    toast.success(`Deleted ${deleted}/${recordingIds.length} recording(s) from ${label}`);
    setBulkDeleting(false);
  };

  // ===== Archive handlers (adapted from EventView) =====
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
      if (archiveFilters.camera_filter) filters.camera_filter = [archiveFilters.camera_filter]; // Added camera filter
      filters.exclude_mode = excludeMode;
      if (deleteAfterArchive) filters.delete_after = true;
      if (cleanOverlay) filters.clean_up_extensions = ['.overlay.mp4'];
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

  const formatBytes = (bytes) => {
    if (!bytes) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
  };

  const formatDuration = (seconds) => {
    if (!seconds) return '0s';
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const secs = Math.floor(seconds % 60);
    
    if (hours > 0) {
      return `${hours}:${minutes.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
    } else {
      return `${minutes}:${secs.toString().padStart(2, '0')}`;
    }
  };

  const formatDate = (dateString) => {
    return new Date(dateString).toLocaleString();
  };

  // Filter and sort recordings (camera filtering done server-side)
  const filteredRecordings = recordings
    .filter(recording => {
      const camera = cameras.find(c => c.id === recording.camera_id);
      const cameraName = camera?.name || 'Unknown Camera';
      
      const matchesStatus = !filterStatus || recording.status === filterStatus;
      const matchesSearch = !searchTerm || 
        cameraName.toLowerCase().includes(searchTerm.toLowerCase()) ||
        recording.filename.toLowerCase().includes(searchTerm.toLowerCase());
      
      return matchesStatus && matchesSearch;
    })
    .sort((a, b) => {
      let aValue, bValue;
      
      switch (sortBy) {
        case 'camera':
          const aCameraName = cameras.find(c => c.id === a.camera_id)?.name || 'Unknown';
          const bCameraName = cameras.find(c => c.id === b.camera_id)?.name || 'Unknown';
          aValue = aCameraName;
          bValue = bCameraName;
          break;
        case 'duration':
          aValue = a.duration || 0;
          bValue = b.duration || 0;
          break;
        case 'file_size':
          aValue = a.file_size || 0;
          bValue = b.file_size || 0;
          break;
        default:
          aValue = new Date(a[sortBy]);
          bValue = new Date(b[sortBy]);
      }
      
      if (sortOrder === 'asc') {
        return aValue > bValue ? 1 : -1;
      } else {
        return aValue < bValue ? 1 : -1;
      }
    });

  // Build nested structure: camera -> date -> recordings
  const nestedRecordings = useMemo(() => {
    const map = {};
    for (const rec of filteredRecordings) {
      const camId = rec.camera_id || 'unknown';
      const dt = rec.created_at || rec.start_time || '';
      const dateKey = dt ? dt.slice(0, 10) : 'unknown';
      if (!map[camId]) map[camId] = {};
      if (!map[camId][dateKey]) map[camId][dateKey] = [];
      map[camId][dateKey].push(rec);
    }
    // Sort dates descending within each camera
    const result = Object.entries(map).map(([camId, dates]) => ({
      camId,
      camName: cameras.find(c => c.id === camId)?.name || camId,
      dates: Object.entries(dates)
        .sort(([a], [b]) => b.localeCompare(a))
        .map(([dateKey, recs]) => ({ dateKey, recordings: recs })),
      totalCount: Object.values(dates).reduce((s, arr) => s + arr.length, 0),
    }));
    result.sort((a, b) => a.camName.localeCompare(b.camName));
    return result;
  }, [filteredRecordings, cameras]);

  const toggleCamera = (camId) =>
    setCollapsedCameras(prev => ({ ...prev, [camId]: !prev[camId] }));
  // Dates default to collapsed (recordings hidden); toggling sets to false (expanded)
  const isDateCollapsed = (key) => collapsedDates[key] !== false;
  const toggleDate = (key) =>
    setCollapsedDates(prev => ({ ...prev, [key]: prev[key] === false ? true : false }));

  const VideoPlayerModal = ({ recording, onClose }) => {
    const camera = cameras.find(c => c.id === recording.camera_id);
    
    return (
      <div className="modal-overlay" onClick={(e) => e.target === e.currentTarget && onClose()}>
        <div className="modal" style={{ maxWidth: '90vw', maxHeight: '90vh' }}>
          <div className="modal-header">
            <h3 className="modal-title">
              {camera?.name || 'Unknown Camera'} - {recording.filename}
            </h3>
            <button className="modal-close" onClick={onClose}>×</button>
          </div>
          
          <div className="modal-body" style={{ padding: 0 }}>
            <div className="video-container" style={{ height: '60vh' }}>
              {playbackMode === 'stream' ? (
                <img
                  src={api.appendQueryParams(api.getRecordingStreamUrl(recording.id, 'stream'), {
                    ts: Date.now(),
                  })}
                  alt={`Recording ${recording.filename}`}
                  className="video-stream"
                  style={{
                    width: '100%',
                    height: '100%',
                    objectFit: 'contain',
                    backgroundColor: '#000'
                  }}
                />
              ) : (
                <video
                  src={api.getRecordingStreamUrl(recording.id, 'play')}
                  poster={api.getRecordingThumbnailUrl(recording.id)}
                  className="video-stream"
                  style={{
                    width: '100%',
                    height: '100%',
                    objectFit: 'contain',
                    backgroundColor: '#000'
                  }}
                  controls
                  autoPlay
                  playsInline
                />
              )}
            </div>
          </div>
          
          <div className="modal-footer">
            <div style={{ display: 'flex', gap: '12px', alignItems: 'center', flex: 1 }}>
              <span className="btn btn-secondary" style={{ cursor: 'default' }}>
                <Clock size={14} /> {formatDuration(recording.duration)}
              </span>
              <span className="btn btn-secondary" style={{ cursor: 'default' }}>
                <HardDrive size={14} /> {formatBytes(recording.file_size)}
              </span>
            </div>
            <button 
              className="btn btn-primary"
              onClick={() => handleDownloadRecording(recording.id)}
            >
              <Download size={14} />
              Download
            </button>
            <button className="btn btn-secondary" onClick={onClose}>
              Close
            </button>
          </div>
        </div>
      </div>
    );
  };

  return (
    <div>
      <div className="page-header">
        <h1 className="page-title">Recordings</h1>
        <p className="page-subtitle">Browse and manage your recorded videos</p>
      </div>

      {/* Archive Panel */}
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

              {/* Filter row 1 — dates and camera */}
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
                <label style={{ fontSize: 12, color: '#546e7a', minWidth: 50 }}>Camera</label>
                <select
                  className="form-control form-select"
                  value={archiveFilters.camera_filter}
                  onChange={(e) => setArchiveFilters((f) => ({ ...f, camera_filter: e.target.value }))}
                  style={{ width: 160 }}
                >
                  <option value="">All Cameras</option>
                  {cameras.map(camera => (
                    <option key={camera.id} value={camera.id}>{camera.name || camera.id}</option>
                  ))}
                </select>
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
                  style={{ width: 120 }}
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
                  style={{ width: 120 }}
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
                  style={{ width: 120 }}
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
                <label style={{ display: 'inline-flex', alignItems: 'center', gap: 6, cursor: 'pointer', userSelect: 'none', fontSize: 13, color: cleanOverlay ? '#00897b' : '#546e7a', fontWeight: cleanOverlay ? 600 : 400 }}>
                  <input
                    type="checkbox"
                    checked={cleanOverlay}
                    onChange={(e) => setCleanOverlay(e.target.checked)}
                    style={{ accentColor: '#00897b', width: 15, height: 15, cursor: 'pointer' }}
                  />
                  Clean .overlay.mp4
                </label>
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

            {/* Storage management */}
            <div style={{ background: 'rgba(255,255,255,0.7)', borderRadius: 10, padding: '12px 14px', border: '1px solid rgba(0,150,136,0.15)' }}>
              <div style={{ fontWeight: 600, fontSize: 13, color: '#004d40', marginBottom: 10 }}>Storage management</div>
              <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center', marginBottom: 10 }}>
                <label style={{ fontSize: 13, color: '#546e7a' }}>Delete oldest recording when free storage &lt;</label>
                <select
                  className="form-control form-select"
                  value={minFreeStorageGiB}
                  onChange={(e) => handleSaveMinFreeStorage(Number(e.target.value))}
                  disabled={storageSaving}
                  style={{ width: 100 }}
                >
                  <option value={0}>Off</option>
                  <option value={0.5}>0.5 GiB</option>
                  <option value={1}>1 GiB</option>
                  <option value={2}>2 GiB</option>
                  <option value={4}>4 GiB</option>
                  <option value={8}>8 GiB</option>
                  <option value={16}>16 GiB</option>
                  <option value={32}>32 GiB</option>
                </select>
                {storageInfo && (
                  <span style={{ fontSize: 12, color: '#78909c' }}>
                    Free: {(storageInfo.free_bytes / (1024 ** 3)).toFixed(1)} GiB / {(storageInfo.total_bytes / (1024 ** 3)).toFixed(1)} GiB
                  </span>
                )}
              </div>
              <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center' }}>
                <label style={{ fontSize: 13, color: '#546e7a' }}>Auto-archive recordings older than</label>
                <select
                  className="form-control form-select"
                  value={autoArchiveDays}
                  onChange={(e) => handleSaveAutoArchiveDays(Number(e.target.value))}
                  disabled={storageSaving}
                  style={{ width: 120 }}
                >
                  <option value={0}>Off</option>
                  <option value={3}>3 days</option>
                  <option value={5}>5 days</option>
                  <option value={7}>7 days</option>
                  <option value={14}>14 days</option>
                  <option value={30}>30 days</option>
                  <option value={60}>60 days</option>
                  <option value={90}>90 days</option>
                </select>
                <span style={{ fontSize: 11, color: '#90a4ae' }}>
                  Archives the oldest date when recordings span more than {autoArchiveDays || '—'} distinct days
                </span>
              </div>
            </div>

            {/* Load archive section */}
            <div style={{ background: 'rgba(255,255,255,0.7)', borderRadius: 10, padding: '12px 14px', border: '1px solid rgba(0,150,136,0.15)' }}>
              <div style={{ fontWeight: 600, fontSize: 13, color: '#004d40', marginBottom: 8 }}>Load an archive into the recording view</div>
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

      <div className="content-section">
        <div className="section-header">
          <h2 className="section-title">Video Recordings ({filteredRecordings.length})</h2>
          <div className="section-subtitle" style={{ marginTop: '8px' }}>
            Available Storage: {storageInfo ? formatBytes(storageInfo.free_bytes) : 'Loading...'}
          </div>
          <div style={{ marginTop: '12px' }}>
            <label className="form-label" style={{ marginRight: '8px' }}>Playback Mode</label>
            <select
              className="form-control form-select"
              value={playbackMode}
              onChange={(e) => {
                const mode = e.target.value === 'stream' ? 'stream' : 'play';
                setPlaybackMode(mode);
                api.setRecordingPlaybackMode(mode);
              }}
              style={{ width: '180px', display: 'inline-block' }}
            >
              <option value="play">File Playback</option>
              <option value="stream">Legacy Stream (Video only)</option>
            </select>
          </div>
        </div>
        
        {/* Filters */}
        <div className="card" style={{ marginBottom: '24px' }}>
          <div className="grid grid-4">
            <div className="form-group" style={{ marginBottom: 0 }}>
              <label className="form-label">Search</label>
              <div style={{ position: 'relative' }}>
                <input 
                  type="text"
                  className="form-control"
                  placeholder="Search recordings..."
                  value={searchTerm}
                  onChange={(e) => setSearchTerm(e.target.value)}
                  style={{ paddingLeft: '36px' }}
                />
                <Search 
                  size={16} 
                  style={{ 
                    position: 'absolute', 
                    left: '12px', 
                    top: '50%', 
                    transform: 'translateY(-50%)',
                    color: '#b0b0b0'
                  }}
                />
              </div>
            </div>
            
            <div className="form-group" style={{ marginBottom: 0 }}>
              <label className="form-label">Camera</label>
              <select 
                className="form-control form-select"
                value={filterCamera}
                onChange={(e) => setFilterCamera(e.target.value)}
              >
                <option value="">All Cameras</option>
                {cameras.map(camera => (
                  <option key={camera.id} value={camera.id}>
                    {camera.name}
                  </option>
                ))}
              </select>
            </div>
            
            <div className="form-group" style={{ marginBottom: 0 }}>
              <label className="form-label">Status</label>
              <select 
                className="form-control form-select"
                value={filterStatus}
                onChange={(e) => setFilterStatus(e.target.value)}
              >
                <option value="">All Status</option>
                <option value="recording">Recording</option>
                <option value="completed">Completed</option>
                <option value="failed">Failed</option>
              </select>
            </div>
            
            <div className="form-group" style={{ marginBottom: 0 }}>
              <label className="form-label">Sort By</label>
              <div style={{ display: 'flex', gap: '4px' }}>
                <select 
                  className="form-control form-select"
                  value={sortBy}
                  onChange={(e) => setSortBy(e.target.value)}
                >
                  <option value="created_at">Date Created</option>
                  <option value="camera">Camera</option>
                  <option value="duration">Duration</option>
                  <option value="file_size">File Size</option>
                </select>
                <button 
                  className="btn btn-secondary"
                  onClick={() => setSortOrder(sortOrder === 'asc' ? 'desc' : 'asc')}
                  style={{ minWidth: '60px' }}
                >
                  {sortOrder === 'asc' ? '↑' : '↓'}
                </button>
              </div>
            </div>
          </div>
        </div>

        {/* Recording List — nested by Camera → Date → Recording */}
        <div className="recording-list">
          {nestedRecordings.length === 0 ? (
            <div className="recording-item">
              <div className="recording-info">
                <div className="recording-name">
                  {recordings.length === 0 ? 'No recordings found' : 'No recordings match your filters'}
                </div>
                <div className="recording-details">
                  <span>
                    {recordings.length === 0 
                      ? 'Start recording from cameras to see them here'
                      : 'Try adjusting your search filters'
                    }
                  </span>
                </div>
              </div>
            </div>
          ) : (
            nestedRecordings.map(({ camId, camName, dates, totalCount }) => {
              const camCollapsed = collapsedCameras[camId];
              const allCamRecIds = dates.flatMap(d => d.recordings.map(r => r.id));
              return (
                <div key={camId} style={{ marginBottom: 12 }}>
                  {/* Camera header */}
                  <div
                    style={{
                      display: 'flex', alignItems: 'center', gap: 8, padding: '8px 12px',
                      background: 'rgba(0,150,136,0.08)', borderRadius: 10,
                      border: '1px solid rgba(0,150,136,0.18)', cursor: 'pointer', userSelect: 'none',
                    }}
                    onClick={() => toggleCamera(camId)}
                  >
                    {camCollapsed ? <ChevronRight size={16} /> : <ChevronDown size={16} />}
                    <Camera size={16} style={{ color: '#00897b' }} />
                    <span style={{ fontWeight: 700, fontSize: 14, color: '#004d40' }}>{camName}</span>
                    <span style={{ fontSize: 12, color: '#546e7a', marginLeft: 4 }}>({totalCount})</span>
                    <button
                      className="btn btn-danger"
                      style={{ marginLeft: 'auto', padding: '2px 10px', fontSize: 11 }}
                      disabled={bulkDeleting}
                      onClick={(e) => { e.stopPropagation(); handleBulkDelete(allCamRecIds, camName); }}
                    >
                      <Trash2 size={12} /> Delete all
                    </button>
                  </div>

                  {!camCollapsed && dates.map(({ dateKey, recordings: dateRecs }) => {
                    const dateCollapseKey = `${camId}__${dateKey}`;
                    const dateCollapsed = isDateCollapsed(dateCollapseKey);
                    const dateRecIds = dateRecs.map(r => r.id);
                    return (
                      <div key={dateCollapseKey} style={{ marginLeft: 20, marginTop: 6 }}>
                        {/* Date header */}
                        <div
                          style={{
                            display: 'flex', alignItems: 'center', gap: 8, padding: '5px 10px',
                            background: 'rgba(0,150,136,0.04)', borderRadius: 8,
                            border: '1px solid rgba(0,150,136,0.10)', cursor: 'pointer', userSelect: 'none',
                          }}
                          onClick={() => toggleDate(dateCollapseKey)}
                        >
                          {dateCollapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
                          <Calendar size={14} style={{ color: '#00897b' }} />
                          <span style={{ fontWeight: 600, fontSize: 13, color: '#37474f' }}>{dateKey}</span>
                          <span style={{ fontSize: 12, color: '#78909c' }}>({dateRecs.length})</span>
                          <button
                            className="btn btn-danger"
                            style={{ marginLeft: 'auto', padding: '2px 8px', fontSize: 11 }}
                            disabled={bulkDeleting}
                            onClick={(e) => { e.stopPropagation(); handleBulkDelete(dateRecIds, `${camName} / ${dateKey}`); }}
                          >
                            <Trash2 size={11} /> Delete day
                          </button>
                        </div>

                        {!dateCollapsed && dateRecs.map(recording => (
                          <div key={recording.id} className="recording-item" style={{ marginLeft: 16, marginTop: 2, padding: '3px 8px', display: 'flex', alignItems: 'center', gap: 8 }}>
                            <div style={{ flex: 1, minWidth: 0 }}>
                              <div style={{ fontSize: 12, fontWeight: 600, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                                {recording.filename}
                              </div>
                              <div style={{ display: 'flex', gap: 8, fontSize: 11, color: '#78909c', flexWrap: 'wrap' }}>
                                <span><Clock size={10} /> {formatDate(recording.created_at)}</span>
                                <span><Clock size={10} /> {formatDuration(recording.duration)}</span>
                                <span><HardDrive size={10} /> {formatBytes(recording.file_size)}</span>
                                <span className={`status status-${recording.status}`} style={{ fontSize: 10 }}>
                                  {recording.status}
                                </span>
                              </div>
                            </div>
                            <div style={{ display: 'flex', gap: 4, flexShrink: 0 }}>
                              {recording.status === 'completed' && (
                                <>
                                  <button className="btn btn-primary" style={{ padding: '2px 6px', fontSize: 11, lineHeight: 1 }} onClick={() => setSelectedRecording(recording)} title="Play">
                                    <Play size={12} />
                                  </button>
                                  <button className="btn btn-secondary" style={{ padding: '2px 6px', fontSize: 11, lineHeight: 1 }} onClick={() => handleDownloadRecording(recording.id)} title="Download">
                                    <Download size={12} />
                                  </button>
                                </>
                              )}
                              <button className="btn btn-danger" style={{ padding: '2px 6px', fontSize: 11, lineHeight: 1 }} onClick={() => handleDeleteRecording(recording.id)} title="Delete">
                                <Trash2 size={12} />
                              </button>
                            </div>
                          </div>
                        ))}
                      </div>
                    );
                  })}
                </div>
              );
            })
          )}
        </div>
      </div>

      {selectedRecording && (
        <VideoPlayerModal 
          recording={selectedRecording}
          onClose={() => setSelectedRecording(null)}
        />
      )}
    </div>
  );
};

export default RecordingList;