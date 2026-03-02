# WebRTC Deployment Checklist

Use this checklist to ensure all components are properly configured before deployment.

## Prerequisites

- [ ] VPS with public IP (2GB+ RAM)
- [ ] Domain name (e.g., `example.com`)
- [ ] Cloudflare account (free tier works)
- [ ] Raspberry Pi 4 (4GB+ recommended)
- [ ] RTSP camera accessible from Pi

## Dummy Domains Used in This Guide

**Replace these placeholders with your actual values:**

- `signal.example.com` → Your Cloudflare tunnel hostname for signaling
- `turn.example.com` → Your VPS DNS name for TURN server
- `example.com` → Your actual domain

## 1. Cloud/VPS Configuration

### DNS Setup
- [ ] `turn.example.com` → A record pointing to VPS public IP
- [ ] Cloudflare DNS configured for tunnel

### Secrets Generation
```bash
# Generate TURN secret (64 characters)
TURN_SECRET=$(openssl rand -hex 32)
echo "TURN Secret: $TURN_SECRET"

# Generate device token (64 characters)
DEVICE_TOKEN=$(openssl rand -hex 32)
echo "Device Token: $DEVICE_TOKEN"
```

### Files to Edit

**`cloud/docker-compose.yml`:**
```yaml
- TURN_REALM=turn.example.com        # ← Replace
- TURN_SHARED_SECRET=<TURN_SECRET>   # ← Replace
- TURN_HOST=turn.example.com         # ← Replace
```

**`cloud/coturn/turnserver.conf`:**
```conf
realm=turn.example.com                # ← Replace
static-auth-secret=<TURN_SECRET>      # ← Replace (same as above)
```

**`cloud/cloudflared/config.yml`:**
```yaml
tunnel: <TUNNEL-UUID>                 # ← Replace with your tunnel UUID
credentials-file: /etc/cloudflared/<TUNNEL-UUID>.json  # ← Replace
ingress:
  - hostname: signal.example.com      # ← Replace
    service: http://signaling:8000
```

### Firewall Rules
```bash
sudo ufw allow 3478/tcp
sudo ufw allow 3478/udp
sudo ufw allow 49160:49200/udp
sudo ufw enable
```

### Start Services
```bash
cd cloud
docker-compose up -d
docker-compose ps  # All should be "Up"
```

### Cloudflare Access
- [ ] Go to Cloudflare Zero Trust → Access → Applications
- [ ] Add application for `signal.example.com`
- [ ] Configure authentication (email OTP, Google, etc.)
- [ ] Add yourself to allowed users

## 2. Raspberry Pi Configuration

### Files to Edit

**`pi/.env`:**
```bash
SIGNAL_WSS=wss://signal.example.com/ws/device  # ← Replace
DEVICE_ID=dev1                                 # ← Customize
DEVICE_TOKEN=<DEVICE_TOKEN>                    # ← Replace (generated above)

# Option 1: Individual camera parameters
CAM_IP=192.168.1.100                           # ← Replace
CAM_USER=admin                                 # ← Replace
CAM_PASS=your-camera-password                  # ← Replace

# Option 2: Complete RTSP URL (overrides above)
# RTSP_URL=rtsp://admin:pass@192.168.1.100:554/cam/realmonitor?channel=1&subtype=1&unicast=true&proto=Onvif
```

### Test Camera Connection
```bash
export $(cat .env | xargs)
ffprobe -rtsp_transport tcp -select_streams v:0 "$RTSP_URL"
# Should show video stream info without errors
```

### Install and Start
```bash
cd pi
./setup.sh  # Installs dependencies

# Test manually first
python3 publisher_raw.py

# Then install as service
sudo cp webrtc-device.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable webrtc-device
sudo systemctl start webrtc-device
sudo systemctl status webrtc-device  # Should be "active (running)"
```

## 3. Web Viewer Configuration

**`web/viewer.html`:**
```javascript
const SIGNAL_WSS = "wss://signal.example.com/ws/viewer";  // ← Replace
```

### Test Locally
```bash
cd web
python3 -m http.server 8080
# Open http://localhost:8080/viewer.html
```

## 4. Verification Tests

### Cloud Tests
```bash
# Test signaling locally
curl http://localhost:8000

# Test via tunnel (should require Cloudflare Access login)
curl https://signal.example.com

# Check services
docker-compose ps  # All "Up"
docker-compose logs -f coturn  # Should show TURN server running
```

### Pi Tests
```bash
# Check service
sudo systemctl status webrtc-device  # "active (running)"

# View logs
sudo journalctl -u webrtc-device -f  # Should show connection to signaling

# Test camera
ffprobe -rtsp_transport tcp "$RTSP_URL"  # Should show video stream
```

### Viewer Tests
1. [ ] Open viewer.html in browser
2. [ ] Cloudflare Access login works
3. [ ] WebSocket connects (green status)
4. [ ] Select "raw" stream and device "dev1"
5. [ ] Click "Connect"
6. [ ] Video plays within 5-10 seconds
7. [ ] Connection state shows "connected"
8. [ ] ICE state shows "connected" or "completed"

## 5. Troubleshooting Checklist

### WebSocket won't connect
- [ ] Cloudflare Access configured correctly
- [ ] Tunnel is running: `docker-compose logs cloudflared`
- [ ] Browser allows cookies (try incognito)
- [ ] Check browser console for errors

### WebRTC stuck on "Connecting"
- [ ] TURN server running: `docker-compose logs coturn`
- [ ] Firewall ports open (3478, 49160-49200)
- [ ] Pi publisher running: `sudo journalctl -u webrtc-device -f`
- [ ] Check chrome://webrtc-internals for ICE failures

### No video
- [ ] Browser supports H.264 (chrome://gpu)
- [ ] Camera stream working (ffprobe test)
- [ ] Check Pi logs for GStreamer errors
- [ ] Try different browser (Chrome usually best)

## 6. Performance Optimization

After basic setup works:

- [ ] Switch to camera substream (`subtype=1`) for better Pi performance
- [ ] Enable hardware encoding (`v4l2h264enc`) if available
- [ ] Adjust bitrate based on your network (500-4000 kbps)
- [ ] Lower resolution if needed (720p → 480p)
- [ ] Monitor Pi CPU temperature and add cooling if needed

## 7. Production Readiness

### Security
- [ ] Strong TURN secret (64+ chars)
- [ ] Strong device tokens (64+ chars)
- [ ] Cloudflare Access enabled
- [ ] Regular system updates
- [ ] Log rotation configured
- [ ] Firewall rules minimal (only required ports)

### Monitoring
- [ ] System resource monitoring (CPU, RAM, bandwidth)
- [ ] Log aggregation
- [ ] Uptime monitoring
- [ ] Alert on service failures

### Backup
- [ ] Configuration files backed up
- [ ] Device tokens documented securely
- [ ] Recovery procedure tested

## Summary

Once all checkboxes are complete, you have:

- ✅ Secure cloud signaling via Cloudflare Access
- ✅ TURN server for NAT traversal
- ✅ Pi publishing RTSP stream via WebRTC
- ✅ Browser viewer with connection status
- ✅ P2P-first architecture (low cloud cost)
- ✅ Hardware encoding on Pi (optimal performance)

**Test from multiple networks** (home, mobile, coffee shop) to ensure TURN fallback works correctly.

## Quick Commands Reference

```bash
# Cloud
docker-compose up -d              # Start all services
docker-compose logs -f signaling  # View signaling logs
docker-compose restart coturn     # Restart TURN server
docker-compose down               # Stop all services

# Pi  
sudo systemctl status webrtc-device      # Check status
sudo systemctl restart webrtc-device     # Restart service
sudo journalctl -u webrtc-device -f      # View logs
python3 publisher_raw.py                 # Test manually

# Testing
turnutils_uclient -v turn.example.com    # Test TURN
wscat -c wss://signal.example.com/ws/viewer  # Test WebSocket
ffprobe -rtsp_transport tcp "$RTSP_URL"  # Test camera stream
```

## Next Steps

- [ ] Integrate your AI processing into `publisher_ai.py`
- [ ] Deploy viewer to production hosting
- [ ] Add multiple devices/cameras
- [ ] Set up recording capability
- [ ] Create management dashboard
- [ ] Add mobile app support

---

For detailed instructions, see [GETTING_STARTED.md](GETTING_STARTED.md).
