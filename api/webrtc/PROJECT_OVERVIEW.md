# WebRTC Streaming Project - Overview

## What This Project Does

This is a complete WebRTC-based video streaming system that enables:

1. **Low-latency live streaming** from Raspberry Pi camera to web browsers
2. **Two streaming modes**:
   - **Raw RTSP**: Direct camera feed via WebRTC
   - **AI Processed**: Your AI-annotated video via WebRTC
3. **P2P-first architecture**: Video streams directly between Pi and viewer (low cloud cost)
4. **TURN fallback**: Works even behind strict NATs
5. **Secure access**: Protected by Cloudflare Access

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                         CLOUD (VPS)                              │
│  ┌──────────────┐  ┌──────────────┐  ┌─────────────────────┐   │
│  │   Signaling  │  │    Redis     │  │   TURN/STUN Server  │   │
│  │   (FastAPI)  │──│  (Sessions)  │  │     (coturn)        │   │
│  └──────┬───────┘  └──────────────┘  └─────────────────────┘   │
│         │                                                        │
│  ┌──────┴────────────────────────────────────────────────────┐  │
│  │              Cloudflare Tunnel + Access                   │  │
│  │         (wss://signal.example.com)                        │  │
│  └───────────────────────────────────────────────────────────┘  │
└─────────────┬───────────────────────────────┬──────────────────┘
              │                               │
              │ WSS                           │ WSS
              │                               │
    ┌─────────┴─────────┐         ┌──────────┴─────────┐
    │   Raspberry Pi    │         │    Web Viewer      │
    │   (Edge Device)   │         │    (Browser)       │
    ├───────────────────┤         ├────────────────────┤
    │ • Device Agent    │         │ • viewer.html      │
    │ • Raw Publisher   │◄────────┤ • WebRTC Client    │
    │ • AI Publisher    │  P2P    │ • Stream Selector  │
    │                   │ WebRTC  │                    │
    ├───────────────────┤  (via   ├────────────────────┤
    │ RTSP Camera Feed  │  TURN   │  Video Display     │
    │ ↓                 │  relay  │                    │
    │ AI Processing     │  if NAT)│                    │
    └───────────────────┘         └────────────────────┘
                │
                │ RTSP
                │
        ┌───────┴────────┐
        │  Dahua Camera  │
        │   (H.265)      │
        └────────────────┘
```

## File Structure

```
webrtc/
├── README.md              # Complete setup guide
├── QUICKREF.md            # Quick reference commands
│
├── cloud/                 # Cloud/VPS components
│   ├── docker-compose.yml # Orchestrates all services
│   ├── setup.sh          # Automated setup script
│   │
│   ├── signaling/        # WebSocket signaling server
│   │   ├── app.py        # FastAPI server with WebRTC signaling
│   │   ├── requirements.txt
│   │   └── Dockerfile
│   │
│   ├── coturn/           # TURN/STUN server
│   │   └── turnserver.conf
│   │
│   └── cloudflared/      # Cloudflare tunnel
│       └── config.yml
│
├── pi/                   # Raspberry Pi components
│   ├── device_agent.py   # Main agent (manages publishers)
│   ├── publisher_raw.py  # Raw RTSP → WebRTC publisher
│   ├── publisher_ai.py   # AI frames → WebRTC publisher
│   ├── requirements.txt
│   ├── setup.sh         # Automated setup script
│   ├── .env.example     # Configuration template
│   └── webrtc-device.service  # systemd service file
│
└── web/                  # Web viewer
    └── viewer.html       # Browser-based viewer
```

## Key Components

### 1. Cloud Signaling Server (FastAPI)
- Handles WebSocket connections from devices and viewers
- Exchanges SDP offers/answers and ICE candidates
- Manages sessions and online presence in Redis
- Generates ephemeral TURN credentials
- **Does NOT relay video** (only signaling)

### 2. TURN/STUN Server (coturn)
- Provides NAT traversal
- STUN: helps discover public IP/port
- TURN: relays media if P2P fails
- Uses REST API for ephemeral credentials

### 3. Cloudflare Tunnel + Access
- Exposes signaling server securely (HTTPS/WSS)
- Protects with authentication (email OTP, Google, etc.)
- No need to expose ports or manage SSL certificates

### 4. Device Agent (Pi)
- Maintains WebSocket connection to signaling
- Launches appropriate publisher based on stream request
- Handles lifecycle (start/stop/reconnect)

### 5. Raw WebRTC Publisher (Pi)
- GStreamer pipeline: RTSP → decode → scale → H.264 → WebRTC
- Creates SDP offer
- Exchanges ICE candidates
- Streams video P2P to viewer

### 6. AI WebRTC Publisher (Pi)
- OpenCV loop: RTSP → AI processing → annotated frames
- GStreamer appsrc: annotated frames → H.264 → WebRTC
- Placeholder for your AI integration
- Same signaling/WebRTC flow as raw publisher

### 7. Web Viewer
- HTML5 + WebRTC browser client
- Stream mode selector (raw/ai)
- Shows connection status and diagnostics
- Works on desktop and mobile browsers

## Workflow

### Viewer Requests Stream

1. Viewer opens `viewer.html`, selects stream mode (raw/ai)
2. Viewer connects to signaling via WSS
3. Viewer sends `viewer_join` message with device_id and stream type
4. Signaling creates session, sends ICE servers to viewer
5. Signaling tells device to start stream

### Device Publishes Stream

6. Device agent receives `start_session` message
7. Device agent launches appropriate publisher (raw or ai)
8. Publisher creates WebRTC peer connection
9. Publisher creates SDP offer, sends to signaling
10. Signaling forwards offer to viewer

### WebRTC Negotiation

11. Viewer receives offer, creates answer, sends to signaling
12. Signaling forwards answer to publisher
13. Both sides exchange ICE candidates via signaling
14. ICE negotiation finds best connection path (P2P or TURN)
15. Media flows directly between Pi and viewer (or via TURN if needed)

### Result

- **Best case**: Direct P2P connection (lowest latency, no cloud bandwidth)
- **NAT traversal**: STUN helps with port mapping
- **Fallback**: TURN relay if P2P impossible (still low cloud cost)

## Integration Points

### For Your Existing AI Pipeline

Replace the placeholder in `pi/publisher_ai.py` → `ai_processing_loop()`:

```python
# Import your modules
from your_ai_module import initialize_detector, process_frame

detector = initialize_detector()

while self.running:
    ret, frame = cap.read()
    frame = cv2.resize(frame, (1280, 720))
    
    # YOUR AI HERE
    annotated = process_frame(detector, frame)
    
    # Push to WebRTC
    self.push_frame_to_appsrc(annotated)
```

### For Your FastAPI Streaming Endpoint

You can keep your existing HTTP-based AI stream as a fallback:
- Use raw WebRTC for low-latency live view
- Use FastAPI `/stream/ai` for recording/playback/debugging
- Both can coexist behind Cloudflare Access

## Advantages

✅ **Low cloud cost**: Only signaling + TURN relay (usually P2P)  
✅ **Low latency**: Direct connection, no routing through cloud  
✅ **Scalable**: Multiple devices and viewers, minimal server load  
✅ **Secure**: Cloudflare Access + WSS + ephemeral TURN credentials  
✅ **NAT-friendly**: Works behind most firewalls/routers  
✅ **Flexible**: Easy to add recording, multiple streams, etc.  

## Performance Characteristics

### Bandwidth Usage (Cloud)
- Signaling: < 1 KB/s per connection
- TURN relay (if needed): Same as video bitrate (1-4 Mbps per stream)
- Most connections will be P2P: **0 cloud video bandwidth**

### Bandwidth Usage (Pi)
- RTSP ingest: ~5-15 Mbps (HEVC, depends on resolution)
- WebRTC outbound: 1-4 Mbps (H.264, configurable bitrate)
- Multiple viewers: Bandwidth multiplies (consider limit)

### Latency
- P2P connection: 100-500ms (depends on network)
- TURN relay: 200-800ms (add relay hop)
- HTTP stream (traditional): 2-5 seconds

### CPU Usage (Pi 4)
- Raw stream with hardware encode: ~20-30%
- AI processing (depends on your model): 30-80%
- Software encode (x264enc): 40-70%
- Use substream + hardware encoder for best performance

## Next Steps After Setup

1. **Test basic connectivity**: Raw stream from Pi to viewer
2. **Integrate your AI**: Replace placeholder in publisher_ai.py
3. **Optimize for Pi**: Hardware encoding, substream, resolution
4. **Add features**:
   - Multiple devices (already supported)
   - Recording capability
   - Viewer authentication
   - Device management dashboard
   - Motion detection triggers
   - Mobile app
5. **Monitor and tune**: Check WebRTC stats, adjust bitrate/quality

## Cost Estimate

### Cloud (monthly)
- VPS (2GB RAM): $5-10
- Cloudflare Tunnel: Free
- Cloudflare Access: Free (up to 50 users)
- Bandwidth (signaling only): Negligible
- **Total: ~$5-10/month** for unlimited P2P streams

### Traditional Streaming (comparison)
- Cloud transcoding: $50-100/month per stream
- Bandwidth: $0.05-0.15/GB (tens of dollars per viewer)
- **WebRTC saves 90%+ on streaming costs**

## Security Model

1. **Device authentication**: Each device has unique token in Redis
2. **Viewer authentication**: Cloudflare Access (email/SSO)
3. **Signaling**: WSS only (TLS encrypted)
4. **TURN credentials**: Ephemeral (10min TTL, HMAC-signed)
5. **Media**: SRTP encrypted (WebRTC)
6. **Camera credentials**: In .env file, never exposed
7. **No public ports on Pi**: Outbound connections only

## Troubleshooting Quick Links

- Can't connect to signaling? → Check Cloudflare Access + tunnel status
- Can't read RTSP? → Check camera IP, credentials, ffprobe test
- Video won't play? → Check WebRTC connection state in viewer status
- High latency? → Verify P2P connection, check for TURN relay
- High CPU on Pi? → Use hardware encoding + camera substream

See README.md and QUICKREF.md for detailed troubleshooting.

## Support and Extensions

This is an MVP implementation. Possible extensions:

- **Mobile apps**: React Native with WebRTC
- **Recording**: Add MediaRecorder or FFmpeg recorder
- **Multi-camera**: Dashboard with grid view
- **Analytics**: Track viewer count, bandwidth, quality
- **Adaptive bitrate**: Adjust based on network conditions
- **Audio**: Add microphone or speaker support
- **AI triggers**: Motion detection → push notifications
- **Authentication**: JWT tokens for devices
- **Admin panel**: Device management, viewer permissions

## Technologies Used

- **Python 3**: Device logic, signaling server
- **FastAPI**: WebSocket signaling server
- **Redis**: Session and presence storage
- **GStreamer**: Media pipelines on Pi
- **OpenCV**: Frame processing for AI
- **Docker**: Cloud service orchestration
- **coturn**: TURN/STUN server
- **Cloudflare**: Tunnel and Access
- **WebRTC**: Browser and GStreamer webrtcbin
- **HTML5**: Web viewer

## License

See main project license.
