#!/usr/bin/env python3
"""
Raw WebRTC Publisher for RTSP stream
Connects to signaling server and publishes RTSP stream via WebRTC

This production-ready version includes:
- Hardware encoding support (v4l2h264enc) with x264enc fallback
- Thread-safe WebSocket messaging from GStreamer callbacks
- Proper STUN/TURN configuration
- Graceful shutdown handling
- Detailed error logging
"""
import os
import json
import asyncio
import threading
import signal
from typing import Any, Dict, Optional, List

import websockets

import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstSdp", "1.0")
gi.require_version("GstWebRTC", "1.0")
from gi.repository import Gst, GstSdp, GstWebRTC, GLib


Gst.init(None)


def pick_stun_turn(ice_servers: List[dict]) -> Dict[str, Optional[str]]:
    """
    Convert WebRTC-style iceServers into GStreamer webrtcbin properties:
      - stun-server: "stun://host:port"
      - turn-server: "turn://user:pass@host:port?transport=udp" (or tcp)

    Note: webrtcbin supports only one stun-server and one turn-server property.
    We'll pick the first STUN and the first TURN in the list.
    """
    stun = None
    turn = None

    for srv in ice_servers:
        urls = srv.get("urls") or []
        if isinstance(urls, str):
            urls = [urls]

        for u in urls:
            if u.startswith("stun:") and stun is None:
                # Convert "stun:host:port" -> "stun://host:port"
                stun = "stun://" + u[len("stun:"):]
            elif u.startswith("turn:") and turn is None:
                # Convert "turn:host:port?transport=udp" + creds -> "turn://user:pass@host:port?transport=udp"
                username = srv.get("username")
                credential = srv.get("credential")
                hostpart = u[len("turn:"):]  # host:port?transport=...
                if username and credential:
                    turn = f"turn://{username}:{credential}@{hostpart}"
                else:
                    # If no creds supplied, still pass through (may work if TURN is open, usually not)
                    turn = "turn://" + hostpart

    return {"stun": stun, "turn": turn}


class RawWebRTCPublisher:
    """
    GStreamer webrtcbin publisher that:
      - pulls RTSP HEVC/H265
      - decodes -> converts -> scales -> encodes H264
      - feeds RTP/H264 to webrtcbin
      - negotiates via external WebSocket signaling
    """

    def __init__(self, rtsp_url: str, prefer_hw_enc: bool = True):
        self.rtsp_url = rtsp_url
        self.prefer_hw_enc = prefer_hw_enc

        self.pipeline: Optional[Gst.Pipeline] = None
        self.webrtc: Optional[Gst.Element] = None
        self.bus: Optional[Gst.Bus] = None

        self.session_id: Optional[str] = None
        self.ws: Optional[websockets.WebSocketClientProtocol] = None

        self.mainloop = GLib.MainLoop()
        self.mainloop_thread = threading.Thread(target=self.mainloop.run, daemon=True)

        self._stopping = False

    def _build_pipeline(self, width=1280, height=720, fps_num=15, fps_den=1, bitrate=1200_000) -> None:
        """
        Build a pipeline:
          rtspsrc -> rtph265depay -> h265parse -> avdec_h265 -> videoconvert -> videoscale -> caps
          -> (v4l2h264enc or x264enc) -> h264parse -> rtph264pay -> webrtcbin
        """
        # Encoder selection:
        # - v4l2h264enc is typically available on Pi and efficient
        # - if missing, fallback to x264enc tune=zerolatency
        enc = ""
        if self.prefer_hw_enc:
            enc = (
                f"v4l2h264enc extra-controls=\"controls,video_bitrate={bitrate};\" "
                f"! h264parse "
            )
        else:
            enc = f"x264enc tune=zerolatency bitrate={bitrate // 1000} speed-preset=ultrafast ! h264parse "

        pipeline_str = f"""
            webrtcbin name=wb bundle-policy=max-bundle

            rtspsrc name=src location="{self.rtsp_url}" protocols=tcp latency=200 !
              rtph265depay !
              h265parse !
              avdec_h265 !
              videoconvert !
              videoscale !
              video/x-raw,width={width},height={height},framerate={fps_num}/{fps_den} !
              queue !
              {enc}
              rtph264pay config-interval=1 pt=96 !
              application/x-rtp,media=video,encoding-name=H264,payload=96 !
              wb.
        """

        # Try to build pipeline
        try:
            self.pipeline = Gst.parse_launch(pipeline_str)
        except Exception as e:
            # If v4l2h264enc isn't available, rebuild with x264enc
            if self.prefer_hw_enc and "v4l2h264enc" in str(e):
                print("[WARN] v4l2h264enc not available, falling back to x264enc")
                self.prefer_hw_enc = False
                self._build_pipeline(width, height, fps_num, fps_den, bitrate)
                return
            raise

        assert isinstance(self.pipeline, Gst.Pipeline)
        self.webrtc = self.pipeline.get_by_name("wb")
        if not self.webrtc:
            raise RuntimeError("webrtcbin not found in pipeline")

        # Detect if v4l2h264enc exists; if not, rebuild using x264enc
        if self.prefer_hw_enc:
            if not Gst.ElementFactory.find("v4l2h264enc"):
                print("[WARN] v4l2h264enc not available, falling back to x264enc")
                self.prefer_hw_enc = False
                self._build_pipeline(width, height, fps_num, fps_den, bitrate)
                return

        self.webrtc.connect("on-negotiation-needed", self._on_negotiation_needed)
        self.webrtc.connect("on-ice-candidate", self._on_ice_candidate)

        # Bus logging (useful)
        self.bus = self.pipeline.get_bus()
        self.bus.add_signal_watch()
        self.bus.connect("message", self._on_bus_message)

    def _on_bus_message(self, bus: Gst.Bus, message: Gst.Message) -> None:
        t = message.type
        if t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            print(f"[GST ERROR] {err} debug={debug}")
        elif t == Gst.MessageType.WARNING:
            err, debug = message.parse_warning()
            print(f"[GST WARN] {err} debug={debug}")
        elif t == Gst.MessageType.STATE_CHANGED and message.src == self.pipeline:
            old, new, pending = message.parse_state_changed()
            print(f"[GST] pipeline state {old.value_nick} -> {new.value_nick}")

    def start(self, session_id: str, ice_servers: List[dict]) -> None:
        # Start GLib loop once
        if not self.mainloop_thread.is_alive():
            self.mainloop_thread.start()

        self.session_id = session_id

        # Stop any previous pipeline
        self.stop()

        # Build pipeline
        self._build_pipeline()

        # Configure ICE (stun/turn)
        picked = pick_stun_turn(ice_servers)
        if picked["stun"]:
            print(f"[ICE] stun-server={picked['stun']}")
            self.webrtc.set_property("stun-server", picked["stun"])
        if picked["turn"]:
            print(f"[ICE] turn-server={picked['turn']}")
            self.webrtc.set_property("turn-server", picked["turn"])

        # Start pipeline
        self.pipeline.set_state(Gst.State.PLAYING)

    def stop(self) -> None:
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
            self.webrtc = None
            self.bus = None

    def attach_ws(self, ws: websockets.WebSocketClientProtocol) -> None:
        self.ws = ws

    def _send_ws_json_threadsafe(self, msg: Dict[str, Any]) -> None:
        """
        Called from GLib/GStreamer callbacks (non-async threads).
        We schedule actual async send using asyncio loop.
        """
        if not self.ws:
            return
        loop = asyncio.get_event_loop()
        asyncio.run_coroutine_threadsafe(self.ws.send(json.dumps(msg)), loop)

    def _on_negotiation_needed(self, element: Gst.Element) -> None:
        # Create SDP offer
        print("[WEBRTC] negotiation needed -> creating offer")
        promise = Gst.Promise.new_with_change_func(self._on_offer_created, None)
        element.emit("create-offer", None, promise)

    def _on_offer_created(self, promise: Gst.Promise, _user_data: Any) -> None:
        promise.wait()
        reply = promise.get_reply()
        offer = reply.get_value("offer")
        assert offer is not None

        # Set local description
        self.webrtc.emit("set-local-description", offer, Gst.Promise.new())

        # Send offer to viewer via signaling
        sdp_text = offer.sdp.as_text()
        msg = {"type": "offer", "session_id": self.session_id, "sdp": sdp_text}
        print("[WEBRTC] sending offer")
        self._send_ws_json_threadsafe(msg)

    def set_answer(self, sdp: str) -> None:
        if not self.webrtc:
            return
        res, sdpmsg = GstSdp.sdp_message_new()
        if res != GstSdp.SDPResult.OK:
            raise RuntimeError("Failed to allocate SDP message")

        parse_res = GstSdp.sdp_message_parse_buffer(bytes(sdp, "utf-8"), sdpmsg)
        if parse_res != GstSdp.SDPResult.OK:
            raise RuntimeError("Failed to parse SDP answer")

        answer = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.ANSWER, sdpmsg)
        print("[WEBRTC] setting remote description (answer)")
        self.webrtc.emit("set-remote-description", answer, Gst.Promise.new())

    def add_ice_candidate(self, candidate: Dict[str, Any]) -> None:
        if not self.webrtc:
            return
        # Candidate object from browser usually contains:
        #  {candidate: "...", sdpMid: "0", sdpMLineIndex: 0}
        cstr = candidate.get("candidate")
        mline = candidate.get("sdpMLineIndex")
        if cstr is None or mline is None:
            return
        self.webrtc.emit("add-ice-candidate", int(mline), cstr)

    def _on_ice_candidate(self, element: Gst.Element, mlineindex: int, candidate: str) -> None:
        # Send candidate to viewer
        # Viewer code expects msg.candidate to be an RTCIceCandidateInit-like dict.
        msg = {
            "type": "ice_candidate",
            "session_id": self.session_id,
            "candidate": {
                "candidate": candidate,
                "sdpMid": "0",
                "sdpMLineIndex": int(mlineindex),
            },
        }
        self._send_ws_json_threadsafe(msg)


async def run_device_publisher():
    # ---- Configure from env ----
    SIGNAL_WSS = os.getenv("SIGNAL_WSS", "wss://signal.example.com/ws/device")
    DEVICE_ID = os.getenv("DEVICE_ID", "dev1")
    DEVICE_TOKEN = os.getenv("DEVICE_TOKEN", "CHANGE_ME_DEVICE_TOKEN")
    CAM_IP = os.getenv("CAM_IP", "192.168.0.100")
    CAM_USER = os.getenv("CAM_USER", "admin")
    CAM_PASS = os.getenv("CAM_PASS", "password")

    RTSP_PATH = "/cam/realmonitor?channel=1&subtype=0&unicast=true&proto=Onvif"
    RTSP_URL = os.getenv("RTSP_URL", f"rtsp://{CAM_USER}:{CAM_PASS}@{CAM_IP}:554{RTSP_PATH}")

    print(f"[CONFIG] Device ID: {DEVICE_ID}")
    print(f"[CONFIG] Signaling: {SIGNAL_WSS}")
    print(f"[CONFIG] RTSP: rtsp://{CAM_USER}:***@{CAM_IP}:554{RTSP_PATH}")

    pub = RawWebRTCPublisher(RTSP_URL, prefer_hw_enc=True)

    url = f"{SIGNAL_WSS}?device_id={DEVICE_ID}&token={DEVICE_TOKEN}"

    # Graceful shutdown
    stop_event = asyncio.Event()

    def _sigterm(*_):
        print("\n[SIGNAL] Received shutdown signal, stopping...")
        stop_event.set()

    signal.signal(signal.SIGINT, _sigterm)
    signal.signal(signal.SIGTERM, _sigterm)

    while not stop_event.is_set():
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=8_000_000) as ws:
                print("[WS] connected to signaling as device")
                pub.attach_ws(ws)

                # heartbeat loop
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
                            session_id = msg["session_id"]
                            ice = msg.get("ice_servers", [])
                            stream = msg.get("stream", "raw")
                            if stream != "raw":
                                # This publisher only handles raw.
                                continue
                            print(f"[SESSION] start_session {session_id} stream={stream}")
                            pub.start(session_id=session_id, ice_servers=ice)

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
            print(f"[WS] connection error: {e}")
            await asyncio.sleep(2)

    pub.stop()
    print("[EXIT] Publisher stopped")


if __name__ == "__main__":
    asyncio.run(run_device_publisher())
