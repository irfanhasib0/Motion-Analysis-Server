"""Re-Identification (ReID) Service
====================================
Provides person gallery browsing and OSNet-based similarity search
across recorded motion clips.

Storage layout understood by this service
------------------------------------------
New format  (per-clip directory):
    recordings/<cam_id>/<recording_id>/persons/pid_N.jpg   ← body crop
    recordings/<cam_id>/<recording_id>/thumbs/pid_N.jpg    ← full-frame thumb
    recordings/<cam_id>/<recording_id>/persons/pid_N.npy   ← cached embedding

Legacy format (sidecar directories):
    recordings/<cam_id>/<recording_id>_thumbs/pid_N.jpg

Embeddings are computed lazily on first search and cached as .npy sidecar
files next to the .jpg crops.  No vector DB is required at this scale.
"""

import logging
import os
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ─── OSNet path ────────────────────────────────────────────────────────────────
_SRC_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'src')
)
if _SRC_PATH not in sys.path:
    sys.path.insert(0, _SRC_PATH)


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _extract_ts(name: str) -> Optional[int]:
    """Return the trailing integer timestamp from a name like 'cam_id_1234567890'."""
    parts = name.rsplit('_', 1)
    if len(parts) == 2:
        try:
            return int(parts[1])
        except ValueError:
            pass
    return None


def _cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def _batch_cosine_sim(ref: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """Vectorised cosine similarity: ``ref`` (D,) vs ``matrix`` (N, D) → (N,)."""
    ref_norm = np.linalg.norm(ref)
    if ref_norm == 0:
        return np.zeros(matrix.shape[0], dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1)
    norms[norms == 0] = 1.0  # avoid div-by-zero
    return (matrix @ ref) / (norms * ref_norm)


# Maximum number of embeddings loaded into memory in a single search pass.
# When the candidate set exceeds this, the search proceeds in multiple passes,
# each scoring at most this many embeddings before moving on.
_MAX_EMB_BATCH = 500


# ─── Service ───────────────────────────────────────────────────────────────────

class ReIDService:
    """Manages person gallery browsing and OSNet embedding similarity search."""

    def __init__(
        self,
        recordings_dir: str,
        data_dir: Optional[str] = None,
        model_variant: str = 'osnet_x0_25',
    ):
        self.recordings_dir = os.path.abspath(recordings_dir)
        self._model_variant = model_variant
        self._data_dir = os.path.abspath(
            data_dir or os.path.join(
                os.path.dirname(__file__), '..', '..', '..', 'data'
            )
        )
        self._extractor = None
        self._reid_enabled = False
        self._try_load_extractor()

    # ── Initialisation ─────────────────────────────────────────────────────────

    def _try_load_extractor(self) -> None:
        try:
            from improc.osnet_detector import OSNetExtractor
            self._extractor = OSNetExtractor(
                model_variant=self._model_variant,
                data_dir=self._data_dir,
            )
            self._reid_enabled = self._extractor.is_enabled()
        except Exception as exc:
            logger.warning(
                f"ReID model not loaded — gallery browse-only mode. ({exc})"
            )
            self._reid_enabled = False

    # ── Filesystem scan helpers ────────────────────────────────────────────────

    def _camera_dirs(self, camera_id: Optional[str] = None) -> List[str]:
        """Return absolute paths to camera sub-directories under recordings_dir."""
        if camera_id:
            p = os.path.join(self.recordings_dir, camera_id)
            return [p] if os.path.isdir(p) else []
        return [
            os.path.join(self.recordings_dir, d)
            for d in os.listdir(self.recordings_dir)
            if d != 'hls'
            and os.path.isdir(os.path.join(self.recordings_dir, d))
        ]

    def _iter_persons_for_date(
        self, date_str: str, camera_id: Optional[str] = None
    ):
        """
        Yield ``(recording_id, cam_id, persons_dir, timestamp)`` for every
        clip that falls on ``date_str`` and has at least one person crop.
        """
        try:
            dt = datetime.strptime(date_str, '%Y-%m-%d')
        except ValueError:
            return
        day_start = int(dt.timestamp())
        day_end   = day_start + 86_400

        for cam_dir in self._camera_dirs(camera_id):
            cam_id = os.path.basename(cam_dir)
            for entry in os.listdir(cam_dir):
                entry_path = os.path.join(cam_dir, entry)
                if not os.path.isdir(entry_path):
                    continue

                # ── New format: <recording_id>/ containing crops/ persons/ or thumbs/ ──
                c_dir = os.path.join(entry_path, 'crops')
                p_dir = os.path.join(entry_path, 'persons')
                t_dir = os.path.join(entry_path, 'thumbs')
                if os.path.isdir(c_dir) or os.path.isdir(p_dir) or os.path.isdir(t_dir):
                    ts = _extract_ts(entry)
                    if ts and day_start <= ts < day_end:
                        if os.path.isdir(c_dir):
                            chosen = c_dir
                        elif os.path.isdir(p_dir):
                            chosen = p_dir
                        else:
                            chosen = t_dir
                        yield (entry, cam_id, chosen, ts)
                    continue

                # ── Legacy format: <recording_id>_thumbs/ at camera level ─────
                if entry.endswith('_thumbs'):
                    rec_id = entry[:-7]  # strip _thumbs
                    ts = _extract_ts(rec_id)
                    if ts and day_start <= ts < day_end:
                        yield (rec_id, cam_id, entry_path, ts)

    def _person_files(self, persons_dir: str) -> List[Tuple[int, str]]:
        """Return ``[(pid, image_path), ...]`` sorted by pid."""
        result = []
        for f in os.listdir(persons_dir):
            if not f.endswith('.jpg'):
                continue
            name = os.path.splitext(f)[0]  # "pid_67"
            if name.startswith('pid_'):
                try:
                    result.append((int(name[4:]), os.path.join(persons_dir, f)))
                except ValueError:
                    pass
        return sorted(result)

    # ── Public API ────────────────────────────────────────────────────────────

    def get_persons_by_date(
        self,
        date_str: str,
        camera_id: Optional[str] = None,
    ) -> List[Dict]:
        """
        Return a list of person crop descriptors for all clips on ``date_str``.

        Each entry:
            ``{id, recording_id, camera_id, pid, clip_time, persons_dir_type}``
        """
        results: List[Dict] = []
        for rec_id, cam_id, pdir, ts in self._iter_persons_for_date(date_str, camera_id):
            clip_time = datetime.fromtimestamp(ts).isoformat()
            dir_type = os.path.basename(pdir)  # 'persons' | 'thumbs' | legacy dir
            for pid, _ in self._person_files(pdir):
                results.append({
                    'id': f'{rec_id}__pid_{pid}',
                    'recording_id': rec_id,
                    'camera_id': cam_id,
                    'pid': pid,
                    'clip_time': clip_time,
                    'persons_dir_type': dir_type,
                })
        # Sort: newest clip first, then by pid
        results.sort(key=lambda x: (x['clip_time'], x['pid']), reverse=True)
        return results

    def get_persons_by_date_range(
        self,
        date_from: str,
        date_to: str,
        camera_id: Optional[str] = None,
    ) -> List[Dict]:
        """Return person crop descriptors across a date range."""
        try:
            d_from = datetime.strptime(date_from, '%Y-%m-%d').date()
            d_to   = datetime.strptime(date_to,   '%Y-%m-%d').date()
        except ValueError:
            return []
        results: List[Dict] = []
        cur = d_from
        while cur <= d_to:
            results.extend(
                self.get_persons_by_date(cur.strftime('%Y-%m-%d'), camera_id)
            )
            cur += timedelta(days=1)
        # Re-sort across all dates
        results.sort(key=lambda x: (x['clip_time'], x['pid']), reverse=True)
        return results

    def get_person_image_path(
        self, recording_id: str, pid: int
    ) -> Optional[str]:
        """
        Resolve the filesystem path for ``pid_<n>.jpg`` in a clip.
        Tries new-format first, then legacy, then falls back to a full scan.
        """
        # Guess camera dir from recording_id (strip trailing _<ts>)
        cam_hint = '_'.join(recording_id.rsplit('_', 1)[:-1])
        cam_dir = os.path.join(self.recordings_dir, cam_hint)

        candidates = [
            os.path.join(cam_dir, recording_id, 'crops',   f'pid_{pid}.jpg'),
            os.path.join(cam_dir, recording_id, 'persons', f'pid_{pid}.jpg'),
            os.path.join(cam_dir, recording_id, 'thumbs',  f'pid_{pid}.jpg'),
            os.path.join(cam_dir, f'{recording_id}_thumbs', f'pid_{pid}.jpg'),
            os.path.join(cam_dir, f'{recording_id}_persons', f'pid_{pid}.jpg'),
        ]
        for c in candidates:
            if os.path.isfile(c):
                return c

        # Fallback: scan all camera dirs
        for cam_dir in self._camera_dirs():
            for sub in ('crops', 'persons', 'thumbs'):
                p = os.path.join(cam_dir, recording_id, sub, f'pid_{pid}.jpg')
                if os.path.isfile(p):
                    return p
            for suffix in ('_thumbs', '_persons'):
                p = os.path.join(cam_dir, f'{recording_id}{suffix}', f'pid_{pid}.jpg')
                if os.path.isfile(p):
                    return p
        return None

    # ── Embedding I/O ─────────────────────────────────────────────────────────

    def _get_embedding(self, image_path: str) -> Optional[np.ndarray]:
        """Return cached embedding or compute and cache it."""
        npy_path = os.path.splitext(image_path)[0] + '.npy'
        if os.path.isfile(npy_path):
            try:
                return np.load(npy_path)
            except Exception:
                pass

        if not self._reid_enabled or self._extractor is None:
            return None

        try:
            import cv2
            img = cv2.imread(image_path)
            if img is None:
                return None
            emb = self._extractor.extract(img)
        except Exception as exc:
            logger.debug(f"Embedding failed for {image_path}: {exc}")
            return None

        try:
            np.save(npy_path, emb)
        except Exception:
            pass
        return emb

    # ── Similarity Search ─────────────────────────────────────────────────────

    def _build_candidates(self, date_from, date_to, camera_id):
        """Phase 1 shared by search() and search_streaming(): collect all candidate paths."""
        try:
            d_from = datetime.strptime(date_from, '%Y-%m-%d').date()
            d_to   = datetime.strptime(date_to,   '%Y-%m-%d').date()
        except ValueError:
            return None, None

        date_strs: List[str] = []
        cur = d_from
        while cur <= d_to:
            date_strs.append(cur.strftime('%Y-%m-%d'))
            cur += timedelta(days=1)

        Candidate = Tuple[str, str, int, str]  # (rec_id, cam_id, pid, img_path)
        candidates: List[Tuple[Candidate, str]] = []
        for ds in date_strs:
            for rec_id, cam_id_i, pdir, ts in self._iter_persons_for_date(ds, camera_id):
                clip_time = datetime.fromtimestamp(ts).isoformat()
                for pid, img_path in self._person_files(pdir):
                    candidates.append(((rec_id, cam_id_i, pid, img_path), clip_time))
        return candidates, date_strs

    def search(
        self,
        ref_image_path: str,
        date_from: str,
        date_to: str,
        camera_id: Optional[str] = None,
        top_k: int = 20,
        threshold: float = 0.55,
    ) -> Dict:
        """Non-streaming search — collects all results and returns once done."""
        result = None
        for event in self.search_streaming(ref_image_path, date_from, date_to, camera_id, top_k, threshold):
            if event.get('type') == 'done':
                result = event
        if result is None:
            return {'results': [], 'reid_enabled': self._reid_enabled, 'total_scanned': 0}
        return {k: v for k, v in result.items() if k != 'type'}

    def search_streaming(
        self,
        ref_image_path: str,
        date_from: str,
        date_to: str,
        camera_id: Optional[str] = None,
        top_k: int = 20,
        threshold: float = 0.55,
    ):
        """
        Generator that yields progress dicts during search, then a final 'done' dict.

        Yields:
            {'type': 'start',    'total_candidates': int, 'total_batches': int}
            {'type': 'progress', 'pct': int, 'batch': int, 'total_batches': int,
             'scanned': int, 'total_candidates': int, 'found_so_far': int, 'top_sim': float}
            {'type': 'done',     'results': [...], 'reid_enabled': bool, 'total_scanned': int}
        """
        ref_emb = self._get_embedding(ref_image_path)
        if ref_emb is None:
            yield {'type': 'error', 'error': 'no_embedding',
                   'reid_enabled': self._reid_enabled, 'total_scanned': 0}
            return

        candidates, _ = self._build_candidates(date_from, date_to, camera_id)
        if candidates is None:
            yield {'type': 'error', 'error': 'invalid_date',
                   'reid_enabled': self._reid_enabled, 'total_scanned': 0}
            return

        total_candidates = len(candidates)
        total_batches = max(1, (total_candidates + _MAX_EMB_BATCH - 1) // _MAX_EMB_BATCH)

        yield {'type': 'start', 'total_candidates': total_candidates, 'total_batches': total_batches}

        total_scanned = 0
        scored: List[Dict] = []
        top_sim = 0.0

        for batch_idx, batch_start in enumerate(range(0, max(1, total_candidates), _MAX_EMB_BATCH)):
            batch = candidates[batch_start : batch_start + _MAX_EMB_BATCH]

            emb_list: List[np.ndarray] = []
            meta_list: List[Tuple[str, str, int, str]] = []
            for (rec_id, cam_id_i, pid, img_path), clip_time in batch:
                emb = self._get_embedding(img_path)
                total_scanned += 1
                if emb is not None:
                    emb_list.append(emb)
                    meta_list.append((rec_id, cam_id_i, pid, clip_time))

            if emb_list:
                emb_matrix = np.stack(emb_list, axis=0)
                sims = _batch_cosine_sim(ref_emb, emb_matrix)
                del emb_matrix, emb_list

                for idx, sim_val in enumerate(sims):
                    if sim_val >= threshold:
                        rec_id, cam_id_i, pid, clip_time = meta_list[idx]
                        scored.append({
                            'recording_id': rec_id,
                            'camera_id':    cam_id_i,
                            'pid':          pid,
                            'similarity':   round(float(sim_val), 4),
                            'clip_time':    clip_time,
                        })
                        if sim_val > top_sim:
                            top_sim = float(sim_val)

                if len(scored) > top_k * 2:
                    scored.sort(key=lambda x: x['similarity'], reverse=True)
                    scored = scored[:top_k]

            pct = int(((batch_idx + 1) / total_batches) * 100)
            yield {
                'type':             'progress',
                'pct':              pct,
                'batch':            batch_idx + 1,
                'total_batches':    total_batches,
                'scanned':          total_scanned,
                'total_candidates': total_candidates,
                'found_so_far':     len(scored),
                'top_sim':          round(top_sim, 4),
            }

        # Phase 3: deduplicate per-clip
        best_per_clip: Dict[str, Dict] = {}
        for entry in scored:
            key = entry['recording_id']
            if key not in best_per_clip or entry['similarity'] > best_per_clip[key]['similarity']:
                best_per_clip[key] = entry

        final = sorted(best_per_clip.values(), key=lambda x: x['similarity'], reverse=True)
        yield {
            'type':          'done',
            'results':       final[:top_k],
            'reid_enabled':  self._reid_enabled,
            'total_scanned': total_scanned,
        }

    # ── Status & config ───────────────────────────────────────────────────────

    def get_status(self) -> Dict:
        return {
            'reid_enabled':  self._reid_enabled,
            'model_variant': self._model_variant,
        }
