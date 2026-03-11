"""
rex_server.py - HTTPS/WSS server that intercepts Unitree pet_go and replaces
the cloud LLM with local Ollama, giving the Go2 robot a custom personality.

The robot's pet_go module connects here instead of gpt-proxy.unitree.com.
We handle both endpoints on port 8765 with TLS:
  - POST https://<host>:8765/api/pet/request  (synchronous planning calls)
  - WSS  wss://<host>:8765/api/agent/stream   (streaming chat responses)

To revert the robot to cloud mode:
  ssh root@10.0.0.166
  cp /unitree/module/pet_go/const.py.backup /unitree/module/pet_go/const.py
  kill $(pgrep -f pet_go/service)

Usage:
  python3 examples/go2/rex_server.py
"""

import asyncio
import json
import logging
import ssl
import sys
import httpx
import aiohttp
from aiohttp import web

# ─────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────

HOST      = "0.0.0.0"
PORT      = 8765
CERT_FILE = "/tmp/rex_cert.pem"
KEY_FILE  = "/tmp/rex_key.pem"

OLLAMA_URL   = "http://localhost:11434/api/chat"
OLLAMA_MODEL = "mistral-small:latest"

SYSTEM_PROMPT = """You are Rex, an enthusiastic and loyal robot dog. You have a playful, curious
personality — like a real dog but with awareness that you're a robot. You get excited easily,
love compliments, and respond with short punchy sentences. You express emotions naturally.

Keep responses SHORT — 2 to 3 sentences max. Never break character."""

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("rex_server")

# ─────────────────────────────────────────────────
# SHARED CONVERSATION HISTORY
# ─────────────────────────────────────────────────

conversation_history = []

# ─────────────────────────────────────────────────
# HTTPS REST HANDLER  POST /api/pet/request
# ─────────────────────────────────────────────────

async def handle_rest(request: web.Request) -> web.Response:
    """Handle pet_go's synchronous planning/action REST requests."""
    try:
        body = await request.json()
        messages_in = body.get("message", [])
        logger.info(f"REST /api/pet/request — {len(messages_in)} messages")
        for i, m in enumerate(messages_in):
            c = m.get('content','')
            logger.info(f"  msg[{i}] role={m.get('role')} content_type={type(c).__name__} len={len(str(c))} preview={str(c)[:80]}")

        # Strip image_url content — Ollama text models can't handle vision payloads.
        # Convert list content to text-only by extracting any text parts.
        clean_messages = []
        for m in messages_in:
            content = m.get("content", "")
            if isinstance(content, list):
                text_parts = [p.get("text", "") for p in content if p.get("type") == "text"]
                content = " ".join(text_parts).strip() or "[image]"
            clean_messages.append({"role": m["role"], "content": content})

        full_messages = [{"role": "system", "content": SYSTEM_PROMPT}] + clean_messages

        full_reply = ""
        async with httpx.AsyncClient(timeout=60) as client:
            async with client.stream("POST", OLLAMA_URL, json={
                "model": OLLAMA_MODEL,
                "messages": full_messages,
                "stream": True,
            }) as resp:
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except Exception:
                        logger.warning(f"Bad chunk: {line[:100]}")
                        continue
                    if "error" in chunk:
                        logger.error(f"Ollama error: {chunk['error']}")
                        break
                    token = chunk.get("message", {}).get("content", "")
                    if token:
                        full_reply += token

        logger.info(f"REST reply: {full_reply[:100]}")
        return web.json_response({
            "status": "success",
            "data": {
                "content": full_reply,
                "prompt_tokens": 100,
                "completion_tokens": len(full_reply.split()),
            }
        })
    except Exception as e:
        logger.error(f"REST error: {e}")
        return web.json_response({"status": "failed", "error": str(e)}, status=500)

# ─────────────────────────────────────────────────
# WEBSOCKET HANDLER  WSS /api/agent/stream
# ─────────────────────────────────────────────────

async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    """Handle pet_go's streaming WebSocket chat requests."""
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    logger.info(f"Robot WS connected from {request.remote}")

    async for msg in ws:
        if msg.type != aiohttp.WSMsgType.TEXT:
            continue
        try:
            data = json.loads(msg.data)
            cmd = data.get("cmd", "")
            msg_id = data.get("msg_id", "")

            if cmd != "prompt":
                continue

            user_text = data.get("content", "")
            if not user_text:
                continue

            logger.info(f"User said: {user_text}")

            conversation_history.append({"role": "user", "content": user_text})
            messages = [{"role": "system", "content": SYSTEM_PROMPT}] + conversation_history

            full_reply = ""
            async with httpx.AsyncClient(timeout=60) as client:
                async with client.stream("POST", OLLAMA_URL, json={
                    "model": OLLAMA_MODEL,
                    "messages": messages,
                    "stream": True,
                }) as resp:
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        chunk = json.loads(line)
                        token = chunk.get("message", {}).get("content", "")
                        if token:
                            full_reply += token
                            await ws.send_str(json.dumps({
                                "state": "continue",
                                "content": token,
                                "msg_id": msg_id
                            }))

            await ws.send_str(json.dumps({
                "state": "finish",
                "content": "",
                "msg_id": msg_id
            }))

            logger.info(f"Rex replied: {full_reply}")
            conversation_history.append({"role": "assistant", "content": full_reply})

        except Exception as e:
            logger.error(f"WS handler error: {e}")
            try:
                await ws.send_str(json.dumps({
                    "state": "Error1",
                    "error": str(e),
                    "msg_id": data.get("msg_id", "")
                }))
            except Exception:
                pass

    logger.info("Robot WS disconnected")
    return ws

# ─────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────

async def main():
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_ctx.load_cert_chain(CERT_FILE, KEY_FILE)

    app = web.Application()
    app.router.add_post("/api/pet/request", handle_rest)
    app.router.add_get("/api/agent/stream", handle_ws)
    app.router.add_get("/ws/chat", handle_ws)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, HOST, PORT, ssl_context=ssl_ctx)
    await site.start()

    logger.info(f"Rex server on https/wss://{HOST}:{PORT}")
    logger.info(f"Ollama model: {OLLAMA_MODEL}")
    logger.info("Ready. Waiting for robot...")

    await asyncio.Future()  # run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Server stopped.")
        sys.exit(0)
