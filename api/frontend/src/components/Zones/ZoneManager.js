/**
 * ZoneManager — always-visible zone editor.
 *
 * Layout:
 *   ┌─ zone chips (+ New | Zone A ✕ | Zone B ✕ | …) ─────────────────────┐
 *   ├─ form bar (name · color · type · hit mode · dwell · ▶/■ camera) ────┤
 *   └─ ZoneEditor canvas with left sidebar ──────────────────────────────┘
 *
 * Props:
 *   camera     {object}  Camera object (.id, .name, .status)
 *   streamUrl  {string}  MJPEG URL for the drawing canvas background
 *   onClose    {fn}
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { toast } from 'react-hot-toast';
import zonesApi from '../../zonesApi';
import { api } from '../../api';
import ZoneEditor from './ZoneEditor';
import './zones.css';

const ZONE_TYPES = [
  { value: 'active_mask', label: 'Active Mask' },
  { value: 'active_zone', label: 'Active Zone' },
];

const HIT_MODES = [
  { value: 'centroid', label: 'Centroid' },
  { value: 'bbox_any', label: 'Any bbox corner' },
];

const DEFAULT_COLORS = [
  '#ef4444', '#f97316', '#eab308', '#22c55e',
  '#14b8a6', '#3b82f6', '#8b5cf6', '#ec4899',
];

const freshForm = (zones) => ({
  name: `Zone ${zones.length + 1}`,
  color: DEFAULT_COLORS[zones.length % DEFAULT_COLORS.length],
  zone_type: 'active_zone',
  hit_mode: 'bbox_any',
  min_dwell_frames: 1,
  include_background: false,
  enabled: true,
});

const ZoneManager = ({ camera, streamUrl, onClose }) => {
  const [zones,        setZones]        = useState([]);
  const [loading,      setLoading]      = useState(true);
  const [editingZone,  setEditingZone]  = useState(null);  // null = new zone
  const [form,         setForm]         = useState(freshForm([]));
  const [cameraStatus, setCameraStatus] = useState(camera.status || 'offline');
  const [camBusy,      setCamBusy]      = useState(false);

  // Sync local status when parent prop updates (e.g. camera stopped elsewhere)
  useEffect(() => { setCameraStatus(camera.status || 'offline'); }, [camera.status]);

  // ── load ─────────────────────────────────────────────────────────────────

  const fetchZones = useCallback(async () => {
    try {
      setLoading(true);
      const { data } = await zonesApi.getCameraZones(camera.id);
      setZones(data.zones || []);
    } catch {
      toast.error('Failed to load zones');
    } finally {
      setLoading(false);
    }
  }, [camera.id]);

  useEffect(() => { fetchZones(); }, [fetchZones]);

  // ── zone selection ────────────────────────────────────────────────────────

  const selectNew = (currentZones) => {
    setEditingZone(null);
    setForm(freshForm(currentZones ?? zones));
  };

  const selectZone = (zone) => {
    setEditingZone(zone);
    setForm({
      name: zone.name,
      color: zone.color,
      zone_type: zone.zone_type,
      hit_mode: zone.hit_mode,
      min_dwell_frames: zone.min_dwell_frames,
      include_background: zone.include_background,
      enabled: zone.enabled,
    });
  };

  // ── save ──────────────────────────────────────────────────────────────────

  const handleEditorSave = async (polygons, updatedOtherZones) => {
    const payload = { ...form, polygons };
    try {
      if (editingZone) {
        await zonesApi.updateZone(camera.id, editingZone.zone_id, payload);
      } else {
        await zonesApi.createZone(camera.id, payload);
      }
      if (updatedOtherZones && updatedOtherZones.length > 0) {
        await Promise.all(
          updatedOtherZones.map(oz =>
            zonesApi.updateZone(camera.id, oz.zone_id, { polygons: oz.polygons })
          )
        );
      }
      toast.success(editingZone ? 'Zone updated' : 'Zone created');
      const { data } = await zonesApi.getCameraZones(camera.id);
      const newZones = data.zones || [];
      setZones(newZones);
      if (!editingZone) setForm(freshForm(newZones)); // reset form for next new zone, keep editingZone=null
    } catch {
      toast.error('Failed to save zone');
    }
  };

  // ── delete / toggle ───────────────────────────────────────────────────────

  const handleDelete = async (zone) => {
    if (!window.confirm(`Delete zone "${zone.name}"?`)) return;
    try {
      await zonesApi.deleteZone(camera.id, zone.zone_id);
      toast.success('Zone deleted');
      const { data } = await zonesApi.getCameraZones(camera.id);
      const newZones = data.zones || [];
      setZones(newZones);
      if (editingZone?.zone_id === zone.zone_id) selectNew(newZones);
    } catch {
      toast.error('Failed to delete zone');
    }
  };

  const handleToggle = async (zone) => {
    try {
      await zonesApi.toggleZone(camera.id, zone.zone_id, !zone.enabled);
      fetchZones();
    } catch {
      toast.error('Failed to toggle zone');
    }
  };

  // ── camera start / stop  ──────────────────────────────────────────────────

  const handleStartCamera = async () => {
    setCamBusy(true);
    try {
      await api.startCamera(camera.id);
      setCameraStatus('online');
      toast.success('Camera started');
    } catch {
      toast.error('Failed to start camera');
    } finally {
      setCamBusy(false);
    }
  };

  const handleStopCamera = async () => {
    setCamBusy(true);
    try {
      await api.stopCamera(camera.id);
      setCameraStatus('offline');
      toast.success('Camera stopped');
    } catch {
      toast.error('Failed to stop camera');
    } finally {
      setCamBusy(false);
    }
  };

  const isOnline = cameraStatus === 'online' || cameraStatus === 'recording';
  // Only feed a live stream URL when the camera is actually running
  const activeStreamUrl = isOnline ? streamUrl : null;

  // ── render ────────────────────────────────────────────────────────────────

  return (
    <div className="zone-manager">

      {/* ── zone chips row ── */}
      <div className="zone-chip-row">
        {loading && <span style={{ fontSize: 11, color: '#78909c', padding: '0 6px' }}>Loading…</span>}

        {zones.map((zone) => (
          <div
            key={zone.zone_id}
            className={`zone-chip ${editingZone?.zone_id === zone.zone_id ? 'zone-chip--active' : ''}`}
          >
            <span className="zone-chip__dot" style={{ background: zone.color, opacity: zone.enabled ? 1 : 0.4 }} />
            <span className="zone-chip__name" onClick={() => selectZone(zone)}>
              {zone.name}
            </span>
            <button
              className="zone-chip__btn"
              title={zone.enabled ? 'Disable' : 'Enable'}
              onClick={(e) => { e.stopPropagation(); handleToggle(zone); }}
            >
              {zone.enabled ? '●' : '○'}
            </button>
            <button
              className="zone-chip__btn zone-chip__btn--del"
              title="Delete zone"
              onClick={(e) => { e.stopPropagation(); handleDelete(zone); }}
            >
              ✕
            </button>
          </div>
        ))}
      </div>

      {/* ── form bar ── */}
      <div className="zone-form zone-form--bar">
        <div className="zone-form__field">
          <label>Zone name</label>
          <input
            type="text"
            style={{ width: 130 }}
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
          />
        </div>
        <div className="zone-form__field">
          <label>Colour</label>
          <input
            type="color"
            value={form.color}
            onChange={(e) => setForm({ ...form, color: e.target.value })}
          />
        </div>
        <div className="zone-form__field">
          <label>Type</label>
          <select
            value={form.zone_type}
            onChange={(e) => setForm({ ...form, zone_type: e.target.value })}
          >
            {ZONE_TYPES.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
          </select>
        </div>
        <div className="zone-form__field">
          <label>Hit mode</label>
          <select
            value={form.hit_mode}
            onChange={(e) => setForm({ ...form, hit_mode: e.target.value })}
            disabled={form.zone_type === 'active_mask'}
          >
            {HIT_MODES.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
          </select>
        </div>
        <div className="zone-form__field">
          <label>Dwell frames</label>
          <input
            type="text"
            style={{ width: 55 }}
            value={form.min_dwell_frames}
            onChange={(e) => setForm({ ...form, min_dwell_frames: parseInt(e.target.value, 10) || 1 })}
          />
        </div>
        {form.zone_type === 'active_zone' && (
          <div className="zone-form__field" style={{ justifyContent: 'flex-end' }}>
            <label style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 11, color: '#546e7a', cursor: 'pointer' }}>
              <input
                type="checkbox"
                checked={form.include_background}
                onChange={(e) => setForm({ ...form, include_background: e.target.checked })}
              />
              Include bg
            </label>
          </div>
        )}

        {/* ── camera start / stop ── */}
        <div className="zone-form__field zone-form__field--cam">
          <label>{camera.name || camera.id}</label>
          <button
            className={`zone-btn ${isOnline ? 'zone-btn--cam-stop' : 'zone-btn--cam-start'}`}
            onClick={isOnline ? handleStopCamera : handleStartCamera}
            disabled={camBusy}
            title={isOnline ? 'Stop camera stream' : 'Start camera stream'}
          >
            {camBusy ? '…' : (isOnline ? '■ Stop' : '▶ Start')}
          </button>
        </div>
      </div>

      {/* ── editor canvas ── */}
      <ZoneEditor
        streamUrl={activeStreamUrl}
        zones={zones}
        editZone={editingZone}
        color={form.color}
        resolution={camera.resolution}
        onSave={handleEditorSave}
        onCancel={() => selectNew()}
      />

      {onClose && (
        <button className="zone-btn" style={{ alignSelf: 'flex-start', marginTop: 6 }} onClick={onClose}>
          ← Back
        </button>
      )}
    </div>
  );
};

export default ZoneManager;
