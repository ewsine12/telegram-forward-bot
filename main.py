import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import datetime

# বট টোকেন ও অ্যাডমিন আইডি বসাও 👇
BOT_TOKEN = "8414051726:AAF6vcdJu2KSs67VBlZqj1F7QeNoPFzCMPc"
ADMIN_IDS = [7149740820]  # এখানে চাইলে আরও অ্যাডমিন আইডি দিতে পারো
connected_groups = set()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in ADMIN_IDS:
        await update.message.reply_text("✅ Bot Active!\nUse /help for commands.")
    else:
        await update.message.reply_text("🚫 You are not authorized to use this bot.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """
📋 Available Commands:
🔹 /groups - Show connected groups
🔹 /admins - Show admin list
🔹 /status - Bot activity log
🔹 .sms <text> - Send message to all groups (Admin only)
"""
    await update.message.reply_text(text)

async def connect_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type in ["group", "supergroup"]:
        connected_groups.add(chat.id)
        await update.message.reply_text("✅ Group connected successfully!")
        logger.info(f"Connected group: {chat.title} ({chat.id})")

async def send_sms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        return await update.message.reply_text("🚫 You are not admin.")
    
    text = update.message.text.replace(".sms", "").strip()
    if not text:
        return await update.message.reply_text("⚠️ Usage: .sms <message>")
    
    success, fail = 0, 0
    for group_id in list(connected_groups):
        try:
            await context.bot.send_message(chat_id=group_id, text=text)
            success += 1
        except Exception as e:
            fail += 1
            logger.warning(f"Failed to send to {group_id}: {e}")
    
    report = f"✅ Sent to {success} groups.\n❌ Failed: {fail}"
    await update.message.reply_text(report)

async def group_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return await update.message.reply_text("🚫 Admins only.")
    
    if not connected_groups:
        await update.message.reply_text("No groups connected.")
    else:
        text = "\n".join([f"🔹 {gid}" for gid in connected_groups])
        await update.message.reply_text(f"Connected Groups:\n{text}")

async def admin_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "\n".join([f"👑 {aid}" for aid in ADMIN_IDS])
    await update.message.reply_text(f"Admin List:\n{text}")

async def channel_forward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.channel_post:
        msg = update.channel_post.text
        for group_id in connected_groups:
            try:
                await context.bot.send_message(chat_id=group_id, text=msg)
            except Exception as e:
                logger.warning(f"Channel forward fail: {e}")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("groups", group_list))
    app.add_handler(CommandHandler("admins", admin_list))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, connect_group))
    app.add_handler(MessageHandler(filters.Regex(r'^\.sms'), send_sms))
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, channel_forward))
    
    logger.info("🚀 Bot started successfully!")
    app.run_polling()

if __name__ == "__main__":
    main()
