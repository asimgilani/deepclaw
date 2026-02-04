"""
Simplified server using Deepgram Voice Agent API with OpenClaw as custom LLM.

Deepgram handles: Flux STT, Aura-2 TTS, turn-taking, barge-in
OpenClaw handles: LLM responses via /v1/chat/completions
This server: bridges Twilio <-> Deepgram Voice Agent API AND proxies LLM requests to OpenClaw
"""

import asyncio
import base64
import json
import logging
import os
import re
import secrets

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import Response, StreamingResponse
import websockets
import httpx

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Configuration
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")
OPENCLAW_GATEWAY_URL = os.getenv("OPENCLAW_GATEWAY_URL", "http://127.0.0.1:18789")
OPENCLAW_GATEWAY_TOKEN = os.getenv("OPENCLAW_GATEWAY_TOKEN", "")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))

# Generate a random proxy secret on startup (Deepgram will send this back to us)
PROXY_SECRET = os.getenv("PROXY_SECRET", secrets.token_hex(16))

DEEPGRAM_AGENT_URL = "wss://agent.deepgram.com/v1/agent/converse"

app = FastAPI(title="deepclaw-voice-agent")


def strip_markdown(text: str) -> str:
    """Strip markdown formatting for voice output."""
    # Remove code blocks
    text = re.sub(r'```[\s\S]*?```', '', text)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    # Remove bold/italic
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    text = re.sub(r'\*([^*]+)\*', r'\1', text)
    text = re.sub(r'__([^_]+)__', r'\1', text)
    text = re.sub(r'_([^_]+)_', r'\1', text)
    # Remove headers
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # Remove bullet points and numbered lists
    text = re.sub(r'^\s*[-*+]\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*\d+\.\s+', '', text, flags=re.MULTILINE)
    # Remove links, keep text
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    # Remove images
    text = re.sub(r'!\[([^\]]*)\]\([^)]+\)', '', text)
    # Remove horizontal rules
    text = re.sub(r'^[-*_]{3,}\s*$', '', text, flags=re.MULTILINE)
    # Remove blockquotes
    text = re.sub(r'^\s*>\s+', '', text, flags=re.MULTILINE)
    # Remove common emojis (basic set)
    text = re.sub(r'[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF\U0001F1E0-\U0001F1FF\U00002702-\U000027B0\U0001F900-\U0001F9FF]+', '', text)
    # Collapse multiple newlines into spaces for voice
    text = re.sub(r'\n+', ' ', text)
    return text


# ============================================================================
# LLM Proxy - Deepgram calls this, we forward to local OpenClaw
# ============================================================================

@app.post("/v1/chat/completions")
async def proxy_chat_completions(request: Request):
    """
    Proxy LLM requests from Deepgram Voice Agent to local OpenClaw.
    This eliminates the need for a second ngrok tunnel.
    """
    # Auth disabled for debugging
    logger.info("LLM proxy request received")

    body = await request.json()

    # Force fast model for voice interactions
    body["model"] = "claude-haiku-4-5"

    stream = body.get("stream", False)
    logger.info(f"Proxying chat completion - stream={stream}, messages={len(body.get('messages', []))}")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {OPENCLAW_GATEWAY_TOKEN}",
    }

    async def stream_response():
        """Stream the response from OpenClaw, stripping markdown for voice."""
        chunk_count = 0
        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream(
                "POST",
                f"{OPENCLAW_GATEWAY_URL}/v1/chat/completions",
                json=body,
                headers=headers,
            ) as response:
                async for chunk in response.aiter_text():
                    chunk_count += 1
                    if chunk_count == 1:
                        logger.info("First chunk received from OpenClaw")

                    # Process SSE lines
                    for line in chunk.split('\n'):
                        if line.startswith('data: ') and line != 'data: [DONE]':
                            try:
                                data = json.loads(line[6:])
                                # Extract and clean content from delta
                                if 'choices' in data and data['choices']:
                                    delta = data['choices'][0].get('delta', {})
                                    if 'content' in delta and delta['content']:
                                        delta['content'] = strip_markdown(delta['content'])
                                yield f"data: {json.dumps(data)}\n\n"
                            except json.JSONDecodeError:
                                yield f"{line}\n\n"
                        elif line.strip():
                            yield f"{line}\n\n"

                logger.info(f"Stream complete: {chunk_count} chunks")

    if stream:
        return StreamingResponse(
            stream_response(),
            media_type="text/event-stream",
        )
    else:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{OPENCLAW_GATEWAY_URL}/v1/chat/completions",
                json=body,
                headers=headers,
            )
            return Response(
                content=response.content,
                status_code=response.status_code,
                media_type="application/json",
            )


# ============================================================================
# Agent Configuration
# ============================================================================

def get_agent_config(public_url: str) -> dict:
    """Build Deepgram Voice Agent configuration with OpenClaw as custom LLM."""

    # Point Deepgram to OUR proxy endpoint (same ngrok URL)
    llm_url = f"{public_url}/v1/chat/completions"

    return {
        "type": "Settings",
        "audio": {
            "input": {
                "encoding": "mulaw",
                "sample_rate": 8000,
            },
            "output": {
                "encoding": "mulaw",
                "sample_rate": 8000,
                "container": "none",
            },
        },
        "agent": {
            "language": "en",
            "listen": {
                "provider": {
                    "type": "deepgram",
                    "model": "flux-general-en",
                },
            },
            "think": {
                "provider": {
                    "type": "open_ai",
                    "model": "gpt-4o-mini",
                },
                "endpoint": {
                    "url": llm_url,
                },
                "prompt": "You are a helpful voice assistant on a phone call. Keep responses concise and conversational (1-3 sentences). Never use markdown, bullet points, numbered lists, or emojis - your responses will be spoken aloud.",
            },
            "speak": {
                "provider": {
                    "type": "deepgram",
                    "model": "aura-2-thalia-en",
                },
            },
            "greeting": "Hello! How can I help you?",
        },
    }


# ============================================================================
# Twilio Webhook & Media Stream
# ============================================================================

TWIML_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="wss://{host}/twilio/media" />
    </Connect>
</Response>"""


@app.post("/twilio/incoming")
async def twilio_incoming(request: Request):
    """Handle incoming Twilio call - returns TwiML to start media stream.

    Note: For production, add Twilio signature validation:
    https://www.twilio.com/docs/usage/security#validating-requests
    """
    host = request.headers.get("host", "localhost:8000")
    twiml = TWIML_TEMPLATE.format(host=host)
    logger.info(f"Incoming call, connecting to wss://{host}/twilio/media")
    return Response(content=twiml, media_type="application/xml")


@app.websocket("/twilio/media")
async def twilio_media_websocket(websocket: WebSocket):
    """Bridge Twilio media stream to Deepgram Voice Agent API."""
    await websocket.accept()
    logger.info("Twilio WebSocket connected")

    stream_sid: str | None = None
    deepgram_ws = None
    sender_task = None
    receiver_task = None

    # Audio buffer for batching
    audio_buffer = bytearray()
    BUFFER_SIZE = 20 * 160  # 20 messages * 160 bytes = 0.4 seconds at 8kHz mulaw

    async def send_to_deepgram():
        """Forward buffered audio from Twilio to Deepgram."""
        nonlocal audio_buffer
        while True:
            if len(audio_buffer) >= BUFFER_SIZE and deepgram_ws:
                chunk = bytes(audio_buffer[:BUFFER_SIZE])
                audio_buffer = audio_buffer[BUFFER_SIZE:]
                try:
                    await deepgram_ws.send(chunk)
                except Exception as e:
                    logger.error(f"Error sending to Deepgram: {e}")
                    break
            await asyncio.sleep(0.01)

    async def receive_from_deepgram():
        """Receive audio/events from Deepgram and send to Twilio."""
        nonlocal stream_sid
        while True:
            try:
                message = await deepgram_ws.recv()

                # Binary = audio data
                if isinstance(message, bytes):
                    if stream_sid:
                        payload = base64.b64encode(message).decode("utf-8")
                        media_msg = {
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"payload": payload},
                        }
                        await websocket.send_json(media_msg)

                # Text = JSON event
                else:
                    event = json.loads(message)
                    event_type = event.get("type", "")

                    if event_type == "Welcome":
                        logger.info("Connected to Deepgram Voice Agent")
                    elif event_type == "SettingsApplied":
                        logger.info("Agent settings applied")
                    elif event_type == "UserStartedSpeaking":
                        logger.debug("User started speaking")
                        # Clear any queued audio (barge-in)
                        if stream_sid:
                            await websocket.send_json({
                                "event": "clear",
                                "streamSid": stream_sid,
                            })
                    elif event_type == "AgentStartedSpeaking":
                        logger.debug("Agent started speaking")
                    elif event_type == "ConversationText":
                        role = event.get("role", "")
                        content = event.get("content", "")
                        logger.info(f"{role.capitalize()}: {content}")
                    elif event_type == "Error":
                        logger.error(f"Deepgram error: {event}")

            except websockets.exceptions.ConnectionClosed:
                logger.info("Deepgram connection closed")
                break
            except Exception as e:
                logger.error(f"Error receiving from Deepgram: {e}")
                break

    try:
        # Connect to Deepgram Voice Agent API
        deepgram_ws = await websockets.connect(
            DEEPGRAM_AGENT_URL,
            additional_headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"},
        )
        logger.info("Connected to Deepgram Voice Agent API")

        # Wait for stream to start to get the public URL
        while True:
            message = await websocket.receive_json()
            event = message.get("event")

            if event == "connected":
                logger.info("Twilio media stream connected")

            elif event == "start":
                stream_sid = message.get("streamSid")

                # Get the public URL from the websocket headers
                host = websocket.headers.get("host", "localhost:8000")
                public_url = f"https://{host}"

                logger.info(f"Stream started: {stream_sid}")
                logger.info(f"Public URL for LLM proxy: {public_url}")

                # Now send agent config with correct URL
                config = get_agent_config(public_url)
                await deepgram_ws.send(json.dumps(config))
                logger.info("Sent agent config")

                # Start background tasks
                sender_task = asyncio.create_task(send_to_deepgram())
                receiver_task = asyncio.create_task(receive_from_deepgram())
                break

        # Continue processing Twilio messages
        while True:
            message = await websocket.receive_json()
            event = message.get("event")

            if event == "media":
                # Decode and buffer audio
                payload = message.get("media", {}).get("payload", "")
                if payload:
                    audio_data = base64.b64decode(payload)
                    audio_buffer.extend(audio_data)

            elif event == "stop":
                logger.info("Stream stopped")
                break

    except WebSocketDisconnect:
        logger.info("Twilio WebSocket disconnected")
    except Exception as e:
        logger.error(f"Error in media WebSocket: {e}")
    finally:
        # Cleanup
        if sender_task:
            sender_task.cancel()
        if receiver_task:
            receiver_task.cancel()
        if deepgram_ws:
            await deepgram_ws.close()
        logger.info("Cleanup complete")


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok", "service": "deepclaw-voice-agent"}


def main():
    """Run the server."""
    import uvicorn

    # Validate required configuration
    if not DEEPGRAM_API_KEY:
        logger.error("DEEPGRAM_API_KEY not set. Get one at https://console.deepgram.com/")
        return
    if not OPENCLAW_GATEWAY_TOKEN:
        logger.error("OPENCLAW_GATEWAY_TOKEN not set. Generate with: openssl rand -hex 32")
        return

    logger.info(f"Starting deepclaw voice agent server on {HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
