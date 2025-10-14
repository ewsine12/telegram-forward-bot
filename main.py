
#!/usr/bin/env python3
# full_forward_bot.py
# Full-feature Telegram Forward Bot (all requested features)
# - Multiple admins
# - Auto-register chats when bot added
# - Auto-remove when bot removed
# - Channel->Groups forwarding (channel posts forwarded only to groups)
# - Admin-only SMS/broadcasting
# - DB persistence (SQLite): admins, chats, messages, deliveries, left_chats
# - Reports (per-date message counts, failed deliveries, groups added per date)
# - Group name/id list (admins only)
# - Keep-alive small web server for Replit/Koyeb
#
# Requirements:
#   pip install python-telegram-bot==20.5 aiohttp Flask
#
# USAGE: python full_forward_bot.py
# (This file currently contains the BOT_TOKEN and MAIN_ADMIN_ID hard-coded
#  because you provided them. You can replace with os.getenv(...) if desired.)

import os
import sqlite3
import asyncio
import logging
from datetime import datetime, date
from typing import Optional, Tuple, List

from telegram import Update, ChatMember, Message, Chat
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    ChatMemberHandler,
    filters,
)

# ---------------- CONFIG ----------------
# You provided these values — keep them here or replace with os.getenv("BOT_TOKEN")
BOT_TOKEN = "8414051726:AAF6vcdJu2KSs67VBlZqj1F7QeNoPFzCMPc"
MAIN_ADMIN_ID = 7149740820

DB_PATH = os.getenv("DB_PATH", "full_forward_bot.db")
SEND_DELAY = float(os.getenv("SEND_DELAY", "0.6"))  # seconds between sends
CHECK_ADMIN_BEFORE_SEND = False  # set to True to require target group has admins before sending
# ----------------------------------------

if not BOT_TOKEN or not MAIN_ADMIN_ID:
    raise SystemExit("Set BOT_TOKEN and MAIN_ADMIN_ID in the script or environment variables.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


# ----------------- Database -----------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    # admins table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            added_at TEXT
        )
    """)
    # chats table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            chat_id TEXT PRIMARY KEY,
            type TEXT,
            title TEXT,
            username TEXT,
            added_by INTEGER,
            added_at TEXT
        )
    """)
    # messages table (each broadcast action)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            msg_date TEXT,
            from_user INTEGER,
            from_chat_id TEXT,
            message_id INTEGER,
            content_type TEXT,
            text_preview TEXT,
            total_target INTEGER,
            total_sent INTEGER,
            total_failed INTEGER
        )
    """)
    # deliveries: per-message per-target record
    cur.execute("""
        CREATE TABLE IF NOT EXISTS deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_row_id INTEGER,
            target_chat_id TEXT,
            status TEXT,  -- 'sent' / 'failed' / 'skipped'
            error TEXT
        )
    """)
    # left chats (where bot removed)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS left_chats (
            chat_id TEXT PRIMARY KEY,
            title TEXT,
            removed_at TEXT
        )
    """)
    # ensure main admin exists
    cur.execute("INSERT OR IGNORE INTO admins (user_id, added_at) VALUES (?, ?)", (MAIN_ADMIN_ID, now_iso()))
    conn.commit()
    conn.close()


def now_iso():
    return datetime.utcnow().isoformat()


# Admin functions
def add_admin_db(user_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO admins (user_id, added_at) VALUES (?, ?)", (int(user_id), now_iso()))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def remove_admin_db(user_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM admins WHERE user_id = ?", (int(user_id),))
    changed = cur.rowcount
    conn.commit()
    conn.close()
    return changed > 0


def list_admins_db() -> List[int]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM admins")
    rows = [r[0] for r in cur.fetchall()]
    conn.close()
    return rows


def is_admin(user_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM admins WHERE user_id = ?", (int(user_id),))
    ok = cur.fetchone() is not None
    conn.close()
    return ok


# Chat functions
def add_chat_db(chat_id: str, ctype: str, title: str = "", username: str = "", added_by: Optional[int] = None) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO chats (chat_id, type, title, username, added_by, added_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (str(chat_id), ctype, title or "", username or "", added_by, now_iso()))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def remove_chat_db(chat_id: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM chats WHERE chat_id = ?", (str(chat_id),))
    changed = cur.rowcount
    conn.commit()
    conn.close()
    return changed > 0


def list_chats_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT chat_id, type, title, username, added_by, added_at FROM chats ORDER BY added_at DESC")
    rows = cur.fetchall()
    conn.close()
    return rows


def log_left_chat(chat_id: str, title: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO left_chats (chat_id, title, removed_at) VALUES (?, ?, ?)",
                (str(chat_id), title or "", now_iso()))
    conn.commit()
    conn.close()


# Message & delivery logging
def create_message_row(msg: Message) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    content_type = detect_content_type(msg)
    preview = (msg.text or (msg.caption if hasattr(msg, "caption") else ""))[:300]
    cur.execute("""
        INSERT INTO messages (msg_date, from_user, from_chat_id, message_id, content_type, text_preview, total_target, total_sent, total_failed)
        VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0)
    """, (now_iso(), msg.from_user.id if msg.from_user else None, str(msg.chat_id), msg.message_id, content_type, preview))
    row_id = cur.lastrowid
    conn.commit()
    conn.close()
    return row_id


def update_message_counts(row_id: int, target_total: int, sent: int, failed: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        UPDATE messages
        SET total_target=?, total_sent=?, total_failed=?
        WHERE id=?
    """, (target_total, sent, failed, row_id))
    conn.commit()
    conn.close()


def add_delivery_record(message_row_id: int, target_chat_id: str, status: str, error: Optional[str] = None):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO deliveries (message_row_id, target_chat_id, status, error)
        VALUES (?, ?, ?, ?)
    """, (message_row_id, str(target_chat_id), status, error or ""))
    conn.commit()
    conn.close()


# ---------------- Utility ----------------
def detect_content_type(msg: Message) -> str:
    if msg.text:
        return "text"
    if msg.photo:
        return "photo"
    if msg.video:
        return "video"
    if msg.document:
        return "document"
    if msg.audio:
        return "audio"
    if msg.voice:
        return "voice"
    if msg.sticker:
        return "sticker"
    return "other"


async def safe_copy(bot, from_chat_id, message_id, to_chat_id):
    try:
        await bot.copy_message(chat_id=int(to_chat_id), from_chat_id=int(from_chat_id), message_id=int(message_id))
        return True, None
    except Exception as e:
        return False, str(e)


async def check_group_has_admins(bot, chat_id) -> bool:
    try:
        admins = await bot.get_chat_administrators(chat_id=int(chat_id))
        return len(admins) > 0
    except Exception:
        return False


# ----------------- Broadcast Logic -----------------
async def broadcast_message_to_all(msg: Message, context: ContextTypes.DEFAULT_TYPE):
    """
    Core broadcast:
    - create message row
    - iterate all registered chats
    - optionally check admins presence
    - copy_message to each group
    - log deliveries and update message counts
    """
    # create DB row
    row_id = create_message_row(msg)
    targets = list_chats_db()
    target_ids = [r[0] for r in targets if r[1] in ("group", "supergroup")]  # only groups
    total = len(target_ids)
    sent = 0
    failed = 0

    for tid in target_ids:
        # optionally skip if no admins in group
        if CHECK_ADMIN_BEFORE_SEND:
            ok_admins = await check_group_has_admins(context.bot, tid)
            if not ok_admins:
                add_delivery_record(row_id, tid, "skipped", "no_admins")
                failed += 1
                await asyncio.sleep(SEND_DELAY)
                continue

        ok, err = await safe_copy(context.bot, msg.chat_id, msg.message_id, tid)
        if ok:
            add_delivery_record(row_id, tid, "sent", None)
            sent += 1
        else:
            add_delivery_record(row_id, tid, "failed", str(err))
            failed += 1
        await asyncio.sleep(SEND_DELAY)

    update_message_counts(row_id, total, sent, failed)
    return {"row_id": row_id, "total": total, "sent": sent, "failed": failed}


# ----------------- Handlers / Commands -----------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Full Forward Bot is running.\n"
        "Admins can use:\n"
        "/addadmin <id>\n/removeadmin <id>\n/listadmins\n"
        "/groups\n/details\n/status\n/report <YYYY-MM-DD>\n/broadcast <text>\n\n"
        "Add the bot to groups or channels and it will auto-register. Channel posts will forward to groups only."
    )


# Admin management
async def addadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caller = update.effective_user
    if caller is None or not is_admin(caller.id):
        return await update.message.reply_text("❌ Permission denied.")
    if not context.args:
        return await update.message.reply_text("Usage: /addadmin <telegram_user_id>")
    try:
        uid = int(context.args[0])
    except ValueError:
        return await update.message.reply_text("user_id must be number.")
    ok = add_admin_db(uid)
    if ok:
        await update.message.reply_text(f"✅ Admin {uid} added.")
    else:
        await update.message.reply_text("Already an admin.")


async def removeadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caller = update.effective_user
    if caller is None or not is_admin(caller.id):
        return await update.message.reply_text("❌ Permission denied.")
    if not context.args:
        return await update.message.reply_text("Usage: /removeadmin <telegram_user_id>")
    try:
        uid = int(context.args[0])
    except ValueError:
        return await update.message.reply_text("user_id must be number.")
    ok = remove_admin_db(uid)
    if ok:
        await update.message.reply_text(f"🗑️ Admin {uid} removed.")
    else:
        await update.message.reply_text("Admin not found.")


async def listadmins_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caller = update.effective_user
    if caller is None or not is_admin(caller.id):
        return await update.message.reply_text("❌ Permission denied.")
    admins = list_admins_db()
    await update.message.reply_text("Admins:\n" + "\n".join(str(a) for a in admins))


# Groups / chats
async def groups_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caller = update.effective_user
    if caller is None or not is_admin(caller.id):
        return await update.message.reply_text("❌ Permission denied.")
    rows = list_chats_db()
    if not rows:
        return await update.message.reply_text("No chats registered.")
    lines = []
    for chat_id, ctype, title, username, added_by, added_at in rows:
        label = title or chat_id
        if username:
            label += f" (@{username})"
        lines.append(f"- [{ctype}] {label} — {chat_id} — added: {added_at}")
    await update.message.reply_text("Registered chats:\n" + "\n".join(lines))


async def details_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await groups_cmd(update, context)


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caller = update.effective_user
    if caller is None or not is_admin(caller.id):
        return await update.message.reply_text("❌ Permission denied.")
    total_chats = len(list_chats_db())
    admins = list_admins_db()
    await update.message.reply_text(
        f"Status:\nAdmins: {len(admins)}\nRegistered chats: {total_chats}\nSEND_DELAY: {SEND_DELAY}s\nCHECK_ADMIN_BEFORE_SEND: {CHECK_ADMIN_BEFORE_SEND}"
    )


# reports
def query_messages_by_date(query_date: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    start_iso = f"{query_date}T00:00:00"
    end_iso = f"{query_date}T23:59:59"
    cur.execute("""
        SELECT id, msg_date, content_type, text_preview, total_target, total_sent, total_failed
        FROM messages
        WHERE msg_date BETWEEN ? AND ?
        ORDER BY id DESC
    """, (start_iso, end_iso))
    rows = cur.fetchall()
    conn.close()
    return rows


async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caller = update.effective_user
    if caller is None or not is_admin(caller.id):
        return await update.message.reply_text("❌ Permission denied.")
    if not context.args:
        return await update.message.reply_text("Usage: /report YYYY-MM-DD")
    qdate = context.args[0]
    try:
        # basic validation
        _ = datetime.strptime(qdate, "%Y-%m-%d")
    except Exception:
        return await update.message.reply_text("Invalid date format. Use YYYY-MM-DD")
    rows = query_messages_by_date(qdate)
    if not rows:
        return await update.message.reply_text("No messages on that date.")
    lines = []
    for r in rows:
        lines.append(f"ID:{r[0]} {r[1][:19]} {r[2]} sent:{r[5]} failed:{r[6]} targets:{r[4]}\nPreview: {r[3]}")
    await update.message.reply_text("\n\n".join(lines))


# manual broadcast (text) - admin only
async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caller = update.effective_user
    if caller is None or not is_admin(caller.id):
        return await update.message.reply_text("❌ Permission denied.")
    if not context.args:
        return await update.message.reply_text("Usage: /broadcast <text>")
    text = " ".join(context.args)
    # create a fake Message-like object? easiest: send a text to ourselves and grab it -> but simpler: create minimal object
    # We'll send using bot.send_message to each group (not copy_message)
    rows = list_chats_db()
    group_ids = [r[0] for r in rows if r[1] in ("group", "supergroup")]
    total = len(group_ids)
    sent = 0
    failed = 0
    for gid in group_ids:
        try:
            await context.bot.send_message(chat_id=int(gid), text=text)
            sent += 1
        except Exception as e:
            failed += 1
            logger.warning("Broadcast failed to %s: %s", gid, e)
        await asyncio.sleep(SEND_DELAY)
    await update.message.reply_text(f"Broadcast done — sent: {sent}, failed: {failed}")


# Private messages by admin -> broadcast (copy original message where possible)
async def private_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None or not is_admin(user.id):
        return
    msg = update.message
    if not msg:
        return
    summary = await broadcast_message_to_all(msg, context)
    await update.message.reply_text(f"Broadcast completed. sent: {summary['sent']}, failed: {summary['failed']}")


# Handle channel posts (forward channel posts -> groups only)
async def channel_post_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # channel_post is available as update.channel_post
    msg = update.channel_post
    if not msg:
        return
    # automatically broadcast channel posts to groups only
    await broadcast_message_to_all(msg, context)


# Chat member updates for my_chat_member to auto-register/remove
def extract_status_change(old: ChatMember, new: ChatMember) -> Optional[Tuple[str, str]]:
    try:
        old_status = old.status
        new_status = new.status
        if old_status == new_status:
            return None
        return old_status, new_status
    except Exception:
        return None


async def my_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not chat:
        return
    old = update.my_chat_member.old_chat_member
    new = update.my_chat_member.new_chat_member
    status_change = extract_status_change(old, new)
    if not status_change:
        return
    old_status, new_status = status_change
    cid = str(chat.id)
    ctype = chat.type
    title = chat.title or ""
    username = getattr(chat, "username", "") or ""
    # became member/admin -> register
    if new_status in ("member", "administrator", "creator"):
        added = add_chat_db(cid, ctype, title, username, None)
        if added:
            logger.info("Registered chat %s (%s)", title or cid, ctype)
    elif new_status in ("left", "kicked", "banned"):
        # removed -> delete and log left
        removed = remove_chat_db(cid)
        if removed:
            log_left_chat(cid, title)
            logger.info("Removed chat %s because bot left/kicked", cid)


# Helper commands for delivery/failure report per message id
async def deliveries_for_message_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caller = update.effective_user
    if caller is None or not is_admin(caller.id):
        return await update.message.reply_text("❌ Permission denied.")
    if not context.args:
        return await update.message.reply_text("Usage: /deliveries <message_row_id>")
    try:
        mid = int(context.args[0])
    except ValueError:
        return await update.message.reply_text("invalid id")
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT target_chat_id, status, error FROM deliveries WHERE message_row_id = ?", (mid,))
    rows = cur.fetchall()
    conn.close()
    if not rows:
        return await update.message.reply_text("No deliveries found for that id.")
    ok = sum(1 for r in rows if r[1] == "sent")
    failed = sum(1 for r in rows if r[1] == "failed")
    skipped = sum(1 for r in rows if r[1] == "skipped")
    text = f"Deliveries for {mid}: sent={ok}, failed={failed}, skipped={skipped}\n\n"
    sample = "\n".join([f"{r[0]} — {r[1]} — {r[2][:150]}" for r in rows[:200]])
    await update.message.reply_text(text + sample)


# ----------------- Main -----------------
def main():
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # commands
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("addadmin", addadmin_cmd))
    app.add_handler(CommandHandler("removeadmin", removeadmin_cmd))
    app.add_handler(CommandHandler("listadmins", listadmins_cmd))
    app.add_handler(CommandHandler("groups", groups_cmd))
    app.add_handler(CommandHandler("details", details_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("report", report_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("deliveries", deliveries_for_message_cmd))

    # chat member updates for auto-register
    app.add_handler(ChatMemberHandler(my_chat_member_update, chat_member_types=ChatMemberHandler.MY_CHAT_MEMBER))

    # channel posts
    app.add_handler(MessageHandler(filters.CHAT_TYPE_CHANNEL & ~filters.COMMAND, channel_post_handler))

    # private messages from admins -> broadcast
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (~filters.COMMAND), private_message_handler))

    logger.info("Bot started — polling for updates...")
    app.run_polling(allowed_updates=[
        "message",
        "edited_message",
        "channel_post",
        "my_chat_member",
        "chat_member",
    ])


# --------------- KEEP-ALIVE (Flask) ---------------
from flask import Flask
from threading import Thread

web_app = Flask("")

@web_app.route("/")
def home():
    return "Full Forward Bot is alive!"

def run_web():
    web_app.run(host="0.0.0.0", port=8080)

def keep_alive():
    t = Thread(target=run_web)
    t.daemon = True
    t.start()


# ---------------- START ----------------
if __name__ == "__main__":
    keep_alive()
    main()
