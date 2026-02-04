"""Deepgram Flux WebSocket client for conversational speech recognition."""

import asyncio
import json
import logging
import base64
from typing import AsyncIterator, Callable, Awaitable
from dataclasses import dataclass
from enum import Enum, auto

import websockets
from websockets.client import WebSocketClientProtocol

logger = logging.getLogger(__name__)

DEEPGRAM_FLUX_URL = "wss://api.deepgram.com/v1/listen"


class FluxEventType(Enum):
    START_OF_TURN = auto()
    END_OF_TURN = auto()
    TRANSCRIPT = auto()
    ERROR = auto()


@dataclass
class FluxEvent:
    type: FluxEventType
    transcript: str = ""
    is_final: bool = False
    error: str | None = None


class FluxClient:
    """
    Deepgram Flux WebSocket client with native turn detection.

    Flux provides StartOfTurn and EndOfTurn events based on semantic
    understanding, not just silence detection.
    """

    def __init__(
        self,
        api_key: str,
        sample_rate: int = 8000,
        encoding: str = "mulaw",
        channels: int = 1,
        eot_threshold: float = 0.7,
        eot_silence_threshold_ms: int = 5000,
    ) -> None:
        self.api_key = api_key
        self.sample_rate = sample_rate
        self.encoding = encoding
        self.channels = channels
        self.eot_threshold = eot_threshold
        self.eot_silence_threshold_ms = eot_silence_threshold_ms

        self._ws: WebSocketClientProtocol | None = None
        self._connected = False
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 30.0

    def _build_url(self) -> str:
        """Build Flux WebSocket URL with query parameters."""
        params = [
            f"sample_rate={self.sample_rate}",
            f"encoding={self.encoding}",
            f"channels={self.channels}",
            "model=flux",
            "punctuate=true",
            "interim_results=true",
            f"endpointing={self.eot_silence_threshold_ms}",
            f"utterance_end_ms={self.eot_silence_threshold_ms}",
            "vad_events=true",
        ]
        return f"{DEEPGRAM_FLUX_URL}?{'&'.join(params)}"

    async def connect(self) -> None:
        """Establish WebSocket connection to Deepgram Flux."""
        url = self._build_url()
        headers = {"Authorization": f"Token {self.api_key}"}

        try:
            self._ws = await websockets.connect(url, extra_headers=headers)
            self._connected = True
            self._reconnect_delay = 1.0
            logger.info("Connected to Deepgram Flux")
        except Exception as e:
            logger.error(f"Failed to connect to Deepgram Flux: {e}")
            raise

    async def disconnect(self) -> None:
        """Close WebSocket connection."""
        if self._ws:
            self._connected = False
            await self._ws.close()
            self._ws = None
            logger.info("Disconnected from Deepgram Flux")

    async def send_audio(self, audio_data: bytes) -> None:
        """Send audio data to Flux for transcription."""
        if not self._ws or not self._connected:
            logger.warning("Cannot send audio: not connected")
            return

        try:
            await self._ws.send(audio_data)
        except websockets.exceptions.ConnectionClosed:
            logger.warning("Connection closed while sending audio")
            self._connected = False

    async def receive_events(self) -> AsyncIterator[FluxEvent]:
        """
        Receive and parse Flux events.

        Yields FluxEvent objects for:
        - START_OF_TURN: User started speaking
        - END_OF_TURN: User finished speaking (semantic detection)
        - TRANSCRIPT: Interim or final transcript
        - ERROR: Error from Deepgram
        """
        if not self._ws:
            return

        try:
            async for message in self._ws:
                event = self._parse_message(message)
                if event:
                    yield event
        except websockets.exceptions.ConnectionClosed as e:
            logger.warning(f"Flux connection closed: {e}")
            self._connected = False

    def _parse_message(self, message: str) -> FluxEvent | None:
        """Parse Deepgram WebSocket message into FluxEvent."""
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse Flux message: {message}")
            return None

        msg_type = data.get("type", "")

        # Speech started event (VAD)
        if msg_type == "SpeechStarted":
            logger.debug("Flux: SpeechStarted")
            return FluxEvent(type=FluxEventType.START_OF_TURN)

        # Utterance end event (semantic turn detection)
        if msg_type == "UtteranceEnd":
            logger.debug("Flux: UtteranceEnd (EndOfTurn)")
            return FluxEvent(type=FluxEventType.END_OF_TURN)

        # Transcript results
        if msg_type == "Results":
            channel = data.get("channel", {})
            alternatives = channel.get("alternatives", [])
            if alternatives:
                transcript = alternatives[0].get("transcript", "")
                is_final = data.get("is_final", False)
                speech_final = data.get("speech_final", False)

                if transcript:
                    event = FluxEvent(
                        type=FluxEventType.TRANSCRIPT,
                        transcript=transcript,
                        is_final=is_final,
                    )

                    # speech_final indicates semantic end of turn
                    if speech_final:
                        logger.debug(f"Flux: speech_final with transcript: {transcript}")
                        return FluxEvent(
                            type=FluxEventType.END_OF_TURN,
                            transcript=transcript,
                        )

                    return event

        # Error handling
        if msg_type == "Error":
            error_msg = data.get("message", "Unknown error")
            logger.error(f"Flux error: {error_msg}")
            return FluxEvent(type=FluxEventType.ERROR, error=error_msg)

        return None

    async def reconnect_with_backoff(self) -> None:
        """Reconnect with exponential backoff."""
        while not self._connected:
            try:
                logger.info(f"Reconnecting in {self._reconnect_delay}s...")
                await asyncio.sleep(self._reconnect_delay)
                await self.connect()
            except Exception as e:
                logger.error(f"Reconnection failed: {e}")
                self._reconnect_delay = min(
                    self._reconnect_delay * 2, self._max_reconnect_delay
                )
