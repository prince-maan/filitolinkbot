import os
import asyncio
import re
import logging
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

# रेंडर में लॉग्स देखने के लिए
logging.basicConfig(level=logging.INFO)

# ==========================================
# रेंडर के Environment Variables से डिटेल्स लेना
# ==========================================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH")
DB_CHANNEL_ID = int(os.environ.get("DB_CHANNEL_ID", 0))
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
AUTO_DELETE_TIME = int(os.environ.get("AUTO_DELETE_TIME", 300)) # डिफ़ॉल्ट 5 मिनट

app = Client("render_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
user_states = {}

# 1. ADMIN: फाइल रिसीव करना
@app.on_message(filters.private & filters.user(ADMIN_ID) & (filters.document | filters.video | filters.photo | filters.audio))
async def handle_media(client: Client, message: Message):
    try:
        copied_msg = await message.copy(chat_id=DB_CHANNEL_ID)
        user_states[message.chat.id] = {"db_msg_id": copied_msg.id, "buttons": []}
        
        await message.reply_text(
            "✅ **फाइल डेटाबेस चैनल में सेव हो गई!**\n\n"
            "क्या तुम इस पोस्ट में **बटन्स** जोड़ना चाहते हो?\n"
            "ऐसे भेजो: `[बटन का नाम](लिंक)`\n"
            "*(उदाहरण: `[Google](https://google.com)`)*\n\n"
            "नहीं जोड़ने हैं, तो सीधा **/done** भेज दो।"
        )
    except Exception as e:
        await message.reply_text(f"❌ चैनल में सेव करने में एरर: {e}")

# 2. ADMIN: बटन्स ऐड करना
@app.on_message(filters.private & filters.user(ADMIN_ID) & filters.text & ~filters.command(["start", "done"]))
async def add_buttons(client: Client, message: Message):
    state = user_states.get(message.chat.id)
    if not state:
        return 
    
    matches = re.findall(r"\[(.*?)\]\((.*?)\)", message.text)
    if matches:
        for name, url in matches:
            state["buttons"].append(InlineKeyboardButton(name, url=url))
        await message.reply_text("✅ बटन जुड़ गया! और जोड़ने हैं तो भेजते रहो, वरना **/done** भेजो।")
    else:
        await message.reply_text("❌ गलत फॉर्मेट! ऐसे भेजो: `[बटन का नाम](लिंक)`")

# 3. ADMIN: लिंक जनरेट करना
@app.on_message(filters.private & filters.user(ADMIN_ID) & filters.command("done"))
async def finish_and_get_link(client: Client, message: Message):
    state = user_states.get(message.chat.id)
    if not state:
        return await message.reply_text("❌ पहले कोई मीडिया फाइल तो भेजो!")
    
    db_msg_id = state["db_msg_id"]
    buttons = state["buttons"]
    
    try:
        if buttons:
            reply_markup = InlineKeyboardMarkup([[btn] for btn in buttons])
            await client.edit_message_reply_markup(chat_id=DB_CHANNEL_ID, message_id=db_msg_id, reply_markup=reply_markup)
        
        bot_info = await client.get_me()
        link = f"https://t.me/{bot_info.username}?start={db_msg_id}"
        
        await message.reply_text(
            f"🎉 **लो भाई, तुम्हारा शेयरिंग लिंक तैयार है!**\n\n"
            f"🔗 `{link}`\n\n"
            f"⏳ यह फाइल यूजर्स को {AUTO_DELETE_TIME // 60} मिनट बाद ऑटो-डिलीट हो जाएगी।",
            disable_web_page_preview=True
        )
        del user_states[message.chat.id]
    except Exception as e:
        await message.reply_text(f"❌ लिंक बनाने में दिक्कत आ गई: {e}")

# 4. USER: फाइल सेंड करना और ऑटो-डिलीट
@app.on_message(filters.command("start") & filters.private)
async def start_command(client: Client, message: Message):
    if len(message.command) > 1:
        try:
            msg_id = int(message.command[1])
            sent_msg = await client.copy_message(chat_id=message.chat.id, from_chat_id=DB_CHANNEL_ID, message_id=msg_id)
            warning_msg = await message.reply_text(f"⚠️ **ध्यान दें:** यह मैसेज {AUTO_DELETE_TIME // 60} मिनट बाद अपने आप डिलीट हो जाएगा!")
            
            asyncio.create_task(auto_delete_task(client, message.chat.id, sent_msg.id, warning_msg.id))
        except Exception as e:
            logging.error(f"Error fetching file: {e}")
            await message.reply_text("❌ फाइल नहीं मिली या लिंक एक्सपायर हो गया है।")
    else:
        await message.reply_text("👋 नमस्ते! मैं एक फाइल शेयरिंग बोट हूँ।\nमुझसे फाइल लेने के लिए मेरे दिए गए लिंक पर क्लिक करें।")

# 5. ऑटो-डिलीट टाइमर
async def auto_delete_task(client: Client, chat_id: int, file_msg_id: int, warning_msg_id: int):
    await asyncio.sleep(AUTO_DELETE_TIME)
    try:
        await client.delete_messages(chat_id=chat_id, message_ids=[file_msg_id, warning_msg_id])
    except Exception as e:
        logging.error(f"Delete एरर: {e}")

if __name__ == "__main__":
    logging.info("🤖 Bot Started on Render!")
    app.run()