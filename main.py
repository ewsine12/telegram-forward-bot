import asyncio
from pyrogram import Client, filters
from pyrogram.errors import FloodWait
import sqlite3
import datetime
import os

# Load env variables
BOT_TOKEN = os.getenv("8414051726:AAF6vcdJu2KSs67VBlZqj1F7QeNoPFzCMPc")
ADMIN_ID = int(os.getenv("ADMIN_ID", "7149740820"))

# Database setup
conn = sqlite3.connect("database.sqlite", check_same_thread=False)
c = conn.cursor()
c.execute("""CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY)""")
c.execute("""CREATE TABLE IF NOT EXISTS groups (group_id INTEGER PRIMARY KEY, title TEXT, date_added TEXT)""")
c.execute("""CREATE TABLE IF NOT EXISTS logs (date TEXT, sent INTEGER, failed INTEGER, joined INTEGER, left INTEGER)""")
conn.commit()

app = Client("forward_bot", bot_token=BOT_TOKEN)

# Ensure main admin exists
c.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (ADMIN_ID,))
conn.commit()

# ---------- Helper Functions ----------
def is_admin(user_id):
    c.execute("SELECT user_id FROM admins WHERE user_id=?", (user_id,))
    return c.fetchone() is not None

def log_event(sent=0, failed=0, joined=0, left=0):
    date = str(datetime.date.today())
    c.execute("SELECT * FROM logs WHERE date=?", (date,))
    row = c.fetchone()
    if row:
        c.execute("UPDATE logs SET sent=sent+?, failed=failed+?, joined=joined+?, left=left+? WHERE date=?",
                  (sent, failed, joined, left, date))
    else:
        c.execute("INSERT INTO logs (date, sent, failed, joined, left) VALUES (?, ?, ?, ?, ?)",
                  (date, sent, failed, joined, left))
    conn.commit()

async def send_to_all_groups(app, text):
    c.execute("SELECT group_id FROM groups")
    groups = c.fetchall()
    sent, failed = 0, 0
    for g in groups:
        try:
            await app.send_message(g[0], text)
            sent += 1
            await asyncio.sleep(0.3)
        except FloodWait as e:
            await asyncio.sleep(e.value)
        except Exception:
            failed += 1
    log_event(sent=sent, failed=failed)
    return sent, failed

# ---------- Handlers ----------

@app.on_message(filters.command("start") & filters.private)
async def start_cmd(client, message):
    if is_admin(message.from_user.id):
        await message.reply_text("✅ Bot is running.\nUse /help for commands.")
    else:
        await message.reply_text("⛔ You are not authorized to use this bot.")

@app.on_message(filters.command("help") & filters.private)
async def help_cmd(client, message):
    if not is_admin(message.from_user.id):
        return
    await message.reply_text("""
🧩 **Admin Commands**
/addadmin [id] - Add new admin
/removeadmin [id] - Remove admin
/admins - List admins
/groups - List groups
/status - Daily report
/sms [text] - Send message to all groups
/report - Detailed stats
""")

@app.on_message(filters.command("addadmin") & filters.private)
async def add_admin(client, message):
    if not is_admin(message.from_user.id):
        return
    try:
        new_id = int(message.text.split()[1])
        c.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (new_id,))
        conn.commit()
        await message.reply_text(f"✅ Admin {new_id} added.")
    except:
        await message.reply_text("❌ Usage: /addadmin [user_id]")

@app.on_message(filters.command("removeadmin") & filters.private)
async def remove_admin(client, message):
    if not is_admin(message.from_user.id):
        return
    try:
        rem_id = int(message.text.split()[1])
        c.execute("DELETE FROM admins WHERE user_id=?", (rem_id,))
        conn.commit()
        await message.reply_text(f"✅ Admin {rem_id} removed.")
    except:
        await message.reply_text("❌ Usage: /removeadmin [user_id]")

@app.on_message(filters.command("admins") & filters.private)
async def list_admins(client, message):
    if not is_admin(message.from_user.id):
        return
    c.execute("SELECT user_id FROM admins")
    admins = "\n".join([str(x[0]) for x in c.fetchall()])
    await message.reply_text(f"👑 **Admins:**\n{admins}")

@app.on_message(filters.command("groups") & filters.private)
async def list_groups(client, message):
    if not is_admin(message.from_user.id):
        return
    c.execute("SELECT group_id, title FROM groups")
    rows = c.fetchall()
    msg = "\n".join([f"{r[1]} ({r[0]})" for r in rows]) if rows else "No groups added yet."
    await message.reply_text(f"📋 **Groups:**\n{msg}")

@app.on_message(filters.command("sms") & filters.private)
async def send_sms(client, message):
    if not is_admin(message.from_user.id):
        return
    text = message.text.split(" ", 1)[1] if len(message.text.split()) > 1 else None
    if not text:
        await message.reply_text("❌ Usage: /sms [message]")
        return
    sent, failed = await send_to_all_groups(client, text)
    await message.reply_text(f"✅ Sent: {sent}\n❌ Failed: {failed}")

@app.on_message(filters.command("status") & filters.private)
async def status_cmd(client, message):
    if not is_admin(message.from_user.id):
        return
    c.execute("SELECT * FROM logs")
    data = c.fetchall()
    if not data:
        await message.reply_text("No activity logged yet.")
        return
    lines = [f"{d[0]} ➤ Sent: {d[1]}, Failed: {d[2]}, Joined: {d[3]}, Left: {d[4]}" for d in data]
    await message.reply_text("\n".join(lines))

@app.on_message(filters.command("report") & filters.private)
async def report_cmd(client, message):
    if not is_admin(message.from_user.id):
        return
    c.execute("SELECT COUNT(*) FROM groups")
    total_groups = c.fetchone()[0]
    await message.reply_text(f"📊 Total Groups: {total_groups}")

@app.on_message(filters.new_chat_members)
async def joined_group(client, message):
    for user in message.new_chat_members:
        if user.is_self:
            gid = message.chat.id
            title = message.chat.title or "Unnamed"
            c.execute("INSERT OR IGNORE INTO groups (group_id, title, date_added) VALUES (?, ?, ?)",
                      (gid, title, str(datetime.date.today())))
            conn.commit()
            log_event(joined=1)
            await message.reply_text("🤖 Bot connected successfully!")

@app.on_message(filters.left_chat_member)
async def left_group(client, message):
    if message.left_chat_member and message.left_chat_member.is_self:
        gid = message.chat.id
        c.execute("DELETE FROM groups WHERE group_id=?", (gid,))
        conn.commit()
        log_event(left=1)

print("🤖 Bot is starting...")
app.run()
