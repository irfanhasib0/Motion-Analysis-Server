import React, { useEffect, useRef, useState } from 'react';
import { Plus, Edit, Trash2, Play, Square, Settings, Power, PowerOff, Mic, MicOff } from 'lucide-react';
import { toast } from 'react-hot-toast';
import { api } from '../api';
import './CameraList.css';

const DEFAULT_CAMERA_FORM = {
  name: '',
  camera_type: 'rtsp',
  source: '',
  resolution: '1920x1080',
  fps: 30,
  enabled: true,
  description: '',
  location: '',
  audio_enabled: false,
  audio_source: 'default',
  audio_input_format: 'pulse',
  audio_sample_rate: 16000,
  audio_chunk_size: 512,
};

const COMPACT_BUTTON_STYLE = { padding: '6px 10px', fontSize: '12px', lineHeight: 1.1 };

const AUDIO_TOGGLE_STYLE = {
  display: 'inline-flex',
  alignItems: 'center',
  justifyContent: 'center',
  width: '32px',
  height: '32px',
  borderRadius: '8px',
  border: '1px solid #2f3743',
  cursor: 'pointer',
};

const AUDIO_PANEL_STYLE = {
  display: 'inline-flex',
  flexDirection: 'column',
  alignItems: 'stretch',
  width: '280px',
  flex: '0 0 auto',
  padding: '4px 8px',
  border: '1px solid rgba(148,163,184,0.25)',
  borderRadius: '10px',
  background: 'rgba(10,14,20,0.35)',
};

const AUDIO_PLAYER_STYLE = { width: '100%', maxWidth: '100%', height: '22px' };

const AUDIO_SAMPLE_RATE_OPTIONS = [
  { value: 16000, label: '16000 (Recommended balance)' },
  { value: 22050, label: '22050 (Clearer voice)' },
  { value: 32000, label: '32000 (Higher clarity)' },
  { value: 44100, label: '44100 (High quality)' },
  { value: 48000, label: '48000 (Studio / highest quality)' },
  { value: 8000, label: '8000 (Very low bandwidth)' },
];

const AUDIO_CHUNK_SIZE_OPTIONS = [
  { value: 256, label: '256 (Low latency, more CPU)' },
  { value: 512, label: '512 (Recommended balance)' },
  { value: 1024, label: '1024 (Stable, higher latency)' },
  { value: 2048, label: '2048 (Very stable, high latency)' },
  { value: 4096, label: '4096 (Maximum stability, highest latency)' },
];

const AUDIO_INPUT_FORMAT_OPTIONS = [
  { value: 'pulse', label: 'pulse' },
  { value: 'alsa', label: 'alsa' },
];

const AUDIO_SOURCE_OPTIONS = [
  { value: 'default', label: 'default' },
  { value: 'alsa_input.pci-0000_00_1f.3.analog-stereo', label: 'alsa_input (Intel PCH)' },
  { value: 'hw:1,0', label: 'hw:1,0 (ALSA direct)' },
];

const AUDIO_LATENCY_PROFILES = {
  low: { audio_sample_rate: 16000, audio_chunk_size: 256 },
  balanced: { audio_sample_rate: 16000, audio_chunk_size: 512 },
  stable: { audio_sample_rate: 22050, audio_chunk_size: 1024 },
};

const SENSITIVITY_LEVEL = 5;
const DEFAULT_SENSITIVITY = 2;

const getCameraAspectRatio = (resolution) => {
  const match = /^([0-9]+)x([0-9]+)$/i.exec(resolution || '');
  if (!match) {
    return '16 / 9';
  }
  const width = parseInt(match[1], 10) || 16;
  const height = parseInt(match[2], 10) || 9;
  return `${width} / ${height}`;
};

// Backend always streams raw PCM wrapped in a WAV header — format is fixed.
const AUDIO_STREAM_FORMAT = 'wav';

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

  const isLiveHlsMode = liveStreamMode === 'hls';

  const handleToggleLiveStreamMode = async () => {
    const targetMode = liveStreamMode === 'hls' ? 'mjpeg' : 'hls';
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

  // -----------------------------
  // Subcomponents
  // -----------------------------
  
  const CameraCard = ({ camera }) => {
    return (
      <div className="camera-card">
        <div className="camera-header">
          <div className="camera-title">{camera.name}</div>
          <div className="camera-info">
            <span className={`status status-${camera.status}`}>{camera.status}</span>
            <span>{camera.resolution}</span>
            <span>{camera.fps ? `${camera.fps} FPS` : 'FPS N/A'}</span>
            <span>{camera.camera_type}</span>
          </div>
        </div>

        <div className="camera-video" style={{ display: 'flex', gap: '16px', flexWrap: 'wrap' }}>
          <div className="camera-column primary-column">
            <div className="camera-frame">
              <CameraVideoPanel camera={camera} variant="primary" />
            </div>

            <div className="camera-controls-card">
              {camera.status === 'offline' || camera.status === 'error' ? (
                <button
                  className="btn btn-success"
                  onClick={() => handleStartCamera(camera.id)}
                  style={COMPACT_BUTTON_STYLE}
                >
                  <Power size={14} />
                  Start
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
              <CameraVideoPanel camera={camera} variant="support" />
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

  const CameraAudioPanel = ({ camera, disabled = false }) => {
    const audioRef = useRef(null);
    const isOnline = camera.status === 'online' || camera.status === 'recording';
    const shouldRenderAudio = isOnline && Boolean(camera.audio_enabled) && !disabled;
    const [audioPlaybackFormat] = useState(AUDIO_STREAM_FORMAT);
    const [activeAudioUrl, setActiveAudioUrl] = useState('');
    const [audioLoadFailed, setAudioLoadFailed] = useState(false);

    const handleAudioSuccess = () => {
      setAudioLoadFailed(false);
    };

    const handleAudioError = () => {
      setAudioLoadFailed(true);
    };

    const handleAudioStalled = () => {
      // Optionally, treat as error or ignore
    };

    const currentInputFormat = String(camera.audio_input_format || 'pulse').toLowerCase();
    const currentInputSource = String(camera.audio_source || 'default').toLowerCase();
    const suggestAlsa = currentInputFormat === 'pulse' || currentInputSource === 'default';
    const suggestedInput = suggestAlsa ? 'alsa / hw:1,0' : 'pulse / default';

    useEffect(() => {
      setActiveAudioUrl('');
      setAudioLoadFailed(false);
    }, [camera.id]);

    useEffect(() => {
      if (shouldRenderAudio) {
        return;
      }
      setActiveAudioUrl('');
      setAudioLoadFailed(false);
      const audioEl = audioRef.current;
      if (!audioEl) {
        return;
      }
      try {
        audioEl.pause();
        audioEl.removeAttribute('src');
        audioEl.load();
      } catch {}
    }, [shouldRenderAudio]);

    // Only attempt to play audio once on mount or when camera changes
    useEffect(() => {
      if (!shouldRenderAudio) {
        return undefined;
      }

      let cancelled = false;

      const startAudio = async () => {
        try {
          // No longer auto-start camera - user must click start button first
          // await api.startCamera(camera.id);

          if (cancelled) {
            return;
          }

          const streamUrl = api.getCameraAudioStreamUrl(camera.id, audioPlaybackFormat);
          setActiveAudioUrl(streamUrl);
          setAudioLoadFailed(false);

          const audioEl = audioRef.current;
          if (!audioEl || cancelled) {
            return;
          }

          audioEl.autoplay = true;
          audioEl.defaultMuted = true;
          audioEl.muted = true;
          audioEl.src = streamUrl;
          audioEl.load();
          try {
            await audioEl.play();
          } catch {
            // Ignore autoplay errors
          }
        } catch {
          if (!cancelled) {
            setActiveAudioUrl('');
            setAudioLoadFailed(true);
          }
        }
      };

      startAudio();

      return () => {
        cancelled = true;
        const audioEl = audioRef.current;
        if (audioEl) {
          try {
            audioEl.pause();
            audioEl.removeAttribute('src');
            audioEl.load();
          } catch {}
        }
        //api.stopCameraAudioStream(camera.id).catch(() => {});
      };
    }, [camera.id, shouldRenderAudio]);//camera.id, shouldRenderAudio, audioPlaybackFormat]);

    //if (!shouldRenderAudio) {
    //  return null;
    //}

    return (
      <div style={AUDIO_PANEL_STYLE}>
        <audio
          ref={audioRef}
          key={`${camera.id}:controls:audio`}
          src={activeAudioUrl}
          autoPlay={true}
          muted={true}
          defaultMuted={true}
          controls={true}
          preload="auto"
          playsInline={true}
          style={AUDIO_PLAYER_STYLE}
          onCanPlay={handleAudioSuccess}
          onLoadedData={handleAudioSuccess}
          onPlaying={handleAudioSuccess}
          onError={handleAudioError}
          onStalled={handleAudioStalled}
          onAbort={handleAudioError}
        />
        {audioLoadFailed && (
          <button onClick={() => {
            setAudioLoadFailed(false);
            // Re-run the audio start logic
            const audioEl = audioRef.current;
            if (audioEl) {
              audioEl.load();
              audioEl.play().catch(() => {});
            }
          }} style={{ marginTop: 8 }}>
            Retry Audio
          </button>
        )}
      </div>
    );
  };

  const CameraVideoPanel = ({ camera, variant = 'primary' }) => {
    const videoRef = useRef(null);
    const isOnline = camera.status === 'online' || camera.status === 'recording';
    const isHlsStream = isLiveHlsMode && variant === 'primary' && !hlsFailedByCamera[camera.id];

    useEffect(() => {
      if (!isOnline || !isHlsStream || !videoRef.current) {
        return undefined;
      }

      let destroyed = false;
      let hlsInstance = null;
      const videoEl = videoRef.current;
      const sourceUrl = api.getCameraVideoStreamUrl(camera.id, 'hls');

      const attachStream = async () => {
        if (videoEl.canPlayType('application/vnd.apple.mpegurl')) {
          videoEl.src = sourceUrl;
          try { await videoEl.play(); } catch {}
          return;
        }

        try {
          const hlsModule = await import('hls.js');
          const Hls = hlsModule.default;
          if (destroyed || !videoRef.current) {
            return;
          }

          if (Hls.isSupported()) {
            hlsInstance = new Hls({
              lowLatencyMode: true,
              liveSyncDurationCount: 3,
            });
            hlsInstance.on(Hls.Events.ERROR, (_event, data) => {
              if (data?.fatal) {
                setHlsFailedByCamera((prev) => ({ ...prev, [camera.id]: true }));
              }
            });
            hlsInstance.loadSource(sourceUrl);
            hlsInstance.attachMedia(videoRef.current);
            hlsInstance.on(Hls.Events.MANIFEST_PARSED, () => {
              if (!destroyed && videoRef.current) {
                videoRef.current.play().catch(() => {});
              }
            });
          } else {
            videoRef.current.src = sourceUrl;
            videoRef.current.play().catch(() => {});
          }
        } catch (error) {
          console.error('Failed to initialize HLS stream:', error);
          setHlsFailedByCamera((prev) => ({ ...prev, [camera.id]: true }));
          if (!destroyed && videoRef.current) {
            videoRef.current.src = sourceUrl;
          }
        }
      };

      attachStream();

      return () => {
        destroyed = true;
        if (hlsInstance) {
          hlsInstance.destroy();
        }
        if (videoEl) {
          try {
            videoEl.pause();
            videoEl.removeAttribute('src');
            videoEl.load();
          } catch {}
        }
      };
    }, [camera.id, isHlsStream, isOnline]);

    const mediaStyle = {
      width: '100%',
      height: 'auto',
      aspectRatio: getCameraAspectRatio(camera.resolution),
      objectFit: 'contain',
      background: '#000',
    };

    if (!isOnline) {
      return (
        <img
          src={`${api.getBlankStreamUrl(camera.id)}`}
          alt={`Camera ${camera.name}`}
          className="camera-stream"
          style={mediaStyle}
        />
      );
    }

    if (isHlsStream) {
      return (
        <video
          ref={videoRef}
          key={`${camera.id}:${variant}:hls`}
          className="camera-stream"
          style={mediaStyle}
          autoPlay
          playsInline
          controls
          onError={() => setHlsFailedByCamera((prev) => ({ ...prev, [camera.id]: true }))}
        />
      );
    }

    const streamUrl = variant === 'support'
      ? api.appendQueryParams(api.getProcessingStreamUrl(camera.id), { view: 'support' })
      : api.getCameraVideoStreamUrl(camera.id, 'mjpeg');

    return (
      <img
        key={`${camera.id}:${variant}:mjpeg`}
        src={streamUrl}
        alt={variant === 'support' ? `Support view for ${camera.name}` : `Camera ${camera.name}`}
        className="camera-stream"
        style={mediaStyle}
      />
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
      await api.startCamera(cameraId);
      // Give background threads time to start before UI shows streams
      await new Promise(resolve => setTimeout(resolve, 750));
      patchCameraInState(cameraId, { status: 'online', last_seen: new Date().toISOString() });
      toast.success('Camera started');
    } catch (error) {
      console.error('Start camera error:', error);
      toast.error('Failed to start camera: ' + (error.response?.data?.detail || error.message));
    }
  };

  const handleStopCamera = async (cameraId) => {
    try {
      await api.stopCamera(cameraId);
      try { await api.closeCameraStream(cameraId); } catch {}
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

  const CameraForm = ({ initialCamera, onSubmit, title, submitText }) => {
    const [form, setForm] = useState(initialCamera);
    
    const handleSubmit = async (e) => {
      e.preventDefault();
      await onSubmit(form);
    };

    return (
      <div className="modal-overlay" onClick={(e) => e.target === e.currentTarget && (setShowAddModal(false) || setEditingCamera(null))}>
        <div className="modal">
          <div className="modal-header">
            <h3 className="modal-title">{title}</h3>
            <button 
              className="modal-close" 
              onClick={() => {
                setShowAddModal(false);
                setEditingCamera(null);
              }}
            >
              ×
            </button>
          </div>
          
          <form onSubmit={handleSubmit}>
            <div className="modal-body">
              <div className="form-group">
                <label className="form-label">Name</label>
                <input 
                  type="text"
                  className="form-control"
                  value={form.name}
                  onChange={(e) => setForm({...form, name: e.target.value})}
                  required
                />
              </div>
              
              <div className="form-group">
                <label className="form-label">Camera Type</label>
                <select 
                  className="form-control form-select"
                  value={form.camera_type}
                  onChange={(e) => setForm({...form, camera_type: e.target.value})}
                >
                  <option value="recorded">Recorded Data</option>
                  <option value="rtsp">RTSP Stream</option>
                  <option value="webcam">Webcam</option>
                  <option value="ip_camera">IP Camera</option>
                </select>
              </div>
              
              <div className="form-group">
                <label className="form-label">Source</label>
                <input 
                  type="text"
                  className="form-control"
                  value={form.source}
                  onChange={(e) => setForm({...form, source: e.target.value})}
                  placeholder={
                    form.camera_type === 'recorded' ? '/path/to/video/file.mp4 or recording_id' :
                    form.camera_type === 'rtsp' ? 'rtsp://username:password@ip:port/path' :
                    form.camera_type === 'webcam' ? '0' :
                    'http://ip:port/video'
                  }
                  required
                />
              </div>
              
              <div className="grid grid-2">
                <div className="form-group">
                  <label className="form-label">Resolution</label>
                  <select 
                    className="form-control form-select"
                    value={form.resolution}
                    onChange={(e) => setForm({...form, resolution: e.target.value})}
                  >
                    <option value="320x240">320x240</option>
                    <option value="480x360">480x360</option>
                    <option value="640x480">640x480</option>
                    <option value="1280x720">1280x720</option>
                    <option value="1920x1080">1920x1080</option>
                  </select>
                </div>
                
                <div className="form-group">
                  <label className="form-label">FPS</label>
                  <input 
                    type="number"
                    className="form-control"
                    style={{ width: '80px', minWidth: '60px', display: 'inline-block' }}
                    value={form.fps}
                    onChange={(e) => setForm({...form, fps: parseInt(e.target.value)})}
                    min="1"
                    max="60"
                  />
                </div>
              </div>
              
              <div className="form-group">
                <label className="form-label">Location</label>
                <input 
                  type="text"
                  className="form-control"
                  value={form.location || ''}
                  onChange={(e) => setForm({...form, location: e.target.value})}
                  placeholder="e.g., Front Door, Parking Lot"
                />
              </div>
              
              <div className="form-group">
                <label className="form-label">Description</label>
                <textarea 
                  className="form-control"
                  value={form.description || ''}
                  onChange={(e) => setForm({...form, description: e.target.value})}
                  rows="3"
                  placeholder="Optional description"
                />
              </div>
              
              <div className="form-group">
                <label className="form-label">
                  <input 
                    type="checkbox"
                    checked={form.enabled ?? true}
                    onChange={(e) => setForm({...form, enabled: e.target.checked})}
                    style={{ marginRight: '8px' }}
                  />
                  Enabled
                </label>
              </div>

              <div className="form-group">
                <label className="form-label">
                  <input
                    type="checkbox"
                    checked={form.audio_enabled ?? false}
                    onChange={(e) => setForm({...form, audio_enabled: e.target.checked})}
                    style={{ marginRight: '8px' }}
                  />
                  Enable Audio (record + live)
                </label>
              </div>

              {form.audio_enabled && (
                <div className="grid grid-2">
                  {(String(form.camera_type || '').toLowerCase() === 'rtsp' || String(form.camera_type || '').toLowerCase() === 'ip_camera') ? (
                    <div className="form-group" style={{ gridColumn: '1 / -1' }}>
                      <label className="form-label">Audio Input</label>
                      <div className="form-control" style={{ display: 'flex', alignItems: 'center' }}>
                        RTSP stream audio (auto)
                      </div>
                    </div>
                  ) : (
                    <>
                      <div className="form-group">
                        <label className="form-label">Audio Input Format</label>
                        <select
                          className="form-control form-select"
                          style={{ width: '100px', minWidth: '70px', display: 'inline-block' }}
                          value={form.audio_input_format || 'pulse'}
                          onChange={(e) => setForm({...form, audio_input_format: e.target.value})}
                        >
                          {AUDIO_INPUT_FORMAT_OPTIONS.map((item) => (
                            <option key={`aif:${item.value}`} value={item.value}>{item.label}</option>
                          ))}
                        </select>
                      </div>

                      <div className="form-group">
                        <label className="form-label">Audio Source</label>
                        <select
                          className="form-control form-select"
                          style={{ width: '180px', minWidth: '100px', display: 'inline-block' }}
                          value={AUDIO_SOURCE_OPTIONS.some((item) => item.value === (form.audio_source || 'default')) ? (form.audio_source || 'default') : 'custom'}
                          onChange={(e) => {
                            if (e.target.value !== 'custom') {
                              setForm({ ...form, audio_source: e.target.value });
                            }
                          }}
                        >
                          {AUDIO_SOURCE_OPTIONS.map((item) => (
                            <option key={`as:${item.value}`} value={item.value}>{item.label}</option>
                          ))}
                          <option value="custom">Custom...</option>
                        </select>
                        {!AUDIO_SOURCE_OPTIONS.some((item) => item.value === (form.audio_source || 'default')) && (
                          <input
                            type="text"
                            className="form-control"
                            style={{ width: '180px', minWidth: '100px', display: 'inline-block', marginTop: '8px' }}
                            value={form.audio_source || ''}
                            onChange={(e) => setForm({ ...form, audio_source: e.target.value })}
                            placeholder="e.g. alsa_input.pci-..."
                            spellCheck={false}
                          />
                        )}
                      </div>
                    </>
                  )}

                  <div className="form-group">
                    <label className="form-label">Audio Latency Profile</label>
                    <select
                      className="form-control form-select"
                      style={{ width: '120px', minWidth: '90px', display: 'inline-block' }}
                      defaultValue=""
                      onChange={(e) => {
                        const profile = AUDIO_LATENCY_PROFILES[e.target.value];
                        if (!profile) {
                          return;
                        }
                        setForm({ ...form, ...profile });
                      }}
                    >
                      <option value="">Manual (keep current values)</option>
                      <option value="low">Low Latency (16000 / 256)</option>
                      <option value="balanced">Balanced (16000 / 512)</option>
                      <option value="stable">Stable (22050 / 1024)</option>
                    </select>
                  </div>

                  <div className="form-group">
                    <label className="form-label">Audio Sample Rate</label>
                    <select
                      className="form-control form-select"
                      style={{ width: '110px', minWidth: '80px', display: 'inline-block' }}
                      value={AUDIO_SAMPLE_RATE_OPTIONS.some((item) => item.value === Number(form.audio_sample_rate)) ? Number(form.audio_sample_rate) : 'custom'}
                      onChange={(e) => {
                        const value = e.target.value;
                        if (value !== 'custom') {
                          setForm({ ...form, audio_sample_rate: Number(value) });
                        }
                      }}
                    >
                      {AUDIO_SAMPLE_RATE_OPTIONS.map((item) => (
                        <option key={item.value} value={item.value}>{item.label}</option>
                      ))}
                      <option value="custom">Custom</option>
                    </select>
                    {!AUDIO_SAMPLE_RATE_OPTIONS.some((item) => item.value === Number(form.audio_sample_rate)) && (
                      <input
                        type="number"
                        className="form-control"
                        style={{ width: '80px', minWidth: '60px', display: 'inline-block', marginTop: '8px' }}
                        value={form.audio_sample_rate ?? 16000}
                        onChange={(e) => setForm({ ...form, audio_sample_rate: parseInt(e.target.value, 10) || 16000 })}
                        min="8000"
                        max="48000"
                        step="1000"
                        placeholder="Custom sample rate"
                      />
                    )}
                  </div>

                  <div className="form-group">
                    <label className="form-label">Audio Chunk Size</label>
                    <select
                      className="form-control form-select"
                      style={{ width: '110px', minWidth: '80px', display: 'inline-block' }}
                      value={AUDIO_CHUNK_SIZE_OPTIONS.some((item) => item.value === Number(form.audio_chunk_size)) ? Number(form.audio_chunk_size) : 'custom'}
                      onChange={(e) => {
                        const value = e.target.value;
                        if (value !== 'custom') {
                          setForm({ ...form, audio_chunk_size: Number(value) });
                        }
                      }}
                    >
                      {AUDIO_CHUNK_SIZE_OPTIONS.map((item) => (
                        <option key={item.value} value={item.value}>{item.label}</option>
                      ))}
                      <option value="custom">Custom</option>
                    </select>
                    {!AUDIO_CHUNK_SIZE_OPTIONS.some((item) => item.value === Number(form.audio_chunk_size)) && (
                      <input
                        type="number"
                        className="form-control"
                        style={{ width: '80px', minWidth: '60px', display: 'inline-block', marginTop: '8px' }}
                        value={form.audio_chunk_size ?? 512}
                        onChange={(e) => setForm({ ...form, audio_chunk_size: parseInt(e.target.value, 10) || 512 })}
                        min="128"
                        max="16384"
                        step="128"
                        placeholder="Custom chunk size"
                      />
                    )}
                  </div>
                </div>
              )}
            </div>
            
            <div style={{ fontWeight: 600, fontSize: '13px', margin: '10px 0 2px 0', color: '#004d40' }}>
              Camera Config (including Audio)
            </div>
            <div className="modal-footer">
              <button
                type="button"
                className="btn btn-secondary"
                style={COMPACT_BUTTON_STYLE}
                onClick={() => {
                  setShowAddModal(false);
                  setEditingCamera(null);
                }}
              >
                Cancel
              </button>
              <button type="submit" className="btn btn-primary" style={COMPACT_BUTTON_STYLE}>
                {submitText}
              </button>
            </div>
          </form>
        </div>
      </div>
    );
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
                : `Mode: ${liveStreamMode.toUpperCase()} (Switch to ${isLiveHlsMode ? 'MJPEG' : 'HLS'})`}
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
          initialCamera={newCamera}
          onSubmit={handleAddCamera}
          title="Add New Camera ..."
          submitText="Add Camera"
        />
      )}

      {editingCamera && (
        <CameraForm
          initialCamera={editingCamera}
          onSubmit={handleUpdateCamera}
          title="Edit Camera"
          submitText="Update Camera"
        />
      )}
    </div>
  );
};

export default CameraList;