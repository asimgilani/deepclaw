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

## Setup

### Prerequisites

- Python 3.10+
- [Deepgram account](https://console.deepgram.com/) (free tier available)
- [Twilio account](https://www.twilio.com/) with a phone number
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

## Coming Soon

- **Local wake-word mode** — Talk to OpenClaw hands-free at your desk, no phone needed
- **One-click desktop installer** — No terminal required
- **Native OpenClaw plugin** — Install with one command

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
