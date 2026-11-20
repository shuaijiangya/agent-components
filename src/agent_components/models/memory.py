"""提供独立于供应商的会话历史契约及版本化存储。"""

from typing import Protocol, Self

from pydantic import Field, model_validator

from agent_components.core.contracts import Contract, Message, StateEnvelope, StoredState

_BACKEND = "model-conversation"
_SCHEMA_VERSION = "1"


class MemorySnapshot(Contract):
    """承载会话历史及其乐观并发控制版本。

    版本零表示会话尚未保存。历史按时间顺序保存完整的用户与助手消息对。
    """

    revision: int = Field(default=0, ge=0, strict=True)
    messages: tuple[Message, ...] = ()

    @model_validator(mode="after")
    def _validate_turns(self) -> Self:
        if len(self.messages) % 2 or any(
            message.role != ("assistant" if index % 2 else "user")
            for index, message in enumerate(self.messages)
        ):
            raise ValueError("conversation history must contain complete user and assistant pairs")
        return self


class ConversationMemory(Protocol):
    """约定按会话读取历史并按版本保存完整快照的能力。"""

    async def load(self, session_id: str) -> MemorySnapshot:
        """读取会话历史及版本，缺失的会话返回版本为零的空快照。"""
        ...

    async def save(
        self, session_id: str, messages: tuple[Message, ...], *, expected_revision: int
    ) -> None:
        """在存储版本匹配时保存完整历史，版本冲突时拒绝覆盖。"""
        ...


class _StateStore(Protocol):
    async def load(self, namespace: str, key: str, /) -> StoredState | None: ...

    async def save(
        self, namespace: str, key: str, state: StateEnvelope, /, *, expected_revision: int
    ) -> StoredState: ...


class StateConversationMemory:
    """在版本化状态存储中读写会话历史。"""

    def __init__(self, store: _StateStore, *, namespace: str = "model-conversations") -> None:
        """绑定由调用方管理生命周期的状态存储及会话命名空间。"""
        self._store = store
        self._namespace = namespace

    async def load(self, session_id: str) -> MemorySnapshot:
        """读取并校验已保存的会话历史，或返回空快照。

        Raises:
            ValueError: 状态类型、Schema 版本或消息内容不符合会话历史契约。
        """
        stored = await self._store.load(self._namespace, session_id)
        if stored is None:
            return MemorySnapshot()
        state = stored.state
        if (state.backend, state.schema_version) != (_BACKEND, _SCHEMA_VERSION):
            raise ValueError("incompatible conversation state backend or schema version")
        if state.payload.keys() != {"messages"}:
            raise ValueError("conversation state payload must contain only messages")
        return MemorySnapshot.model_validate(
            {"revision": stored.revision, "messages": state.payload["messages"]}
        )

    async def save(
        self, session_id: str, messages: tuple[Message, ...], *, expected_revision: int
    ) -> None:
        """验证完整历史，并使用预期版本提交状态。

        Raises:
            ValueError: 预期版本或消息内容不符合会话历史契约。
            RevisionConflictError: 当前存储版本与预期不符。
        """
        snapshot = MemorySnapshot(revision=expected_revision, messages=messages)
        state = StateEnvelope(
            backend=_BACKEND,
            schema_version=_SCHEMA_VERSION,
            payload={
                "messages": [message.model_dump(mode="json") for message in snapshot.messages]
            },
        )
        await self._store.save(
            self._namespace, session_id, state, expected_revision=expected_revision
        )
