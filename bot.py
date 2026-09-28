import asyncio
import json
import os
import re
import shutil
import time
import uuid
from urllib.parse import quote

# Python 3.12+ ൽ ക്രാഷ് ആവാതിരിക്കാൻ ഇവന്റ് ലൂപ്പ് ഉണ്ടാക്കുന്നു
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

import aiohttp
from aiohttp import web
from dotenv import load_dotenv
from pyrogram import Client, filters, idle
from pyrogram.types import Message

load_dotenv()

API_ID = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", 8080))

MAX_SIZE = 2 * 1024 * 1024 * 1024  # 2GB ടെലിഗ്രാം ബോട്ട് പരിധി

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
    print(f"Health server listening on port {PORT}", flush=True)
    return runner


# --- Progress Bar ---
_last_edit = {}


async def progress(current, total, message: Message, start_time, action="Uploading"):
    if not total:
        return
    now = time.time()
    key = message.id
    # 5 സെക്കൻഡിൽ ഒരിക്കൽ മാത്രം edit (FloodWait ഒഴിവാക്കാൻ)
    if current != total and now - _last_edit.get(key, 0) < 5:
        return
    _last_edit[key] = now

    diff = now - start_time
    percentage = current * 100 / total
    speed = current / diff if diff > 0 else 0
    eta = round((total - current) / speed) if speed > 0 else 0

    text = (
        f"⚡ **{action}...**\n"
        f"📊 **Progress:** `{percentage:.2f}%`\n"
        f"💾 **Size:** `{current / 1048576:.2f} MB / {total / 1048576:.2f} MB`\n"
        f"🚀 **Speed:** `{speed / 1024:.2f} KB/s`\n"
        f"⏱️ **ETA:** `{eta}s`"
    )
    try:
        await message.edit_text(text)
    except Exception:
        pass


# --- Helpers ---
def safe_filename(name: str, ext: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", str(name)).strip(" .") or "file"
    ext = re.sub(r"[^a-zA-Z0-9]", "", str(ext or "mp4")).lower() or "mp4"
    if name.lower().endswith("." + ext):
        name = name[: -(len(ext) + 1)]
    return f"{name[:100]}.{ext}"


# --- DiskWala API Fetch (Cloudflare മറികടക്കാൻ curl_cffi) ---
async def fetch_diskwala_data(url: str):
    from curl_cffi.requests import AsyncSession

    m = re.search(r"/app/([A-Za-z0-9]+)", url)
    file_id = m.group(1) if m else url.rstrip("/").split("/")[-1]

    candidates = [
        url,
        file_id,
        f"https://diskwala.net/app/{file_id}",
        f"https://www.diskwala.net/app/{file_id}",
        f"https://diskwala.com/app/{file_id}",
    ]

    headers = {
        "Referer": "https://diskwala.net/",
        "Accept": "application/json, text/plain, */*",
    }

    async with AsyncSession(impersonate="chrome") as session:
        for cand in candidates:
            api_url = f"https://diskwala.net/web/api/status?link={quote(cand, safe='')}"
            try:
                resp = await session.get(api_url, headers=headers, timeout=20)
                body = resp.text
                print(
                    f"[DEBUG] try={cand} status={resp.status_code} body={body[:300]}",
                    flush=True,
                )
                if resp.status_code != 200:
                    continue
                data = json.loads(body)
                if isinstance(data, dict) and data.get("ok") and data.get("file"):
                    file_obj = data["file"]
                    for k in ("downloadUrl", "download_url", "url", "link", "direct"):
                        if file_obj.get(k):
                            file_obj["downloadUrl"] = file_obj[k]
                            break
                    return file_obj
            except Exception as e:
                print(f"[DEBUG] error try={cand}: {type(e).__name__}: {e}", flush=True)
    return None


# --- Handlers ---
@app.on_message(filters.command("start") & filters.private)
async def start_handler(client, message: Message):
    await message.reply_text(
        "👋 **ഹലോ!**\n\nDiskWala ലിങ്ക് അയക്കൂ, ഫയൽ ഞാൻ നേരിട്ട് ഡൗൺലോഡ് ചെയ്ത് അപ്‌ലോഡ് ചെയ്തു തരാം."
    )


@app.on_message(filters.text & filters.private & ~filters.command("start"))
async def diskwala_handler(client, message: Message):
    user_text = message.text.strip()

    if "diskwala" not in user_text.lower():
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
    extension = str(file_info.get("extension") or "mp4").lstrip(".").lower()
    filename = safe_filename(file_info.get("name", "file"), extension)
    thumb_url = file_info.get("thumb")

    # ഓരോ റിക്വസ്റ്റിനും പ്രത്യേക ഫോൾഡർ
    work_dir = os.path.join("downloads", uuid.uuid4().hex)
    os.makedirs(work_dir, exist_ok=True)
    file_path = os.path.join(work_dir, filename)
    thumb_path = os.path.join(work_dir, "thumb.jpg")

    await status_msg.edit_text(f"📥 **ഡൗൺലോഡ് ആരംഭിക്കുന്നു:** `{filename}`")

    start_time = time.time()
    try:
        # 1. ഫയൽ ഡൗൺലോഡ്
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(download_url) as resp:
                if resp.status != 200:
                    await status_msg.edit_text(
                        "❌ ഡൗൺലോഡ് സെർവറിൽ നിന്ന് ഡാറ്റ ലഭിച്ചില്ല."
                    )
                    return

                total_size = int(resp.headers.get("content-length", 0))
                if total_size > MAX_SIZE:
                    await status_msg.edit_text(
                        "❌ ഫയൽ 2GB-യിൽ കൂടുതലാണ്. ടെലിഗ്രാം ബോട്ടിന് ഇത് അപ്‌ലോഡ് ചെയ്യാൻ പറ്റില്ല."
                    )
                    return

                downloaded = 0
                with open(file_path, "wb") as f:
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
                try:
                    async with session.get(
                        thumb_url, timeout=aiohttp.ClientTimeout(total=20)
                    ) as t_resp:
                        if t_resp.status == 200:
                            with open(thumb_path, "wb") as tf:
                                tf.write(await t_resp.read())
                except Exception:
                    pass

        # 3. ടെലിഗ്രാമിലേക്ക് അപ്‌ലോഡ്
        await status_msg.edit_text("📤 **ടെലിഗ്രാമിലേക്ക് അപ്‌ലോഡ് ചെയ്യുന്നു...**")
        upload_start = time.time()
        caption = f"🎬 **File Name:** `{filename}`"
        thumb_file = thumb_path if os.path.exists(thumb_path) else None

        if extension in ["mp4", "mkv", "webm", "mov"]:
            await message.reply_video(
                video=file_path,
                caption=caption,
                thumb=thumb_file,
                supports_streaming=True,
                progress=progress,
                progress_args=(status_msg, upload_start, "Uploading Video"),
            )
        else:
            await message.reply_document(
                document=file_path,
                caption=caption,
                thumb=thumb_file,
                progress=progress,
                progress_args=(status_msg, upload_start, "Uploading Document"),
            )

        await status_msg.delete()

    except Exception as e:
        try:
            await status_msg.edit_text(f"❌ **എറർ സംഭവിച്ചു:** `{str(e)}`")
        except Exception:
            pass

    finally:
        _last_edit.pop(status_msg.id, None)
        shutil.rmtree(work_dir, ignore_errors=True)


# --- Runner ---
async def main():
    runner = await start_web_server()
    print("Bot is starting...", flush=True)
    await app.start()
    print("Bot is up and listening for messages!", flush=True)
    await idle()
    await app.stop()
    await runner.cleanup()


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
