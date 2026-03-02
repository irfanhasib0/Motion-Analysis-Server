import os
from typing import Optional

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile

#BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")
#CHAT_ID = os.environ.get("TG_CHAT_ID")

BOT_TOKEN = "8389619370:AAGyrUB83sIgozXbT7qfxhVzkYTwWHNMPMo"
CHAT_ID   = "8523833100"

if not BOT_TOKEN or not CHAT_ID:
    raise RuntimeError("Set TG_BOT_TOKEN and TG_CHAT_ID environment variables")

API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

app = FastAPI(title="Telegram Alarm Sender")


async def tg_send_message(text: str) -> None:
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(f"{API_BASE}/sendMessage", json=payload)
    body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    if response.status_code != 200 or not body.get("ok"):
        raise HTTPException(status_code=500, detail=f"Telegram sendMessage failed: {response.text}")


async def tg_send_video(video_bytes: bytes, filename: str, caption: str = "") -> None:
    files = {"video": (filename, video_bytes, "video/mp4")}
    data = {"chat_id": CHAT_ID, "caption": caption}
    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(f"{API_BASE}/sendVideo", data=data, files=files)
    body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    if response.status_code != 200 or not body.get("ok"):
        raise HTTPException(status_code=500, detail=f"Telegram sendVideo failed: {response.text}")


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.post("/alarm")
async def alarm(text: str = Form(...), video: Optional[UploadFile] = File(None)) -> dict:
    await tg_send_message(text)

    if video is not None:
        content = await video.read()
        await tg_send_video(content, video.filename or "clip.mp4", caption="Alarm clip")

    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9001)