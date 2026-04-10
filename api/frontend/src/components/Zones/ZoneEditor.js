/**
 * ZoneEditor — canvas polygon drawing overlay on top of the live camera stream.
 *
 * Features:
 *   • Click to add vertices, double-click or snap-to-first to close a polygon
 *   • Drag existing vertices to reposition them
 *   • Right-click (or long-press) an existing vertex to delete it
 *   • Controls in a right-side sidebar so the canvas is never obscured
 *
 * Coordinates are stored normalised [0, 1] for resolution independence.
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import './zones.css';

const SNAP_PX      = 12;  // snap-to-close radius while drawing
const VERTEX_HIT   = 12;  // hit-radius for drag / right-click on finished vertices
const EDGE_HIT     = 9;   // hit-radius for edge midpoint insertion handles
const MIN_PTS      = 3;   // minimum vertices to close a polygon

// ── coordinate helpers ────────────────────────────────────────────────────────

function normToPx(pt, w, h) { return { x: pt[0] * w, y: pt[1] * h }; }
function pxToNorm(x, y, w, h) {
  return [Math.max(0, Math.min(1, x / w)), Math.max(0, Math.min(1, y / h))];
}
function dist(a, b) { return Math.hypot(a.x - b.x, a.y - b.y); }

// ─────────────────────────────────────────────────────────────────────────────

const ZoneEditor = ({ streamUrl, zones = [], editZone = null, color = '#00ff00', resolution = null, onSave, onCancel }) => {
  const imgRef    = useRef(null);
  const canvasRef = useRef(null);
  const [imgSize, setImgSize] = useState({ w: 640, h: 360 });

  // polygons — finished polygon list for this zone session
  // current  — vertices of the polygon currently being drawn
  const [polygons,  setPolygons]  = useState([]);
  const [current,   setCurrent]   = useState([]);
  const [mousePos,  setMousePos]  = useState(null);
  const [hoverVert, setHoverVert] = useState(null);  // { polyIdx, vertIdx }

  const dragRef      = useRef(null);   // { polyIdx, vertIdx } during vertex drag
  const didDragRef   = useRef(false);  // true once pointer moved ≥ 2px during drag

  const [paused,       setPaused]       = useState([]);    // incomplete polygons paused with ESC
  const [selectedVert, setSelectedVert] = useState(null);  // { polyIdx, vertIdx } — click to select for Delete
  const [otherZones,    setOtherZones]    = useState([]);   // editable copies of all other saved zones
  const [otherHoverVert, setOtherHoverVert] = useState(null); // { zoneId, polyIdx, vertIdx }

  const [hoverEdge,      setHoverEdge]      = useState(null);  // { polyIdx, edgeIdx }
  const [otherHoverEdge, setOtherHoverEdge] = useState(null);  // { zoneId, polyIdx, edgeIdx }
  const [undoCount,      setUndoCount]      = useState(0);     // reactive mirror of undoStackRef length
  const undoStackRef = useRef([]);  // undo history: array of polygon snapshots

  // ── load existing polygons when editing ────────────────────────────────────

  useEffect(() => {
    setPolygons(editZone ? (editZone.polygons || []) : []);
    setCurrent([]);
    setPaused([]);
    setSelectedVert(null);
    undoStackRef.current = [];
    setUndoCount(0);
    // Snapshot all other zones for in-canvas editing
    setOtherZones(
      (zones || [])
        .filter(z => !editZone || z.zone_id !== editZone.zone_id)
        .map(z => ({
          zone_id: z.zone_id,
          name: z.name,
          color: z.color,
          polygons: (z.polygons || []).map(p => p.map(v => [v[0], v[1]])),
        }))
    );
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editZone]);

  // When zones load for the first time (async after mount) populate otherZones
  // without disturbing any in-progress drawing state.
  useEffect(() => {
    setOtherZones(prev => {
      // If user already has edits (prev is non-empty), don't overwrite
      if (prev.length > 0) return prev;
      return (zones || [])
        .filter(z => !editZone || z.zone_id !== editZone.zone_id)
        .map(z => ({
          zone_id: z.zone_id,
          name: z.name,
          color: z.color,
          polygons: (z.polygons || []).map(p => p.map(v => [v[0], v[1]])),
        }));
    });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [zones]);

  // ── sync canvas size to the img element ───────────────────────────────────

  const syncSize = useCallback(() => {
    const img = imgRef.current;
    if (!img) return;
    const r = img.getBoundingClientRect();
    setImgSize({ w: r.width, h: r.height });
  }, []);

  useEffect(() => {
    const el = imgRef.current;
    if (!el) return;
    const isImg = el.tagName === 'IMG';
    if (isImg) el.addEventListener('load', syncSize);
    const ro = new ResizeObserver(syncSize);
    ro.observe(el);
    syncSize();
    return () => { if (isImg) el.removeEventListener('load', syncSize); ro.disconnect(); };
  }, [syncSize, streamUrl]);  // re-attach when streamUrl switches img↔div

  // ── nearest finished vertex lookup ────────────────────────────────────────

  const findNearestVertex = useCallback((pos, threshold) => {
    const { w, h } = imgSize;
    let best = null;
    let bestDist = threshold;
    polygons.forEach((poly, polyIdx) => {
      poly.forEach((pt, vertIdx) => {
        const px = normToPx(pt, w, h);
        const d  = dist(pos, px);
        if (d < bestDist) { bestDist = d; best = { polyIdx, vertIdx }; }
      });
    });
    return best;
  }, [polygons, imgSize]);

  // find a paused polygon's endpoint (first or last vertex) near a position
  const findPausedEndpoint = useCallback((pos, threshold) => {
    const { w, h } = imgSize;
    let best = null;
    let bestDist = threshold;
    paused.forEach((poly, pausedIdx) => {
      const fp = normToPx(poly[0], w, h);
      const df = dist(pos, fp);
      if (df < bestDist) { bestDist = df; best = { pausedIdx, end: 'first' }; }
      if (poly.length > 1) {
        const lp = normToPx(poly[poly.length - 1], w, h);
        const dl = dist(pos, lp);
        if (dl < bestDist) { bestDist = dl; best = { pausedIdx, end: 'last' }; }
      }
    });
    return best;
  }, [paused, imgSize]);

  // Search all other-zone vertices for the nearest hit
  const findNearestOtherVertex = useCallback((pos, threshold) => {
    const { w, h } = imgSize;
    let best = null;
    let bestDist = threshold;
    otherZones.forEach((zone) => {
      (zone.polygons || []).forEach((poly, polyIdx) => {
        poly.forEach((pt, vertIdx) => {
          const px = normToPx(pt, w, h);
          const d  = dist(pos, px);
          if (d < bestDist) { bestDist = d; best = { zoneId: zone.zone_id, polyIdx, vertIdx }; }
        });
      });
    });
    return best;
  }, [otherZones, imgSize]);

  // find nearest finished polygon edge midpoint (for vertex insertion)
  const findNearestEdgeMidpoint = useCallback((pos, threshold) => {
    const { w, h } = imgSize;
    let best = null;
    let bestDist = threshold;
    polygons.forEach((poly, polyIdx) => {
      poly.forEach((pt, vi) => {
        const nvi = (vi + 1) % poly.length;
        const mid = { x: (pt[0] + poly[nvi][0]) / 2 * w, y: (pt[1] + poly[nvi][1]) / 2 * h };
        const d   = dist(pos, mid);
        if (d < bestDist) { bestDist = d; best = { polyIdx, edgeIdx: vi }; }
      });
    });
    return best;
  }, [polygons, imgSize]);

  const findNearestOtherEdgeMidpoint = useCallback((pos, threshold) => {
    const { w, h } = imgSize;
    let best = null;
    let bestDist = threshold;
    otherZones.forEach((zone) => {
      (zone.polygons || []).forEach((poly, polyIdx) => {
        poly.forEach((pt, vi) => {
          const nvi = (vi + 1) % poly.length;
          const mid = { x: (pt[0] + poly[nvi][0]) / 2 * w, y: (pt[1] + poly[nvi][1]) / 2 * h };
          const d   = dist(pos, mid);
          if (d < bestDist) { bestDist = d; best = { zoneId: zone.zone_id, polyIdx, edgeIdx: vi }; }
        });
      });
    });
    return best;
  }, [otherZones, imgSize]);

  // ── undo helpers ──────────────────────────────────────────────────────────

  const pushUndo = useCallback(() => {
    undoStackRef.current.push(polygons.map(p => p.map(v => [v[0], v[1]])));
    if (undoStackRef.current.length > 40) undoStackRef.current.shift();
    setUndoCount(undoStackRef.current.length);
  }, [polygons]);

  const handleUndo = useCallback(() => {
    const prev = undoStackRef.current.pop();
    if (prev) { setPolygons(prev); setUndoCount(undoStackRef.current.length); }
  }, []);

  const handleUndoVertex = useCallback(() => {
    setCurrent(prev => prev.slice(0, -1));
  }, []);

  // ── canvas draw ───────────────────────────────────────────────────────────

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const { w, h } = imgSize;
    canvas.width  = w;
    canvas.height = h;
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, w, h);

    // ── pixel-coordinate grid overlay ────────────────────────────────────
    ctx.save();
    // Parse native camera resolution (e.g. "640x480") for tick labels
    let nativeW = imgSize.w, nativeH = imgSize.h;
    if (resolution) {
      const parts = String(resolution).toLowerCase().split('x');
      if (parts.length === 2) {
        const pw = parseInt(parts[0], 10);
        const ph = parseInt(parts[1], 10);
        if (pw > 0 && ph > 0) { nativeW = pw; nativeH = ph; }
      }
    }
    // Choose a round step so we get ~8-12 lines
    const rawStepX = nativeW / 10;
    const rawStepY = nativeH / 10;
    const niceStep = (v) => {
      const mag = Math.pow(10, Math.floor(Math.log10(v)));
      const norm = v / mag;
      if (norm < 1.5) return mag;
      if (norm < 3.5) return 2 * mag;
      if (norm < 7.5) return 5 * mag;
      return 10 * mag;
    };
    const stepX = niceStep(rawStepX);
    const stepY = niceStep(rawStepY);
    // Main grid lines — cyan-tinted, dashed
    ctx.strokeStyle = 'rgba(0,230,230,0.35)';
    ctx.lineWidth = 0.75;
    ctx.setLineDash([3, 5]);
    for (let px = stepX; px < nativeW; px += stepX) {
      const cx = px / nativeW * w;
      ctx.beginPath(); ctx.moveTo(cx, 0); ctx.lineTo(cx, h); ctx.stroke();
    }
    for (let py = stepY; py < nativeH; py += stepY) {
      const cy = py / nativeH * h;
      ctx.beginPath(); ctx.moveTo(0, cy); ctx.lineTo(w, cy); ctx.stroke();
    }
    ctx.setLineDash([]);
    // Tick labels — pixel coordinates, dark outline for readability
    ctx.font = 'bold 10px monospace';
    ctx.textBaseline = 'top';
    ctx.shadowColor = 'rgba(0,0,0,0.95)';
    ctx.shadowBlur = 3;
    ctx.fillStyle = 'rgba(0,255,240,0.9)';
    for (let px = stepX; px < nativeW; px += stepX) {
      const cx = px / nativeW * w;
      ctx.fillText(String(px), cx + 2, 2);
    }
    ctx.textBaseline = 'alphabetic';
    for (let py = stepY; py < nativeH; py += stepY) {
      const cy = py / nativeH * h;
      ctx.fillText(String(py), 2, cy - 2);
    }
    ctx.restore();
    // ──────────────────────────────────────────────────────────────────────

    const c = color || '#00ff00';

    // --- other-zone overlays (fully editable) ---
    otherZones.forEach((zone) => {
      const oc = zone.color || '#888888';
      (zone.polygons || []).forEach((poly, polyIdx) => {
        if (poly.length < 2) return;
        ctx.beginPath();
        const os0 = normToPx(poly[0], w, h);
        ctx.moveTo(os0.x, os0.y);
        poly.slice(1).forEach(pt => { const p = normToPx(pt, w, h); ctx.lineTo(p.x, p.y); });
        ctx.closePath();
        ctx.fillStyle = oc + '22';
        ctx.fill();
        ctx.strokeStyle = oc;
        ctx.lineWidth = 1.5;
        ctx.setLineDash([5, 3]);
        ctx.stroke();
        ctx.setLineDash([]);
        // zone name label
        const ncx = poly.reduce((s, p) => s + p[0], 0) / poly.length * w;
        const ncy = poly.reduce((s, p) => s + p[1], 0) / poly.length * h;
        ctx.font = '11px sans-serif';
        ctx.shadowColor = 'rgba(0,0,0,0.9)';
        ctx.shadowBlur = 4;
        ctx.fillStyle = '#ffffff';
        ctx.fillText(zone.name, ncx - 15, ncy + 4);
        ctx.shadowBlur = 0;
        // vertex handles
        poly.forEach((pt, vi) => {
          const px = normToPx(pt, w, h);
          const hot = otherHoverVert && otherHoverVert.zoneId === zone.zone_id
            && otherHoverVert.polyIdx === polyIdx && otherHoverVert.vertIdx === vi;
          ctx.beginPath();
          ctx.arc(px.x, px.y, hot ? 8 : 5, 0, Math.PI * 2);
          ctx.fillStyle = hot ? '#ffffff' : oc;
          ctx.fill();
          ctx.strokeStyle = hot ? oc : 'rgba(0,0,0,0.6)';
          ctx.lineWidth = hot ? 2 : 1;
          ctx.stroke();
          if (hot) {
            ctx.font = 'bold 10px sans-serif';
            ctx.shadowColor = 'rgba(0,0,0,0.9)';
            ctx.shadowBlur = 3;
            ctx.fillStyle = '#ff6b6b';
            ctx.fillText('× right-click', px.x + 11, px.y - 8);
            ctx.shadowBlur = 0;
          }
        });
        // ── edge midpoint handles for other-zone polygons ─────────────────
        poly.forEach((pt, vi) => {
          const nvi = (vi + 1) % poly.length;
          const mid = { x: (pt[0] + poly[nvi][0]) / 2 * w, y: (pt[1] + poly[nvi][1]) / 2 * h };
          const hotEdge = otherHoverEdge && otherHoverEdge.zoneId === zone.zone_id
            && otherHoverEdge.polyIdx === polyIdx && otherHoverEdge.edgeIdx === vi;
          if (hotEdge) {
            ctx.beginPath();
            ctx.arc(mid.x, mid.y, 7, 0, Math.PI * 2);
            ctx.fillStyle   = '#ffffff';
            ctx.fill();
            ctx.strokeStyle = oc;
            ctx.lineWidth   = 2;
            ctx.stroke();
            ctx.font        = 'bold 9px sans-serif';
            ctx.shadowColor = 'rgba(0,0,0,0.9)';
            ctx.shadowBlur  = 3;
            ctx.fillStyle   = '#005b4d';
            ctx.fillText('+ insert', mid.x + 9, mid.y - 5);
            ctx.shadowBlur  = 0;
          } else if (!otherHoverVert) {
            ctx.beginPath();
            ctx.arc(mid.x, mid.y, 3, 0, Math.PI * 2);
            ctx.fillStyle = oc + '55';
            ctx.fill();
          }
        });
      });
    });

    // --- finished polygons ---
    polygons.forEach((poly, idx) => {
      if (poly.length < 2) return;

      // filled shape
      ctx.beginPath();
      const s0 = normToPx(poly[0], w, h);
      ctx.moveTo(s0.x, s0.y);
      poly.slice(1).forEach(pt => { const p = normToPx(pt, w, h); ctx.lineTo(p.x, p.y); });
      ctx.closePath();
      ctx.fillStyle = c + '2e';  // ≈18% opacity
      ctx.fill();
      ctx.strokeStyle = c;
      ctx.lineWidth = 2;
      ctx.setLineDash([]);
      ctx.stroke();

      // centroid label — white text with black shadow
      const cx = poly.reduce((s, p) => s + p[0], 0) / poly.length * w;
      const cy = poly.reduce((s, p) => s + p[1], 0) / poly.length * h;
      ctx.font         = 'bold 13px sans-serif';
      ctx.shadowColor  = 'rgba(0,0,0,0.95)';
      ctx.shadowBlur   = 5;
      ctx.fillStyle    = '#ffffff';
      ctx.fillText(`#${idx + 1}`, cx - 8, cy + 5);
      ctx.shadowBlur   = 0;

      // vertex handles — always visible for drag affordance
      poly.forEach((pt, vi) => {
        const px  = normToPx(pt, w, h);
        const hot = hoverVert && hoverVert.polyIdx === idx && hoverVert.vertIdx === vi;
        const sel = selectedVert && selectedVert.polyIdx === idx && selectedVert.vertIdx === vi;
        ctx.beginPath();
        ctx.arc(px.x, px.y, hot ? 9 : (sel ? 8 : 5), 0, Math.PI * 2);
        ctx.fillStyle   = hot ? '#ffffff' : (sel ? '#ef4444' : c);
        ctx.fill();
        ctx.strokeStyle = hot ? c : (sel ? 'rgba(0,0,0,0.8)' : 'rgba(0,0,0,0.7)');
        ctx.lineWidth   = hot ? 2 : 1;
        ctx.stroke();

        if (hot) {
          ctx.font        = 'bold 10px sans-serif';
          ctx.shadowColor = 'rgba(0,0,0,0.9)';
          ctx.shadowBlur  = 3;
          ctx.fillStyle   = '#ff6b6b';
          ctx.fillText('× right-click', px.x + 11, px.y - 8);
          ctx.shadowBlur  = 0;
        }
        if (sel && !hot) {
          ctx.font        = 'bold 10px sans-serif';
          ctx.shadowColor = 'rgba(0,0,0,0.9)';
          ctx.shadowBlur  = 3;
          ctx.fillStyle   = '#ef4444';
          ctx.fillText('Del to remove', px.x + 11, px.y - 8);
          ctx.shadowBlur  = 0;
        }
      });
      // ── edge midpoint handles ──────────────────────────────────────────────
      poly.forEach((pt, vi) => {
        const nvi = (vi + 1) % poly.length;
        const mid = { x: (pt[0] + poly[nvi][0]) / 2 * w, y: (pt[1] + poly[nvi][1]) / 2 * h };
        const hotEdge = hoverEdge && hoverEdge.polyIdx === idx && hoverEdge.edgeIdx === vi;
        if (hotEdge) {
          ctx.beginPath();
          ctx.arc(mid.x, mid.y, 7, 0, Math.PI * 2);
          ctx.fillStyle   = '#ffffff';
          ctx.fill();
          ctx.strokeStyle = c;
          ctx.lineWidth   = 2;
          ctx.stroke();
          ctx.font        = 'bold 9px sans-serif';
          ctx.shadowColor = 'rgba(0,0,0,0.9)';
          ctx.shadowBlur  = 3;
          ctx.fillStyle   = '#006d5b';
          ctx.fillText('+ insert', mid.x + 9, mid.y - 5);
          ctx.shadowBlur  = 0;
        } else if (!hoverVert) {
          ctx.beginPath();
          ctx.arc(mid.x, mid.y, 3, 0, Math.PI * 2);
          ctx.fillStyle = c + '55';
          ctx.fill();
        }
      });
    });

    // --- paused incomplete polygons ---
    paused.forEach((poly) => {
      if (poly.length < 1) return;
      const s0 = normToPx(poly[0], w, h);
      if (poly.length >= 2) {
        ctx.beginPath();
        ctx.moveTo(s0.x, s0.y);
        poly.slice(1).forEach(pt => { const p = normToPx(pt, w, h); ctx.lineTo(p.x, p.y); });
        ctx.strokeStyle = c;
        ctx.globalAlpha = 0.45;
        ctx.lineWidth   = 2;
        ctx.setLineDash([8, 5]);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.globalAlpha = 1;
      }
      // amber endpoint handles — click to resume
      const endpoints = poly.length === 1 ? [0] : [0, poly.length - 1];
      endpoints.forEach(ei => {
        const ep = normToPx(poly[ei], w, h);
        ctx.beginPath();
        ctx.arc(ep.x, ep.y, 7, 0, Math.PI * 2);
        ctx.fillStyle   = '#fbbf24';
        ctx.fill();
        ctx.strokeStyle = 'rgba(0,0,0,0.7)';
        ctx.lineWidth   = 1.5;
        ctx.stroke();
      });
      // "resume" hint above first endpoint
      ctx.font        = 'bold 10px sans-serif';
      ctx.shadowColor = 'rgba(0,0,0,0.9)';
      ctx.shadowBlur  = 3;
      ctx.fillStyle   = '#fbbf24';
      ctx.fillText('⏸ click to resume', s0.x + 11, s0.y - 8);
      ctx.shadowBlur  = 0;
    });

    // --- in-progress polygon ---
    if (current.length > 0) {
      ctx.beginPath();
      const s0 = normToPx(current[0], w, h);
      ctx.moveTo(s0.x, s0.y);
      current.slice(1).forEach(pt => { const p = normToPx(pt, w, h); ctx.lineTo(p.x, p.y); });
      if (mousePos) ctx.lineTo(mousePos.x, mousePos.y);
      ctx.strokeStyle = c;
      ctx.lineWidth   = 2;
      ctx.setLineDash([6, 4]);
      ctx.stroke();
      ctx.setLineDash([]);

      current.forEach((pt, i) => {
        const px = normToPx(pt, w, h);
        ctx.beginPath();
        ctx.arc(px.x, px.y, i === 0 ? 6 : 5, 0, Math.PI * 2);
        ctx.fillStyle   = i === 0 ? '#ffffff' : c;
        ctx.fill();
        ctx.strokeStyle = c;
        ctx.lineWidth   = 1.5;
        ctx.stroke();
      });

      // snap ring on first vertex
      if (mousePos && current.length >= MIN_PTS) {
        const fp = normToPx(current[0], w, h);
        if (dist(mousePos, fp) <= SNAP_PX) {
          ctx.beginPath();
          ctx.arc(fp.x, fp.y, SNAP_PX, 0, Math.PI * 2);
          ctx.strokeStyle = '#ffffff';
          ctx.lineWidth   = 1.5;
          ctx.setLineDash([3, 3]);
          ctx.stroke();
          ctx.setLineDash([]);
        }
      }
    }

    // ── cursor coordinate tooltip ─────────────────────────────────────────
    if (mousePos) {
      const px_x = Math.round(mousePos.x / w * nativeW);
      const px_y = Math.round(mousePos.y / h * nativeH);
      ctx.font         = '10px monospace';
      ctx.shadowColor  = 'rgba(0,0,0,0.95)';
      ctx.shadowBlur   = 3;
      ctx.fillStyle    = 'rgba(255,255,220,0.95)';
      ctx.textBaseline = 'bottom';
      ctx.fillText(`${px_x}, ${px_y}`, mousePos.x + 12, mousePos.y - 2);
      ctx.textBaseline = 'alphabetic';
      ctx.shadowBlur   = 0;
    }
  }, [polygons, current, mousePos, hoverVert, hoverEdge, imgSize, color, paused, selectedVert, otherZones, otherHoverVert, otherHoverEdge, resolution]);

  // ── keyboard shortcuts ────────────────────────────────────────────────────

  useEffect(() => {
    const onKey = (e) => {
      // Ctrl/Cmd+Z — undo last polygon mutation
      if ((e.ctrlKey || e.metaKey) && e.key === 'z') {
        e.preventDefault();
        handleUndo();
        return;
      }
      if (e.key === 'Escape') {
        if (current.length >= 1) {
          setPaused(prev => [...prev, current]);
          setCurrent([]);
        } else if (selectedVert) {
          setSelectedVert(null);
        }
        return;
      }
      if ((e.key === 'Delete' || e.key === 'Backspace') && selectedVert) {
        e.preventDefault();
        const poly = polygons[selectedVert.polyIdx];
        if (poly && poly.length > MIN_PTS) {
          pushUndo();
          setPolygons(prev => {
            const next = prev.map(p => [...p]);
            next[selectedVert.polyIdx] = next[selectedVert.polyIdx].filter((_, i) => i !== selectedVert.vertIdx);
            return next;
          });
        }
        setSelectedVert(null);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [current, selectedVert, polygons, pushUndo, handleUndo]);

  // ── canvas position helper ────────────────────────────────────────────────

  const getPos = useCallback((e) => {
    const canvas = canvasRef.current;
    if (!canvas) return { x: 0, y: 0 };
    const r  = canvas.getBoundingClientRect();
    const cx = e.touches ? e.touches[0].clientX : e.clientX;
    const cy = e.touches ? e.touches[0].clientY : e.clientY;
    return { x: cx - r.left, y: cy - r.top };
  }, []);

  // ── pointer handlers ──────────────────────────────────────────────────────

  const handleMouseMove = useCallback((e) => {
    const pos = getPos(e);

    if (dragRef.current) {
      const drag = dragRef.current;
      const { w, h } = imgSize;
      if (drag.source === 'other') {
        setOtherZones(prev => prev.map(zone => {
          if (zone.zone_id !== drag.zoneId) return zone;
          const polys = zone.polygons.map((p, pi) =>
            pi === drag.polyIdx
              ? p.map((v, vi) => vi === drag.vertIdx ? pxToNorm(pos.x, pos.y, w, h) : v)
              : [...p]
          );
          return { ...zone, polygons: polys };
        }));
      } else {
        const { polyIdx, vertIdx } = drag;
        setPolygons(prev => {
          const next = prev.map(p => [...p]);
          next[polyIdx] = [...next[polyIdx]];
          next[polyIdx][vertIdx] = pxToNorm(pos.x, pos.y, w, h);
          return next;
        });
      }
      didDragRef.current = true;
      return;
    }

    setMousePos(pos);
    const sv  = findNearestVertex(pos, VERTEX_HIT);
    setHoverVert(sv);
    const ohv = sv ? null : findNearestOtherVertex(pos, VERTEX_HIT);
    setOtherHoverVert(ohv);
    // edge midpoint hover — only when no vertex is hovered
    const he  = (!sv && !ohv) ? findNearestEdgeMidpoint(pos, EDGE_HIT) : null;
    setHoverEdge(he);
    const ohe = (!sv && !ohv && !he) ? findNearestOtherEdgeMidpoint(pos, EDGE_HIT) : null;
    setOtherHoverEdge(ohe);
  }, [getPos, imgSize, findNearestVertex, findNearestOtherVertex, findNearestEdgeMidpoint, findNearestOtherEdgeMidpoint]);

  const handleMouseLeave = useCallback(() => {
    setMousePos(null);
    setHoverVert(null);
    setHoverEdge(null);
    setOtherHoverEdge(null);
  }, []);

  const handleMouseDown = useCallback((e) => {
    if (e.button !== 0) return;
    const pos = getPos(e);
    const v = findNearestVertex(pos, VERTEX_HIT);
    if (v) {
      pushUndo();
      dragRef.current    = { source: 'self', ...v };
      didDragRef.current = false;
      e.preventDefault();
      return;
    }
    const ov = findNearestOtherVertex(pos, VERTEX_HIT);
    if (ov) {
      dragRef.current    = { source: 'other', ...ov };
      didDragRef.current = false;
      e.preventDefault();
      return;
    }
    // Edge midpoint — insert new vertex then immediately drag it
    const em = findNearestEdgeMidpoint(pos, EDGE_HIT);
    if (em) {
      pushUndo();
      const { polyIdx, edgeIdx } = em;
      const { w, h } = imgSize;
      const newPt = pxToNorm(pos.x, pos.y, w, h);
      setPolygons(prev => {
        const next = prev.map(p => [...p]);
        next[polyIdx] = [...next[polyIdx]];
        next[polyIdx].splice(edgeIdx + 1, 0, newPt);
        return next;
      });
      dragRef.current    = { source: 'self', polyIdx, vertIdx: edgeIdx + 1 };
      didDragRef.current = false;
      setHoverEdge(null);
      e.preventDefault();
      return;
    }
    const oem = findNearestOtherEdgeMidpoint(pos, EDGE_HIT);
    if (oem) {
      const { zoneId, polyIdx, edgeIdx } = oem;
      const { w, h } = imgSize;
      const newPt = pxToNorm(pos.x, pos.y, w, h);
      setOtherZones(prev => prev.map(zone => {
        if (zone.zone_id !== zoneId) return zone;
        return { ...zone, polygons: zone.polygons.map((p, pi) => {
          if (pi !== polyIdx) return [...p];
          const updated = [...p];
          updated.splice(edgeIdx + 1, 0, newPt);
          return updated;
        })};
      }));
      dragRef.current    = { source: 'other', zoneId, polyIdx, vertIdx: edgeIdx + 1 };
      didDragRef.current = false;
      setOtherHoverEdge(null);
      e.preventDefault();
    }
  }, [getPos, findNearestVertex, findNearestOtherVertex, findNearestEdgeMidpoint, findNearestOtherEdgeMidpoint, imgSize, pushUndo]);

  const handleMouseUp = useCallback(() => {
    dragRef.current = null;
  }, []);

  const handleClick = useCallback((e) => {
    if (didDragRef.current) { didDragRef.current = false; return; }
    e.preventDefault();

    const pos      = getPos(e);
    const { w, h } = imgSize;

    // ── while actively drawing ──────────────────────────────────────────────
    if (current.length > 0) {
      // If clicking an existing finished vertex, let drag handle it — don't add to current
      const existingV = findNearestVertex(pos, VERTEX_HIT);
      if (existingV) return;

      if (current.length >= MIN_PTS) {
        const fp = normToPx(current[0], w, h);
        if (dist(pos, fp) <= SNAP_PX) {
          pushUndo();
          setPolygons(prev => [...prev, current]);
          setCurrent([]);
          return;
        }
      }
      setCurrent(prev => [...prev, pxToNorm(pos.x, pos.y, w, h)]);
      return;
    }

    // ── idle ────────────────────────────────────────────────────────────────
    // 1. Resume a paused polygon by clicking one of its endpoints
    const pe = findPausedEndpoint(pos, VERTEX_HIT);
    if (pe) {
      const poly = paused[pe.pausedIdx];
      setPaused(prev => prev.filter((_, i) => i !== pe.pausedIdx));
      // clicking 'first' vertex → reverse so we draw toward the other end
      setCurrent(pe.end === 'first' ? [...poly].reverse() : [...poly]);
      setSelectedVert(null);
      return;
    }

    // 2. Click a finished vertex → select / deselect for Delete
    const v = findNearestVertex(pos, VERTEX_HIT);
    if (v) {
      setSelectedVert(prev =>
        prev && prev.polyIdx === v.polyIdx && prev.vertIdx === v.vertIdx ? null : v
      );
      return;
    }

    // 3. Empty area → clear selection, start a new polygon
    setSelectedVert(null);
    setCurrent([pxToNorm(pos.x, pos.y, w, h)]);
  }, [current, imgSize, getPos, findPausedEndpoint, findNearestVertex, paused, pushUndo]);

  const handleDoubleClick = useCallback((e) => {
    e.preventDefault();
    if (current.length >= MIN_PTS) {
      pushUndo();
      setPolygons(prev => [...prev, current]);
      setCurrent([]);
    }
  }, [current, pushUndo]);

  const handleContextMenu = useCallback((e) => {
    e.preventDefault();
    const pos = getPos(e);
    const v = findNearestVertex(pos, VERTEX_HIT);
    if (v) {
      const poly = polygons[v.polyIdx];
      if (poly.length <= MIN_PTS) return;
      pushUndo();
      setPolygons(prev => {
        const next = prev.map(p => [...p]);
        next[v.polyIdx] = next[v.polyIdx].filter((_, i) => i !== v.vertIdx);
        return next;
      });
      setHoverVert(null);
      return;
    }
    const ov = findNearestOtherVertex(pos, VERTEX_HIT);
    if (ov) {
      setOtherZones(prev => prev.map(zone => {
        if (zone.zone_id !== ov.zoneId) return zone;
        const poly = zone.polygons[ov.polyIdx];
        if (poly.length <= MIN_PTS) return zone;
        return { ...zone, polygons: zone.polygons.map((p, pi) =>
          pi === ov.polyIdx ? p.filter((_, vi) => vi !== ov.vertIdx) : [...p]
        )};
      }));
      setOtherHoverVert(null);
    }
  }, [getPos, findNearestVertex, findNearestOtherVertex, polygons, pushUndo]);

  // ── toolbar actions ───────────────────────────────────────────────────────

  const handleCancel = useCallback(() => {
    setPolygons([]);
    setCurrent([]);
    setPaused([]);
    setSelectedVert(null);
    undoStackRef.current = [];
    setUndoCount(0);
    onCancel();
  }, [onCancel]);
  const handleDiscardCurrent   = () => setCurrent([]);
  const handleRemoveLastPoly   = () => { pushUndo(); setPolygons(p => p.slice(0, -1)); };

  const handleSave = () => {
    const closedCurrent = current.length >= MIN_PTS ? [current] : [];
    const closedPaused  = paused.filter(p => p.length >= MIN_PTS);
    const final = [...polygons, ...closedPaused, ...closedCurrent];
    if (final.length === 0) return;
    onSave(final, otherZones);
  };

  const isDrawing = current.length > 0;
  const hasPaused = paused.length > 0;
  const canSave   = polygons.length > 0 || isDrawing || paused.some(p => p.length >= MIN_PTS);
  const cursor = isDrawing
    ? 'crosshair'
    : (hoverVert || otherHoverVert)
      ? (dragRef.current ? 'grabbing' : 'grab')
      : (hoverEdge || otherHoverEdge)
        ? 'cell'
        : 'crosshair';

  // ── render ────────────────────────────────────────────────────────────────

  return (
    <div>
      <div style={{ display: 'flex', gap: 10, alignItems: 'flex-start' }}>

        {/* ── Canvas ── */}
        <div className="zone-editor-wrap" style={{ flex: 1, minWidth: 0 }}>
          {streamUrl ? (
            <img
              ref={imgRef}
              src={streamUrl}
              alt="camera"
              style={{ width: '100%', display: 'block', userSelect: 'none', pointerEvents: 'none' }}
              onLoad={syncSize}
            />
          ) : (
            <div
              ref={imgRef}
              className="zone-editor-offline"
              style={{ width: '100%', aspectRatio: '16/9', display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', background: 'rgba(0,0,0,0.35)', color: '#80cbc4', fontSize: 13, gap: 8 }}
            >
              <span style={{ fontSize: 28, opacity: 0.6 }}>⏻</span>
              <span>Camera offline — start it to see the live feed</span>
              <span style={{ fontSize: 11, opacity: 0.6 }}>You can still draw zones on a blank canvas</span>
            </div>
          )}
          <canvas
            ref={canvasRef}
            className="zone-editor-canvas"
            style={{ width: imgSize.w, height: imgSize.h, cursor }}
            onMouseDown={handleMouseDown}
            onMouseUp={handleMouseUp}
            onMouseMove={handleMouseMove}
            onMouseLeave={handleMouseLeave}
            onClick={handleClick}
            onDoubleClick={handleDoubleClick}
            onContextMenu={handleContextMenu}
            onTouchStart={handleClick}
            onTouchMove={handleMouseMove}
          />
        </div>

        {/* ── Sidebar toolbar ── */}
        <div className="zone-editor-sidebar" style={{ order: -1 }}>

          <div className="zone-editor-sidebar__section">
            <span className="zone-editor-sidebar__label">Draw</span>
            <button className="zone-btn" onClick={handleUndoVertex} disabled={!isDrawing} title="Remove last vertex">
              ↩ Undo vertex
            </button>
            <button className="zone-btn" onClick={handleDiscardCurrent} disabled={!isDrawing} title="Discard in-progress polygon">
              ✕ Discard
            </button>
          </div>

          <div className="zone-editor-sidebar__section">
            <span className="zone-editor-sidebar__label">Polygons</span>
            <button className="zone-btn" onClick={handleRemoveLastPoly} disabled={polygons.length === 0} title="Remove last closed polygon">
              − Remove last
            </button>
            <button className="zone-btn" onClick={handleUndo} disabled={undoCount === 0} title="Ctrl+Z — Undo last polygon change">
              ↺ Undo (Ctrl+Z)
            </button>
          </div>

          <div className="zone-editor-sidebar__section zone-editor-sidebar__section--actions">
            <button className="zone-btn zone-btn--primary" onClick={handleSave} disabled={!canSave}>
              ✓ Save zone
            </button>
            <button className="zone-btn zone-btn--danger" onClick={handleCancel}>
              ✗ Cancel
            </button>
          </div>

          <div className="zone-editor-sidebar__hint">
            <strong>{polygons.length}</strong> polygon{polygons.length !== 1 ? 's' : ''}
            {otherZones.length > 0 && <><br /><span style={{ color: '#888' }}>{otherZones.length} other</span></>}
            {hasPaused && <><br /><strong>{paused.length}</strong> paused</>}
            {isDrawing && <><br /><strong>{current.length}</strong> pts in progress</>}
            {!isDrawing && (polygons.length > 0 || hasPaused) && (
              <><br />Drag ◾ to move<br />Hover edge • to insert<br />Right-click ◾ to delete<br />Click ◾ + Del<br />Ctrl+Z to undo</>
            )}
          </div>

        </div>
      </div>

      {/* hint bar under canvas */}
      <div className="zone-editor-hint">
        {isDrawing
          ? <>Click to add vertices · snap or double-click to close · <kbd style={{ background: 'rgba(255,255,255,0.1)', borderRadius: 3, padding: '0 3px' }}>Esc</kbd> to pause · all other zones are live-editable</>
          : selectedVert
          ? <>Vertex selected · press <kbd style={{ background: 'rgba(255,255,255,0.1)', borderRadius: 3, padding: '0 3px' }}>Del</kbd> to remove · <kbd style={{ background: 'rgba(255,255,255,0.1)', borderRadius: 3, padding: '0 3px' }}>Ctrl+Z</kbd> to undo · click elsewhere to deselect</>
          : (hoverEdge || otherHoverEdge)
          ? <>Edge — drag down to insert a new vertex on this edge</>
          : <>Click to draw · drag any ◾ vertex to move · hover edge • to insert vertex · right-click ◾ to delete · <kbd style={{ background: 'rgba(255,255,255,0.1)', borderRadius: 3, padding: '0 3px' }}>Ctrl+Z</kbd> undo</>
        }
      </div>
    </div>
  );
};

export default ZoneEditor;
