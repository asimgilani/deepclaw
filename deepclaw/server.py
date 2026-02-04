"""FastAPI server with Twilio webhook endpoints."""

import asyncio
import base64
import json
import logging
import os
from contextlib import asynccontextmanager
from time import perf_counter

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import Response

from .state import CallState, CallSession, SessionManager
from .flux_client import FluxClient, FluxEventType
from .tts import TTSClient, TTSFallback
from .openclaw import OpenClawClient, get_completion_text

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Configuration
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
OPENCLAW_GATEWAY_URL = os.getenv("OPENCLAW_GATEWAY_URL", "http://127.0.0.1:18789")
OPENCLAW_GATEWAY_TOKEN = os.getenv("OPENCLAW_GATEWAY_TOKEN", "")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))

# Global session manager
session_manager = SessionManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    logger.info("deepclaw server starting...")
    yield
    logger.info("deepclaw server shutting down...")


app = FastAPI(title="deepclaw", lifespan=lifespan)


# TwiML response for incoming calls
TWIML_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="wss://{host}/twilio/media">
            <Parameter name="call_sid" value="{{{{CallSid}}}}"/>
        </Stream>
    </Connect>
</Response>"""


@app.post("/twilio/incoming")
async def twilio_incoming(request: Request):
    """
    Handle incoming Twilio call.

    Returns TwiML to start a bidirectional media stream.
    """
    # Get host from request for WebSocket URL
    host = request.headers.get("host", "localhost:8000")

    # Generate TwiML with the stream URL
    twiml = TWIML_RESPONSE.format(host=host)

    logger.info(f"Incoming call, starting media stream to wss://{host}/twilio/media")

    return Response(content=twiml, media_type="application/xml")


@app.websocket("/twilio/media")
async def twilio_media_websocket(websocket: WebSocket):
    """
    Handle bidirectional Twilio media stream.

    Receives audio from caller, sends audio back.
    """
    await websocket.accept()
    logger.info("Twilio media WebSocket connected")

    call_sid: str | None = None
    session: CallSession | None = None
    stream_sid: str | None = None

    # Clients
    flux_client: FluxClient | None = None
    tts_client: TTSClient | None = None
    openclaw_client: OpenClawClient | None = None

    # Tasks
    flux_receiver_task: asyncio.Task | None = None
    audio_buffer: list[bytes] = []
    current_transcript = ""

    async def stop_tts_and_clear():
        """Stop TTS playback for barge-in."""
        nonlocal audio_buffer
        if tts_client:
            await tts_client.cancel()
        audio_buffer.clear()

        # Send clear message to Twilio to stop playback
        if stream_sid:
            clear_msg = {"event": "clear", "streamSid": stream_sid}
            try:
                await websocket.send_json(clear_msg)
                logger.debug("Sent clear message to Twilio")
            except Exception as e:
                logger.warning(f"Failed to send clear message: {e}")

    async def send_audio_to_twilio(audio_data: bytes):
        """Send audio chunk to Twilio."""
        if not stream_sid:
            return

        # Twilio expects base64-encoded mulaw audio
        payload = base64.b64encode(audio_data).decode("utf-8")
        media_msg = {
            "event": "media",
            "streamSid": stream_sid,
            "media": {"payload": payload},
        }
        try:
            await websocket.send_json(media_msg)
        except Exception as e:
            logger.warning(f"Failed to send audio to Twilio: {e}")

    async def process_flux_events():
        """Process events from Flux STT."""
        nonlocal current_transcript

        if not flux_client or not session:
            return

        async for event in flux_client.receive_events():
            if event.type == FluxEventType.START_OF_TURN:
                await session.handle_start_of_turn()

            elif event.type == FluxEventType.END_OF_TURN:
                transcript = event.transcript or current_transcript
                if transcript.strip():
                    await session.handle_end_of_turn(transcript)
                    await process_turn(transcript)
                current_transcript = ""

            elif event.type == FluxEventType.TRANSCRIPT:
                if event.transcript:
                    current_transcript = event.transcript
                    logger.debug(f"Transcript: {event.transcript}")

            elif event.type == FluxEventType.ERROR:
                logger.error(f"Flux error: {event.error}")

    async def process_turn(transcript: str):
        """Process a complete user turn."""
        if not session or not openclaw_client or not tts_client:
            return

        logger.info(f"User: {transcript}")
        session.add_user_message(transcript)

        try:
            # Get response from OpenClaw
            response = await get_completion_text(
                openclaw_client, session.conversation_history
            )
            session.metrics.openclaw_response_time = perf_counter()

            if not response:
                response = "I didn't catch that. Could you say it again?"

            logger.info(f"Assistant: {response}")
            session.add_assistant_message(response)

            # Transition to speaking
            await session.transition_to(CallState.SPEAKING)

            # Stream TTS audio to Twilio
            first_byte = True
            async for chunk in tts_client.synthesize_stream(response):
                if session.state != CallState.SPEAKING:
                    # Barge-in occurred
                    break

                if first_byte:
                    session.metrics.tts_first_byte_time = perf_counter()
                    session.metrics.log_summary()
                    first_byte = False

                await send_audio_to_twilio(chunk)

            # Done speaking, back to listening
            if session.state == CallState.SPEAKING:
                await session.transition_to(CallState.LISTENING)
                session.reset_metrics()

        except Exception as e:
            logger.error(f"Error processing turn: {e}")
            # Send fallback audio
            fallback = TTSFallback(tts_client)
            async for chunk in fallback.get_fallback_audio():
                await send_audio_to_twilio(chunk)
            await session.transition_to(CallState.LISTENING)

    try:
        async with TTSClient(DEEPGRAM_API_KEY) as tts, OpenClawClient(
            OPENCLAW_GATEWAY_URL, OPENCLAW_GATEWAY_TOKEN
        ) as openclaw:
            tts_client = tts
            openclaw_client = openclaw

            while True:
                message = await websocket.receive_json()
                event = message.get("event")

                if event == "connected":
                    logger.info("Twilio media stream connected")

                elif event == "start":
                    # Extract call info from start message
                    start_data = message.get("start", {})
                    stream_sid = message.get("streamSid")
                    call_sid = start_data.get("callSid") or start_data.get(
                        "customParameters", {}
                    ).get("call_sid")

                    logger.info(f"Stream started - CallSid: {call_sid}, StreamSid: {stream_sid}")

                    # Create session
                    session = await session_manager.create_session(call_sid or "unknown")
                    session.on_barge_in = stop_tts_and_clear
                    await session.transition_to(CallState.LISTENING)

                    # Connect to Flux
                    flux_client = FluxClient(DEEPGRAM_API_KEY)
                    await flux_client.connect()

                    # Start Flux event processor
                    flux_receiver_task = asyncio.create_task(process_flux_events())

                elif event == "media":
                    # Forward audio to Flux
                    if flux_client and session and session.state in (
                        CallState.LISTENING,
                        CallState.SPEAKING,  # Keep listening during TTS for barge-in
                    ):
                        payload = message.get("media", {}).get("payload", "")
                        if payload:
                            audio_data = base64.b64decode(payload)
                            await flux_client.send_audio(audio_data)

                elif event == "stop":
                    logger.info("Stream stopped")
                    break

    except WebSocketDisconnect:
        logger.info("Twilio WebSocket disconnected")
    except Exception as e:
        logger.error(f"Error in media WebSocket: {e}")
    finally:
        # Cleanup
        if flux_receiver_task:
            flux_receiver_task.cancel()
            try:
                await flux_receiver_task
            except asyncio.CancelledError:
                pass

        if flux_client:
            await flux_client.disconnect()

        if call_sid:
            await session_manager.remove_session(call_sid)

        logger.info("Media WebSocket cleanup complete")


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok", "service": "deepclaw"}


def main():
    """Run the server."""
    import uvicorn

    logger.info(f"Starting deepclaw server on {HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
