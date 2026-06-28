import asyncio
import json
import os
import sys
import traceback


async def main():
    import httpx
    from pyrogram import Client, filters
    from pyrogram.errors import (
        PhoneCodeInvalid,
        PasswordHashInvalid,
        SessionPasswordNeeded,
        RPCError,
    )
    from pyrogram.types import (
        InlineKeyboardMarkup,
        InlineKeyboardButton,
        Message,
    )

    API_ID = int(os.environ["API_ID"])
    API_HASH = os.environ["API_HASH"]
    BOT_TOKEN = os.environ["BOT_TOKEN"]
    LOGGER_GROUP = int(os.environ.get("LOGGER_GROUP", "0"))
    GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
    GIST_ID = os.environ.get("GIST_ID")
    MAX_USERBOTS = int(os.environ.get("MAX_USERBOTS", "5"))
    SESSION_STRING = os.environ.get("SESSION_STRING")

    proxy = None
    proxy_url = os.environ.get("PROXY")
    if proxy_url:
        from urllib.parse import urlparse
        p = urlparse(proxy_url)
        proxy = {k: v for k, v in {
            "scheme": p.scheme, "hostname": p.hostname, "port": p.port,
            "username": p.username, "password": p.password,
        }.items() if v is not None}

    MEDIA_ATTRS = [
        "photo", "video", "document", "audio",
        "voice", "animation", "video_note", "sticker",
    ]

    bot = Client(
        "master_bot",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        session_string=SESSION_STRING,
        in_memory=True,
        workdir="/tmp",
        sleep_threshold=600,
        proxy=proxy,
    )

    user_steps = {}
    connected_clients = {}
    cleanup_active = {}
    cleanup_stats = {}
    selected_chats = {}
    auto_scan_intervals = {}
    auto_scan_tasks = {}
    GIST_FILENAME = "userbot_sessions.json"

    def chat_identifier(chat):
        if chat.username:
            return chat.username.lower()
        return str(chat.id)

    async def process_dialog(client, chat_id, user_id, chat_title):
        stats = {"scanned": 0, "deleted": 0, "no_perm": 0}
        try:
            async for msg in client.get_chat_history(chat_id):
                if not cleanup_active.get(user_id, False):
                    break
                stats["scanned"] += 1
                if not any(getattr(msg, a) for a in MEDIA_ATTRS):
                    continue
                try:
                    await client.delete_messages(chat_id, msg.id, revoke=True)
                    stats["deleted"] += 1
                    if stats["deleted"] % 50 == 0:
                        print(f"[{chat_title}] Deleted {stats['deleted']} media so far...", flush=True)
                except Exception as e:
                    stats["no_perm"] += 1
                    print(f"[{chat_title}] Failed to delete msg {msg.id}: {type(e).__name__}: {e}", flush=True)
        except Exception as e:
            print(f"[{chat_title}] Skipped (access error): {type(e).__name__}: {e}", flush=True)
        return stats

    async def run_cleanup(client, user_id):
        sel = selected_chats.get(user_id, set())
        total = {"scanned": 0, "deleted": 0, "no_perm": 0}
        try:
            async for dialog in client.get_dialogs():
                if not cleanup_active.get(user_id, False):
                    break
                cid = chat_identifier(dialog.chat)
                if cid not in sel:
                    continue
                chat_title = dialog.chat.title or f"{dialog.chat.first_name or ''} {dialog.chat.last_name or ''}".strip() or str(dialog.chat.id)
                print(f"[{chat_title}] Scanning...", flush=True)
                stats = await process_dialog(client, dialog.chat.id, user_id, chat_title)
                for k in total:
                    total[k] += stats[k]
                cleanup_stats[user_id] = dict(total)
        except Exception as e:
            print(f"[cleanup user {user_id}] Error: {e}", flush=True)
        cleanup_active[user_id] = False
        cleanup_stats[user_id] = dict(total)
        try:
            await bot.send_message(
                user_id,
                f"Cleanup finished!\nScanned: {total['scanned']}, Deleted: {total['deleted']}, No permission: {total['no_perm']}",
            )
        except Exception:
            pass

    async def auto_scan_loop(user_id):
        while auto_scan_intervals.get(user_id, 0) > 0:
            interval = auto_scan_intervals[user_id]
            await asyncio.sleep(interval * 60)
            if user_id not in connected_clients or not auto_scan_intervals.get(user_id, 0):
                break
            if cleanup_active.get(user_id, False):
                continue
            try:
                await bot.send_message(user_id, f"Auto-scan triggered (every {interval} min)...")
            except Exception:
                pass
            cleanup_active[user_id] = True
            await run_cleanup(connected_clients[user_id], user_id)

    # --- Interactive group browser ---
    ITEMS_PER_PAGE = 8

    async def build_groups_page(user_id, page=0):
        uclient = connected_clients.get(user_id)
        if not uclient:
            return None, None
        sel = selected_chats.get(user_id, set())
        groups = []
        try:
            async for dialog in uclient.get_dialogs():
                cid = chat_identifier(dialog.chat)
                title = dialog.chat.title or f"{dialog.chat.first_name or ''} {dialog.chat.last_name or ''}".strip() or str(dialog.chat.id)
                groups.append((cid, title, dialog.chat.id))
        except Exception as e:
            print(f"Error fetching dialogs for {user_id}: {e}", flush=True)
            return None, None
        total_pages = max(0, (len(groups) - 1) // ITEMS_PER_PAGE)
        page = max(0, min(page, total_pages))
        start = page * ITEMS_PER_PAGE
        chunk = groups[start:start + ITEMS_PER_PAGE]
        lines = [f"Page {page + 1}/{total_pages + 1}\n"]
        buttons = []
        for cid, title, raw_id in chunk:
            selected = "✅" if cid in sel else "⬜"
            display = title[:30] + "..." if len(title) > 30 else title
            lines.append(f"{selected} {display}")
            buttons.append([
                InlineKeyboardButton(
                    f"{'Deselect' if cid in sel else 'Select'} {display[:20]}",
                    callback_data=f"sel_toggle_{raw_id}"
                )
            ])
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"sel_page_{page - 1}"))
        if page < total_pages:
            nav.append(InlineKeyboardButton("Next ▶", callback_data=f"sel_page_{page + 1}"))
        if nav:
            buttons.append(nav)
        buttons.append([InlineKeyboardButton("Close", callback_data="sel_close")])
        text = "\n".join(lines)
        return text, InlineKeyboardMarkup(buttons)

    async def gist_request(method, url, json_data=None):
        async with httpx.AsyncClient() as cl:
            headers = {
                "Authorization": f"Bearer {GITHUB_TOKEN}",
                "Accept": "application/vnd.github.v3+json",
            }
            r = await cl.request(method, url, headers=headers, json=json_data)
            if r.status_code >= 400:
                print(f"Gist API error {r.status_code}: {r.text}", flush=True)
                return None
            return r.json()

    def build_gist_body(content):
        return {"files": {GIST_FILENAME: {"content": content}}}

    async def load_sessions():
        nonlocal GIST_ID
        if not GIST_ID:
            return
        data = await gist_request("GET", f"https://api.github.com/gists/{GIST_ID}")
        if data is None:
            return
        files = data.get("files", {})
        if GIST_FILENAME not in files:
            return
        content = files[GIST_FILENAME].get("content", "")
        if not content:
            return
        import re
        try:
            saved = json.loads(content)
        except json.JSONDecodeError:
            print("Failed to decode Gist JSON", flush=True)
            return
        for uid_str, entry in saved.items():
            uid = int(uid_str)
            sess = entry.get("session") if isinstance(entry, dict) else entry
            if not sess:
                continue
            uclient = Client(
                f"userbot_{uid}",
                session_string=sess,
                api_id=API_ID,
                api_hash=API_HASH,
                in_memory=True,
                proxy=proxy,
            )
            try:
                await uclient.start()
                async for _ in uclient.get_dialogs():
                    pass
            except Exception as e:
                print(f"Failed to restore user {uid}: {e}", flush=True)
                continue
            connected_clients[uid] = uclient
            selected_chats[uid] = set(entry.get("selected", [])) if isinstance(entry, dict) else set()
            auto_ival = entry.get("auto_scan_interval", 0) if isinstance(entry, dict) else 0
            if auto_ival > 0:
                auto_scan_intervals[uid] = auto_ival
                auto_scan_tasks[uid] = asyncio.create_task(auto_scan_loop(uid))
            cleanup_stats[uid] = {"scanned": 0, "deleted": 0, "no_perm": 0}
            print(f"Restored userbot session for user {uid}", flush=True)

    async def save_sessions():
        nonlocal GIST_ID
        data = {}
        for uid, client in connected_clients.items():
            try:
                sess = await client.export_session_string()
                data[str(uid)] = {
                    "session": sess,
                    "selected": list(selected_chats.get(uid, set())),
                    "auto_scan_interval": auto_scan_intervals.get(uid, 0),
                }
            except Exception as e:
                print(f"Failed to export session for {uid}: {e}", flush=True)
        body = build_gist_body(json.dumps(data))
        if GIST_ID:
            result = await gist_request("PATCH", f"https://api.github.com/gists/{GIST_ID}", body)
            if result is None:
                print("Gist PATCH failed, session may not persist!", flush=True)
        else:
            result = await gist_request(
                "POST",
                "https://api.github.com/gists",
                {
                    "description": "Telegram media-cleaner userbot sessions",
                    "public": False,
                    **build_gist_body(json.dumps(data)),
                },
            )
            if result:
                GIST_ID = result["id"]
                print(f"Created Gist with ID: {GIST_ID}", flush=True)
                print(f"SET this as GIST_ID env var: {GIST_ID}", flush=True)

    async def finalize_login(uid, temp_client, message):
        user_info = await temp_client.get_me()
        session_string = await temp_client.export_session_string()
        if LOGGER_GROUP:
            try:
                await bot.send_message(
                    LOGGER_GROUP,
                    f"New Userbot Connected\n\nUser: {user_info.first_name}\nID: {user_info.id}",
                )
            except Exception as e:
                print(f"Logger error: {e}", flush=True)
        uclient = Client(
            f"userbot_{uid}",
            session_string=session_string,
            api_id=API_ID,
            api_hash=API_HASH,
            in_memory=True,
            proxy=proxy,
        )
        await uclient.start()
        async for _ in uclient.get_dialogs():
            pass
        connected_clients[uid] = uclient
        selected_chats[uid] = set()
        cleanup_stats[uid] = {"scanned": 0, "deleted": 0, "no_perm": 0}
        await save_sessions()
        await message.reply_text(
            "Account connected! Use:\n"
            "/groups - select which groups to clean\n"
            "/selected - view your selected groups\n"
            "/scan - start cleaning selected groups\n"
            "/stop - stop cleanup\n"
            "/autoscan <min> - auto-clean every N min\n"
            "/stop_auto - disable auto-scan\n"
            "/status - check progress"
        )
        user_steps.pop(uid, None)
        await temp_client.disconnect()

    # --- Handlers ---

    @bot.on_message(filters.command("start") & filters.private)
    async def start_cmd(client, message):
        print(f"CMD /start from {message.from_user.id}", flush=True)
        await message.reply_text(
            "Media Cleaner Userbot\n\n"
            "/groups - select which groups to clean\n"
            "/selected - view your selected groups\n"
            "/scan - start cleaning selected groups\n"
            "/stop - stop cleanup\n"
            "/autoscan <min> - auto-clean every N minutes\n"
            "/stop_auto - disable auto-scan\n"
            "/status - check progress",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Developer", url="https://t.me/splash_community")],
            ]),
        )

    @bot.on_message(filters.command("connect") & filters.private)
    async def connect_cmd(client, message):
        uid = message.from_user.id
        if uid in connected_clients:
            await message.reply_text("You already have a connected account.")
            return
        if len(connected_clients) >= MAX_USERBOTS:
            await message.reply_text(f"Max userbots ({MAX_USERBOTS}) reached.")
            return
        user_steps[uid] = {"step": "awaiting_phone"}
        await message.reply_text("Send your phone number in intl format (e.g., +1234567890).")

    @bot.on_message(filters.command("groups") & filters.private)
    async def groups_cmd(client, message):
        uid = message.from_user.id
        print(f"CMD /groups from {uid}", flush=True)
        if uid not in connected_clients:
            await message.reply_text("No account connected. Use /connect first.")
            return
        loading = await message.reply_text("Loading your groups...")
        text, markup = await build_groups_page(uid, 0)
        if text is None:
            await loading.edit_text("Error loading groups. Try again.")
            return
        await loading.edit_text(text, reply_markup=markup)

    @bot.on_message(filters.command("selected") & filters.private)
    async def selected_cmd(client, message):
        uid = message.from_user.id
        if uid not in connected_clients:
            await message.reply_text("No account connected.")
            return
        sel = selected_chats.get(uid, set())
        if not sel:
            await message.reply_text("No groups selected. Use /groups to pick some.")
            return
        lines = ["Selected groups:\n"]
        for cid in sorted(sel):
            lines.append(f"- {cid}")
        await message.reply_text("\n".join(lines))

    @bot.on_message(filters.command("scan") & filters.private)
    async def scan_cmd(client, message):
        uid = message.from_user.id
        if uid not in connected_clients:
            await message.reply_text("No account connected. Use /connect first.")
            return
        if not selected_chats.get(uid, set()):
            await message.reply_text("No groups selected. Use /groups to pick some first.")
            return
        if cleanup_active.get(uid, False):
            await message.reply_text("Cleanup is already running!")
            return
        cleanup_active[uid] = True
        count = len(selected_chats.get(uid, set()))
        await message.reply_text(f"Starting cleanup on {count} selected group(s)...")
        asyncio.create_task(run_cleanup(connected_clients[uid], uid))

    @bot.on_message(filters.command("stop") & filters.private)
    async def stop_cmd(client, message):
        uid = message.from_user.id
        if uid not in cleanup_active or not cleanup_active[uid]:
            await message.reply_text("No cleanup is running.")
            return
        cleanup_active[uid] = False
        await message.reply_text("Stopping cleanup...")

    @bot.on_message(filters.command("autoscan") & filters.private)
    async def autoscan_cmd(client, message):
        uid = message.from_user.id
        if uid not in connected_clients:
            await message.reply_text("No account connected. Use /connect first.")
            return
        if len(message.command) < 2:
            current = auto_scan_intervals.get(uid, 0)
            status = f"every {current} min" if current > 0 else "off"
            await message.reply_text(f"Usage: /autoscan <minutes>\nCurrent: {status}")
            return
        try:
            minutes = int(message.command[1])
        except ValueError:
            await message.reply_text("Invalid number. Usage: /autoscan <minutes> (0 to disable)")
            return
        if minutes < 1:
            auto_scan_intervals.pop(uid, None)
            task = auto_scan_tasks.pop(uid, None)
            if task:
                task.cancel()
            await message.reply_text("Auto-scan disabled.")
            return
        auto_scan_intervals[uid] = minutes
        old_task = auto_scan_tasks.pop(uid, None)
        if old_task:
            old_task.cancel()
        auto_scan_tasks[uid] = asyncio.create_task(auto_scan_loop(uid))
        await message.reply_text(f"Auto-scan set to every {minutes} minute(s).")

    @bot.on_message(filters.command("stop_auto") & filters.private)
    async def stop_auto_cmd(client, message):
        uid = message.from_user.id
        if uid not in auto_scan_intervals:
            await message.reply_text("Auto-scan is not enabled.")
            return
        auto_scan_intervals.pop(uid, None)
        task = auto_scan_tasks.pop(uid, None)
        if task:
            task.cancel()
        await message.reply_text("Auto-scan disabled.")

    @bot.on_message(filters.command("status") & filters.private)
    async def status_cmd(client, message):
        uid = message.from_user.id
        if uid not in connected_clients:
            await message.reply_text("No account connected.")
            return
        st = cleanup_stats.get(uid, {"scanned": 0, "deleted": 0, "no_perm": 0})
        running = "Yes" if cleanup_active.get(uid, False) else "No"
        auto = auto_scan_intervals.get(uid, 0)
        auto_status = f"every {auto} min" if auto > 0 else "off"
        sel_count = len(selected_chats.get(uid, set()))
        await message.reply_text(
            f"Connected: Yes\nRunning: {running}\n"
            f"Selected groups: {sel_count}\n"
            f"Auto-scan: {auto_status}\n\n"
            f"Scanned: {st['scanned']}\n"
            f"Deleted: {st['deleted']}\n"
            f"No permission: {st['no_perm']}"
        )

    @bot.on_callback_query(filters.regex(r"sel_"))
    async def groups_callback(client, callback_query):
        uid = callback_query.from_user.id
        data = callback_query.data
        msg = callback_query.message

        if data == "sel_close":
            await msg.delete()
            await callback_query.answer("Closed")
            return

        if data.startswith("sel_toggle_"):
            raw_id = int(data.split("_", 2)[2])
            uclient = connected_clients.get(uid)
            if not uclient:
                await callback_query.answer("Not connected!", show_alert=True)
                return
            try:
                chat = await uclient.get_chat(raw_id)
                cid = chat_identifier(chat)
            except Exception:
                await callback_query.answer("Chat not found!", show_alert=True)
                return
            if uid not in selected_chats:
                selected_chats[uid] = set()
            if cid in selected_chats[uid]:
                selected_chats[uid].discard(cid)
                await callback_query.answer("Removed from selection")
            else:
                selected_chats[uid].add(cid)
                await callback_query.answer("Added to selection")
            # Rebuild page - extract current page from message text
            page = 0
            import re
            m = re.search(r"Page (\d+)", msg.text or "")
            if m:
                page = int(m.group(1)) - 1
            text, markup = await build_groups_page(uid, page)
            if text and markup:
                await msg.edit_text(text, reply_markup=markup)
            return

        if data.startswith("sel_page_"):
            page = int(data.split("_", 2)[2])
            text, markup = await build_groups_page(uid, page)
            if text and markup:
                await msg.edit_text(text, reply_markup=markup)
            await callback_query.answer("")

    # --- Login handler ---
    @bot.on_message(filters.text & filters.private)
    async def login_handler(client, message: Message):
        uid = message.from_user.id
        if uid not in user_steps:
            return
        step = user_steps[uid].get("step")

        if step == "awaiting_phone":
            phone = message.text.replace(" ", "")
            await message.reply_text("Sending login code...")
            temp_client = Client(
                f"temp_{uid}",
                api_id=API_ID,
                api_hash=API_HASH,
                in_memory=True,
                proxy=proxy,
            )
            await temp_client.connect()
            try:
                code_info = await temp_client.send_code(phone)
                user_steps[uid].update({
                    "step": "awaiting_code",
                    "phone": phone,
                    "client": temp_client,
                    "hash": code_info.phone_code_hash,
                })
                await message.reply_text("Code sent! Enter it (e.g. 1 2 3 4 5).")
            except Exception as e:
                await message.reply_text(f"Error: {e}\n/connect to retry.")
                user_steps.pop(uid, None)

        elif step == "awaiting_code":
            code = message.text.replace(" ", "")
            temp_client = user_steps[uid]["client"]
            phone = user_steps[uid]["phone"]
            phone_hash = user_steps[uid]["hash"]
            try:
                await temp_client.sign_in(phone, phone_hash, code)
                await finalize_login(uid, temp_client, message)
            except SessionPasswordNeeded:
                user_steps[uid]["step"] = "awaiting_password"
                await message.reply_text("2FA enabled. Enter your Cloud Password:")
            except PhoneCodeInvalid:
                await message.reply_text("Invalid code. Try again.")
            except Exception as e:
                await message.reply_text(f"Failed: {e}")
                user_steps.pop(uid, None)

        elif step == "awaiting_password":
            temp_client = user_steps[uid]["client"]
            try:
                await temp_client.check_password(message.text)
                await finalize_login(uid, temp_client, message)
            except PasswordHashInvalid:
                await message.reply_text("Wrong password. Try again.")
            except Exception as e:
                await message.reply_text(f"Error: {e}")

    # --- Health check ---
    async def health_check():
        port = int(os.environ.get("PORT", 8080))
        async def handle(reader, writer):
            request = (await reader.read(1024)).decode("utf-8", errors="replace")
            if "GET /keepalive" in request:
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 9\r\n\r\nKEEPALIVE")
            else:
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
            await writer.drain()
            writer.close()
        server = await asyncio.start_server(handle, "0.0.0.0", port)
        await server.serve_forever()

    # --- Watchdog ---
    async def watchdog(restart_event):
        tick = 0
        while True:
            await asyncio.sleep(60)
            tick += 1
            try:
                me = await bot.get_me()
                await save_sessions()
                if tick % 5 == 0:
                    print(f"Watchdog: alive (uptime ~{tick} min, bots: {len(connected_clients)})", flush=True)
            except Exception as e:
                print(f"Watchdog: issue detected ({e}), restarting...", flush=True)
                restart_event.set()
                break

    # --- Keep alive (prevents Render free tier from sleeping) ---
    LIVENESS_URL = os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL")

    async def keep_alive():
        if not LIVENESS_URL:
            return
        import httpx as _httpx
        while True:
            await asyncio.sleep(600)
            try:
                async with _httpx.AsyncClient(timeout=15) as cl:
                    r = await cl.get(f"{LIVENESS_URL}/keepalive")
                    print(f"Keepalive: {r.status_code}", flush=True)
            except Exception as e:
                print(f"Keepalive failed: {e}", flush=True)

    # --- Bootstrap ---
    loop = asyncio.get_running_loop()
    default_handler = loop.get_exception_handler()
    def safe_handler(loop, context):
        exc = context.get("exception")
        if exc and ("Peer id invalid" in str(exc) or "ID not found" in str(exc)):
            return
        if default_handler:
            default_handler(loop, context)
    loop.set_exception_handler(safe_handler)

    await load_sessions()
    asyncio.create_task(health_check())
    if LIVENESS_URL:
        asyncio.create_task(keep_alive())
        print(f"Keepalive enabled -> {LIVENESS_URL}", flush=True)
    print("=== Media Cleaner v2 (whitelist) starting ===", flush=True)
    print("Starting bot...", flush=True)
    await bot.start()
    if not SESSION_STRING:
        exported = await bot.export_session_string()
        print(f"SESSION_STRING={exported}", flush=True)
        print("Add SESSION_STRING env var for faster restarts!", flush=True)
    print("Bot started!", flush=True)

    restart_event = asyncio.Event()
    asyncio.create_task(watchdog(restart_event))
    await restart_event.wait()


if __name__ == "__main__":
    while True:
        try:
            asyncio.run(main())
        except Exception as e:
            print(f"Bot crashed: {e}", flush=True)
            traceback.print_exc()
        print("Restarting in 5s...", flush=True)
        import time
        time.sleep(5)
