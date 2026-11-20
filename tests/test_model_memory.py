import asyncio

import pytest
from pydantic import ValidationError

from agent_components.core.contracts import Message, StateEnvelope
from agent_components.core.errors import RevisionConflictError
from agent_components.models.memory import MemorySnapshot, StateConversationMemory


@pytest.fixture
def state_store():
    pytest.importorskip("sqlalchemy")
    pytest.importorskip("aiosqlite")
    from agent_components.storage.sqlite import SQLiteStateStore

    return SQLiteStateStore


@pytest.fixture
def turn():
    return (Message(role="user", text="你好"), Message(role="assistant", text="您好"))


def test_memory_snapshot_defaults_are_immutable():
    snapshot = MemorySnapshot()
    assert snapshot.revision == 0
    assert snapshot.messages == ()
    with pytest.raises(ValidationError, match="frozen"):
        snapshot.revision = 1


@pytest.mark.parametrize(
    "roles",
    [
        ("user",),
        ("assistant", "user"),
        ("user", "user"),
        ("user", "system"),
        ("user", "assistant", "assistant", "user"),
    ],
)
def test_memory_snapshot_rejects_incomplete_or_unordered_turns(roles):
    with pytest.raises(ValidationError):
        MemorySnapshot(messages=tuple(Message(role=role, text="text") for role in roles))


def test_memory_snapshot_rejects_negative_revision():
    with pytest.raises(ValidationError):
        MemorySnapshot(revision=-1)


@pytest.mark.integration
async def test_memory_recovers_message_order_and_revision_after_sqlite_reopens(
    tmp_path, state_store, turn
):
    path = tmp_path / "states.db"
    next_turn = (Message(role="user", text="继续"), Message(role="assistant", text="好的"))
    async with state_store(path, create_tables=True) as store:
        memory = StateConversationMemory(store)
        assert await memory.load("session") == MemorySnapshot()
        await memory.save("session", turn, expected_revision=0)
        await memory.save("session", turn + next_turn, expected_revision=1)

    async with state_store(path) as store:
        memory = StateConversationMemory(store, namespace="model-conversations")
        snapshot = await memory.load("session")
        assert snapshot.revision == 2
        assert snapshot.messages == turn + next_turn


@pytest.mark.integration
async def test_memory_isolates_session_keys_and_namespaces(tmp_path, state_store, turn):
    async with state_store(tmp_path / "states.db", create_tables=True) as store:
        first = StateConversationMemory(store, namespace="first-model")
        second = StateConversationMemory(store, namespace="second-model")
        await first.save("session-a", turn, expected_revision=0)

        assert await first.load("session-b") == MemorySnapshot()
        assert await second.load("session-a") == MemorySnapshot()
        await second.save("session-a", (), expected_revision=0)
        assert (await second.load("session-a")).revision == 1
        assert (await first.load("session-a")).messages == turn


@pytest.mark.integration
async def test_memory_forwards_conflicts_and_preserves_the_winning_update(
    tmp_path, state_store, turn
):
    path = tmp_path / "states.db"
    alternative = (Message(role="user", text="Other"), Message(role="assistant", text="Reply"))
    async with state_store(path, create_tables=True) as store_a, state_store(path) as store_b:
        memory_a = StateConversationMemory(store_a)
        memory_b = StateConversationMemory(store_b)
        await memory_a.save("session", (), expected_revision=0)
        revision_a = (await memory_a.load("session")).revision
        revision_b = (await memory_b.load("session")).revision

        outcomes = await asyncio.gather(
            memory_a.save("session", turn, expected_revision=revision_a),
            memory_b.save("session", alternative, expected_revision=revision_b),
            return_exceptions=True,
        )

        assert outcomes.count(None) == 1
        assert sum(isinstance(item, RevisionConflictError) for item in outcomes) == 1
        successful_messages = turn if outcomes[0] is None else alternative
        assert await memory_a.load("session") == MemorySnapshot(
            revision=2, messages=successful_messages
        )


@pytest.mark.integration
async def test_memory_rejects_an_incomplete_save_before_writing(tmp_path, state_store, turn):
    async with state_store(tmp_path / "states.db", create_tables=True) as store:
        memory = StateConversationMemory(store)
        with pytest.raises(ValidationError):
            await memory.save("session", turn[:1], expected_revision=0)
        assert await memory.load("session") == MemorySnapshot()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("backend", "schema_version"), [("other-backend", "1"), ("model-conversation", "future")]
)
async def test_memory_rejects_incompatible_state_envelopes(
    tmp_path, state_store, backend, schema_version
):
    state = StateEnvelope(backend=backend, schema_version=schema_version, payload={"messages": []})
    async with state_store(tmp_path / "states.db", create_tables=True) as store:
        await store.save("model-conversations", "session", state, expected_revision=0)
        with pytest.raises(ValueError, match="incompatible"):
            await StateConversationMemory(store).load("session")


@pytest.mark.integration
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"messages": [], "revision": 0},
        {"messages": "invalid"},
        {"messages": [{"role": "user", "text": "incomplete"}]},
        {"messages": [{"role": "user", "text": "prompt"}, {"role": "system", "text": "wrong"}]},
    ],
)
async def test_memory_revalidates_messages_in_persisted_state(tmp_path, state_store, payload):
    state = StateEnvelope(backend="model-conversation", schema_version="1", payload=payload)
    async with state_store(tmp_path / "states.db", create_tables=True) as store:
        await store.save("model-conversations", "session", state, expected_revision=0)
        with pytest.raises(ValueError):
            await StateConversationMemory(store).load("session")
