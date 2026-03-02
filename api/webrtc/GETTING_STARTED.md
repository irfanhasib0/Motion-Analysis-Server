# WebRTC Streaming System - Getting Started

This guide will walk you through setting up the complete WebRTC streaming system in the correct order.

## Quick Reference: Dummy Domains Used

Throughout this guide, we use these **placeholder domains** that you should replace with your actual values:

- **Signaling Server:** `signal.example.com` → Your Cloudflare tunnel domain
- **TURN Server:** `turn.example.com` → Your VPS public DNS name
- **VPS Public IP:** `VPS_PUBLIC_IP` → Your VPS IPv4 address

**Before deploying, replace `example.com` with your real domain in all configuration files.**

### Quick Copy-Paste Examples

**Viewer WebSocket:**
```javascript
const SIGNAL_WSS = "wss://signal.example.com/ws/viewer";
```

**Pi Environment:**
```bash
export SIGNAL_WSS="wss://signal.example.com/ws/device"
export DEVICE_ID="dev1"
export DEVICE_TOKEN="your-random-token-here"
export CAM_IP="192.168.1.100"
export CAM_USER="admin"
export CAM_PASS="your-camera-password"
```

**Cloud Environment:**
```bash
TURN_HOST=turn.example.com
TURN_PORT=3478
TURN_REALM=turn.example.com
TURN_SHARED_SECRET=your-random-secret-here
```

---

## Prerequisites Checklist

### Cloud/VPS Requirements
- [ ] Ubuntu/Debian VPS with public IP (2GB RAM minimum)
- [ ] Docker and Docker Compose installed
- [ ] Domain name (free from Cloudflare, Freenom, etc.)
- [ ] Cloudflare account (free tier works)

### Raspberry Pi Requirements
- [ ] Raspberry Pi 4 (4GB+ recommended)
- [ ] Raspberry Pi OS (64-bit Bookworm or Bullseye)
- [ ] Network access to your RTSP camera
- [ ] SSH access to Pi

### Camera Requirements
- [ ] RTSP-capable camera (your Dahua camera works!)
- [ ] Camera IP address and credentials
- [ ] Network connectivity to Pi

## Step-by-Step Setup

### Phase 1: Cloud Setup (30 minutes)

#### 1.1 Configure Your Domain

Point these DNS records to your VPS:
```
turn.example.com     → A record → VPS_PUBLIC_IP
```

**Note:** Replace `example.com` with your actual domain throughout this guide.

#### 1.2 Set Up Cloudflare Tunnel

On your VPS:
```bash
# Install cloudflared
wget https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i cloudflared-linux-amd64.deb

# Authenticate with Cloudflare
cloudflared tunnel login

# Create tunnel
cloudflared tunnel create webrtc-signal

# Note the tunnel UUID that's displayed
```

Copy tunnel credentials:
```bash
# Find your tunnel JSON file
ls ~/.cloudflared/*.json

# Copy it to project
sudo cp ~/.cloudflared/YOUR-TUNNEL-UUID.json /path/to/webrtc/cloud/cloudflared/
```

Edit `cloud/cloudflared/config.yml`:
```yaml
tunnel: YOUR-TUNNEL-UUID
credentials-file: /etc/cloudflared/YOUR-TUNNEL-UUID.json

ingress:
  - hostname: signal.example.com
    service: http://signaling:8000
  - service: http_status:404
```

Configure DNS in Cloudflare dashboard:
```
Cloudflare Zero Trust → Networks → Tunnels → webrtc-signal → Public Hostnames
  Add:
    Public hostname: signal.example.com
    Service: http://signaling:8000
```

#### 1.3 Enable Cloudflare Access

```
Cloudflare Zero Trust → Access → Applications → Add an application
  Application name: WebRTC Signaling
  Session duration: 24 hours
  Application domain: signal.example.com
  
Add policy:
  Policy name: Allow yourself
  Action: Allow
  Include: Emails → your@email.com
```

#### 1.4 Configure and Start Cloud Services

```bash
cd /path/to/Motion-Analysis/webrtc/cloud

# Run automated setup (generates TURN secret, starts services)
./setup.sh

# Or manual setup:

# 1. Generate TURN secret
TURN_SECRET=$(openssl rand -hex 32)
echo "Your TURN secret: $TURN_SECRET"

# 2. Edit docker-compose.yml
nano docker-compose.yml
# Replace CHANGE_ME_LONG_RANDOM with your secret
# Replace example.com with your actual domain

# 3. Edit coturn/turnserver.conf
nano coturn/turnserver.conf
# Replace CHANGE_ME_LONG_RANDOM with your secret
# Replace example.com with your actual domain

# 4. Start services
docker-compose up -d

# 5. Check status
docker-compose ps
docker-compose logs -f
```

#### 1.5 Configure VPS Firewall

```bash
# Allow TURN/STUN ports
sudo ufw allow 3478/tcp
sudo ufw allow 3478/udp
sudo ufw allow 49160:49200/udp
sudo ufw enable
```

#### 1.6 Test Cloud Setup

```bash
# Test signaling server locally
curl http://localhost:8000

# Test via Cloudflare tunnel
curl https://signal.example.com
# Should redirect to Cloudflare Access login

# Check coturn logs
docker-compose logs coturn
```

### Phase 2: Raspberry Pi Setup (20 minutes)

#### 2.1 Install Dependencies

SSH into your Pi:
```bash
ssh pi@PI_IP_ADDRESS
```

Navigate to project and run setup:
```bash
cd /home/pi/Motion-Analysis/webrtc/pi

# Automated setup
./setup.sh

# Or manual installation:
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

pip3 install -r requirements.txt
```

#### 2.2 Configure Pi Environment

```bash
cd /home/pi/Motion-Analysis/webrtc/pi

# Copy example config
cp .env.example .env

# Edit configuration
nano .env
```

Fill in your actual values:
```bash
SIGNAL_WSS=wss://signal.example.com/ws/device
DEVICE_ID=dev1
DEVICE_TOKEN=GENERATE_RANDOM_TOKEN_HERE

# Option 1: Set individual camera parameters
CAM_IP=192.168.1.100
CAM_USER=admin
CAM_PASS=YOUR_PASSWORD

# Option 2: Or use complete RTSP URL (overrides above)
# RTSP_URL=rtsp://admin:YOUR_PASSWORD@192.168.1.100:554/cam/realmonitor?channel=1&subtype=1&unicast=true&proto=Onvif
```

Generate device token:
```bash
# Generate random token
openssl rand -hex 32
# Copy this into .env as DEVICE_TOKEN
```

#### 2.3 Test RTSP Connection

```bash
# Load .env
export $(cat .env | xargs)

# Test camera connection
ffprobe -rtsp_transport tcp -select_streams v:0 "$RTSP_URL"

# Should show video stream info
# If you see "Unsupported codec with id 0", that's the Data stream - ignore it

# Check substream exists (lower quality = better performance)
ffprobe -rtsp_transport tcp -select_streams v:0 \
  "rtsp://admin:PASS@192.168.1.100:554/cam/realmonitor?channel=1&subtype=1&unicast=true&proto=Onvif"
```

#### 2.4 Test Publishers

Test raw publisher:
```bash
# Start raw publisher manually
python3 publisher_raw.py

# Watch for:
# - "Connected to signaling server as dev1"
# - Wait for viewer connection to trigger stream
# Ctrl+C to stop
```

Test AI publisher:
```bash
# Start AI publisher manually
python3 publisher_ai.py

# Watch for similar connection messages
# Ctrl+C to stop
```

#### 2.5 Install as System Service (Recommended)

```bash
# Copy service file
sudo cp webrtc-device.service /etc/systemd/system/

# Edit paths if needed
sudo nano /etc/systemd/system/webrtc-device.service

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable webrtc-device
sudo systemctl start webrtc-device

# Check status
sudo systemctl status webrtc-device

# View logs
sudo journalctl -u webrtc-device -f
```

### Phase 3: Web Viewer Setup (5 minutes)

#### 3.1 Update Viewer Configuration

```bash
cd /home/irfan/Desktop/Code/Motion-Analysis/webrtc/web

# Edit viewer.html
nano viewer.html
```

Update WebSocket URL:
```javascript
const SIGNAL_WSS = "wss://signal.example.com/ws/viewer";
```

#### 3.2 Test Locally

```bash
# Start simple HTTP server
python3 -m http.server 8080

# Open in browser
# http://localhost:8080/viewer.html
```

#### 3.3 Deploy to Production (Optional)

Deploy `viewer.html` to:
- **Cloudflare Pages**: Drag and drop `web/` folder
- **Netlify**: Connect git repo or drag/drop
- **GitHub Pages**: Push to gh-pages branch
- **Your VPS**: Serve with nginx/Apache

### Phase 4: First Stream Test (10 minutes)

#### 4.1 Verify All Services Running

**Cloud:**
```bash
ssh user@VPS_IP
cd webrtc/cloud
docker-compose ps
# All services should be "Up"
```

**Pi:**
```bash
ssh pi@PI_IP
sudo systemctl status webrtc-device
# Should be "active (running)"
```

#### 4.2 Connect from Viewer

1. Open viewer.html in browser
2. You'll be redirected to Cloudflare Access login
3. Authenticate with your email/Google
4. Back at viewer:
   - Device ID: `dev1` (or whatever you set in Pi .env)
   - Stream: Select "raw" for first test
   - Click "Connect"
5. Watch status panel:
   - WebSocket: Should turn green "Connected"
   - WebRTC: Should progress "Waiting for offer" → "Connecting" → "Connected"
   - Session: Shows session ID
   - ICE State: Should reach "connected" or "completed"
   - Connection State: Should reach "connected"
6. Video should start playing!

#### 4.3 Test AI Stream

1. In viewer, disconnect current stream
2. Select stream: "ai"  
3. Click "Connect"
4. Should see video with "AI Processing Active" overlay

#### 4.4 Troubleshooting First Stream

**WebSocket won't connect:**
```bash
# Check Cloudflare Access is configured
# Check browser allows cookies
# Try incognito mode
# Check browser console for errors
```

**WebRTC stuck on "Connecting":**
```bash
# Check TURN server:
docker-compose logs coturn

# Check Pi publisher is running:
sudo journalctl -u webrtc-device -f

# Check firewall on VPS:
sudo ufw status
# Should allow 3478/tcp, 3478/udp, 49160-49200/udp

# Try from different network (mobile hotspot)
```

**Video won't play:**
```bash
# Check browser supports H.264:
# Open chrome://gpu and look for H264 decode support

# Check GStreamer pipeline on Pi:
sudo journalctl -u webrtc-device -f
# Look for errors about encoding

# Try different browser (Chrome usually works best)
```

## Verification Checklist

After setup, verify each component:

- [ ] Cloud signaling accessible at https://signal.example.com
- [ ] Cloudflare Access login works
- [ ] TURN server running (docker-compose ps shows coturn Up)
- [ ] Pi device agent running (systemctl status webrtc-device)
- [ ] Pi can reach RTSP camera (ffprobe test passes)
- [ ] Viewer.html loads in browser
- [ ] WebSocket connects (green status in viewer)
- [ ] Raw stream works (video plays in viewer)
- [ ] AI stream works (annotated video plays)
- [ ] Connection state shows "connected"
- [ ] ICE state shows "connected" or "completed"

## Post-Setup Tasks

### Optimize for Your Use Case

1. **Performance tuning**:
   - Use camera substream (subtype=1)
   - Enable hardware encoding on Pi (see README.md)
   - Adjust bitrate (500-4000 kbps)
   - Lower resolution if needed

2. **Integrate your AI**:
   - Edit `pi/publisher_ai.py`
   - Replace placeholder in `ai_processing_loop()`
   - Import your detection/tracking modules
   - Process frames and push to WebRTC

3. **Add more devices**:
   - Create new .env with different DEVICE_ID on each Pi
   - Generate unique DEVICE_TOKEN for each
   - Deploy and start service on each Pi
   - Viewer can select device by ID

4. **Monitor and maintain**:
   - Set up log rotation
   - Monitor resource usage
   - Check video quality
   - Test from different networks

### Security Hardening

- [ ] Change default TURN secret
- [ ] Use strong device tokens (32+ random characters)
- [ ] Enable fail2ban on VPS
- [ ] Keep systems updated
- [ ] Review Cloudflare Access logs
- [ ] Rotate credentials periodically

## Quick Commands Reference

### Cloud
```bash
# Start services
cd cloud && docker-compose up -d

# View logs
docker-compose logs -f signaling

# Restart service
docker-compose restart signaling

# Stop all
docker-compose down
```

### Pi
```bash
# Restart device agent
sudo systemctl restart webrtc-device

# View logs
sudo journalctl -u webrtc-device -f

# Test RTSP
export $(cat .env | xargs) && ffprobe -rtsp_transport tcp "$RTSP_URL"
```

### Debugging
```bash
# Test WebSocket (on VPS)
wscat -c wss://signal.example.com/ws/viewer

# Test TURN (on VPS)
turnutils_uclient -v turn.example.com

# Browser WebRTC internals
# chrome://webrtc-internals
```

## What's Next?

Now that your basic system is running:

1. **Read the documentation**:
   - PROJECT_OVERVIEW.md - Architecture and concepts
   - README.md - Complete reference
   - QUICKREF.md - Common commands

2. **Customize**:
   - Integrate your AI models
   - Adjust video quality
   - Add recording
   - Create dashboard

3. **Scale**:
   - Add more cameras
   - Deploy multiple Pis
   - Create viewer authentication
   - Build mobile app

4. **Monitor**:
   - Check WebRTC stats in chrome://webrtc-internals
   - Monitor CPU/memory on Pi
   - Review connection quality
   - Track bandwidth usage

## Getting Help

If you encounter issues:

1. Check logs first:
   - Cloud: `docker-compose logs -f`
   - Pi: `sudo journalctl -u webrtc-device -f`
   - Browser: F12 → Console

2. Verify connectivity:
   - Ping VPS from Pi
   - Curl signaling endpoint
   - Test RTSP with ffprobe
   - Check firewall rules

3. Review documentation:
   - README.md has detailed troubleshooting
   - QUICKREF.md has common fixes
   - Check browser compatibility

4. Test components individually:
   - RTSP → ffplay
   - GStreamer → test pipeline
   - WebSocket → wscat
   - TURN → turnutils_uclient

## Success!

You now have a complete, production-ready WebRTC streaming system with:
- ✅ Low-latency P2P video streaming
- ✅ Secure cloud signaling
- ✅ NAT traversal with TURN fallback
- ✅ Dual stream modes (raw + AI)
- ✅ Browser-based viewer
- ✅ Scalable architecture
- ✅ Low cloud costs

Enjoy your new streaming system!
