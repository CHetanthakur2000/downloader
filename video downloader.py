import telebot
from telebot import types
from yt_dlp import YoutubeDL
import sqlite3, os
import threading
ghbOE_4yQjBuj5-F_yV1hS5qx7i89Oo'
bot = telebot.TeleBot(TOKEN)

# Database setup
conn = sqlite3.connect('users.db', check_same_thread=False)
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, premium INTEGER)''')
conn.commit()

# Replace with your UPI ID
UPI_ID = "yourupi@bank"
# Replace with your Telegram ID as admin
ADMIN_ID = 123456789

# Lock for thread-safe user_data
user_data_lock = threading.Lock()
bot.user_data = {}

def is_premium(user_id):
    c.execute("SELECT premium FROM users WHERE id=?", (user_id,))
    result = c.fetchone()
    return result and result[0] == 1

# /start command
@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(message.chat.id, 
        "Send me a video link to download!\n"
        "Free: 480p\nPremium: HD 720p/1080p\n\n"
        "Supported: YouTube, xHamster"
    )

# /upgrade command - show Paid button for users
@bot.message_handler(commands=['upgrade'])
def upgrade(message):
    user_id = message.from_user.id

    # Show Paid button only
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton("Paid ✅", callback_data=f"paid_{user_id}"))

    bot.send_message(message.chat.id,
                     f"Click Paid ✅ to request Premium access. Admin will approve it.\n\n"
                     f"Support via UPI ₹10: {UPI_ID}",
                     reply_markup=keyboard)

# Handle Paid requests
@bot.callback_query_handler(func=lambda call: call.data.startswith("paid_"))
def paid_request(call):
    user_id = int(call.data.split("_")[1])

    # If admin clicked on their own button → auto-approve
    if call.from_user.id == ADMIN_ID:
        c.execute("INSERT OR REPLACE INTO users(id, premium) VALUES(?,?)", (user_id, 1))
        conn.commit()
        bot.answer_callback_query(call.id, "🎉 You are admin. Premium granted!")
        bot.send_message(user_id, "🎉 You are now Premium!")
        return

    # Notify admin
    bot.send_message(ADMIN_ID,
                     f"User @{call.from_user.username} ({user_id}) clicked Paid ✅.\n"
                     f"Approve with: /approve_{user_id}")

    # Notify user
    bot.answer_callback_query(call.id, "✅ Your payment request has been sent to admin!")

# Admin approves request
@bot.message_handler(commands=lambda m: m.text.startswith("/approve_"))
def approve_payment(message):
    if message.from_user.id != ADMIN_ID:
        return

    user_id = int(message.text.split("_")[1])
    c.execute("INSERT OR REPLACE INTO users(id, premium) VALUES(?,?)", (user_id, 1))
    conn.commit()
    
    bot.send_message(user_id, "🎉 Your payment is confirmed! You are now Premium.")
    bot.send_message(ADMIN_ID, f"User {user_id} is now Premium.")

# Handle video links (YouTube + xHamster)
@bot.message_handler(func=lambda m: True)
def handle_link(message):
    url = message.text.strip()
    user_id = message.from_user.id

    # Supported sites
    if any(site in url for site in ["youtube.com", "youtu.be", "xhamster.com"]):
        with user_data_lock:
            bot.user_data[user_id] = url

        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(types.InlineKeyboardButton("480p", callback_data="480p"))
        keyboard.add(types.InlineKeyboardButton("720p", callback_data="720p"))
        keyboard.add(types.InlineKeyboardButton("1080p", callback_data="1080p"))
        bot.send_message(message.chat.id, "Select video quality:", reply_markup=keyboard)
    else:
        bot.send_message(message.chat.id, "❌ Unsupported link. Please send a valid YouTube or xHamster video link.")

# Download video function
def download_video(url, quality, chat_id, user_id):
    for attempt in range(3):  # Retry 3 times
        try:
            ydl_opts = {
                'format': f'bestvideo[height<={quality.replace("p","")}] + bestaudio/best',
                'outtmpl': f'%(title)s.%(ext)s',
                'merge_output_format': 'mp4',
                'noplaylist': True,
                'quiet': True,
                'retries': 20,
                'socket_timeout': 60,
                'fragment_retries': 20,
                'continuedl': True
            }

            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                file_name = os.path.abspath(ydl.prepare_filename(info))

            # Check Telegram file size limit
            if os.path.getsize(file_name) < 50*1024*1024:
                bot.send_video(chat_id, open(file_name, 'rb'))
            else:
                # Send direct download link instead
                direct_url = info.get("url", url)
                bot.send_message(chat_id,
                                 f"⚠️ Video too big for Telegram "
                                 f"({os.path.getsize(file_name)//1024//1024}MB).\n\n"
                                 f"👉 Download manually: {direct_url}")

            os.remove(file_name)
            with user_data_lock:
                bot.user_data.pop(user_id, None)
            break  # success

        except Exception as e:
            if attempt == 2:
                bot.send_message(chat_id, f"Error: {str(e)}\nDownload failed after 3 attempts.")

# Handle quality selection
@bot.callback_query_handler(func=lambda call: True)
def handle_quality(call):
    user_id = call.from_user.id
    with user_data_lock:
        url = bot.user_data.get(user_id)
    quality = call.data

    if not url:
        bot.answer_callback_query(call.id, "No video URL found. Send the link first.")
        return

    # Free users restriction
    if not is_premium(user_id) and quality != "480p":
        bot.answer_callback_query(call.id, "⚠️ Free users can only download 480p videos.")
        return

    bot.edit_message_text("Downloading video... ⏳", call.message.chat.id, call.message.message_id)

    threading.Thread(target=download_video, args=(url, quality, call.message.chat.id, user_id)).start()

bot.polling()


