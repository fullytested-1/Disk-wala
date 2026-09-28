import asyncio
import os
import time
from urllib.parse import quote

# Python 3.12+ ഇഷ്യൂ ഒഴിവാക്കാൻ Pyrogram ഇംപോർട്ടിന് മുൻപ് ലൂപ്പ് സെറ്റ് ചെയ്യുന്നു
try:
    asyncio.get_event_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

import aiohttp
from aiohttp import web
from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.types import Message

load_dotenv()

API_ID = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", 8080))

if not all([API_ID, API_HASH, BOT_TOKEN]):
    raise ValueError(
        "API_ID, API_HASH, അല്ലെങ്കിൽ BOT_TOKEN ലഭ്യമല്ല! Environment variables പരിശോധിക്കുക."
    )

app = Client("diskwala_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)


# --- Render Health Check Web Server ---
async def handle_ping(request):
    return web.Response(text="Bot is running smoothly!")


async def start_web_server():
    server = web.Application()
    server.router.add_get("/", handle_ping)
    server.router.add_get("/health", handle_ping)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Web server started on port {PORT}")


# --- Progress Bar Handler ---
async def progress(
    current, total, message: Message, start_time, action="Uploading"
):
    now = time.time()
    diff = now - start_time
    if round(diff % 4.00) == 0 or current == total:
        percentage = current * 100 / total
        speed = current / diff if diff > 0 else 0
        eta = round((total - current) / speed) if speed > 0 else 0

        current_mb = current / (1024 * 1024)
        total_mb = total / (1024 * 1024)
        speed_kb = speed / 1024

        text = (
            f"⚡ **{action}...**\n"
            f"📊 **Progress:** `{percentage:.2f}%`\n"
            f"💾 **Size:** `{current_mb:.2f} MB / {total_mb:.2f} MB`\n"
            f"🚀 **Speed:** `{speed_kb:.2f} KB/s`\n"
            f"⏱️ **ETA:** `{eta}s`"
        )
        try:
            await message.edit_text(text)
        except Exception:
            pass


# --- DiskWala API Fetch ---
async def fetch_diskwala_data(url: str):
    api_url = f"https://diskwala.net/web/api/status?link={quote(url, safe='')}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://diskwala.net/",
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(api_url, headers=headers, timeout=20) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get("ok") and data.get("file"):
                    return data["file"]
    return None


@app.on_message(filters.command("start"))
async def start_handler(client, message: Message):
    await message.reply_text(
        "👋 **ഹലോ!**\n\nDiskWala ലിങ്ക് അയക്കൂ, ഫയൽ ഞാൻ direct ആയി അപ്‌ലോഡ് ചെയ്തു തരാം."
    )


@app.on_message(filters.text & filters.private)
async def diskwala_handler(client, message: Message):
    user_text = message.text.strip()

    if "diskwala" not in user_text:
        await message.reply_text("⚠️ സാധുവായ ഒരു DiskWala ലിങ്ക് നൽകുക.")
        return

    status_msg = await message.reply_text("🔍 ഫയൽ വിവരങ്ങൾ പരിശോധിക്കുന്നു...")

    file_info = await fetch_diskwala_data(user_text)
    if not file_info or not file_info.get("downloadUrl"):
        await status_msg.edit_text(
            "❌ ഡൗൺലോഡ് ലിങ്ക് ലഭിച്ചില്ല. ലിങ്ക് ശരിയാണോ എന്ന് പരിശോധിക്കുക."
        )
        return

    download_url = file_info["downloadUrl"]
    filename = (
        f"{file_info.get('name', 'file')}.{file_info.get('extension', 'mp4')}"
    )
    thumb_url = file_info.get("thumb")
    thumb_path = f"thumb_{int(time.time())}.jpg"

    await status_msg.edit_text(f"📥 **ഡൗൺലോഡ് ആരംഭിക്കുന്നു:** `{filename}`")

    start_time = time.time()
    try:
        # 1. Download
        async with aiohttp.ClientSession() as session:
            async with session.get(download_url) as resp:
                if resp.status != 200:
                    await status_msg.edit_text(
                        "❌ ഡൗൺലോഡ് സെർവറിൽ നിന്ന് ഡാറ്റ ലഭിച്ചില്ല."
                    )
                    return

                total_size = int(resp.headers.get("content-length", 0))
                downloaded = 0

                with open(filename, "wb") as f:
                    async for chunk in resp.content.iter_chunked(1024 * 1024):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total_size > 0:
                            await progress(
                                downloaded,
                                total_size,
                                status_msg,
                                start_time,
                                action="Downloading",
                            )

        # 2. Thumbnail
        if thumb_url:
            async with aiohttp.ClientSession() as session:
                async with session.get(thumb_url) as t_resp:
                    if t_resp.status == 200:
                        with open(thumb_path, "wb") as tf:
                            tf.write(await t_resp.read())

        # 3. Upload
        await status_msg.edit_text("📤 **ടെലിഗ്രാമിലേക്ക് അപ്‌ലോഡ് ചെയ്യുന്നു...**")
        upload_start = time.time()
        caption = f"🎬 **File Name:** `{filename}`"
        thumb_file = thumb_path if os.path.exists(thumb_path) else None

        if file_info.get("extension") in ["mp4", "mkv", "webm", "mov"]:
            await message.reply_video(
                video=filename,
                caption=caption,
                thumb=thumb_file,
                progress=progress,
                progress_args=(status_msg, upload_start, "Uploading Video"),
            )
        else:
            await message.reply_document(
                document=filename,
                caption=caption,
                thumb=thumb_file,
                progress=progress,
                progress_args=(status_msg, upload_start, "Uploading Document"),
            )

        await status_msg.delete()

    except Exception as e:
        await status_msg.edit_text(f"❌ **എറർ സംഭവിച്ചു:** `{str(e)}`")

    finally:
        if os.path.exists(filename):
            os.remove(filename)
        if os.path.exists(thumb_path):
            os.remove(thumb_path)


# --- Main Runner ---
async def main():
    await start_web_server()
    await app.start()
    print("Bot is up and listening for messages...")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
