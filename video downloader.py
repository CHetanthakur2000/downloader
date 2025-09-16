import telebot
from telebot import types
from yt_dlp import YoutubeDL
import sqlite3, os
import threading

TOKEN = os.environ['TOKEN']
bot = telebot.TeleBot(TOKEN)

# Database setup
conn = sqlite3.connect('users.db', check_same_thread=False)
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, premium INTEGER)''')
conn.commit()

# Replace with your UPI ID
UPI_ID = "anjuanju7640@naviaxis"
# Replace with your Telegram ID as admin
ADMIN_ID = 123456789

def is_premium(user_id):
    c.execute("SELECT premium FROM users WHERE id=?", (user_id,))
    result = c.fetchone()
    return result and result[0] == 1

# /start command
@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(message.chat.id, "Send me a YouTube link to download!\nFree: 480p\nPremium: HD 720p/1080p")

# /upgrade command - automatically premium + payment option
@bot.message_handler(commands=['upgrade'])
def upgrade(message):
    user_id = message.from_user.id

    # Automatic premium for everyone
    c.execute("INSERT OR REPLACE INTO users(id, premium) VALUES(?,?)", (user_id, 1))
    conn.commit()

    # Also show payment option if user wants to pay
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton("Paid ✅", callback_data=f"confirm_{user_id}"))

    bot.send_message(message.chat.id,
                     f"🎉 You are now Premium! You can download HD videos (720p/1080p) without payment.\n\n"
                     f"If you want, you can also support via UPI ₹10:\nUPI ID: {UPI_ID}\n\n"
                     f"After payment, admin will confirm and show a special message.",
                     reply_markup=keyboard)

# Handle YouTube links
@bot.message_handler(func=lambda m: True)
def handle_link(message):
    url = message.text.strip()
    user_id = message.from_user.id

    bot.user_data = getattr(bot, 'user_data', {})
    bot.user_data[user_id] = url

    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton("144p", callback_data="144p"))
    keyboard.add(types.InlineKeyboardButton("260p", callback_data="260p"))
    keyboard.add(types.InlineKeyboardButton("360p", callback_data="360p"))
    keyboard.add(types.InlineKeyboardButton("480p", callback_data="480p"))
    keyboard.add(types.InlineKeyboardButton("720p", callback_data="720p"))
    bot.send_message(message.chat.id, "Select video quality:", reply_markup=keyboard)

# Function to download video safely in background
def download_video(url, quality, chat_id, user_id):
    try:
        ydl_opts = {
            'format': f'bestvideo[height<={quality.replace("p","")}] + bestaudio/best',
            'outtmpl': f'%(title)s.%(ext)s',
            'merge_output_format': 'mp4',
            'noplaylist': True,
            'quiet': True,
            'retries': 10,
            'socket_timeout': 30,
            'fragment_retries': 10,
            'continuedl': True
        }

        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            file_name = ydl.prepare_filename(info)

        bot.send_video(chat_id, open(file_name, 'rb'))
        os.remove(file_name)
        bot.user_data.pop(user_id, None)

    except Exception as e:
        bot.send_message(chat_id, f"Error: {str(e)}")

# Handle quality selection
@bot.callback_query_handler(func=lambda call: True)
def handle_quality(call):
    user_id = call.from_user.id
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
    
    # Start download in background thread
    threading.Thread(target=download_video, args=(url, quality, call.message.chat.id, user_id)).start()

bot.polling()


