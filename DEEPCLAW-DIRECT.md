# DeepClaw Direct Mode

**Project:** DeepClaw Direct
**Started:** 2026-02-06
**Status:** In Progress
**Goal:** Sub-2-second TTFB for voice calls

## Problem

Current DeepClaw routes through OpenClaw gateway which adds 10-40 seconds latency due to:
- Session loading
- Plugin initialization  
- Memory plugin processing
- Full agent context assembly

## Solution

Bypass OpenClaw for normal voice conversation. Call Grok directly via xAI API.

### Architecture

```
CURRENT (slow):
Speech → Deepgram STT → DeepClaw → OpenClaw (10-40s) → Grok → Deepgram TTS → Speech

NEW (fast):
Speech → Deepgram STT → DeepClaw → Remem pre-fetch (1s) → Grok direct (1-2s) → Deepgram TTS → Speech
```

### Components

1. **Fast Path (default):**
   - Remem pre-fetch for memory context (~1s)
   - Direct xAI API call to Grok 4 Fast
   - SOUL/persona in system prompt
   - Target: <2s TTFB

2. **Slow Path (on-demand):**
   - When user asks for tasks/work
   - Async call to OpenClaw API
   - `sessions_spawn` for sub-agents (Hermione, Dobby, etc.)
   - `sessions_send` to ping main Maya
   - Doesn't block the voice call

### System Prompt Structure

```
[SOUL - Maya's personality, voice-optimized]
[Current time]
[Remem context - pre-fetched]
[Voice rules - short responses, no markdown]
[Available actions - can spawn agents, escalate to main]
```

### API Endpoints Used

- **Remem:** `POST https://api.remem.io/v1/query` (X-API-Key header)
- **Grok:** `POST https://api.x.ai/v1/chat/completions` (xai/grok-4-1-fast)
- **OpenClaw (async):** `POST http://127.0.0.1:18789/v1/chat/completions` (for spawning only)

### Files to Modify

1. `/mnt/maya-shared/projects/deepclaw/deepclaw/voice_agent_server.py`
   - Add direct Grok path
   - Keep OpenClaw path for agent spawning
   - Add spawn/send helpers

### Environment Variables

- `XAI_API_KEY` - Grok API key
- `REMEM_API_KEY` - Remem API key  
- `OPENCLAW_GATEWAY_URL` - For async agent spawning
- `OPENCLAW_GATEWAY_TOKEN` - Auth token

### Success Criteria

- [ ] TTFB < 2 seconds for normal conversation
- [ ] Remem context properly injected
- [ ] Soul/personality preserved
- [ ] Can spawn sub-agents on request
- [ ] Can escalate to main Maya

### Conversation Context

This project started from analyzing ElevenLabs' ElevenAgents article showing proactive voice calling. We built DeepClaw with OpenClaw routing but discovered the latency was unacceptable (10-40s TTFB). Root cause: OpenClaw's session/plugin overhead. Solution: bypass for voice, keep for agent spawning.

---

## Implementation Log

### 2026-02-06

- Identified OpenClaw as latency bottleneck
- Fixed Remem endpoint (was /v1/search, now /v1/query)
- Fixed Remem header (was Bearer, now X-API-Key)
- Created voice agent workspace with SOUL.md
- Decided to implement direct Grok path

**Next:** Modify voice_agent_server.py to call Grok directly
