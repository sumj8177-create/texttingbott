import asyncio, json, uuid, os
import aiohttp, discord
from discord.ext import commands
from aiohttp import web

# Railway injects PORT automatically; fall back to 8080 for local dev
WEB_PORT = int(os.environ.get("PORT", 8080))

# ── Per-token bot registry ────────────────────────────────────────────────────
# Maps token -> {"bot": commands.Bot, "ready": asyncio.Event, "task": Task}
_bot_registry: dict[str, dict] = {}
_registry_lock = asyncio.Lock()

sse_connections: dict[str, list] = {}   # channel_id -> [Queue, ...]
extra_bots:      dict[str, dict] = {}   # custom bots added via sidebar

# ── Helper: get/create bot for a token ───────────────────────────────────────
async def get_bot(token: str) -> commands.Bot | None:
    """Return a running, ready Bot for the given token. Creates one if needed."""
    async with _registry_lock:
        if token in _bot_registry:
            entry = _bot_registry[token]
            # Wait up to 15 s for it to become ready
            try:
                await asyncio.wait_for(entry["ready"].wait(), timeout=15)
            except asyncio.TimeoutError:
                return None
            return entry["bot"]

        # First time we've seen this token — spin up a bot
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True

        bot = commands.Bot(command_prefix="!", intents=intents)
        ready_event = asyncio.Event()

        @bot.event
        async def on_ready():
            print(f"[Bot] Logged in as {bot.user} (token …{token[-6:]})")
            ready_event.set()

        @bot.event
        async def on_message(message):
            channel_id = str(message.channel.id)
            is_reply_to_bot, mentions_bot, ref_data = False, False, None
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
            await bot.process_commands(message)

        @bot.command(name="status")
        async def cmd_status(ctx):
            """!status — posts a formatted status embed for this bot."""
            guilds  = len(bot.guilds)
            members = sum(g.member_count or 0 for g in bot.guilds)
            chans   = sum(len(g.text_channels) for g in bot.guilds)
            lines = [
                f"🟢 **{bot.user.name}** is online",
                f"",
                f"📡 **Servers:** {guilds}",
                f"👥 **Members:** {members}",
                f"💬 **Text channels:** {chans}",
                f"",
                f"⚠️ *If the bot appears offline, check that all three Privileged Intents are enabled in the Developer Portal.*",
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

# ── Extract token from request ────────────────────────────────────────────────
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
    return web.Response(body=Path(__file__).with_name("policy.html").read_bytes(),
                        content_type="text/html")

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
    print(f"[Bots] Added custom bot: {username} ({name})")
    return web.json_response({"success": True, "id": bid, "username": username})

async def handle_bots_delete(request):
    bid = request.match_info["bot_id"]
    if bid in extra_bots:
        print(f"[Bots] Removed: {extra_bots[bid]['username']}")
        del extra_bots[bid]
    return web.json_response({"success": True})

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    app = web.Application()
    app.router.add_get("/",                     handle_root)
    app.router.add_get("/policy",               handle_policy)
    app.router.add_get("/status",               handle_status)
    app.router.add_get("/guilds",               handle_guilds)
    app.router.add_get("/channels/{guild_id}",  handle_channels)
    app.router.add_get("/history/{channel_id}", handle_history)
    app.router.add_get("/events/{channel_id}",  handle_events)
    app.router.add_post("/send",                handle_send)
    app.router.add_post("/reply",               handle_reply)
    app.router.add_get("/bots",                 handle_bots_list)
    app.router.add_post("/bots",                handle_bots_add)
    app.router.add_delete("/bots/{bot_id}",     handle_bots_delete)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", WEB_PORT).start()
    print(f"\n🚀  Bot Dashboard running on port {WEB_PORT}")
    print(f"    Open your Railway URL and paste your bot token in the login screen.\n")

    # Keep running indefinitely (bots are spun up on demand)
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())