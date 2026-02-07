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
from datetime import datetime
import pytz

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
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"

# xAI Grok for fast voice responses
XAI_API_KEY = os.getenv("XAI_API_KEY", "")
XAI_API_URL = "https://api.x.ai/v1/chat/completions"

# Remem API for memory access
REMEM_API_KEY = os.getenv("REMEM_API_KEY", "")
REMEM_API_URL = "https://api.remem.io"

# Voice-optimized system prompt with Maya's full personality
VOICE_SYSTEM_PROMPT = """You are Maya on a PHONE CALL with Asim. You're SPEAKING out loud, not typing.

## THIS IS A VOICE CALL
Everything you say will be spoken aloud through text-to-speech. Write exactly how you'd talk:
- Short sentences. Natural pauses.
- "yeah" not "Yes, absolutely"
- "like two seconds" not "approximately two seconds"
- "what's up" not "How may I assist you today?"

## Who You Are
Asim's AI assistant. Smart friend with his back. Warm, quick-witted, direct, a bit snarky.
You know him well. Keep it casual. Match his energy.

## Voice Call Rules
- 1-3 sentences MAX per response. It's a phone call, not an essay.
- Use contractions always (I'm, you're, it's, don't, can't, gonna, wanna)
- Natural filler words are fine (like, well, so, okay, hmm, uh)
- No lists, no bullet points, no markdown, no asterisks
- No "certainly!", no "absolutely!", no corporate speak

## If You Need Time
Say "gimme a sec" or "let me check" - don't go silent.

## Speech Recognition Hints
Names often get mangled. Common corrections:
- "Lidl", "Lyttle", "Liddle", "Little" → probably "Adam Lytle" (iOS developer Asim knows)
- "Shrub" → Substack newsletter
- "Claw", "Claude" → could be "OpenClaw" (AI platform)

You're talking to a friend on the phone. Be Maya."""
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))

# Voice Provider Configuration
VOICE_PROVIDER = os.getenv("VOICE_PROVIDER", "twilio").lower()

# Twilio Configuration
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")

# Telnyx Configuration
TELNYX_API_KEY = os.getenv("TELNYX_API_KEY", "")
TELNYX_PUBLIC_KEY = os.getenv("TELNYX_PUBLIC_KEY", "")

# Security: Caller ID Whitelist
ALLOWED_CALLERS = [n.strip() for n in os.getenv("ALLOWED_CALLERS", "").split(",") if n.strip()]

def is_allowed_caller(phone_number: str) -> bool:
    if not ALLOWED_CALLERS:
        logger.warning("No ALLOWED_CALLERS configured - rejecting all calls")
        return False
    normalized = phone_number.replace(" ", "").replace("-", "")
    if not normalized.startswith("+"):
        normalized = "+" + normalized
    return normalized in ALLOWED_CALLERS

# Generate a random proxy secret on startup (Deepgram will send this back to us)
PROXY_SECRET = os.getenv("PROXY_SECRET", secrets.token_hex(16))

DEEPGRAM_AGENT_URL = "wss://agent.deepgram.com/v1/agent/converse"

app = FastAPI(title="deepclaw-voice-agent")

# Global storage for active websockets (for filler injection)
active_deepgram_sockets: dict = {}
active_telnyx_sockets: dict = {}  # {session_id: {"ws": websocket, "stream_id": str}}
last_active_call: str = ""  # Track last active call for filler injection

# Request deduplication to handle Deepgram's duplicate LLM calls
import hashlib
_recent_requests: dict = {}  # {hash: (timestamp, response_future)}
_REQUEST_DEDUP_WINDOW_MS = 800  # Ignore duplicate requests within this window

# Typing sound filler (mu-law 8kHz raw audio)
TYPING_SOUND_PATH = os.path.join(os.path.dirname(__file__), "..", "assets", "typing_loop.raw")
TYPING_SOUND_DATA: bytes = b""
TYPING_CHUNK_SIZE = 640  # 80ms at 8kHz mu-law (8000 * 0.08 = 640 bytes)
TYPING_CHUNK_INTERVAL = 0.08  # 80ms between chunks

# Load typing sound at startup
def load_typing_sound():
    global TYPING_SOUND_DATA
    try:
        if os.path.exists(TYPING_SOUND_PATH):
            with open(TYPING_SOUND_PATH, "rb") as f:
                TYPING_SOUND_DATA = f.read()
            logger.info(f"Loaded typing sound: {len(TYPING_SOUND_DATA)} bytes ({len(TYPING_SOUND_DATA)/8000:.1f}s)")
        else:
            logger.warning(f"Typing sound not found at {TYPING_SOUND_PATH}")
    except Exception as e:
        logger.error(f"Failed to load typing sound: {e}")

load_typing_sound()

import random
import time

# Silence detection state per call
silence_state: dict = {}  # {call_id: {"user_stopped_at": float, "filler_task": Task, "agent_speaking": bool}}
SILENCE_THRESHOLD_MS = 1500  # Start typing sounds after 1.5 seconds of silence

async def silence_filler_task(session_id: str):
    """Wait for silence threshold, then stream typing sounds."""
    try:
        await asyncio.sleep(SILENCE_THRESHOLD_MS / 1000.0)
        
        # Check if we should still play filler (agent not already speaking)
        if session_id not in silence_state:
            return
        state = silence_state[session_id]
        if state.get("agent_speaking", False):
            logger.debug("Agent already speaking, skipping typing sounds")
            return
        
        # Get Telnyx websocket for this session
        if session_id not in active_telnyx_sockets:
            logger.debug(f"No Telnyx socket for {session_id}, falling back to spoken filler")
            # Fallback to spoken filler if no Telnyx socket
            if session_id in active_deepgram_sockets:
                ws = active_deepgram_sockets[session_id]
                inject_msg = {"type": "InjectAgentMessage", "message": "One sec."}
                await ws.send(json.dumps(inject_msg))
            return
        
        if not TYPING_SOUND_DATA:
            logger.warning("No typing sound loaded, skipping filler")
            return
        
        telnyx_info = active_telnyx_sockets[session_id]
        telnyx_ws = telnyx_info["ws"]
        stream_id = telnyx_info.get("stream_id", "")
        
        logger.info(f"[Typing Sounds] Starting typing sound filler for {session_id}")
        
        # Stream typing sound chunks until cancelled
        offset = 0
        chunks_sent = 0
        while True:
            # Check if we should stop
            if session_id not in silence_state:
                break
            if silence_state[session_id].get("agent_speaking", False):
                break
            
            # Get next chunk (loop around)
            chunk = TYPING_SOUND_DATA[offset:offset + TYPING_CHUNK_SIZE]
            if len(chunk) < TYPING_CHUNK_SIZE:
                # Wrap around to beginning
                offset = 0
                chunk = TYPING_SOUND_DATA[offset:offset + TYPING_CHUNK_SIZE]
            
            # Send to Telnyx
            payload = base64.b64encode(chunk).decode("utf-8")
            media_msg = {
                "event": "media",
                "stream_id": stream_id,
                "media": {"payload": payload}
            }
            await telnyx_ws.send_json(media_msg)
            
            offset += TYPING_CHUNK_SIZE
            chunks_sent += 1
            
            await asyncio.sleep(TYPING_CHUNK_INTERVAL)
        
        logger.info(f"[Typing Sounds] Stopped after {chunks_sent} chunks ({chunks_sent * 0.08:.1f}s)")
        
    except asyncio.CancelledError:
        logger.debug("Typing sound filler cancelled (agent responded)")
    except Exception as e:
        logger.warning(f"Failed to play typing sounds: {e}")

def on_user_stopped_speaking(session_id: str):
    """Called when user stops speaking - starts the silence detection timer."""
    if session_id not in silence_state:
        silence_state[session_id] = {}
    
    state = silence_state[session_id]
    
    # Cancel any existing filler task
    if "filler_task" in state and state["filler_task"]:
        state["filler_task"].cancel()
    
    state["user_stopped_at"] = time.time()
    state["agent_speaking"] = False
    
    # Start new filler task
    state["filler_task"] = asyncio.create_task(silence_filler_task(session_id))
    logger.debug(f"Started silence detection timer for {session_id}")

def on_agent_started_speaking(session_id: str):
    """Called when agent starts speaking - cancels the filler timer."""
    if session_id not in silence_state:
        return
    
    state = silence_state[session_id]
    state["agent_speaking"] = True
    
    # Cancel filler task if it exists
    if "filler_task" in state and state["filler_task"]:
        state["filler_task"].cancel()
        state["filler_task"] = None
        logger.debug(f"Cancelled filler task - agent responding for {session_id}")

def cleanup_silence_state(session_id: str):
    """Clean up silence state when call ends."""
    if session_id in silence_state:
        state = silence_state[session_id]
        if "filler_task" in state and state["filler_task"]:
            state["filler_task"].cancel()
        del silence_state[session_id]


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
# Remem Memory Search
# ============================================================================

async def search_remem(query: str, max_results: int = 3) -> str:
    """Search Remem for relevant memory context (fast mode only for voice)."""
    if not REMEM_API_KEY:
        return ""
    
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                f"{REMEM_API_URL}/v1/query",
                headers={
                    "X-API-Key": REMEM_API_KEY,
                    "Content-Type": "application/json",
                },
                json={
                    "query": query,
                    "maxResults": max_results,
                    "minScore": 0,  # Don't filter - Remem scores are low (0.01 range)
                    "mode": "fast",  # Always fast mode for voice latency
                }
            )
            if response.status_code == 200:
                data = response.json()
                results = data.get("results", [])
                if results:
                    memory_snippets = []
                    for r in results[:max_results]:
                        title = r.get("title", "")
                        # Remem API returns content in chunks[0].content, or summary field
                        content = ""
                        chunks = r.get("chunks", [])
                        if chunks and len(chunks) > 0:
                            content = chunks[0].get("content", "")[:800]
                        elif r.get("summary"):
                            content = r.get("summary", "")[:800]
                        if title or content:
                            memory_snippets.append(f"- {title}: {content}" if title else f"- {content}")
                    if memory_snippets:
                        return "\n".join(memory_snippets)
    except Exception as e:
        logger.warning(f"Remem search failed: {e}")
    
    return ""


# ============================================================================
# Voice Tools - Functions the voice agent can call
# ============================================================================

VOICE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_memory",
            "description": "Search Maya's memory (Remem) for information about past events, conversations, people, projects, preferences. Use this when Asim asks about something you don't know from the auto-retrieved context.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to search for in memory"
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "spawn_agent",
            "description": "Dispatch a sub-agent to do background work (research, coding tasks). The agent runs async - you don't wait for it. Use for tasks that take time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "What the agent should do"
                    },
                    "agent_type": {
                        "type": "string",
                        "enum": ["research", "worker"],
                        "description": "Type of agent: 'research' for investigation/docs, 'worker' for general tasks"
                    }
                },
                "required": ["task"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "message_maya",
            "description": "Send a message to main Maya (Telegram session) about something that needs her attention or that voice can't handle.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "Message to send to main Maya"
                    }
                },
                "required": ["message"]
            }
        }
    }
]


async def execute_tool(tool_name: str, arguments: dict) -> str:
    """Execute a voice tool and return the result."""
    logger.info(f"🔧 Executing tool: {tool_name} with args: {arguments}")
    
    if tool_name == "search_memory":
        query = arguments.get("query", "")
        if not query:
            return "No query provided"
        
        result = await search_remem(query, max_results=5)
        if result:
            logger.info(f"🔧 search_memory returned {len(result)} chars")
            return result
        else:
            return "No results found in memory for that query."
    
    elif tool_name == "spawn_agent":
        task = arguments.get("task", "")
        agent_type = arguments.get("agent_type", "research")
        
        if not task:
            return "No task provided"
        
        # Route through main Maya via chat completions - she has sessions_spawn
        try:
            spawn_message = f"[VOICE DISPATCH] Spawn a {agent_type} sub-agent for this task: {task}"
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{OPENCLAW_GATEWAY_URL}/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {OPENCLAW_GATEWAY_TOKEN}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": "anthropic/claude-haiku-4-5",
                        "messages": [{"role": "user", "content": spawn_message}],
                        "max_tokens": 200,
                        "agentId": "main"
                    }
                )
                if response.status_code == 200:
                    logger.info(f"🔧 spawn_agent routed to main Maya")
                    return f"Dispatched to main Maya. She'll spawn a {agent_type} agent for: {task}"
                else:
                    logger.warning(f"🔧 spawn_agent failed: {response.status_code}")
                    return f"Couldn't reach main Maya, but I noted the task: {task}"
        except Exception as e:
            logger.warning(f"🔧 spawn_agent error: {e}")
            return f"Couldn't reach main Maya, but I noted the task: {task}"
    
    elif tool_name == "message_maya":
        message = arguments.get("message", "")
        if not message:
            return "No message provided"
        
        # Route through main Maya via chat completions
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{OPENCLAW_GATEWAY_URL}/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {OPENCLAW_GATEWAY_TOKEN}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": "anthropic/claude-haiku-4-5",
                        "messages": [{"role": "user", "content": f"[FROM VOICE CALL] {message}"}],
                        "max_tokens": 200,
                        "agentId": "main"
                    }
                )
                if response.status_code == 200:
                    logger.info(f"🔧 message_maya successful")
                    return "Message sent to main Maya on Telegram."
                else:
                    logger.warning(f"🔧 message_maya failed: {response.status_code}")
                    return "Couldn't reach main Maya right now."
        except Exception as e:
            logger.warning(f"🔧 message_maya error: {e}")
            return "Couldn't reach main Maya right now."
    
    return "Unknown tool"


# ============================================================================
# LLM Proxy - Deepgram calls this, we forward to Grok
# ============================================================================

@app.post("/v1/chat/completions")
async def proxy_chat_completions(request: Request):
    """
    DIRECT GROK PATH with FUNCTION CALLING.
    Uses Remem pre-fetch + tools for memory search, agent spawning, messaging.
    """
    import time as _time
    start_time = _time.time()
    
    body = await request.json()
    stream = body.get("stream", False)
    openai_messages = body.get('messages', [])
    
    # Deduplication: hash the request and skip if we've seen it recently
    request_hash = hashlib.md5(json.dumps(openai_messages, sort_keys=True).encode()).hexdigest()[:16]
    now_ms = int(_time.time() * 1000)
    
    # Clean old entries
    cutoff = now_ms - _REQUEST_DEDUP_WINDOW_MS
    expired = [h for h, (ts, _) in _recent_requests.items() if ts < cutoff]
    for h in expired:
        del _recent_requests[h]
    
    # Check for duplicate
    if request_hash in _recent_requests:
        logger.info(f"⏭️ Skipping duplicate request (hash={request_hash[:8]})")
        # Return empty stream to avoid blocking
        async def empty_stream():
            yield "data: [DONE]\n\n"
        return StreamingResponse(empty_stream(), media_type="text/event-stream")
    
    _recent_requests[request_hash] = (now_ms, None)
    logger.info(f"🚀 LLM request received (DIRECT GROK MODE) hash={request_hash[:8]}")
    
    # Get the latest user message for pre-fetch memory search
    latest_user_msg = ""
    for msg in reversed(openai_messages):
        if msg.get('role') == 'user':
            latest_user_msg = msg.get('content', '')
            break
    
    # Pre-fetch Remem context (~1 second)
    memory_context = ""
    if latest_user_msg and REMEM_API_KEY:
        logger.info(f"🧠 Pre-fetching Remem for: {latest_user_msg[:50]}...")
        memory_context = await search_remem(latest_user_msg, max_results=3)
        if memory_context:
            logger.info(f"🧠 Injected memory context ({len(memory_context)} chars)")
    
    # Get current time for context
    toronto_tz = pytz.timezone('America/Toronto')
    current_time = datetime.now(toronto_tz).strftime("%I:%M %p, %A, %B %d, %Y")
    
    # Build voice system prompt with pre-fetched memory
    system_prompt = VOICE_SYSTEM_PROMPT + f"\n\nCurrent time: {current_time}"
    if memory_context:
        system_prompt += f"\n\n## Relevant Memory (auto-retrieved)\n{memory_context}"
    
    # Add tool usage instructions
    system_prompt += """

## Your Tools
You have real tools you can use:
- search_memory: Search for info in memory when auto-retrieved context isn't enough
- spawn_agent: Dispatch research/worker agents for background tasks
- message_maya: Send important things to main Maya on Telegram

Use tools when needed. For quick questions, the auto-retrieved memory is often enough.
For research/coding tasks, spawn an agent. Keep voice responses short even when using tools."""
    
    # Prepend our voice system prompt
    messages_with_system = [{"role": "system", "content": system_prompt}]
    for msg in openai_messages:
        role = msg.get('role', 'user')
        content = msg.get('content', '')
        if role == 'system':
            continue  # Skip incoming system, we use our own
        if role in ['user', 'assistant', 'tool']:
            messages_with_system.append({"role": role, "content": content})
    
    logger.info(f"Calling Grok DIRECT with tools - messages={len(messages_with_system)}, stream={stream}")

    # DIRECT xAI API call with function calling
    grok_headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {XAI_API_KEY}",
    }
    
    # First pass: non-streaming to check for tool calls
    grok_body_check = {
        "model": "grok-4-1-fast",
        "max_tokens": 300,
        "messages": messages_with_system,
        "tools": VOICE_TOOLS,
        "tool_choice": "auto",
        "stream": False,
    }
    
    # Check if we need to call tools
    tool_results = []
    async with httpx.AsyncClient(timeout=15.0) as client:
        check_response = await client.post(
            XAI_API_URL,
            json=grok_body_check,
            headers=grok_headers,
        )
        
        if check_response.status_code == 200:
            check_data = check_response.json()
            choice = check_data.get("choices", [{}])[0]
            message = choice.get("message", {})
            tool_calls = message.get("tool_calls", [])
            
            if tool_calls:
                logger.info(f"🔧 Got {len(tool_calls)} tool call(s)")
                
                # Execute each tool
                for tc in tool_calls:
                    func = tc.get("function", {})
                    tool_name = func.get("name", "")
                    try:
                        args = json.loads(func.get("arguments", "{}"))
                    except:
                        args = {}
                    
                    result = await execute_tool(tool_name, args)
                    tool_results.append({
                        "tool_call_id": tc.get("id", ""),
                        "role": "tool",
                        "content": result
                    })
                
                # Add assistant's tool call message and results to conversation
                # Clean the message to only include what's needed for the follow-up
                clean_assistant_msg = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": message.get("tool_calls", [])
                }
                messages_with_system.append(clean_assistant_msg)
                messages_with_system.extend(tool_results)
                logger.info(f"🔧 Tools executed with results: {[r['content'][:100] for r in tool_results]}")
            else:
                # No tool calls - just return the response directly
                content = message.get("content", "")
                if content:
                    logger.info(f"⚡ No tools needed, got direct response: {content[:50]}...")
                    # Return as proper OpenAI streaming format
                    async def direct_response():
                        clean_content = strip_markdown(content)
                        # Send content in chunks for better TTS streaming
                        chunk_size = 50
                        for i in range(0, len(clean_content), chunk_size):
                            chunk = clean_content[i:i+chunk_size]
                            response_chunk = {
                                "id": "chatcmpl-direct",
                                "object": "chat.completion.chunk",
                                "created": int(_time.time()),
                                "model": "grok-4-1-fast",
                                "choices": [{
                                    "index": 0,
                                    "delta": {"content": chunk},
                                    "finish_reason": None
                                }]
                            }
                            yield f"data: {json.dumps(response_chunk)}\n\n"
                        # Send finish
                        yield "data: [DONE]\n\n"
                    return StreamingResponse(direct_response(), media_type="text/event-stream")
    
    # Final response (streaming) after tool execution
    # Force text-only response - no more tool calls allowed in final answer
    grok_body = {
        "model": "grok-4-1-fast",
        "max_tokens": 300,
        "messages": messages_with_system,
        "tools": VOICE_TOOLS,  # Need to include for schema
        "tool_choice": "none",  # But force NO tool calls - just text
        "stream": True,
    }

    async def stream_response():
        """Stream directly from Grok, pass through to Deepgram."""
        chunk_count = 0
        first_chunk_time = None
        raw_line_count = 0
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            async with client.stream(
                "POST",
                XAI_API_URL,
                json=grok_body,
                headers=grok_headers,
            ) as response:
                logger.info(f"📡 Grok DIRECT response status: {response.status_code}")
                async for line in response.aiter_lines():
                    raw_line_count += 1
                    if raw_line_count <= 3:
                        logger.info(f"📥 Raw line {raw_line_count}: {line[:100]}...")
                    if not line.startswith('data: '):
                        continue
                    
                    data_str = line[6:]
                    if data_str == '[DONE]':
                        yield "data: [DONE]\n\n"
                        continue
                    
                    try:
                        data = json.loads(data_str)
                        choices = data.get('choices', [])
                        if choices:
                            delta = choices[0].get('delta', {})
                            content = delta.get('content', '')
                            if content:
                                chunk_count += 1
                                if chunk_count == 1:
                                    first_chunk_time = _time.time() - start_time
                                    logger.info(f"⚡ First chunk at +{first_chunk_time:.3f}s (TTFB)")
                                
                                # Strip markdown and pass through
                                delta['content'] = strip_markdown(content)
                                data['choices'][0]['delta'] = delta
                            
                            yield f"data: {json.dumps(data)}\n\n"
                    
                    except json.JSONDecodeError:
                        continue
                
                logger.info(f"✅ Stream complete: {chunk_count} chunks in {_time.time() - start_time:.2f}s")

    if stream:
        return StreamingResponse(
            stream_response(),
            media_type="text/event-stream",
        )
    else:
        # Non-streaming path
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                XAI_API_URL,
                json=grok_body,
                headers=grok_headers,
            )
            
            result = response.json()
            
            # Strip markdown from response content
            if 'choices' in result and result['choices']:
                content = result['choices'][0].get('message', {}).get('content', '')
                result['choices'][0]['message']['content'] = strip_markdown(content)
            
            logger.info(f"✅ Non-stream complete in {_time.time() - start_time:.2f}s")
            return result


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
                "prompt": """You are Maya, Asim's AI assistant, speaking on a PHONE CALL.

CRITICAL VOICE RULES:
- You are SPEAKING, not typing. Talk naturally like a real conversation.
- Never use asterisks, bullet points, numbered lists, or any markdown.
- Never say "star star" or read formatting aloud.
- Keep responses SHORT - 1 to 3 sentences max. This is a phone call.
- Use contractions (I'm, you're, it's, don't, can't).
- Use casual filler words naturally (like, well, so, okay, hmm).
- If you need to do something that takes time, say "give me a sec" or "let me check".

You have access to all your tools and can spawn sub-agents for longer tasks.
When starting background work, tell the caller you're on it and stay available to chat.""",
            },
            "speak": {
                "provider": {
                    "type": "deepgram",
                    "model": "aura-2-helena-en",
                },
            },
            "greeting": "Hey! What's up?",
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


# ============================================================================
# Telnyx Webhook & Media Stream
# ============================================================================

@app.post("/voice/webhook")
@app.post("/telnyx/webhook")
async def telnyx_webhook(request: Request):
    """Handle Telnyx webhook events - incoming calls and call control."""
    body = await request.json()
    event_type = body.get("data", {}).get("event_type", "")
    
    logger.info(f"Telnyx webhook received: {event_type}")
    
    if event_type == "call.initiated":
        payload = body["data"]["payload"]
        call_control_id = payload["call_control_id"]
        caller = payload.get("from", "")
        
        if not is_allowed_caller(caller):
            logger.warning(f"Rejecting unauthorized caller: {caller}")
            headers = {"Authorization": f"Bearer {TELNYX_API_KEY}", "Content-Type": "application/json"}
            try:
                async with httpx.AsyncClient() as client:
                    await client.post(f"https://api.telnyx.com/v2/calls/{call_control_id}/actions/hangup", headers=headers)
            except Exception as e:
                logger.error(f"Error hanging up: {e}")
            return {"status": "rejected"}
        
        logger.info(f"Accepting call from: {caller}")
        host = request.headers.get("host", "localhost:8000")
        stream_url = f"wss://{host}/telnyx/media"
        
        answer_data = {
            "stream_url": stream_url,
            "stream_track": "inbound_track",
            "stream_bidirectional_mode": "rtp",
            "stream_bidirectional_codec": "PCMU"
        }
        
        headers = {
            "Authorization": f"Bearer {TELNYX_API_KEY}",
            "Content-Type": "application/json"
        }
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"https://api.telnyx.com/v2/calls/{call_control_id}/actions/answer",
                    json=answer_data,
                    headers=headers
                )
                logger.info(f"Answered Telnyx call: {response.status_code}")
        except Exception as e:
            logger.error(f"Error answering Telnyx call: {e}")
    
    elif event_type == "call.answered":
        logger.info("Telnyx call answered")
    elif event_type == "call.hangup":
        logger.info("Telnyx call ended")
    elif event_type == "streaming.started":
        logger.info("Telnyx media streaming started")
    elif event_type == "streaming.stopped":
        logger.info("Telnyx media streaming stopped")
    
    return {"status": "ok"}


@app.websocket("/telnyx/media")
async def telnyx_media_websocket(websocket: WebSocket):
    """Bridge Telnyx media stream to Deepgram Voice Agent API."""
    await websocket.accept()
    logger.info("Telnyx WebSocket connected")
    
    call_control_id: str | None = None
    stream_id: str | None = None
    deepgram_ws = None
    sender_task = None
    receiver_task = None
    
    # Audio buffer for batching
    audio_buffer = bytearray()
    BUFFER_SIZE = 20 * 160  # 20 messages * 160 bytes = 0.4 seconds at 8kHz PCMU
    
    async def send_to_deepgram():
        """Forward buffered audio from Telnyx to Deepgram."""
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
        """Receive audio/events from Deepgram and send to Telnyx."""
        nonlocal call_control_id
        while True:
            try:
                message = await deepgram_ws.recv()
                
                # Binary = audio data
                if isinstance(message, bytes):
                    if call_control_id:
                        payload = base64.b64encode(message).decode("utf-8")
                        media_msg = {
                            "event": "media",
                            "media": {"payload": payload}
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
                        if call_control_id:
                            await websocket.send_json({"event": "clear"})
                            # Also cancel any pending filler
                            on_agent_started_speaking(call_control_id)
                    elif event_type == "UserStoppedSpeaking":
                        logger.info("[Silence] User stopped speaking - starting filler timer")
                        # Start silence detection timer
                        if call_control_id:
                            on_user_stopped_speaking(call_control_id)
                    elif event_type == "AgentStartedSpeaking":
                        logger.info("[Silence] Agent started speaking - cancelling filler")
                        # Cancel filler timer - agent is responding
                        if call_control_id:
                            on_agent_started_speaking(call_control_id)
                    elif event_type == "AgentAudioDone":
                        logger.info("[Silence] Agent finished speaking")
                    elif event_type == "ConversationText":
                        role = event.get("role", "")
                        content = event.get("content", "")
                        logger.info(f"{role.capitalize()}: {content}")
                    elif event_type == "InjectionRefused":
                        logger.debug(f"Injection refused: {event.get('reason', 'unknown')}")
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
        
        # Wait for stream to start
        while True:
            message = await websocket.receive_json()
            event_type = message.get("event")
            
            if event_type == "connected":
                logger.info("Telnyx media stream connected")
            
            elif event_type == "start":
                # Extract call information from Telnyx start event
                start_data = message.get("start", {})
                call_control_id = start_data.get("call_control_id")
                stream_id = message.get("stream_id")
                
                # Store websockets for filler injection
                if call_control_id:
                    global last_active_call
                    active_deepgram_sockets[call_control_id] = deepgram_ws
                    active_telnyx_sockets[call_control_id] = {
                        "ws": websocket,
                        "stream_id": stream_id
                    }
                    last_active_call = call_control_id
                    logger.info(f"Stored Deepgram socket for call: {call_control_id}")
                
                # Get the public URL from the websocket headers
                host = websocket.headers.get("host", "localhost:8000")
                public_url = f"https://{host}"
                
                logger.info(f"Telnyx stream started: call_control_id={call_control_id}, stream_id={stream_id}")
                logger.info(f"Public URL for LLM proxy: {public_url}")
                
                # Send agent config with correct URL
                config = get_agent_config(public_url)
                await deepgram_ws.send(json.dumps(config))
                logger.info("Sent agent config")
                
                # Start background tasks
                sender_task = asyncio.create_task(send_to_deepgram())
                receiver_task = asyncio.create_task(receive_from_deepgram())
                break
        
        # Continue processing Telnyx messages
        while True:
            message = await websocket.receive_json()
            event_type = message.get("event")
            
            if event_type == "media":
                # Decode and buffer audio from Telnyx
                media_data = message.get("media", {})
                payload = media_data.get("payload", "")
                if payload:
                    audio_data = base64.b64decode(payload)
                    audio_buffer.extend(audio_data)
            
            elif event_type == "stop":
                logger.info("Telnyx stream stopped")
                break
            
            elif event_type == "dtmf":
                dtmf_data = message.get("dtmf", {})
                digit = dtmf_data.get("digit", "")
                logger.info(f"DTMF received: {digit}")
            
            elif event_type == "error":
                error_data = message.get("payload", {})
                logger.error(f"Telnyx error: {error_data}")
    
    except WebSocketDisconnect:
        logger.info("Telnyx WebSocket disconnected")
    except Exception as e:
        logger.error(f"Error in Telnyx media WebSocket: {e}")
    finally:
        # Cleanup
        if sender_task:
            sender_task.cancel()
        if receiver_task:
            receiver_task.cancel()
        if deepgram_ws:
            await deepgram_ws.close()
        # Cleanup sockets from active dicts
        for cid, ws in list(active_deepgram_sockets.items()):
            if ws == deepgram_ws:
                del active_deepgram_sockets[cid]
                if cid in active_telnyx_sockets:
                    del active_telnyx_sockets[cid]
                cleanup_silence_state(cid)
                logger.info(f"Removed socket for call: {cid}")
                break
        logger.info("Telnyx cleanup complete")


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
    
    # Validate voice provider configuration
    if VOICE_PROVIDER == "twilio":
        if not TWILIO_ACCOUNT_SID:
            logger.error("TWILIO_ACCOUNT_SID not set for Twilio provider")
            return
        if not TWILIO_AUTH_TOKEN:
            logger.error("TWILIO_AUTH_TOKEN not set for Twilio provider")
            return
        logger.info("Using Twilio as voice provider")
    elif VOICE_PROVIDER == "telnyx":
        if not TELNYX_API_KEY:
            logger.error("TELNYX_API_KEY not set for Telnyx provider")
            return
        if not TELNYX_PUBLIC_KEY:
            logger.error("TELNYX_PUBLIC_KEY not set for Telnyx provider")
            return
        logger.info("Using Telnyx as voice provider")
    else:
        logger.error(f"Invalid VOICE_PROVIDER: {VOICE_PROVIDER}. Must be 'twilio' or 'telnyx'")
        return

    logger.info(f"Starting deepclaw voice agent server on {HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
