import React, { useEffect, useMemo, useState } from 'react';
import { toast } from 'react-hot-toast';
import { api } from '../../api';

const AUDIO_SAMPLE_RATE_OPTIONS = [8000, 16000, 22050, 32000, 44100, 48000];
const AUDIO_CHUNK_SIZE_OPTIONS = [256, 512, 1024, 2048, 4096];
const FPS_OPTIONS = [5, 10, 15, 20, 25, 30];
const RESOLUTION_OPTIONS = [
  { value: '640x480', label: '640x480 (VGA)' },
  { value: '1280x720', label: '1280x720 (HD)' },
  { value: '1920x1080', label: '1920x1080 (Full HD)' },
  { value: '2560x1440', label: '2560x1440 (QHD)' },
  { value: '3840x2160', label: '3840x2160 (4K)' },
];
const CAMERA_TARGET_OPTIONS = [
  { value: 'all', label: 'All Cameras' },
  { value: 'cam1', label: 'Camera 1' },
  { value: 'cam2', label: 'Camera 2' },
  { value: 'cam3', label: 'Camera 3' },
];
const DEFAULT_CAMERA_PRESET = {
  fps: 30,
  resolution: '1920x1080',
  audio_sample_rate: 16000,
  audio_chunk_size: 512,
  audio_enabled: false,
};
const LOW_POWER_CAMERA_PRESET = {
  fps: 15,
  resolution: '1280x720',
  audio_sample_rate: 8000,
  audio_chunk_size: 256,
  audio_enabled: false,
};

const PARAM_ROW_STYLE = { alignItems: 'center' };
const PARAM_LABEL_STYLE = { minWidth: 180 };
const PARAM_CONTROL_STYLE = { display: 'flex', alignItems: 'center', gap: 8, marginLeft: 'auto' };
const PARAM_INPUT_STYLE = { width: 132 };
const PARAM_UNIT_STYLE = { minWidth: 34, textAlign: 'right' };

const SystemSettings = ({ systemInfo, cameras = [], setCameras }) => {
  const [settings, setSettings] = useState({
    live_stream_mode: 'mjpeg',
    sensitivity: 4,
    jpeg_quality: 70,
    pipe_buffer_size: 100000000,
    max_vel: 0.1,
    bg_diff: 50,
    max_clip_length: 60,
    motion_check_interval: 10,
    min_free_storage_bytes: 1 * 1024 * 1024 * 1024,
    total_memory_bytes: 0,
    frame_rbf_len: 10,
    audio_rbf_len: 10,
    results_rbf_len: 10,
    mux_realtime: false,
    rtsp_unified_demux_enabled: false,
    auto_archive_days: 7,
  });
  const [playbackMode, setPlaybackMode] = useState(api.getRecordingPlaybackMode());
  const [audioByCamera, setAudioByCamera] = useState({});
  const [cameraSettings, setCameraSettings] = useState({
    fps: { target_cameras: 'all', value: 30 },
    resolution: { target_cameras: 'all', value: '1920x1080' },
    audio_sample_rate: { target_cameras: 'all', value: 16000 },
    audio_chunk_size: { target_cameras: 'all', value: 512 },
    audio_enabled: { target_cameras: 'all', value: false },
  });
  const [cameraPreset, setCameraPreset] = useState(() => {
    return localStorage.getItem('cameraPreset') || 'default';
  });
  const [lastNonCustomCameraPreset, setLastNonCustomCameraPreset] = useState(() => {
    return localStorage.getItem('lastNonCustomCameraPreset') || 'default';
  });
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [savingAudioId, setSavingAudioId] = useState('');
  const [savingCameraSettings, setSavingCameraSettings] = useState(false);
  const [performanceProfile, setPerformanceProfile] = useState('default');
  const [availablePresets, setAvailablePresets] = useState({});
  const [loadingPresets, setLoadingPresets] = useState(true);

  useEffect(() => {
    const map = {};
    cameras.forEach((camera) => {
      map[camera.id] = {
        audio_sample_rate: Number(camera.audio_sample_rate),
        audio_chunk_size: Number(camera.audio_chunk_size),
        fps: Number(camera.fps) || 30,
        audio_enabled: Boolean(camera.audio_enabled),
      };
    });
    setAudioByCamera(map);
  }, [cameras]);

  useEffect(() => {
    let cancelled = false;

    const load = async () => {
      setLoading(true);
      try {
        const response = await api.getSystemSettings();
        if (cancelled) {
          return;
        }
        const data = response?.data || {};
        setSettings((prev) => ({
          ...prev,
          live_stream_mode: data.live_stream_mode === 'hls' ? 'hls' : 'mjpeg',
          sensitivity: Number(data.sensitivity ?? prev.sensitivity),
          jpeg_quality: Number(data.jpeg_quality ?? prev.jpeg_quality),
          pipe_buffer_size: Number(data.pipe_buffer_size ?? prev.pipe_buffer_size),
          max_vel: Number(data.max_vel ?? prev.max_vel),
          bg_diff: Number(data.bg_diff ?? prev.bg_diff),
          max_clip_length: Number(data.max_clip_length ?? prev.max_clip_length),
          motion_check_interval: Number(data.motion_check_interval ?? prev.motion_check_interval),
          min_free_storage_bytes: Number(data.min_free_storage_bytes ?? prev.min_free_storage_bytes),
          total_memory_bytes: Number(data.total_memory_bytes || 0),
          frame_rbf_len: Number(data.frame_rbf_len ?? prev.frame_rbf_len),
          audio_rbf_len: Number(data.audio_rbf_len ?? prev.audio_rbf_len),
          results_rbf_len: Number(data.results_rbf_len ?? prev.results_rbf_len),
          mux_realtime: Boolean(data.mux_realtime),
          rtsp_unified_demux_enabled: Boolean(data.rtsp_unified_demux_enabled),
          auto_archive_days: Number(data.auto_archive_days ?? prev.auto_archive_days),
        }));
        // Trust backend's active_preset
        if (data.active_preset) {
          setPerformanceProfile(data.active_preset);
        }
      } catch (error) {
        toast.error(`Failed to load system settings: ${error?.response?.data?.detail || error.message}`);
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    };

    load();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;

    const loadPresets = async () => {
      setLoadingPresets(true);
      try {
        const response = await api.getSystemPresets();
        if (cancelled) {
          return;
        }
        const data = response?.data || {};
        setAvailablePresets(data.presets || {});
        
        // Update performance profile based on active preset from backend
        if (data.active_preset) {
          setPerformanceProfile(data.active_preset);
        }
      } catch (error) {
        toast.error(`Failed to load system presets: ${error?.response?.data?.detail || error.message}`);
      } finally {
        if (!cancelled) {
          setLoadingPresets(false);
        }
      }
    };

    loadPresets();
    return () => {
      cancelled = true;
    };
  }, []);

  const ramGiB = useMemo(() => {
    const runtimeMemoryBytes = Number(settings.total_memory_bytes || 0);
    const systemInfoMemoryBytes = Number(
      systemInfo?.settings?.total_memory_bytes
      ?? systemInfo?.total_memory_bytes
      ?? systemInfo?.memory_total_bytes
      ?? 0,
    );
    const resolvedBytes = runtimeMemoryBytes || systemInfoMemoryBytes;
    if (!resolvedBytes) return 0;
    return resolvedBytes / (1024 ** 3);
  }, [settings.total_memory_bytes, systemInfo]);

  const handleSaveSystemSettings = async () => {
    setSaving(true);
    try {
      const payload = {
        live_stream_mode: settings.live_stream_mode,
        sensitivity: Number(settings.sensitivity),
        jpeg_quality: Number(settings.jpeg_quality),
        pipe_buffer_size: Number(settings.pipe_buffer_size),
        max_vel: Number(settings.max_vel),
        bg_diff: Number(settings.bg_diff),
        max_clip_length: Number(settings.max_clip_length),
        motion_check_interval: Number(settings.motion_check_interval),
        min_free_storage_bytes: Number(settings.min_free_storage_bytes),
        rtsp_unified_demux_enabled: Boolean(settings.rtsp_unified_demux_enabled),
        frame_rbf_len: Number(settings.frame_rbf_len),
        audio_rbf_len: Number(settings.audio_rbf_len),
        results_rbf_len: Number(settings.results_rbf_len),
        mux_realtime: Boolean(settings.mux_realtime),
        auto_archive_days: Number(settings.auto_archive_days),
      };
      const response = await api.updateSystemSettings(payload);
      const data = response?.data || {};
      setSettings((prev) => ({
        ...prev,
        live_stream_mode: data.live_stream_mode === 'hls' ? 'hls' : 'mjpeg',
        sensitivity: Number(data.sensitivity ?? prev.sensitivity),
        jpeg_quality: Number(data.jpeg_quality ?? prev.jpeg_quality),
        pipe_buffer_size: Number(data.pipe_buffer_size ?? prev.pipe_buffer_size),
        max_vel: Number(data.max_vel ?? prev.max_vel),
        bg_diff: Number(data.bg_diff ?? prev.bg_diff),
        max_clip_length: Number(data.max_clip_length ?? prev.max_clip_length),
        motion_check_interval: Number(data.motion_check_interval ?? prev.motion_check_interval),
        min_free_storage_bytes: Number(data.min_free_storage_bytes ?? prev.min_free_storage_bytes),
        frame_rbf_len: Number(data.frame_rbf_len ?? prev.frame_rbf_len),
        audio_rbf_len: Number(data.audio_rbf_len ?? prev.audio_rbf_len),
        results_rbf_len: Number(data.results_rbf_len ?? prev.results_rbf_len),
        mux_realtime: Boolean(data.mux_realtime),
        rtsp_unified_demux_enabled: Boolean(data.rtsp_unified_demux_enabled),
        auto_archive_days: Number(data.auto_archive_days ?? prev.auto_archive_days),
      }));
      if (data.active_preset) {
        setPerformanceProfile(data.active_preset);
      }
      return data;
    } catch (error) {
      toast.error(`Failed to update system settings: ${error?.response?.data?.detail || error.message}`);
      return null;
    } finally {
      setSaving(false);
    }
  };

  const applyPerformanceProfile = async (profile) => {
    try {
      setSaving(true);
      
      // Call the backend API to apply the preset
      const response = await api.updatePerformanceProfile({
        preset_name: profile
      });
      
      if (response?.data) {
        const data = response.data;
        setPerformanceProfile(data.active_preset || profile);
        
        // Update local settings with the applied preset values
        if (data.settings) {
          setSettings(prev => ({
            ...prev,
            ...data.settings
          }));
        }
        
        toast.success(`Applied ${profile} performance profile`);
      }
    } catch (error) {
      toast.error(`Failed to apply ${profile} profile: ${error?.response?.data?.detail || error.message}`);
    } finally {
      setSaving(false);
    }
  };

  const handleCustomPerformanceValue = async (key, value) => {
    try {
      setPerformanceProfile('custom');
      setSettings((prev) => ({ ...prev, [key]: value }));
      
      // Update backend with custom values
      const customSettings = {
        preset_name: 'custom',
        [key]: value
      };
      
      await api.updatePerformanceProfile(customSettings);
    } catch (error) {
      toast.error(`Failed to update ${key}: ${error?.response?.data?.detail || error.message}`);
    }
  };

  const handlePlaybackModeChange = (mode) => {
    const normalized = mode === 'stream' ? 'stream' : 'play';
    api.setRecordingPlaybackMode(normalized);
    setPlaybackMode(normalized);
    toast.success(`Playback mode set to ${normalized}`);
  };

  const applyCameraPreset = (preset) => {
    // Save preset to localStorage
    localStorage.setItem('cameraPreset', preset);
    
    if (preset === 'default') {
      setCameraPreset('default');
      setLastNonCustomCameraPreset('default');
      localStorage.setItem('lastNonCustomCameraPreset', 'default');
      setCameraSettings(prev => ({
        fps: { ...prev.fps, value: DEFAULT_CAMERA_PRESET.fps },
        resolution: { ...prev.resolution, value: DEFAULT_CAMERA_PRESET.resolution },
        audio_sample_rate: { ...prev.audio_sample_rate, value: DEFAULT_CAMERA_PRESET.audio_sample_rate },
        audio_chunk_size: { ...prev.audio_chunk_size, value: DEFAULT_CAMERA_PRESET.audio_chunk_size },
        audio_enabled: { ...prev.audio_enabled, value: DEFAULT_CAMERA_PRESET.audio_enabled },
      }));
      return;
    }

    if (preset === 'low_power') {
      setCameraPreset('low_power');
      setLastNonCustomCameraPreset('low_power');
      localStorage.setItem('lastNonCustomCameraPreset', 'low_power');
      setCameraSettings(prev => ({
        fps: { ...prev.fps, value: LOW_POWER_CAMERA_PRESET.fps },
        resolution: { ...prev.resolution, value: LOW_POWER_CAMERA_PRESET.resolution },
        audio_sample_rate: { ...prev.audio_sample_rate, value: LOW_POWER_CAMERA_PRESET.audio_sample_rate },
        audio_chunk_size: { ...prev.audio_chunk_size, value: LOW_POWER_CAMERA_PRESET.audio_chunk_size },
        audio_enabled: { ...prev.audio_enabled, value: LOW_POWER_CAMERA_PRESET.audio_enabled },
      }));
      return;
    }

    if (preset === 'custom') {
      setCameraPreset('custom');
      // Keep current values when switching to custom
      // Or apply last non-custom preset as base for custom settings
      const basePreset = lastNonCustomCameraPreset === 'low_power' ? LOW_POWER_CAMERA_PRESET : DEFAULT_CAMERA_PRESET;
      setCameraSettings(prev => ({
        fps: { ...prev.fps, value: prev.fps.value || basePreset.fps },
        resolution: { ...prev.resolution, value: prev.resolution.value || basePreset.resolution },
        audio_sample_rate: { ...prev.audio_sample_rate, value: prev.audio_sample_rate.value || basePreset.audio_sample_rate },
        audio_chunk_size: { ...prev.audio_chunk_size, value: prev.audio_chunk_size.value || basePreset.audio_chunk_size },
        audio_enabled: { ...prev.audio_enabled, value: prev.audio_enabled.value !== undefined ? prev.audio_enabled.value : basePreset.audio_enabled },
      }));
    }
  };

  const handleCustomCameraValue = (key, value) => {
    setCameraPreset('custom');
    setCameraSettings(prev => ({
      ...prev,
      [key]: { ...prev[key], value }
    }));
  };

  const handleSaveCameraAudio = async (cameraId) => {
    const next = audioByCamera[cameraId];
    if (!next) {
      return;
    }

    setSavingAudioId(cameraId);
    try {
      const response = await api.updateCamera(cameraId, {
        audio_sample_rate: Number(next.audio_sample_rate),
        audio_chunk_size: Number(next.audio_chunk_size),
        fps: Number(next.fps) || 30,
        audio_enabled: Boolean(next.audio_enabled),
      });
      const updatedCamera = response?.data;
      if (updatedCamera && typeof setCameras === 'function') {
        setCameras((prev) => prev.map((camera) => (camera.id === cameraId ? updatedCamera : camera)));
      }
      toast.success('Camera config updated');
    } catch (error) {
      toast.error(`Failed to update camera config: ${error?.response?.data?.detail || error.message}`);
    } finally {
      setSavingAudioId('');
    }
  };

  const handleApplyCameraSettings = async () => {
    setSavingCameraSettings(true);
    try {
      const settingKeys = Object.keys(cameraSettings);
      let updatedCameras = [];

      for (const settingKey of settingKeys) {
        const setting = cameraSettings[settingKey];
        const { target_cameras, value } = setting;

        // Determine which cameras to update
        let targetCameraIds = [];
        if (target_cameras === 'all') {
          targetCameraIds = cameras.map(c => c.id);
        } else if (target_cameras.startsWith('cam')) {
          // Find camera by index (cam1 = first camera, cam2 = second, etc.)
          const cameraIndex = parseInt(target_cameras.replace('cam', '')) - 1;
          if (cameras[cameraIndex]) {
            targetCameraIds = [cameras[cameraIndex].id];
          }
        }

        // Update each target camera
        for (const cameraId of targetCameraIds) {
          try {
            const updatePayload = {};
            if (settingKey === 'fps') {
              updatePayload.fps = Number(value);
            } else if (settingKey === 'resolution') {
              updatePayload.resolution = String(value);
            } else if (settingKey === 'audio_sample_rate') {
              updatePayload.audio_sample_rate = Number(value);
            } else if (settingKey === 'audio_chunk_size') {
              updatePayload.audio_chunk_size = Number(value);
            } else if (settingKey === 'audio_enabled') {
              updatePayload.audio_enabled = Boolean(value);
            }

            const response = await api.updateCamera(cameraId, updatePayload);
            if (response?.data) {
              updatedCameras.push(response.data);
            }
          } catch (error) {
            console.error(`Failed to update ${settingKey} for camera ${cameraId}:`, error);
          }
        }
      }

      // Update the cameras list with the updated cameras
      if (typeof setCameras === 'function' && updatedCameras.length > 0) {
        setCameras((prev) => {
          const updated = [...prev];
          updatedCameras.forEach((updatedCamera) => {
            const index = updated.findIndex(c => c.id === updatedCamera.id);
            if (index !== -1) {
              updated[index] = updatedCamera;
            }
          });
          return updated;
        });
      }

      toast.success('Camera settings applied successfully');
    } catch (error) {
      toast.error(`Failed to apply camera settings: ${error?.message || 'Unknown error'}`);
    } finally {
      setSavingCameraSettings(false);
    }
  };

  return (
    <div>
      <div className="page-header">
        <h1 className="page-title">System Settings</h1>
        <p className="page-subtitle">Live mode, recording playback, performance, and audio configuration</p>
      </div>

      <div className="system-info-grid system-settings-grid">
        <div className="info-card">
          <h3 className="info-title">Stream & Playback</h3>

          <div className="info-item" style={{ alignItems: 'center', opacity: performanceProfile === 'custom' ? 1 : 0.6 }}>
            <span className="info-label">Live stream mode</span>
            <div style={{ display: 'flex', gap: 8 }}>
              <button 
                type="button" 
                className={settings.live_stream_mode === 'mjpeg' ? 'btn btn-primary' : 'btn btn-secondary'} 
                onClick={() => handleCustomPerformanceValue('live_stream_mode', 'mjpeg')}
                disabled={performanceProfile !== 'custom'}
              >
                MJPEG
              </button>
              <button 
                type="button" 
                className={settings.live_stream_mode === 'hls' ? 'btn btn-primary' : 'btn btn-secondary'} 
                onClick={() => handleCustomPerformanceValue('live_stream_mode', 'hls')}
                disabled={performanceProfile !== 'custom'}
              >
                HLS
              </button>
            </div>
          </div>

          <div className="info-item" style={{ alignItems: 'center' }}>
            <span className="info-label">Recording playback</span>
            <div style={{ display: 'flex', gap: 8 }}>
              <button type="button" className={playbackMode === 'play' ? 'btn btn-primary' : 'btn btn-secondary'} onClick={() => handlePlaybackModeChange('play')}>File</button>
              <button type="button" className={playbackMode === 'stream' ? 'btn btn-primary' : 'btn btn-secondary'} onClick={() => handlePlaybackModeChange('stream')}>Stream</button>
            </div>
          </div>
          
          <div className="info-item" style={{ alignItems: 'center', opacity: performanceProfile === 'custom' ? 1 : 0.6 }}>
            <span className="info-label">RTSP Unified Demux</span>
            <div style={{ display: 'flex', gap: 8 }}>
              <button
                type="button"
                className={settings.rtsp_unified_demux_enabled ? 'btn btn-primary' : 'btn btn-secondary'}
                onClick={() => handleCustomPerformanceValue('rtsp_unified_demux_enabled', true)}
                disabled={performanceProfile !== 'custom'}
              >
                Enabled
              </button>
              <button
                type="button"
                className={!settings.rtsp_unified_demux_enabled ? 'btn btn-primary' : 'btn btn-secondary'}
                onClick={() => handleCustomPerformanceValue('rtsp_unified_demux_enabled', false)}
                disabled={performanceProfile !== 'custom'}
              >
                Disabled
              </button>
            </div>
          </div>
        </div>

        <div className="info-card">
          <h3 className="info-title">Motion & Recording</h3>

          <div className="info-item" style={{ ...PARAM_ROW_STYLE, opacity: performanceProfile === 'custom' ? 1 : 0.6 }}>
            <span className="info-label" style={PARAM_LABEL_STYLE}>Max Vel</span>
            <div style={PARAM_CONTROL_STYLE}>
              <input
                type="number"
                min="0"
                max="5"
                step="0.01"
                value={settings.max_vel}
                onChange={(e) => handleCustomPerformanceValue('max_vel', Number(e.target.value || 0))}
                disabled={performanceProfile !== 'custom'}
                style={PARAM_INPUT_STYLE}
              />
              <span className="info-value" style={PARAM_UNIT_STYLE}> </span>
            </div>
          </div>

          <div className="info-item" style={{ ...PARAM_ROW_STYLE, opacity: performanceProfile === 'custom' ? 1 : 0.6 }}>
            <span className="info-label" style={PARAM_LABEL_STYLE}>BG Diff</span>
            <div style={PARAM_CONTROL_STYLE}>
              <input
                type="number"
                min="1"
                max="5000"
                step="1"
                value={settings.bg_diff}
                onChange={(e) => handleCustomPerformanceValue('bg_diff', Number(e.target.value || 1))}
                disabled={performanceProfile !== 'custom'}
                style={PARAM_INPUT_STYLE}
              />
              <span className="info-value" style={PARAM_UNIT_STYLE}> </span>
            </div>
          </div>

          <div className="info-item" style={{ ...PARAM_ROW_STYLE, opacity: performanceProfile === 'custom' ? 1 : 0.6 }}>
            <span className="info-label" style={PARAM_LABEL_STYLE}>Max Clip Length (seconds)</span>
            <div style={PARAM_CONTROL_STYLE}>
              <input
                type="number"
                min="5"
                max="600"
                step="1"
                value={settings.max_clip_length}
                onChange={(e) => handleCustomPerformanceValue('max_clip_length', Number(e.target.value || 5))}
                disabled={performanceProfile !== 'custom'}
                style={PARAM_INPUT_STYLE}
              />
              <span className="info-value" style={PARAM_UNIT_STYLE}>sec</span>
            </div>
          </div>

          <div className="info-item" style={{ ...PARAM_ROW_STYLE, opacity: performanceProfile === 'custom' ? 1 : 0.6 }}>
            <span className="info-label" style={PARAM_LABEL_STYLE}>Motion Check Interval (seconds)</span>
            <div style={PARAM_CONTROL_STYLE}>
              <input
                type="number"
                min="1"
                max="120"
                step="1"
                value={settings.motion_check_interval}
                onChange={(e) => handleCustomPerformanceValue('motion_check_interval', Number(e.target.value || 1))}
                disabled={performanceProfile !== 'custom'}
                style={PARAM_INPUT_STYLE}
              />
              <span className="info-value" style={PARAM_UNIT_STYLE}>sec</span>
            </div>
          </div>

          <div className="info-item" style={{ ...PARAM_ROW_STYLE, opacity: performanceProfile === 'custom' ? 1 : 0.6 }}>
            <span className="info-label" style={PARAM_LABEL_STYLE}>Delete oldest when free storage &lt;</span>
            <div style={PARAM_CONTROL_STYLE}>
              <input
                type="number"
                min="0"
                max="64"
                step="0.5"
                value={Number((settings.min_free_storage_bytes / (1024 ** 3)).toFixed(2))}
                onChange={(e) => handleCustomPerformanceValue('min_free_storage_bytes', Math.round(Number(e.target.value || 0) * 1024 ** 3))}
                disabled={performanceProfile !== 'custom'}
                style={PARAM_INPUT_STYLE}
              />
              <span className="info-value" style={PARAM_UNIT_STYLE}>GiB</span>
            </div>
          </div>

          <div className="info-item" style={{ opacity: performanceProfile === 'custom' ? 1 : 0.8 }}>
            <span className="info-label">Real-time Mux</span>
            <div style={{ display: 'flex', gap: 8 }}>
              <button
                type="button"
                className={settings.mux_realtime ? 'btn btn-primary' : 'btn btn-secondary'}
                onClick={() => handleCustomPerformanceValue('mux_realtime', true)}
                disabled={performanceProfile !== 'custom'}
              >
                Enabled
              </button>
              <button
                type="button"
                className={!settings.mux_realtime ? 'btn btn-primary' : 'btn btn-secondary'}
                onClick={() => handleCustomPerformanceValue('mux_realtime', false)}
                disabled={performanceProfile !== 'custom'}
              >
                Disabled
              </button>
            </div>
          </div>
        </div>

        {/* Performance Profile Card */}
        <div className="info-card">
          <h3 className="info-title">Performance Profile</h3>

          <div className="info-item" style={{ alignItems: 'center' }}>
            <span className="info-label">Profile</span>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
              <button
                type="button"
                className={performanceProfile === 'default' ? 'btn btn-primary' : 'btn btn-secondary'}
                onClick={() => applyPerformanceProfile('default')}
              >
                Default
              </button>
              <button
                type="button"
                className={performanceProfile === 'low_power' ? 'btn btn-primary' : 'btn btn-secondary'}
                onClick={() => applyPerformanceProfile('low_power')}
              >
                Low Power
              </button>
              <button
                type="button"
                className={performanceProfile === 'custom' ? 'btn btn-primary' : 'btn btn-secondary'}
                onClick={() => applyPerformanceProfile('custom')}
              >
                Custom
              </button>
            </div>
          </div>

          <div className="info-item" style={{ ...PARAM_ROW_STYLE, opacity: performanceProfile === 'custom' ? 1 : 0.8 }}>
            <span className="info-label" style={PARAM_LABEL_STYLE}>Sensitivity</span>
            <div style={PARAM_CONTROL_STYLE}>
              <input
                type="range"
                min="0"
                max="5"
                value={settings.sensitivity}
                onChange={(e) => handleCustomPerformanceValue('sensitivity', Number(e.target.value))}
                disabled={performanceProfile !== 'custom'}
                style={PARAM_INPUT_STYLE}
              />
              <span className="info-value" style={PARAM_UNIT_STYLE}>{settings.sensitivity}</span>
            </div>
          </div>

          <div className="info-item" style={{ ...PARAM_ROW_STYLE, opacity: performanceProfile === 'custom' ? 1 : 0.8 }}>
            <span className="info-label" style={PARAM_LABEL_STYLE}>JPEG quality</span>
            <div style={PARAM_CONTROL_STYLE}>
              <input
                type="range"
                min="25"
                max="95"
                value={settings.jpeg_quality}
                onChange={(e) => handleCustomPerformanceValue('jpeg_quality', Number(e.target.value))}
                disabled={performanceProfile !== 'custom'}
                style={PARAM_INPUT_STYLE}
              />
              <span className="info-value" style={PARAM_UNIT_STYLE}>{settings.jpeg_quality}</span>
            </div>
          </div>
        </div>

        {/* System Metrics Card */}
        <div className="info-card">
          <h3 className="info-title">System Metrics</h3>

          <div className="info-item">
            <span className="info-label">CPU Usage</span>
            <span className="info-value">{systemInfo?.cpu_percent ? `${systemInfo.cpu_percent.toFixed(1)}%` : 'N/A'}</span>
          </div>

          <div className="info-item">
            <span className="info-label">Memory Usage</span>
            <span className="info-value">
              {systemInfo?.memory_percent ? 
                `${systemInfo.memory_percent.toFixed(1)}% (${ramGiB ? ramGiB.toFixed(1) : 'N/A'} GB)` : 
                `${ramGiB ? `${ramGiB.toFixed(2)} GB` : 'N/A'}`
              }
            </span>
          </div>

          <div className="info-item">
            <span className="info-label">Active Cameras</span>
            <span className="info-value">{cameras.filter(c => c.status === 'online').length} / {cameras.length}</span>
          </div>

          <div className="info-item">
            <span className="info-label">Recording Cameras</span>
            <span className="info-value">{cameras.filter(c => c.is_recording).length}</span>
          </div>

          <div className="info-item">
            <span className="info-label">Disk Usage</span>
            <span className="info-value">{systemInfo?.disk_usage_percent ? `${systemInfo.disk_usage_percent.toFixed(1)}%` : 'N/A'}</span>
          </div>

          <div className="info-item">
            <span className="info-label">System Uptime</span>
            <span className="info-value">
              {systemInfo?.uptime ? 
                (typeof systemInfo.uptime === 'object' ? systemInfo.uptime.text || 'N/A' : systemInfo.uptime) : 
                'N/A'
              }
            </span>
          </div>
        </div>

        {/* Advanced Performance Card */}
        <div className="info-card">
          <h3 className="info-title">Advanced Performance</h3>

          <div className="info-item" style={{ ...PARAM_ROW_STYLE, opacity: performanceProfile === 'custom' ? 1 : 0.6 }}>
            <span className="info-label" style={PARAM_LABEL_STYLE}>Pipe buffer size</span>
            <div style={PARAM_CONTROL_STYLE}>
              <input
                type="number"
                min="65536"
                max="268435456"
                step="65536"
                value={settings.pipe_buffer_size}
                onChange={(e) => handleCustomPerformanceValue('pipe_buffer_size', Number(e.target.value || 65536))}
                disabled={performanceProfile !== 'custom'}
                style={PARAM_INPUT_STYLE}
              />
              <span className="info-value" style={PARAM_UNIT_STYLE}>bytes</span>
            </div>
          </div>

          <div className="info-item" style={{ ...PARAM_ROW_STYLE, opacity: performanceProfile === 'custom' ? 1 : 0.6 }}>
            <span className="info-label" style={PARAM_LABEL_STYLE}>Frame RBF Length</span>
            <div style={PARAM_CONTROL_STYLE}>
              <input
                type="number"
                min="1"
                max="100"
                step="1"
                value={settings.frame_rbf_len || 10}
                onChange={(e) => handleCustomPerformanceValue('frame_rbf_len', Number(e.target.value || 10))}
                disabled={performanceProfile !== 'custom'}
                style={PARAM_INPUT_STYLE}
              />
              <span className="info-value" style={PARAM_UNIT_STYLE}>frames</span>
            </div>
          </div>

          <div className="info-item" style={{ ...PARAM_ROW_STYLE, opacity: performanceProfile === 'custom' ? 1 : 0.6 }}>
            <span className="info-label" style={PARAM_LABEL_STYLE}>Audio RBF Length</span>
            <div style={PARAM_CONTROL_STYLE}>
              <input
                type="number"
                min="1"
                max="100"
                step="1"
                value={settings.audio_rbf_len || 10}
                onChange={(e) => handleCustomPerformanceValue('audio_rbf_len', Number(e.target.value || 10))}
                disabled={performanceProfile !== 'custom'}
                style={PARAM_INPUT_STYLE}
              />
              <span className="info-value" style={PARAM_UNIT_STYLE}>chunks</span>
            </div>
          </div>

          <div className="info-item" style={{ ...PARAM_ROW_STYLE, opacity: performanceProfile === 'custom' ? 1 : 0.6 }}>
            <span className="info-label" style={PARAM_LABEL_STYLE}>Results RBF Length</span>
            <div style={PARAM_CONTROL_STYLE}>
              <input
                type="number"
                min="1"
                max="100"
                step="1"
                value={settings.results_rbf_len || 10}
                onChange={(e) => handleCustomPerformanceValue('results_rbf_len', Number(e.target.value || 10))}
                disabled={performanceProfile !== 'custom'}
                style={PARAM_INPUT_STYLE}
              />
              <span className="info-value" style={PARAM_UNIT_STYLE}>results</span>
            </div>
          </div>
        </div>
      </div>

      {/* Save System Settings Button */}
      <div style={{ marginTop: 16, display: 'flex', justifyContent: 'flex-end' }}>
        <button type="button" className="btn btn-primary" disabled={saving || loading} onClick={async () => {
          const data = await handleSaveSystemSettings();
          if (data) toast.success('System settings saved');
        }}>
          {saving ? 'Saving...' : 'Save System Settings'}
        </button>
      </div>

      <div className="content-section">
        <div className="section-header">
          <h2 className="section-title" style={{ color: '#004d40' }}>Camera-Specific Settings</h2>
        </div>
        <div className="info-card">
          {/* Preset Control Buttons */}
          <div style={{ marginBottom: '24px', borderBottom: '2px solid rgba(0, 150, 136, 0.2)', paddingBottom: '16px' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
              <strong style={{ color: '#004d40', fontSize: '16px' }}>Camera Preset</strong>
            </div>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              <button
                type="button"
                className={cameraPreset === 'default' ? 'btn btn-primary' : 'btn btn-secondary'}
                onClick={() => applyCameraPreset('default')}
              >
                Default
              </button>
              <button
                type="button"
                className={cameraPreset === 'low_power' ? 'btn btn-primary' : 'btn btn-secondary'}
                onClick={() => applyCameraPreset('low_power')}
              >
                Low Power
              </button>
              <button
                type="button"
                className={cameraPreset === 'custom' ? 'btn btn-primary' : 'btn btn-secondary'}
                onClick={() => applyCameraPreset('custom')}
              >
                Custom
              </button>
            </div>
          </div>

          <div style={{ display: 'grid', gap: '20px' }}>
            
            {/* First Row: FPS and Resolution */}
            <div style={{ borderBottom: '1px solid rgba(0, 150, 136, 0.12)', paddingBottom: '16px', opacity: cameraPreset === 'custom' ? 1 : 0.6 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
                <strong style={{ color: '#004d40' }}>Video Settings</strong>
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>
                {/* FPS Setting */}
                <div>
                  <div style={{ marginBottom: 8 }}>
                    <strong style={{ color: '#004d40', fontSize: '14px' }}>Frame Rate (FPS)</strong>
                  </div>
                  <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
                    <select
                      value={cameraSettings.fps.target_cameras}
                      onChange={(e) => setCameraSettings(prev => ({
                        ...prev,
                        fps: { ...prev.fps, target_cameras: e.target.value }
                      }))}
                      disabled={cameraPreset !== 'custom'}
                      style={{ minWidth: '100px', fontSize: '12px' }}
                    >
                      {CAMERA_TARGET_OPTIONS.map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                    <select
                      value={cameraSettings.fps.value}
                      onChange={(e) => handleCustomCameraValue('fps', Number(e.target.value))}
                      disabled={cameraPreset !== 'custom'}
                      style={{ minWidth: '90px', fontSize: '12px' }}
                    >
                      {FPS_OPTIONS.map((value) => (
                        <option key={value} value={value}>
                          {value} FPS
                        </option>
                      ))}
                    </select>
                  </div>
                </div>
                {/* Resolution Setting */}
                <div>
                  <div style={{ marginBottom: 8 }}>
                    <strong style={{ color: '#004d40', fontSize: '14px' }}>Resolution</strong>
                  </div>
                  <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
                    <select
                      value={cameraSettings.resolution.target_cameras}
                      onChange={(e) => setCameraSettings(prev => ({
                        ...prev,
                        resolution: { ...prev.resolution, target_cameras: e.target.value }
                      }))}
                      disabled={cameraPreset !== 'custom'}
                      style={{ minWidth: '100px', fontSize: '12px' }}
                    >
                      {CAMERA_TARGET_OPTIONS.map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                    <select
                      value={cameraSettings.resolution.value}
                      onChange={(e) => handleCustomCameraValue('resolution', e.target.value)}
                      disabled={cameraPreset !== 'custom'}
                      style={{ minWidth: '140px', fontSize: '12px' }}
                    >
                      {RESOLUTION_OPTIONS.map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  </div>
                </div>
              </div>
            </div>

            {/* Second Row: All Audio Settings Side by Side */}
            <div style={{ paddingBottom: '8px', opacity: cameraPreset === 'custom' ? 1 : 0.8 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
                <strong style={{ color: '#004d40' }}>Audio Settings</strong>
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '12px' }}>
                {/* Audio Sample Rate */}
                <div>
                  <div style={{ marginBottom: 8 }}>
                    <strong style={{ color: '#004d40', fontSize: '14px' }}>Sample Rate</strong>
                  </div>
                  <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
                    <select
                      value={cameraSettings.audio_sample_rate.target_cameras}
                      onChange={(e) => setCameraSettings(prev => ({
                        ...prev,
                        audio_sample_rate: { ...prev.audio_sample_rate, target_cameras: e.target.value }
                      }))}
                      disabled={cameraPreset !== 'custom'}
                      style={{ minWidth: '90px', fontSize: '12px' }}
                    >
                      {CAMERA_TARGET_OPTIONS.map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                    <select
                      value={cameraSettings.audio_sample_rate.value}
                      onChange={(e) => handleCustomCameraValue('audio_sample_rate', Number(e.target.value))}
                      disabled={cameraPreset !== 'custom'}
                      style={{ minWidth: '90px', fontSize: '12px' }}
                    >
                      {AUDIO_SAMPLE_RATE_OPTIONS.map((value) => (
                        <option key={value} value={value}>
                          {value} Hz
                        </option>
                      ))}
                    </select>
                  </div>
                </div>
                {/* Audio Chunk Size */}
                <div>
                  <div style={{ marginBottom: 8 }}>
                    <strong style={{ color: '#004d40', fontSize: '14px' }}>Chunk Size</strong>
                  </div>
                  <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
                    <select
                      value={cameraSettings.audio_chunk_size.target_cameras}
                      onChange={(e) => setCameraSettings(prev => ({
                        ...prev,
                        audio_chunk_size: { ...prev.audio_chunk_size, target_cameras: e.target.value }
                      }))}
                      disabled={cameraPreset !== 'custom'}
                      style={{ minWidth: '90px', fontSize: '12px' }}
                    >
                      {CAMERA_TARGET_OPTIONS.map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                    <select
                      value={cameraSettings.audio_chunk_size.value}
                      onChange={(e) => handleCustomCameraValue('audio_chunk_size', Number(e.target.value))}
                      disabled={cameraPreset !== 'custom'}
                      style={{ minWidth: '100px', fontSize: '12px' }}
                    >
                      {AUDIO_CHUNK_SIZE_OPTIONS.map((value) => (
                        <option key={value} value={value}>
                          {value} samples
                        </option>
                      ))}
                    </select>
                  </div>
                </div>
                {/* Audio Enabled */}
                <div>
                  <div style={{ marginBottom: 8 }}>
                    <strong style={{ color: '#004d40', fontSize: '14px' }}>Audio Enabled</strong>
                  </div>
                  <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
                    <select
                      value={cameraSettings.audio_enabled.target_cameras}
                      onChange={(e) => setCameraSettings(prev => ({
                        ...prev,
                        audio_enabled: { ...prev.audio_enabled, target_cameras: e.target.value }
                      }))}
                      disabled={cameraPreset !== 'custom'}
                      style={{ minWidth: '90px', fontSize: '12px' }}
                    >
                      {CAMERA_TARGET_OPTIONS.map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                    <select
                      value={cameraSettings.audio_enabled.value}
                      onChange={(e) => handleCustomCameraValue('audio_enabled', e.target.value === 'true')}
                      disabled={cameraPreset !== 'custom'}
                      style={{ minWidth: '90px', fontSize: '12px' }}
                    >
                      <option value={true}>Enabled</option>
                      <option value={false}>Disabled</option>
                    </select>
                  </div>
                </div>
              </div>
            </div>



          </div>
        </div>
      </div>

      <div style={{ marginTop: 16, display: 'flex', justifyContent: 'flex-end' }}>
        <button type="button" className="btn btn-primary" disabled={savingCameraSettings || cameraPreset !== 'custom'} onClick={handleApplyCameraSettings}>
          {savingCameraSettings ? 'Applying...' : 'Apply Camera Settings'}
        </button>
      </div>
    </div>
  );
};

export default SystemSettings;