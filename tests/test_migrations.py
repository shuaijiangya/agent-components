import asyncio
from pathlib import Path

import pytest

pytest.importorskip("alembic")

from alembic import command
from alembic.config import Config

from agent_components.core.contracts import StateEnvelope
from agent_components.storage.sqlite import SQLiteStateStore

pytestmark = pytest.mark.integration
CONFIG_PATH = Path(__file__).resolve().parents[1] / "alembic.ini"


async def test_initial_migration_creates_usable_store(tmp_path, monkeypatch):
    path = tmp_path / "migrated.db"
    monkeypatch.setenv("AGENT_COMPONENTS_DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    config = Config(str(CONFIG_PATH))
    await asyncio.to_thread(command.upgrade, config, "head")
    async with SQLiteStateStore(path) as store:
        saved = await store.save(
            "scope",
            "key",
            StateEnvelope(backend="test", schema_version="1", payload={}),
            expected_revision=0,
        )
        assert saved.revision == 1
