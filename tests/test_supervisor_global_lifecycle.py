"""Global-supervisor cold-start: admin scope + isolated memory scope.

See dashboard-shell-v2 plan §Task 3.

Piggybacks on the same fake-provider fixtures used by
``tests/test_session_lens.py``: the lens is exercised against an
in-memory SQLite DB with a FakeProvider so nothing spawns a real
process.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.messages import SessionLens
from src.models import AgentProfile
from src.sessions import SessionProviderRegistry
from src.sessions.fake import FakeProvider
from src.sessions.harness_parser import Harness
from src.sessions.harness_registry import HarnessRegistry
from src.sessions.spec import SessionSpecBuilder, named_session_name


TEST_EPOCH = "epoch-globaltest"


@pytest.fixture
async def db(tmp_path):
    from src.database import Database

    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
def config():
    class _Sessions:
        provider = "fake"

    class _Global:
        idle_timeout_seconds = 2700

    class _Supervisor:
        global_ = _Global()

    class _Cfg:
        vault_root = "/tmp/vault"
        mcp_server = None
        sessions = _Sessions()
        supervisor = _Supervisor()

    return _Cfg()


@pytest.fixture
def providers(config):
    return SessionProviderRegistry({FakeProvider.name: FakeProvider}, config=config)


@pytest.fixture
def harness_registry():
    reg = HarnessRegistry()
    reg.upsert(
        Harness(
            id="claude",
            name="claude",
            command="claude",
            prompt_mode="arg",
            session_id_flag="--session-id",
            process_names=("claude",),
        )
    )
    return reg


@pytest.fixture
def spec_builder(config, harness_registry):
    return SessionSpecBuilder(config, harness_registry)


@pytest.fixture
def supervisor_profile():
    return AgentProfile(id="supervisor", name="Supervisor", harness="claude", lifecycle="named")


@pytest.fixture
def profiles_loader(supervisor_profile):
    async def _load(profile_id: str):
        if profile_id == "supervisor":
            return supervisor_profile
        return None

    return _load


@pytest.fixture
def token_store():
    store = MagicMock()
    store.mint = AsyncMock(return_value="aqs_globaltoken")
    return store


@pytest.fixture
def lens(db, providers, spec_builder, harness_registry, config, profiles_loader, token_store):
    return SessionLens(
        db=db,
        providers=providers,
        spec_builder=spec_builder,
        harness_registry=harness_registry,
        config=config,
        profiles_loader=profiles_loader,
        epoch=TEST_EPOCH,
        token_store=token_store,
    )


class TestGlobalSupervisorColdStart:
    async def test_supervisor_global_runtime_session_name(self):
        # Runtime name for the ``supervisor-global`` address must be
        # ``n-supervisor--global`` so the reconciler adopts it on restart.
        from src.messages.session_lens import _resolve_runtime_session_name

        assert (
            _resolve_runtime_session_name("session", "supervisor-global")
            == "n-supervisor--global"
        )
        assert named_session_name("supervisor", "global") == "n-supervisor--global"

    async def test_ensure_started_supervisor_global_mints_admin_token(
        self, lens, token_store, providers
    ):
        ok = await lens.ensure_started(
            kind="session", target_id="supervisor-global", project_id=None
        )
        assert ok is True
        token_store.mint.assert_awaited_once()
        call = token_store.mint.await_args.kwargs
        # Admin scope: elevated + project_id=None.
        assert call["project_id"] is None
        assert call["elevated"] is True
        assert call["task_id"] is None
        row = await lens._db.get_session_by_name("n-supervisor--global")
        assert call["session_instance_token"] == row.instance_token
        # Provider actually got a start with the ``n-supervisor--global``
        # runtime name so the reconciler can adopt it.
        starts = providers.create("fake").starts
        assert len(starts) == 1
        assert starts[0].session_name == "n-supervisor--global"
        assert not starts[0].env.get("AQ_PROJECT_ID")

    async def test_ensure_started_supervisor_global_row_persisted(self, lens):
        ok = await lens.ensure_started(
            kind="session", target_id="supervisor-global", project_id=None
        )
        assert ok is True
        row = await lens._db.get_session_by_name("n-supervisor--global")
        assert row is not None
        assert row.profile_id == "supervisor"
        assert row.state == "running"
        assert row.epoch == TEST_EPOCH
        assert row.project_id is None
        assert await lens._db.get_project("global") is None

    async def test_ensure_started_per_project_still_mints_project_scoped_token(
        self, db, lens, token_store
    ):
        # Regression: a per-project supervisor address must still mint a
        # project-scoped token (elevated=True, project_id=<pid>).
        from src.models import Project

        await db.create_project(Project(id="proj1", name="Proj1"))
        ok = await lens.ensure_started(
            kind="session", target_id="supervisor-proj1", project_id="proj1"
        )
        assert ok is True
        call = token_store.mint.await_args.kwargs
        assert call["project_id"] == "proj1"
        assert call["elevated"] is True
        row = await lens._db.get_session_by_name("n-supervisor--proj1")
        assert call["session_instance_token"] == row.instance_token
