import React, { useEffect, useRef, useState } from 'react';
import { Plus, Edit, Trash2, Play, Square, Settings, Power, PowerOff, Mic, MicOff, RotateCw, FolderOpen } from 'lucide-react';
import { toast } from 'react-hot-toast';
import { api } from '../../api';
import { DEFAULT_CAMERA_FORM, COMPACT_BUTTON_STYLE, CameraForm } from './CameraForm';
import {
  AUDIO_TOGGLE_STYLE, SENSITIVITY_LEVEL, DEFAULT_SENSITIVITY,
  CameraAudioPanel, CameraVideoPanel,
} from './CameraViewPanels';
import './CameraList.css';

const CameraList = ({ cameras, setCameras }) => {
  // -----------------------------
  // Page-level state
  // -----------------------------
  const [showAddModal, setShowAddModal] = useState(false);
  const [editingCamera, setEditingCamera] = useState(null);
  const [newCamera, setNewCamera] = useState({ ...DEFAULT_CAMERA_FORM });

  // Per-camera UI settings for supporting screen
  const [cameraSettings, setCameraSettings] = useState({}); // { [id]: { sensitivity: number(0..5), supportEnabled: boolean } }
  const [hlsFailedByCamera, setHlsFailedByCamera] = useState({});
  const [liveStreamMode, setLiveStreamMode] = useState('mjpeg');
  const [isTogglingLiveMode, setIsTogglingLiveMode] = useState(false);
  const [rtspUnifiedCaptureEnabled, setRtspUnifiedCaptureEnabled] = useState(false);
  const [isTogglingRtspUnifiedCapture, setIsTogglingRtspUnifiedCapture] = useState(false);

  // File browser state for recorded camera type
  const [showFileBrowser, setShowFileBrowser] = useState(false);
  const [fileBrowserItems, setFileBrowserItems] = useState([]);
  const [fileBrowserPath, setFileBrowserPath] = useState(null);
  const [fileBrowserParent, setFileBrowserParent] = useState(null);
  const [fileBrowserFormSetter, setFileBrowserFormSetter] = useState(null);

  // Stream health monitoring state
  const [streamHealth, setStreamHealth] = useState({}); // { [camera_id]: health_data }
  const [refreshingStreams, setRefreshingStreams] = useState({}); // { [camera_id]: boolean }
  const [restartingCameras, setRestartingCameras] = useState({}); // { [camera_id]: boolean }
  const [startingCameras, setStartingCameras] = useState({}); // { [camera_id]: boolean }

  const isLiveHlsMode = liveStreamMode === 'hls';
  const isWsMode = liveStreamMode === 'ws';

  const handleToggleLiveStreamMode = async () => {
    const modeOrder = ['mjpeg', 'ws', 'hls'];
    const currentIdx = modeOrder.indexOf(liveStreamMode);
    const targetMode = modeOrder[(currentIdx + 1) % modeOrder.length];
    setIsTogglingLiveMode(true);
    try {
      const mode = await api.setLiveStreamMode(targetMode);
      setLiveStreamMode(mode);
      if (mode === 'hls') {
        setHlsFailedByCamera({});
      }
      toast.success(`Live mode switched to ${mode.toUpperCase()}`);
    } catch (error) {
      toast.error('Failed to switch live mode: ' + (error.response?.data?.detail || error.message));
    } finally {
      setIsTogglingLiveMode(false);
    }
  };

  const handleToggleRtspUnifiedCapture = async () => {
    const target = !rtspUnifiedCaptureEnabled;
    setIsTogglingRtspUnifiedCapture(true);
    try {
      const response = await api.updateSystemSettings({ rtsp_unified_demux_enabled: target });
      const enabled = Boolean(response?.data?.rtsp_unified_demux_enabled);
      setRtspUnifiedCaptureEnabled(enabled);
      toast.success(`RTSP unified capture ${enabled ? 'enabled' : 'disabled'}`);
    } catch (error) {
      toast.error('Failed to update RTSP unified capture: ' + (error.response?.data?.detail || error.message));
    } finally {
      setIsTogglingRtspUnifiedCapture(false);
    }
  };

  useEffect(() => {
    let cancelled = false;
    const loadLiveMode = async () => {
      try {
        const mode = await api.getLiveStreamMode();
        if (!cancelled) {
          setLiveStreamMode(mode);
        }
      } catch (error) {
        console.error('Failed to load live stream mode from backend, defaulting to mjpeg:', error);
        if (!cancelled) {
          setLiveStreamMode('mjpeg');
        }
      }
    };

    loadLiveMode();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    const loadUnifiedCaptureSetting = async () => {
      try {
        const response = await api.getSystemSettings();
        if (cancelled) {
          return;
        }
        setRtspUnifiedCaptureEnabled(Boolean(response?.data?.rtsp_unified_demux_enabled));
      } catch {
        if (!cancelled) {
          setRtspUnifiedCaptureEnabled(false);
        }
      }
    };

    loadUnifiedCaptureSetting();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;

    const syncCameraSensitivities = async () => {
      if (!Array.isArray(cameras) || cameras.length === 0) {
        if (!cancelled) {
          setCameraSettings((prev) => {
            const next = {};
            return Object.keys(next).length === Object.keys(prev).length ? prev : next;
          });
        }
        return;
      }

      const sensitivityEntries = await Promise.all(
        cameras.map(async (camera) => {
          try {
            const response = await api.getCameraSensitivity(camera.id);
            const value = Number(response?.data?.sensitivity);
            const safe = Math.max(0, Math.min(SENSITIVITY_LEVEL, Number.isFinite(value) ? value : DEFAULT_SENSITIVITY));
            return [camera.id, safe];
          } catch {
            return [camera.id, DEFAULT_SENSITIVITY];
          }
        })
      );

      if (cancelled) {
        return;
      }

      setCameraSettings((prev) => {
        const next = {};
        sensitivityEntries.forEach(([cameraId, sensitivity]) => {
          const previous = prev[cameraId] || {};
          next[cameraId] = {
            supportEnabled: previous.supportEnabled ?? false,
            sensitivity,
          };
        });
        return next;
      });
    };

    syncCameraSensitivities();

    return () => {
      cancelled = true;
    };
  }, [cameras]);

  // Stream health monitoring with backoff on failure
  useEffect(() => {
    let cancelled = false;
    let timeoutId = null;
    let consecutiveFailures = 0;
    const BASE_INTERVAL = 30000;
    const MAX_INTERVAL = 120000;

    const scheduleNext = () => {
      const delay = Math.min(BASE_INTERVAL * Math.pow(2, consecutiveFailures), MAX_INTERVAL);
      timeoutId = setTimeout(checkStreamHealth, delay);
    };

    const checkStreamHealth = async () => {
      try {
        const response = await api.getAllCamerasStreamHealth();
        if (!cancelled) {
          setStreamHealth(response.data.cameras || {});
          consecutiveFailures = 0;
        }
      } catch (error) {
        console.warn('Failed to check stream health:', error);
        if (!cancelled) consecutiveFailures++;
      }
      if (!cancelled) scheduleNext();
    };

    // Initial health check
    checkStreamHealth();

    return () => {
      cancelled = true;
      if (timeoutId) clearTimeout(timeoutId);
    };
  }, []);

  // Refresh stream functionality
  const handleRefreshStream = async (cameraId) => {
    try {
      setRefreshingStreams(prev => ({ ...prev, [cameraId]: true }));
      
      // Force stream refresh by requesting new stream URLs with cache busting
      const timestamp = Date.now();
      
      // Find and refresh video stream elements
      const videoElements = document.querySelectorAll(`img[src*="${cameraId}"]`);
      videoElements.forEach(element => {
        const currentSrc = element.src;
        const baseUrl = currentSrc.split('?')[0];
        element.src = `${baseUrl}?refresh=${timestamp}`;
      });

      // Find and refresh audio stream elements
      const audioElements = document.querySelectorAll(`audio source[src*="${cameraId}"]`);
      audioElements.forEach(source => {
        const audio = source.parentElement;
        const currentSrc = source.src;
        const baseUrl = currentSrc.split('?')[0];
        source.src = `${baseUrl}?refresh=${timestamp}`;
        audio.load(); // Force reload of audio element
      });

      toast.success(`Refreshed streams for camera ${cameraId}`);
      
      // Check stream health after refresh
      setTimeout(async () => {
        try {
          const response = await api.getCameraStreamHealth(cameraId);
          setStreamHealth(prev => ({ ...prev, [cameraId]: response.data }));
        } catch (error) {
          console.warn('Failed to check stream health after refresh:', error);
        }
      }, 2000);
      
    } catch (error) {
      toast.error(`Failed to refresh streams: ${error.message}`);
    } finally {
      setRefreshingStreams(prev => ({ ...prev, [cameraId]: false }));
    }
  };

  const handleRestartCamera = async (cameraId) => {
    try {
      setRestartingCameras(prev => ({ ...prev, [cameraId]: true }));
      
      await api.restartCamera(cameraId);
      toast.success(`Camera ${cameraId} restarted successfully`);
      
      // Refresh cameras list after restart
      setTimeout(async () => {
        try {
          const response = await api.getCameras();
          setCameras(response.data);
        } catch (error) {
          console.warn('Failed to refresh cameras after restart:', error);
        }
      }, 2000);
      
    } catch (error) {
      toast.error(`Failed to restart camera: ${error.message}`);
    } finally {
      setRestartingCameras(prev => ({ ...prev, [cameraId]: false }));
    }
  };

  // -----------------------------
  // Subcomponents
  // -----------------------------

  // Stream Health Indicator Component
  const StreamHealthIndicator = ({ cameraId, health }) => {
    if (!health) {
      return (
        <div style={{ fontSize: '8px', color: '#64748b' }}>
          Health: --
        </div>
      );
    }

    const { lag_stats, health_issues, needs_manual_refresh } = health;
    const hasIssues = health_issues.video_producer_frozen || health_issues.audio_producer_frozen || health_issues.recording_frozen;
    
    const getStatusColor = () => {
      if (needs_manual_refresh) return '#ef4444'; // red
      if (hasIssues) return '#f59e0b'; // amber
      return '#10b981'; // green
    };

    const getStatusText = () => {
      if (needs_manual_refresh) return 'Manual refresh needed';
      if (hasIssues) return 'Stream issues detected';
      return 'Healthy';
    };

    return (
      <div style={{ 
        fontSize: '8px', 
        display: 'flex',
        alignItems: 'center',
        flex: 1
      }}>
        <div style={{ 
          color: getStatusColor(),
          fontWeight: '500',
          fontSize: '8px'
        }}>
          {getStatusText()}
        </div>
      </div>
    );
  };
  
  const CameraCard = ({ camera }) => {
    return (
      <div className="camera-card">
        <div className="camera-header" style={{ position: 'relative' }}>
          <div className="camera-title">{camera.name}</div>
          <div className="camera-info" style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
            <span className={`status status-${camera.status}`}>{camera.status}</span>
            <span>{camera.resolution}</span>
            <span>{camera.fps ? `${camera.fps} FPS` : 'FPS N/A'}</span>
            <span>{camera.audio_sample_rate ? `${camera.audio_sample_rate} Hz` : 'Audio N/A'}</span>
            <span>{camera.camera_type}</span>
            
            {/* Health Status - Inline with camera info */}
            <div style={{
              display: 'flex',
              alignItems: 'center',
              gap: '4px',
              padding: '2px 6px',
              borderRadius: '4px',
              background: 'rgba(10,14,20,0.35)',
              border: '1px solid rgba(148,163,184,0.15)',
              fontSize: '8px',
              minWidth: 'fit-content',
              maxWidth: '120px'
            }}>
              <StreamHealthIndicator cameraId={camera.id} health={streamHealth[camera.id]} />
            </div>
            
            {/* Action Buttons - Reload and Restart */}
            <div style={{ display: 'flex', gap: '4px', alignItems: 'center' }}>
              {/* Reload Button */}
              <button
                onClick={() => handleRefreshStream(camera.id)}
                disabled={refreshingStreams[camera.id] || camera.status !== 'online'}
                title="Refresh streams if having connection issues"
                style={{
                  background: 'rgba(10,14,20,0.7)',
                  border: '1px solid rgba(148,163,184,0.3)',
                  borderRadius: '6px',
                  width: '20px',
                  height: '20px',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  cursor: camera.status !== 'online' ? 'not-allowed' : 'pointer',
                  opacity: camera.status !== 'online' ? 0.5 : (refreshingStreams[camera.id] ? 0.7 : 1),
                  color: '#d6deea',
                  transition: 'opacity 0.2s ease',
                  flexShrink: 0
                }}
              >
                <RotateCw 
                  size={10} 
                  style={{
                    transform: refreshingStreams[camera.id] ? 'rotate(360deg)' : 'none',
                    transition: refreshingStreams[camera.id] ? 'transform 1s linear infinite' : 'transform 0.2s ease'
                  }} 
                />
              </button>
              
              {/* Restart Button */}
              <button
                onClick={() => handleRestartCamera(camera.id)}
                disabled={restartingCameras[camera.id] || camera.status !== 'online'}
                title="Restart camera (stop recording, stop camera, start camera)"
                style={{
                  background: 'rgba(10,14,20,0.7)',
                  border: '1px solid rgba(255,99,99,0.4)', // Light red border
                  borderRadius: '6px',
                  width: 'auto',
                  height: '20px',
                  padding: '0 6px',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  cursor: camera.status !== 'online' ? 'not-allowed' : 'pointer',
                  opacity: camera.status !== 'online' ? 0.5 : (restartingCameras[camera.id] ? 0.7 : 1),
                  color: restartingCameras[camera.id] ? '#ff6b6b' : '#ff9999', // Light red colors
                  transition: 'all 0.2s ease',
                  flexShrink: 0,
                  fontSize: '9px',
                  fontWeight: 500
                }}
              >
                <span style={{ fontSize: '9px', lineHeight: 1 }}>restart</span>
              </button>
            </div>
          </div>
        </div>

        <div className="camera-video" style={{ display: 'flex', gap: '16px', flexWrap: 'wrap' }}>
          <div className="camera-column primary-column">
            <div className="camera-frame">
              <CameraVideoPanel camera={camera} variant="primary" liveStreamMode={liveStreamMode} hlsFailedByCamera={hlsFailedByCamera} setHlsFailedByCamera={setHlsFailedByCamera} />
            </div>

            <div className="camera-controls-card">
              {camera.status === 'offline' || camera.status === 'error' ? (
                <button
                  className="btn btn-success"
                  onClick={() => handleStartCamera(camera.id)}
                  disabled={startingCameras[camera.id]}
                  style={{
                    ...COMPACT_BUTTON_STYLE,
                    animation: startingCameras[camera.id] ? 'pulse 1.5s ease-in-out infinite' : 'none',
                    opacity: startingCameras[camera.id] ? 0.8 : 1,
                  }}
                >
                  <Power 
                    size={14} 
                    style={{
                      transform: startingCameras[camera.id] ? 'scale(1.2)' : 'scale(1)',
                      transition: 'transform 0.3s ease'
                    }} 
                  />
                  {startingCameras[camera.id] ? 'Starting...' : 'Start'}
                </button>
              ) : (
                <button
                  className="btn btn-secondary"
                  onClick={() => handleStopCamera(camera.id)}
                  disabled={camera.status === 'recording'}
                  style={COMPACT_BUTTON_STYLE}
                >
                  <PowerOff size={14} />
                  Stop
                </button>
              )}

              {camera.status === 'recording' ? (
                <button
                  className="btn btn-danger"
                  onClick={() => handleStopRecording(camera.id)}
                  style={COMPACT_BUTTON_STYLE}
                >
                  <Square size={14} />
                  Stop Rec
                </button>
              ) : (
                <button
                  className="btn btn-success"
                  onClick={() => handleStartRecording(camera.id)}
                  disabled={camera.status !== 'online'}
                  title={camera.status !== 'online' ? `Camera must be online to record (current: ${camera.status})` : 'Start recording'}
                  style={{
                    ...COMPACT_BUTTON_STYLE,
                    opacity: camera.status !== 'online' ? 0.92 : 1,
                    filter: 'none',
                    background: camera.status !== 'online' ? '#2f5f37' : undefined,
                    borderColor: camera.status !== 'online' ? '#3d7d48' : undefined,
                    color: camera.status !== 'online' ? '#e8f5eb' : undefined,
                  }}
                >
                  <Play size={14} />
                  Record
                </button>
              )}

              <button
                className="btn btn-primary"
                onClick={() => setEditingCamera(camera)}
                style={COMPACT_BUTTON_STYLE}
              >
                <Edit size={14} />
                Edit
              </button>

              {camera.camera_type === 'recorded' && (
                <button
                  className="btn btn-secondary"
                  onClick={() => openFileBrowser((path) => {
                    api.updateCamera(camera.id, { source: path })
                      .then((res) => {
                        patchCameraInState(camera.id, { source: path });
                        toast.success('Source updated to: ' + path);
                      })
                      .catch((err) => toast.error('Failed to update source: ' + (err.response?.data?.detail || err.message)));
                  })}
                  style={COMPACT_BUTTON_STYLE}
                  title="Browse for a video file"
                >
                  <FolderOpen size={14} />
                  Browse
                </button>
              )}

              <button
                className="btn btn-danger"
                onClick={() => handleDeleteCamera(camera.id)}
                style={COMPACT_BUTTON_STYLE}
              >
                <Trash2 size={14} />
                Delete
              </button>

              <label
                title="Enable Audio (record + live)"
                style={{
                  ...AUDIO_TOGGLE_STYLE,
                  background: camera.audio_enabled ? '#1e293b' : '#151a21',
                  color: camera.audio_enabled ? '#22c55e' : '#d6deea',
                }}
              >
                <input
                  type="checkbox"
                  checked={Boolean(camera.audio_enabled)}
                  onChange={(e) => handleToggleCameraAudioEnabled(camera, e.target.checked)}
                  style={{ display: 'none' }}
                  aria-label="Enable audio"
                />
                {camera.audio_enabled ? <Mic size={14} /> : <MicOff size={14} />}
              </label>

              <CameraAudioPanel camera={camera} disabled={isLiveHlsMode} />
            </div>
          </div>

          <div className="camera-column support-column">
            <div className="camera-frame">
              <CameraVideoPanel camera={camera} variant="support" liveStreamMode={liveStreamMode} hlsFailedByCamera={hlsFailedByCamera} setHlsFailedByCamera={setHlsFailedByCamera} />
            </div>

            <div className="view-controls-card">
              <div className="sensitivity-group">
                <span className="sensitivity-label">Sensitivity:</span>
                <input
                  type="range"
                  min="0"
                  max={SENSITIVITY_LEVEL}
                  step="1"
                  value={Number(cameraSettings[camera.id]?.sensitivity ?? DEFAULT_SENSITIVITY)}
                  onChange={(e) => handleSensitivityChange(camera.id, e.target.value)}
                  style={{ width: '140px' }}
                />
                <span className="sensitivity-option-text">
                  {Number(cameraSettings[camera.id]?.sensitivity ?? DEFAULT_SENSITIVITY)} / {SENSITIVITY_LEVEL}
                </span>
              </div>
              <button
                className="btn btn-outline support-toggle-btn"
                onClick={() => handleSupportToggle(camera.id)}
                style={COMPACT_BUTTON_STYLE}
              >
                {(cameraSettings[camera.id]?.supportEnabled ?? true) ? 'Disable' : 'Enable'}
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  };

  const handleSensitivityChange = async (cameraId, level) => {
    const nextSensitivity = Math.max(0, Math.min(SENSITIVITY_LEVEL, Number(level) || 0));
    setCameraSettings(prev => ({
      ...prev,
      [cameraId]: {
        ...(prev[cameraId] || { supportEnabled: false, sensitivity: DEFAULT_SENSITIVITY }),
        sensitivity: nextSensitivity
      }
    }));

    try {
      await api.setCameraSensitivity(cameraId, nextSensitivity);
    } catch (error) {
      toast.error('Failed to update sensitivity: ' + (error.response?.data?.detail || error.message));
    }
  };

  const handleSupportToggle = (cameraId) => {
    setCameraSettings(prev => {
      const cur = prev[cameraId] || { sensitivity: DEFAULT_SENSITIVITY, supportEnabled: false };
      return {
        ...prev,
        [cameraId]: { ...cur, supportEnabled: !cur.supportEnabled }
      };
    });
  };

  const openFileBrowser = async (formSetter) => {
    setFileBrowserFormSetter(() => formSetter);
    try {
      const res = await api.browseFiles(null);
      setFileBrowserPath(res.data.current);
      setFileBrowserParent(res.data.parent);
      setFileBrowserItems(res.data.items);
      setShowFileBrowser(true);
    } catch (err) {
      toast.error('Failed to browse files: ' + (err.response?.data?.detail || err.message));
    }
  };

  const navigateFileBrowser = async (dirPath) => {
    try {
      const res = await api.browseFiles(dirPath);
      setFileBrowserPath(res.data.current);
      setFileBrowserParent(res.data.parent);
      setFileBrowserItems(res.data.items);
    } catch (err) {
      toast.error('Cannot access: ' + (err.response?.data?.detail || err.message));
    }
  };

  const selectFile = (filePath) => {
    if (fileBrowserFormSetter) fileBrowserFormSetter(filePath);
    setShowFileBrowser(false);
  };

  const handleAddCamera = async (formData) => {
    try {
      const response = await api.createCamera(formData);
      setCameras(prev => [...prev, response.data]);
      setShowAddModal(false);
      setNewCamera({ ...DEFAULT_CAMERA_FORM });
      toast.success('Camera added successfully');
    } catch (error) {
      toast.error('Failed to add camera: ' + (error.response?.data?.detail || error.message));
    }
  };

  // Utility to update one camera entry in local list state.
  const patchCameraInState = (cameraId, partial) => {
    setCameras((prev) => prev.map((item) => (item.id === cameraId ? { ...item, ...partial } : item)));
  };

  const handleUpdateCamera = async (formData) => {
    try {
      const response = await api.updateCamera(formData.id || editingCamera.id, formData);
      setCameras(prev => prev.map(c => c.id === editingCamera.id ? response.data : c));
      setEditingCamera(null);
      toast.success('Camera updated successfully');
    } catch (error) {
      toast.error('Failed to update camera: ' + (error.response?.data?.detail || error.message));
    }
  };

  const handleDeleteCamera = async (cameraId) => {
    if (!window.confirm('Are you sure you want to delete this camera?')) {
      return;
    }
    
    try {
      await api.deleteCamera(cameraId);
      setCameras(prev => prev.filter(c => c.id !== cameraId));
      toast.success('Camera deleted successfully');
    } catch (error) {
      toast.error('Failed to delete camera: ' + (error.response?.data?.detail || error.message));
    }
  };
  
  const handleStartCamera = async (cameraId) => {
    try {
      setStartingCameras(prev => ({ ...prev, [cameraId]: true }));
      await api.startCamera(cameraId);
      // Give background threads time to start before UI shows streams
      await new Promise(resolve => setTimeout(resolve, 750));
      patchCameraInState(cameraId, { status: 'online', last_seen: new Date().toISOString() });
      toast.success('Camera started');
    } catch (error) {
      console.error('Start camera error:', error);
      toast.error('Failed to start camera: ' + (error.response?.data?.detail || error.message));
    } finally {
      setStartingCameras(prev => ({ ...prev, [cameraId]: false }));
    }
  };

  const handleStopCamera = async (cameraId) => {
    try {
      await api.stopCamera(cameraId);
      try { await api.stopCamera(cameraId); } catch {}
      patchCameraInState(cameraId, { status: 'offline' });
      toast.success('Camera stopped');
    } catch (error) {
      console.error('Stop camera error:', error);
      toast.error('Failed to stop camera: ' + (error.response?.data?.detail || error.message));
    }
  };

  const handleStartRecording = async (cameraId) => {
    try {
      await api.startRecording(cameraId);
      patchCameraInState(cameraId, { status: 'recording' });
      toast.success('Recording started');
    } catch (error) {
      console.error('Start recording error:', error);
      toast.error('Failed to start recording: ' + (error.response?.data?.detail || error.message));
    }
  };

  const handleStopRecording = async (cameraId) => {
    try {
      await api.stopRecording(cameraId);
      patchCameraInState(cameraId, { status: 'online' });
      toast.success('Recording stopped');
    } catch (error) {
      console.error('Stop recording error:', error);
      toast.error('Failed to stop recording: ' + (error.response?.data?.detail || error.message));
    }
  };

  const handleToggleCameraAudioEnabled = async (camera, enabled) => {
    const cameraType = String(camera?.camera_type || '').toLowerCase();
    const updates = { audio_enabled: Boolean(enabled) };

    if (enabled && (cameraType === 'rtsp' || cameraType === 'ip_camera')) {
      updates.audio_input_format = 'rtsp';
      updates.audio_source = camera?.source || 'rtsp';
    }

    try {
      const response = await api.updateCamera(camera.id, updates);
      setCameras((prev) => prev.map((item) => (item.id === camera.id ? response.data : item)));
    } catch (error) {
      toast.error('Failed to update audio setting: ' + (error.response?.data?.detail || error.message));
    }
  };

  return (
    <div>
      <div className="page-header">
        <h1 className="page-title">Cameras</h1>
        <p className="page-subtitle">Manage your cameras and recording settings</p>
      </div>

      <div className="content-section">
        <div className="section-header">
          <h2 className="section-title">Camera List ({cameras.length})</h2>
          <div style={{ display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap' }}>
            <button
              className="btn btn-secondary"
              onClick={handleToggleLiveStreamMode}
              disabled={isTogglingLiveMode}
              title="Toggle live stream mode"
              style={COMPACT_BUTTON_STYLE}
            >
              {isTogglingLiveMode
                ? 'Switching...'
                : `Mode: ${liveStreamMode.toUpperCase()}`}
            </button>
            <button
              className="btn btn-secondary"
              onClick={handleToggleRtspUnifiedCapture}
              disabled={isTogglingRtspUnifiedCapture}
              title="Enable/disable unified RTSP capture+demux"
              style={COMPACT_BUTTON_STYLE}
            >
              {isTogglingRtspUnifiedCapture
                ? 'Updating...'
                : `RTSP Unified: ${rtspUnifiedCaptureEnabled ? 'ON' : 'OFF'}`}
            </button>
            <button
              className="btn btn-primary"
              onClick={() => setShowAddModal(true)}
              style={COMPACT_BUTTON_STYLE}
            >
              <Plus size={16} />
              Add Camera
            </button>
          </div>
        </div>

        <div className="camera-grid">
          {cameras.map((camera) => (
            <CameraCard key={camera.id} camera={camera} />
          ))}

          {cameras.length === 0 && (
            <div className="camera-card">
              <div className="camera-video">
                <div className="camera-placeholder">
                  <p>No cameras configured</p>
                  <button
                    className="btn btn-primary"
                    onClick={() => setShowAddModal(true)}
                    style={COMPACT_BUTTON_STYLE}
                  >
                    <Plus size={16} />
                    Add Your First Camera
                  </button>
                </div>
              </div>
            </div>
          )}
        </div>
      </div>

      {showAddModal && (
        <CameraForm
          key="add"
          initialCamera={newCamera}
          onSubmit={handleAddCamera}
          onClose={() => setShowAddModal(false)}
          onBrowse={openFileBrowser}
          title="Add New Camera ..."
          submitText="Add Camera"
        />
      )}

      {editingCamera && (
        <CameraForm
          key={editingCamera.id}
          initialCamera={editingCamera}
          onSubmit={handleUpdateCamera}
          onClose={() => setEditingCamera(null)}
          onBrowse={openFileBrowser}
          title="Edit Camera"
          submitText="Update Camera"
        />
      )}

      {showFileBrowser && (
        <div className="modal-overlay" onClick={() => setShowFileBrowser(false)}>
          <div className="modal-content" onClick={(e) => e.stopPropagation()} style={{ maxWidth: '550px', maxHeight: '70vh', display: 'flex', flexDirection: 'column' }}>
            <h3 style={{ margin: '0 0 8px', fontSize: '14px' }}>Browse Video Files</h3>
            <div style={{ fontSize: '12px', color: '#888', marginBottom: '8px', wordBreak: 'break-all' }}>{fileBrowserPath}</div>
            <div style={{ flex: 1, overflowY: 'auto', border: '1px solid #333', borderRadius: '4px' }}>
              {fileBrowserParent && (
                <div
                  style={{ padding: '6px 10px', cursor: 'pointer', borderBottom: '1px solid #333', fontSize: '13px' }}
                  onClick={() => navigateFileBrowser(fileBrowserParent)}
                >📁 ..</div>
              )}
              {fileBrowserItems.map((item) => (
                <div
                  key={item.path}
                  style={{ padding: '6px 10px', cursor: 'pointer', borderBottom: '1px solid #222', fontSize: '13px' }}
                  onClick={() => item.type === 'directory' ? navigateFileBrowser(item.path) : selectFile(item.path)}
                >
                  {item.type === 'directory' ? '📁' : '🎬'} {item.name}
                </div>
              ))}
              {fileBrowserItems.length === 0 && (
                <div style={{ padding: '12px', color: '#666', fontSize: '13px', textAlign: 'center' }}>No video files or folders</div>
              )}
            </div>
            <div style={{ marginTop: '8px', textAlign: 'right' }}>
              <button className="btn btn-secondary" style={COMPACT_BUTTON_STYLE} onClick={() => setShowFileBrowser(false)}>Cancel</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default CameraList;