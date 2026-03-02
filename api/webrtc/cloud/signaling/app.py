import os, time, hmac, base64, hashlib, json, asyncio
from typing import Dict, Any, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import redis.asyncio as redis

app = FastAPI()
r = redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))

TURN_REALM = os.getenv("TURN_REALM", "turn.example.com")
TURN_SHARED_SECRET = os.getenv("TURN_SHARED_SECRET", "CHANGE_ME_LONG_RANDOM")
TURN_HOST = os.getenv("TURN_HOST", "turn.example.com")
TURN_PORT = int(os.getenv("TURN_PORT", "3478"))

# In-memory WS maps (good enough for MVP)
devices: Dict[str, WebSocket] = {}
viewers: Dict[str, WebSocket] = {}
sessions: Dict[str, Dict[str, Any]] = {}

def turn_rest_credentials(ttl_seconds: int = 600) -> Dict[str, str]:
    # TURN REST: username = "<expiry_unix_timestamp>:<user>"
    expiry = int(time.time()) + ttl_seconds
    username = f"{expiry}:viewer"
    mac = hmac.new(TURN_SHARED_SECRET.encode(), username.encode(), hashlib.sha1).digest()
    password = base64.b64encode(mac).decode()
    return {"username": username, "password": password, "ttl": ttl_seconds}

def ice_servers() -> list:
    creds = turn_rest_credentials(600)
    return [
        {"urls": [f"stun:{TURN_HOST}:{TURN_PORT}"]},
        {"urls": [f"turn:{TURN_HOST}:{TURN_PORT}?transport=udp",
                  f"turn:{TURN_HOST}:{TURN_PORT}?transport=tcp"],
         "username": creds["username"], "credential": creds["password"]}
    ]

async def send_json(ws: WebSocket, msg: Dict[str, Any]):
    await ws.send_text(json.dumps(msg))

@app.websocket("/ws/device")
async def ws_device(ws: WebSocket, device_id: str, token: str):
    await ws.accept()
    # MVP auth: token must match stored value in Redis (provision once)
    expected = await r.get(f"device_token:{device_id}")
    if expected is None:
        # First time provisioning shortcut (MVP): set it
        await r.set(f"device_token:{device_id}", token)
    else:
        if expected.decode() != token:
            await ws.close(code=4401)
            return

    devices[device_id] = ws
    await r.set(f"device_online:{device_id}", "1", ex=30)

    try:
        while True:
            msg = json.loads(await ws.receive_text())
            t = msg.get("type")
            if t == "heartbeat":
                await r.set(f"device_online:{device_id}", "1", ex=30)
            elif t in ("offer", "answer", "ice_candidate", "hangup"):
                sid = msg.get("session_id")
                sess = sessions.get(sid)
                if not sess:
                    continue
                vws = viewers.get(sess["viewer_id"])
                if vws:
                    await send_json(vws, msg)
    except WebSocketDisconnect:
        pass
    finally:
        devices.pop(device_id, None)
        await r.delete(f"device_online:{device_id}")

@app.websocket("/ws/viewer")
async def ws_viewer(ws: WebSocket):
    await ws.accept()
    viewer_id = f"viewer-{id(ws)}"
    viewers[viewer_id] = ws

    try:
        while True:
            msg = json.loads(await ws.receive_text())
            t = msg.get("type")

            if t == "viewer_join":
                device_id = msg["device_id"]
                stream = msg.get("stream", "raw")  # "raw" or "ai"
                dws = devices.get(device_id)
                if not dws:
                    await send_json(ws, {"type": "error", "error": "device_offline"})
                    continue

                session_id = f"s-{int(time.time()*1000)}-{device_id}"
                sessions[session_id] = {"device_id": device_id, "viewer_id": viewer_id, "stream": stream}

                await send_json(ws, {"type": "session_created", "session_id": session_id, "ice_servers": ice_servers()})
                # Tell device to start stream and create offer
                await send_json(dws, {"type": "start_session", "session_id": session_id, "stream": stream, "ice_servers": ice_servers()})

            elif t in ("offer", "answer", "ice_candidate", "hangup"):
                sid = msg.get("session_id")
                sess = sessions.get(sid)
                if not sess:
                    continue
                dws = devices.get(sess["device_id"])
                if dws:
                    await send_json(dws, msg)

    except WebSocketDisconnect:
        pass
    finally:
        viewers.pop(viewer_id, None)
