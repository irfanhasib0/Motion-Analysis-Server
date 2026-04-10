/**
 * PersonGallery — Browse detected person crops by date and search for
 * similar appearances across clips using on-device OSNet re-ID.
 *
 * Layout
 * ──────
 *  ┌─ Header: date navigator · camera filter · upload probe · ReID badge ─┐
 *  ├─ Gallery panel (left) ───┬─ Search / results panel (right) ──────────┤
 *  │  3-col crop grid         │  Probe banner                              │
 *  │  click → probe           │  Date range + threshold controls           │
 *  │                          │  [Find similar] button                     │
 *  │                          │  Ranked result cards with similarity bar   │
 *  └──────────────────────────┴────────────────────────────────────────────┘
 *
 * "Ahead of community" features implemented here
 * ───────────────────────────────────────────────
 *  • Upload-probe: drag any external photo to search against the on-device gallery
 *  • Cross-date range search: not limited to one day
 *  • Cosine similarity bar: visual confidence indicator per result
 *  • Graceful degradation: browse-only mode if OSNet model is not loaded
 *  • Camera filter: restrict gallery + search to a single camera
 *  • Per-result clip thumbnail: see the scene context, not just the crop
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { toast } from 'react-hot-toast';
import { Search, Users, Upload, ChevronLeft, ChevronRight, X, Copy, ExternalLink } from 'lucide-react';
import { api } from '../../api';
import './PersonGallery.css';

// ─── Helpers ──────────────────────────────────────────────────────────────────

/** Format ISO datetime string as "HH:MM:SS" */
function fmtTime(iso) {
  if (!iso) return '';
  try { return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }); }
  catch { return iso; }
}

/** Format date as "Mon Apr 08" */
function fmtDate(isoDate) {
  try { return new Date(isoDate + 'T12:00:00').toLocaleDateString([], { weekday: 'short', month: 'short', day: '2-digit' }); }
  catch { return isoDate; }
}

/** Shift an ISO date string by ±N days */
function shiftDate(isoDate, delta) {
  const d = new Date(isoDate + 'T12:00:00');
  d.setDate(d.getDate() + delta);
  return d.toISOString().split('T')[0];
}

/** Color for similarity bar (red → amber → green) */
function simColor(sim) {
  if (sim >= 0.80) return '#2e7d32';
  if (sim >= 0.65) return '#f57f17';
  return '#c62828';
}

// ─── Sub-components ───────────────────────────────────────────────────────────

/** Single person crop card in the gallery grid */
function PersonCard({ person, selected, onSelect }) {
  const imgUrl = api.getPersonImageUrl(person.recording_id, person.pid);
  return (
    <div
      className={`pg-person-card${selected ? ' selected' : ''}`}
      onClick={() => onSelect(person)}
      title={`${person.camera_id}  ·  ${fmtTime(person.clip_time)}`}
    >
      <img src={imgUrl} alt={`pid ${person.pid}`} loading="lazy" />
      <div className="pg-card-meta">
        <span className="pg-cam-badge">{person.camera_id.split('_')[0]}</span>
        <span className="pg-time-badge">{fmtTime(person.clip_time)}</span>
      </div>
      {selected && <div className="pg-selected-indicator">✓</div>}
    </div>
  );
}

/** Skeleton placeholder card while loading */
function SkeletonCard() {
  return <div className="pg-skeleton" />;
}

/** Similarity score bar */
function SimBar({ sim }) {
  const pct = Math.round(sim * 100);
  return (
    <div className="pg-sim-row">
      <div className="pg-sim-bar-bg">
        <div
          className="pg-sim-bar-fill"
          style={{ width: `${pct}%`, background: simColor(sim) }}
        />
      </div>
      <span className="pg-sim-score" style={{ color: simColor(sim) }}>
        {pct}%
      </span>
    </div>
  );
}

/** One result card in the search results list */
function ResultCard({ result, cameras }) {
  const navigate = useNavigate();
  const thumbUrl = api.getRecordingThumbnailUrl(result.recording_id);
  const personUrl = api.getPersonImageUrl(result.recording_id, result.pid);
  const cam = cameras.find(c => c.id === result.camera_id);
  const camLabel = cam ? cam.name : result.camera_id.split('_')[0];

  const handleViewClip = () => {
    // Navigate to EventView; dispatch custom event so EventView can spotlight the recording.
    window.dispatchEvent(new CustomEvent('gallery-focus-recording', { detail: { recording_id: result.recording_id } }));
    navigate('/live');
  };

  const handleCopyId = () => {
    navigator.clipboard.writeText(result.recording_id).then(
      () => toast.success('Recording ID copied'),
      () => toast.error('Copy failed'),
    );
  };

  return (
    <div className="pg-result-card">
      {/* person crop thumbnail */}
      <img
        className="pg-result-thumb"
        src={personUrl}
        alt={`pid ${result.pid}`}
        loading="lazy"
        onError={e => { e.target.style.display = 'none'; }}
      />

      {/* clip thumbnail (scene context) */}
      <img
        className="pg-result-thumb"
        src={thumbUrl}
        alt="clip"
        loading="lazy"
        onError={e => { e.target.style.display = 'none'; }}
      />

      <div className="pg-result-info">
        <div className="pg-result-cam">{camLabel}</div>
        <div className="pg-result-time">{fmtTime(result.clip_time)}  ·  {result.clip_time.split('T')[0]}</div>
        <SimBar sim={result.similarity} />
      </div>

      <div className="pg-result-actions">
        <button className="pg-view-btn" onClick={handleViewClip} title="Open in Event View">
          <ExternalLink size={13} /> View
        </button>
        <button className="pg-copy-btn" onClick={handleCopyId} title="Copy recording ID">
          <Copy size={11} /> ID
        </button>
      </div>
    </div>
  );
}

/** Drag-and-drop upload zone for probe photo */
function UploadProbeZone({ onFile }) {
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef(null);

  const handleDrop = e => {
    e.preventDefault();
    setDragging(false);
    const file = e.dataTransfer.files[0];
    if (file) onFile(file);
  };

  return (
    <div
      className={`pg-dropzone${dragging ? ' drag-over' : ''}`}
      onDragOver={e => { e.preventDefault(); setDragging(true); }}
      onDragLeave={() => setDragging(false)}
      onDrop={handleDrop}
      onClick={() => inputRef.current?.click()}
    >
      <Upload size={22} style={{ marginBottom: 6, opacity: 0.6 }} />
      <div>Drop a photo or click to upload a probe image</div>
      <input
        ref={inputRef}
        type="file"
        accept="image/*"
        style={{ display: 'none' }}
        onChange={e => { if (e.target.files[0]) onFile(e.target.files[0]); }}
      />
    </div>
  );
}

// ─── Main component ───────────────────────────────────────────────────────────

export default function PersonGallery({ cameras = [] }) {
  const today = new Date().toISOString().split('T')[0];

  // ── Gallery state ──
  const [galleryDateFrom, setGalleryDateFrom] = useState(today);
  const [galleryDateTo,   setGalleryDateTo]   = useState(today);
  const [cameraFilter, setCameraFilter]   = useState('');
  const [persons, setPersons]             = useState([]);
  const [galleryLoading, setGalleryLoading] = useState(false);

  // ── Probe / search state ──
  const [probe, setProbe]             = useState(null);   // {recording_id, pid, camera_id, clip_time} | {upload: true, name, url}
  const [searchDateFrom, setSearchDateFrom] = useState(today);
  const [searchDateTo,   setSearchDateTo]   = useState(today);
  const [threshold,   setThreshold]   = useState(0.55);
  const [isSearching, setIsSearching] = useState(false);
  const [searchProgress, setSearchProgress] = useState(null); // {pct, scanned, total, topSim, found}
  const [searchResults, setSearchResults] = useState(null);  // null = not yet searched
  const [reidEnabled,   setReidEnabled]   = useState(false);

  // ── Upload probe ──
  const [uploadFile,    setUploadFile]    = useState(null);  // File object
  const [uploadPreview, setUploadPreview] = useState(null);  // object URL

  // ── Load ReID status once ──
  useEffect(() => {
    api.getReidStatus()
      .then(r => setReidEnabled(r.data?.reid_enabled === true))
      .catch(() => {});
  }, []);

  // ── Load gallery whenever date or camera changes ──
  useEffect(() => {
    let cancelled = false;
    setGalleryLoading(true);
    setPersons([]);
    setProbe(null);
    setSearchResults(null);

    api.getPersonsByDate(galleryDateFrom, galleryDateTo, cameraFilter || null)
      .then(r => { if (!cancelled) setPersons(r.data.persons || []); })
      .catch(() => { if (!cancelled) toast.error('Failed to load gallery'); })
      .finally(() => { if (!cancelled) setGalleryLoading(false); });

    return () => { cancelled = true; };
  }, [galleryDateFrom, galleryDateTo, cameraFilter]);

  // ── Handlers ──

  const handleSelectPerson = useCallback(person => {
    setProbe(person);
    setSearchResults(null);
    // Keep search date range aligned with gallery dates by default
    setSearchDateFrom(galleryDateFrom);
    setSearchDateTo(galleryDateTo);
    // Clean up any previous upload probe
    if (uploadPreview) {
      URL.revokeObjectURL(uploadPreview);
      setUploadFile(null);
      setUploadPreview(null);
    }
  }, [galleryDateFrom, galleryDateTo, uploadPreview]);

  const handleUploadProbe = useCallback(file => {
    if (uploadPreview) URL.revokeObjectURL(uploadPreview);
    const url = URL.createObjectURL(file);
    setUploadFile(file);
    setUploadPreview(url);
    setProbe({ upload: true, name: file.name, url });
    setSearchResults(null);
  }, [uploadPreview]);

  const handleClearProbe = () => {
    if (uploadPreview) URL.revokeObjectURL(uploadPreview);
    setProbe(null);
    setUploadFile(null);
    setUploadPreview(null);
    setSearchResults(null);
  };

  const handleSearch = async () => {
    if (!probe) return;
    setIsSearching(true);
    setSearchResults(null);
    setSearchProgress({ pct: 0, scanned: 0, total: 0, topSim: 0, found: 0 });

    const token = localStorage.getItem('nvr_access_token');
    const authHeaders = token ? { Authorization: `Bearer ${token}` } : {};
    const API_BASE = process.env.NODE_ENV === 'production' ? window.location.origin : 'http://localhost:9001';

    try {
      let response;
      if (probe.upload) {
        const form = new FormData();
        form.append('file', uploadFile);
        const params = new URLSearchParams({
          date_from: searchDateFrom, date_to: searchDateTo,
          top_k: 30, threshold,
          ...(cameraFilter ? { camera_id: cameraFilter } : {}),
        });
        response = await fetch(
          `${API_BASE}/api/persons/search/upload/stream?${params}`,
          { method: 'POST', headers: authHeaders, body: form },
        );
      } else {
        response = await fetch(
          `${API_BASE}/api/persons/search/stream`,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', ...authHeaders },
            body: JSON.stringify({
              ref_recording_id: probe.recording_id,
              ref_pid:          probe.pid,
              date_from:        searchDateFrom,
              date_to:          searchDateTo,
              camera_id:        cameraFilter || null,
              top_k:            30,
              threshold,
            }),
          }
        );
      }

      if (!response.ok) {
        const detail = await response.text();
        throw new Error(detail || `HTTP ${response.status}`);
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop();
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          let evt;
          try { evt = JSON.parse(line.slice(6)); } catch { continue; }
          if (evt.type === 'start') {
            setSearchProgress({ pct: 0, scanned: 0, total: evt.total_candidates, topSim: 0, found: 0 });
          } else if (evt.type === 'progress') {
            setSearchProgress({ pct: evt.pct, scanned: evt.scanned, total: evt.total_candidates, topSim: evt.top_sim, found: evt.found_so_far });
          } else if (evt.type === 'done') {
            setSearchResults(evt);
            setSearchProgress(null);
            if (!evt.reid_enabled) toast('ReID model not loaded — similarity search unavailable.', { icon: 'ℹ️' });
          } else if (evt.type === 'error') {
            throw new Error(evt.error);
          }
        }
      }
    } catch (e) {
      toast.error('Search failed: ' + (e?.message || String(e)));
      setSearchProgress(null);
    } finally {
      setIsSearching(false);
    }
  };

  // ── Probe image URL ──
  const probeImgUrl = probe
    ? probe.upload
      ? probe.url
      : api.getPersonImageUrl(probe.recording_id, probe.pid)
    : null;

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <div className="pg-root">

      {/* ── Header ── */}
      <div className="pg-header">
        <div className="pg-title">
          <Users size={22} />
          Person Gallery
        </div>

        <div className="pg-controls">
          {/* Date range navigator */}
          <div className="pg-date-nav">
            <button onClick={() => {
              setGalleryDateFrom(d => shiftDate(d, -1));
              setGalleryDateTo(d => shiftDate(d, -1));
            }} title="Shift range back 1 day">
              <ChevronLeft size={16} />
            </button>
            <label className="pg-date-range-label">From</label>
            <input
              type="date"
              value={galleryDateFrom}
              onChange={e => {
                const v = e.target.value;
                setGalleryDateFrom(v);
                if (v > galleryDateTo) setGalleryDateTo(v);
              }}
              style={{ cursor: 'pointer', background: 'none', border: 'none', color: '#00695c', fontWeight: 600, fontSize: 13, outline: 'none', width: 130 }}
            />
            <label className="pg-date-range-label">To</label>
            <input
              type="date"
              value={galleryDateTo}
              onChange={e => {
                const v = e.target.value;
                setGalleryDateTo(v);
                if (v < galleryDateFrom) setGalleryDateFrom(v);
              }}
              style={{ cursor: 'pointer', background: 'none', border: 'none', color: '#00695c', fontWeight: 600, fontSize: 13, outline: 'none', width: 130 }}
            />
            <button onClick={() => {
              setGalleryDateFrom(d => shiftDate(d, 1));
              setGalleryDateTo(d => shiftDate(d, 1));
            }} title="Shift range forward 1 day">
              <ChevronRight size={16} />
            </button>
          </div>

          {/* Camera filter */}
          <select
            className="pg-cam-select"
            value={cameraFilter}
            onChange={e => setCameraFilter(e.target.value)}
          >
            <option value="">All cameras</option>
            {cameras.map(c => (
              <option key={c.id} value={c.id}>{c.name || c.id}</option>
            ))}
          </select>

          {/* Upload probe */}
          <label className="pg-upload-btn" title="Search by uploading a photo">
            <Upload size={14} />
            Upload probe
            <input
              type="file"
              accept="image/*"
              style={{ display: 'none' }}
              onChange={e => { if (e.target.files[0]) handleUploadProbe(e.target.files[0]); }}
            />
          </label>

          {/* ReID badge */}
          <div className={`pg-reid-badge ${reidEnabled ? 'active' : 'inactive'}`}>
            {reidEnabled ? '● ReID on' : '○ Browse only'}
          </div>
        </div>
      </div>

      {/* ── Body ── */}
      <div className="pg-body">

        {/* ─ Left: Gallery grid ─ */}
        <div className="pg-gallery-panel">
          <div className="pg-card" style={{ flex: 1 }}>
            <div className="pg-section-label">
              {galleryDateFrom === galleryDateTo
                ? <>{fmtDate(galleryDateFrom)} · {persons.length} person{persons.length !== 1 ? 's' : ''}</>
                : <>{fmtDate(galleryDateFrom)} – {fmtDate(galleryDateTo)} · {persons.length} person{persons.length !== 1 ? 's' : ''}</>
              }
            </div>

            {galleryLoading ? (
              <div className="pg-grid">
                {Array.from({ length: 9 }).map((_, i) => <SkeletonCard key={i} />)}
              </div>
            ) : persons.length === 0 ? (
              <div className="pg-empty">
                <Users size={32} opacity={0.4} />
                No person crops found for this date.
                <span style={{ fontSize: 12, opacity: 0.7 }}>Try a different date or camera.</span>
              </div>
            ) : (
              <div className="pg-grid">
                {persons.map(p => (
                  <PersonCard
                    key={p.id}
                    person={p}
                    selected={
                      probe && !probe.upload &&
                      probe.recording_id === p.recording_id &&
                      probe.pid === p.pid
                    }
                    onSelect={handleSelectPerson}
                  />
                ))}
              </div>
            )}
          </div>

          {/* Drag-drop upload */}
          <UploadProbeZone onFile={handleUploadProbe} />
        </div>

        {/* ─ Right: Search panel ─ */}
        <div className="pg-results-panel">

          {!probe ? (
            <div className="pg-card" style={{ flex: 1 }}>
              <div className="pg-hint">
                <div className="pg-hint-icon">🔍</div>
                <strong>Select a person from the gallery</strong>
                <span>
                  Click any crop on the left — or drop a photo — to search<br />
                  for matching appearances across your recorded clips.
                </span>
              </div>
            </div>
          ) : (
            <>
              {/* Probe banner */}
              <div className="pg-card">
                <div className="pg-section-label">Search probe</div>
                <div className="pg-probe-banner">
                  {probeImgUrl && (
                    <img className="pg-probe-img" src={probeImgUrl} alt="probe" />
                  )}
                  <div className="pg-probe-info">
                    {probe.upload ? (
                      <>
                        <div className="pg-probe-name">Uploaded: {probe.name}</div>
                        <div className="pg-probe-sub">External probe photo</div>
                      </>
                    ) : (
                      <>
                        <div className="pg-probe-name">PID {probe.pid}</div>
                        <div className="pg-probe-sub">{probe.camera_id} · {fmtTime(probe.clip_time)}</div>
                      </>
                    )}
                  </div>
                  <button className="pg-probe-clear" onClick={handleClearProbe} title="Clear probe">
                    <X size={16} />
                  </button>
                </div>
              </div>

              {/* Search controls */}
              <div className="pg-card">
                <div className="pg-section-label">Search parameters</div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                  <div className="pg-search-controls">
                    <label>From</label>
                    <input type="date" value={searchDateFrom} onChange={e => setSearchDateFrom(e.target.value)} />
                    <label>To</label>
                    <input type="date" value={searchDateTo}   onChange={e => setSearchDateTo(e.target.value)} />
                  </div>

                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 10 }}>
                    <div className="pg-threshold-row">
                      <label>Min similarity</label>
                      <input
                        type="range"
                        min={0.3} max={0.95} step={0.05}
                        value={threshold}
                        onChange={e => setThreshold(parseFloat(e.target.value))}
                      />
                      <span className="pg-threshold-val">{Math.round(threshold * 100)}%</span>
                    </div>

                    <button
                      className="pg-search-btn"
                      onClick={handleSearch}
                      disabled={isSearching || !reidEnabled}
                      title={!reidEnabled ? 'ReID model not loaded' : ''}
                    >
                      {isSearching
                        ? <><div className="pg-spinner" /> Searching…</>
                        : <><Search size={14} /> Find similar</>
                      }
                    </button>

                    {isSearching && searchProgress && (
                      <div className="pg-progress-wrap">
                        <div className="pg-progress-track">
                          <div className="pg-progress-fill" style={{ width: `${searchProgress.pct}%` }} />
                        </div>
                        <div className="pg-progress-meta">
                          <span>{searchProgress.pct}%{searchProgress.total > 0 ? ` · ${searchProgress.scanned}/${searchProgress.total} crops` : ''}</span>
                          {searchProgress.topSim > 0 && (
                            <span className="pg-progress-topsim">best {Math.round(searchProgress.topSim * 100)}%</span>
                          )}
                          {searchProgress.found > 0 && (
                            <span>{searchProgress.found} found</span>
                          )}
                        </div>
                      </div>
                    )}
                  </div>

                  {!reidEnabled && (
                    <div style={{ fontSize: 12, color: '#b71c1c', fontStyle: 'italic' }}>
                      OSNet model not loaded — export the OSNet ONNX weights and restart the server to enable similarity search.
                    </div>
                  )}
                </div>
              </div>

              {/* Results */}
              {searchResults && (
                <div className="pg-card" style={{ flex: 1 }}>
                  <div className="pg-section-label">Results</div>
                  {searchResults.results?.length > 0 ? (
                    <>
                      <div className="pg-summary">
                        {searchResults.results.length} match{searchResults.results.length !== 1 ? 'es' : ''} found
                        {' '}<span>· {searchResults.total_scanned ?? '?'} crops scanned</span>
                      </div>
                      <div className="pg-results-list">
                        {searchResults.results.map((r, i) => (
                          <ResultCard key={`${r.recording_id}-${r.pid}-${i}`} result={r} cameras={cameras} />
                        ))}
                      </div>
                    </>
                  ) : (
                    <div className="pg-no-results">
                      <Search size={30} opacity={0.4} />
                      <strong>No matches found</strong>
                      <span>Try lowering the similarity threshold or widening the date range.</span>
                    </div>
                  )}
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
