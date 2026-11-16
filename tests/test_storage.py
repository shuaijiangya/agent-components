import asyncio

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("aiosqlite")

from agent_components.core.contracts import StateEnvelope
from agent_components.core.errors import RevisionConflictError
from agent_components.storage.sqlite import SQLiteStateStore

pytestmark = pytest.mark.integration


async def test_state_reopens_with_namespace_isolation(tmp_path):
    path = tmp_path / "states.db"
    state = StateEnvelope(backend="agentscope", schema_version="2.0.11", payload={"step": 2})
    async with SQLiteStateStore(path, create_tables=True) as store:
        saved = await store.save("tenant-a", "session", state, expected_revision=0)
        assert saved.revision == 1
        assert await store.load("tenant-b", "session") is None
    async with SQLiteStateStore(path) as store:
        assert (await store.load("tenant-a", "session")).state == state


async def test_compare_and_set_allows_only_one_competing_writer(tmp_path):
    path = tmp_path / "states.db"
    state = StateEnvelope(backend="test", schema_version="1", payload={})
    async with SQLiteStateStore(path, create_tables=True) as a, SQLiteStateStore(path) as b:
        await a.save("scope", "key", state, expected_revision=0)
        outcomes = await asyncio.gather(
            a.save("scope", "key", state, expected_revision=1),
            b.save("scope", "key", state, expected_revision=1),
            return_exceptions=True,
        )
        assert sum(isinstance(item, RevisionConflictError) for item in outcomes) == 1
        assert (await a.load("scope", "key")).revision == 2
        with pytest.raises(RevisionConflictError):
            await a.save("scope", "key", state, expected_revision=0)


async def test_native_agentscope_sql_storage_without_starting_service(tmp_path):
    pytest.importorskip("agentscope")
    pytest.importorskip("apscheduler")
    from agent_components.adapters.agentscope.storage import create_native_sql_storage

    async with create_native_sql_storage(tmp_path / "native.db", create_tables=True) as store:
        assert await store.list_agents("tenant-test") == []
