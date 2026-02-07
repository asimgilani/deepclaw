# Voice Escalation Implementation Plan

**Date:** 2026-02-06  
**Branch:** `dev/voice-escalation`  
**Worktree:** `.worktrees/voice-escalation`

## Overview

Add proactive outbound calling capability to DeepClaw, enabling Maya to offer and initiate voice calls mid-conversation with full context handoff.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          OpenClaw Main Session (Telegram)                     │
│                              (voice-escalation skill)                         │
└──────────────────────────────────────────────────────────────┬──────────────┘
                                                               │
                                                               │ 1. Trigger detected
                                                               │ 2. User consents
                                                               ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                    DeepClaw Outbound Call API                                 │
│  POST /v1/outbound/call                                                     │
│  - phone_number: target number                                              │
│  - context: conversation history + topic + intent                             │
│  - remem_query: optional memory search terms                                │
└──────────────────────────────────────────────────────────────┬──────────────┘
                                                               │
                                                               │ 3. Initiate Telnyx
                                                               │    outbound call
                                                               ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                    Telnyx API                                               │
│  POST https://api.telnyx.com/v2/calls                                       │
│  - to: user's phone                                                         │
│  - from: +1-289-815-4986                                                    │
│  - connection_id: Telnyx connection for DeepClaw                              │
│  - webhook_url: https://voice.appforgeinc.com/telnyx/outbound              │
└──────────────────────────────────────────────────────────────┬──────────────┘
                                                               │
                                                               │ 4. Call answered
                                                               ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                    DeepClaw WebSocket Handler                               │
│  - /telnyx/outbound (new endpoint)                                          │
│  - Inject context into voice agent system prompt                            │
│  - Use x-openclaw-session-key for continuity                                │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Implementation Tasks

### Task 1: Add Outbound Call API Endpoint

**File:** `deepclaw/voice_agent_server.py`

Add a new FastAPI endpoint `/v1/outbound/call` that:
- Accepts POST with JSON body: `{phone_number, context, remem_query}`
- Validates phone number against whitelist
- Calls Telnyx API to initiate outbound call
- Returns call control ID for tracking

**Key components:**
```python
@app.post("/v1/outbound/call")
async def initiate_outbound_call(request: Request):
    # Validate request
    # Call Telnyx API
    # Store context for later injection
    # Return call tracking info
```

### Task 2: Create Outbound Webhook Handler

**File:** `deepclaw/voice_agent_server.py`

Add new webhook endpoint `/telnyx/outbound` that:
- Handles call.answered event differently from inbound
- Retrieves stored context from temporary cache
- Injects context into Deepgram agent config
- Uses modified system prompt with handoff context

**Key components:**
```python
@app.post("/telnyx/outbound")
async def telnyx_outbound_webhook(request: Request):
    # Handle call answered with context injection
    # Different from inbound webhook
```

### Task 3: Build Context Injection System

**File:** `deepclaw/voice_agent_server.py`

Create context storage and injection mechanism:
- In-memory cache for pending calls (with TTL)
- Context formatter that builds handoff prompt
- Modified `get_agent_config()` for outbound with context

**Context format:**
```python
OUTBOUND_HANDOFF_PROMPT = """You are continuing a conversation that started in text chat.

## Previous Conversation (last {n} messages):
{context}

## Current Topic: {topic}
## User Intent: {intent}

The user just said yes to a phone call. Call them now and pick up where you left off.
Be natural - don't read the context verbatim, just continue the conversation seamlessly.
"""
```

### Task 4: Create voice-escalation Skill

**New File:** `skills/voice-escalation/SKILL.md`

Create the skill that Maya uses to:
- Detect escalation triggers (heuristics or explicit)
- Offer voice call to user
- Upon consent, invoke DeepClaw outbound API
- Handle post-call summary

**Skill structure:**
```yaml
---
name: voice-escalation
description: Offer and initiate voice calls when text chat gets complex
requires:
  env:
    - DEEPCLAW_OUTBOUND_API_URL
    - DEEPCLAW_API_KEY
---
```

### Task 5: Add Trigger Detection (Optional for v1)

**File:** `skills/voice-escalation/SKILL.md`

Basic trigger heuristics in the skill:
- Message count threshold
- Frustration keywords
- Explicit "call me" detection
- Offer construction logic

## Files to Modify

1. `deepclaw/voice_agent_server.py` - Add outbound endpoints and context injection
2. `deepclaw/__init__.py` - Update version if needed
3. `skills/voice-escalation/SKILL.md` - New skill (create dir)

## API Contract

### Outbound Call Initiation

**Endpoint:** `POST /v1/outbound/call`

**Request:**
```json
{
  "phone_number": "+1-647-XXX-XXXX",
  "context": {
    "messages": [
      {"role": "user", "content": "..."},
      {"role": "assistant", "content": "..."}
    ],
    "topic": "discussing voice escalation",
    "intent": "technical planning"
  },
  "remem_query": "voice agent architecture"
}
```

**Response:**
```json
{
  "call_control_id": "call-xxx-xxx",
  "status": "initiated",
  "context_id": "ctx-xxx-xxx"
}
```

### Context Storage

Temporary in-memory store with TTL:
```python
outbound_contexts: Dict[str, Dict] = {}
# Key: context_id, Value: {context, created_at, call_control_id}
# TTL: 5 minutes (calls expire if not answered)
```

## Security Considerations

1. **Phone whitelist validation** - Only allow calls to pre-approved numbers
2. **API key authentication** - Require bearer token for outbound API
3. **Rate limiting** - Max 3 calls per hour per session
4. **Consent verification** - Never auto-call without explicit user confirmation

## Testing Plan

1. Unit test: Outbound API endpoint validation
2. Integration test: Telnyx API call (mock)
3. E2E test: Context injection flows correctly
4. Manual test: Actual call to test number

## Cost Estimate

Per outbound call (5 min avg):
- Telnyx outbound: ~$0.03
- Deepgram Voice Agent: ~$0.35
- Grok 4.1 Fast: ~$0.10
- **Total: ~$0.50 per call**

## Success Criteria

- [ ] POST /v1/outbound/call returns 200 with call_control_id
- [ ] Telnyx outbound call connects to DeepClaw
- [ ] Context appears in voice agent system prompt
- [ ] Voice agent continues conversation seamlessly
- [ ] voice-escalation skill can invoke the API
- [ ] Security: Only whitelisted numbers work

## Notes

- Telnyx number: +1-289-815-4986 (already configured)
- Outbound webhook URL: Use same base as inbound but different path
- Context TTL: 5 minutes should be sufficient
- Memory pre-fetch: Optional for v1, can add later
