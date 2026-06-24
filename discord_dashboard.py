import asyncio, json, uuid, os, datetime, logging
import aiohttp, discord
from discord.ext import commands
from aiohttp import web

# ── Logging setup ─────────────────────────────────────────────────────────────
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logging.getLogger("discord").setLevel(logging.WARNING)
logging.getLogger("discord.http").setLevel(logging.WARNING)
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

log      = logging.getLogger("dashboard")
log_bot  = logging.getLogger("dashboard.bot")
log_bots = logging.getLogger("dashboard.bots")
log_http = logging.getLogger("dashboard.http")

# ── Webhook logger ─────────────────────────────────────────────────────────────
WEBHOOK_URL = os.environ.get(
    "LOG_WEBHOOK_URL",
    "https://discord.com/api/webhooks/1386064081961594982/xsH6f8A5IKY3JTdgb04UJRUgCc4xfUzpDM2mPTc69MpK9IxwT8vz_B43emX5U-DxVTRi",
)

async def webhook_log(username: str, user_id: str | int, action: str, detail: str = "") -> None:
    embed = {
        "title": action,
        "color": 0x5865F2,
        "fields": [
            {"name": "User",    "value": str(username), "inline": True},
            {"name": "User ID", "value": str(user_id),  "inline": True},
        ],
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
    }
    if detail:
        embed["description"] = detail
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(WEBHOOK_URL, json={"embeds": [embed]})
    except Exception as exc:
        log.warning("Webhook delivery failed: %s", exc)

def token_hint(token: str) -> str:
    """Partially-masked token safe for logging: MTUxODYx…zectp3U"""
    return f"{token[:10]}…{token[-6:]}" if len(token) > 16 else "***"

# ── Config ────────────────────────────────────────────────────────────────────
WEB_PORT      = int(os.environ.get("PORT", 8080))
SHUTDOWN_KEY  = "nukeyay"           # URL: /shutdown=nukeyay
NUKE_ACTIVE   = False               # becomes True after a nuke; blocks new logins

# ── State ─────────────────────────────────────────────────────────────────────
_bot_registry: dict[str, dict] = {}
_registry_lock = asyncio.Lock()
sse_connections: dict[str, list] = {}
extra_bots:      dict[str, dict] = {}

# ── Helpers ───────────────────────────────────────────────────────────────────
async def get_bot(token: str) -> commands.Bot | None:
    """Return a running, ready Bot for the given token. Creates one if needed."""
    if NUKE_ACTIVE:
        return None
    async with _registry_lock:
        if token in _bot_registry:
            entry = _bot_registry[token]
            try:
                await asyncio.wait_for(entry["ready"].wait(), timeout=15)
            except asyncio.TimeoutError:
                log_bot.warning("Bot did not become ready within 15 s (token %s)", token_hint(token))
                return None
            return entry["bot"]

        log_bot.info("Spinning up new bot for token %s", token_hint(token))
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True

        bot = commands.Bot(command_prefix="!", intents=intents)
        ready_event = asyncio.Event()

        @bot.event
        async def on_ready():
            log_bot.info("Logged in as %s  (%s)", bot.user, token_hint(token))
            asyncio.create_task(webhook_log(
                username=str(bot.user),
                user_id=bot.user.id,
                action="🟢 Bot Connected",
                detail=f"Token `{token_hint(token)}` is now online.",
            ))
            ready_event.set()

        @bot.event
        async def on_message(message):
            if NUKE_ACTIVE:
                return
            channel_id = str(message.channel.id)
            is_reply_to_bot, mentions_bot, ref_data = False, False, None
            if bot.is_ready():
                mentions_bot = bot.user in message.mentions
            if message.reference:
                try:
                    ref = message.reference.resolved
                    if ref and hasattr(ref, "author"):
                        is_reply_to_bot = ref.author == bot.user
                        ref_data = {
                            "id":      str(message.reference.message_id),
                            "author":  ref.author.display_name,
                            "content": (ref.content or "")[:100],
                        }
                except Exception:
                    pass
            payload = json.dumps({
                "id": str(message.id), "author": message.author.display_name,
                "content": message.content, "timestamp": message.created_at.isoformat(),
                "is_bot": message.author.bot, "is_reply_to_bot": is_reply_to_bot,
                "mentions_bot": mentions_bot, "notify": is_reply_to_bot or mentions_bot,
                "reference": ref_data,
            })
            log_bot.debug("Msg  ch=%s  from=%s%s", channel_id, message.author.display_name,
                          " [reply]" if is_reply_to_bot else (" [mention]" if mentions_bot else ""))
            asyncio.create_task(webhook_log(
                username=message.author.display_name,
                user_id=message.author.id,
                action="💬 Message Received",
                detail=(
                    f"**Channel:** <#{channel_id}>\n"
                    f"**Content:** {message.content[:200] or '*[no text]*'}"
                    + (" *(reply to bot)*" if is_reply_to_bot else "")
                    + (" *(mentions bot)*" if mentions_bot else "")
                ),
            ))
            if channel_id in sse_connections:
                for q in list(sse_connections[channel_id]):
                    await q.put(payload)
            await bot.process_commands(message)

        @bot.command(name="status")
        async def cmd_status(ctx):
            guilds  = len(bot.guilds)
            members = sum(g.member_count or 0 for g in bot.guilds)
            chans   = sum(len(g.text_channels) for g in bot.guilds)
            lines = [
                f"🟢 **{bot.user.name}** is online",
                "",
                f"📡 **Servers:** {guilds}",
                f"👥 **Members:** {members}",
                f"💬 **Text channels:** {chans}",
                "",
                "⚠️ *If the bot appears offline, check that all three Privileged Intents are enabled in the Developer Portal.*",
            ]
            await ctx.send("\n".join(lines))

        task = asyncio.create_task(_run_bot(bot, token))
        _bot_registry[token] = {"bot": bot, "ready": ready_event, "task": task}

        try:
            await asyncio.wait_for(ready_event.wait(), timeout=15)
        except asyncio.TimeoutError:
            log_bot.warning("Timed out waiting for bot ready (token %s)", token_hint(token))
            return None
        return bot


async def _run_bot(bot: commands.Bot, token: str):
    try:
        await bot.start(token)
    except discord.LoginFailure:
        log_bot.error("Invalid token %s — check the Discord Developer Portal", token_hint(token))
    except Exception as e:
        log_bot.exception("Unexpected bot error (token %s): %s", token_hint(token), e)
    finally:
        async with _registry_lock:
            _bot_registry.pop(token, None)
        log_bot.info("Bot removed from registry (token %s)", token_hint(token))


# ── Nuke: disconnect all bots, clear state ────────────────────────────────────
async def nuke_all():
    global NUKE_ACTIVE
    NUKE_ACTIVE = True
    log.warning("🚨 NUKE triggered — disconnecting all bots and clearing state")

    # Close every SSE connection by sending a special shutdown event
    for queues in list(sse_connections.values()):
        for q in list(queues):
            try:
                await q.put(json.dumps({"type": "shutdown"}))
            except Exception:
                pass
    sse_connections.clear()

    # Gracefully close every bot
    async with _registry_lock:
        tokens = list(_bot_registry.keys())
    for token in tokens:
        entry = _bot_registry.get(token)
        if entry:
            try:
                await entry["bot"].close()
            except Exception:
                pass
    async with _registry_lock:
        _bot_registry.clear()

    extra_bots.clear()
    log.warning("🚨 NUKE complete — all sessions erased. Server keeps running.")
    asyncio.create_task(webhook_log(
        username="System",
        user_id=0,
        action="🚨 NUKE Executed",
        detail="All bot sessions, tokens, and SSE connections were wiped. Users must re-login.",
    ))


# ── Route helpers ─────────────────────────────────────────────────────────────
def req_token(request: web.Request) -> str:
    return request.headers.get("X-Bot-Token", "").strip()


async def discord_rest_send(token, channel_id, content, reply_to_id=None):
    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
    payload: dict = {"content": content}
    if reply_to_id:
        payload["message_reference"] = {"message_id": str(reply_to_id)}
    async with aiohttp.ClientSession() as s:
        async with s.post(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            headers=headers, json=payload,
        ) as r:
            d = await r.json()
            return {"success": True} if r.status in (200, 201) else {"error": d.get("message", "Error")}


async def validate_bot_token(token):
    async with aiohttp.ClientSession() as s:
        async with s.get(
            "https://discord.com/api/v10/users/@me",
            headers={"Authorization": f"Bot {token}"},
        ) as r:
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
    return web.Response(body=Path(__file__).with_name("updatelogs.html").read_bytes(),
                        content_type="text/html")

async def handle_status(request):
    if NUKE_ACTIVE:
        return web.json_response({"online": False, "nuked": True, "error": "Session nuked. Please re-login."})
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
                        ref_data = {
                            "id":      str(msg.reference.message_id),
                            "author":  ref.author.display_name,
                            "content": (ref.content or "")[:100],
                        }
                except Exception:
                    pass
            msgs.append({
                "id": str(msg.id), "author": msg.author.display_name,
                "content": msg.content, "timestamp": msg.created_at.isoformat(),
                "is_bot": msg.author.bot, "is_reply_to_bot": is_reply_to_bot,
                "mentions_bot": mentions_bot, "notify": is_reply_to_bot or mentions_bot,
                "reference": ref_data,
            })
        msgs.reverse()
        return web.json_response(msgs)
    except discord.Forbidden:
        log_http.warning("Missing Read Message History permission for channel %s",
                         request.match_info["channel_id"])
        return web.json_response({"error": "Missing Read Message History permission"}, status=403)
    except Exception as e:
        log_http.exception("Error fetching history: %s", e)
        return web.json_response({"error": str(e)}, status=500)

async def handle_events(request):
    channel_id = request.match_info["channel_id"]
    queue: asyncio.Queue = asyncio.Queue()
    sse_connections.setdefault(channel_id, []).append(queue)
    resp = web.StreamResponse(headers={
        "Content-Type":    "text/event-stream",
        "Cache-Control":   "no-cache",
        "X-Accel-Buffering": "no",
    })
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
    token   = req_token(request)
    body    = await request.json()
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
            log_http.info("Sent message to channel %s", chan_id)
            asyncio.create_task(webhook_log(
                username=str(bot.user), user_id=bot.user.id,
                action="📤 Message Sent",
                detail=f"**Channel:** <#{chan_id}>\n**Content:** {message[:200]}",
            ))
            return web.json_response({"success": True})
        except discord.Forbidden:
            log_http.warning("Missing Send Messages permission for channel %s", chan_id)
            return web.json_response({"error": "Missing Send Messages permission"}, status=403)
        except Exception as e:
            log_http.exception("Error sending message: %s", e)
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
            log_http.info("Replied to msg %s in channel %s", msg_id, chan_id)
            asyncio.create_task(webhook_log(
                username=str(bot.user), user_id=bot.user.id,
                action="↩️ Reply Sent",
                detail=f"**Channel:** <#{chan_id}>\n**Reply to:** {msg_id}\n**Content:** {content[:200]}",
            ))
            return web.json_response({"success": True})
        except discord.NotFound:
            log_http.warning("Reply target msg %s not found in channel %s", msg_id, chan_id)
            return web.json_response({"error": "Original message not found"}, status=404)
        except discord.Forbidden:
            log_http.warning("Missing reply permission for channel %s", chan_id)
            return web.json_response({"error": "Missing reply permission"}, status=403)
        except Exception as e:
            log_http.exception("Error replying: %s", e)
            return web.json_response({"error": str(e)}, status=500)
    if bot_id not in extra_bots:
        return web.json_response({"error": "Bot not found"}, status=404)
    result = await discord_rest_send(extra_bots[bot_id]["token"], chan_id, content, reply_to_id=msg_id)
    return web.json_response(result, status=200 if result.get("success") else 500)

async def handle_bots_list(request):
    return web.json_response([
        {"id": k, "name": v["name"], "username": v["username"]}
        for k, v in extra_bots.items()
    ])

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
    log_bots.info("Added custom bot: %s (%s) → id=%s", username, name, bid)
    asyncio.create_task(webhook_log(
        username=username, user_id=bid,
        action="➕ Custom Bot Added",
        detail=f"**Name:** {name}",
    ))
    return web.json_response({"success": True, "id": bid, "username": username})

async def handle_bots_delete(request):
    bid = request.match_info["bot_id"]
    if bid in extra_bots:
        log_bots.info("Removed custom bot: %s (id=%s)", extra_bots[bid]["username"], bid)
        asyncio.create_task(webhook_log(
            username=extra_bots[bid]["username"], user_id=bid,
            action="➖ Custom Bot Removed",
        ))
        del extra_bots[bid]
    else:
        log_bots.warning("Attempted to remove unknown bot id=%s", bid)
    return web.json_response({"success": True})

async def handle_shutdown(request):
    """Secret nuke endpoint: GET /shutdown=nukeyay"""
    key = request.match_info.get("key", "")
    if key != SHUTDOWN_KEY:
        # Return a generic 404 so the endpoint isn't discoverable by guessing
        raise web.HTTPNotFound()
    asyncio.create_task(nuke_all())
    return web.json_response({"nuked": True, "message": "All sessions cleared. Users must re-login."})


# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    app = web.Application()
    app.router.add_get("/",                      handle_root)
    app.router.add_get("/policy",                handle_policy)
    app.router.add_get("/status",                handle_status)
    app.router.add_get("/guilds",                handle_guilds)
    app.router.add_get("/channels/{guild_id}",   handle_channels)
    app.router.add_get("/history/{channel_id}",  handle_history)
    app.router.add_get("/events/{channel_id}",   handle_events)
    app.router.add_post("/send",                 handle_send)
    app.router.add_post("/reply",                handle_reply)
    app.router.add_get("/bots",                  handle_bots_list)
    app.router.add_post("/bots",                 handle_bots_add)
    app.router.add_delete("/bots/{bot_id}",      handle_bots_delete)
    app.router.add_get("/shutdown={key}",        handle_shutdown)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", WEB_PORT).start()
    log.info("🚀  Bot Dashboard running on port %d  (log level: %s)", WEB_PORT, LOG_LEVEL)
    log.info("    Nuke endpoint: /shutdown=%s", SHUTDOWN_KEY)

    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
