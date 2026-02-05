# Hosted Voice Agent Platform Design

**Date:** 2026-02-04
**Status:** Draft

## Overview

A hosted voice AI platform where anyone can get their own phone number connected to a personal AI agent. Voice-native onboarding, full OpenClaw capabilities, no local setup required.

## User Experience

### Onboarding (Voice-Native)

1. User calls the setup hotline (1-800-DEEPCLAW or similar)
2. Setup agent: "Hi! I'll help you create your personal AI agent. First, I'll send a verification code to this number."
3. SMS verification code sent to caller's number
4. User reads code back (or enters via keypad) → phone verified
5. Agent collects email address
6. Email verification (link or code)
7. Agent collects: voice preference, personality (casual/formal)
8. "All set! Your agent's number is 734-555-1234. Calling it now so you can say hi!"
9. Transfer/connect directly to their new agent

### Daily Usage

- **Call** their number → full voice conversation with agent
- **Text** their number → async text conversation, same agent, same memory
- Agent can **text them** proactively (reminders, follow-ups)
- Agent can **call them back** (reminders, task completion notifications)

### Web Dashboard

For things that don't work well over voice:
- Connect Google/Outlook for calendar + email (OAuth)
- View conversation history (calls + texts)
- Adjust personality, voice, agent name
- Add custom tools / MCP servers
- Usage stats + billing

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         DigitalOcean Infrastructure                      │
│                                                                          │
│  ┌─────────────┐    ┌─────────────────────────────────────────────────┐ │
│  │   Twilio    │    │              Kubernetes Cluster                  │ │
│  │             │    │                                                  │ │
│  │ Setup Line  │───▶│  ┌──────────┐  ┌──────────┐  ┌──────────┐      │ │
│  │ Number Pool │    │  │ Onboard  │  │  Voice   │  │  API /   │      │ │
│  │             │◀──▶│  │ Service  │  │  Proxy   │  │ Dashboard│      │ │
│  └─────────────┘    │  └──────────┘  └────┬─────┘  └──────────┘      │ │
│                     │                      │                          │ │
│                     │         ┌────────────┼────────────┐             │ │
│                     │         ▼            ▼            ▼             │ │
│  ┌─────────────┐    │  ┌──────────┐ ┌──────────┐ ┌──────────┐        │ │
│  │  Deepgram   │◀───│  │OpenClaw  │ │OpenClaw  │ │OpenClaw  │  ...   │ │
│  │ Voice Agent │    │  │ User A   │ │ User B   │ │ User C   │        │ │
│  │    API      │───▶│  └──────────┘ └──────────┘ └──────────┘        │ │
│  └─────────────┘    │       │             │             │             │ │
│                     │       ▼             ▼             ▼             │ │
│                     │  ┌─────────────────────────────────────────┐   │ │
│                     │  │         Persistent Storage              │   │ │
│                     │  │   (User data, memory, conversations)    │   │ │
│                     │  └─────────────────────────────────────────┘   │ │
│                     └─────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────┘
```

### Components

| Component | Purpose |
|-----------|---------|
| **Onboard Service** | Handles setup calls, verification, user creation |
| **Voice Proxy** | Bridges Deepgram Voice Agent API ↔ user's OpenClaw container |
| **API / Dashboard** | Web app for config, OAuth, history, billing |
| **OpenClaw Containers** | One per user, fully isolated, persistent storage |
| **Number Pool Manager** | Maintains warm pool of Twilio numbers, auto-replenishes |

## Call Flow

### Inbound Call (User → Agent)

```
1. User calls their number (734-555-1234)
2. Twilio webhook → Voice Proxy
3. Voice Proxy looks up user by number
4. Voice Proxy connects to Deepgram Voice Agent API
5. Twilio audio ↔ Deepgram (STT + TTS)
6. Deepgram calls LLM endpoint → Voice Proxy → User's OpenClaw container
7. OpenClaw streams response → Voice Proxy → Deepgram → TTS → User hears it
```

### SMS Flow

```
User texts their number
    → Twilio webhook
    → Voice Proxy (or SMS handler)
    → User's OpenClaw container
    → Response via Twilio SMS
```

### Outbound Callback

```
OpenClaw decides to call user (reminder, etc.)
    → Triggers via internal API
    → Voice Proxy initiates Twilio call to user's phone
    → Same voice flow as inbound
```

## Container Management

### Provisioning (New User)

1. Verification complete
2. Assign number from warm pool
3. Create user record in database (email, phone, number, preferences)
4. Spin up OpenClaw container
   - Pull OpenClaw image
   - Mount persistent volume for user data
   - Inject config (voice, personality)
   - Start container
5. Register routing: Twilio number → container endpoint
6. Replenish warm pool if below threshold

### Container Lifecycle

| State | Description |
|-------|-------------|
| **Running** | Active call or recent activity. Full resources. |
| **Idle** | No activity for N minutes. Scaled down, kept warm. |
| **Sleeping** | No activity for hours. Stopped, volume retained. ~5s wake. |
| **Archived** | Inactive for weeks. Data stored, container removed. Slower wake. |

### Scheduled Tasks

External scheduler service tracks reminders/tasks across users. Wakes specific container just-in-time for execution, then allows it to sleep again.

## Agent Capabilities

### Works Immediately (No Setup)

- Conversation with memory
- Web search
- General knowledge
- Code execution (in user's container)
- Reminders (stored in OpenClaw)

### Requires Dashboard Setup

- Calendar integration (Google/Outlook OAuth)
- Email integration (OAuth)
- Custom MCP servers
- File storage connections

## Security & Isolation

### Container Isolation

- Each user's OpenClaw runs in own container
- Separate network namespace
- Dedicated persistent volume
- Resource limits (CPU, memory) per container

### Code Execution

- Runs in user's own container
- If they break something, only affects them
- Can reset to clean state

### Secrets Management

- OAuth tokens stored encrypted
- Injected at runtime, not baked into image
- Database encryption at rest

### Voice Proxy Security

- Validates Twilio webhook signatures
- Validates Deepgram callbacks
- Rate limiting per user
- No user credentials pass through

## Tech Stack

| Component | Technology |
|-----------|------------|
| Container orchestration | DigitalOcean Kubernetes (DOKS) |
| User containers | OpenClaw Docker image |
| Voice Proxy | Python (FastAPI) |
| API / Dashboard | Next.js or Python + React |
| Database | PostgreSQL (DigitalOcean managed) |
| Storage | DigitalOcean Spaces (S3-compatible) |
| Voice | Deepgram Voice Agent API |
| Phone/SMS | Twilio |
| Secrets | DigitalOcean Secrets or HashiCorp Vault |

## Implementation Phases

### Phase 1: Core MVP

- Setup hotline with verification
- Provision user containers
- Voice calls working (inbound)
- Basic web dashboard (login, see your number)
- Number warm pool

### Phase 2: Full Communication

- SMS inbound/outbound
- Agent callbacks to user
- Conversation history in dashboard

### Phase 3: Integrations

- Google OAuth (calendar, email)
- Outlook OAuth
- Custom MCP servers

### Phase 4: Scale & Polish

- Container sleep/wake optimization
- Usage-based billing
- Advanced dashboard (tools config, analytics)
- Mobile app wrapper (optional)

## Open Questions

- Exact heartbeat/scheduler mechanics for sleeping containers
- Pricing model details
- Geographic distribution (single region vs multi-region)
- Backup/disaster recovery strategy
