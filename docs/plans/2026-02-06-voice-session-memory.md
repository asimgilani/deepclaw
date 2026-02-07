# Voice Session Memory Spec

**Created:** 2026-02-06
**Updated:** 2026-02-06
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
2. During the call, buffer transcript locally
3. On hangup, summarize and store clean notes in Remem
4. When Asim calls back, Maya remembers what was discussed

## Design Decision: Summarized vs Raw

**We store summarized content, not raw transcripts.**

Raw transcripts are noisy:
- Filler words ("um", "uh", "like")
- Greetings and small talk ("hey how are you")
- Speech recognition artifacts
- Repeated/corrected words

This makes them hard to search and wasteful as context.

Instead:
1. Buffer raw transcript locally during call
2. On hangup, run LLM summarization (Grok/Haiku)
3. Store clean summary in Remem
4. Optionally archive raw transcript locally (not in Remem)

## Architecture

### Session Identification

Each voice call gets a unique session ID:
```
voice-session:{uuid}
```

Options for session ID source:
- **ElevenLabs:** Use their `conversation_id`
- **DeepClaw Direct:** Generate UUID at call connect

### Remem Document Schema (Summarized)

```json
{
  "title": "Voice Call Summary — 2026-02-06 11:15 PM",
  "source": "voice-call",
  "tags": "voice-session, deepclaw, phone-call",
  "content": "## Voice Call Summary — 2026-02-06 11:15 PM\n\n**Session:** voice-session:abc123\n**Duration:** 4m 22s\n**Caller:** Asim (+16479802995)\n\n### Topics Discussed\n- Remem indexing fix for Orlando search\n- Voice session persistence design\n- Decided to summarize calls before storing\n\n### Key Decisions\n- Store summarized content in Remem, not raw transcripts\n- Use post-call LLM processing to clean up\n\n### Action Items\n- [ ] Build voice session memory into DeepClaw\n- [ ] Test callback context loading\n\n### Context for Next Call\nWe were working on making voice calls remember prior conversations. The approach is to summarize calls on hangup and store clean notes."
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
        │  3. Inject summaries into system prompt│
        │  4. Initialize local transcript buffer │
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
        │     (in-memory, no Remem writes yet)  │
        └───────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│                       CALL END                                   │
└─────────────────────────────────────────────────────────────────┘
                                │
                                ▼
        ┌───────────────────────────────────────┐
        │  6. Save raw transcript to local file │
        │     /tmp/voice-sessions/{session_id}  │
        │     (backup, not in Remem)            │
        └───────────────────────────────────────┘
                                │
                                ▼
        ┌───────────────────────────────────────┐
        │  7. LLM Summarization (Grok/Haiku):   │
        │     - Extract key topics              │
        │     - Capture decisions made          │
        │     - List action items               │
        │     - Note important facts            │
        │     - Generate "context for next call"│
        └───────────────────────────────────────┘
                                │
                                ▼
        ┌───────────────────────────────────────┐
        │  8. POST summary to Remem             │
        │     - Clean, searchable document      │
        │     - Tagged: voice-session           │
        └───────────────────────────────────────┘
```

### Summarization Prompt

```
Summarize this voice call transcript between Asim and Maya.

Extract:
1. **Topics Discussed** - Main subjects covered (bullet points)
2. **Key Decisions** - Any decisions or conclusions reached
3. **Action Items** - Tasks or follow-ups mentioned (use checkboxes)
4. **Important Facts** - Names, dates, numbers, or facts worth remembering
5. **Context for Next Call** - 1-2 sentences summarizing where we left off

Ignore:
- Greetings and small talk
- Filler words (um, uh, like, you know)
- Repeated or corrected words
- "How are you" type exchanges

Be concise. Focus on actionable information.

---
TRANSCRIPT:
{raw_transcript}
```

## Implementation

### Phase 1: Local Buffering + Post-Call Summary (MVP)

**Files to modify:**
- `deepclaw/voice_agent_server.py`

**New state per connection:**
```python
@dataclass
class VoiceSession:
    session_id: str
    caller_id: str
    started_at: datetime
    transcript_buffer: list[dict]  # [{"speaker": "asim", "text": "...", "ts": ...}, ...]
```

**New functions:**
```python
async def init_voice_session(caller_id: str) -> VoiceSession:
    """Initialize a new voice session with empty buffer."""
    return VoiceSession(
        session_id=f"voice-session:{uuid.uuid4()}",
        caller_id=caller_id,
        started_at=datetime.now(),
        transcript_buffer=[]
    )

def append_transcript(session: VoiceSession, speaker: str, text: str):
    """Buffer a transcript turn locally (no network call)."""
    session.transcript_buffer.append({
        "speaker": speaker,
        "text": text,
        "ts": datetime.now().isoformat()
    })

async def finalize_voice_session(session: VoiceSession):
    """On hangup: save raw, summarize, store to Remem."""
    duration = (datetime.now() - session.started_at).total_seconds()
    
    # 1. Build raw transcript string
    raw_transcript = "\n".join([
        f"{turn['speaker'].upper()}: {turn['text']}" 
        for turn in session.transcript_buffer
    ])
    
    # 2. Save raw to local file (backup)
    raw_path = f"/tmp/voice-sessions/{session.session_id}.txt"
    Path(raw_path).parent.mkdir(parents=True, exist_ok=True)
    Path(raw_path).write_text(raw_transcript)
    
    # 3. Summarize with LLM
    summary = await summarize_transcript(raw_transcript, session, duration)
    
    # 4. Store summary in Remem
    await store_in_remem(session, summary)

async def summarize_transcript(raw: str, session: VoiceSession, duration: float) -> str:
    """Use Grok/Haiku to generate clean summary."""
    prompt = SUMMARIZATION_PROMPT.format(raw_transcript=raw)
    
    # Call Grok (fast, cheap)
    response = await call_grok(prompt, max_tokens=500)
    
    # Format final document
    return f"""## Voice Call Summary — {session.started_at.strftime('%Y-%m-%d %I:%M %p')}

**Session:** {session.session_id}
**Duration:** {int(duration // 60)}m {int(duration % 60)}s
**Caller:** Asim ({session.caller_id})

{response}
"""

async def store_in_remem(session: VoiceSession, summary: str):
    """POST summarized call notes to Remem."""
    async with httpx.AsyncClient() as client:
        await client.post(
            f"{REMEM_API_URL}/v1/documents",
            headers={"X-API-Key": REMEM_API_KEY},
            json={
                "title": f"Voice Call Summary — {session.started_at.strftime('%Y-%m-%d %I:%M %p')}",
                "content": summary,
                "source": "voice-call",
                "tags": "voice-session, deepclaw, phone-call"
            }
        )
```

**Integration points:**
1. `handle_telnyx_websocket()` / `handle_twilio_websocket()`:
   - On connect: `session = await init_voice_session(caller_id)`
   - Store session in connection state

2. `process_deepgram_message()`:
   - On `transcript.final` from user: `append_transcript(session, "asim", text)`
   - On `transcript.final` from agent: `append_transcript(session, "maya", text)`

3. On disconnect:
   - `await finalize_voice_session(session)`

### Phase 2: Context Loading on Call Start

**On call connect, before first LLM call:**
```python
async def load_voice_context() -> str:
    """Fetch last 3 voice session summaries for context injection."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{REMEM_API_URL}/v1/query",
            headers={"X-API-Key": REMEM_API_KEY},
            json={"text": "voice call summary", "limit": 3}
        )
        results = resp.json().get("results", [])
    
    if not results:
        return ""
    
    context = "## Recent Voice Calls\n\n"
    for doc in results:
        # Include title and first ~500 chars of content
        context += f"### {doc.get('title', 'Untitled')}\n"
        context += doc.get('snippet', '')[:500] + "\n\n"
    
    return context
```

**Inject into system prompt:**
```python
voice_context = await load_voice_context()

VOICE_SYSTEM_PROMPT = f"""You are Maya on a PHONE CALL with Asim...

{voice_context}

You may have discussed some of these topics in previous calls. Reference them naturally if relevant.
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
   - Wait 10-15 seconds for summarization to complete
   - Check Remem for the summary:
     ```bash
     curl -X POST https://api.remem.io/v1/query \
       -H "X-API-Key: $REMEM_API_KEY" \
       -d '{"text": "voice call summary", "limit": 1}'
     ```
   - Call back
   - Ask "what did we just talk about?"
   - Maya should recall the Tesla service reminder

2. **Verify raw transcript backup:**
   ```bash
   ls -la /tmp/voice-sessions/
   cat /tmp/voice-sessions/voice-session-*.txt
   ```

3. **Verify summary quality:**
   - Check that filler words are stripped
   - Confirm action items are captured
   - Ensure "context for next call" makes sense

## Open Questions

1. **Summarization model:** Grok 4 Fast vs Haiku? (Grok is already loaded for voice, might be simpler)
2. **Summarization latency:** Is 5-10s acceptable post-hangup, or should we fire-and-forget?
3. **Tag filtering:** Can we filter Remem queries by tag, or do we rely on text matching?
4. **Cleanup:** Should old voice sessions auto-archive after N days?
5. **Raw transcript retention:** Keep forever, or auto-delete after 7 days?

## Timeline

- **Phase 1 (MVP):** 2-3 hours
  - Local buffering during call
  - Post-call summarization with Grok
  - Store summary in Remem
  - Raw backup to /tmp
  
- **Phase 2:** 1-2 hours
  - Context loading on call connect
  - Inject recent summaries into system prompt
  
- **Phase 3:** Future
  - Smart pinning for recurring topics
  - Auto-archival of old sessions

## Success Metrics

- Summarized docs appear in Remem within 30s of call end
- Summaries are clean (no filler, no small talk)
- Action items are captured accurately
- Call-back context works: Maya recalls last call's topics
- No noticeable latency impact on voice responses (summarization is post-hangup)
