# ByteTrack Integration Changelog

## 2026-03-23 — Fix imports and dependencies for local integration

ByteTrack was cloned into `src/improc/bytetrack/` but the import paths assumed it was
installed as a top-level package. These fixes make ByteTrack work as a local subpackage
without `pip install` or modifying `PYTHONPATH` manually.

### Files Modified

#### `yolox/__init__.py`
- **Removed** heavy `configure_module()` call and cascading util imports.
  The original imported the full YOLOX framework utils (allreduce_norm, boxes, checkpoint,
  demo_utils, dist, ema, logger, lr_scheduler, metric, model_utils, setup_env, visualize)
  which aren't needed for tracker-only usage and caused import errors.
- **Kept** only `__version__`.

#### `yolox/tracker/__init__.py` (NEW)
- **Created** empty `__init__.py` — required for Python to recognize `yolox.tracker`
  as a subpackage. Was missing, causing relative imports within the tracker to fail.

#### `yolox/tracker/byte_tracker.py`
- **Changed** `import torch` / `import torch.nn.functional as F` → optional try/except.
  torch is only used for `.cpu().numpy()` tensor conversion in the `else` branch of
  `BYTETracker.update()`. Our pipeline always sends numpy arrays (shape[1]==5 path),
  so torch is not required at runtime.
- **Changed** `from yolox.tracker import matching` → `from . import matching` (relative import).
  The absolute import required `yolox` to be a top-level package on sys.path.
- **Added** `hasattr(output_results, 'cpu')` guard before calling `.cpu().numpy()`.

#### `yolox/tracker/matching.py`
- **Replaced** `import lap` → `from scipy.optimize import linear_sum_assignment`.
  The `lap` package (Linear Assignment Problem) was not installed. scipy provides
  equivalent functionality via `linear_sum_assignment`.
- **Rewrote** `linear_assignment()` function to use `scipy.optimize.linear_sum_assignment`
  instead of `lap.lapjv()`, with threshold filtering for matches.
- **Changed** `from yolox.tracker import kalman_filter` → `from . import kalman_filter`
  (relative import).

### Files Modified Outside bytetrack/

#### `src/trackers/trackers.py`
- **Added** `sys.path` manipulation at module level to make `yolox` importable:
  ```python
  _bytetrack_path = os.path.join(os.path.dirname(__file__), '..', 'improc', 'bytetrack')
  sys.path.insert(0, os.path.abspath(_bytetrack_path))
  ```
- **Changed** ByteTracker class imports from `from bytetrack.yolox.tracker...` →
  `from yolox.tracker...` (now resolves via the sys.path addition).
- **Added** `_bytetrack_available` flag with try/except — previously the except block
  was commented out, causing a bare `NameError` on `BYTETracker` if import failed.
- **Added** guard in `__init__` — raises clear `ImportError` instead of cryptic `NameError`.

### Previous Manual Patches (pre-existing, not changed)
- `matching.py`: `cython_bbox` → pure numpy `numpy_bbox_ious()` implementation
- `byte_tracker.py` / matching: `np.float` → `np.float32` (numpy deprecation fix)
