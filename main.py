import os
import json
import time
import secrets
import sqlite3
import asyncio
import logging
import threading
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import (
    Update,
    InlineKeyboardButton as Btn,
    InlineKeyboardMarkup as Kb,
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaDocument,
    InputMediaAudio,
)
from telegram.error import Forbidden, BadRequest, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")

# ============================================================
# CONFIG (set these as environment variables on Render)
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "0").split(",") if x.strip().isdigit()}
DB_PATH = os.getenv("DB_PATH", "bot.db")
PORT = int(os.getenv("PORT", "10000"))             # Render sets this automatically
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "")  # Render sets this automatically
DEFAULT_DELETE_MIN = 5
MAX_ALBUM = 10                                     # Telegram limit per album

MEDIA_TYPES = {
    "photo": InputMediaPhoto,
    "video": InputMediaVideo,
    "document": InputMediaDocument,
    "audio": InputMediaAudio,
}

BUTTON_HELP = (
    "Add buttons (one row per line, use && for more buttons in the same row):\n\n"
    "Label | https://link\n"
    "Label1 | https://a && Label2 | https://b\n\n"
    "Or press Skip."
)

# ============================================================
# KEEP-ALIVE (stops Render from sleeping the service)
# ============================================================
class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is alive")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        return  # keep logs clean


def start_web_server():
    server = HTTPServer(("0.0.0.0", PORT), PingHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Keep-alive server running on port %s", PORT)


def start_self_ping():
    if not RENDER_URL:
        log.info("RENDER_EXTERNAL_URL not set, self-ping disabled")
        return

    def loop():
        while True:
            time.sleep(4 * 60)
            try:
                urllib.request.urlopen(RENDER_URL, timeout=20).read()
            except Exception as e:
                log.warning("Self-ping failed: %s", e)

    threading.Thread(target=loop, daemon=True).start()
    log.info("Self-ping enabled for %s", RENDER_URL)


# ============================================================
# DATABASE
# ============================================================
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS users (
    user_id    INTEGER PRIMARY KEY,
    name       TEXT,
    started_at TEXT,
    blocked    INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS links (
    code       TEXT PRIMARY KEY,
    text       TEXT,
    buttons    TEXT,
    items      TEXT,
    created_at TEXT,
    opens      INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS pending_deletes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER,
    message_ids TEXT,
    delete_at   TEXT
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


def is_admin(user_id):
    return user_id in ADMIN_IDS


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def save_user(user):
    run(
        "INSERT INTO users (user_id, name, started_at) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET name=excluded.name, blocked=0",
        user.id, user.first_name or "", now_iso(),
    )


def create_link(text, buttons, items):
    code = secrets.token_urlsafe(9)  # safe for Telegram deep links
    run(
        "INSERT INTO links (code, text, buttons, items, created_at, opens) VALUES (?, ?, ?, ?, ?, 0)",
        code, text, json.dumps(buttons), json.dumps(items), now_iso(),
    )
    return code


# ============================================================
# HELPERS
# ============================================================
def extract_item(message):
    if message.photo:
        return {"t": "photo", "id": message.photo[-1].file_id}
    if message.video:
        return {"t": "video", "id": message.video.file_id}
    if message.document:
        return {"t": "document", "id": message.document.file_id}
    if message.audio:
        return {"t": "audio", "id": message.audio.file_id}
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


def build_keyboard(rows):
    if not rows:
        return None
    return Kb([[Btn(b["text"], url=b["url"]) for b in row] for row in rows])


async def say(context, chat_id, text, keyboard=None):
    return await context.bot.send_message(chat_id, text, reply_markup=keyboard)


def deep_link(context, code):
    return f"https://t.me/{context.bot_data['username']}?start={code}"


async def send_single(bot, chat_id, item, protect):
    method = getattr(bot, f"send_{item['t']}")
    return await method(chat_id, item["id"], protect_content=protect)


# ============================================================
# AUTO-DELETE (survives restarts, because it is stored in the DB)
# ============================================================
async def delete_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    for mid in data["ids"]:
        try:
            await context.bot.delete_message(data["chat"], mid)
        except TelegramError:
            pass
    run("DELETE FROM pending_deletes WHERE id=?", data["row"])


def queue_delete(app, row_id, chat_id, message_ids, seconds):
    app.job_queue.run_once(
        delete_job,
        when=max(1, seconds),
        data={"row": row_id, "chat": chat_id, "ids": message_ids},
        name=f"del_{row_id}",
    )


def schedule_deletion(app, chat_id, message_ids, seconds):
    due = datetime.now() + timedelta(seconds=seconds)
    cur = run(
        "INSERT INTO pending_deletes (chat_id, message_ids, delete_at) VALUES (?, ?, ?)",
        chat_id, json.dumps(message_ids), due.isoformat(timespec="seconds"),
    )
    queue_delete(app, cur.lastrowid, chat_id, message_ids, seconds)


async def restore_pending_deletes(app):
    now = datetime.now()
    for row in run("SELECT * FROM pending_deletes").fetchall():
        due = datetime.fromisoformat(row["delete_at"])
        seconds = (due - now).total_seconds()
        queue_delete(app, row["id"], row["chat_id"], json.loads(row["message_ids"]), seconds)
    log.info("Restored %d pending deletions", len(run("SELECT id FROM pending_deletes").fetchall()))


async def send_content(context, chat_id, link, auto_delete=True):
    """Sends album(s) + text + buttons. auto_delete=True removes everything after the set time."""
    protect = get_setting("protect") == "1"
    items = link["items"]
    message_ids = []

    for start in range(0, len(items), MAX_ALBUM):
        chunk = items[start:start + MAX_ALBUM]
        sent_as_album = False
        if len(chunk) >= 2:
            try:
                msgs = await context.bot.send_media_group(
                    chat_id,
                    [MEDIA_TYPES[x["t"]](x["id"]) for x in chunk],
                    protect_content=protect,
                )
                message_ids += [m.message_id for m in msgs]
                sent_as_album = True
            except BadRequest:
                sent_as_album = False  # mixed types Telegram rejects, send one by one
        if not sent_as_album:
            for x in chunk:
                m = await send_single(context.bot, chat_id, x, protect)
                message_ids.append(m.message_id)

    minutes = int(get_setting("delete_min"))
    text = link.get("text") or "📎 Your files are above."
    if auto_delete:
        text += f"\n\n⏳ These files will be deleted in {minutes} minute(s)."

    m = await context.bot.send_message(
        chat_id,
        text,
        reply_markup=build_keyboard(link.get("buttons") or []),
        protect_content=protect,
    )
    message_ids.append(m.message_id)

    if auto_delete:
        schedule_deletion(context.application, chat_id, message_ids, minutes * 60)


# ============================================================
# KEYBOARDS AND SCREENS
# ============================================================
def admin_menu_kb():
    return Kb([
        [Btn("📦 Album Link", callback_data="new_album")],
        [Btn("📄 Single File Link", callback_data="new_single")],
        [Btn("📚 Batch Links", callback_data="new_batch")],
        [Btn("📢 Broadcast", callback_data="broadcast")],
        [Btn("📊 Stats", callback_data="stats"), Btn("⚙️ Settings", callback_data="settings")],
    ])


def builder_kb(stage):
    if stage == "collect":
        rows = [[Btn("➡️ Next (Text & Buttons)", callback_data="bld_next")]]
    elif stage == "text":
        rows = [[Btn("⏭ Skip Text", callback_data="bld_skip_text")]]
    else:
        rows = [[Btn("✅ Skip Buttons & Create", callback_data="bld_finish")]]
    rows.append([Btn("❌ Cancel", callback_data="menu")])
    return Kb(rows)


async def show_menu(context, chat_id):
    await say(context, chat_id, "🏠 Main Menu\nChoose an option below:", admin_menu_kb())


async def show_stats(context, chat_id):
    total = count("SELECT COUNT(*) FROM users")
    blocked = count("SELECT COUNT(*) FROM users WHERE blocked=1")
    today = count("SELECT COUNT(*) FROM users WHERE started_at >= ?",
                  datetime.now().strftime("%Y-%m-%d"))
    links = count("SELECT COUNT(*) FROM links")
    opens = count("SELECT COALESCE(SUM(opens), 0) FROM links")
    pending = count("SELECT COUNT(*) FROM pending_deletes")

    text = (
        "📊 Stats\n\n"
        f"👥 Total users who started the bot: {total}\n"
        f"✅ Active users: {total - blocked}\n"
        f"🚫 Blocked the bot: {blocked}\n"
        f"🆕 Started today: {today}\n\n"
        f"🔗 Total links: {links}\n"
        f"👀 Total link opens: {opens}\n"
        f"🗑 Files waiting to be deleted: {pending}\n\n"
        "ℹ️ Blocked users are only detected during a broadcast."
    )
    await say(context, chat_id, text, Kb([[Btn("🏠 Menu", callback_data="menu")]]))


def count(sql, *args):
    return run(sql, *args).fetchone()[0]


async def show_settings(context, chat_id):
    minutes = get_setting("delete_min")
    protect_on = get_setting("protect") == "1"
    text = (
        "⚙️ Settings\n\n"
        f"⏳ Auto-delete time: {minutes} minute(s)\n"
        f"🔒 Content protection: {'ON' if protect_on else 'OFF'}\n\n"
        "Protection blocks forwarding and saving. Screenshots cannot be fully blocked by Telegram."
    )
    keyboard = Kb([
        [Btn(f"{m} min", callback_data=f"set_min:{m}") for m in (1, 3, 5)],
        [Btn(f"{m} min", callback_data=f"set_min:{m}") for m in (6, 10, 15)],
        [Btn("✏️ Custom Minutes", callback_data="custom_min")],
        [Btn(f"🔒 Protection: turn {'OFF' if protect_on else 'ON'}", callback_data="toggle_protect")],
        [Btn("🏠 Menu", callback_data="menu")],
    ])
    await say(context, chat_id, text, keyboard)


async def finish_builder(context, chat_id):
    ud = context.user_data
    link = {
        "text": ud.get("text", ""),
        "buttons": ud.get("buttons", []),
        "items": ud.get("items", []),
    }
    if not link["items"] and not link["text"]:
        return await say(context, chat_id, "❌ Nothing to create yet. Send a file or text first.",
                         builder_kb("collect"))

    if ud.get("mode") == "album":
        code = create_link(link["text"], link["buttons"], link["items"])
        url = deep_link(context, code)
        ud.clear()
        return await say(context, chat_id, f"🔗 Your link is ready:\n{url}", admin_menu_kb())

    # Broadcast: show preview first, then ask for confirmation
    ud["state"] = "confirm"
    ud["bc"] = link
    await send_content(context, chat_id, link, auto_delete=False)
    await say(
        context, chat_id,
        "👆 This is the preview. Send it to all users?",
        Kb([
            [Btn("🚀 Send to All", callback_data="bc_send")],
            [Btn("❌ Cancel", callback_data="menu")],
        ]),
    )


async def run_broadcast(context, admin_chat, bc):
    users = run("SELECT user_id FROM users WHERE blocked=0").fetchall()
    ok = fail = 0
    for u in users:
        try:
            await send_content(context, u["user_id"], bc, auto_delete=False)
            ok += 1
        except Forbidden:
            run("UPDATE users SET blocked=1 WHERE user_id=?", u["user_id"])
            fail += 1
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after)
            fail += 1
        except TelegramError:
            fail += 1
        await asyncio.sleep(0.05)  # avoid Telegram rate limits
    await say(
        context, admin_chat,
        f"📢 Broadcast finished\n✅ Delivered: {ok}\n❌ Failed: {fail}",
        admin_menu_kb(),
    )


# ============================================================
# HANDLERS
# ============================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    save_user(user)

    if context.args:  # opened via a link
        code = context.args[0]
        row = run("SELECT * FROM links WHERE code=?", code).fetchone()
        if not row:
            return await say(context, chat_id, "❌ This link is invalid or has been removed.")
        run("UPDATE links SET opens=opens+1 WHERE code=?", code)
        link = {
            "text": row["text"],
            "buttons": json.loads(row["buttons"]),
            "items": json.loads(row["items"]),
        }
        try:
            await send_content(context, chat_id, link, auto_delete=True)
        except TelegramError:
            await say(context, chat_id, "⚠️ Something went wrong. Please try again in a moment.")
        return

    if is_admin(user.id):
        return await show_menu(context, chat_id)

    await say(context, chat_id, "👋 Hello! This bot opens shared files and albums through links.")


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    chat_id = query.message.chat_id
    data = query.data
    ud = context.user_data

    if data == "menu":
        ud.clear()
        return await show_menu(context, chat_id)

    if data in ("new_album", "broadcast"):
        ud.clear()
        ud.update(
            state="collect",
            mode="album" if data == "new_album" else "broadcast",
            items=[], text="", buttons=[],
        )
        return await say(
            context, chat_id,
            "📤 Send photos, videos or files (one by one or as an album).\nWhen done, press ➡️ Next.",
            builder_kb("collect"),
        )

    if data == "new_single":
        ud.clear()
        ud["state"] = "single"
        return await say(context, chat_id, "📄 Send a file and I will create its link right away.",
                         Kb([[Btn("🏠 Menu", callback_data="menu")]]))

    if data == "new_batch":
        ud.clear()
        ud["state"] = "batch"
        return await say(
            context, chat_id,
            "📚 Batch mode is ON. Send files one after another, each one gets its own link.\n"
            "Press ✅ Done when finished.",
            Kb([[Btn("✅ Done", callback_data="menu")]]),
        )

    if data == "bld_next":
        ud["state"] = "text"
        return await say(context, chat_id,
                         "✍️ Send the text. It will appear below the files when the link is opened.",
                         builder_kb("text"))

    if data == "bld_skip_text":
        ud["state"] = "buttons"
        return await say(context, chat_id, BUTTON_HELP, builder_kb("buttons"))

    if data == "bld_finish":
        return await finish_builder(context, chat_id)

    if data == "bc_send":
        bc = ud.get("bc")
        ud.clear()
        if bc:
            asyncio.create_task(run_broadcast(context, chat_id, bc))
            await say(context, chat_id, "🚀 Broadcast started. I will let you know when it is done.")
        return

    if data == "stats":
        return await show_stats(context, chat_id)

    if data == "settings":
        return await show_settings(context, chat_id)

    if data.startswith("set_min:"):
        set_setting("delete_min", data.split(":")[1])
        return await show_settings(context, chat_id)

    if data == "toggle_protect":
        set_setting("protect", "0" if get_setting("protect") == "1" else "1")
        return await show_settings(context, chat_id)

    if data == "custom_min":
        ud.clear()
        ud["state"] = "custom_min"
        return await say(context, chat_id, "✏️ Send the number of minutes (between 1 and 1440).")


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not is_admin(message.from_user.id):
        return

    ud = context.user_data
    chat_id = message.chat_id
    state = ud.get("state")
    item = extract_item(message)

    if state is None:
        return await show_menu(context, chat_id)

    if state == "single" and item:
        code = create_link(message.caption or "", [], [item])
        return await say(context, chat_id, f"🔗 Link is ready:\n{deep_link(context, code)}")

    if state == "batch" and item:
        code = create_link(message.caption or "", [], [item])
        return await say(context, chat_id, f"🔗 Link is ready:\n{deep_link(context, code)}",
                         Kb([[Btn("✅ Done", callback_data="menu")]]))

    if state == "collect":
        if item:
            ud["items"].append(item)
            if message.caption and not ud.get("text"):
                ud["text"] = message.caption
            return await say(
                context, chat_id,
                f"✅ {len(ud['items'])} file(s) added. Send more, or press ➡️ Next.",
                builder_kb("collect"),
            )
        return await say(context, chat_id, "Please send a photo, video or file, or press ➡️ Next.",
                         builder_kb("collect"))

    if state == "text" and message.text:
        ud["text"] = message.text
        ud["state"] = "buttons"
        return await say(context, chat_id, BUTTON_HELP, builder_kb("buttons"))

    if state == "buttons" and message.text:
        rows = parse_buttons(message.text)
        if not rows:
            return await say(
                context, chat_id,
                "❌ Wrong format. Use this:\nLabel | https://...",
                builder_kb("buttons"),
            )
        ud["buttons"] = rows
        return await finish_builder(context, chat_id)

    if state == "custom_min" and message.text and message.text.strip().isdigit():
        minutes = int(message.text.strip())
        if 1 <= minutes <= 1440:
            set_setting("delete_min", minutes)
            ud.clear()
            return await show_settings(context, chat_id)
        return await say(context, chat_id, "❌ Please send a number between 1 and 1440.")

    await say(
        context, chat_id,
        "⚠️ I did not understand that. Send the right input or press ❌ Cancel.",
        Kb([[Btn("❌ Cancel", callback_data="menu")]]),
    )


# ============================================================
# STARTUP
# ============================================================
async def post_init(app: Application):
    me = await app.bot.get_me()
    app.bot_data["username"] = me.username
    await restore_pending_deletes(app)
    log.info("Bot @%s is running", me.username)


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set.")

    start_web_server()
    start_self_ping()

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
