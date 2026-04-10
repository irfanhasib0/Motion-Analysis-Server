/**
 * ZoneStudio — dedicated full-page tool for zone management and (in the
 * future) person search.
 *
 * Layout:
 *   ┌─ header (title + tabs) ──────────────────────┐
 *   │ [Zones] [Person Search (disabled)]            │
 *   ├─ sidebar ─────────┬─ main ────────────────────┤
 *   │  camera picker    │  ZoneManager / editor     │
 *   │  (status dots)    │  (live MJPEG background)  │
 *   └───────────────────┴───────────────────────────┘
 *
 * URL param: ?camera=<id>  — pre-selects a camera when navigating from
 *   the Cameras page "Zones" button.
 */

import React, { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { MapPin } from 'lucide-react';

import ZoneManager from '../Zones/ZoneManager';
import { api } from '../../api';
import zonesApi from '../../zonesApi';
import './ZoneStudio.css';

const ZoneStudio = ({ cameras = [] }) => {
  const [searchParams, setSearchParams] = useSearchParams();
  const [activeTab,       setActiveTab]       = useState('zones');
  const [selectedCamera,  setSelectedCamera]  = useState(null);

  // Pre-select camera from URL param; if absent, pick online camera with most zones
  useEffect(() => {
    const paramId = searchParams.get('camera');
    if (cameras.length === 0) return;
    if (paramId) {
      const found = cameras.find((c) => c.id === paramId);
      if (found) setSelectedCamera(found);
      return;
    }
    // No URL param — prefer online/recording cameras, pick the one with most zones
    zonesApi.getZonesSummary()
      .then(({ data }) => {
        const counts = {};
        Object.entries(data).forEach(([camId, cfg]) => {
          counts[camId] = cfg.zones?.length ?? 0;
        });
        const online = cameras.filter(c => c.status === 'online' || c.status === 'recording');
        const pool = online.length > 0 ? online : cameras;
        const best = pool.reduce((acc, cam) =>
          (counts[cam.id] ?? 0) >= (counts[acc.id] ?? 0) ? cam : acc
        , pool[0]);
        if (best) {
          setSelectedCamera(best);
          setSearchParams({ camera: best.id }, { replace: true });
        }
      })
      .catch(() => {
        const online = cameras.filter(c => c.status === 'online' || c.status === 'recording');
        const fallback = (online.length > 0 ? online : cameras)[0];
        if (fallback) {
          setSelectedCamera(fallback);
          setSearchParams({ camera: fallback.id }, { replace: true });
        }
      });
  }, [cameras]); // eslint-disable-line react-hooks/exhaustive-deps

  const selectCamera = (cam) => {
    setSelectedCamera(cam);
    setSearchParams({ camera: cam.id }, { replace: true });
  };

  const statusClass = (cam) => {
    if (cam.status === 'recording') return 'zs-cam-status--recording';
    if (cam.status === 'online')    return 'zs-cam-status--online';
    return 'zs-cam-status--offline';
  };

  const streamUrl = selectedCamera
    ? api.getCameraVideoStreamUrl(selectedCamera.id, 'mjpeg')
    : null;

  return (
    <div className="zone-studio">

      {/* ── header ── */}
      <div className="zs-header">
        <MapPin size={18} style={{ color: '#3b82f6', flexShrink: 0 }} />
        <h1 className="zs-header__title">Zones</h1>
      </div>

      {/* ── body ── */}
      <div className="zs-body">

        {/* ── camera picker sidebar ── */}
        <div className="zs-sidebar">
          <span className="zs-sidebar__label">Cameras</span>
          {cameras.length === 0 && (
            <span style={{ fontSize: 11, color: '#475569', padding: '4px 12px' }}>No cameras</span>
          )}
          {cameras.map((cam) => (
            <div
              key={cam.id}
              className={`zs-cam-item ${selectedCamera?.id === cam.id ? 'zs-cam-item--active' : ''}`}
              onClick={() => selectCamera(cam)}
              title={cam.id}
            >
              <span className={`zs-cam-status ${statusClass(cam)}`} />
              <span className="zs-cam-name">{cam.name || cam.id}</span>
            </div>
          ))}
        </div>

        {/* ── main content ── */}
        <div className="zs-main">
          {activeTab === 'zones' && (
            selectedCamera ? (
              <ZoneManager
                key={selectedCamera.id}
                camera={selectedCamera}
                streamUrl={streamUrl}
                onClose={null}
              />
            ) : (
              <div className="zs-empty">
                <div className="zs-empty__icon">⬡</div>
                <div>Select a camera to manage its zones</div>
                <div className="zs-empty__hint">
                  Zones restrict tracking regions or tag motion events with location names.
                </div>
              </div>
            )
          )}
        </div>
      </div>
    </div>
  );
};

export default ZoneStudio;
