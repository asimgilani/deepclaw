# Voice Session Memory Spec

**Created:** 2026-02-06
**Status:** Draft
**Author:** Maya

## Problem

Voice calls through DeepClaw are ephemeral. When Asim calls back, Maya has no memory of the previous conversation. The voice agent starts fresh every time.

Currently:
- Text sessions (Telegram/Signal) → synced to Remem via 5-min cron
- Voice calls → completely ephemeral, no persistence

## Goal

Enable voice call continuity:
1. When a call starts, load context from recent voice sessions
2. During the call, persist each turn to Remem
3. When Asim calls back, Maya remembers what was discussed

## Architecture

### Session Identification

Each voice call gets a unique session ID:
```
voice-session:{uuid}
```

Options for session ID source:
- **ElevenLabs:** Use their `conversation_id`
- **DeepClaw Direct:** Generate UUID at call connect

### Remem Document Schema

```json
{
  "title": "Voice Session 2026-02-06T23:15:00-0500",
  "source": "voice-call",
  "tags": "voice-session, deepclaw, phone-call",
  "content": "## Session: voice-session:abc123\n**Started:** 2026-02-06 23:15:00 EST\n**Caller:** +16479802995 (Asim)\n\n### Transcript\n\n**Asim:** Hey Maya, what's my calendar look like tomorrow?\n\n**Maya:** You've got a 10am with Andrew and then lunch with Sumaira at 1.\n\n**Asim:** Cool, can you remind me to prep the deck before the Andrew call?\n\n**Maya:** Done, I'll ping you at 9:30.\n\n---\n**Ended:** 2026-02-06 23:18:42 EST\n**Duration:** 3m 42s"
}
```

### Data Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                         CALL START                               │
└─────────────────────────────────────────────────────────────────┘
                                │
                                ▼
        ┌───────────────────────────────────────┐
        │  1. Generate session_id (UUID)        │
        │  2. Query Remem: last 3 voice sessions│
        │  3. Inject summary into system prompt │
        └───────────────────────────────────────┘
                                │
                                ▼
        ┌───────────────────────────────────────┐
        │  4. Create Remem doc with session_id  │
        │     (empty transcript, metadata only) │
        └───────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│                      DURING CALL                                 │
└─────────────────────────────────────────────────────────────────┘
                                │
                                ▼
        ┌───────────────────────────────────────┐
        │  On each transcript.final event:      │
        │  5. Append turn to local buffer       │
        │  6. Every 30s OR on significant turn: │
        │     Update Remem doc with new content │
        └───────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│                       CALL END                                   │
└─────────────────────────────────────────────────────────────────┘
                                │
                                ▼
        ┌───────────────────────────────────────┐
        │  7. Final Remem update with:          │
        │     - Complete transcript             │
        │     - End timestamp                   │
        │     - Duration                        │
        │     - Optional: LLM-generated summary │
        └───────────────────────────────────────┘
```

## Implementation

### Phase 1: Basic Persistence (MVP)

**Files to modify:**
- `deepclaw/voice_agent_server.py`

**New functions:**
```python
async def create_voice_session(caller_id: str) -> str:
    """Create a new voice session doc in Remem, return session_id."""
    session_id = f"voice-session:{uuid.uuid4()}"
    doc = {
        "title": f"Voice Session {datetime.now().isoformat()}",
        "source": "voice-call",
        "tags": "voice-session, deepclaw",
        "content": f"## Session: {session_id}\n**Started:** {datetime.now()}\n**Caller:** {caller_id}\n\n### Transcript\n\n"
    }
    # POST to Remem /v1/documents
    return session_id

async def append_to_session(session_id: str, speaker: str, text: str):
    """Append a transcript turn to the session doc."""
    # GET current doc, append, PUT back
    # Or use Remem's append mode if available

async def finalize_session(session_id: str, duration_seconds: int):
    """Add end timestamp and duration to session doc."""
```

**Integration points:**
1. `handle_telnyx_websocket()` / `handle_twilio_websocket()`:
   - On connect: `session_id = await create_voice_session(caller_id)`
   - Store session_id in connection state

2. `process_deepgram_message()`:
   - On `transcript.final`: `await append_to_session(session_id, speaker, text)`

3. On disconnect:
   - `await finalize_session(session_id, duration)`

### Phase 2: Context Loading

**On call connect, before first LLM call:**
```python
async def load_voice_context() -> str:
    """Fetch last 3 voice sessions for context injection."""
    results = await remem_query(
        query="voice-session phone call",
        limit=3,
        tags="voice-session"
    )
    
    if not results:
        return ""
    
    summary = "## Recent Voice Calls\n\n"
    for doc in results:
        # Extract key points from each session
        summary += f"- {doc['title']}: {extract_summary(doc['content'])}\n"
    
    return summary
```

**Inject into system prompt:**
```python
VOICE_SYSTEM_PROMPT = f"""You are Maya on a PHONE CALL with Asim...

{await load_voice_context()}

Remember: you may have discussed some of these topics in previous calls.
"""
```

### Phase 3: Smart Pinning (Future)

For long-running topics that span multiple calls:
- Detect recurring topics (e.g., "Orlando sale", "Dubai move")
- Create pinned context docs that persist across sessions
- Auto-update pinned docs when status changes

## Remem API Reference

**Create document:**
```bash
POST /v1/documents
{
  "title": "...",
  "content": "...",
  "source": "voice-call",
  "tags": "voice-session"
}
# Returns: { "id": "uuid", ... }
```

**Update document:**
```bash
PUT /v1/documents/{id}
{
  "content": "...",
  "mode": "replace"  # or "append" if supported
}
```

**Query with tag filter:**
```bash
POST /v1/query
{
  "text": "voice-session",
  "limit": 3,
  "tags": ["voice-session"]  # if tag filtering supported
}
```

## Configuration

New env vars for `start.sh`:
```bash
export VOICE_SESSION_ENABLED=true
export VOICE_SESSION_SYNC_INTERVAL=30  # seconds
export VOICE_SESSION_CONTEXT_LIMIT=3   # sessions to load
```

## Testing

1. **Manual test:**
   - Make a call, discuss something specific ("remind me about the Tesla service")
   - Hang up
   - Call back within 5 minutes
   - Ask "what did we just talk about?"
   - Maya should recall the Tesla service reminder

2. **Verify Remem persistence:**
   ```bash
   curl -X POST https://api.remem.io/v1/query \
     -H "X-API-Key: $REMEM_API_KEY" \
     -d '{"text": "voice-session", "limit": 5}'
   ```

## Open Questions

1. **Append vs Replace:** Does Remem support append mode, or do we need GET+concat+PUT?
2. **Tag filtering:** Can we filter queries by tag, or do we rely on text matching?
3. **Rate limits:** What's the safe update frequency for Remem during a call?
4. **Cleanup:** Should old voice sessions auto-archive after N days?

## Timeline

- **Phase 1 (MVP):** 2-3 hours - basic persistence, no context loading
- **Phase 2:** 1-2 hours - context loading on call connect  
- **Phase 3:** Future - smart pinning, topic tracking

## Success Metrics

- Voice session docs appear in Remem within 60s of call end
- Call-back context works: Maya recalls last call's topics
- No noticeable latency impact on voice responses
