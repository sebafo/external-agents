import asyncio
import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, Field

from .contracts import ChatMessage


class ConversationData(BaseModel):
    messages: list[ChatMessage] = Field(default_factory=list)
    replies: dict[str, str] = Field(default_factory=dict)

    def remember(self, activity_id: str, user_text: str, reply: str) -> None:
        self.messages.extend(
            [
                ChatMessage(role="user", content=user_text),
                ChatMessage(role="assistant", content=reply),
            ]
        )
        self.messages = self.messages[-20:]
        self.replies[activity_id] = reply
        self.replies = dict(list(self.replies.items())[-20:])


@dataclass
class Conversation:
    data: ConversationData
    save: Callable[[], Awaitable[None]]


class ConversationStore(Protocol):
    def turn(self, key: str) -> AbstractAsyncContextManager[Conversation]: ...

    async def close(self) -> None: ...


def conversation_key(agent: str, tenant: str, channel: str, conversation: str, user: str) -> str:
    # Length prefixes prevent ambiguous concatenation of conversation identifiers.
    value = "".join(f"{len(part)}:{part}" for part in (agent, tenant, channel, conversation, user))
    return hashlib.sha256(value.encode()).hexdigest()


class MemoryConversationStore:
    def __init__(self) -> None:
        self._data: dict[str, ConversationData] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def turn(self, key: str) -> AsyncIterator[Conversation]:
        async with self._locks.setdefault(key, asyncio.Lock()):
            data = self._data.get(key, ConversationData()).model_copy(deep=True)

            async def save() -> None:
                self._data[key] = data.model_copy(deep=True)

            yield Conversation(data, save)

    async def close(self) -> None:
        self._data.clear()
        self._locks.clear()
