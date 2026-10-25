import os
import json
import secrets
import sqlite3
import asyncio
import logging
from datetime import datetime

from telegram import (
    Update, InlineKeyboardButton as B, InlineKeyboardMarkup as KM,
    InputMediaPhoto, InputMediaVideo, InputMediaDocument, InputMediaAudio,
)
from telegram.error import Forbidden, BadRequest, RetryAfter, TelegramError
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters,
)

logging.basicConfig(level=logging.INFO)

# ================= CONFIG =================
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "123456789").split(",")}
DB_PATH = "bot.db"
DEFAULT_DELETE_MIN = 5

MEDIA = {
    "photo": InputMediaPhoto,
    "video": InputMediaVideo,
    "document": InputMediaDocument,
    "audio": InputMediaAudio,
}

BUTTON_HELP = (
    "🔘 बटन ऐसे लिखें (हर लाइन एक रो बनेगी, एक ही रो में कई बटन के लिए && लगाएं):\n"
    "लेबल | https://link\n"
    "लेबल1 | https://a && लेबल2 | https://b\n\n"
    "या ⏭ छोड़ो दबाएं।"
)

# ================= DATABASE =================
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    name TEXT,
    started_at TEXT,
    blocked INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS links (
    code TEXT PRIMARY KEY,
    text TEXT,
    buttons TEXT,
    items TEXT,
    created_at TEXT,
    opens INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
""")
db.execute("INSERT OR IGNORE INTO settings VALUES ('delete_min', ?)", (str(DEFAULT_DELETE_MIN),))
db.execute("INSERT OR IGNORE INTO settings VALUES ('protect', '1')")
db.commit()


def run(sql, *args):
    cur = db.execute(sql, args)
    db.commit()
    return cur


def get_setting(key):
    return run("SELECT value FROM settings WHERE key=?", key).fetchone()["value"]


def set_setting(key, value):
    run("INSERT OR REPLACE INTO settings VALUES (?, ?)", key, str(value))


def is_admin(uid):
    return uid in ADMIN_IDS


def save_user(u):
    run(
        "INSERT INTO users (user_id, name, started_at) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET name=excluded.name, blocked=0",
        u.id, u.first_name or "", datetime.now().isoformat(timespec="seconds"),
    )


def create_link(text, buttons, items):
    code = secrets.token_urlsafe(9)  # Telegram deep link ke liye safe characters
    run(
        "INSERT INTO links VALUES (?, ?, ?, ?, ?, 0)",
        code, text, json.dumps(buttons), json.dumps(items),
        datetime.now().isoformat(timespec="seconds"),
    )
    return code


# ================= HELPERS =================
def extract_item(m):
    if m.photo:
        return {"t": "photo", "id": m.photo[-1].file_id}
    if m.video:
        return {"t": "video", "id": m.video.file_id}
    if m.document:
        return {"t": "document", "id": m.document.file_id}
    if m.audio:
        return {"t": "audio", "id": m.audio.file_id}
    return None


def parse_buttons(text):
    rows = []
    for line in text.splitlines():
        row = []
        for part in line.split("&&"):
            if "|" not in part:
                continue
            label, url = [x.strip() for x in part.split("|", 1)]
            if label and url.startswith(("http://", "https://", "tg://")):
                row.append({"text": label, "url": url})
        if row:
            rows.append(row)
    return rows


def build_kb(rows):
    if not rows:
        return None
    return KM([[B(b["text"], url=b["url"]) for b in row] for row in rows])


async def say(context, chat_id, text, kb=None):
    return await context.bot.send_message(chat_id, text, reply_markup=kb)


async def deep_link(context, code):
    return f"https://t.me/{context.bot_data['username']}?start={code}"


async def send_single(bot, chat_id, item, protect):
    fn = {
        "photo": bot.send_photo,
        "video": bot.send_video,
        "document": bot.send_document,
        "audio": bot.send_audio,
    }[item["t"]]
    return await fn(chat_id, item["id"], protect_content=protect)


async def delete_job(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data
    for mid in d["ids"]:
        try:
            await context.bot.delete_message(d["chat"], mid)
        except TelegramError:
            pass


async def send_content(context, chat_id, link, schedule=True):
    """Album + text + buttons bhejta hai. schedule=True par auto-delete lagta hai."""
    protect = get_setting("protect") == "1"
    mins = int(get_setting("delete_min"))
    items = link["items"]
    ids = []

    for i in range(0, len(items), 10):  # Telegram ek album me max 10
        chunk = items[i:i + 10]
        if len(chunk) >= 2:
            try:
                msgs = await context.bot.send_media_group(
                    chat_id,
                    [MEDIA[x["t"]](x["id"]) for x in chunk],
                    protect_content=protect,
                )
                ids += [m.message_id for m in msgs]
                continue
            except BadRequest:
                pass  # mixed types ho to alag-alag bhejo
        for x in chunk:
            m = await send_single(context.bot, chat_id, x, protect)
            ids.append(m.message_id)

    note = f"\n\n⏳ यह {mins} मिनट बाद अपने-आप डिलीट हो जाएगा।" if schedule else ""
    body = (link.get("text") or "📎 नीचे दी गई फाइल(एँ) देखें") + note
    m = await context.bot.send_message(
        chat_id, body,
        reply_markup=build_kb(link.get("buttons") or []),
        protect_content=protect,
    )
    ids.append(m.message_id)

    if schedule:
        context.application.job_queue.run_once(
            delete_job, when=mins * 60,
            data={"chat": chat_id, "ids": ids},
            name=f"del_{chat_id}_{ids[0]}",
        )


# ================= KEYBOARDS =================
def admin_kb():
    return KM([
        [B("📦 एल्बम / मीडिया लिंक", callback_data="new_album")],
        [B("📄 सिंगल फाइल → लिंक", callback_data="new_single")],
        [B("📚 बैच लिंक (कई एक साथ)", callback_data="new_batch")],
        [B("📢 ब्रॉडकास्ट", callback_data="broadcast")],
        [B("📊 स्टैट्स", callback_data="stats"), B("⚙️ सेटिंग्स", callback_data="settings")],
    ])


def builder_kb(stage):
    if stage == "collect":
        rows = [[B("➡️ आगे (टेक्स्ट/बटन)", callback_data="bld_next")]]
    elif stage == "text":
        rows = [[B("⏭ टेक्स्ट छोड़ो", callback_data="bld_skip_text")]]
    else:
        rows = [[B("⏭ बटन छोड़ो → बनाओ", callback_data="bld_finish")]]
    rows.append([B("❌ रद्द", callback_data="menu")])
    return KM(rows)


async def show_menu(context, chat):
    await say(context, chat, "🏠 मेन मेनू, नीचे से चुनें:", admin_kb())


async def show_stats(context, chat):
    def one(sql, *a):
        return run(sql, *a).fetchone()[0]

    total = one("SELECT COUNT(*) FROM users")
    blocked = one("SELECT COUNT(*) FROM users WHERE blocked=1")
    today = one("SELECT COUNT(*) FROM users WHERE started_at >= ?",
                datetime.now().strftime("%Y-%m-%d"))
    links = one("SELECT COUNT(*) FROM links")
    opens = one("SELECT COALESCE(SUM(opens), 0) FROM links")
    text = (
        "📊 स्टैट्स\n\n"
        f"👥 कुल जिन्होंने बोट स्टार्ट किया: {total}\n"
        f"✅ एक्टिव: {total - blocked}\n"
        f"🚫 बोट ब्लॉक/डिलीट किया: {blocked}\n"
        f"🆕 आज स्टार्ट किया: {today}\n"
        f"🔗 कुल लिंक: {links}\n"
        f"👀 कुल लिंक खुले: {opens}\n\n"
        "ℹ️ ब्लॉक की गिनती सिर्फ ब्रॉडकास्ट के समय पता चलती है।"
    )
    await say(context, chat, text, KM([[B("🏠 मेनू", callback_data="menu")]]))


async def show_settings(context, chat):
    mins = get_setting("delete_min")
    prot = get_setting("protect") == "1"
    text = (
        "⚙️ सेटिंग्स\n\n"
        f"⏳ ऑटो-डिलीट: {mins} मिनट\n"
        f"🔒 कंटेंट प्रोटेक्शन: {'ON' if prot else 'OFF'}"
    )
    kb = KM([
        [B(f"{m} मिनट", callback_data=f"set_min:{m}") for m in (1, 3, 5)],
        [B(f"{m} मिनट", callback_data=f"set_min:{m}") for m in (6, 10, 15)],
        [B("✏️ कस्टम मिनट", callback_data="custom_min")],
        [B(f"🔒 प्रोटेक्शन {'OFF करो' if prot else 'ON करो'}", callback_data="toggle_protect")],
        [B("🏠 मेनू", callback_data="menu")],
    ])
    await say(context, chat, text, kb)


async def finish_builder(context, chat):
    ud = context.user_data
    link = {
        "text": ud.get("text", ""),
        "buttons": ud.get("buttons", []),
        "items": ud.get("items", []),
    }
    if not link["items"] and not link["text"]:
        return await say(context, chat, "❌ कुछ भी नहीं है। पहले फाइल या टेक्स्ट भेजो।", builder_kb("collect"))

    if ud.get("mode") == "album":
        code = create_link(link["text"], link["buttons"], link["items"])
        url = await deep_link(context, code)
        ud.clear()
        return await say(context, chat, f"🔗 लिंक तैयार है:\n{url}", admin_kb())

    # broadcast: pehle preview, phir confirm
    ud["state"] = "confirm"
    ud["bc"] = link
    await send_content(context, chat, link, schedule=False)
    await say(
        context, chat, "👆 यह प्रीव्यू है। सभी यूज़र्स को भेजना है?",
        KM([[B("🚀 सभी को भेजो", callback_data="bc_send")],
            [B("❌ रद्द", callback_data="menu")]]),
    )


async def run_broadcast(context, admin_chat, bc):
    users = run("SELECT user_id FROM users WHERE blocked=0").fetchall()
    ok = fail = 0
    for u in users:
        try:
            await send_content(context, u["user_id"], bc, schedule=False)
            ok += 1
        except Forbidden:
            run("UPDATE users SET blocked=1 WHERE user_id=?", u["user_id"])
            fail += 1
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after)
            fail += 1
        except TelegramError:
            fail += 1
        await asyncio.sleep(0.05)
    await say(context, admin_chat,
              f"📢 ब्रॉडकास्ट खत्म\n✅ पहुंचा: {ok}\n❌ नहीं पहुंचा: {fail}", admin_kb())


# ================= HANDLERS =================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    chat = update.effective_chat.id
    save_user(u)

    if context.args:  # link se aaya hai
        row = run("SELECT * FROM links WHERE code=?", context.args[0]).fetchone()
        if not row:
            return await say(context, chat, "❌ यह लिंक गलत है या हटा दिया गया है।")
        run("UPDATE links SET opens=opens+1 WHERE code=?", context.args[0])
        link = {
            "text": row["text"],
            "buttons": json.loads(row["buttons"]),
            "items": json.loads(row["items"]),
        }
        try:
            await send_content(context, chat, link)
        except TelegramError:
            await say(context, chat, "⚠️ कुछ गड़बड़ हुई, थोड़ी देर बाद फिर कोशिश करें।")
        return

    if is_admin(u.id):
        return await show_menu(context, chat)
    await say(context, chat, "👋 नमस्ते! यह बोट लिंक से फाइल/एल्बम खोलने के लिए है।")


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()
    if not is_admin(cq.from_user.id):
        return

    chat = cq.message.chat_id
    d = cq.data
    ud = context.user_data

    if d == "menu":
        ud.clear()
        return await show_menu(context, chat)

    if d in ("new_album", "broadcast"):
        ud.clear()
        ud.update(state="collect", mode="album" if d == "new_album" else "broadcast",
                  items=[], text="", buttons=[])
        return await say(context, chat,
                         "📤 फोटो/वीडियो/फाइलें भेजें (एक-एक करके या एल्बम की तरह)। फिर ➡️ आगे दबाएं।",
                         builder_kb("collect"))

    if d == "new_single":
        ud.clear()
        ud["state"] = "single"
        return await say(context, chat, "📄 फाइल भेजो, मैं तुरंत लिंक बना दूंगा।",
                         KM([[B("🏠 मेनू", callback_data="menu")]]))

    if d == "new_batch":
        ud.clear()
        ud["state"] = "batch"
        return await say(context, chat,
                         "📚 बैच मोड ON: फाइलें एक-एक करके भेजते जाओ, हर फाइल का अलग लिंक बनेगा। खत्म होने पर ✅ दबाओ।",
                         KM([[B("✅ बैच खत्म", callback_data="menu")]]))

    if d == "bld_next":
        ud["state"] = "text"
        return await say(context, chat, "✍️ टेक्स्ट भेजो (लिंक खोलने पर फाइलों के नीचे दिखेगा)।",
                         builder_kb("text"))

    if d == "bld_skip_text":
        ud["state"] = "buttons"
        return await say(context, chat, BUTTON_HELP, builder_kb("buttons"))

    if d == "bld_finish":
        return await finish_builder(context, chat)

    if d == "bc_send":
        bc = ud.get("bc")
        ud.clear()
        if bc:
            asyncio.create_task(run_broadcast(context, chat, bc))
            await say(context, chat, "🚀 ब्रॉडकास्ट शुरू हो गया है, खत्म होने पर बताऊंगा।")
        return

    if d == "stats":
        return await show_stats(context, chat)

    if d == "settings":
        return await show_settings(context, chat)

    if d.startswith("set_min:"):
        set_setting("delete_min", d.split(":")[1])
        return await show_settings(context, chat)

    if d == "toggle_protect":
        set_setting("protect", "0" if get_setting("protect") == "1" else "1")
        return await show_settings(context, chat)

    if d == "custom_min":
        ud.clear()
        ud["state"] = "custom_min"
        return await say(context, chat, "✏️ मिनट लिखकर भेजो (1 से 1440 के बीच)।")


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.message
    if not m or not is_admin(m.from_user.id):
        return

    ud = context.user_data
    chat = m.chat_id
    state = ud.get("state")
    item = extract_item(m)

    if state is None:
        return await show_menu(context, chat)

    if state == "single" and item:
        code = create_link("", [], [item])
        url = await deep_link(context, code)
        return await say(context, chat, f"🔗 लिंक तैयार:\n{url}")

    if state == "batch" and item:
        code = create_link(m.caption or "", [], [item])
        url = await deep_link(context, code)
        return await say(context, chat, f"🔗 लिंक तैयार:\n{url}")

    if state == "collect":
        if item:
            ud["items"].append(item)
            if m.caption and not ud.get("text"):
                ud["text"] = m.caption
            return await say(context, chat,
                             f"✅ {len(ud['items'])} फाइल जुड़ी। और भेजो, या ➡️ आगे दबाओ।",
                             builder_kb("collect"))
        return await say(context, chat, "सिर्फ फोटो/वीडियो/फाइल भेजो, या ➡️ आगे दबाओ।",
                         builder_kb("collect"))

    if state == "text" and m.text:
        ud["text"] = m.text
        ud["state"] = "buttons"
        return await say(context, chat, BUTTON_HELP, builder_kb("buttons"))

    if state == "buttons" and m.text:
        rows = parse_buttons(m.text)
        if not rows:
            return await say(context, chat, "❌ फॉर्मेट गलत है। ऐसे लिखो:\nलेबल | https://...",
                             builder_kb("buttons"))
        ud["buttons"] = rows
        return await finish_builder(context, chat)

    if state == "custom_min" and m.text and m.text.strip().isdigit():
        mins = int(m.text.strip())
        if 1 <= mins <= 1440:
            set_setting("delete_min", mins)
            ud.clear()
            return await show_settings(context, chat)
        return await say(context, chat, "❌ 1 से 1440 के बीच नंबर भेजो।")

    await say(context, chat, "⚠️ ये इनपुट समझ नहीं आया। सही चीज़ भेजो या ❌ रद्द दबाओ।",
              KM([[B("❌ रद्द", callback_data="menu")]]))


async def post_init(app: Application):
    me = await app.bot.get_me()
    app.bot_data["username"] = me.username


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, on_message))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
