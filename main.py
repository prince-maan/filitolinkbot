import os
import asyncio
import re
import logging
import base64
from aiohttp import web
from pyrogram import Client, filters, idle
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

logging.basicConfig(level=logging.INFO)

# ==========================================
# Environment Variables (For Render)
# ==========================================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH")
DB_CHANNEL_ID = int(os.environ.get("DB_CHANNEL_ID", 0))

# Get Multiple Admin IDs (कॉमा से अलग की गई IDs)
ADMIN_IDS = [int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
AUTO_DELETE_TIME = int(os.environ.get("AUTO_DELETE_TIME", 300)) # Default 5 mins

app = Client("render_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
user_states = {}

# ==========================================
# 1. ADMIN: Receive File or Album
# ==========================================
@app.on_message(filters.private & filters.user(ADMIN_IDS) & (filters.document | filters.video | filters.photo | filters.audio))
async def handle_media(client: Client, message: Message):
    state = user_states.setdefault(message.chat.id, {'db_ids': [], 'buttons': [], 'processed_groups': set()})
    
    if message.media_group_id:
        if message.media_group_id in state['processed_groups']:
            return 
        state['processed_groups'].add(message.media_group_id)
        try:
            copied_msgs = await client.copy_media_group(DB_CHANNEL_ID, message.chat.id, message.id)
            state['db_ids'].extend([m.id for m in copied_msgs])
            await message.reply_text("✅ **Album saved!**\nDo you want to add buttons?\nFormat: `[Name](Link)`\nSend **/done** to finish.")
        except Exception as e:
            await message.reply_text(f"❌ Error saving album: {e}")
    else:
        try:
            copied_msg = await message.copy(chat_id=DB_CHANNEL_ID)
            state['db_ids'].append(copied_msg.id)
            await message.reply_text("✅ **File saved!**\nDo you want to add buttons?\nFormat: `[Name](Link)`\nSend **/done** to finish.")
        except Exception as e:
            await message.reply_text(f"❌ Error saving file: {e}")

# ==========================================
# 2. ADMIN: Add Buttons
# ==========================================
@app.on_message(filters.private & filters.user(ADMIN_IDS) & filters.text & ~filters.command(["start", "done"]))
async def add_buttons(client: Client, message: Message):
    state = user_states.get(message.chat.id)
    if not state:
        return 
    
    matches = re.findall(r"\[(.*?)\]\((.*?)\)", message.text)
    if matches:
        for name, url in matches:
            if not url.startswith("http://") and not url.startswith("https://"):
                url = "https://" + url
            state["buttons"].append(InlineKeyboardButton(name, url=url))
        await message.reply_text("✅ **Button added!**\nSend more or send **/done**.")
    else:
        await message.reply_text("❌ Invalid format! Example: `[Google](https://google.com)`")

# ==========================================
# 3. ADMIN: Generate Link (/done)
# ==========================================
@app.on_message(filters.private & filters.user(ADMIN_IDS) & filters.command("done"))
async def finish_and_get_link(client: Client, message: Message):
    state = user_states.get(message.chat.id)
    if not state or not state['db_ids']:
        return await message.reply_text("❌ Please send a media file or album first!")
    
    db_ids = state['db_ids']
    buttons = state['buttons']
    
    try:
        if buttons:
            reply_markup = InlineKeyboardMarkup([[btn] for btn in buttons])
            if len(db_ids) == 1:
                try:
                    await client.edit_message_reply_markup(chat_id=DB_CHANNEL_ID, message_id=db_ids[0], reply_markup=reply_markup)
                except:
                    btn_msg = await client.send_message(DB_CHANNEL_ID, "🔗 **Links:**", reply_markup=reply_markup)
                    db_ids.append(btn_msg.id)
            else:
                btn_msg = await client.send_message(DB_CHANNEL_ID, "🔗 **Links:**", reply_markup=reply_markup)
                db_ids.append(btn_msg.id)
        
        ids_str = "-".join(map(str, db_ids))
        payload = base64.urlsafe_b64encode(ids_str.encode()).decode().rstrip('=')
        
        bot_info = await client.get_me()
        link = f"https://t.me/{bot_info.username}?start={payload}"
        
        await message.reply_text(f"🎉 **Link ready:**\n\n🔗 `{link}`", disable_web_page_preview=True)
        del user_states[message.chat.id]
    except Exception as e:
        await message.reply_text(f"❌ Error generating link: {e}")

# ==========================================
# 4. USER: Receive File (Silent Auto-Delete)
# ==========================================
@app.on_message(filters.command("start") & filters.private)
async def start_command(client: Client, message: Message):
    if len(message.command) > 1:
        payload = message.command[1]
        try:
            padding = 4 - (len(payload) % 4)
            if padding != 4:
                payload += "=" * padding
            ids_str = base64.urlsafe_b64decode(payload).decode()
            db_ids = [int(x) for x in ids_str.split('-')]
        except:
            return await message.reply_text("❌ Invalid or expired link!")
        
        try:
            messages = await client.get_messages(DB_CHANNEL_ID, db_ids)
            sent_msg_ids = []
            processed_groups = set()
            
            for msg in messages:
                if msg.empty: continue
                if msg.media_group_id:
                    if msg.media_group_id not in processed_groups:
                        processed_groups.add(msg.media_group_id)
                        sent_msgs = await client.copy_media_group(message.chat.id, DB_CHANNEL_ID, msg.id)
                        sent_msg_ids.extend([m.id for m in sent_msgs])
                else:
                    sent_msg = await client.copy_message(message.chat.id, DB_CHANNEL_ID, msg.id)
                    sent_msg_ids.append(sent_msg.id)
            
            if sent_msg_ids:
                asyncio.create_task(auto_delete_task(client, message.chat.id, sent_msg_ids))
            else:
                await message.reply_text("❌ File not found.")
        except Exception as e:
            logging.error(f"Error fetching file: {e}")
    else:
        await message.reply_text("👋 Hello! Click a valid link to get files.")

# ==========================================
# 5. Background Task: Auto-Delete
# ==========================================
async def auto_delete_task(client: Client, chat_id: int, message_ids: list):
    await asyncio.sleep(AUTO_DELETE_TIME)
    try:
        await client.delete_messages(chat_id=chat_id, message_ids=message_ids)
    except Exception as e:
        logging.error(f"Delete Error: {e}")

# ==========================================
# Web Server & STABLE Main Loop 
# ==========================================
async def handle(request):
    return web.Response(text="Bot is running smoothly on Render!")

async def main():
    # 1. वेब सर्वर स्टार्ट (रेंडर के लिए)
    web_app = web.Application()
    web_app.router.add_get('/', handle)
    runner = web.AppRunner(web_app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logging.info(f"✅ Web server started on port {port}")

    # 2. बोट स्टार्ट
    await app.start()
    logging.info("✅ Bot Started")

    # 3. चैनल को कैशे (Cache) करना
    try:
        chat = await app.get_chat(DB_CHANNEL_ID)
        logging.info(f"✅ Channel Cached Successfully: {chat.title}")
    except Exception as e:
        logging.error(f"❌ Failed to cache channel: {e}")

    # 4. बोट को परमानेंट चालू रखना (यहीं पर पिछली बार क्रैश हुआ था)
    logging.info("🤖 Bot is now completely LIVE and waiting for messages...")
    await idle() 

    # 5. अगर कोई बोट को बंद करे, तो सफाई से बंद होना
    await app.stop()
    await runner.cleanup()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
