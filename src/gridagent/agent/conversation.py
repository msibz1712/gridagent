"""Multi-turn conversations: follow-up questions that build on earlier answers.

The Claude API keeps no state between requests, so a conversation is just the
message history we send again with every new question. A `Conversation` holds:
- `messages`: the full history (user turns, assistant turns incl. thinking and
  tool calls, tool results), only ever appended to;
- `toolbox`: the run's ToolBox, so data fetched in earlier turns stays available
  under its ref (a follow-up like "and for solar?" reuses last week's data);
- `today`: fixed when the conversation starts, so "last week" stays consistent.

`ConversationStore` keeps conversations in memory with limits: a maximum number
of turns per conversation (long histories cost more and drift), an idle timeout
(after which a conversation is deleted) and a maximum number of conversations.
Nothing is written to disk; the run logs still record each question separately.
"""

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from gridagent.tools.toolbox import ToolBox

MAX_TURNS = 10
IDLE_TIMEOUT_SECONDS = 30 * 60
MAX_CONVERSATIONS = 200


class ConversationLimitError(Exception):
    """The conversation has reached its maximum number of questions."""


@dataclass
class Conversation:
    toolbox: ToolBox
    today: date
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    messages: list[dict[str, Any]] = field(default_factory=list)
    turns: int = 0
    max_turns: int = MAX_TURNS
    last_used: float = 0.0

    @property
    def turns_left(self) -> int:
        return max(self.max_turns - self.turns, 0)

    def check_capacity(self) -> None:
        if self.turns >= self.max_turns:
            raise ConversationLimitError(
                f"This conversation has reached its limit of {self.max_turns} questions. "
                "Please start a new conversation."
            )


class ConversationStore:
    """Thread-safe, in-memory conversations with idle expiry and a size cap."""

    def __init__(
        self,
        max_turns: int = MAX_TURNS,
        idle_timeout_seconds: float = IDLE_TIMEOUT_SECONDS,
        max_conversations: int = MAX_CONVERSATIONS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_turns = max_turns
        self.idle_timeout_seconds = idle_timeout_seconds
        self.max_conversations = max_conversations
        self._clock = clock
        self._conversations: dict[str, Conversation] = {}
        self._lock = threading.Lock()

    def get(self, conversation_id: str | None) -> Conversation | None:
        """The live conversation with this id, or None if unknown or expired."""
        if not conversation_id:
            return None
        with self._lock:
            self._expire()
            conversation = self._conversations.get(conversation_id)
            if conversation is not None:
                conversation.last_used = self._clock()
            return conversation

    def start(self, toolbox: ToolBox, today: date) -> Conversation:
        with self._lock:
            self._expire()
            while len(self._conversations) >= self.max_conversations:
                oldest = min(self._conversations.values(), key=lambda c: c.last_used)
                del self._conversations[oldest.id]
            conversation = Conversation(
                toolbox=toolbox, today=today, max_turns=self.max_turns, last_used=self._clock()
            )
            self._conversations[conversation.id] = conversation
            return conversation

    def delete(self, conversation_id: str) -> bool:
        with self._lock:
            return self._conversations.pop(conversation_id, None) is not None

    def __len__(self) -> int:
        with self._lock:
            self._expire()
            return len(self._conversations)

    def _expire(self) -> None:
        now = self._clock()
        expired = [
            cid for cid, c in self._conversations.items()
            if now - c.last_used > self.idle_timeout_seconds
        ]  # fmt: skip
        for cid in expired:
            del self._conversations[cid]
