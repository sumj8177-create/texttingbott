import asyncio, json, uuid, time
import aiohttp, discord
from discord.ext import commands
from aiohttp import web

WEB_PORT = 8080
ADMIN_CHANNEL_ID = 1519122658308919428
ADMIN_COMMAND    = "!admin=panel"

# ── Per-token bot registry ────────────────────────────────────────────────────
_bot_registry: dict[str, dict] = {}
_registry_lock = asyncio.Lock()

sse_connections:  dict[str, list] = {}   # channel_id -> [Queue, ...]
dm_sse:           dict[str, list] = {}   # user_id    -> [Queue, ...]
admin_sse:        list            = []   # broadcast queues for admin events
extra_bots:       dict[str, dict] = {}   # custom bots added via sidebar

# Site-wide lockdown flag
site_locked = False

# Pending admin sessions (user_id -> expiry timestamp)
admin_sessions: dict[str, float] = {}

# ── Helper: get/create bot for a token ───────────────────────────────────────
async def get_bot(token: str) -> commands.Bot | None:
    async with _registry_lock:
        if token in _bot_registry:
            entry = _bot_registry[token]
            try:
                await asyncio.wait_for(entry["ready"].wait(), timeout=15)
            except asyncio.TimeoutError:
                return None
            return entry["bot"]

        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds           = True
        intents.members          = True   # needed for online presence
        intents.presences        = True

        bot = commands.Bot(command_prefix="!", intents=intents)
        ready_event = asyncio.Event()

        @bot.event
        async def on_ready():
            print(f"[Bot] Logged in as {bot.user} (token …{token[-6:]})")
            ready_event.set()

        @bot.event
        async def on_message(message):
            global site_locked
            channel_id = str(message.channel.id)

            # ── Admin panel trigger ──────────────────────────────────────────
            if (message.channel.id == ADMIN_CHANNEL_ID
                    and message.content.strip() == ADMIN_COMMAND):
                uid = str(message.author.id)
                admin_sessions[uid] = time.time() + 3 * 3600   # 3-hour pass
                payload = json.dumps({"type": "admin_unlock", "user_id": uid,
                                      "user": message.author.display_name})
                for q in list(admin_sse):
                    await q.put(payload)

            is_reply_to_bot = False
            mentions_bot    = False
            ref_data        = None
            if bot.is_ready():
                mentions_bot = bot.user in message.mentions
            if message.reference:
                try:
                    ref = message.reference.resolved
                    if ref and hasattr(ref, "author"):
                        is_reply_to_bot = ref.author == bot.user
                        ref_data = {"id": str(message.reference.message_id),
                                    "author": ref.author.display_name,
                                    "content": (ref.content or "")[:100]}
                except Exception:
                    pass

            payload = json.dumps({
                "id": str(message.id), "author": message.author.display_name,
                "content": message.content, "timestamp": message.created_at.isoformat(),
                "is_bot": message.author.bot, "is_reply_to_bot": is_reply_to_bot,
                "mentions_bot": mentions_bot, "notify": is_reply_to_bot or mentions_bot,
                "reference": ref_data,
            })
            if channel_id in sse_connections:
                for q in list(sse_connections[channel_id]):
                    await q.put(payload)

            # DM stream
            if isinstance(message.channel, discord.DMChannel):
                uid = str(message.author.id)
                if uid in dm_sse:
                    for q in list(dm_sse[uid]):
                        await q.put(payload)

            await bot.process_commands(message)

        @bot.command(name="status")
        async def cmd_status(ctx):
            guilds  = len(bot.guilds)
            members = sum(g.member_count or 0 for g in bot.guilds)
            chans   = sum(len(g.text_channels) for g in bot.guilds)
            lines = [
                f"🟢 **{bot.user.name}** is online", "",
                f"📡 **Servers:** {guilds}",
                f"👥 **Members:** {members}",
                f"💬 **Text channels:** {chans}", "",
                "⚠️ *If the bot appears offline, check Privileged Intents in the Developer Portal.*",
            ]
            await ctx.send("\n".join(lines))

        task = asyncio.create_task(_run_bot(bot, token))
        _bot_registry[token] = {"bot": bot, "ready": ready_event, "task": task}
        try:
            await asyncio.wait_for(ready_event.wait(), timeout=15)
        except asyncio.TimeoutError:
            return None
        return bot

async def _run_bot(bot: commands.Bot, token: str):
    try:
        await bot.start(token)
    except discord.LoginFailure:
        print(f"[Bot] Invalid token …{token[-6:]}")
    except Exception as e:
        print(f"[Bot] Error: {e}")
    finally:
        async with _registry_lock:
            _bot_registry.pop(token, None)

# ── Extract token from request ─────────────────────────────────────────────
def req_token(request: web.Request) -> str:
    return request.headers.get("X-Bot-Token", "").strip()

# ── REST helpers ──────────────────────────────────────────────────────────────
async def discord_rest_send(token, channel_id, content, reply_to_id=None):
    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
    payload = {"content": content}
    if reply_to_id:
        payload["message_reference"] = {"message_id": str(reply_to_id)}
    async with aiohttp.ClientSession() as s:
        async with s.post(f"https://discord.com/api/v10/channels/{channel_id}/messages",
                          headers=headers, json=payload) as r:
            d = await r.json()
            return {"success": True} if r.status in (200, 201) else {"error": d.get("message", "Error")}

async def validate_bot_token(token):
    async with aiohttp.ClientSession() as s:
        async with s.get("https://discord.com/api/v10/users/@me",
                         headers={"Authorization": f"Bot {token}"}) as r:
            if r.status == 200:
                return (await r.json()).get("username")
    return None

# ── Route handlers ────────────────────────────────────────────────────────────
async def handle_root(request):
    from pathlib import Path
    return web.Response(body=Path(__file__).with_name("dashboard.html").read_bytes(),
                        content_type="text/html")

async def handle_policy(request):
    from pathlib import Path
    try:
        return web.Response(body=Path(__file__).with_name("policy.html").read_bytes(),
                            content_type="text/html")
    except FileNotFoundError:
        return web.Response(text="Policy page not found", status=404)

async def handle_status(request):
    token = req_token(request)
    if not token:
        return web.json_response({"online": False, "error": "No token"})
    bot = await get_bot(token)
    if bot and bot.is_ready():
        return web.json_response({"online": True, "username": str(bot.user)})
    return web.json_response({"online": False})

async def handle_guilds(request):
    token = req_token(request)
    bot = await get_bot(token) if token else None
    if not bot:
        return web.json_response([])
    return web.json_response([{"id": str(g.id), "name": g.name} for g in bot.guilds])

async def handle_channels(request):
    token = req_token(request)
    bot = await get_bot(token) if token else None
    if not bot:
        return web.json_response([])
    guild = bot.get_guild(int(request.match_info["guild_id"]))
    if not guild:
        return web.json_response([])
    return web.json_response([{"id": str(c.id), "name": c.name} for c in guild.text_channels])

# ── Online members for a guild ────────────────────────────────────────────────
async def handle_online_members(request):
    token = req_token(request)
    bot = await get_bot(token) if token else None
    if not bot:
        return web.json_response([])
    guild = bot.get_guild(int(request.match_info["guild_id"]))
    if not guild:
        return web.json_response([])
    online = []
    for m in guild.members:
        if m.status in (discord.Status.online, discord.Status.idle, discord.Status.dnd):
            online.append({
                "id": str(m.id),
                "name": m.display_name,
                "status": str(m.status),
                "is_bot": m.bot,
            })
    return web.json_response(online)

# ── DM list & history ─────────────────────────────────────────────────────────
async def handle_dm_list(request):
    token = req_token(request)
    bot = await get_bot(token) if token else None
    if not bot:
        return web.json_response({"error": "Not authenticated"}, status=401)
    dms = []
    for ch in bot.private_channels:
        if isinstance(ch, discord.DMChannel) and ch.recipient:
            dms.append({
                "id": str(ch.recipient.id),
                "name": ch.recipient.display_name,
                "channel_id": str(ch.id),
            })
    return web.json_response(dms)

async def handle_dm_history(request):
    token = req_token(request)
    bot = await get_bot(token) if token else None
    if not bot:
        return web.json_response({"error": "Not authenticated"}, status=401)
    user_id = int(request.match_info["user_id"])
    try:
        user = await bot.fetch_user(user_id)
        dm_ch = await user.create_dm()
        msgs = []
        async for msg in dm_ch.history(limit=50):
            msgs.append({
                "id": str(msg.id), "author": msg.author.display_name,
                "content": msg.content, "timestamp": msg.created_at.isoformat(),
                "is_bot": msg.author.bot, "is_reply_to_bot": False,
                "mentions_bot": False, "notify": False, "reference": None,
            })
        msgs.reverse()
        return web.json_response(msgs)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def handle_dm_events(request):
    user_id = request.match_info["user_id"]
    queue = asyncio.Queue()
    dm_sse.setdefault(user_id, []).append(queue)
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream",
                                        "Cache-Control": "no-cache",
                                        "X-Accel-Buffering": "no"})
    await resp.prepare(request)
    try:
        while True:
            try:
                data = await asyncio.wait_for(queue.get(), timeout=25)
                await resp.write(f"data: {data}\n\n".encode())
                await resp.drain()
            except asyncio.TimeoutError:
                await resp.write(b": ping\n\n")
                await resp.drain()
    except Exception:
        pass
    finally:
        try:
            dm_sse[user_id].remove(queue)
        except (KeyError, ValueError):
            pass
    return resp

async def handle_dm_send(request):
    token = req_token(request)
    body  = await request.json()
    user_id = int(body.get("user_id", 0))
    content = body.get("message", "").strip()
    if not content:
        return web.json_response({"error": "Empty message"}, status=400)
    bot = await get_bot(token) if token else None
    if not bot:
        return web.json_response({"error": "Not authenticated"}, status=401)
    try:
        user = await bot.fetch_user(user_id)
        dm_ch = await user.create_dm()
        await dm_ch.send(content)
        return web.json_response({"success": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

# ── Message history ───────────────────────────────────────────────────────────
async def handle_history(request):
    token = req_token(request)
    bot = await get_bot(token) if token else None
    if not bot:
        return web.json_response({"error": "Not authenticated"}, status=401)
    channel = bot.get_channel(int(request.match_info["channel_id"]))
    if not channel:
        return web.json_response({"error": "Channel not found"}, status=404)
    try:
        msgs = []
        async for msg in channel.history(limit=50):
            ref_data, is_reply_to_bot = None, False
            mentions_bot = bot.user in msg.mentions if bot.is_ready() else False
            if msg.reference:
                try:
                    ref = msg.reference.resolved
                    if ref and hasattr(ref, "author"):
                        is_reply_to_bot = ref.author == bot.user
                        ref_data = {"id": str(msg.reference.message_id),
                                    "author": ref.author.display_name,
                                    "content": (ref.content or "")[:100]}
                except Exception:
                    pass
            msgs.append({"id": str(msg.id), "author": msg.author.display_name,
                         "content": msg.content, "timestamp": msg.created_at.isoformat(),
                         "is_bot": msg.author.bot, "is_reply_to_bot": is_reply_to_bot,
                         "mentions_bot": mentions_bot, "notify": is_reply_to_bot or mentions_bot,
                         "reference": ref_data})
        msgs.reverse()
        return web.json_response(msgs)
    except discord.Forbidden:
        return web.json_response({"error": "Missing Read Message History permission"}, status=403)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def handle_events(request):
    channel_id = request.match_info["channel_id"]
    queue = asyncio.Queue()
    sse_connections.setdefault(channel_id, []).append(queue)
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream",
                                       "Cache-Control": "no-cache",
                                       "X-Accel-Buffering": "no"})
    await resp.prepare(request)
    try:
        while True:
            try:
                data = await asyncio.wait_for(queue.get(), timeout=25)
                await resp.write(f"data: {data}\n\n".encode())
                await resp.drain()
            except asyncio.TimeoutError:
                await resp.write(b": ping\n\n")
                await resp.drain()
    except Exception:
        pass
    finally:
        try:
            sse_connections[channel_id].remove(queue)
        except (KeyError, ValueError):
            pass
    return resp

async def handle_send(request):
    token  = req_token(request)
    body   = await request.json()
    chan_id = int(body.get("channel_id", 0))
    message = body.get("message", "").strip()
    bot_id  = body.get("bot_id", "main")
    if not message:
        return web.json_response({"error": "Empty message"}, status=400)
    if site_locked:
        return web.json_response({"error": "Site is locked down"}, status=403)
    if bot_id == "main":
        bot = await get_bot(token) if token else None
        if not bot:
            return web.json_response({"error": "Not authenticated"}, status=401)
        channel = bot.get_channel(chan_id)
        if not channel:
            return web.json_response({"error": "Channel not found"}, status=404)
        try:
            await channel.send(message)
            return web.json_response({"success": True})
        except discord.Forbidden:
            return web.json_response({"error": "Missing Send Messages permission"}, status=403)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)
    if bot_id not in extra_bots:
        return web.json_response({"error": "Bot not found"}, status=404)
    result = await discord_rest_send(extra_bots[bot_id]["token"], chan_id, message)
    return web.json_response(result, status=200 if result.get("success") else 500)

async def handle_reply(request):
    token   = req_token(request)
    body    = await request.json()
    chan_id = int(body.get("channel_id", 0))
    msg_id  = int(body.get("message_id", 0))
    content = body.get("content", "").strip()
    bot_id  = body.get("bot_id", "main")
    if not content:
        return web.json_response({"error": "Empty message"}, status=400)
    if site_locked:
        return web.json_response({"error": "Site is locked down"}, status=403)
    if bot_id == "main":
        bot = await get_bot(token) if token else None
        if not bot:
            return web.json_response({"error": "Not authenticated"}, status=401)
        channel = bot.get_channel(chan_id)
        if not channel:
            return web.json_response({"error": "Channel not found"}, status=404)
        try:
            target = await channel.fetch_message(msg_id)
            await target.reply(content)
            return web.json_response({"success": True})
        except discord.NotFound:
            return web.json_response({"error": "Original message not found"}, status=404)
        except discord.Forbidden:
            return web.json_response({"error": "Missing reply permission"}, status=403)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)
    if bot_id not in extra_bots:
        return web.json_response({"error": "Bot not found"}, status=404)
    result = await discord_rest_send(extra_bots[bot_id]["token"], chan_id, content, reply_to_id=msg_id)
    return web.json_response(result, status=200 if result.get("success") else 500)

# ── Custom bots ───────────────────────────────────────────────────────────────
async def handle_bots_list(request):
    return web.json_response([{"id": k, "name": v["name"], "username": v["username"]}
                               for k, v in extra_bots.items()])

async def handle_bots_add(request):
    body  = await request.json()
    token = body.get("token", "").strip()
    name  = body.get("name", "").strip() or "Custom Bot"
    if not token:
        return web.json_response({"error": "No token provided"}, status=400)
    username = await validate_bot_token(token)
    if not username:
        return web.json_response({"error": "Invalid token"}, status=401)
    bid = str(uuid.uuid4())
    extra_bots[bid] = {"name": name, "token": token, "username": username}
    return web.json_response({"success": True, "id": bid, "username": username})

async def handle_bots_delete(request):
    bid = request.match_info["bot_id"]
    if bid in extra_bots:
        del extra_bots[bid]
    return web.json_response({"success": True})

# ── Admin endpoints ───────────────────────────────────────────────────────────
async def handle_admin_check(request):
    """Verify if caller has a live admin session."""
    uid = request.headers.get("X-Admin-User", "")
    if not uid:
        return web.json_response({"admin": False})
    expiry = admin_sessions.get(uid, 0)
    if time.time() < expiry:
        return web.json_response({"admin": True, "expires": int(expiry)})
    admin_sessions.pop(uid, None)
    return web.json_response({"admin": False})

async def handle_admin_lock(request):
    global site_locked
    uid = request.headers.get("X-Admin-User", "")
    if not uid or time.time() > admin_sessions.get(uid, 0):
        return web.json_response({"error": "Unauthorized"}, status=403)
    site_locked = True
    broadcast = json.dumps({"type": "lockdown", "locked": True})
    for q in list(admin_sse):
        await q.put(broadcast)
    return web.json_response({"success": True, "locked": True})

async def handle_admin_unlock(request):
    global site_locked
    uid = request.headers.get("X-Admin-User", "")
    if not uid or time.time() > admin_sessions.get(uid, 0):
        return web.json_response({"error": "Unauthorized"}, status=403)
    site_locked = False
    broadcast = json.dumps({"type": "lockdown", "locked": False})
    for q in list(admin_sse):
        await q.put(broadcast)
    return web.json_response({"success": True, "locked": False})

async def handle_admin_status(request):
    return web.json_response({"locked": site_locked})

async def handle_admin_events(request):
    """SSE stream for admin events (lockdown changes, panel unlock)."""
    queue = asyncio.Queue()
    admin_sse.append(queue)
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream",
                                       "Cache-Control": "no-cache",
                                       "X-Accel-Buffering": "no"})
    await resp.prepare(request)
    try:
        while True:
            try:
                data = await asyncio.wait_for(queue.get(), timeout=25)
                await resp.write(f"data: {data}\n\n".encode())
                await resp.drain()
            except asyncio.TimeoutError:
                await resp.write(b": ping\n\n")
                await resp.drain()
    except Exception:
        pass
    finally:
        try:
            admin_sse.remove(queue)
        except ValueError:
            pass
    return resp

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    app = web.Application()
    app.router.add_get("/",                          handle_root)
    app.router.add_get("/policy",                    handle_policy)
    app.router.add_get("/status",                    handle_status)
    app.router.add_get("/guilds",                    handle_guilds)
    app.router.add_get("/channels/{guild_id}",       handle_channels)
    app.router.add_get("/members/{guild_id}",        handle_online_members)
    app.router.add_get("/history/{channel_id}",      handle_history)
    app.router.add_get("/events/{channel_id}",       handle_events)
    app.router.add_post("/send",                     handle_send)
    app.router.add_post("/reply",                    handle_reply)
    app.router.add_get("/bots",                      handle_bots_list)
    app.router.add_post("/bots",                     handle_bots_add)
    app.router.add_delete("/bots/{bot_id}",          handle_bots_delete)
    # DM routes
    app.router.add_get("/dms",                       handle_dm_list)
    app.router.add_get("/dms/{user_id}/history",     handle_dm_history)
    app.router.add_get("/dms/{user_id}/events",      handle_dm_events)
    app.router.add_post("/dms/send",                 handle_dm_send)
    # Admin routes
    app.router.add_get("/admin/check",               handle_admin_check)
    app.router.add_get("/admin/status",              handle_admin_status)
    app.router.add_post("/admin/lock",               handle_admin_lock)
    app.router.add_post("/admin/unlock",             handle_admin_unlock)
    app.router.add_get("/admin/events",              handle_admin_events)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", WEB_PORT).start()
    print(f"\n🚀  Bot Dashboard running → http://localhost:{WEB_PORT}")
    print(f"    To open Admin Panel: say  {ADMIN_COMMAND}  in channel {ADMIN_CHANNEL_ID}\n")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
