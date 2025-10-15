#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bot.py — Full Forward Bot (Bangla commands)
Features implemented (1..16 from your list):
- Multi-admin add/remove
- Unlimited groups register (DB)
- Auto-register when bot added to group
- Channel -> Groups forwarding (only sends to groups)
- Only admins can send broadcasts (.বার্তা or private message)
- Admin-only add-admin
- Group list viewable by admins
- Admin list viewable by admins
- Date-wise groups added logging
- Date-wise messages sent logging
- Group list with names
- Bot status command
- Track which groups failed to receive messages
- Track groups where bot was removed
- Persistent data saved in SQLite (bot_data.db) + data.json
- Channel automatic SMS forward + logging
"""

import os
import sqlite3
import asyncio
import logging
import json
from datetime import datetime
from typing import Optional, List
from threading import Thread

from telegram import Update, ChatMember, Message
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    ChatMemberHandler,
    filters,
)

# ---------------- CONFIG ----------------
# Token and main admin (you gave these; kept here for convenience)
BOT_TOKEN = "8414051726:AAF6vcdJu2KSs67VBlZqj1F7QeNoPFzCMPc"
MAIN_ADMIN_ID = 7149740820

DB_PATH = os.getenv("DB_PATH", "bot_data.db")
JSON_PATH = os.getenv("JSON_PATH", "data.json")
SEND_DELAY = float(os.getenv("SEND_DELAY", "0.6"))  # seconds between sends to avoid rate limits
CHECK_ADMIN_BEFORE_SEND = os.getenv("CHECK_ADMIN_BEFORE_SEND", "False").lower() in ("1", "true", "yes")

if not BOT_TOKEN or MAIN_ADMIN_ID == 0:
    raise SystemExit("Please set BOT_TOKEN and MAIN_ADMIN_ID before running.")

# ---------------- Logging ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ---------------- Helpers ----------------
def now_iso():
    return datetime.utcnow().isoformat()

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY, added_at TEXT)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS chats (
        chat_id TEXT PRIMARY KEY, type TEXT, title TEXT, username TEXT, added_by INTEGER, added_at TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, msg_date TEXT, from_user INTEGER, from_chat_id TEXT,
        message_id INTEGER, content_type TEXT, text_preview TEXT, total_target INTEGER, total_sent INTEGER, total_failed INTEGER
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS deliveries (
        id INTEGER PRIMARY KEY AUTOINCREMENT, message_row_id INTEGER, target_chat_id TEXT, status TEXT, error TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS left_chats (
        chat_id TEXT PRIMARY KEY, title TEXT, removed_at TEXT
    )""")
    cur.execute("INSERT OR IGNORE INTO admins (user_id, added_at) VALUES (?, ?)", (MAIN_ADMIN_ID, now_iso()))
    conn.commit()
    conn.close()

# ---------------- JSON persistence for date-based logs ----------------
def ensure_json():
    if not os.path.exists(JSON_PATH):
        data = {"groups_added_by_date": {}, "messages_by_date": {}, "admins": [], "left_chats": []}
        with open(JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

def json_add_group(chat_id, title):
    ensure_json()
    with open(JSON_PATH, "r", encoding="utf-8") as f:
        d = json.load(f)
    today = datetime.utcnow().strftime("%Y-%m-%d")
    d.setdefault("groups_added_by_date", {})
    d["groups_added_by_date"].setdefault(today, [])
    d["groups_added_by_date"][today].append({"id": chat_id, "title": title})
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)

def json_add_message(sent, failed):
    ensure_json()
    with open(JSON_PATH, "r", encoding="utf-8") as f:
        d = json.load(f)
    today = datetime.utcnow().strftime("%Y-%m-%d")
    d.setdefault("messages_by_date", {})
    d["messages_by_date"].setdefault(today, {"sent": 0, "failed": 0})
    d["messages_by_date"][today]["sent"] += sent
    d["messages_by_date"][today]["failed"] += failed
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)

def json_log_left(chat_id, title):
    ensure_json()
    with open(JSON_PATH, "r", encoding="utf-8") as f:
        d = json.load(f)
    d.setdefault("left_chats", [])
    d["left_chats"].append({"id": chat_id, "title": title, "removed_at": now_iso()})
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)

def json_add_admin(uid):
    ensure_json()
    with open(JSON_PATH, "r", encoding="utf-8") as f:
        d = json.load(f)
    d.setdefault("admins", [])
    if uid not in d["admins"]:
        d["admins"].append(uid)
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)

def json_remove_admin(uid):
    ensure_json()
    with open(JSON_PATH, "r", encoding="utf-8") as f:
        d = json.load(f)
    d.setdefault("admins", [])
    if uid in d["admins"]:
        d["admins"].remove(uid)
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)

# ---------------- DB helpers ----------------
def add_admin_db(user_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO admins (user_id, added_at) VALUES (?, ?)", (int(user_id), now_iso()))
        conn.commit()
        added = True
    except sqlite3.IntegrityError:
        added = False
    conn.close()
    if added:
        json_add_admin(user_id)
    return added

def remove_admin_db(user_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM admins WHERE user_id = ?", (int(user_id),))
    changed = cur.rowcount
    conn.commit()
    conn.close()
    if changed:
        json_remove_admin(user_id)
    return changed > 0

def list_admins_db() -> List[int]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM admins ORDER BY added_at DESC")
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

def add_chat_db(chat_id: str, ctype: str, title: str = "", username: str = "", added_by: Optional[int] = None) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO chats (chat_id, type, title, username, added_by, added_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (str(chat_id), ctype, title or "", username or "", added_by, now_iso()))
        conn.commit()
        added = True
    except sqlite3.IntegrityError:
        added = False
    conn.close()
    if added:
        json_add_group(chat_id, title)
    return added

def remove_chat_db(chat_id: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM chats WHERE chat_id = ?", (str(chat_id),))
    changed = cur.rowcount
    conn.commit()
    conn.close()
    if changed:
        json_log_left(chat_id, "")
    return changed > 0

def list_chats_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT chat_id, type, title, username, added_by, added_at FROM chats ORDER BY added_at DESC")
    rows = cur.fetchall()
    conn.close()
    return rows

# ---------------- Message logging helpers ----------------
def create_message_row(msg: Message) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    content_type = detect_content_type(msg)
    preview = (msg.text or (getattr(msg, "caption", "") or ""))[:300]
    cur.execute("INSERT INTO messages (msg_date, from_user, from_chat_id, message_id, content_type, text_preview, total_target, total_sent, total_failed) VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0)",
                (now_iso(), msg.from_user.id if msg.from_user else None, str(msg.chat_id), msg.message_id, content_type, preview))
    row_id = cur.lastrowid
    conn.commit()
    conn.close()
    return row_id

def update_message_counts(row_id: int, target_total: int, sent: int, failed: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE messages SET total_target=?, total_sent=?, total_failed=? WHERE id=?", (target_total, sent, failed, row_id))
    conn.commit()
    conn.close()

def add_delivery_record(message_row_id: int, target_chat_id: str, status: str, error: Optional[str] = None):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT INTO deliveries (message_row_id, target_chat_id, status, error) VALUES (?, ?, ?, ?)",
                (message_row_id, str(target_chat_id), status, error or ""))
    conn.commit()
    conn.close()

def log_left_chat(chat_id: str, title: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO left_chats (chat_id, title, removed_at) VALUES (?, ?, ?)", (str(chat_id), title or "", now_iso()))
    conn.commit()
    conn.close()

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

# ---------------- Safe copy and checks ----------------
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

# ---------------- Broadcast logic ----------------
async def broadcast_message_to_all(msg: Message, context: ContextTypes.DEFAULT_TYPE):
    row_id = create_message_row(msg)
    rows = list_chats_db()
    group_ids = [r[0] for r in rows if r[1] in ("group", "supergroup")]
    total = len(group_ids)
    sent = 0
    failed = 0
    for gid in group_ids:
        if CHECK_ADMIN_BEFORE_SEND:
            ok_admins = await check_group_has_admins(context.bot, gid)
            if not ok_admins:
                add_delivery_record(row_id, gid, "skipped", "no_admins")
                failed += 1
                await asyncio.sleep(SEND_DELAY)
                continue
        ok, err = await safe_copy(context.bot, msg.chat_id, msg.message_id, gid)
        if ok:
            add_delivery_record(row_id, gid, "sent", "")
            sent += 1
        else:
            add_delivery_record(row_id, gid, "failed", str(err))
            failed += 1
        await asyncio.sleep(SEND_DELAY)
    update_message_counts(row_id, total, sent, failed)
    json_add_message(sent, failed)
    return {"row_id": row_id, "total": total, "sent": sent, "failed": failed}

# ---------------- Handlers / Commands (Bangla-friendly) ----------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 বট অন আছে — অ্যাডমিনরা /help বা /সহায়তা কমান্ড দেখতে পারেন।")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("⛔ আপনি অ্যাডমিন নন।")
    text = (
        "🛠️ কমান্ড (অ্যাডমিনদের জন্য):\n"
        "/start - বট স্ট্যাটাস\n"
        "/সহায়তা বা /help - কমান্ড তালিকা\n"
        "/addadmin <id> - নতুন অ্যাডমিন\n"
        "/removeadmin <id> - অ্যাডমিন রিমুভ\n"
        "/listadmins - অ্যাডমিন তালিকা\n"
        "/groups - গ্রুপ তালিকা\n"
        "/অবস্থা - বট স্ট্যাটাস\n"
        "/report <YYYY-MM-DD> - ওই দিনের রিপোর্ট\n"
        "/broadcast <text> - সব গ্রুপে পাঠাও\n"
        ".বার্তা <text> - (ব্যানলা শর্ট কমান্ড) সব গ্রুপে পাঠাও\n"
    )
    await update.message.reply_text(text)

async def addadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("⛔ অনুমতি নেই।")
    if not context.args:
        return await update.message.reply_text("ব্যবহার: /addadmin <telegram_user_id>")
    try:
        uid = int(context.args[0])
    except Exception:
        return await update.message.reply_text("user_id সংখ্যায় হতে হবে।")
    ok = add_admin_db(uid)
    if ok:
        await update.message.reply_text(f"✅ অ্যাডমিন যোগ করা হলো: {uid}")
    else:
        await update.message.reply_text("⚠️ আগেই আছে।")

async def removeadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("⛔ অনুমতি নেই।")
    if not context.args:
        return await update.message.reply_text("ব্যবহার: /removeadmin <telegram_user_id>")
    try:
        uid = int(context.args[0])
    except Exception:
        return await update.message.reply_text("user_id সংখ্যায় হতে হবে।")
    ok = remove_admin_db(uid)
    if ok:
        await update.message.reply_text(f"🗑️ অ্যাডমিন সরানো হলো: {uid}")
    else:
        await update.message.reply_text("অ্যাডমিন পাওয়া যায়নি।")

async def listadmins_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("⛔ অনুমতি নেই।")
    admins = list_admins_db()
    await update.message.reply_text("অ্যাডমিনস:\n" + "\n".join(str(a) for a in admins))

async def groups_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("⛔ অনুমতি নেই।")
    rows = list_chats_db()
    if not rows:
        return await update.message.reply_text("কোনো গ্রুপ রেজিস্টার নেই।")
    lines = []
    for chat_id, ctype, title, username, added_by, added_at in rows:
        label = title or chat_id
        if username:
            label += f" (@{username})"
        lines.append(f"- [{ctype}] {label} — {chat_id} — যোগ: {added_at[:10]}")
    await update.message.reply_text("📋 গ্রুপ তালিকা:\n" + "\n".join(lines))

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("⛔ অনুমতি নেই।")
    rows = list_chats_db()
    admins = list_admins_db()
    await update.message.reply_text(f"🔎 অবস্থা:\nঅ্যাডমিনস: {len(admins)}\nমোট গ্রুপ: {len(rows)}\nSEND_DELAY: {SEND_DELAY}s")

async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("⛔ অনুমতি নেই।")
    if not context.args:
        return await update.message.reply_text("ব্যবহার: /report YYYY-MM-DD")
    qdate = context.args[0]
    try:
        datetime.strptime(qdate, "%Y-%m-%d")
    except Exception:
        return await update.message.reply_text("তারিখ ফরম্যাট ভুল — YYYY-MM-DD")
    ensure_json()
    with open(JSON_PATH, "r", encoding="utf-8") as f:
        d = json.load(f)
    groups_added = d.get("groups_added_by_date", {}).get(qdate, [])
    messages = d.get("messages_by_date", {}).get(qdate, {"sent":0,"failed":0})
    text = f"রিপোর্ট {qdate}:\nগ্রুপ যোগ: {len(groups_added)}\nমেসেজ পাঠানো: {messages.get('sent',0)}\nফেইল: {messages.get('failed',0)}"
    await update.message.reply_text(text)

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("⛔ অনুমতি নেই।")
    if not context.args:
        return await update.message.reply_text("ব্যবহার: /broadcast <text>")
    text = " ".join(context.args)
    rows = list_chats_db()
    group_ids = [r[0] for r in rows if r[1] in ("group","supergroup")]
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
    json_add_message(sent, failed)
    await update.message.reply_text(f"ব্রডকাস্ট সম্পন্ন — পাঠানো: {sent}, ব্যর্থ: {failed}")

async def bangla_barta_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # handler for messages starting with .বার্তা
    user = update.effective_user
    if not user or not is_admin(user.id):
        return await update.message.reply_text("⛔ অনুমতি নেই।")
    text = update.message.text
    parts = text.split(" ",1)
    if len(parts)<2 or not parts[1].strip():
        return await update.message.reply_text("ব্যবহার: .বার্তা <মেসেজ>")
    summary = await broadcast_message_to_all(update.message, context)
    await update.message.reply_text(f"প্রেরণ: পাঠানো={summary['sent']} ব্যর্থ={summary['failed']}")

async def private_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # private messages from admin -> broadcast
    user = update.effective_user
    if not user or not is_admin(user.id):
        return
    summary = await broadcast_message_to_all(update.message, context)
    await update.message.reply_text(f"প্রেরণ: পাঠানো={summary['sent']} ব্যর্থ={summary['failed']}")

async def channel_post_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # channel posts forwarded only to registered groups
    msg = update.channel_post
    if not msg:
        return
    summary = await broadcast_message_to_all(msg, context)
    # optionally notify MAIN_ADMIN
    try:
        await context.bot.send_message(chat_id=MAIN_ADMIN_ID, text=f"Channel post forwarded: sent={summary['sent']} failed={summary['failed']}")
    except Exception:
        pass

def extract_status_change(old: ChatMember, new: ChatMember) -> Optional[tuple]:
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
    if new_status in ("member","administrator","creator"):
        added = add_chat_db(cid, ctype, title, username, None)
        if added:
            logger.info("Registered chat %s (%s)", title or cid, ctype)
            json_add_group(cid, title)
    elif new_status in ("left","kicked","banned"):
        removed = remove_chat_db(cid)
        if removed:
            log_left_chat(cid, title)
            json_log_left(cid, title)
            logger.info("Removed chat %s because bot left/kicked", cid)

async def deliveries_for_message_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("⛔ অনুমতি নেই।")
    if not context.args:
        return await update.message.reply_text("ব্যবহার: /deliveries <message_r    except sqlite3.IntegrityError:
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
    preview = (msg.text or (getattr(msg, "caption", "") or ""))[:300]
    cur.execute("""INSERT INTO messages 
                   (msg_date, from_user, from_chat_id, message_id, content_type, text_preview, total_target, total_sent, total_failed)
                   VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0)""",
                (now_iso(), (msg.from_user.id if msg.from_user else None), str(msg.chat_id), msg.message_id, content_type, preview))
    row_id = cur.lastrowid
    conn.commit()
    conn.close()
    return row_id

def update_message_counts(row_id: int, target_total: int, sent: int, failed: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE messages SET total_target=?, total_sent=?, total_failed=? WHERE id=?",
                (target_total, sent, failed, row_id))
    conn.commit()
    conn.close()

def add_delivery_record(message_row_id: int, target_chat_id: str, status: str, error: Optional[str] = None):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT INTO deliveries (message_row_id, target_chat_id, status, error) VALUES (?, ?, ?, ?)",
                (message_row_id, str(target_chat_id), status, error or ""))
    conn.commit()
    conn.close()

# Utilities
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

# Broadcast logic
async def broadcast_message_to_all(msg: Message, context: ContextTypes.DEFAULT_TYPE):
    row_id = create_message_row(msg)
    targets = list_chats_db()
    target_ids = [r[0] for r in targets if r[1] in ("group", "supergroup")]
    total = len(target_ids)
    sent = 0
    failed = 0
    for tid in target_ids:
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

# ---------------- Handlers / Commands ----------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Full Forward Bot running.\n"
        "Admins can use:\n"
        "/addadmin <id>\n/removeadmin <id>\n/listadmins\n/groups\n/status\n/report <YYYY-MM-DD>\n/broadcast <text>\n\n"
        "Add bot to groups/channels and it will auto-register. Channel posts forwarded to groups only."
    )

async def addadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caller = update.effective_user
    if not caller or not is_admin(caller.id):
        await update.message.reply_text("❌ Permission denied.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /addadmin <telegram_user_id>")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id must be number.")
        return
    ok = add_admin_db(uid)
    if ok:
        await update.message.reply_text(f"✅ Admin {uid} added.")
    else:
        await update.message.reply_text("Already an admin.")

async def removeadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caller = update.effective_user
    if not caller or not is_admin(caller.id):
        await update.message.reply_text("❌ Permission denied.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /removeadmin <telegram_user_id>")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id must be number.")
        return
    ok = remove_admin_db(uid)
    if ok:
        await update.message.reply_text(f"🗑️ Admin {uid} removed.")
    else:
        await update.message.reply_text("Admin not found.")

async def listadmins_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caller = update.effective_user
    if not caller or not is_admin(caller.id):
        await update.message.reply_text("❌ Permission denied.")
        return
    admins = list_admins_db()
    await update.message.reply_text("Admins:\n" + "\n".join(str(a) for a in admins))

async def groups_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caller = update.effective_user
    if not caller or not is_admin(caller.id):
        await update.message.reply_text("❌ Permission denied.")
        return
    rows = list_chats_db()
    if not rows:
        await update.message.reply_text("No chats registered.")
        return
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
    if not caller or not is_admin(caller.id):
        await update.message.reply_text("❌ Permission denied.")
        return
    total_chats = len(list_chats_db())
    admins = list_admins_db()
    await update.message.reply_text(
        f"Status:\nAdmins: {len(admins)}\nRegistered chats: {total_chats}\nSEND_DELAY: {SEND_DELAY}s\nCHECK_ADMIN_BEFORE_SEND: {CHECK_ADMIN_BEFORE_SEND}"
    )

def query_messages_by_date(query_date: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    start_iso = f"{query_date}T00:00:00"
    end_iso = f"{query_date}T23:59:59"
    cur.execute("""SELECT id, msg_date, content_type, text_preview, total_target, total_sent, total_failed
                   FROM messages WHERE msg_date BETWEEN ? AND ? ORDER BY id DESC""", (start_iso, end_iso))
    rows = cur.fetchall()
    conn.close()
    return rows

async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caller = update.effective_user
    if not caller or not is_admin(caller.id):
        await update.message.reply_text("❌ Permission denied.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /report YYYY-MM-DD")
        return
    qdate = context.args[0]
    try:
        _ = datetime.strptime(qdate, "%Y-%m-%d")
    except Exception:
        await update.message.reply_text("Invalid date format. Use YYYY-MM-DD")
        return
    rows = query_messages_by_date(qdate)
    if not rows:
        await update.message.reply_text("No messages on that date.")
        return
    lines = []
    for r in rows:
        lines.append(f"ID:{r[0]} {r[1][:19]} {r[2]} sent:{r[5]} failed:{r[6]} targets:{r[4]}\nPreview: {r[3]}")
    await update.message.reply_text("\n\n".join(lines))

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caller = update.effective_user
    if not caller or not is_admin(caller.id):
        await update.message.reply_text("❌ Permission denied.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <text>")
        return
    text = " ".join(context.args)
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

async def private_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_admin(user.id):
        return
    msg = update.message
    if not msg:
        return
    summary = await broadcast_message_to_all(msg, context)
    await update.message.reply_text(f"Broadcast completed. sent: {summary['sent']}, failed: {summary['failed']}")

async def channel_post_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post
    if not msg:
        return
    await broadcast_message_to_all(msg, context)

# Chat member updates
def extract_status_change(old: ChatMember, new: ChatMember) -> Optional[tuple]:
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
    if new_status in ("member", "administrator", "creator"):
        added = add_chat_db(cid, ctype, title, username, None)
        if added:
            logger.info("Registered chat %s (%s)", title or cid, ctype)
    elif new_status in ("left", "kicked", "banned"):
        removed = remove_chat_db(cid)
        if removed:
            log_left_chat(cid, title)
            logger.info("Removed chat %s because bot left/kicked", cid)

async def deliveries_for_message_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caller = update.effective_user
    if not caller or not is_admin(caller.id):
        await update.message.reply_text("❌ Permission denied.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /deliveries <message_row_id>")
        return
    try:
        mid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("invalid id")
        return
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT target_chat_id, status, error FROM deliveries WHERE message_row_id = ?", (mid,))
    rows = cur.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("No deliveries found for that id.")
        return
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

    # chat member updates
    app.add_handler(ChatMemberHandler(my_chat_member_update, chat_member_types=ChatMemberHandler.MY_CHAT_MEMBER))

    # channel posts
    app.add_handler(MessageHandler(filters.CHAT_TYPE_CHANNEL & ~filters.COMMAND, channel_post_handler))

    # private admin messages -> broadcast
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (~filters.COMMAND), private_message_handler))

    logger.info("Bot started — polling for updates...")
    app.run_polling(allowed_updates=[
        "message", "edited_message", "channel_post", "my_chat_member", "chat_member"
    ])

# ---------------- KEEP-ALIVE (Flask) ----------------
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

# Start
if __name__ == "__main__":
    keep_alive()
    main()
