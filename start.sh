#!/bin/bash
set -e
cd "$(dirname "$0")"

echo "======================================"
echo "DeepClaw Voice Server (Official)"
echo "======================================"

# Load ALL secrets from Doppler - never hardcode
export DEEPGRAM_API_KEY=$(doppler secrets get DEEPGRAM_API_KEY --plain -p clawdbot -c prd)
export TELNYX_API_KEY=$(doppler secrets get TELNYX_API_KEY --plain -p clawdbot -c prd)
export TELNYX_PUBLIC_KEY=$(doppler secrets get TELNYX_PUBLIC_KEY --plain -p clawdbot -c prd 2>/dev/null || echo "")
export OPENCLAW_GATEWAY_URL="http://127.0.0.1:18789"
export OPENCLAW_GATEWAY_TOKEN=$(doppler secrets get OPENCLAW_GATEWAY_TOKEN --plain -p clawdbot -c prd)
export XAI_API_KEY=$(doppler secrets get XAI_API_KEY --plain -p clawdbot -c prd)
export REMEM_API_KEY=$(doppler secrets get REMEM_API_KEY --plain -p clawdbot -c prd)
export ANTHROPIC_API_KEY=$(doppler secrets get ANTHROPIC_API_KEY --plain -p clawdbot -c prd)
export HOST="127.0.0.1"
export PORT="8000"
export VOICE_PROVIDER="telnyx"
export ALLOWED_CALLERS="+16479802995"

echo "✓ Secrets loaded from Doppler"
echo "✓ Allowed callers: $ALLOWED_CALLERS"

export PATH="$HOME/.local/bin:$PATH"
python3 -m deepclaw
