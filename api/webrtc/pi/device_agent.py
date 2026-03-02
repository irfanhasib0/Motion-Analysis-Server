import asyncio, json, os, subprocess, signal
import websockets

SIGNAL_WSS = os.getenv("SIGNAL_WSS", "wss://signal.example.com/ws/device")
DEVICE_ID  = os.getenv("DEVICE_ID", "dev1")
DEVICE_TOKEN = os.getenv("DEVICE_TOKEN", "CHANGE_ME_DEVICE_TOKEN")

RTSP_URL = os.getenv("RTSP_URL", "rtsp://USER:PASS@CAM_IP:554/cam/realmonitor?channel=1&subtype=0&unicast=true&proto=Onvif")
# Strongly consider subtype=1 for lighter stream once confirmed.

proc_raw = None
proc_ai  = None

def kill_proc(p):
    if p and p.poll() is None:
        p.send_signal(signal.SIGTERM)
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()

def raw_pipeline_cmd():
    # Launch the raw WebRTC publisher
    return ["python3", "publisher_raw.py"]

def ai_pipeline_cmd():
    # Launch the AI WebRTC publisher  
    return ["python3", "publisher_ai.py"]

async def main():
    global proc_raw, proc_ai
    url = f"{SIGNAL_WSS}?device_id={DEVICE_ID}&token={DEVICE_TOKEN}"

    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                await ws.send(json.dumps({"type": "heartbeat"}))
                async for text in ws:
                    msg = json.loads(text)
                    t = msg.get("type")

                    if t == "start_session":
                        stream = msg.get("stream", "raw")
                        # In the MVP we keep publishers always-on or one-session-at-a-time.
                        # You'll integrate the actual WebRTC offer creation in the publisher module.
                        if stream == "raw":
                            if proc_raw is None or proc_raw.poll() is not None:
                                proc_raw = subprocess.Popen(raw_pipeline_cmd())
                        else:
                            if proc_ai is None or proc_ai.poll() is not None:
                                proc_ai = subprocess.Popen(ai_pipeline_cmd())

                    elif t == "hangup":
                        # optional: stop pipelines when no viewers
                        pass

                    # Forward offer/answer/ice to publishers (when integrated)
        except Exception as e:
            print(f"Connection error: {e}")
            await asyncio.sleep(2)

if __name__ == "__main__":
    asyncio.run(main())
