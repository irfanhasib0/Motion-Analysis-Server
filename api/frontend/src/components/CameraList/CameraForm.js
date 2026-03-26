import React, { useState } from 'react';

export const DEFAULT_CAMERA_FORM = {
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

export const COMPACT_BUTTON_STYLE = { padding: '6px 10px', fontSize: '12px', lineHeight: 1.1 };



export const AUDIO_SAMPLE_RATE_OPTIONS = [
  { value: 16000, label: '16000 (Recommended balance)' },
  { value: 22050, label: '22050 (Clearer voice)' },
  { value: 32000, label: '32000 (Higher clarity)' },
  { value: 44100, label: '44100 (High quality)' },
  { value: 48000, label: '48000 (Studio / highest quality)' },
  { value: 8000, label: '8000 (Very low bandwidth)' },
];

export const AUDIO_CHUNK_SIZE_OPTIONS = [
  { value: 256, label: '256 (Low latency, more CPU)' },
  { value: 512, label: '512 (Recommended balance)' },
  { value: 1024, label: '1024 (Stable, higher latency)' },
  { value: 2048, label: '2048 (Very stable, high latency)' },
  { value: 4096, label: '4096 (Maximum stability, highest latency)' },
];

export const AUDIO_INPUT_FORMAT_OPTIONS = [
  { value: 'pulse', label: 'pulse' },
  { value: 'alsa', label: 'alsa' },
];

export const AUDIO_SOURCE_OPTIONS = [
  { value: 'default', label: 'default' },
  { value: 'alsa_input.pci-0000_00_1f.3.analog-stereo', label: 'alsa_input (Intel PCH)' },
  { value: 'hw:1,0', label: 'hw:1,0 (ALSA direct)' },
];

export const AUDIO_LATENCY_PROFILES = {
  low: { audio_sample_rate: 16000, audio_chunk_size: 256 },
  balanced: { audio_sample_rate: 16000, audio_chunk_size: 512 },
  stable: { audio_sample_rate: 22050, audio_chunk_size: 1024 },
};



export const CameraForm = ({ initialCamera, onSubmit, onClose, onBrowse, title, submitText }) => {
  const [form, setForm] = useState(initialCamera);

  const handleSubmit = async (e) => {
    e.preventDefault();
    await onSubmit(form);
  };

  return (
    <div className="modal-overlay" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal">
        <div className="modal-header">
          <h3 className="modal-title">{title}</h3>
          <button className="modal-close" onClick={onClose}>×</button>
        </div>

        <form onSubmit={handleSubmit}>
          <div className="modal-body">
            <div className="form-group">
              <label className="form-label">Name</label>
              <input
                type="text"
                className="form-control"
                value={form.name}
                onChange={(e) => setForm(p => ({...p, name: e.target.value}))}
                required
              />
            </div>

            <div className="form-group">
              <label className="form-label">Camera Type</label>
              <select
                className="form-control form-select"
                value={form.camera_type}
                onChange={(e) => setForm(p => ({...p, camera_type: e.target.value}))}
              >
                <option value="recorded">Recorded Data</option>
                <option value="rtsp">RTSP Stream</option>
                <option value="webcam">Webcam</option>
                <option value="ip_camera">IP Camera</option>
              </select>
            </div>

            <div className="form-group">
              <label className="form-label">Source</label>
              <div style={{ display: 'flex', gap: '6px' }}>
                <input
                  type="text"
                  className="form-control"
                  style={{ flex: 1 }}
                  value={form.source}
                  onChange={(e) => setForm(p => ({...p, source: e.target.value}))}
                  placeholder={
                    form.camera_type === 'recorded' ? '/path/to/video/file.mp4' :
                    form.camera_type === 'rtsp' ? 'rtsp://username:password@ip:port/path' :
                    form.camera_type === 'webcam' ? '0' :
                    'http://ip:port/video'
                  }
                  required
                />
                {form.camera_type === 'recorded' && (
                  <button
                    type="button"
                    className="btn btn-secondary"
                    style={{ whiteSpace: 'nowrap', ...COMPACT_BUTTON_STYLE }}
                    onClick={() => onBrowse((path) => setForm(p => ({...p, source: path})))}
                  >
                    Browse
                  </button>
                )}
              </div>
            </div>

            <div className="grid grid-2">
              <div className="form-group">
                <label className="form-label">Resolution</label>
                <select
                  className="form-control form-select"
                  value={form.resolution}
                  onChange={(e) => setForm(p => ({...p, resolution: e.target.value}))}
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
                  onChange={(e) => setForm(p => ({...p, fps: parseInt(e.target.value)}))}
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
                onChange={(e) => setForm(p => ({...p, location: e.target.value}))}
                placeholder="e.g., Front Door, Parking Lot"
              />
            </div>

            <div className="form-group">
              <label className="form-label">Description</label>
              <textarea
                className="form-control"
                value={form.description || ''}
                onChange={(e) => setForm(p => ({...p, description: e.target.value}))}
                rows="3"
                placeholder="Optional description"
              />
            </div>

            <div className="form-group">
              <label className="form-label">
                <input
                  type="checkbox"
                  checked={form.enabled ?? true}
                  onChange={(e) => setForm(p => ({...p, enabled: e.target.checked}))}
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
                  onChange={(e) => setForm(p => ({...p, audio_enabled: e.target.checked}))}
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
                        onChange={(e) => setForm(p => ({...p, audio_input_format: e.target.value}))}
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
                            setForm(p => ({ ...p, audio_source: e.target.value }));
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
                          onChange={(e) => setForm(p => ({ ...p, audio_source: e.target.value }))}
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
                      if (!profile) return;
                      setForm(p => ({ ...p, ...profile }));
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
                        setForm(p => ({ ...p, audio_sample_rate: Number(value) }));
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
                      onChange={(e) => setForm(p => ({ ...p, audio_sample_rate: parseInt(e.target.value, 10) || 16000 }))}
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
                        setForm(p => ({ ...p, audio_chunk_size: Number(value) }));
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
                      onChange={(e) => setForm(p => ({ ...p, audio_chunk_size: parseInt(e.target.value, 10) || 512 }))}
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
              onClick={onClose}
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
