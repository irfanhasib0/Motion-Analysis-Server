#!/usr/bin/env python3
"""
AI Stream Server - Frame Producer
Provides both:
1. FastAPI StreamingResponse endpoint (/stream/ai)
2. Shared frame buffer for WebRTC publisher

This server:
- Pulls RTSP via ffmpeg
- Runs AI processing (placeholder - replace with your model)
- Produces annotated frames
- Serves HTTP MJPEG stream
- Shares latest frame for WebRTC publisher
"""
import os, time, threading, subprocess
import numpy as np
import cv2
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

# Configuration
RTSP_URL = os.getenv("RTSP_URL")  # Required: RTSP camera URL
FPS_LIMIT = float(os.getenv("AI_FPS", "10"))  # AI processing FPS (lower = less CPU)
OUT_W = int(os.getenv("AI_W", "854"))  # Output width
OUT_H = int(os.getenv("AI_H", "480"))  # Output height

app = FastAPI()

# Shared state (thread-safe)
latest_jpeg = None  # For HTTP streaming (JPEG compressed)
latest_bgr = None   # For WebRTC publisher (raw BGR)
lock = threading.Lock()


def run_ai_and_draw(bgr: np.ndarray) -> np.ndarray:
    """
    AI Processing Hook - REPLACE THIS WITH YOUR ACTUAL AI PIPELINE
    
    Your integration should:
    1. Run detection/tracking on the frame
    2. Draw bounding boxes, labels, etc.
    3. Return annotated frame
    
    Example placeholders below.
    """
    h, w = bgr.shape[:2]
    
    # Placeholder: Add text overlay
    cv2.putText(bgr, f"AI Processing {w}x{h}", (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 0), 2)
    cv2.putText(bgr, f"FPS: {FPS_LIMIT}", (10, 70),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
    
    # Placeholder: Draw dummy detection box
    cv2.rectangle(bgr, (int(w*0.2), int(h*0.2)), 
                  (int(w*0.6), int(h*0.6)), (0, 255, 0), 2)
    cv2.putText(bgr, "Person: 0.95", (int(w*0.2), int(h*0.2)-10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
    
    # TODO: Replace above with your actual AI processing:
    # from your_ai_module import process_frame
    # detections = process_frame(bgr)
    # bgr = draw_detections(bgr, detections)
    
    return bgr


def ffmpeg_reader():
    """
    Background thread: Read RTSP stream via ffmpeg, process with AI, update shared state
    """
    global latest_jpeg, latest_bgr

    if not RTSP_URL:
        raise RuntimeError("RTSP_URL environment variable not set")

    print(f"[FFMPEG] Starting RTSP reader: {RTSP_URL}")
    print(f"[FFMPEG] Output: {OUT_W}x{OUT_H} @ {FPS_LIMIT} FPS")

    # ffmpeg command: RTSP (HEVC) → scale → fps limit → raw BGR24
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        "-rtsp_transport", "tcp",
        "-i", RTSP_URL,
        "-map", "0:v:0",  # Select video stream only (ignore data stream)
        "-vf", f"scale={OUT_W}:{OUT_H},fps={FPS_LIMIT}",
        "-pix_fmt", "bgr24",
        "-f", "rawvideo",
        "pipe:1",
    ]
    
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=10**7)
    frame_bytes = OUT_W * OUT_H * 3
    frame_count = 0

    print("[FFMPEG] Reader started, processing frames...")

    while True:
        raw = p.stdout.read(frame_bytes)
        if len(raw) != frame_bytes:
            print(f"[FFMPEG] Short read ({len(raw)} bytes), reconnecting...")
            time.sleep(0.2)
            continue

        # Decode raw bytes to numpy array
        bgr = np.frombuffer(raw, dtype=np.uint8).reshape((OUT_H, OUT_W, 3))
        
        # Run AI processing
        bgr = run_ai_and_draw(bgr)
        
        # Encode to JPEG for HTTP streaming
        ok, jpg = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        
        if ok:
            with lock:
                latest_jpeg = jpg.tobytes()
                latest_bgr = bgr.copy()
            
            frame_count += 1
            if frame_count % 100 == 0:
                print(f"[FFMPEG] Processed {frame_count} frames")


def mjpeg_generator():
    """
    Generator for HTTP MJPEG streaming
    """
    boundary = b"frame"
    while True:
        with lock:
            jpg = latest_jpeg
        
        if jpg is None:
            time.sleep(0.05)
            continue
        
        yield (b"--" + boundary + b"\r\n"
               b"Content-Type: image/jpeg\r\n"
               b"Content-Length: " + str(len(jpg)).encode() + b"\r\n\r\n" +
               jpg + b"\r\n")
        time.sleep(0.02)  # ~50 FPS max for HTTP stream


@app.get("/stream/ai")
def stream_ai():
    """
    HTTP MJPEG stream endpoint
    Usage: http://PI_IP:9000/stream/ai
    """
    return StreamingResponse(
        mjpeg_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame"
    )


@app.get("/health")
def health():
    """
    Health check endpoint
    """
    with lock:
        has_frames = latest_bgr is not None
    
    return {
        "ok": True,
        "width": OUT_W,
        "height": OUT_H,
        "fps": FPS_LIMIT,
        "has_frames": has_frames,
        "rtsp_url": RTSP_URL.split("@")[-1] if RTSP_URL else None  # Hide credentials
    }


@app.get("/")
def root():
    """
    Root endpoint with usage info
    """
    return {
        "service": "AI Stream Server",
        "endpoints": {
            "/stream/ai": "MJPEG stream",
            "/health": "Health check"
        }
    }


def start_background():
    """
    Start background ffmpeg reader thread
    """
    print("[SERVER] Starting background frame reader...")
    t = threading.Thread(target=ffmpeg_reader, daemon=True)
    t.start()


if __name__ == "__main__":
    start_background()
    
    import uvicorn
    print("[SERVER] Starting HTTP server on port 9000...")
    print("[SERVER] Endpoints:")
    print(f"[SERVER]   http://0.0.0.0:9000/health")
    print(f"[SERVER]   http://0.0.0.0:9000/stream/ai")
    
    uvicorn.run(app, host="0.0.0.0", port=9000)
