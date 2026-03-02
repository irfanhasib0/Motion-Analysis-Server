# WebRTC Quick Reference

## Quick Start

### Cloud (VPS)
```bash
cd cloud
./setup.sh
```

### Raspberry Pi
```bash
cd pi
./setup.sh
# Edit .env with your settings
python3 device_agent.py
```

### Web Viewer
```bash
cd web
python3 -m http.server 8080
# Open http://localhost:8080/viewer.html
```

## Common Commands

### Cloud Management
```bash
# Start services
docker-compose up -d

# Stop services
docker-compose down

# View logs
docker-compose logs -f signaling
docker-compose logs -f coturn

# Restart service
docker-compose restart signaling

# Rebuild after code changes
docker-compose up -d --build signaling
```

### Pi Management
```bash
# Run manually
python3 device_agent.py

# Install as service
sudo cp webrtc-device.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable webrtc-device
sudo systemctl start webrtc-device

# Service management
sudo systemctl status webrtc-device
sudo systemctl restart webrtc-device
sudo journalctl -u webrtc-device -f
```

### Testing

#### Test RTSP Stream
```bash
# Check stream info
ffprobe -rtsp_transport tcp -select_streams v:0 "$RTSP_URL"

# Play stream
ffplay -rtsp_transport tcp "$RTSP_URL"

# Test for data stream issue
ffprobe -rtsp_transport tcp "$RTSP_URL" 2>&1 | grep -i "unsupported\|data"
```

#### Test GStreamer
```bash
# Test videotestsrc
gst-launch-1.0 videotestsrc ! autovideosink

# Test RTSP pipeline
gst-launch-1.0 rtspsrc location="$RTSP_URL" ! rtph265depay ! h265parse ! avdec_h265 ! autovideosink

# Test encoding
gst-launch-1.0 videotestsrc ! x264enc ! fakesink
```

#### Test TURN Server
```bash
# Install tools
sudo apt install coturn-utils

# Test TURN
turnutils_uclient -v turn.example.com
```

## Configuration Variables

### Cloud Environment
```bash
REDIS_URL=redis://redis:6379/0
TURN_REALM=turn.example.com
TURN_SHARED_SECRET=<random-secret>
TURN_HOST=turn.example.com
TURN_PORT=3478
```

### Pi Environment
```bash
SIGNAL_WSS=wss://signal.example.com/ws/device
DEVICE_ID=dev1
DEVICE_TOKEN=<random-token>
RTSP_URL=rtsp://user:pass@ip:554/path
```

## Troubleshooting

### Connection Issues

**Signaling won't connect:**
```bash
# Test WebSocket
wscat -c wss://signal.example.com/ws/viewer
# Or in browser console:
new WebSocket("wss://signal.example.com/ws/viewer")
```

**RTSP won't connect:**
```bash
# Test network access
ping CAMERA_IP
telnet CAMERA_IP 554

# Test credentials
ffprobe -rtsp_transport tcp "rtsp://USER:PASS@IP:554/path"
```

**WebRTC won't connect:**
- Check ICE state in viewer status
- Verify TURN server is accessible
- Check browser console for errors
- Try from different network

### Performance Issues

**High CPU on Pi:**
- Use camera substream (subtype=1)
- Lower resolution in pipeline
- Use hardware encoder (v4l2h264enc or omxh264enc)
- Reduce bitrate

**High latency:**
- Reduce video buffer: `cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)`
- Use zerolatency tune: `x264enc tune=zerolatency`
- Check network bandwidth
- Verify P2P connection (not relay)

**Stuttering video:**
- Increase bitrate
- Check network stability
- Monitor Pi temperature
- Use wired ethernet

### Common Errors

**"No module named 'gi'"**
```bash
sudo apt install python3-gi gir1.2-gstreamer-1.0
```

**"Device offline" error:**
```bash
# Check device agent is running
systemctl status webrtc-device
# Check signaling connection
journalctl -u webrtc-device -f
```

**"Unsupported codec" in RTSP:**
```bash
# Your camera has extra "Data" stream
# Use -map 0:v:0 to select video only, or ignore the warning
```

## Network Ports

### Cloud (must be open on VPS)
- `3478/tcp` - TURN/STUN
- `3478/udp` - TURN/STUN  
- `49160-49200/udp` - TURN relay range

### Pi (outbound only)
- `554/tcp` - RTSP to camera
- `443/tcp` - WSS to signaling
- Random - WebRTC media (P2P)

## Security Checklist

- [ ] Use strong random TURN secret
- [ ] Use strong random device tokens
- [ ] Enable Cloudflare Access on signaling
- [ ] Use HTTPS/WSS only
- [ ] Keep RTSP credentials in .env (never commit)
- [ ] Update system packages regularly
- [ ] Monitor logs for unusual activity
- [ ] Use firewall rules on VPS
- [ ] Consider VPN for camera network

## Performance Tips

1. **Use camera substream** - Much lighter on bandwidth and CPU
2. **Hardware encoding** - Use GPU encoder on Pi if available
3. **Wired network** - More reliable than WiFi
4. **Cooling** - Add heatsink/fan to Pi under load
5. **Resolution** - 720p is good balance for remote viewing
6. **Bitrate** - Start at 2Mbps, adjust based on quality/bandwidth
7. **Buffer size** - Keep small (1-2) for low latency

## Monitoring

### Check Cloud Health
```bash
# Container status
docker-compose ps

# Resource usage
docker stats

# Logs
docker-compose logs --tail=100 -f
```

### Check Pi Health
```bash
# Service status
systemctl status webrtc-device

# CPU temperature
vcgencmd measure_temp

# CPU usage
htop

# Network
iftop
```

### Check Stream Quality
- Browser DevTools → Network tab
- WebRTC internals: `chrome://webrtc-internals`
- Check bitrate, packet loss, framerate

## Backup & Recovery

### Backup Cloud Config
```bash
tar -czf webrtc-cloud-backup.tar.gz cloud/
```

### Backup Pi Config
```bash
tar -czf webrtc-pi-backup.tar.gz pi/.env pi/*.py
```

### Quick Recovery
```bash
# Cloud
cd cloud && docker-compose up -d

# Pi
sudo systemctl restart webrtc-device
```
