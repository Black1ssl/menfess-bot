import os
import re
import sqlite3
import asyncio
from datetime import datetime, date, timedelta

import httpx
from telegram import Update
from telegram.error import TimedOut, NetworkError
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import yt_dlp

# ======================
# KONFIGURASI (ENV)
# ======================

BOT_TOKEN = os.getenv("BOT_TOKEN")  # TOKEN DI RAILWAY
OWNER_ID = int(os.getenv("OWNER_ID", "0"))  # OWNER / SUPERUSER dari ENV (Railway)

TARGET_CHANNEL_ID = int(os.getenv("TARGET_CHANNEL_ID", "-1001234567890"))
CHANNEL_LOG_ID = int(os.getenv("CHANNEL_LOG_ID", "-1001234567892"))
GROUP_PUBLIK = int(os.getenv("GROUP_PUBLIK", "-1001234567891"))

MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "50"))

# Safety / concurrency tuning (env optional)
API_CONCURRENCY = int(os.getenv("API_CONCURRENCY", "5"))
SAFE_SLEEP = float(os.getenv("SAFE_SLEEP", "0.25"))

# ======================
# DATABASE
# ======================

conn = sqlite3.connect("database.db", check_same_thread=False)
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS limits (
    user_id INTEGER,
    type TEXT,
    count INTEGER,
    date TEXT
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS welcome (
    user_id INTEGER PRIMARY KEY
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS chat_stats (
    user_id INTEGER,
    count INTEGER,
    date TEXT
)
""")

conn.commit()

# ======================
# GLOBAL API RATE LIMIT (SEMAPHORE)
# ======================

API_LIMIT = asyncio.Semaphore(API_CONCURRENCY)

# ======================
# UTILITIES
# ======================

def is_link(text):
    return bool(re.search(r"http[s]?://", text))

def check_limit(user_id, limit_type, max_limit):
    today = str(date.today())
    cur.execute(
        "SELECT count FROM limits WHERE user_id=? AND type=? AND date=?",
        (user_id, limit_type, today),
    )
    row = cur.fetchone()

    if row and row[0] >= max_limit:
        return False

    if row:
        cur.execute(
            "UPDATE limits SET count=count+1 WHERE user_id=? AND type=? AND date=?",
            (user_id, limit_type, today),
        )
    else:
        cur.execute(
            "INSERT INTO limits VALUES (?, ?, 1, ?)",
            (user_id, limit_type, today),
        )

    conn.commit()
    return True

def add_chat_stat(user_id):
    today = str(date.today())
    cur.execute(
        "SELECT count FROM chat_stats WHERE user_id=? AND date=?",
        (user_id, today),
    )
    row = cur.fetchone()

    if row:
        cur.execute(
            "UPDATE chat_stats SET count=count+1 WHERE user_id=? AND date=?",
            (user_id, today),
        )
    else:
        cur.execute(
            "INSERT INTO chat_stats VALUES (?, 1, ?)",
            (user_id, today),
        )
    conn.commit()

# ======================
# SAFE TELEGRAM CALL HELPERS
# ======================

async def safe_reply(msg, text, **kwargs):
    """Reply to a message with protections for timeouts/network errors and concurrency."""
    if msg is None:
        return None
    try:
        async with API_LIMIT:
            result = await msg.reply_text(text, **kwargs)
            # small delay to avoid burst sends
            await asyncio.sleep(SAFE_SLEEP)
            return result
    except (TimedOut, NetworkError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.HTTPError):
        return None
    except Exception:
        return None

async def safe_send_message(bot, chat_id, text, **kwargs):
    try:
        async with API_LIMIT:
            result = await bot.send_message(chat_id=chat_id, text=text, **kwargs)
            await asyncio.sleep(SAFE_SLEEP)
            return result
    except (TimedOut, NetworkError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.HTTPError):
        return None
    except Exception:
        return None

async def safe_copy_message(bot, chat_id, from_chat_id, message_id, **kwargs):
    try:
        async with API_LIMIT:
            result = await bot.copy_message(chat_id=chat_id, from_chat_id=from_chat_id, message_id=message_id, **kwargs)
            await asyncio.sleep(SAFE_SLEEP)
            return result
    except (TimedOut, NetworkError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.HTTPError):
        return None
    except Exception:
        return None

async def safe_ban(bot, chat_id, user_id, until_date=None):
    try:
        async with API_LIMIT:
            await bot.ban_chat_member(chat_id=chat_id, user_id=user_id, until_date=until_date)
            await asyncio.sleep(SAFE_SLEEP)
    except (TimedOut, NetworkError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.HTTPError):
        return None
    except Exception:
        return None

async def safe_unban(bot, chat_id, user_id):
    try:
        async with API_LIMIT:
            await bot.unban_chat_member(chat_id=chat_id, user_id=user_id)
            await asyncio.sleep(SAFE_SLEEP)
    except (TimedOut, NetworkError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.HTTPError):
        return None
    except Exception:
        return None

# ======================
# HELPERS: ADMIN CHECK (DARI GRUP)
# ======================

async def is_group_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False

# ======================
# MENFESS HANDLER
# ======================

async def menfess_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg is None:
        return
    user = msg.from_user
    text = msg.text or msg.caption or ""

    if "#pria" not in text and "#wanita" not in text:
        await safe_reply(msg, "⚠️ Wajib sertakan #pria atau #wanita")
        return

    limit_type = "media" if msg.photo or msg.video else "text"
    max_limit = 10 if limit_type == "media" else 5

    if not check_limit(user.id, limit_type, max_limit):
        await safe_reply(msg, "⛔ Limit harian tercapai")
        return

    for target in (TARGET_CHANNEL_ID, GROUP_PUBLIK):
        await safe_copy_message(context.bot, chat_id=target, from_chat_id=msg.chat_id, message_id=msg.message_id)

    # log (use safe send)
    await safe_send_message(
        context.bot,
        CHANNEL_LOG_ID,
        f"MENFESS\nNama: {user.full_name}\nUsername: @{user.username}\nID: {user.id}\nIsi: {text[:200]}"
    )

    await safe_reply(msg, "✅ Menfess berhasil dikirim")

# ======================
# DOWNLOAD HANDLER
# ======================

async def download_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return
    user = update.message.from_user

    if not context.args:
        await safe_reply(update.message, "Gunakan: /dl <link>")
        return

    if not check_limit(user.id, "download", 2):
        await safe_reply(update.message, "⛔ Limit download harian habis")
        return

    url = context.args[0]

    ydl_opts = {
        "format": "best[height<=720]/best",
        "outtmpl": "media.%(ext)s",
        "quiet": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        for file in os.listdir():
            if file.startswith("media."):
                try:
                    with open(file, "rb") as f:
                        async with API_LIMIT:
                            # try as video, fallback to document
                            try:
                                await update.message.reply_video(f)
                            except Exception:
                                f.seek(0)
                                try:
                                    await update.message.reply_document(f)
                                except Exception:
                                    await safe_reply(update.message, "❌ Gagal mengirim file hasil download")
                            await asyncio.sleep(SAFE_SLEEP)
                except Exception:
                    await safe_reply(update.message, "❌ Gagal mengirim file hasil download")
                finally:
                    try:
                        os.remove(file)
                    except Exception:
                        pass
                break

    except Exception:
        await safe_reply(update.message, "❌ Gagal download")

# ======================
# ANTI LINK GRUP
# ======================

async def antispam_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg is None:
        return
    user = msg.from_user

    add_chat_stat(user.id)

    # owner always allowed
    if user.id == OWNER_ID:
        return

    # check if user is group admin
    is_admin = False
    try:
        is_admin = await is_group_admin(context, msg.chat.id, user.id)
    except Exception:
        is_admin = False

    if is_admin:
        return

    if is_link(msg.text or ""):
        try:
            await msg.delete()
        except Exception:
            pass

        until = datetime.utcnow() + timedelta(hours=1)
        await safe_ban(context.bot, chat_id=msg.chat_id, user_id=user.id, until_date=until)

# ======================
# WELCOME HANDLER
# ======================

async def welcome_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return
    for member in update.message.new_chat_members:
        cur.execute("SELECT user_id FROM welcome WHERE user_id=?", (member.id,))
        if not cur.fetchone():
            cur.execute("INSERT INTO welcome VALUES (?)", (member.id,))
            conn.commit()
            await safe_reply(update.message, f"👋 Selamat datang {member.full_name}\nSilakan baca rules.")

# ======================
# ADMIN COMMAND: BAN & KICK
# ======================

async def ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return
    user = update.message.from_user
    chat = update.message.chat

    if chat.type == "private":
        await safe_reply(update.message, "❌ Perintah ini hanya bisa di grup")
        return

    # OWNER selalu boleh
    if user.id != OWNER_ID:
        is_admin = await is_group_admin(context, chat.id, user.id)
        if not is_admin:
            await safe_reply(update.message, "⛔ Kamu bukan admin grup")
            return

    if not context.args:
        await safe_reply(update.message, "Gunakan: /ban <user_id> [jam]")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await safe_reply(update.message, "ID tidak valid")
        return

    hours = int(context.args[1]) if len(context.args) > 1 else 1
    until = datetime.utcnow() + timedelta(hours=hours)

    await safe_ban(context.bot, chat_id=chat.id, user_id=target_id, until_date=until)
    await safe_reply(update.message, f"✅ User diban {hours} jam")

async def kick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return
    user = update.message.from_user
    chat = update.message.chat

    if chat.type == "private":
        await safe_reply(update.message, "❌ Perintah ini hanya bisa di grup")
        return

    # OWNER selalu boleh
    if user.id != OWNER_ID:
        is_admin = await is_group_admin(context, chat.id, user.id)
        if not is_admin:
            await safe_reply(update.message, "⛔ Kamu bukan admin grup")
            return

    if not context.args:
        await safe_reply(update.message, "Gunakan: /kick <user_id>")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await safe_reply(update.message, "ID tidak valid")
        return

    # ban then unban to simulate kick
    await safe_ban(context.bot, chat_id=chat.id, user_id=target_id)
    await safe_unban(context.bot, chat_id=chat.id, user_id=target_id)
    await safe_reply(update.message, "✅ User dikick dari grup")

# ======================
# LEADERBOARD
# ======================

async def topchat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return
    today = str(date.today())
    cur.execute(
        "SELECT user_id, count FROM chat_stats WHERE date=? ORDER BY count DESC LIMIT 10",
        (today,),
    )
    rows = cur.fetchall()

    text = "🏆 TOP CHAT HARI INI\n\n"
    for i, (uid, cnt) in enumerate(rows, 1):
        text += f"{i}. ID {uid} → {cnt} pesan\n"

    await safe_reply(update.message, text)

# ======================
# MAIN (with HTTPXRequest timeouts)
# ======================

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN tidak diset di environment variables")

    # Important: use HTTPXRequest with longer timeouts on Railway (cold starts / unstable network)
    request = HTTPXRequest(
        connect_timeout=20.0,
        read_timeout=20.0,
        write_timeout=20.0,
        pool_timeout=20.0,
    )

    app = Application.builder().token(BOT_TOKEN).request(request).build()

    app.add_handler(CommandHandler("dl", download_handler))
    app.add_handler(CommandHandler("ban", ban))
    app.add_handler(CommandHandler("kick", kick))
    app.add_handler(CommandHandler("topchat", topchat))

    app.add_handler(MessageHandler(filters.ChatType.PRIVATE, menfess_handler))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_handler))
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.GROUPS, antispam_handler))

    print("🤖 Bot berjalan...")
    app.run_polling()

if __name__ == "__main__":
    main()
