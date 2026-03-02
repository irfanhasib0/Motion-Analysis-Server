import os
from datetime import datetime
from telethon import TelegramClient, events

#API_ID = int(os.environ["TG_API_ID"])
#API_HASH = os.environ["TG_API_HASH"]

API_ID = 33823397 
API_HASH = "2bad112405d60dbd549c2913625e3567"

# REQUIRED: set this to the chat you want to monitor (from list_chats.py)
TARGET_CHAT_ID = 8389619370#8523833100

DOWNLOAD_DIR = './downloads'
if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

client = TelegramClient("telethon_session", API_ID, API_HASH)


def ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@client.on(events.NewMessage(chats=TARGET_CHAT_ID))
async def on_new_message(event: events.NewMessage.Event):
    msg = event.message

    # Text
    if msg.message:
        print(f"{ts()} [TEXT] {msg.message}")

    # Video (or any media)
    if msg.media:
        # This will download videos/photos/documents etc.
        path = await msg.download_media(file=DOWNLOAD_DIR)
        if path:
            print(f"{ts()} [MEDIA] saved: {path}")


async def main():
    print(f"Listening on chat_id={TARGET_CHAT_ID} ...")
    await client.run_until_disconnected()


if __name__ == "__main__":
    client.start()  # will prompt login if first run
    client.loop.run_until_complete(main())
