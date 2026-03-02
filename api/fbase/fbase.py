import os
from typing import Optional, Dict, Any
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import firebase_admin
from firebase_admin import credentials, messaging

#BBcS6OsSNXUFtqFZkryAIj0QS1IFe0dtaGiQGOxEITy1fX8ro4u9RimVWhMTauLBNrjXTsTGFJ3MTwH3Rftl__A
#vpjDdyLVT4MjOpG_3lLqowXg9kOcvbonkBHjzbrta7k

# ---- Firebase Admin init ----
#SERVICE_ACCOUNT_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "service-account.json")
SERVICE_ACCOUNT_PATH = '../configs/nvr-101-firebase-adminsdk-fbsvc-43c07822a3.json'
if not firebase_admin._apps:
    cred = credentials.Certificate(SERVICE_ACCOUNT_PATH)
    firebase_admin.initialize_app(cred)

app = FastAPI(title="FCM Hello World (FastAPI)")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, specify your frontend domain
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# In production: store tokens in DB keyed by user/org/device.
TOKENS: Dict[str, str] = {}  # user_id -> fcm_token


class RegisterTokenReq(BaseModel):
    user_id: str = Field(..., examples=["user_123"])
    token: str


class SendHelloReq(BaseModel):
    token: Optional[str] = None
    user_id: Optional[str] = None
    title: str = "Hello"
    body: str = "Hello World from FastAPI + Firebase!"
    data: Dict[str, Any] = Field(default_factory=lambda: {"kind": "hello"})


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/register_token")
def register_token(req: RegisterTokenReq):
    TOKENS[req.user_id] = req.token
    return {"ok": True, "user_id": req.user_id}


@app.post("/send_hello")
def send_hello(req: SendHelloReq):
    token = req.token
    if not token and req.user_id:
        token = TOKENS.get(req.user_id)

    if not token:
        raise HTTPException(status_code=400, detail="Provide token or user_id with registered token")

    message = messaging.Message(
        token=token,
        notification=messaging.Notification(title=req.title, body=req.body),
        data={k: str(v) for k, v in (req.data or {}).items()},
    )

    try:
        message_id = messaging.send(message)
        return {"ok": True, "message_id": message_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"FCM send failed: {e}")
    
if __name__ == "__main__":
    import uvicorn
    uvicorn.run('fbase:app', host="0.0.0.0", port=3001, reload=True)
