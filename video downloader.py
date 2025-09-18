import os
import re
import time
import tempfile
import threading
import shutil
import sqlite3
from telebot import TeleBot, types
from yt_dlp import YoutubeDL
from moviepy.editor import VideoFileClip
from google.cloud import storage
from PIL import Image

# ---------------- CONFIG ----------------
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GCS_BUCKET_NAME = os.getenv("GCS_BUCKET_NAME", "your-gcs-bucket-name")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # int me convert

# Make sure environment variable GOOGLE_APPLICATION_CREDENTIALS is set to service account JSON path
bot = TeleBot(TOKEN)

# ---------- Database (premium) ----------
conn = sqlite3.connect("users.db", check_same_thread=False)
c = conn.cursor()
c.execute("CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, premium INTEGER)")
conn.commit()

def is_premium(user_id):
    c.execute("SELECT premium FROM users WHERE id=?", (user_id,))
    r = c.fetchone()
    return r and r[0] == 1

# ---------- In-memory session data ----------
user_data_lock = threading.Lock()
user_data = {}  # { chat_id: {...} }

# ---------- Utilities ----------
def safe_filename(name: str, max_len=120) -> str:
    if not name:
        return "file"
    name = name.replace("\n", " ").replace("\r", " ")
    name = re.sub(r'[\\/*?:"<>|]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    if len(name) > max_len:
        name = name[:max_len]
    return name

def upload_to_cloud(file_path, title):
    try:
        client = storage.Client()
        bucket = client.bucket(GCS_BUCKET_NAME)
        blob_name = f"files/{safe_filename(title)}_{int(os.path.getmtime(file_path))}{os.path.splitext(file_path)[1]}"
        blob = bucket.blob(blob_name)
        blob.upload_from_filename(file_path)
        url = blob.generate_signed_url(expiration=7200)  # 2 hours
        return url
    except Exception as e:
        print("Cloud upload error:", e)
        return None

# ---------- /start and /upgrade ----------
@bot.message_handler(commands=["start"])
def cmd_start(m):
    bot.send_message(m.chat.id,
        "📥 *Video Downloader Bot*\n\n"
        "Commands:\n"
        "• /audio → Extract audio (interactive)\n"
        "• /video → Download or Trim video (interactive)\n"
        "• /upgrade → Become premium (admin approval)\n\n"
        "Free users limited to 480p.",
        parse_mode="Markdown"
    )

@bot.message_handler(commands=["upgrade"])
def cmd_upgrade(m):
    uid = m.from_user.id
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("✅ I have paid", callback_data=f"paid_{uid}"))
    bot.send_message(m.chat.id,
                     f"💎 Send ₹10 to UPI: `anjuanju7640@naviaxis`\n\nAfter payment click the ✅ button below.",
                     reply_markup=kb, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith("paid_"))
def cb_paid(call):
    target_uid = int(call.data.split("_")[1])
    if call.from_user.id == ADMIN_ID:
        c.execute("INSERT OR REPLACE INTO users(id, premium) VALUES(?,1)", (target_uid,))
        conn.commit()
        bot.answer_callback_query(call.id, "✅ Premium granted!")
        bot.send_message(target_uid, "🎉 You are now Premium!")
    else:
        bot.send_message(ADMIN_ID,
                         f"🔔 User @{call.from_user.username or call.from_user.first_name} ({call.from_user.id}) claims paid. Approve with /approve_{call.from_user.id}")
        bot.answer_callback_query(call.id, "✅ Request sent to admin.")

@bot.message_handler(func=lambda m: m.text and m.text.startswith("/approve_"))
def cmd_approve(m):
    if m.from_user.id != ADMIN_ID:
        return
    try:
        uid = int(m.text.split("_",1)[1])
        c.execute("INSERT OR REPLACE INTO users(id, premium) VALUES(?,1)", (uid,))
        conn.commit()
        bot.send_message(uid, "🎉 Your payment is confirmed. You are now Premium.")
        bot.send_message(ADMIN_ID, f"✅ User {uid} is now Premium.")
    except Exception as e:
        bot.send_message(ADMIN_ID, f"❌ Approve failed: {e}")

# ---------- /audio flow (unchanged) ----------
@bot.message_handler(commands=["audio"])
def cmd_audio(m):
    msg = bot.reply_to(m, "🎵 Send the *link* of the video to extract audio from:", parse_mode="Markdown")
    bot.register_next_step_handler(msg, handle_audio_link)

def handle_audio_link(message):
    chat_id = message.chat.id
    url = message.text.strip()
    try:
        with YoutubeDL({"quiet": True, "skip_download": True}) as ydl:
            info = ydl.extract_info(url, download=False)
        title_raw = info.get("title", "audio")
        title = safe_filename(title_raw)
        formats = info.get("formats", [])
        audio_formats = [f for f in formats if f.get("vcodec") == "none"]
        if not audio_formats:
            bot.send_message(chat_id, "❌ No audio formats found for this link.")
            return
        with user_data_lock:
            user_data[chat_id] = {"mode":"audio", "url": url, "title": title, "audio_formats": audio_formats}
        kb = types.InlineKeyboardMarkup()
        for idx, f in enumerate(sorted(audio_formats, key=lambda x: x.get("abr") or 0, reverse=True)):
            abr = f.get("abr") or "?"
            ext = f.get("ext") or ""
            size = f.get("filesize") or f.get("filesize_approx") or 0
            size_mb = f"{size/1024/1024:.1f} MB" if size else "?"
            kb.add(types.InlineKeyboardButton(f"{abr} kbps {ext} ({size_mb})", callback_data=f"audio_get_{idx}"))
        bot.send_message(chat_id, f"🎵 *{title}*\nSelect audio quality:", reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        bot.send_message(chat_id, f"❌ Failed to read link: {e}")

@bot.callback_query_handler(func=lambda c: c.data.startswith("audio_get_"))
def cb_audio_get(call):
    chat_id = call.message.chat.id
    try:
        idx = int(call.data.split("_")[2])
        with user_data_lock:
            ud = user_data.get(chat_id)
        if not ud or ud.get("mode")!="audio":
            bot.answer_callback_query(call.id, "Session expired. Send /audio again.")
            return
        fmt = ud["audio_formats"][idx]
        bot.answer_callback_query(call.id, "⬇️ Downloading audio...")
        threading.Thread(target=process_audio, args=(chat_id, fmt)).start()
    except Exception as e:
        bot.answer_callback_query(call.id, f"Error: {e}")

def process_audio(chat_id, fmt):
    with user_data_lock:
        ud = user_data.get(chat_id)
    if not ud:
        bot.send_message(chat_id, "❌ Session expired.")
        return
    url = ud["url"]
    title = ud["title"]
    temp_dir = tempfile.mkdtemp()
    try:
        outtmpl = os.path.join(temp_dir, f"{title}.%(ext)s")
        ydl_opts = {"format": fmt["format_id"], "outtmpl": outtmpl, "quiet": True, "noplaylist": True}
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            file_path = ydl.prepare_filename(info)
        size_mb = os.path.getsize(file_path)/(1024*1024)
        if size_mb < 50:
            with open(file_path, "rb") as f:
                bot.send_audio(chat_id, f, caption=f"🎵 {title}")
        else:
            cloud = upload_to_cloud(file_path, title)
            if cloud:
                bot.send_message(chat_id, f"⚠️ Audio too big ({int(size_mb)}MB). Cloud link:\n{cloud}")
            else:
                bot.send_message(chat_id, "🚨 Cloud upload failed.")
    except Exception as e:
        print("process_audio error:", e)
        bot.send_message(chat_id, f"❌ Audio failed: {e}")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        with user_data_lock:
            user_data.pop(chat_id, None)

# ---------- /video flow (fixed audio issue) ----------
@bot.message_handler(commands=["video"])
def cmd_video(m):
    msg = bot.reply_to(m, "🎥 Send the *link* of the video:", parse_mode="Markdown")
    bot.register_next_step_handler(msg, handle_video_link)

def handle_video_link(message):
    chat_id = message.chat.id
    url = message.text.strip()
    try:
        with YoutubeDL({"quiet": True, "skip_download": True}) as ydl:
            info = ydl.extract_info(url, download=False)
        title_raw = info.get("title", "video")
        title = safe_filename(title_raw)
        formats = info.get("formats", [])
        video_formats = [f for f in formats if f.get("height")]
        if not video_formats:
            bot.send_message(chat_id, "❌ No video formats found.")
            return
        video_formats = sorted(video_formats, key=lambda x: x.get("height") or 0)
        with user_data_lock:
            user_data[chat_id] = {"mode":"video", "url": url, "title": title, "video_formats": video_formats}
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🎬 Download Full Video", callback_data="video_action_full"))
        kb.add(types.InlineKeyboardButton("✂️ Trim Video", callback_data="video_action_trim"))
        bot.send_message(chat_id, f"🎬 *{title}*\nChoose action:", reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        bot.send_message(chat_id, f"❌ Failed to read link: {e}")

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("video_action_"))
def cb_video_action(call):
    chat_id = call.message.chat.id
    action = call.data.split("_",2)[2]
    with user_data_lock:
        ud = user_data.get(chat_id)
    if not ud or ud.get("mode")!="video":
        bot.answer_callback_query(call.id, "Session expired. /video again.")
        return
    kb = types.InlineKeyboardMarkup()
    vfmts = ud["video_formats"]
    vfmts_sorted = sorted(vfmts, key=lambda x: x.get("height") or 0)
    for idx, f in enumerate(vfmts_sorted):
        h = f.get("height") or "?"
        ext = f.get("ext") or "mp4"
        size = f.get("filesize") or f.get("filesize_approx") or 0
        size_mb = f"{size/1024/1024:.1f} MB" if size else "?"
        kb.add(types.InlineKeyboardButton(f"{h}p {ext} ({size_mb})", callback_data=f"video_get_{action}_{idx}"))
    bot.send_message(chat_id, "Select quality:", reply_markup=kb)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("video_get_"))
def cb_video_get(call):
    chat_id = call.message.chat.id
    _, _, action, idx = call.data.split("_")
    idx = int(idx)
    with user_data_lock:
        ud = user_data.get(chat_id)
    if not ud or ud.get("mode")!="video":
        bot.answer_callback_query(call.id, "Session expired. /video again.")
        return
    vfmts = ud["video_formats"]
    fmt = vfmts[idx]
    quality = fmt.get("height", 480)
    if not is_premium(call.from_user.id) and quality > 480:
        bot.answer_callback_query(call.id, "⚠️ Free users limited to 480p.")
        return
    if action == "full":
        bot.answer_callback_query(call.id, f"⬇️ Downloading {quality}p...")
        threading.Thread(target=process_video, args=(chat_id, fmt, None)).start()
    elif action == "trim":
        with user_data_lock:
            ud["chosen_fmt_for_trim"] = fmt
        bot.answer_callback_query(call.id, "✂️ Send trim times as `start-end` in seconds (e.g., 5-20).")

@bot.message_handler(func=lambda m: True, content_types=['text'])
def catch_trim_times(message):
    chat_id = message.chat.id
    text = message.text.strip()
    with user_data_lock:
        ud = user_data.get(chat_id)
    if not ud or ud.get("mode")!="video" or not ud.get("chosen_fmt_for_trim"):
        return
    try:
        parts = text.split("-")
        start = float(parts[0])
        end = float(parts[1])
        if end <= start:
            bot.send_message(chat_id, "⚠️ End must be greater than Start.")
            return
    except Exception:
        bot.send_message(chat_id, "⚠️ Use format `start-end` in seconds. Example: `5-20`")
        return
    fmt = ud.pop("chosen_fmt_for_trim")
    bot.send_message(chat_id, f"🔪 Trimming {int(start)}s to {int(end)}s ...")
    threading.Thread(target=process_video, args=(chat_id, fmt, (start, end))).start()

def process_video(chat_id, fmt, trim_times):
    with user_data_lock:
        ud = user_data.get(chat_id)
    if not ud:
        bot.send_message(chat_id, "❌ Session expired.")
        return
    url = ud["url"]
    title = ud["title"]
    temp_dir = tempfile.mkdtemp()
    try:
        outtmpl = os.path.join(temp_dir, f"{title}.%(ext)s")
        ydl_opts = {
            "format": f"{fmt['format_id']}+bestaudio/best",
            "outtmpl": outtmpl,
            "merge_output_format": "mp4",
            "quiet": True,
            "noplaylist": True,
        }
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            file_path = ydl.prepare_filename(info)
        final_path = file_path
        if trim_times:
            start, end = trim_times
            clip = VideoFileClip(file_path, audio=True).subclip(start, end)
            trimmed = os.path.join(temp_dir, f"trimmed_{title}.mp4")
            clip.write_videofile(trimmed, codec="libx264", audio_codec="aac", audio=True, verbose=False, logger=None)
            clip.close()
            final_path = trimmed
        try:
            clip_for_thumb = VideoFileClip(final_path)
            duration = clip_for_thumb.duration
            tthumb = 1 if duration > 1 else 0
            frame = clip_for_thumb.get_frame(tthumb)
            thumb_img = Image.fromarray(frame)
            thumb_path = os.path.join(temp_dir, f"thumb_{safe_filename(title)}.jpg")
            thumb_img.save(thumb_path)
            clip_for_thumb.close()
        except Exception as e:
            print("Thumb create failed:", e)
            thumb_path = None
            duration = 0
        size_mb = os.path.getsize(final_path)/(1024*1024)
        if size_mb < 50:
            with open(final_path, "rb") as vf:
                if thumb_path:
                    with open(thumb_path, "rb") as th:
                        bot.send_video(chat_id, vf, caption=f"{title}\nDuration: {int(duration)}s", thumb=th, supports_streaming=True, timeout=300)
                else:
                    bot.send_video(chat_id, vf, caption=f"{title}\nDuration: {int(duration)}s", supports_streaming=True, timeout=300)
        else:
            cloud_url = upload_to_cloud(final_path, title)
            if cloud_url:
                bot.send_message(chat_id, f"⚠️ File too big ({int(size_mb)}MB). Uploaded to cloud:\n{cloud_url}")
            else:
                bot.send_message(chat_id, "🚨 Cloud upload failed.")
    except Exception as e:
        print("process_video error:", e)
        bot.send_message(chat_id, f"❌ Failed: {e}")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        with user_data_lock:
            user_data.pop(chat_id, None)

# ---------- run ----------
print("Bot is running...")
bot.infinity_polling()
