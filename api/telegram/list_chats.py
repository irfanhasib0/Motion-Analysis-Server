import os
from telethon import TelegramClient

API_ID = "33823397" 
API_HASH = "2bad112405d60dbd549c2913625e3567"

client = TelegramClient("telethon_session", API_ID, API_HASH)

async def main():
    async for dialog in client.iter_dialogs():
        # dialog.id is the chat id you want
        print(f"{dialog.id:>14} | {dialog.name}")

with client:
    client.loop.run_until_complete(main())
