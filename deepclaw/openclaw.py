"""OpenClaw chat/completions client."""

import asyncio
import logging
from typing import AsyncIterator

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0


class OpenClawClient:
    """
    OpenClaw gateway client using OpenAI-compatible chat/completions API.

    Supports streaming responses for lower perceived latency.
    """

    def __init__(
        self,
        gateway_url: str = "http://127.0.0.1:18789",
        gateway_token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.gateway_url = gateway_url.rstrip("/")
        self.gateway_token = gateway_token
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "OpenClawClient":
        self._client = httpx.AsyncClient(timeout=self.timeout)
        return self

    async def __aexit__(self, *args) -> None:
        if self._client:
            await self._client.aclose()

    def _get_headers(self) -> dict[str, str]:
        """Build request headers."""
        headers = {"Content-Type": "application/json"}
        if self.gateway_token:
            headers["Authorization"] = f"Bearer {self.gateway_token}"
        return headers

    async def chat_completion(
        self,
        messages: list[dict[str, str]],
        stream: bool = False,
    ) -> str | AsyncIterator[str]:
        """
        Send chat completion request to OpenClaw.

        Args:
            messages: Conversation history in OpenAI format
            stream: Whether to stream the response

        Returns:
            Complete response text, or async iterator of chunks if streaming
        """
        if not self._client:
            raise RuntimeError("OpenClawClient not initialized. Use async with.")

        url = f"{self.gateway_url}/v1/chat/completions"
        headers = self._get_headers()
        body = {
            "messages": messages,
            "stream": stream,
        }

        if stream:
            return self._stream_completion(url, headers, body)
        else:
            return await self._sync_completion(url, headers, body)

    async def _sync_completion(
        self, url: str, headers: dict, body: dict
    ) -> str:
        """Non-streaming completion request."""
        try:
            response = await self._client.post(url, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()

            # Extract content from OpenAI-compatible response
            choices = data.get("choices", [])
            if choices:
                return choices[0].get("message", {}).get("content", "")
            return ""

        except httpx.TimeoutException:
            logger.error(f"OpenClaw request timed out after {self.timeout}s")
            raise
        except httpx.HTTPStatusError as e:
            logger.error(f"OpenClaw API error: {e.response.status_code}")
            raise
        except Exception as e:
            logger.error(f"OpenClaw request failed: {e}")
            raise

    async def _stream_completion(
        self, url: str, headers: dict, body: dict
    ) -> AsyncIterator[str]:
        """Streaming completion request."""
        try:
            async with self._client.stream(
                "POST", url, headers=headers, json=body
            ) as response:
                response.raise_for_status()

                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            break

                        try:
                            import json
                            data = json.loads(data_str)
                            choices = data.get("choices", [])
                            if choices:
                                delta = choices[0].get("delta", {})
                                content = delta.get("content", "")
                                if content:
                                    yield content
                        except Exception:
                            continue

        except httpx.TimeoutException:
            logger.error(f"OpenClaw stream timed out after {self.timeout}s")
            raise
        except httpx.HTTPStatusError as e:
            logger.error(f"OpenClaw API error: {e.response.status_code}")
            raise


async def get_completion_text(
    client: OpenClawClient,
    messages: list[dict[str, str]],
) -> str:
    """
    Helper to get complete response text from OpenClaw.

    Uses non-streaming for simplicity since we need full text for TTS anyway.
    """
    return await client.chat_completion(messages, stream=False)
