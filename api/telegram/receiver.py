import os

from telethon import TelegramClient, events

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
TARGET_CHAT_ID = os.environ.get("TG_TARGET_CHAT_ID")
DOWNLOAD_DIR = os.environ.get("TG_DOWNLOAD_DIR", "downloads")

if TARGET_CHAT_ID is not None:
    TARGET_CHAT_ID = int(TARGET_CHAT_ID)

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

client = TelegramClient("receiver", API_ID, API_HASH)


@client.on(events.NewMessage)
async def on_new_message(event):
    if TARGET_CHAT_ID is not None and event.chat_id != TARGET_CHAT_ID:
        return

    message = event.message

    if message.message:
        print(f"[text] chat={event.chat_id}: {message.message}")

    if message.video or (message.media and getattr(message.media, "document", None)):
        saved_path = await message.download_media(file=DOWNLOAD_DIR)
        if saved_path:
            print(f"[media] saved to: {saved_path}")


async def main():
    print("Listening for Telegram messages...")
    await client.run_until_disconnected()


with client:
    client.loop.run_until_complete(main())
