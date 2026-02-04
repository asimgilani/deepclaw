"""Call state machine with barge-in support."""

import asyncio
import logging
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Callable, Awaitable
from time import perf_counter

logger = logging.getLogger(__name__)


class CallState(Enum):
    IDLE = auto()
    LISTENING = auto()
    THINKING = auto()
    SPEAKING = auto()


@dataclass
class CallMetrics:
    """Track latencies for performance comparison."""

    end_of_turn_time: float | None = None
    openclaw_response_time: float | None = None
    tts_first_byte_time: float | None = None

    def log_summary(self) -> None:
        if all([self.end_of_turn_time, self.openclaw_response_time, self.tts_first_byte_time]):
            thinking_latency = self.openclaw_response_time - self.end_of_turn_time
            tts_latency = self.tts_first_byte_time - self.openclaw_response_time
            total_latency = self.tts_first_byte_time - self.end_of_turn_time
            logger.info(
                f"Latencies - OpenClaw: {thinking_latency*1000:.0f}ms, "
                f"TTS TTFB: {tts_latency*1000:.0f}ms, "
                f"Total: {total_latency*1000:.0f}ms"
            )


@dataclass
class CallSession:
    """Manages state for a single phone call."""

    call_sid: str
    state: CallState = CallState.IDLE
    transcript_buffer: str = ""
    conversation_history: list[dict] = field(default_factory=list)
    metrics: CallMetrics = field(default_factory=CallMetrics)

    # Callbacks for barge-in
    on_barge_in: Callable[[], Awaitable[None]] | None = None

    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def transition_to(self, new_state: CallState) -> None:
        """Thread-safe state transition with logging."""
        async with self._lock:
            old_state = self.state
            self.state = new_state
            logger.info(f"[{self.call_sid}] {old_state.name} -> {new_state.name}")

    async def handle_start_of_turn(self) -> None:
        """Handle Flux StartOfTurn event."""
        if self.state == CallState.SPEAKING:
            # Barge-in detected
            logger.info(f"[{self.call_sid}] Barge-in detected, stopping TTS")
            if self.on_barge_in:
                await self.on_barge_in()
            await self.transition_to(CallState.LISTENING)
        elif self.state == CallState.IDLE:
            await self.transition_to(CallState.LISTENING)

    async def handle_end_of_turn(self, transcript: str) -> None:
        """Handle Flux EndOfTurn event."""
        self.transcript_buffer = transcript
        self.metrics.end_of_turn_time = perf_counter()
        await self.transition_to(CallState.THINKING)

    def add_user_message(self, content: str) -> None:
        """Add user message to conversation history."""
        self.conversation_history.append({"role": "user", "content": content})

    def add_assistant_message(self, content: str) -> None:
        """Add assistant message to conversation history."""
        self.conversation_history.append({"role": "assistant", "content": content})

    def reset_metrics(self) -> None:
        """Reset metrics for new turn."""
        self.metrics = CallMetrics()


class SessionManager:
    """Manages multiple concurrent call sessions."""

    def __init__(self) -> None:
        self._sessions: dict[str, CallSession] = {}
        self._lock = asyncio.Lock()

    async def create_session(self, call_sid: str) -> CallSession:
        """Create a new call session."""
        async with self._lock:
            session = CallSession(call_sid=call_sid)
            self._sessions[call_sid] = session
            logger.info(f"Created session for call {call_sid}")
            return session

    async def get_session(self, call_sid: str) -> CallSession | None:
        """Get an existing session."""
        return self._sessions.get(call_sid)

    async def remove_session(self, call_sid: str) -> None:
        """Remove a session when call ends."""
        async with self._lock:
            if call_sid in self._sessions:
                del self._sessions[call_sid]
                logger.info(f"Removed session for call {call_sid}")
