"""Deepgram Aura-2 TTS streaming client."""

import asyncio
import logging
from typing import AsyncIterator

import httpx

logger = logging.getLogger(__name__)

DEEPGRAM_TTS_URL = "https://api.deepgram.com/v1/speak"


class TTSClient:
    """
    Deepgram Aura-2 TTS client with streaming support.

    Streams audio chunks for low-latency playback.
    Supports cancellation for barge-in handling.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "aura-2-en-us-luna",
        encoding: str = "mulaw",
        sample_rate: int = 8000,
        container: str = "none",
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.encoding = encoding
        self.sample_rate = sample_rate
        self.container = container

        self._client: httpx.AsyncClient | None = None
        self._current_stream: httpx.Response | None = None
        self._cancelled = False

    async def __aenter__(self) -> "TTSClient":
        self._client = httpx.AsyncClient(timeout=30.0)
        return self

    async def __aexit__(self, *args) -> None:
        if self._client:
            await self._client.aclose()

    def _build_url(self) -> str:
        """Build TTS URL with query parameters."""
        params = [
            f"model={self.model}",
            f"encoding={self.encoding}",
            f"sample_rate={self.sample_rate}",
            f"container={self.container}",
        ]
        return f"{DEEPGRAM_TTS_URL}?{'&'.join(params)}"

    async def synthesize_stream(self, text: str) -> AsyncIterator[bytes]:
        """
        Stream TTS audio chunks for the given text.

        Yields raw audio bytes suitable for Twilio media stream.
        Can be cancelled mid-stream for barge-in support.
        """
        if not self._client:
            raise RuntimeError("TTSClient not initialized. Use async with.")

        self._cancelled = False
        url = self._build_url()
        headers = {
            "Authorization": f"Token {self.api_key}",
            "Content-Type": "application/json",
        }
        body = {"text": text}

        first_chunk = True
        try:
            async with self._client.stream(
                "POST", url, headers=headers, json=body
            ) as response:
                self._current_stream = response
                response.raise_for_status()

                async for chunk in response.aiter_bytes(chunk_size=640):
                    if self._cancelled:
                        logger.debug("TTS stream cancelled (barge-in)")
                        break

                    if first_chunk:
                        logger.debug("TTS first byte received")
                        first_chunk = False

                    yield chunk

        except httpx.HTTPStatusError as e:
            logger.error(f"TTS API error: {e.response.status_code} - {e.response.text}")
            raise
        except httpx.RequestError as e:
            logger.error(f"TTS request error: {e}")
            raise
        finally:
            self._current_stream = None

    async def cancel(self) -> None:
        """Cancel the current TTS stream (for barge-in)."""
        self._cancelled = True
        logger.debug("TTS cancellation requested")

    @property
    def is_streaming(self) -> bool:
        """Check if currently streaming audio."""
        return self._current_stream is not None and not self._cancelled


class TTSFallback:
    """Pre-generated fallback audio for error cases."""

    FALLBACK_TEXT = "Sorry, I couldn't process that. Please try again."

    def __init__(self, tts_client: TTSClient) -> None:
        self.tts_client = tts_client
        self._cached_audio: bytes | None = None

    async def get_fallback_audio(self) -> AsyncIterator[bytes]:
        """Get fallback audio, caching it for reuse."""
        if self._cached_audio:
            # Yield cached audio in chunks
            chunk_size = 640
            for i in range(0, len(self._cached_audio), chunk_size):
                yield self._cached_audio[i : i + chunk_size]
        else:
            # Generate and cache
            chunks = []
            async for chunk in self.tts_client.synthesize_stream(self.FALLBACK_TEXT):
                chunks.append(chunk)
                yield chunk
            self._cached_audio = b"".join(chunks)
