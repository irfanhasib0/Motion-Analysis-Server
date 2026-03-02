# Telegram Relay (Minimal)

Minimal working project for:
- Sending alarm text + optional short video from FastAPI via Telegram bot
- Receiving new Telegram messages with a user account client (Python Telethon or Node gramjs)

## 1) Setup

### Create bot + get chat ID
1. Create bot with `@BotFather` and copy `TG_BOT_TOKEN`
2. Send one message to your bot/chat
3. Open:
   - `https://api.telegram.org/bot<TG_BOT_TOKEN>/getUpdates`
4. Copy `chat.id` as `TG_CHAT_ID`

### Create user API credentials (for receivers)
1. Go to `https://my.telegram.org`
2. Create app and copy `TG_API_ID` + `TG_API_HASH`

## 2) Python dependencies

```bash
cd telegram
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 3) Run sender (FastAPI)

```bash
export TG_BOT_TOKEN="123456:ABCDEF..."
export TG_CHAT_ID="123456789"
uvicorn server:app --host 0.0.0.0 --port 8000
```

Test text only:
```bash
curl -X POST http://127.0.0.1:8000/alarm -F 'text=Motion detected at Gate 2'
```

Test text + video:
```bash
curl -X POST http://127.0.0.1:8000/alarm \
  -F 'text=Motion detected + clip' \
  -F 'video=@/path/to/short_clip.mp4'
```

## 4) Run Python receiver (Telethon)

```bash
export TG_API_ID="12345"
export TG_API_HASH="your_api_hash"
export TG_TARGET_CHAT_ID="123456789"  # optional
python receiver.py
```

First run asks for phone + code and creates `receiver.session`.

## 5) Run Node receiver (gramjs)

```bash
cd telegram
npm install
export TG_API_ID="12345"
export TG_API_HASH="your_api_hash"
node receiver.js
```

First run prints a `TG_STRING_SESSION` you can save for future runs.

## Notes
- Keep alarm clips short (2–6s) and small.
- Telegram acts as relay, so this works even when your local FastAPI server is behind NAT.
