"""Person gallery and re-ID search endpoints.

Routes
------
GET  /api/persons                           List person crops for a date
GET  /api/persons/status                    ReID model status
GET  /api/persons/{recording_id}/{pid}/image  Serve crop JPEG
POST /api/persons/search                    Similarity search by gallery selection
POST /api/persons/search/upload             Similarity search by uploaded probe photo
"""

import asyncio
import json
import os
import tempfile
import threading
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, UploadFile, File, Body
from fastapi.responses import FileResponse, StreamingResponse
from datetime import datetime

from routes import deps

router = APIRouter(prefix="/api", tags=["persons"])


def _reid():
    """Return the ReIDService instance, raising 503 if unavailable."""
    svc = getattr(deps, 'reid_service', None)
    if svc is None:
        raise HTTPException(status_code=503, detail="ReID service not initialised")
    return svc


# ─── Gallery browse ────────────────────────────────────────────────────────────

@router.get("/persons")
async def list_persons(
    date: Optional[str] = Query(default=None, description="ISO date YYYY-MM-DD. Defaults to today."),
    date_from: Optional[str] = Query(default=None, description="Range start YYYY-MM-DD (overrides date)."),
    date_to: Optional[str] = Query(default=None, description="Range end YYYY-MM-DD (overrides date)."),
    camera_id: Optional[str] = Query(default=None),
):
    """Return all person crop metadata detected on a date or date range."""
    svc = _reid()
    if date_from and date_to:
        persons = await asyncio.to_thread(svc.get_persons_by_date_range, date_from, date_to, camera_id)
        return {'persons': persons, 'date_from': date_from, 'date_to': date_to, 'total': len(persons)}
    effective_date = date or datetime.now().strftime('%Y-%m-%d')
    persons = await asyncio.to_thread(svc.get_persons_by_date, effective_date, camera_id)
    return {'persons': persons, 'date': effective_date, 'total': len(persons)}


@router.get("/persons/status")
async def reid_status():
    """Check whether the OSNet re-ID model is loaded and ready."""
    return _reid().get_status()


@router.get("/persons/{recording_id}/{pid}/image")
async def get_person_image(recording_id: str, pid: int):
    """Serve the person crop JPEG for a given recording / PID pair."""
    img_path = await asyncio.to_thread(_reid().get_person_image_path, recording_id, pid)
    if not img_path or not os.path.isfile(img_path):
        raise HTTPException(status_code=404, detail="Person image not found")
    return FileResponse(img_path, media_type="image/jpeg")


# ─── Similarity search ────────────────────────────────────────────────────────

@router.post("/persons/search")
async def search_persons(body: dict = Body(...)):
    """
    Find clips that contain a person visually similar to the selected gallery
    entry.

    Request body (JSON):
        ref_recording_id  str   — recording the probe person belongs to
        ref_pid           int   — person PID within that recording
        date_from         str   — ISO date range start  "YYYY-MM-DD"
        date_to           str   — ISO date range end    "YYYY-MM-DD"
        camera_id         str?  — restrict search to one camera (optional)
        top_k             int   — max results (default 20)
        threshold         float — minimum cosine similarity (default 0.55)
    """
    ref_recording_id = body.get('ref_recording_id')
    ref_pid          = body.get('ref_pid')
    date_from        = body.get('date_from')
    date_to          = body.get('date_to')
    camera_id        = body.get('camera_id') or None
    top_k            = int(body.get('top_k', 20))
    threshold        = float(body.get('threshold', 0.55))

    if not ref_recording_id or ref_pid is None:
        raise HTTPException(status_code=400, detail="ref_recording_id and ref_pid are required")
    if not date_from or not date_to:
        raise HTTPException(status_code=400, detail="date_from and date_to are required")

    svc = _reid()
    ref_path = await asyncio.to_thread(svc.get_person_image_path, ref_recording_id, int(ref_pid))
    if not ref_path or not os.path.isfile(ref_path):
        raise HTTPException(status_code=404, detail="Reference person image not found")

    return await asyncio.to_thread(
        svc.search, ref_path, date_from, date_to, camera_id, top_k, threshold
    )


@router.post("/persons/search/upload")
async def search_by_upload(
    file: UploadFile = File(...),
    date_from: str  = Query(..., description="ISO date YYYY-MM-DD"),
    date_to:   str  = Query(..., description="ISO date YYYY-MM-DD"),
    camera_id: Optional[str]  = Query(default=None),
    top_k:     int  = Query(default=20),
    threshold: float = Query(default=0.55),
):
    """
    Search by uploading an arbitrary probe photo (JPEG/PNG).
    Useful for cross-referencing an external image against the on-device gallery.
    """
    svc = _reid()

    suffix = os.path.splitext(file.filename or 'probe.jpg')[1] or '.jpg'
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        result = await asyncio.to_thread(
            svc.search, tmp_path, date_from, date_to, camera_id, top_k, threshold
        )
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

    return result


# ─── SSE helpers ──────────────────────────────────────────────────────────────

def _sync_gen_to_sse(sync_gen):
    """Wrap a synchronous generator so it runs in a background thread,
    delivering SSE-formatted text/event-stream lines via an asyncio.Queue."""
    import asyncio
    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def _run():
        try:
            for item in sync_gen:
                asyncio.run_coroutine_threadsafe(queue.put(item), loop).result(timeout=30)
        except Exception as exc:
            asyncio.run_coroutine_threadsafe(
                queue.put({'type': 'error', 'error': str(exc)}), loop
            ).result(timeout=5)
        finally:
            asyncio.run_coroutine_threadsafe(queue.put(None), loop).result(timeout=5)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return queue, t


async def _sse_response(sync_gen):
    """Async generator that yields SSE text lines from a synchronous generator."""
    queue, t = _sync_gen_to_sse(sync_gen)
    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            yield f"data: {json.dumps(item)}\n\n"
    finally:
        t.join(timeout=2)


# ─── Streaming similarity search ──────────────────────────────────────────────

@router.post("/persons/search/stream")
async def search_persons_stream(body: dict = Body(...)):
    """
    Same as /persons/search but streams SSE progress events while searching.
    Each batch completion sends a 'progress' event; the final event is 'done'.
    """
    ref_recording_id = body.get('ref_recording_id')
    ref_pid          = body.get('ref_pid')
    date_from        = body.get('date_from')
    date_to          = body.get('date_to')
    camera_id        = body.get('camera_id') or None
    top_k            = int(body.get('top_k', 20))
    threshold        = float(body.get('threshold', 0.55))

    if not ref_recording_id or ref_pid is None:
        raise HTTPException(status_code=400, detail="ref_recording_id and ref_pid are required")
    if not date_from or not date_to:
        raise HTTPException(status_code=400, detail="date_from and date_to are required")

    svc = _reid()
    ref_path = await asyncio.to_thread(svc.get_person_image_path, ref_recording_id, int(ref_pid))
    if not ref_path or not os.path.isfile(ref_path):
        raise HTTPException(status_code=404, detail="Reference person image not found")

    gen = svc.search_streaming(ref_path, date_from, date_to, camera_id, top_k, threshold)
    return StreamingResponse(_sse_response(gen), media_type="text/event-stream")


@router.post("/persons/search/upload/stream")
async def search_by_upload_stream(
    file: UploadFile = File(...),
    date_from: str   = Query(..., description="ISO date YYYY-MM-DD"),
    date_to:   str   = Query(..., description="ISO date YYYY-MM-DD"),
    camera_id: Optional[str]  = Query(default=None),
    top_k:     int   = Query(default=20),
    threshold: float = Query(default=0.55),
):
    """Upload-probe version of search/stream — streams SSE progress."""
    svc = _reid()

    suffix = os.path.splitext(file.filename or 'probe.jpg')[1] or '.jpg'
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    gen = svc.search_streaming(tmp_path, date_from, date_to, camera_id, top_k, threshold)

    async def cleanup_gen():
        try:
            async for chunk in _sse_response(gen):
                yield chunk
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    return StreamingResponse(cleanup_gen(), media_type="text/event-stream")
