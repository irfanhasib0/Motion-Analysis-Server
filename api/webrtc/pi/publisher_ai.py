#!/usr/bin/env python3
"""
AI WebRTC Publisher - sends AI-processed frames via WebRTC
Connects to signaling server and publishes AI-annotated frames via WebRTC

This publisher:
- Reads frames from ai_stream_server (shared module)
- Pushes frames to GStreamer appsrc → H.264 encode → webrtcbin
- Negotiates via signaling server
- Supports hardware encoding (v4l2h264enc) with x264enc fallback
"""
import os, json, asyncio, threading, time, signal
from typing import Any, Dict, Optional, List

import websockets

import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstSdp", "1.0")
gi.require_version("GstWebRTC", "1.0")
from gi.repository import Gst, GstSdp, GstWebRTC, GLib

# Import shared frame producer
# Note: If running ai_stream_server.py separately, use IPC (ZeroMQ/Redis) instead
try:
    import ai_stream_server
    HAS_LOCAL_SERVER = True
except ImportError:
    HAS_LOCAL_SERVER = False
    print("[WARN] ai_stream_server module not found. Frame sharing disabled.")
    print("       Either run ai_stream_server.py separately or ensure it's importable.")

Gst.init(None)


def pick_stun_turn(ice_servers: List[dict]) -> Dict[str, Optional[str]]:
    """
    Convert WebRTC-style iceServers into GStreamer webrtcbin properties
    """
    stun = None
    turn = None
    for srv in ice_servers:
        urls = srv.get("urls") or []
        if isinstance(urls, str):
            urls = [urls]
        for u in urls:
            if u.startswith("stun:") and stun is None:
                stun = "stun://" + u[len("stun:"):]
            elif u.startswith("turn:") and turn is None:
                username = srv.get("username")
                credential = srv.get("credential")
                hostpart = u[len("turn:"):]
                if username and credential:
                    turn = f"turn://{username}:{credential}@{hostpart}"
                else:
                    turn = "turn://" + hostpart
    return {"stun": stun, "turn": turn}


class AIWebRTCPublisher:
    """
    GStreamer webrtcbin publisher for AI-processed frames
    
    Pipeline: appsrc → videoconvert → encoder → rtph264pay → webrtcbin
    """

    def __init__(self, width: int, height: int, fps: int = 10, prefer_hw_enc: bool = True):
        self.width = width
        self.height = height
        self.fps = fps
        self.prefer_hw_enc = prefer_hw_enc

        self.pipeline: Optional[Gst.Pipeline] = None
        self.webrtc: Optional[Gst.Element] = None
        self.appsrc: Optional[Gst.Element] = None

        self.session_id: Optional[str] = None
        self.ws: Optional[websockets.WebSocketClientProtocol] = None

        self.mainloop = GLib.MainLoop()
        self.mainloop_thread = threading.Thread(target=self.mainloop.run, daemon=True)

        self.push_thread: Optional[threading.Thread] = None
        self._pushing = False

    def attach_ws(self, ws):
        """Attach WebSocket connection for signaling"""
        self.ws = ws

    def _send_ws_json_threadsafe(self, msg: Dict[str, Any]) -> None:
        """Send JSON message via WebSocket (thread-safe)"""
        if not self.ws:
            return
        loop = asyncio.get_event_loop()
        asyncio.run_coroutine_threadsafe(self.ws.send(json.dumps(msg)), loop)

    def _build_pipeline(self, bitrate=900_000):
        """Build GStreamer pipeline with hardware or software encoding"""
        # Check encoder availability
        if self.prefer_hw_enc and not Gst.ElementFactory.find("v4l2h264enc"):
            print("[WARN] v4l2h264enc not available, using x264enc")
            self.prefer_hw_enc = False

        # Encoder selection
        if self.prefer_hw_enc:
            enc = f'v4l2h264enc extra-controls="controls,video_bitrate={bitrate};" ! h264parse'
        else:
            # bitrate in kbps for x264enc
            enc = 'x264enc tune=zerolatency speed-preset=ultrafast bitrate=900 key-int-max=30 ! h264parse'

        pipeline_str = f"""
            webrtcbin name=wb bundle-policy=max-bundle

            appsrc name=src is-live=true do-timestamp=true format=time block=true 
              caps=video/x-raw,format=BGR,width={self.width},height={self.height},framerate={self.fps}/1 !
              queue !
              videoconvert !
              {enc} !
              rtph264pay config-interval=1 pt=96 !
              application/x-rtp,media=video,encoding-name=H264,payload=96 !
              wb.
        """
        
        print(f"[PIPELINE] Building appsrc pipeline: {self.width}x{self.height} @ {self.fps} FPS")
        self.pipeline = Gst.parse_launch(pipeline_str)
        self.webrtc = self.pipeline.get_by_name("wb")
        self.appsrc = self.pipeline.get_by_name("src")
        
        if not self.webrtc or not self.appsrc:
            raise RuntimeError("Failed to build pipeline (missing webrtcbin/appsrc)")

        self.webrtc.connect("on-negotiation-needed", self._on_negotiation_needed)
        self.webrtc.connect("on-ice-candidate", self._on_ice_candidate)

    def start(self, session_id: str, ice_servers: List[dict]):
        """Start pipeline and WebRTC session"""
        if not self.mainloop_thread.is_alive():
            self.mainloop_thread.start()

        self.stop()
        self.session_id = session_id

        self._build_pipeline()

        # Configure ICE servers
        picked = pick_stun_turn(ice_servers)
        if picked["stun"]:
            print(f"[ICE] stun-server={picked['stun']}")
            self.webrtc.set_property("stun-server", picked["stun"])
        if picked["turn"]:
            print(f"[ICE] turn-server={picked['turn']}")
            self.webrtc.set_property("turn-server", picked["turn"])

        self.pipeline.set_state(Gst.State.PLAYING)

        # Start pushing frames to appsrc
        self._pushing = True
        self.push_thread = threading.Thread(target=self._push_loop, daemon=True)
        self.push_thread.start()

    def stop(self):
        """Stop pipeline and frame pushing"""
        self._pushing = False
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
        self.pipeline = None
        self.webrtc = None
        self.appsrc = None

    def _on_negotiation_needed(self, element: Gst.Element):
        """Handle WebRTC negotiation"""
        print("[WEBRTC] negotiation needed -> creating offer")
        promise = Gst.Promise.new_with_change_func(self._on_offer_created, None)
        element.emit("create-offer", None, promise)

    def _on_offer_created(self, promise: Gst.Promise, _user_data: Any):
        """Send SDP offer to signaling server"""
        promise.wait()
        reply = promise.get_reply()
        offer = reply.get_value("offer")
        self.webrtc.emit("set-local-description", offer, Gst.Promise.new())
        
        sdp_text = offer.sdp.as_text()
        print("[WEBRTC] sending offer")
        self._send_ws_json_threadsafe({
            "type": "offer",
            "session_id": self.session_id,
            "sdp": sdp_text
        })

    def set_answer(self, sdp: str):
        """Set remote SDP answer"""
        if not self.webrtc:
            return
        
        res, sdpmsg = GstSdp.sdp_message_new()
        if res != GstSdp.SDPResult.OK:
            raise RuntimeError("Failed to allocate SDP message")
        
        pres = GstSdp.sdp_message_parse_buffer(bytes(sdp, "utf-8"), sdpmsg)
        if pres != GstSdp.SDPResult.OK:
            raise RuntimeError("Failed to parse SDP answer")
        
        answer = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.ANSWER, sdpmsg)
        print("[WEBRTC] setting remote description (answer)")
        self.webrtc.emit("set-remote-description", answer, Gst.Promise.new())

    def add_ice_candidate(self, cand: Dict[str, Any]):
        """Add ICE candidate"""
        if not self.webrtc:
            return
        cstr = cand.get("candidate")
        mline = cand.get("sdpMLineIndex")
        if cstr is None or mline is None:
            return
        self.webrtc.emit("add-ice-candidate", int(mline), cstr)

    def _on_ice_candidate(self, element: Gst.Element, mlineindex: int, candidate: str):
        """Send ICE candidate to signaling server"""
        self._send_ws_json_threadsafe({
            "type": "ice_candidate",
            "session_id": self.session_id,
            "candidate": {
                "candidate": candidate,
                "sdpMid": "0",
                "sdpMLineIndex": int(mlineindex)
            },
        })

    def _push_loop(self):
        """
        Push frames to appsrc at configured FPS
        Reads latest frame from ai_stream_server shared state
        """
        if not HAS_LOCAL_SERVER:
            print("[ERROR] No frame source available")
            return

        frame_interval = 1.0 / float(self.fps)
        pts = 0
        frame_count = 0

        print(f"[PUSH] Starting frame push loop @ {self.fps} FPS")

        while self._pushing:
            t0 = time.time()

            # Get latest frame from shared state (thread-safe)
            with ai_stream_server.lock:
                frame = None if ai_stream_server.latest_bgr is None else ai_stream_server.latest_bgr.copy()

            if frame is not None and self.appsrc is not None:
                # Ensure frame has correct dimensions
                if frame.shape[1] != self.width or frame.shape[0] != self.height:
                    import cv2
                    frame = cv2.resize(frame, (self.width, self.height))

                # Convert to GStreamer buffer
                data = frame.tobytes()
                buf = Gst.Buffer.new_allocate(None, len(data), None)
                buf.fill(0, data)

                # Set timestamps
                buf.duration = int(1e9 * frame_interval)
                buf.pts = pts
                buf.dts = pts
                pts += buf.duration

                # Push to appsrc
                ret = self.appsrc.emit("push-buffer", buf)
                if ret != Gst.FlowReturn.OK:
                    # Pipeline may be stopping
                    pass
                else:
                    frame_count += 1
                    if frame_count % 100 == 0:
                        print(f"[PUSH] Pushed {frame_count} frames to WebRTC")

            # Maintain fps rate
            dt = time.time() - t0
            if dt < frame_interval:
                time.sleep(frame_interval - dt)


async def main():
    """Main entry point for AI WebRTC publisher"""
    # Configuration from environment
    SIGNAL_WSS = os.getenv("SIGNAL_WSS", "wss://signal.example.com/ws/device")
    DEVICE_ID = os.getenv("DEVICE_ID", "dev1")
    DEVICE_TOKEN = os.getenv("DEVICE_TOKEN", "CHANGE_ME_DEVICE_TOKEN")

    # Must match ai_stream_server output resolution
    W = int(os.getenv("AI_W", "854"))
    H = int(os.getenv("AI_H", "480"))
    FPS = int(float(os.getenv("AI_FPS", "10")))

    print(f"[CONFIG] Device ID: {DEVICE_ID}")
    print(f"[CONFIG] Signaling: {SIGNAL_WSS}")
    print(f"[CONFIG] Output: {W}x{H} @ {FPS} FPS")

    pub = AIWebRTCPublisher(width=W, height=H, fps=FPS, prefer_hw_enc=True)

    # Graceful shutdown
    stop_event = asyncio.Event()

    def _stop(*_):
        print("\n[SIGNAL] Received shutdown signal, stopping...")
        stop_event.set()
    
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    url = f"{SIGNAL_WSS}?device_id={DEVICE_ID}&token={DEVICE_TOKEN}"

    while not stop_event.is_set():
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=8_000_000) as ws:
                print("[WS] connected to signaling as device (AI publisher)")
                pub.attach_ws(ws)

                # Heartbeat task
                async def heartbeat():
                    while True:
                        await ws.send(json.dumps({"type": "heartbeat"}))
                        await asyncio.sleep(10)

                hb_task = asyncio.create_task(heartbeat())

                try:
                    async for text in ws:
                        msg = json.loads(text)
                        t = msg.get("type")

                        if t == "start_session":
                            # Only handle AI stream requests
                            if msg.get("stream") != "ai":
                                continue
                            
                            sid = msg["session_id"]
                            ice = msg.get("ice_servers", [])
                            print(f"[SESSION] start AI session {sid}")
                            pub.start(sid, ice)

                        elif t == "answer":
                            if msg.get("session_id") == pub.session_id:
                                pub.set_answer(msg["sdp"])

                        elif t == "ice_candidate":
                            if msg.get("session_id") == pub.session_id:
                                cand = msg.get("candidate")
                                if isinstance(cand, dict):
                                    pub.add_ice_candidate(cand)

                        elif t == "hangup":
                            if msg.get("session_id") == pub.session_id:
                                print("[SESSION] hangup")
                                pub.stop()

                finally:
                    hb_task.cancel()
                    pub.stop()

        except Exception as e:
            if stop_event.is_set():
                break
            print(f"[WS] reconnecting after error: {e}")
            await asyncio.sleep(2)

    pub.stop()
    print("[EXIT] Publisher stopped")


if __name__ == "__main__":
    # Start frame producer if running standalone
    # If ai_stream_server.py is running separately, comment out the block below
    if HAS_LOCAL_SERVER and ai_stream_server.latest_bgr is None:
        print("[STARTUP] Starting frame producer in this process...")
        ai_stream_server.start_background()
        # Allow time for ffmpeg to start
        time.sleep(2)
    
    asyncio.run(main())
