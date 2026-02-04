# deepclaw

Call your OpenClaw over the phone with Deepgram Flux + Aura-2.

A faster, cheaper, self-hostable alternative to ElevenLabs Agents.

## Why Deepgram?

| | ElevenLabs | Deepgram |
|---|---|---|
| **Turn detection** | VAD-based | Semantic (Flux) |
| **TTS latency** | ~200ms TTFB | 90ms TTFB |
| **TTS price** | $0.050/1K chars | $0.030/1K chars |
| **Self-host** | No | Yes |
| **Barge-in** | Basic VAD | Native StartOfTurn |

Deepgram Flux understands *when you're done talking* semantically—not just when you stop making noise. This means fewer awkward interruptions and faster responses.

## How It Works

```
Phone Call → Twilio → deepclaw → Deepgram Flux (STT)
                         ↓
                    OpenClaw (LLM)
                         ↓
                  Deepgram Aura-2 (TTS) → Twilio → Phone Call
```

1. You call your Twilio number
2. Twilio streams audio to deepclaw
3. Flux transcribes with semantic turn detection
4. OpenClaw processes the request
5. Aura-2 speaks the response back to you

**Barge-in support:** Start talking while the assistant is speaking and it stops immediately.

## Quick Setup (Let OpenClaw Do It)

The easiest way to set up deepclaw is to let your OpenClaw do it for you:

```bash
# Copy the skill to your OpenClaw
cp -r skills/deepclaw-voice ~/.openclaw/skills/
```

Then tell your OpenClaw: **"I want to call you on the phone"**

OpenClaw will walk you through:
- Creating a Deepgram account (free $200 credit)
- Setting up a Twilio phone number (~$1/month)
- Configuring everything automatically

## Manual Setup

### Prerequisites

- Python 3.10+
- [Deepgram account](https://console.deepgram.com/) (free tier available, $200 credit)
- [Twilio account](https://www.twilio.com/) with a phone number (~$1/month)
- [OpenClaw](https://github.com/openclaw/openclaw) running locally
- [ngrok](https://ngrok.com/) for exposing your local server

### 1. Clone and install

```bash
git clone https://github.com/yourusername/deepclaw.git
cd deepclaw
pip install -e .
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

```env
DEEPGRAM_API_KEY=your_deepgram_api_key
TWILIO_ACCOUNT_SID=your_twilio_account_sid
TWILIO_AUTH_TOKEN=your_twilio_auth_token
OPENCLAW_GATEWAY_URL=http://127.0.0.1:18789
OPENCLAW_GATEWAY_TOKEN=your_openclaw_gateway_token
```

### 3. Configure OpenClaw

In your `openclaw.json`, enable the chat completions endpoint:

```json
{
  "gateway": {
    "http": {
      "endpoints": {
        "chatCompletions": {
          "enabled": true
        }
      }
    }
  }
}
```

### 4. Start the tunnel

```bash
ngrok http 8000
```

Note your ngrok URL (e.g., `https://abc123.ngrok-free.app`).

### 5. Configure Twilio

1. Go to your [Twilio Console](https://console.twilio.com/)
2. Navigate to Phone Numbers → Manage → Active Numbers
3. Click your number
4. Under "Voice Configuration":
   - Set "A Call Comes In" to **Webhook**
   - URL: `https://your-ngrok-url.ngrok-free.app/twilio/incoming`
   - Method: **POST**
5. Save

### 6. Start deepclaw

```bash
python -m deepclaw
```

### 7. Call your number

Pick up the phone and talk to your OpenClaw!

## Architecture

```
┌─────────────┐     ┌─────────────────────────────────────────────────┐
│   Caller    │     │                  Your Machine                   │
│  (Phone)    │     │                                                 │
└──────┬──────┘     │  ┌───────────┐   ┌──────────┐   ┌───────────┐  │
       │            │  │  Twilio   │   │ deepclaw │   │ OpenClaw  │  │
       │ PSTN       │  │  Webhook  │──▶│  Server  │──▶│  Gateway  │  │
       │            │  └───────────┘   └────┬─────┘   └───────────┘  │
       ▼            │                       │                        │
┌──────────────┐    │         ┌─────────────┴─────────────┐          │
│    Twilio    │◀───┼─────────│                           │          │
│  (SIP/Media) │    │         ▼                           ▼          │
└──────────────┘    │  ┌─────────────┐           ┌─────────────┐     │
       │            │  │ Deepgram    │           │ Deepgram    │     │
       │            │  │ Flux (STT)  │           │ Aura-2 (TTS)│     │
       └────────────┼──│ WebSocket   │           │ REST API    │     │
         Audio      │  └─────────────┘           └─────────────┘     │
                    └─────────────────────────────────────────────────┘
```

## Customizing Voice

deepclaw uses Deepgram Aura-2 TTS with 80+ voices in 7 languages. Edit `voice_agent_server.py`:

```python
"speak": {
    "provider": {
        "type": "deepgram",
        "model": "aura-2-orion-en",  # Change voice here
    },
},
```

**Popular voices:**
| Voice | Style |
|-------|-------|
| `aura-2-thalia-en` | Feminine, American (default) |
| `aura-2-orion-en` | Masculine, American |
| `aura-2-draco-en` | Masculine, British |
| `aura-2-estrella-es` | Feminine, Mexican Spanish |
| `aura-2-fabian-de` | Masculine, German |

See `skills/deepclaw-voice/SKILL.md` for the complete voice list (80+ voices in 7 languages), or test voices at https://playground.deepgram.com/

## Coming Soon

- **Local wake-word mode** — Talk to OpenClaw hands-free at your desk, no phone needed
- **One-click desktop installer** — No terminal required
- **Native OpenClaw plugin** — Install with one command

## Known Limitations

**OpenClaw streaming latency:** OpenClaw's `/v1/chat/completions` endpoint currently buffers responses for ~5 seconds before streaming begins. This adds latency to LLM responses regardless of which voice provider you use (Deepgram, ElevenLabs, etc.).

The initial greeting is instant (generated by Deepgram), but subsequent responses wait for OpenClaw's buffer.

This is an upstream limitation. OpenClaw's native WebSocket agent endpoint streams properly, but external voice APIs require the OpenAI-compatible chat completions endpoint.

## Performance

deepclaw logs latency metrics for every turn:

```
Latencies - OpenClaw: 1250ms, TTS TTFB: 87ms, Total: 1337ms
```

Use these to prove you're faster than ElevenLabs.

## License

MIT

## Credits

Built with:
- [Deepgram Flux](https://deepgram.com/product/speech-to-text) — Conversational speech recognition
- [Deepgram Aura-2](https://deepgram.com/product/text-to-speech) — Enterprise text-to-speech
- [OpenClaw](https://github.com/openclaw/openclaw) — Open-source AI assistant
- [Twilio](https://www.twilio.com/) — Phone infrastructure
