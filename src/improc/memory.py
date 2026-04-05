import cv2
import numpy as np
from collections import defaultdict
from matplotlib import pyplot as plt


class _RingBuffer2D:
    """Fixed-capacity circular buffer backed by a pre-allocated numpy array.

    Behaves like deque(maxlen=capacity) but stores (x, y) positions
    directly in a contiguous float32 array — no Python list allocation
    per append and no list→array conversion when reading.
    """
    __slots__ = ('data', 'capacity', 'head', 'count')

    def __init__(self, capacity):
        self.data = np.zeros((capacity, 2), dtype=np.float32)
        self.capacity = capacity
        self.head = 0   # Next write index
        self.count = 0  # Valid entries (≤ capacity)

    def append(self, x, y):
        """Write one (x, y) position, overwriting the oldest if full."""
        self.data[self.head, 0] = x
        self.data[self.head, 1] = y
        self.head = (self.head + 1) % self.capacity
        if self.count < self.capacity:
            self.count += 1

    def get_ordered(self):
        """Return all stored positions in chronological order [count, 2]."""
        return self.get_last_n(self.count)

    def get_last_n(self, n):
        """Return last n positions in chronological order [n, 2].

        Returns a numpy view (zero-copy) when the slice is contiguous,
        or a concatenated copy when the window wraps around the buffer.
        """
        n = min(n, self.count)
        if n == 0:
            return self.data[:0]  # empty [0, 2]
        start = (self.head - n) % self.capacity
        if start < self.head:
            return self.data[start:self.head]       # Contiguous — numpy view
        # Wrapped: tail portion + head portion
        return np.concatenate([self.data[start:], self.data[:self.head]])

class FlowMemory:
    """Stores per-keypoint position history and builds smoothed trajectories.

    Each tracked object (PID) can have multiple keypoints (KPID).
    Positions are stored in numpy ring buffers keyed by "pid|kpid".
    Once enough positions accumulate (≥ min_traj_len), a smoothed
    trajectory is available for velocity calculation and visualization.

    Args:
        maxpid:         PID wrap-around limit (pid % maxpid) to bound ID space.
        min_traj_len:   Minimum positions before a smoothed trajectory is built.
        max_traj_len:   Maximum positions kept per keypoint (ring buffer capacity).
        keep_last_seen: Frames a disappeared object stays eligible for trajectory selection.
    """

    # Detection types considered as "person" for counting/density
    PERSON_TYPES = frozenset(('person', 'yolox_person', 'face', 'body'))

    def __init__(self, maxpid=2500, min_traj_len=10, max_traj_len=100, keep_last_seen=90):
        self.max_traj_len = max_traj_len
        # "pid|kpid" -> _RingBuffer2D (replaces deque + separate motion_trajs)
        self._buffers = {}
        # pid -> {'total_seen': int, 'last_seen': int}
        self.pid_hist = {}
        # "pid|kpid" -> total buffer count (used for ranking)
        self.traj_len = {}
        # pid -> {type_str: int} — detection type histogram per object
        self._type_counts = defaultdict(lambda: defaultdict(int))

        self.maxpid = maxpid
        self.maxkpid = 6
        self.rolling_win = 1  # Rolling mean window size (1 = no smoothing)
        self.min_traj_len = min_traj_len
        self.keep_last_seen = keep_last_seen

    @property
    def motion_trajs(self):
        """Backward-compatible dict view: traj_key -> smoothed [L, 2] array.

        Used by eval.py and coreset. Reads directly from ring buffers
        without maintaining a separate dict.
        """
        result = {}
        for traj_key, buf in self._buffers.items():
            if buf.count >= self.min_traj_len:
                result[traj_key] = self._get_smoothed(buf)
        return result

    def _get_smoothed(self, buf):
        """Extract smoothed trajectory from a ring buffer.

        When rolling_win == 1 (default), returns the raw last-N slice
        directly — no convolution overhead.
        """
        recent = buf.get_last_n(self.min_traj_len)
        if self.rolling_win <= 1:
            # No smoothing needed — return the numpy slice as-is
            return recent.copy()  # Copy to ensure caller owns the data
        kernel = np.ones(self.rolling_win, dtype=np.float32) / self.rolling_win
        smoothed_x = np.convolve(recent[:, 0], kernel, mode='same')
        smoothed_y = np.convolve(recent[:, 1], kernel, mode='same')
        return np.column_stack((smoothed_x, smoothed_y))

    def fill_neg1(self, a):
        """Interpolate -1 sentinel values in a 1D array using linear interpolation."""
        a = a.astype(float)
        x = np.arange(a.size)
        valid = a != -1
        if valid.sum() == 0:
            return a
        a[~valid] = np.interp(x[~valid], x[valid], a[valid])
        return a

    def fill_array_neg1(self, arr_2d):
        """Interpolate -1 sentinels in a 2D array, row-wise then column-wise."""
        filled = np.apply_along_axis(self.fill_neg1, axis=1, arr=arr_2d)
        filled = np.apply_along_axis(self.fill_neg1, axis=0, arr=filled)
        return filled

    def add(self, pts):
        """Add current frame's tracked points to memory.

        1. Age all existing PIDs: last_seen += 1
        2. For each currently visible PID:
           - Reset last_seen = 0, increment total_seen
           - Append each keypoint's (x, y) to its ring buffer
           - Update traj_len once buffer count ≥ min_traj_len

        Args:
            pts: dict[int, dict] from _copy_pts(prev_pts).
                 Each value must contain 'keypoints_1' array of shape [K, 2].
        """
        # Step 1: Age all tracked PIDs (those not seen this frame will drift upward)
        for pid in self.pid_hist:
            self.pid_hist[pid]['last_seen'] += 1

        # Step 2: Process currently visible objects
        for orig_pid in pts:
            pid = orig_pid % self.maxpid  # Wrap to bounded ID space
            if pid not in self.pid_hist:
                self.pid_hist[pid] = {'last_seen': 0, 'total_seen': 0}
            self.pid_hist[pid]['last_seen'] = 0
            self.pid_hist[pid]['total_seen'] += 1

            # Track detection type histogram for this PID
            det_type = pts[orig_pid].get('type', 'motion')
            self._type_counts[pid][det_type] += 1

            keypoints = pts[orig_pid]['keypoints_1']
            for kpid in range(len(keypoints)):
                x, y = keypoints[kpid].ravel()
                traj_key = f'{pid}|{kpid}'

                # Get or create ring buffer for this keypoint
                buf = self._buffers.get(traj_key)
                if buf is None:
                    buf = _RingBuffer2D(self.max_traj_len)
                    self._buffers[traj_key] = buf

                # Append directly to numpy buffer (no Python list creation)
                buf.append(float(x), float(y))

                # Record buffer count for ranking once we have enough positions
                if buf.count >= self.min_traj_len:
                    self.traj_len[traj_key] = buf.count

    def classify_pid(self, pid, min_ratio=0.2):
        """Return the dominant detection type for *pid*.

        Returns the type that appears most often, but only if it accounts
        for at least *min_ratio* of all observations.  Falls back to 'motion'.
        """
        counts = self._type_counts.get(pid)
        if not counts:
            return 'motion'
        total = sum(counts.values())
        best_type, best_count = max(counts.items(), key=lambda kv: kv[1])
        if best_count / total >= min_ratio:
            return best_type
        return 'motion'

    def get_person_count(self, current_pids=()):
        """Count currently visible PIDs that have at least one person-type detection.

        A PID is counted as a "person" if any observation of a PERSON_TYPES
        detection type has been recorded (count > 0), regardless of the ratio.
        Only PIDs in *current_pids* (visible this frame) are counted.
        """
        count = 0
        for pid in current_pids:
            wpid = pid % self.maxpid
            type_counts = self._type_counts.get(wpid, {})
            if any(type_counts.get(t, 0) > 0 for t in self.PERSON_TYPES):
                count += 1
        return count

    def get_sorted_traj_ids(self, curr_pids=(), num_of_kpts=5):
        """Select top trajectory IDs for visualization, preferring visible objects.

        Trajectories are ranked by total deque length (longest first).
        Only trajectories with last_seen ≤ keep_last_seen are eligible.
        Currently visible PIDs are preferred; recently disappeared PIDs
        backfill remaining slots.

        Args:
            curr_pids: Currently visible PID set (from pts.keys()).
            num_of_kpts: Maximum number of trajectory IDs to return.

        Returns:
            List[str]: Up to num_of_kpts trajectory IDs like ["3|0", "5|1"].
                       Sorted alphabetically for consistent frame-to-frame ordering.
        """
        # Sort all trajectory IDs by length, longest first
        all_ids = sorted(self.traj_len, key=self.traj_len.get, reverse=True)

        if len(curr_pids) == 0:
            # No current PIDs — return longest trajectories that are still recent
            valid_ids = [
                tid for tid in all_ids
                if self.pid_hist[int(tid.split('|')[0])]['last_seen'] <= self.keep_last_seen
            ]
            return sorted(valid_ids[:num_of_kpts])

        # Split into currently visible vs recently disappeared
        visible_ids = []
        stale_ids = []
        for tid in all_ids:
            pid = int(tid.split('|')[0])
            if pid in curr_pids:
                visible_ids.append(tid)
            elif self.pid_hist[pid]['last_seen'] <= self.keep_last_seen:
                stale_ids.append(tid)

        # Prefer visible; backfill with recently disappeared if not enough
        if len(visible_ids) < num_of_kpts:
            visible_ids += stale_ids[:num_of_kpts - len(visible_ids)]

        return sorted(visible_ids)

    def get_traj_velocities(self, traj_id=None, traj=None):
        """Compute frame-to-frame Euclidean speed along a trajectory.

        speed[i] = sqrt((x[i+1]-x[i])² + (y[i+1]-y[i])²)

        Args:
            traj_id: Key into ring buffers. Used if traj is None.
            traj:    Direct trajectory array [L, 2]. Overrides traj_id.

        Returns:
            np.ndarray of shape [L-1] with per-step speeds.
        """
        if traj is None:
            assert traj_id is not None, "Either traj_id or traj must be provided"
            buf = self._buffers.get(traj_id)
            if buf is None or buf.count < 2:
                return np.array([], dtype=np.float32)
            traj = self._get_smoothed(buf)
        velocities = np.sqrt(np.sum(np.diff(traj, axis=0) ** 2, axis=1))
        return np.nan_to_num(velocities, nan=0.0, posinf=0.0, neginf=0.0)

    def get_pid_traj_span(self, pid):
        """Return (traj_w, traj_h) — the spatial bounding-box extent of all trajectory
        points recorded for *pid* across every keypoint slot.

        Returns (0.0, 0.0) when no trajectory data is available.
        """
        arrays = []
        for kpid in range(self.maxkpid):
            buf = self._buffers.get(f'{pid}|{kpid}')
            if buf is not None and buf.count > 0:
                arrays.append(buf.get_ordered())
        if not arrays:
            return 0.0, 0.0
        all_pts = np.concatenate(arrays, axis=0)
        traj_w = float(all_pts[:, 0].max() - all_pts[:, 0].min())
        traj_h = float(all_pts[:, 1].max() - all_pts[:, 1].min())
        return traj_w, traj_h

    def save(self):
        pass

class CoresetMemory:
    """
    Coreset-style memory for motion trajectories.

    Stores fixed-length trajectories for keypoints and selects
    representative prototypes using a greedy k-center algorithm.

    Trajectory embedding: by default, uses normalized deltas of positions
    over the trajectory window to be translation-invariant and roughly
    scale-aware.
    """
    def __init__(self, sample_len=25, normalize=False, max_items=5000):
        self.sample_len = int(sample_len)
        self.normalize = bool(normalize)
        self.max_items = int(max_items)
        # Raw stored trajectories: dict traj_id -> np.ndarray [L, 2]
        self.trajectories = {}
        # Cached embeddings: dict traj_id -> np.ndarray [D]
        self.embeddings = {}
        # Incremental buffers per key: dict traj_key -> deque of points
        self.buffers = defaultdict(lambda: deque(maxlen=self.sample_len))
        self._viz_pos = None
        self._viz_vel = None

    def reset(self):
        self.trajectories.clear()
        self.embeddings.clear()
        self.buffers.clear()

    def add_point(self, traj_key, point):
        """
        Incrementally add a point to the buffer for `traj_key`.
        When the buffer reaches `sample_len`, snapshot it into memory.
        traj_key: any hashable id (e.g., f"obj:{obj_id}|kp:{kp_idx}")
        point: (x, y)
        """
        buf = self.buffers[traj_key]
        buf.append((float(point[0]), float(point[1])))
        if len(buf) >= self.sample_len:
            traj = np.array(buf, dtype=np.float32)
            traj_id = f"{traj_key}|start:{len(self.trajectories)}"
            self._store_trajectory(traj_id, traj)
    
    def add_n_traj(self, trajs):
        for traj_id, traj in trajs.items():
            dist = np.abs(traj[:-1] - traj[1:]).max(-1)
            dist = np.concatenate([dist, np.array([dist[-1]])], axis=0)
            val  = (dist < 10.0) & (traj.min(axis=-1).flatten() > 5.0)
            
            count = 0
            max_count = 0
            best_traj = traj[:0] 
            for _ind  in range(0,len(val)):
                if val[_ind]:
                    count +=1
                else:
                    count =0
                if count > max_count:
                    max_count = count
                    best_traj = traj[_ind - count +1: _ind +1]
            traj = best_traj
            #if traj.shape[0] < 5:
            #    continue
            #import pdb; pdb.set_trace()
            self.add_traj(traj_id, traj)

    def add_traj(self, traj_id, traj_points):
        """
        Directly add a trajectory array [L, 2] with a specific id.
        """
        traj = np.asarray(traj_points, dtype=np.float32)
        if traj.shape[0] < self.sample_len:
            # Pad by repeating last point to reach sample_len
            pad_len = self.sample_len - traj.shape[0]
            if traj.shape[0] == 0:
                return
            pad = np.repeat(traj[-1:], pad_len, axis=0)
            traj = np.vstack([traj, pad])
        elif traj.shape[0] > self.sample_len:
            # Uniformly sample to sample_len
            idx = np.linspace(0, traj.shape[0]-1, self.sample_len).astype(int)
            traj = traj[idx]
        self._store_trajectory(traj_id, traj)

    def _store_trajectory(self, traj_id, traj):
        if len(self.trajectories) >= self.max_items:
            # Drop oldest item to bound memory
            oldest_id = next(iter(self.trajectories))
            self.trajectories.pop(oldest_id, None)
            self.embeddings.pop(oldest_id, None)
        self.trajectories[traj_id] = traj
        self.embeddings[traj_id] = self._embed(traj)

    def _embed(self, traj):
        """
        Convert trajectory [L,2] to embedding [D]. Default: normalized deltas.
        - Subtract first point (translation invariance)
        - Optionally scale by path length (approximate scale invariance)
        - Flatten to 2*L vector
        """
        deltas = traj - traj[0]
        if self.normalize:
            # Scale by total displacement magnitude (avoid divide-by-zero)
            scale = np.linalg.norm(deltas[-1]) + 1e-6
            deltas = deltas / scale
        return deltas.reshape(-1)

    def _draw_gradient_polyline(self, canvas, pts, color_start, color_end, thickness=2, lineType=cv2.LINE_AA):
        """Draw a polyline with a color gradient from start to end.
        - canvas: image to draw on
        - pts: np.ndarray of shape [N, 2] with integer coordinates
        - color_start/color_end: BGR tuples (ints 0-255)
        - thickness: line thickness
        - lineType: cv2 line type (default anti-aliased)
        """
        if pts is None or len(pts) < 2:
            return
        # Ensure integer tuples for cv2.line
        pts = np.asarray(pts, dtype=np.int32)
        n_seg = len(pts) - 1
        if n_seg <= 0:
            return
        c0 = np.array(color_start, dtype=np.float32)
        c1 = np.array(color_end, dtype=np.float32)
        for i in range(n_seg):
            # t ranges 0..1 across segments
            t = 0.0 if n_seg == 1 else (i / float(n_seg - 1))
            c = (1.0 - t) * c0 + t * c1
            color = tuple(int(round(v)) for v in c.tolist())
            p0 = (int(pts[i, 0]), int(pts[i, 1]))
            p1 = (int(pts[i+1, 0]), int(pts[i+1, 1]))
            cv2.line(canvas, p0, p1, color, thickness=thickness, lineType=lineType)

    def select_kcenter(self, k=32):
        """
        Greedy k-center selection over current embeddings.
        Returns (selected_ids, centers_embeddings).
        """
        k = int(max(1, k))
        ids = list(self.embeddings.keys())
        if not ids:
            return [], np.empty((0, 0), dtype=np.float32)
        X = np.stack([self.embeddings[i] for i in ids], axis=0)  # [N, D]
        N = X.shape[0]
        # Start with the point farthest from the mean
        centroid = X.mean(axis=0, keepdims=True)
        dists = np.linalg.norm(X - centroid, axis=1)
        first = int(np.argmax(dists))
        selected_idx = [first]
        # Maintain min distance to current selected set
        min_d = np.linalg.norm(X - X[first], axis=1)
        while len(selected_idx) < min(k, N):
            nxt = int(np.argmax(min_d))
            selected_idx.append(nxt)
            # Update min distances
            min_d = np.minimum(min_d, np.linalg.norm(X - X[nxt], axis=1))
        sel_ids = [ids[i] for i in selected_idx]
        centers = X[selected_idx]
        return sel_ids, centers
    
    def clear_viz(self):
        """Clear visualization buffers."""
        self._viz_pos = None
        self._viz_vel = None

    def viz_k_centers(self, _viz_frame, k=2):
        """Return trajectories of selected k-center prototypes."""
        if self._viz_pos is None or self._viz_vel is None:
            self._viz_pos = np.zeros_like(_viz_frame)
            self._viz_vel = np.zeros_like(_viz_frame)
        sel_ids, _ = self.select_kcenter(k)
        H, W = _viz_frame.shape[:2]
        for _id, traj in self.trajectories.items():
        #for _id, traj in self.embeddings.items():#self.trajectories.items():
            traj =  traj.reshape(self.sample_len, 2)
            #traj -= traj.min(axis=0)
            traj *= np.array([1,1])
            color = [0, 255, 0] if _id in sel_ids else [0, 0, 255]

            pts = traj.astype(np.int32)
            
            idx = (pts.min(-1)>5).flatten() & (pts[:,0]<W-5) & (pts[:,1]<H-5)
            pts = pts[idx]
            # Draw polyline and small circles along the trajectory
            if _id in sel_ids:
                # Gradient from red (start) to green (end) in BGR
                self._draw_gradient_polyline(self._viz_pos, pts[1:-1], (255, 0, 0), (0, 255, 0), thickness=4)
            else:
                # Thin gray gradient for non-selected (kept for completeness)
                self._draw_gradient_polyline(self._viz_vel, pts[1:-1], (255, 0, 0), (0, 255, 0), thickness=2)
            '''
            vel = np.diff(pts, axis=0)
            vel = (vel[:,0]**2 + vel[:,1]**2)
            vel = np.sqrt(vel)
            vel = np.concatenate([8*np.arange(vel.shape[0])[:, None], 20*vel[:, None]], axis=1).astype(np.int32)
            cv2.polylines(self._viz_vel, [vel.reshape(-1, 1, 2)], False, color, 1)
            '''
            '''
            vel_pts = pts[:-1] + vel // 2
            for (x, y), (dx, dy) in zip(vel_pts, vel):
                vel = 2*np.sqrt(dx*dx + dy*dy) + 1e-6
                cv2.circle(self._viz_vel, (x, y), int(vel), color, 1)
            '''
            #cv2.polylines(_viz_vel, [pts.reshape(-1, 1, 2)], False, color, 1)
            #for x, y in pts:
            #    cv2.circle(_viz_frame, (int(x), int(y)), 2, color, -1)
        #import pdb; pdb.set_trace()
        return self._viz_pos, self._viz_vel
    
    def get_items(self):
        """Return (ids, trajectories, embeddings) for all stored items."""
        ids = list(self.trajectories.keys())
        trajs = [self.trajectories[i] for i in ids]
        embs = [self.embeddings[i] for i in ids]
        return ids, trajs, embs
    
    def best_matching_trajectory(memory, query, metric="euclidean"):
        """
        Find the best matching stored trajectory in `memory` for the given `query`.
        - memory: CoresetMemory instance
        - query: np.ndarray trajectory [L,2] or embedding vector [D]
        - metric: "euclidean" or "cosine"
        Returns (traj_id, trajectory, distance). If no items, returns (None, None, np.inf).
        """
        ids = list(memory.embeddings.keys())
        if not ids:
            return None, None, float("inf")

        q = np.asarray(query, dtype=np.float32)
        if q.ndim == 2 and q.shape[1] == 2:
            # Normalize length to memory.sample_len
            L = memory.sample_len
            if q.shape[0] < L:
                if q.shape[0] == 0:
                    return None, None, float("inf")
                pad = np.repeat(q[-1:], L - q.shape[0], axis=0)
                q = np.vstack([q, pad])
            elif q.shape[0] > L:
                idx = np.linspace(0, q.shape[0] - 1, L).astype(int)
                q = q[idx]
            q_emb = memory._embed(q)
        else:
            q_emb = q.reshape(-1)

        X = np.stack([memory.embeddings[i] for i in ids], axis=0)  # [N, D]
        if metric == "cosine":
            qn = np.linalg.norm(q_emb) + 1e-8
            Xn = np.linalg.norm(X, axis=1) + 1e-8
            sims = (X @ q_emb) / (Xn * qn)
            best_idx = int(np.argmax(sims))
            dist = 1.0 - float(sims[best_idx])
        else:
            diffs = X - q_emb
            dists = np.linalg.norm(diffs, axis=1)
            best_idx = int(np.argmin(dists))
            dist = float(dists[best_idx])

        best_id = ids[best_idx]
        return best_id, memory.trajectories[best_id], dist