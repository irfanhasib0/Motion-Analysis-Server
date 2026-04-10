/**
 * ZoneFilter — reusable multi-select dropdown for filtering by zone.
 *
 * Used in EventView timeline & MotionActivityChart.
 *
 * Props:
 *   zones         {Array}  [ { zone_id, name, color, camera_id } ]  — flat list
 *   selectedZones {Set}    set of selected zone_ids
 *   onChange      {fn(Set)} called when selection changes
 *   label         {string} optional trigger label (defaults to "Zones")
 *   compact       {bool}   smaller appearance
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { MapPin } from 'lucide-react';
import './zones.css';

const ZoneFilter = ({ zones = [], selectedZones = new Set(), onChange, label = 'Zones', compact = false }) => {
  const [open, setOpen] = useState(false);
  const rootRef = useRef(null);

  // Close on outside click
  useEffect(() => {
    if (!open) return;
    const handler = (e) => {
      if (rootRef.current && !rootRef.current.contains(e.target)) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [open]);

  const toggle = useCallback((zoneId) => {
    const next = new Set(selectedZones);
    if (next.has(zoneId)) {
      next.delete(zoneId);
    } else {
      next.add(zoneId);
    }
    onChange(next);
  }, [selectedZones, onChange]);

  const clearAll = useCallback(() => onChange(new Set()), [onChange]);

  const activeCount = selectedZones.size;

  return (
    <div className="zone-filter" ref={rootRef}>
      <button
        className="zone-filter__trigger"
        style={compact ? { padding: '3px 8px', fontSize: 11 } : {}}
        onClick={() => setOpen((o) => !o)}
        title="Filter by zone"
      >
        <MapPin size={13} style={{ flexShrink: 0 }} />
        <span>{label}</span>
        {activeCount > 0 && (
          <span
            style={{
              background: '#009688',
              color: '#fff',
              borderRadius: '9px',
              padding: '0 6px',
              fontSize: 10,
              fontWeight: 700,
            }}
          >
            {activeCount}
          </span>
        )}
        <span style={{ opacity: 0.5, fontSize: 10 }}>{open ? '▲' : '▼'}</span>
      </button>

      {open && (
        <div className="zone-filter__dropdown">
          {activeCount > 0 && (
            <div
              className="zone-filter__option"
              style={{ borderBottom: '1px solid rgba(148,163,184,0.1)', color: '#64748b' }}
              onClick={clearAll}
            >
              ✕ Clear filter
            </div>
          )}
          {zones.length === 0 ? (
            <div className="zone-filter__option" style={{ color: '#94a3b8', fontStyle: 'italic', cursor: 'default' }}>
              No zones configured
            </div>
          ) : zones.map((zone) => {
            const checked = selectedZones.has(zone.zone_id);
            return (
              <div
                key={zone.zone_id}
                className={`zone-filter__option${checked ? ' zone-filter__option--checked' : ''}`}
                onClick={() => toggle(zone.zone_id)}
              >
                <span className="zone-filter__check">✓</span>
                <span className="zone-filter__swatch" style={{ background: zone.color }} />
                <span style={{ flex: 1 }}>{zone.name}</span>
                {zone.camera_name && (
                  <span style={{ fontSize: 10, color: '#475569' }}>{zone.camera_name}</span>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
};

export default ZoneFilter;
