import React, { useEffect, useMemo, useState } from 'react';
import { toast } from 'react-hot-toast';
import { api } from '../api';

const AUDIO_SAMPLE_RATE_OPTIONS = [8000, 16000, 22050, 32000, 44100, 48000];
const AUDIO_CHUNK_SIZE_OPTIONS = [256, 512, 1024, 2048, 4096];
const DEFAULT_PERFORMANCE_PRESET = {
  low_power_mode: false,
  sensitivity: 4,
  jpeg_quality: 70,
  pipe_buffer_size: 100000000,
};
const LOW_POWER_PERFORMANCE_PRESET = {
  low_power_mode: true,
  sensitivity: 2,
  jpeg_quality: 55,
  pipe_buffer_size: 1000000,
};

const inferPerformanceProfile = (current) => {
  const lowPower = Boolean(current?.low_power_mode);
  const sensitivity = Number(current?.sensitivity ?? DEFAULT_PERFORMANCE_PRESET.sensitivity);
  const jpeg = Number(current?.jpeg_quality || 70);
  const pipeBufferSize = Number(current?.pipe_buffer_size || DEFAULT_PERFORMANCE_PRESET.pipe_buffer_size);

  if (
    lowPower === LOW_POWER_PERFORMANCE_PRESET.low_power_mode
    && sensitivity === LOW_POWER_PERFORMANCE_PRESET.sensitivity
    && jpeg === LOW_POWER_PERFORMANCE_PRESET.jpeg_quality
    && pipeBufferSize === LOW_POWER_PERFORMANCE_PRESET.pipe_buffer_size
  ) {
    return 'low_power';
  }

  if (
    lowPower === DEFAULT_PERFORMANCE_PRESET.low_power_mode
    && sensitivity === DEFAULT_PERFORMANCE_PRESET.sensitivity
    && jpeg === DEFAULT_PERFORMANCE_PRESET.jpeg_quality
    && pipeBufferSize === DEFAULT_PERFORMANCE_PRESET.pipe_buffer_size
  ) {
    return 'default';
  }

  return 'custom';
};

const PARAM_ROW_STYLE = { alignItems: 'center' };
const PARAM_LABEL_STYLE = { minWidth: 180 };
const PARAM_CONTROL_STYLE = { display: 'flex', alignItems: 'center', gap: 8, marginLeft: 'auto' };
const PARAM_INPUT_STYLE = { width: 132 };
const PARAM_UNIT_STYLE = { minWidth: 34, textAlign: 'right' };

const SystemSettings = ({ systemInfo, cameras = [], setCameras }) => {
  const [settings, setSettings] = useState({
    live_stream_mode: 'mjpeg',
    low_power_mode: false,
    sensitivity: DEFAULT_PERFORMANCE_PRESET.sensitivity,
    jpeg_quality: 70,
    pipe_buffer_size: DEFAULT_PERFORMANCE_PRESET.pipe_buffer_size,
    max_vel: 0.1,
    bg_diff: 50,
    max_clip_length: 60,
    motion_check_interval: 10,
    min_free_storage_bytes: 1 * 1024 * 1024 * 1024,
    uvicorn_reload: true,
    total_memory_bytes: 0,
    low_power_ram_threshold_bytes: 1024 * 1024 * 1024,
    ram_auto_low_power_enabled: true,
  });
  const [playbackMode, setPlaybackMode] = useState(api.getRecordingPlaybackMode());
  const [audioByCamera, setAudioByCamera] = useState({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [savingAudioId, setSavingAudioId] = useState('');
  const [performanceProfile, setPerformanceProfile] = useState('default');

  useEffect(() => {
    const map = {};
    cameras.forEach((camera) => {
      map[camera.id] = {
        audio_sample_rate: Number(camera.audio_sample_rate),
        audio_chunk_size: Number(camera.audio_chunk_size),
        fps: Number(camera.fps) || 30,
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
          low_power_mode: Boolean(data.low_power_mode),
          sensitivity: Number(data.sensitivity ?? DEFAULT_PERFORMANCE_PRESET.sensitivity),
          jpeg_quality: Number(data.jpeg_quality || 70),
          pipe_buffer_size: Number(data.pipe_buffer_size || DEFAULT_PERFORMANCE_PRESET.pipe_buffer_size),
          max_vel: Number(data.max_vel ?? 0.1),
          bg_diff: Number(data.bg_diff ?? 50),
          max_clip_length: Number(data.max_clip_length ?? 60),
          motion_check_interval: Number(data.motion_check_interval ?? 10),
          min_free_storage_bytes: Number(data.min_free_storage_bytes ?? 1 * 1024 * 1024 * 1024),
          uvicorn_reload: Boolean(data.uvicorn_reload),
          total_memory_bytes: Number(data.total_memory_bytes || 0),
          low_power_ram_threshold_bytes: Number(data.low_power_ram_threshold_bytes || prev.low_power_ram_threshold_bytes),
          ram_auto_low_power_enabled: Boolean(data.ram_auto_low_power_enabled),
        }));
        setPerformanceProfile(inferPerformanceProfile(data));
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
        low_power_mode: Boolean(settings.low_power_mode),
        sensitivity: Number(settings.sensitivity),
        jpeg_quality: Number(settings.jpeg_quality),
        pipe_buffer_size: Number(settings.pipe_buffer_size),
        max_vel: Number(settings.max_vel),
        bg_diff: Number(settings.bg_diff),
        max_clip_length: Number(settings.max_clip_length),
        motion_check_interval: Number(settings.motion_check_interval),
        min_free_storage_bytes: Number(settings.min_free_storage_bytes),
        uvicorn_reload: Boolean(settings.uvicorn_reload),
      };
      const response = await api.updateSystemSettings(payload);
      const data = response?.data || {};
      setSettings((prev) => ({
        ...prev,
        live_stream_mode: data.live_stream_mode === 'hls' ? 'hls' : 'mjpeg',
        low_power_mode: Boolean(data.low_power_mode),
        sensitivity: Number(data.sensitivity ?? prev.sensitivity),
        jpeg_quality: Number(data.jpeg_quality || prev.jpeg_quality),
        pipe_buffer_size: Number(data.pipe_buffer_size || prev.pipe_buffer_size),
        max_vel: Number(data.max_vel ?? prev.max_vel),
        bg_diff: Number(data.bg_diff ?? prev.bg_diff),
        max_clip_length: Number(data.max_clip_length ?? prev.max_clip_length),
        motion_check_interval: Number(data.motion_check_interval ?? prev.motion_check_interval),
        min_free_storage_bytes: Number(data.min_free_storage_bytes ?? prev.min_free_storage_bytes),
        uvicorn_reload: Boolean(data.uvicorn_reload),
      }));
      setPerformanceProfile(inferPerformanceProfile(data));
      return data;
    } catch (error) {
      toast.error(`Failed to update system settings: ${error?.response?.data?.detail || error.message}`);
      return null;
    } finally {
      setSaving(false);
    }
  };

  const handleApplySystemSettings = async () => {
    const data = await handleSaveSystemSettings();
    if (!data) {
      return;
    }

    if (typeof setCameras === 'function') {
      try {
        const camerasRes = await api.getCameras();
        setCameras(camerasRes?.data || []);
      } catch (error) {
        toast.error(`Settings applied, but failed to refresh cameras: ${error?.response?.data?.detail || error.message}`);
        return;
      }
    }

    if (data.restart_required) {
      toast.success('Applied to cameras (restart required for uvicorn reload change)');
    } else {
      toast.success('Applied to cameras');
    }
  };

  const handleSaveSystemSettingsOnly = async () => {
    const data = await handleSaveSystemSettings();
    if (!data) {
      return;
    }
    if (data.restart_required) {
      toast.success('System settings updated (restart required for uvicorn reload change)');
    } else {
      toast.success('System settings updated');
    }
  };

  const applyPerformanceProfile = (profile) => {
    if (profile === 'default') {
      setPerformanceProfile('default');
      setSettings((prev) => ({ ...prev, ...DEFAULT_PERFORMANCE_PRESET }));
      return;
    }

    if (profile === 'low_power') {
      setPerformanceProfile('low_power');
      setSettings((prev) => ({ ...prev, ...LOW_POWER_PERFORMANCE_PRESET }));
      return;
    }

    setPerformanceProfile('custom');
  };

  const handleCustomPerformanceValue = (key, value) => {
    setPerformanceProfile('custom');
    setSettings((prev) => ({ ...prev, [key]: value }));
  };

  const handlePlaybackModeChange = (mode) => {
    const normalized = mode === 'stream' ? 'stream' : 'play';
    api.setRecordingPlaybackMode(normalized);
    setPlaybackMode(normalized);
    toast.success(`Playback mode set to ${normalized}`);
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
      });
      const updatedCamera = response?.data;
      if (updatedCamera && typeof setCameras === 'function') {
        setCameras((prev) => prev.map((camera) => (camera.id === cameraId ? updatedCamera : camera)));
      }
      toast.success('Camera audio config updated');
    } catch (error) {
      toast.error(`Failed to update camera audio config: ${error?.response?.data?.detail || error.message}`);
    } finally {
      setSavingAudioId('');
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

          <div className="info-item" style={{ alignItems: 'center' }}>
            <span className="info-label">Live stream mode</span>
            <div style={{ display: 'flex', gap: 8 }}>
              <button type="button" className={settings.live_stream_mode === 'mjpeg' ? 'btn btn-primary' : 'btn btn-secondary'} onClick={() => setSettings((prev) => ({ ...prev, live_stream_mode: 'mjpeg' }))}>MJPEG</button>
              <button type="button" className={settings.live_stream_mode === 'hls' ? 'btn btn-primary' : 'btn btn-secondary'} onClick={() => setSettings((prev) => ({ ...prev, live_stream_mode: 'hls' }))}>HLS</button>
            </div>
          </div>

          <div className="info-item" style={{ alignItems: 'center' }}>
            <span className="info-label">Recording playback</span>
            <div style={{ display: 'flex', gap: 8 }}>
              <button type="button" className={playbackMode === 'play' ? 'btn btn-primary' : 'btn btn-secondary'} onClick={() => handlePlaybackModeChange('play')}>File</button>
              <button type="button" className={playbackMode === 'stream' ? 'btn btn-primary' : 'btn btn-secondary'} onClick={() => handlePlaybackModeChange('stream')}>Stream</button>
            </div>
          </div>

          <div className="info-item" style={{ alignItems: 'center' }}>
            <span className="info-label">uvicorn reload</span>
            <div style={{ display: 'flex', gap: 8 }}>
              <button
                type="button"
                className={settings.uvicorn_reload ? 'btn btn-primary' : 'btn btn-secondary'}
                onClick={() => setSettings((prev) => ({ ...prev, uvicorn_reload: true }))}
              >
                Enabled
              </button>
              <button
                type="button"
                className={!settings.uvicorn_reload ? 'btn btn-primary' : 'btn btn-secondary'}
                onClick={() => setSettings((prev) => ({ ...prev, uvicorn_reload: false }))}
              >
                Disabled
              </button>
            </div>
          </div>
        </div>

        <div className="info-card">
          <h3 className="info-title">Motion & Recording</h3>

          <div className="info-item" style={PARAM_ROW_STYLE}>
            <span className="info-label" style={PARAM_LABEL_STYLE}>Max Vel</span>
            <div style={PARAM_CONTROL_STYLE}>
              <input
                type="number"
                min="0"
                max="5"
                step="0.01"
                value={settings.max_vel}
                onChange={(e) => setSettings((prev) => ({ ...prev, max_vel: Number(e.target.value || 0) }))}
                style={PARAM_INPUT_STYLE}
              />
              <span className="info-value" style={PARAM_UNIT_STYLE}> </span>
            </div>
          </div>

          <div className="info-item" style={PARAM_ROW_STYLE}>
            <span className="info-label" style={PARAM_LABEL_STYLE}>BG Diff</span>
            <div style={PARAM_CONTROL_STYLE}>
              <input
                type="number"
                min="1"
                max="5000"
                step="1"
                value={settings.bg_diff}
                onChange={(e) => setSettings((prev) => ({ ...prev, bg_diff: Number(e.target.value || 1) }))}
                style={PARAM_INPUT_STYLE}
              />
              <span className="info-value" style={PARAM_UNIT_STYLE}> </span>
            </div>
          </div>

          <div className="info-item" style={PARAM_ROW_STYLE}>
            <span className="info-label" style={PARAM_LABEL_STYLE}>Max Clip Length (seconds)</span>
            <div style={PARAM_CONTROL_STYLE}>
              <input
                type="number"
                min="5"
                max="600"
                step="1"
                value={settings.max_clip_length}
                onChange={(e) => setSettings((prev) => ({ ...prev, max_clip_length: Number(e.target.value || 5) }))}
                style={PARAM_INPUT_STYLE}
              />
              <span className="info-value" style={PARAM_UNIT_STYLE}>sec</span>
            </div>
          </div>

          <div className="info-item" style={PARAM_ROW_STYLE}>
            <span className="info-label" style={PARAM_LABEL_STYLE}>Motion Check Interval (seconds)</span>
            <div style={PARAM_CONTROL_STYLE}>
              <input
                type="number"
                min="1"
                max="120"
                step="1"
                value={settings.motion_check_interval}
                onChange={(e) => setSettings((prev) => ({ ...prev, motion_check_interval: Number(e.target.value || 1) }))}
                style={PARAM_INPUT_STYLE}
              />
              <span className="info-value" style={PARAM_UNIT_STYLE}>sec</span>
            </div>
          </div>

          <div className="info-item" style={PARAM_ROW_STYLE}>
            <span className="info-label" style={PARAM_LABEL_STYLE}>Delete oldest when free storage &lt;</span>
            <div style={PARAM_CONTROL_STYLE}>
              <input
                type="number"
                min="0"
                max="64"
                step="0.5"
                value={Number((settings.min_free_storage_bytes / (1024 ** 3)).toFixed(2))}
                onChange={(e) => setSettings((prev) => ({ ...prev, min_free_storage_bytes: Math.round(Number(e.target.value || 0) * 1024 ** 3) }))}
                style={PARAM_INPUT_STYLE}
              />
              <span className="info-value" style={PARAM_UNIT_STYLE}>GiB</span>
            </div>
          </div>
        </div>

        <div className="info-card">
          <h3 className="info-title">Performance</h3>

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
                Low power mode
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

          <div className="info-item" style={PARAM_ROW_STYLE}>
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

          <div className="info-item" style={PARAM_ROW_STYLE}>
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

          <div className="info-item" style={PARAM_ROW_STYLE}>
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

          <div className="info-item">
            <span className="info-label">Detected RAM</span>
            <span className="info-value">{ramGiB ? `${ramGiB.toFixed(2)} GB` : 'N/A'}</span>
          </div>

          <div className="info-item">
            <span className="info-label">Auto low-power rule</span>
            <span className="info-value">{settings.ram_auto_low_power_enabled ? 'Enabled (≤ 1GB)' : 'Disabled'}</span>
          </div>
        </div>
      </div>

      <div className="content-section">
        <div className="section-header">
          <h2 className="section-title" style={{ color: '#004d40' }}>Per-Camera Audio Config</h2>
        </div>
        <div className="info-card">
          {cameras.length === 0 ? (
            <div className="info-item">
              <span className="info-label">No cameras found</span>
              <span className="info-value">Add a camera to configure audio</span>
            </div>
          ) : (
            cameras.map((camera) => {
              const audio = audioByCamera[camera.id] || {
                audio_sample_rate: Number(camera.audio_sample_rate),
                audio_chunk_size: Number(camera.audio_chunk_size),
              };
              return (
                <div key={camera.id} style={{ borderBottom: '1px solid rgba(0, 150, 136, 0.12)', padding: '12px 0' }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
                    <strong style={{ color: '#004d40' }}>{camera.name}</strong>
                    <span style={{ color: '#546e7a', fontSize: 12 }}>{camera.id}</span>
                  </div>

                  <div style={{ display: 'flex', gap: 20, alignItems: 'center', flexWrap: 'wrap' }}>
                    <select
                      value={audio.audio_sample_rate}
                      style={{ width: '200px', minWidth: '140px', display: 'inline-block' }}
                      onChange={(e) => setAudioByCamera((prev) => ({
                        ...prev,
                        [camera.id]: { ...audio, audio_sample_rate: Number(e.target.value) },
                      }))}
                    >
                      {AUDIO_SAMPLE_RATE_OPTIONS.map((value) => (
                        <option key={`${camera.id}:sr:${value}`} value={value}>
                          Sample rate: {value}
                        </option>
                      ))}
                    </select>

                    <select
                      value={audio.audio_chunk_size}
                      style={{ width: '200px', minWidth: '140px', display: 'inline-block' }}
                      onChange={(e) => setAudioByCamera((prev) => ({
                        ...prev,
                        [camera.id]: { ...audio, audio_chunk_size: Number(e.target.value) },
                      }))}
                    >
                      {AUDIO_CHUNK_SIZE_OPTIONS.map((value) => (
                        <option key={`${camera.id}:cs:${value}`} value={value}>
                          Chunk size: {value}
                        </option>
                      ))}
                    </select>

                    <div style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
                      <label htmlFor={`fps-${camera.id}`} style={{ fontSize: 13, color: '#004d40', marginRight: 4 }}>FPS</label>
                      <input
                        id={`fps-${camera.id}`}
                        type="number"
                        min="1"
                        max="60"
                        value={audio.fps ?? camera.fps ?? 30}
                        onChange={e => {
                          const fps = parseInt(e.target.value, 10) || 30;
                          setAudioByCamera(prev => ({
                            ...prev,
                            [camera.id]: { ...audio, fps },
                          }));
                        }}
                        style={{ width: '80px', minWidth: '60px', display: 'inline-block', background: '#fff', border: '1px solid #b0bec5', borderRadius: 6, padding: '2px 6px', fontSize: 13 }}
                      />
                    </div>

                    <button
                      type="button"
                      className="btn btn-secondary"
                      disabled={savingAudioId === camera.id}
                      onClick={() => handleSaveCameraAudio(camera.id)}
                    >
                      {savingAudioId === camera.id ? 'Saving...' : 'Save'}
                    </button>
                  </div>
                </div>
              );
            })
          )}
        </div>
      </div>

      <div style={{ marginTop: 16, display: 'flex', justifyContent: 'flex-end' }}>
        <button type="button" className="btn btn-secondary" disabled={saving || loading} onClick={handleApplySystemSettings} style={{ marginRight: 8 }}>
          {saving ? 'Applying...' : 'Apply'}
        </button>
        <button type="button" className="btn btn-primary" disabled={saving || loading} onClick={handleSaveSystemSettingsOnly}>
          {saving ? 'Saving...' : 'Save System Settings'}
        </button>
      </div>
    </div>
  );
};

export default SystemSettings;