# AI Stream Configuration Guide

This guide explains how to integrate your AI processing pipeline with the WebRTC streaming system.

## Architecture

The AI streaming system has two components that can run together or separately:

### 1. **ai_stream_server.py** - Frame Producer
- Pulls RTSP stream via ffmpeg
- Processes frames with your AI model
- Provides two outputs:
  - **HTTP MJPEG** stream at `/stream/ai` (traditional streaming)
  - **Shared frame buffer** for WebRTC publisher

### 2. **publisher_ai.py** - WebRTC Publisher  
- Reads frames from ai_stream_server
- Pushes frames to GStreamer appsrc pipeline
- Streams via WebRTC (P2P-first, low latency)
- Negotiates via signaling server

## Quick Start

### Option A: Single Process (Standalone Mode)

Run only `publisher_ai.py` - it will start the frame producer automatically:

```bash
export SIGNAL_WSS="wss://signal.example.com/ws/device"
export DEVICE_ID="dev1"
export DEVICE_TOKEN="your-device-token"
export RTSP_URL="rtsp://admin:pass@192.168.1.100:554/cam/realmonitor?channel=1&subtype=1&unicast=true&proto=Onvif"
export AI_W=854
export AI_H=480
export AI_FPS=10

python3 publisher_ai.py
```

**Provides:** WebRTC AI stream only

### Option B: Dual Process (HTTP + WebRTC)

Run both processes for HTTP streaming AND WebRTC:

**Terminal 1 - HTTP Server:**
```bash
export RTSP_URL="rtsp://admin:pass@192.168.1.100:554/cam/realmonitor?channel=1&subtype=1&unicast=true&proto=Onvif"
export AI_W=854
export AI_H=480
export AI_FPS=10

python3 ai_stream_server.py
```

**Terminal 2 - WebRTC Publisher:**
```bash
export SIGNAL_WSS="wss://signal.example.com/ws/device"
export DEVICE_ID="dev1"
export DEVICE_TOKEN="your-device-token"
export AI_W=854
export AI_H=480
export AI_FPS=10

python3 publisher_ai.py
```

**Provides:** 
- HTTP MJPEG stream at `http://PI_IP:9000/stream/ai`
- WebRTC AI stream via signaling

## Integrating Your AI Model

### Step 1: Replace the Placeholder

Edit `ai_stream_server.py` and replace the `run_ai_and_draw()` function:

```python
def run_ai_and_draw(bgr: np.ndarray) -> np.ndarray:
    """
    YOUR AI PROCESSING HERE
    """
    # Import your modules
    from your_ai_module import YourDetector, draw_detections
    
    # Run detection
    detections = detector.process(bgr)
    
    # Draw annotations
    bgr = draw_detections(bgr, detections)
    
    return bgr
```

### Step 2: Initialize Your Model

Add initialization before the ffmpeg_reader loop:

```python
# Global model instance
detector = None

def ffmpeg_reader():
    global latest_jpeg, latest_bgr, detector
    
    # Initialize model once
    if detector is None:
        from your_ai_module import YourDetector
        detector = YourDetector()
        print("[AI] Model loaded successfully")
    
    # ... rest of function
```

### Example: RTMPose Integration

If you're using the RTMPose from your main project:

```python
import sys
sys.path.append('/home/pi/Motion-Analysis/src')

from pose_inference import PoseInferencer

# Global
pose_model = None

def run_ai_and_draw(bgr: np.ndarray) -> np.ndarray:
    global pose_model
    
    if pose_model is None:
        pose_model = PoseInferencer(
            det_model='path/to/det_model.onnx',
            pose_model='path/to/pose_model.onnx',
            device='cpu'
        )
    
    # Run pose detection
    results = pose_model.process(bgr)
    
    # Draw skeletons
    for person in results:
        for keypoint in person['keypoints']:
            x, y, conf = keypoint
            if conf > 0.5:
                cv2.circle(bgr, (int(x), int(y)), 3, (0, 255, 0), -1)
        
        # Draw skeleton connections
        # ... your drawing code
    
    return bgr
```

## Configuration Environment Variables

### Required
- **RTSP_URL**: Camera RTSP URL
- **SIGNAL_WSS**: Signaling server WebSocket URL (for WebRTC)
- **DEVICE_ID**: Unique deviceidentifier
- **DEVICE_TOKEN**: Device authentication token

### Optional (with defaults)
- **AI_W**: Output width (default: 854)
- **AI_H**: Output height (default: 480)
- **AI_FPS**: Processing FPS (default: 10)

### Performance Tuning

**Low-end Pi or complex AI:**
```bash
export AI_W=640
export AI_H=360
export AI_FPS=5
```

**High-end Pi or simple AI:**
```bash
export AI_W=1280
export AI_H=720
export AI_FPS=15
```

## Recommended Settings

### For Dahua Camera

**Use substream for better performance:**
```bash
export RTSP_URL="rtsp://admin:pass@192.168.1.100:554/cam/realmonitor?channel=1&subtype=1&unicast=true&proto=Onvif"
```

### For Pi 4 (4GB)

**Balanced quality/performance:**
```bash
export AI_W=854
export AI_H=480
export AI_FPS=10
```

### For Heavy AI Models

Lower FPS to avoid frame drops:
```bash
export AI_FPS=5  # or even lower
```

## Testing

### Test HTTP Stream
```bash
# In browser
http://PI_IP:9000/health
http://PI_IP:9000/stream/ai
```

### Test WebRTC Stream
1. Open web viewer
2. Select stream: "ai"
3. Enter device ID
4. Click "Connect"
5. Video should play with AI annotations

## Dual Streaming Modes

### WebRTC (Recommended for Remote)
- **Latency**: 100-500ms
- **Bandwidth**: P2P (no cloud bandwidth)
- **Use case**: Remote viewing, multiple viewers
- **Cost**: Minimal cloud infrastructure

### HTTP MJPEG (Good for LAN/Debug)
- **Latency**: 2-5 seconds
- **Bandwidth**: Full stream per viewer
- **Use case**: Local network, debugging, recording
- **Cost**: High if exposed publicly

## Production Deployment

### systemd Service for Dual Mode

Create `/etc/systemd/system/webrtc-ai-stream.service`:

```ini
[Unit]
Description=WebRTC AI Stream Server
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/Motion-Analysis/webrtc/pi
EnvironmentFile=/home/pi/Motion-Analysis/webrtc/pi/.env
ExecStart=/bin/bash -c 'python3 ai_stream_server.py & python3 publisher_ai.py'
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable:
```bash
sudo systemctl daemon-reload
sudo systemctl enable webrtc-ai-stream
sudo systemctl start webrtc-ai-stream
```

### Alternative: Supervise with systemd

**Service 1:** `/etc/systemd/system/ai-stream-server.service`
```ini
[Unit]
Description=AI Stream HTTP Server
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/Motion-Analysis/webrtc/pi
EnvironmentFile=/home/pi/Motion-Analysis/webrtc/pi/.env
ExecStart=/usr/bin/python3 ai_stream_server.py
Restart=always

[Install]
WantedBy=multi-user.target
```

**Service 2:** `/etc/systemd/system/ai-webrtc-publisher.service`
```ini
[Unit]
Description=AI WebRTC Publisher
After=network.target ai-stream-server.service
Requires=ai-stream-server.service

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/Motion-Analysis/webrtc/pi
EnvironmentFile=/home/pi/Motion-Analysis/webrtc/pi/.env
ExecStart=/usr/bin/python3 publisher_ai.py
Restart=always

[Install]
WantedBy=multi-user.target
```

## Monitoring

### Check Logs
```bash
# Standalone mode
sudo journalctl -u webrtc-ai-stream -f

# Separate services
sudo journalctl -u ai-stream-server -f
sudo journalctl -u ai-webrtc-publisher -f
```

### Monitor Performance
```bash
# CPU usage
htop

# Temperature
vcgencmd measure_temp

# Frame producer status
curl http://localhost:9000/health
```

## Troubleshooting

### No frames produced
```bash
# Check RTSP connection
ffprobe -rtsp_transport tcp "$RTSP_URL"

# Check ffmpeg logs in ai_stream_server output
# Should see "Reader started, processing frames..."
```

### High CPU usage
- Lower AI_FPS
- Reduce AI_W and AI_H
- Use camera substream (subtype=1)
- Optimize your AI model (quantization, pruning)

### Frame lag/stuttering
- Increase AI_FPS slightly
- Check network bandwidth
- Verify Pi isn't thermal throttling
- Use hardware encoding (v4l2h264enc)

### WebRTC won't connect
- Verify publisher is running and connected to signaling
- Check viewer selects "ai" stream mode
- Verify device_id matches
- Check WebRTC connection state in viewer status

## Advanced: IPC with ZeroMQ

For production deployments with separate processes, use ZeroMQ for efficient frame sharing:

**Coming soon** - Add ZeroMQ IPC support for lower latency and better process isolation.

## Next Steps

1. ✅ Get basic AI stream working with placeholder
2. ✅ Integrate your actual AI model
3. ✅ Optimize performance (resolution, FPS, model)
4. ✅ Deploy as systemd service
5. ⏭️ Add recording capability
6. ⏭️ Implement ZeroMQ IPC for production
7. ⏭️ Add multiple camera support
8. ⏭️ Create management dashboard

## Summary

You now have:
- ✅ HTTP MJPEG streaming (traditional)
- ✅ WebRTC P2P streaming (low-latency, scalable)
- ✅ Easy AI integration point
- ✅ Production deployment options
- ✅ Performance tuning flexibility

Both modes can coexist, giving you flexibility for different use cases!
