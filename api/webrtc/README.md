# WebRTC Streaming System

A complete WebRTC-based video streaming solution with cloud signaling and Raspberry Pi edge processing.

## Architecture

This system enables low-latency, P2P-first video streaming from Raspberry Pi cameras to web browsers with:

- **Cloud Components** (VPS): Signaling server, TURN/STUN server, Cloudflare tunnel
- **Edge Components** (Raspberry Pi): Raw RTSP streaming and AI-processed video streaming
- **Web Viewer**: Browser-based viewer with stream selection

### Features

✅ **P2P-first streaming** with TURN fallback for NAT traversal  
✅ **Dual stream modes**: Raw RTSP and AI-processed  
✅ **AI dual output**: WebRTC (low-latency P2P) + HTTP MJPEG (LAN/debug)  
✅ **Secure access** via Cloudflare Access  
✅ **Hardware-accelerated H.264 encoding** on Pi  
✅ **Low cloud costs** - signaling only, video goes P2P  
✅ **Scalable** - supports multiple devices and viewers  

## Directory Structure

```
webrtc/
├── cloud/                      # Cloud/VPS components
│   ├── docker-compose.yml      # Orchestrates all cloud services
│   ├── signaling/              # WebSocket signaling server
│   │   ├── app.py
│   │   ├── requirements.txt
│   │   └── Dockerfile
│   ├── coturn/                 # TURN/STUN server config
│   │   └── turnserver.conf
│   └── cloudflared/            # Cloudflare tunnel config
│       └── config.yml
├── pi/                         # Raspberry Pi components
│   ├── device_agent.py         # Main device agent
│   ├── publisher_raw.py        # Raw RTSP → WebRTC publisher
│   ├── ai_stream_server.py     # AI frame producer (HTTP + shared buffer)
│   ├── publisher_ai.py         # AI frames → WebRTC publisher
│   └── requirements.txt
└── web/                        # Web viewer
    └── viewer.html
```

## Setup Guide

### 1. Cloud/VPS Setup

#### Prerequisites
- Ubuntu/Debian VPS with public IP
- Domain name pointed to VPS
- Docker and Docker Compose installed

#### Steps

1. **Configure firewall** - Open these ports:
   ```bash
   sudo ufw allow 3478/tcp
   sudo ufw allow 3478/udp
   sudo ufw allow 49160:49200/udp
   ```

2. **Update configuration files**:
   
   Edit `cloud/coturn/turnserver.conf`:
   ```conf
   realm=turn.example.com
   static-auth-secret=<GENERATE_LONG_RANDOM_SECRET>
   ```
   
   Edit `cloud/docker-compose.yml` environment variables:
   ```yaml
   - TURN_REALM=turn.example.com
   - TURN_SHARED_SECRET=<SAME_SECRET_AS_ABOVE>
   - TURN_HOST=turn.example.com
   ```

3. **Set up Cloudflare Tunnel**:
   
   ```bash
   # Install cloudflared
   wget https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
   sudo dpkg -i cloudflared-linux-amd64.deb
   
   # Login and create tunnel
   cloudflared tunnel login
   cloudflared tunnel create webrtc-signal
   
   # Copy credentials to cloud/cloudflared/
   # Update cloud/cloudflared/config.yml with tunnel UUID
   ```

4. **Configure DNS**:
   - `turn.example.com` → A record to VPS IP
   - `signal.example.com` → CNAME to tunnel (configured in Cloudflare Zero Trust)

5. **Enable Cloudflare Access**:
   - Go to Cloudflare Zero Trust → Access → Applications
   - Protect `signal.example.com`
   - Add yourself as allowed user (email OTP or Google)

6. **Start services**:
   ```bash
   cd cloud
   docker-compose up -d
   ```

7. **Verify**:
   ```bash
   docker-compose logs -f signaling
   docker-compose logs -f coturn
   ```

### 2. Raspberry Pi Setup

#### Prerequisites
- Raspberry Pi 4 (4GB+ recommended)
- Raspberry Pi OS (64-bit recommended)
- Network access to RTSP camera
- Network access to cloud signaling server

#### Steps

1. **Install dependencies**:
   ```bash
   sudo apt update
   sudo apt install -y \
     python3 python3-pip \
     gstreamer1.0-tools \
     gstreamer1.0-plugins-base \
     gstreamer1.0-plugins-good \
     gstreamer1.0-plugins-bad \
     gstreamer1.0-plugins-ugly \
     gstreamer1.0-libav \
     python3-gi \
     gir1.2-gst-plugins-base-1.0 \
     gir1.2-gstreamer-1.0 \
     ffmpeg
   ```

2. **Install Python packages**:
   ```bash
   cd pi
   pip3 install -r requirements.txt
   ```

3. **Configure environment**:
   
   Create `pi/.env`:
   ```bash
   SIGNAL_WSS=wss://signal.example.com/ws/device
   DEVICE_ID=dev1
   DEVICE_TOKEN=<GENERATE_RANDOM_TOKEN>
   RTSP_URL=rtsp://USER:PASS@192.168.1.100:554/cam/realmonitor?channel=1&subtype=0&unicast=true&proto=Onvif
   ```

4. **Test RTSP connection**:
   ```bash
   ffprobe -rtsp_transport tcp -select_streams v:0 "$RTSP_URL"
   ```
   
   **Optional**: Try substream (lighter):
   ```bash
   # Change subtype=0 to subtype=1
   ffprobe -rtsp_transport tcp -select_streams v:0 \
     "rtsp://USER:PASS@192.168.1.100:554/cam/realmonitor?channel=1&subtype=1&unicast=true&proto=Onvif"
   ```

5. **Test publishers** (one at a time):
   ```bash
   # Test raw publisher
   python3 publisher_raw.py
   
   # Test AI publisher
   python3 publisher_ai.py
   ```

6. **Run device agent** (production):
   ```bash
   python3 device_agent.py
   ```

#### Run as systemd service (recommended)

Create `/etc/systemd/system/webrtc-device.service`:
```ini
[Unit]
Description=WebRTC Device Agent
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/Motion-Analysis/webrtc/pi
EnvironmentFile=/home/pi/Motion-Analysis/webrtc/pi/.env
ExecStart=/usr/bin/python3 device_agent.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable webrtc-device
sudo systemctl start webrtc-device
sudo systemctl status webrtc-device
```

### 3. Web Viewer Setup

#### Simple HTTP Server (testing)

```bash
cd web
python3 -m http.server 8080
```

Then open `http://localhost:8080/viewer.html`

#### Production Deployment

Deploy `viewer.html` to:
- Cloudflare Pages
- Netlify
- Any static hosting service
- Or serve via nginx/Apache

**Update the WebSocket URL** in `viewer.html`:
```javascript
const SIGNAL_WSS = "wss://signal.example.com/ws/viewer";
```

## Usage

1. **Start cloud services** (if not running):
   ```bash
   cd cloud
   docker-compose up -d
   ```

2. **Start Pi device agent** (if not running as service):
   ```bash
   cd pi
   python3 device_agent.py
   ```

3. **Open web viewer**:
   - Navigate to `viewer.html` in browser
   - Select stream mode (Raw or AI)
   - Enter device ID (default: `dev1`)
   - Click "Connect"

## Configuration

### Camera Settings

For best performance on Raspberry Pi, use camera substream:

- **Main stream**: High resolution (2304×1296) - use for recording
- **Sub stream**: Lower resolution (640×360 or similar) - use for streaming

Update `RTSP_URL` to use `subtype=1` for substream.

### Video Encoding

#### Raspberry Pi 4 Hardware Encoding

Replace `x264enc` in publishers with hardware encoder:

**For older Pi OS (Bullseye and earlier)**:
```python
# In pipeline_str, replace:
'x264enc tune=zerolatency bitrate=2000 speed-preset=ultrafast'
# with:
'omxh264enc target-bitrate=2000000 control-rate=variable'
```

**For Pi OS Bookworm (newer)**:
```python
# Replace with:
'v4l2h264enc extra-controls="controls,video_bitrate=2000000"'
```

### Bitrate Adjustment

Adjust based on your network:
- **Low bandwidth**: `bitrate=500` (500 kbps)
- **Normal**: `bitrate=2000` (2 Mbps) - default
- **High quality**: `bitrate=4000` (4 Mbps)

## Troubleshooting

### Cloud Issues

**Signaling server not accessible**:
```bash
# Check if services are running
docker-compose ps

# Check logs
docker-compose logs signaling
docker-compose logs coturn

# Test locally
curl http://localhost:8000
```

**TURN server not working**:
```bash
# Test TURN connectivity
turnutils_uclient -v turn.example.com -u testuser -w testpass
```

### Pi Issues

**RTSP stream fails**:
```bash
# Test with ffplay
ffplay -rtsp_transport tcp "$RTSP_URL"

# Check for "Data" stream issue
ffprobe -rtsp_transport tcp "$RTSP_URL"
# If you see "Unsupported codec", use -map 0:v:0 in ffmpeg
```

**GStreamer pipeline fails**:
```bash
# Test simple pipeline first
gst-launch-1.0 videotestsrc ! autovideosink

# Test RTSP input
gst-launch-1.0 rtspsrc location="$RTSP_URL" ! rtph265depay ! h265parse ! avdec_h265 ! autovideosink
```

**Python GStreamer bindings not found**:
```bash
# Ensure python3-gi is installed
sudo apt install python3-gi gir1.2-gstreamer-1.0

# Test import
python3 -c "import gi; gi.require_version('Gst', '1.0'); from gi.repository import Gst"
```

### Viewer Issues

**WebSocket connection fails**:
- Check Cloudflare Access authentication
- Verify `SIGNAL_WSS` URL in viewer.html
- Check browser console for errors

**Video not playing**:
- Check WebRTC connection state in status panel
- Verify ICE candidates are being exchanged
- Check if TURN server is configured correctly
- Test with different browsers (Chrome/Firefox)

**High latency**:
- Reduce video resolution in publisher
- Lower bitrate
- Check network bandwidth
- Verify P2P connection (not going through TURN relay)

## Integration with Existing AI Pipeline

## AI Integration

The system supports AI-processed video streaming with dual output modes:

### Architecture
- **ai_stream_server.py**: Frame producer with ffmpeg RTSP reader + AI processing + HTTP MJPEG endpoint
- **publisher_ai.py**: WebRTC publisher reading from shared frame buffer

### Modes

**1. WebRTC Only (Standalone)**
```bash
python3 publisher_ai.py  # Starts frame producer automatically
```

**2. HTTP + WebRTC (Dual Process)**
```bash
# Terminal 1
python3 ai_stream_server.py  # HTTP at :9000/stream/ai

# Terminal 2
python3 publisher_ai.py  # WebRTC via signaling
```

### Quick Start

```bash
export RTSP_URL="rtsp://admin:pass@192.168.1.100:554/cam/realmonitor?channel=1&subtype=1"
export SIGNAL_WSS="wss://signal.example.com/ws/device"
export DEVICE_ID="dev1"
export DEVICE_TOKEN="your-token"
export AI_W=854
export AI_H=480
export AI_FPS=10

python3 publisher_ai.py
```

### Integrate Your AI Model

Edit `ai_stream_server.py` → `run_ai_and_draw()` function:

```python
def run_ai_and_draw(bgr: np.ndarray) -> np.ndarray:
    # YOUR AI HERE
    from your_ai_module import detector
    detections = detector.process(bgr)
    bgr = draw_detections(bgr, detections)
    return bgr
```

**📖 For detailed AI integration guide, see [AI_INTEGRATION.md](AI_INTEGRATION.md)**

This covers:
- Integrating your AI model
- Performance tuning
- systemd deployment
- Troubleshooting
- Production IPC with ZeroMQ

## Security Notes

1. **Device tokens**: Generate strong random tokens for each device
2. **Cloudflare Access**: Protect signaling endpoint with authentication
3. **TURN credentials**: Use ephemeral credentials (auto-generated via TURN REST API)
4. **RTSP credentials**: Never expose in public code - use environment variables
5. **HTTPS/WSS only**: All signaling must use secure WebSocket

## Performance Tips

1. **Use camera substream** for lower bandwidth and CPU usage
2. **Hardware encoding** on Pi for better performance
3. **Adjust resolution** based on use case (720p is good balance)
4. **Monitor Pi temperature**: Use heatsink/fan if needed
5. **Network**: Wired ethernet > WiFi for reliability

## Next Steps

- [ ] Add multiple device support
- [ ] Implement viewer authentication
- [ ] Add recording capability
- [ ] Create device management dashboard
- [ ] Add analytics and monitoring
- [ ] Implement adaptive bitrate
- [ ] Add audio support

## License

See main project license.

## Support

For issues and questions, check:
1. Cloud logs: `docker-compose logs`
2. Pi logs: `journalctl -u webrtc-device -f`
3. Browser console for viewer errors
