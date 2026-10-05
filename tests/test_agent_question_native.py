"""Native OpenCode question dialogs as durable, supervisor-routed questions (prime-dune).

An OpenCode worker blocked on its native ``question`` tool used to sit there
while ``aq question list`` was empty: AQ read questions only from Claude/Codex
transcripts, and the open dialog made every nudge defer. These tests drive the
real :class:`OpenCodeQuestionStore` reader over a fixture SQLite file shaped
like OpenCode 1.18's, the real question service over PostgreSQL, and a fake
terminal that records every key it is sent.
"""

import json
import sqlite3
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from src.config import AppConfig
from src.database import Database
from src.event_bus import EventBus
from src.models import Agent, AgentState, Project, SessionRecord, Task, TaskStatus
from src.sessions import SessionProviderRegistry
from src.sessions import questions as questions_module
from src.sessions.fake import FakeProvider
from src.sessions.harness_parser import Harness
from src.sessions.harness_registry import HarnessRegistry
from src.sessions.native_questions import (
    OpenCodeQuestionStore,
    parse_native_turn_id,
    resolve_native_question_source,
)
from src.sessions.provider import Cap, NotSubmitted, SessionSpec
from src.sessions.questions import AgentQuestionService, _native_requires_human
from tests.db_fixtures import lease_dsn

ROOT = "ses_root"
CHILD = "ses_child"

SCOPE_QUESTION = [
    {
        "header": "Formulas scope",
        "question": "The task names object/variation formulas. Which mechanism does it expect?",
        "options": [
            {"label": "task_graph formulas", "description": "formulas/*.md templates"},
            {"label": "V2 playbook bundle rules only", "description": "no formula files"},
            {"label": "Both", "description": "formula markdown and the reviewed bundle"},
        ],
    },
    {
        "header": "Test coverage",
        "question": "Should I audit which scenarios are covered before adding tests?",
        "options": [{"label": "Audit first"}, {"label": "Fill gaps"}],
        "multiple": True,
    },
]


def _json(value):
    # OpenCode writes with JSON.stringify: no spaces.
    return json.dumps(value, separators=(",", ":"))


class OpenCodeDb:
    """A fixture store with the columns ``OpenCodeQuestionStore`` reads."""

    def __init__(self, data_dir, work_dir):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / "opencode.db"
        self.work_dir = work_dir
        self._seq = 0
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE session (id TEXT PRIMARY KEY, parent_id TEXT,
                    directory TEXT NOT NULL, time_created INTEGER NOT NULL,
                    time_updated INTEGER NOT NULL);
                CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
                    data TEXT NOT NULL);
                CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT NOT NULL,
                    session_id TEXT NOT NULL, time_created INTEGER NOT NULL,
                    time_updated INTEGER NOT NULL, data TEXT NOT NULL);
                """
            )

    def _conn(self):
        return sqlite3.connect(self.path)

    def _id(self, prefix):
        self._seq += 1
        return f"{prefix}_{self._seq:04d}"

    def session(self, session_id, *, at, parent=None, directory=None):
        ms = int(at * 1000)
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO session VALUES (?, ?, ?, ?, ?)",
                (session_id, parent, directory or self.work_dir, ms, ms),
            )

    def message(self, session_id, role, *, at):
        ms = int(at * 1000)
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
                (self._id("msg"), session_id, ms, ms, _json({"role": role})),
            )
            conn.execute("UPDATE session SET time_updated = ? WHERE id = ?", (ms, session_id))

    def tool(self, session_id, call_id, tool, *, at, status="running", inputs=None):
        ms = int(at * 1000)
        data = {
            "type": "tool",
            "tool": tool,
            "callID": call_id,
            "state": {"status": status, "input": inputs or {}, "time": {"start": ms}},
        }
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                (self._id("prt"), "msg_x", session_id, ms, ms, _json(data)),
            )
            conn.execute("UPDATE session SET time_updated = ? WHERE id = ?", (ms, session_id))

    def ask(self, session_id, call_id, questions=None, *, at, status="running"):
        self.tool(
            session_id,
            call_id,
            "question",
            at=at,
            status=status,
            inputs={"questions": questions or SCOPE_QUESTION},
        )

    def raw_part(self, session_id, data, *, at):
        ms = int(at * 1000)
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                (self._id("prt"), "msg_x", session_id, ms, ms, data),
            )

    def remove(self, session_id, call_id):
        """OpenCode's undo/revert deletes the call's part outright."""
        with self._conn() as conn:
            for part_id, data in conn.execute(
                "SELECT id, data FROM part WHERE session_id = ?", (session_id,)
            ).fetchall():
                if json.loads(data).get("callID") == call_id:
                    conn.execute("DELETE FROM part WHERE id = ?", (part_id,))

    def settle(self, session_id, call_id, status):
        with self._conn() as conn:
            for part_id, data in conn.execute(
                "SELECT id, data FROM part WHERE session_id = ?", (session_id,)
            ).fetchall():
                part = json.loads(data)
                if part.get("callID") != call_id:
                    continue
                part["state"]["status"] = status
                if status == "error":
                    part["state"]["error"] = "The user dismissed this question"
                conn.execute("UPDATE part SET data = ? WHERE id = ?", (_json(part), part_id))


class KeyProvider(FakeProvider):
    """The fake terminal plus guarded key input, recording every key."""

    capabilities = FakeProvider.capabilities | {Cap.INPUT}

    def __init__(self, config=None):
        super().__init__(config)
        self.keys = []
        self.on_key = None

    async def send_input(self, h, *, text=None, key=None):
        if self._get(h) is None:
            raise NotSubmitted(f"session {h.name!r} is gone")
        self.keys.append((h.name, h.instance_token, key or text))
        if self.on_key is not None:
            self.on_key(key)


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setattr(questions_module, "_NATIVE_CLEAR_SECONDS", 0.2)
    db = Database(lease_dsn("native_questions.db"))
    await db.initialize()
    await db.create_project(Project(id="p", name="Project"))
    await db.create_agent(
        Agent(id="worker", name="Worker", profile_id="coder", state=AgentState.BUSY)
    )
    await db.create_task(
        Task(
            id="t",
            project_id="p",
            title="Task",
            description="Do work",
            status=TaskStatus.IN_PROGRESS,
            assigned_agent_id="worker",
            claim_epoch=7,
        )
    )
    await db.update_agent("worker", current_task_id="t")
    await db.update_task("t", claim_epoch=7)
    now = time.time()
    work_dir = str(tmp_path / "slot-1")
    row = SessionRecord(
        id="s",
        project_id="p",
        task_id="t",
        profile_id="standard-high-opencode",
        harness="opencode",
        provider="fake",
        name="p-worker",
        lifecycle="pool",
        work_dir=work_dir,
        epoch="daemon",
        instance_token="original",
        started_at=now - 1000,
        state="running",
        agent_id="worker",
        claim_phase="active",
        last_claim_epoch=7,
        claim_phase_at=now - 500,
    )
    await db.create_session(row)
    provider = KeyProvider()
    for name, token in (("p-worker", "original"), ("p-neighbour", "neighbour")):
        await provider.start(
            SessionSpec(
                session_name=name, work_dir=work_dir, command=("fake",), instance_token=token
            )
        )
    registry = SessionProviderRegistry({"fake": KeyProvider})
    registry._instances["fake"] = provider
    config = AppConfig()
    config.sessions.enabled = True
    config.messages.enabled = True
    bus = EventBus(env="dev")
    escalations = []
    bus.subscribe("escalation.created.v1", escalations.append)
    store = OpenCodeDb(tmp_path / "opencode", work_dir)
    store.session(ROOT, at=now - 400)
    yield SimpleNamespace(
        db=db,
        row=row,
        provider=provider,
        registry=registry,
        config=config,
        bus=bus,
        store=store,
        data_dir=tmp_path / "opencode",
        escalations=escalations,
        now=now,
    )
    await db.close()


def service(env):
    def sources(harness, project_id=None):
        return OpenCodeQuestionStore(env.data_dir) if harness == "opencode" else None

    return AgentQuestionService(env.db, env.bus, env.registry, env.config, native_sources=sources)


async def asked(env, questions=None, *, call="call_1", session=ROOT):
    """A worker opens a dialog; one AQ tick later it is a durable question."""
    env.store.ask(session, call, questions, at=env.now - 100)
    svc = service(env)
    await svc.tick(now=env.now)
    rows = await env.db.list_agent_questions(session_id="s")
    assert len(rows) == 1
    return svc, rows[0]


async def global_supervisor(env):
    row = replace(
        env.row,
        id="super",
        name="n-supervisor--global",
        task_id=None,
        project_id=None,
        profile_id="supervisor",
        lifecycle="named",
        harness="claude",
        agent_id=None,
        claim_phase=None,
        instance_token="super-token",
    )
    await env.db.create_session(row)
    return {"kind": "session", "session_id": row.id, "project_id": None, "elevated": True}


def handler_for(env, svc):
    from src.commands.handler import CommandHandler

    return CommandHandler(
        SimpleNamespace(db=env.db, bus=env.bus, agent_questions=svc, plugin_registry=None),
        env.config,
    )


def pointer(question_id):
    return f"[aq question answered] Handle `aq message status question:{question_id}:answer --json`."


# -- the store ---------------------------------------------------------------


def test_store_reads_only_this_sessions_dialogs(tmp_path):
    now = time.time()
    work_dir = str(tmp_path / "slot-1")
    store = OpenCodeDb(tmp_path / "oc", work_dir)
    # A predecessor's abandoned dialog in the same slot, and another slot's.
    store.session("ses_old", at=now - 5000)
    store.ask("ses_old", "call_old", at=now - 5000)
    store.session("ses_other", at=now - 50, directory=str(tmp_path / "slot-2"))
    store.ask("ses_other", "call_other", at=now - 50)
    # This AQ session: a root conversation and a subagent below it.
    store.session(ROOT, at=now - 400)
    store.message(ROOT, "assistant", at=now - 90)
    store.ask(ROOT, "call_done", at=now - 300, status="completed")
    store.ask(ROOT, "call_open", at=now - 100)
    store.session(CHILD, at=now - 80, parent=ROOT)
    store.tool(ROOT, "call_task", "task", at=now - 80)  # a subagent is never "busy"
    store.ask(CHILD, "call_child", at=now - 60)
    store.tool(CHILD, "call_bash", "bash", at=now - 59)  # maybe a permission prompt
    store.message(CHILD, "assistant", at=now - 10)  # a subagent turn is not a resume

    snapshot = OpenCodeQuestionStore(tmp_path / "oc").snapshot(work_dir, now - 1000)

    assert [q.turn_id for q in snapshot.pending] == [
        f"opencode:{ROOT}:call_open",
        f"opencode:{CHILD}:call_child",
    ]
    assert snapshot.settled == {f"opencode:{ROOT}:call_done": "completed"}
    assert snapshot.busy == {CHILD}
    assert snapshot.sessions == {ROOT, CHILD}
    assert snapshot.assistant_activity == pytest.approx(now - 90, abs=0.01)
    # The TUI shows the whole tree's prompts in one view, permissions first:
    # neither dialog is the only thing an Escape could close here.
    assert not snapshot.dismissable(f"opencode:{ROOT}:call_open")
    assert not snapshot.dismissable(f"opencode:{CHILD}:call_child")
    assert snapshot.gone(f"opencode:{ROOT}:call_never_asked")
    assert not snapshot.gone("opencode:ses_unread:call_x")
    question = snapshot.pending[0]
    assert question.asked_at == pytest.approx(now - 100, abs=0.01)
    rendered = question.render()
    for text in (
        "Native opencode question dialog (2 questions)",
        "[Formulas scope] The task names object/variation formulas.",
        "- V2 playbook bundle rules only: no formula files",
        "(more than one option may be chosen)",
    ):
        assert text in rendered
    assert parse_native_turn_id(question.turn_id) == ("opencode", ROOT, "call_open")
    assert parse_native_turn_id("assistant-uuid-1") is None


def test_one_malformed_part_does_not_hide_the_sessions_dialog(tmp_path):
    now = time.time()
    store = OpenCodeDb(tmp_path / "oc", str(tmp_path))
    store.session(ROOT, at=now - 100)
    store.raw_part(ROOT, '{"type":"tool","tool":"question","state":{"status":"running"', at=now - 60)
    store.raw_part(ROOT, _json({"type": "tool", "tool": "question", "callID": "c",
                                "state": {"status": "running", "time": "soon",
                                          "input": {"questions": SCOPE_QUESTION}}}), at=now - 55)
    store.ask(ROOT, "call_ok", at=now - 50)

    snapshot = OpenCodeQuestionStore(tmp_path / "oc").snapshot(str(tmp_path), now - 1000)

    assert [q.call_id for q in snapshot.pending] == ["c", "call_ok"]
    assert snapshot.busy == {ROOT}  # an unreadable running prompt is never pressed past


def test_unreadable_store_is_unknown_not_empty(tmp_path):
    assert OpenCodeQuestionStore(tmp_path / "missing").snapshot(str(tmp_path), 0) is None
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "opencode.db").write_text("not a database")
    assert OpenCodeQuestionStore(broken).snapshot(str(tmp_path), 0) is None
    assert resolve_native_question_source("claude") is None
    assert isinstance(resolve_native_question_source("opencode"), OpenCodeQuestionStore)


def test_a_second_harness_on_the_opencode_cli_shares_its_store():
    # ``opencode-zen`` runs the same executable against OpenCode Zen, so its
    # question dialogs land in the same store and must be read (noble-delta-40).
    registry = HarnessRegistry()
    registry.upsert(Harness(
        id="opencode-zen", command="/home/u/.agent-queue/harness-bin/opencode",
        provider="opencode",
    ))
    registry.upsert(Harness(id="claude-alt", command="claude"))

    def store(harness):
        return resolve_native_question_source(harness, registry=registry)

    assert isinstance(store("opencode-zen"), OpenCodeQuestionStore)
    assert isinstance(store("opencode"), OpenCodeQuestionStore)
    assert store("claude-alt") is None
    assert resolve_native_question_source("opencode-zen") is None  # no registry: id only
    # The service's default resolver is the registry-aware one.
    db = type("NoDb", (), {})()  # the lock table holds it weakly
    service = AgentQuestionService(db, None, None, None, harness_registry=registry)
    assert isinstance(service._native_source("opencode-zen"), OpenCodeQuestionStore)
    assert service._native_source("claude-alt") is None


#: The ``opencode`` executable, as the hosted-pool harness of
#: ``noble-delta-40`` names it.
ZEN = "/home/u/.agent-queue/harness-bin/opencode"


def _zen_registry(system_command, *, project_command=None):
    """``opencode-zen`` at system scope, optionally shadowed in ``p1``."""
    registry = HarnessRegistry()
    registry.upsert(Harness(
        id="opencode-zen", command=system_command, provider="opencode",
    ))
    if project_command is not None:
        registry.upsert(Harness(
            id="opencode-zen", command=project_command, provider="opencode",
            project_id="p1",
        ))
    return registry


def test_a_project_override_moves_the_store_with_the_executable():
    # The launch path reads the project's harness file
    # (``orchestrator/execution.py`` resolves with ``task.project_id``), so p1
    # is running claude and has no OpenCode store to read. Reading the system
    # file instead would never see a dialog -- nobody told, session stuck.
    registry = _zen_registry(ZEN, project_command="claude")

    def store(project_id=None):
        return resolve_native_question_source(
            "opencode-zen", registry=registry, project_id=project_id
        )

    assert store("p1") is None
    assert isinstance(store("p2"), OpenCodeQuestionStore)  # untouched project
    assert isinstance(store(), OpenCodeQuestionStore)  # system scope
    assert resolve_native_question_source("opencode", registry=registry, project_id="p1")


def test_the_reverse_project_override_grants_the_store_to_one_project():
    registry = _zen_registry("/opt/bin/codex", project_command=ZEN)

    def store(project_id=None):
        return resolve_native_question_source(
            "opencode-zen", registry=registry, project_id=project_id
        )

    assert isinstance(store("p1"), OpenCodeQuestionStore)
    assert store("p2") is None
    assert store() is None


def test_an_override_that_keeps_the_command_keeps_the_store():
    registry = _zen_registry(ZEN, project_command="C:/bin/opencode.EXE")

    for project_id in ("p1", "p2", None):
        assert resolve_native_question_source(
            "opencode-zen", registry=registry, project_id=project_id
        ) is not None


def test_the_question_service_resolves_the_store_in_the_session_project():
    # The service resolves through ``_native_source`` per session row, so it
    # must carry that row's project: the same harness id, one project with the
    # store and one without.
    registry = _zen_registry(ZEN, project_command="claude")
    db = type("NoDb", (), {})()  # the lock table holds it weakly
    service = AgentQuestionService(db, None, None, None, harness_registry=registry)

    assert service._native_source("opencode-zen", "p1") is None
    assert isinstance(service._native_source("opencode-zen", "p2"), OpenCodeQuestionStore)
    # System scope (no project on the call) still answers from the system file.
    assert isinstance(service._native_source("opencode-zen"), OpenCodeQuestionStore)


async def test_the_scan_resolves_each_session_in_its_own_project(env):
    # The scan walks live session rows, so the project has to travel from the
    # row into the resolver: a session whose project shadows the harness id
    # must not be resolved as if it were on the system file.
    asked = []

    def sources(harness, project_id=None):
        asked.append((harness, project_id))
        # No store: only the resolution this scan asks for is under test.

    svc = AgentQuestionService(
        env.db, env.bus, env.registry, env.config, native_sources=sources
    )
    await svc.scan_native(now=env.now)

    assert asked == [("opencode", "p")]


@pytest.mark.parametrize(
    ("text", "human"),
    [
        ("Which mechanism does the task expect? - formulas - bundle", False),
        ("Should I continue with the next step?", False),
        ("Audit coverage first or fill gaps? (scope)", False),
        ("Should I push the branch and merge it?", True),
        ("May I delete the old fixtures?", True),
        ("Which API key should I use?", True),
        ("Do you approve this plan?", True),
        ("Deploy to production now?", True),
        ("Should I grant myself access to the prod database?", True),
        ("Run alembic upgrade against the operator database?", True),
        ("Rebase onto main and amend the commit?", True),
        ("Stop the daemon and restart it?", True),
        ("Kill the running tmux sessions?", True),
        ("Truncate the tasks table?", True),
        ("rm -rf the vault directory?", True),
        ("Commit with --no-verify?", True),
        ("Skip the flaky test?", True),
        ("Widen the change to refactor the scheduler too?", True),
        ("This looks out of scope; handle it anyway?", True),
    ],
)
def test_native_human_gates_are_risk_requests_not_choices(text, human):
    assert _native_requires_human(text) is human


# -- durable, routed, waiting ------------------------------------------------


async def test_native_dialog_becomes_one_durable_supervisor_question(env):
    svc, q = await asked(env)

    assert parse_native_turn_id(q["turn_id"]) == ("opencode", ROOT, "call_1")
    assert (q["session_id"], q["instance_token"], q["task_id"], q["claim_epoch"]) == (
        "s",
        "original",
        "t",
        7,
    )
    assert q["state"] == "supervisor" and not q["requires_human"]
    assert "V2 playbook bundle rules only" in q["question"]
    routed = await env.db.get_pending_messages("session", "supervisor-p")
    assert len(routed) == 1
    for text in (
        "blocked on a native question dialog",
        f"aq question answer {q['id']}",
        "A plain message cannot reach the worker",
        f"aq question escalate {q['id']}",
        "V2 playbook bundle rules only",
    ):
        assert text in routed[0].body
    # Waiting holds the claim and suspends the stall ladder; nothing is typed.
    assert await svc.is_waiting(env.row)
    assert (await env.db.get_task("t")).assigned_agent_id == "worker"
    assert env.provider.keys == [] and env.provider.sent_nudges == []

    # A re-read, and a restarted daemon, find the same identity.
    await svc.tick(now=env.now + 11)
    await service(env).tick(now=env.now + 22)
    assert [row["id"] for row in await env.db.list_agent_questions(session_id="s")] == [q["id"]]
    assert len(await env.db.get_pending_messages("session", "supervisor-p")) == 1


async def test_idle_or_unclaimed_sessions_are_never_read(env):
    env.store.ask(ROOT, "call_1", at=env.now - 100)
    await env.db.update_session("s", claim_phase="claiming")
    svc = service(env)
    await svc.tick(now=env.now)
    assert await env.db.list_agent_questions() == []


# -- answering ---------------------------------------------------------------


async def test_supervisor_answer_closes_the_exact_dialog_and_resumes_that_session(env):
    svc, q = await asked(env)
    env.provider.on_key = lambda key: env.store.settle(ROOT, "call_1", "error")
    scope = await global_supervisor(env)
    handler = handler_for(env, svc)

    result = await handler.execute(
        "question_answer", {"question_id": q["id"], "body": "Both", "_scope": scope}
    )

    assert result["state"] == "delivered", result
    assert result["answered_by"] == "session:super"
    assert "native dialog closed" in result["verification"]
    # One Escape and one pointer, into this claim's pane and nobody else's.
    assert env.provider.keys == [("p-worker", "original", "Escape")]
    assert env.provider.sent_nudges == [("p-worker", pointer(q["id"]))]
    stored = await env.db.get_message(f"question:{q['id']}:answer")
    assert "AQ closed your native question dialog" in stored.body
    assert stored.body.endswith("\nBoth")
    # Delivered is not done: the supervisor still sees it until the worker acts.
    listed = await handler.execute("question_list", {"_scope": scope})
    assert [(row["id"], row["state"]) for row in listed["questions"]] == [(q["id"], "delivered")]

    delivered_at = (await env.db.get_agent_question(q["id"]))["delivered_at"]
    env.store.message(ROOT, "assistant", at=delivered_at + 2)
    await svc.tick(now=env.now + 30)

    done = await env.db.get_agent_question(q["id"])
    assert (done["state"], done["reason"]) == ("resolved", "worker resumed after the answer")
    assert (await handler.execute("question_list", {"_scope": scope}))["questions"] == []
    assert env.provider.keys == [("p-worker", "original", "Escape")]
    assert env.escalations == []


async def test_delivery_re_reads_the_store_in_the_session_project(env):
    # The scan resolves per session row (see
    # ``test_the_scan_resolves_each_session_in_its_own_project``), but delivery
    # re-reads the store itself: ``_native_plan``, ``_press_escape`` and
    # ``_native_cleared`` each call ``_native_snapshot(row)`` with no source,
    # so the row's project has to travel from there too.  Reverse override --
    # the system ``opencode-zen`` runs codex, and project ``p`` alone
    # overrides it back to the ``opencode`` executable.  Resolved at system
    # scope the scan still finds the dialog (it resolves through the row) but
    # delivery then finds no store at all: the accepted answer stays
    # ``answered``, no Escape reaches the pane, and the worker stays blocked
    # on the dialog it cannot close.
    registry = HarnessRegistry()
    registry.upsert(Harness(id="opencode-zen", command="/opt/bin/codex", provider="opencode"))
    registry.upsert(Harness(
        id="opencode-zen", command="/usr/bin/opencode", provider="opencode", project_id="p",
    ))
    await env.db.update_session("s", harness="opencode-zen")
    svc = AgentQuestionService(
        env.db, env.bus, env.registry, env.config,
        native_sources=lambda h, project_id=None: resolve_native_question_source(
            h, env.data_dir, registry=registry, project_id=project_id
        ),
    )

    env.store.ask(ROOT, "call_1", at=env.now - 100)
    await svc.tick(now=env.now)
    rows = await env.db.list_agent_questions(session_id="s")
    assert len(rows) == 1
    q = rows[0]
    assert parse_native_turn_id(q["turn_id"]) == ("opencode", ROOT, "call_1")
    env.provider.on_key = lambda key: env.store.settle(ROOT, "call_1", "error")

    result = await handler_for(env, svc).execute(
        "question_answer",
        {"question_id": q["id"], "body": "Both", "_scope": await global_supervisor(env)},
    )

    assert result["state"] == "delivered", result
    assert env.provider.keys == [("p-worker", "original", "Escape")]


@pytest.mark.parametrize("change", ["claim", "instance"])
async def test_answer_for_a_moved_claim_is_rejected_and_types_nothing(env, change):
    svc, q = await asked(env)
    if change == "claim":
        await env.db.update_task("t", claim_epoch=8)
    else:
        await env.db.update_session("s", instance_token="replacement")

    result = await svc.answer(q["id"], "Both", actor="session:super", human=False)

    assert "stale" in result["error"]
    assert (await env.db.get_agent_question(q["id"]))["state"] == "stale"
    assert env.provider.keys == [] and env.provider.sent_nudges == []


async def test_dialog_answered_in_the_terminal_rejects_a_late_answer(env):
    svc, q = await asked(env)
    env.store.settle(ROOT, "call_1", "completed")
    await svc.tick(now=env.now + 11)

    resolved = await env.db.get_agent_question(q["id"])
    assert (resolved["state"], resolved["reason"]) == (
        "resolved",
        "native dialog answered in the terminal",
    )
    late = await svc.answer(q["id"], "Both", actor="session:super", human=False)
    assert late["error"] == "question is no longer awaiting an answer"
    assert env.provider.keys == [] and env.provider.sent_nudges == []


async def test_dismissed_dialog_with_an_idle_worker_still_gets_its_answer(env):
    svc, q = await asked(env)
    env.store.settle(ROOT, "call_1", "error")  # someone pressed Escape; nobody answered
    await svc.tick(now=env.now + 11)
    assert (await env.db.get_agent_question(q["id"]))["state"] == "supervisor"

    result = await svc.answer(q["id"], "Both", actor="session:super", human=False)

    assert result["state"] == "delivered"
    assert env.provider.keys == []  # the dialog is already gone
    assert env.provider.sent_nudges == [("p-worker", pointer(q["id"]))]


async def test_dismissed_dialog_after_which_the_worker_moved_on_resolves(env):
    svc, q = await asked(env)
    env.store.settle(ROOT, "call_1", "error")
    env.store.message(ROOT, "assistant", at=env.now - 5)
    await svc.tick(now=env.now + 11)
    resolved = await env.db.get_agent_question(q["id"])
    assert resolved["state"] == "resolved"
    assert resolved["reason"] == "worker continued after the native dialog was dismissed"


async def test_human_gate_stays_blocked_for_the_supervisor(env):
    gate = [
        {
            "header": "Delivery",
            "question": "Should I push the branch to main and merge it myself?",
            "options": [{"label": "Push and merge"}, {"label": "Leave it"}],
        }
    ]
    svc, q = await asked(env, gate)
    assert q["requires_human"]
    routed = await env.db.get_pending_messages("session", "supervisor-p")
    assert "human-required" in routed[0].body
    assert "--body '<option label" not in routed[0].body

    refused = await svc.answer(q["id"], "Push and merge", actor="session:super", human=False)

    assert refused["error"] == "this question requires a human answer"
    await svc.tick(now=env.now + 11)
    still = await env.db.get_agent_question(q["id"])
    assert still["state"] == "supervisor" and still["answer"] is None
    assert await svc.is_waiting(env.row)
    assert env.provider.keys == [] and env.provider.sent_nudges == []


# -- bounded stall handling ---------------------------------------------------


async def test_a_dialog_that_will_not_close_gets_two_presses_then_one_notice(env):
    svc, q = await asked(env)  # the fake dialog never records a dismissal

    first = await svc.answer(q["id"], "Both", actor="session:super", human=False)
    answered = time.time()
    assert first["state"] == "answered"
    assert "answer accepted" in first["verification"]
    escape = ("p-worker", "original", "Escape")
    # Never a quick second press: OpenCode reads that as "interrupt".
    await svc.tick(now=answered + 11)
    assert env.provider.keys == [escape]
    for offset in (41, 71, 101):
        await svc.tick(now=answered + offset)

    assert env.provider.keys == [escape] * 2
    assert env.provider.sent_nudges == []
    notice = await env.db.get_escalation(f"escalation-question-delivery-{q['id']}")
    assert notice["source_kind"] == "question_delivery"
    assert "will not press another key" in notice["investigation"]
    assert [e["source_kind"] for e in env.escalations] == ["question_delivery"]
    assert (await env.db.get_agent_question(q["id"]))["state"] == "answered"


@pytest.mark.parametrize("obstacle", ["busy", "unreadable"])
async def test_uncertain_evidence_never_presses_a_key(env, obstacle):
    svc, q = await asked(env)
    if obstacle == "busy":
        env.store.tool(ROOT, "call_bash", "bash", at=env.now - 50)
    else:
        env.store.path.unlink()

    accepted = await svc.answer(q["id"], "Both", actor="session:super", human=False)
    assert accepted["state"] == "answered"
    await svc.tick(now=env.now + 11)
    assert env.provider.keys == [] and env.provider.sent_nudges == []

    # Waiting is bounded: past the supervisor timeout, one notice and no key.
    late = env.now + svc._supervisor_timeout() + 60
    await svc.tick(now=late)
    await svc.tick(now=late + 11)
    assert env.provider.keys == []
    assert [e["source_kind"] for e in env.escalations] == ["question_delivery"]


async def test_unanswered_question_raises_one_notice_and_stays_the_supervisors(env):
    svc, q = await asked(env)
    late = q["created_at"] + svc._supervisor_timeout() + 1
    await svc.tick(now=late)
    await svc.tick(now=late + 11)

    still = await env.db.get_agent_question(q["id"])
    assert still["state"] == "supervisor" and not still["requires_human"]
    notice = await env.db.get_escalation(f"escalation-question-unanswered-{q['id']}")
    assert notice["source_kind"] == "question_unanswered"
    assert [e["source_kind"] for e in env.escalations] == ["question_unanswered"]
    # The notice is not an answer path: the supervisor can still answer.
    env.provider.on_key = lambda key: env.store.settle(ROOT, "call_1", "error")
    answered = await svc.answer(q["id"], "Both", actor="session:super", human=False)
    assert answered["state"] == "delivered"


async def test_no_turn_after_delivery_raises_one_resume_notice(env):
    svc, q = await asked(env)
    env.provider.on_key = lambda key: env.store.settle(ROOT, "call_1", "error")
    await svc.answer(q["id"], "Both", actor="session:super", human=False)
    delivered_at = (await env.db.get_agent_question(q["id"]))["delivered_at"]

    await svc.tick(now=delivered_at + 20)
    assert (await env.db.get_agent_question(q["id"]))["state"] == "delivered"
    late = delivered_at + svc._resume_grace() + 20
    await svc.tick(now=late)
    await svc.tick(now=late + 11)

    final = await env.db.get_agent_question(q["id"])
    assert (final["state"], final["reason"]) == (
        "resolved",
        "no worker turn after the answer; escalated",
    )
    assert [e["source_kind"] for e in env.escalations] == ["question_resume"]
    assert env.provider.keys == [("p-worker", "original", "Escape")]


# -- review regressions: tree-wide prompts, human gates, restarts -------------


@pytest.mark.parametrize("elsewhere", ["child_tool_running", "second_dialog"])
async def test_a_prompt_elsewhere_in_the_tree_blocks_the_press(env, elsewhere):
    # The TUI shows a subagent's permission before any question, and only the
    # first question of the tree: an Escape could reject one of those instead.
    env.store.session(CHILD, at=env.now - 90, parent=ROOT)
    if elsewhere == "child_tool_running":
        env.store.tool(CHILD, "call_bash", "bash", at=env.now - 80)
    else:
        env.store.ask(CHILD, "call_child", at=env.now - 80)
    env.store.ask(ROOT, "call_1", at=env.now - 100)
    svc = service(env)
    await svc.tick(now=env.now)
    q = next(
        row
        for row in await env.db.list_agent_questions(session_id="s")
        if row["turn_id"].endswith(":call_1")
    )

    result = await svc.answer(q["id"], "Both", actor="session:super", human=False)
    for offset in (11, 41, 71):
        await svc.tick(now=time.time() + offset)

    assert result["state"] == "answered"
    assert env.provider.keys == [] and env.provider.sent_nudges == []
    assert (await env.db.get_task_meta("t", f"question_native_dismissals:{q['id']}")) is None


async def test_subagent_dialog_closes_and_resumes_through_the_root(env):
    env.store.session(CHILD, at=env.now - 90, parent=ROOT)
    svc, q = await asked(env, call="call_child", session=CHILD)
    env.provider.on_key = lambda key: env.store.settle(CHILD, "call_child", "error")

    result = await svc.answer(q["id"], "Audit first", actor="session:super", human=False)

    assert result["state"] == "delivered"
    assert env.provider.keys == [("p-worker", "original", "Escape")]
    assert env.provider.sent_nudges == [("p-worker", pointer(q["id"]))]
    delivered_at = (await env.db.get_agent_question(q["id"]))["delivered_at"]
    env.store.message(CHILD, "assistant", at=delivered_at + 1)  # not the conversation AQ typed into
    await svc.tick(now=env.now + 30)
    assert (await env.db.get_agent_question(q["id"]))["state"] == "delivered"
    env.store.message(ROOT, "assistant", at=delivered_at + 2)
    await svc.tick(now=env.now + 60)
    assert (await env.db.get_agent_question(q["id"]))["reason"] == "worker resumed after the answer"


async def test_a_dismissed_human_gate_stays_open_when_the_worker_moves_on(env):
    gate = [{"header": "Delivery", "question": "Push the branch and merge it?",
             "options": [{"label": "Yes"}, {"label": "No"}]}]
    svc, q = await asked(env, gate)
    env.store.settle(ROOT, "call_1", "error")
    env.store.message(ROOT, "assistant", at=env.now - 5)
    await svc.tick(now=env.now + 11)

    still = await env.db.get_agent_question(q["id"])
    assert still["state"] == "supervisor" and still["requires_human"]
    assert await svc.is_waiting(env.row)


async def test_human_answer_through_the_escalation_closes_the_dialog(env):
    gate = [{"header": "Delivery", "question": "Push the branch and merge it?",
             "options": [{"label": "Yes"}, {"label": "No"}]}]
    svc, q = await asked(env, gate)
    escalated = await svc.escalate(q["id"], "Delivery is a human decision")
    accepted = await env.db.accept_escalation_reply(
        escalated["escalation_id"],
        transport="dashboard",
        external_message_id="test-reply:" + q["id"],
        verified_actor="human:dashboard:test",
        text="No: leave delivery to the integration owner",
    )
    await env.db.begin_escalation_action(
        escalated["escalation_id"],
        reply_id=accepted["reply"]["id"],
        expected_revision=accepted["escalation"]["revision"],
        idempotency_key="test-action:" + q["id"],
        action_kind="question_answer",
        target_id=q["id"],
        parameters={},
        executor="session:test-supervisor",
    )
    env.provider.on_key = lambda key: env.store.settle(ROOT, "call_1", "error")

    result = await svc.answer(
        q["id"],
        "No: leave delivery to the integration owner",
        actor="human:dashboard:test",
        human=True,
        verified_escalation_id=escalated["escalation_id"],
    )

    assert result["state"] == "delivered", result
    assert env.provider.keys == [("p-worker", "original", "Escape")]
    assert env.provider.sent_nudges == [("p-worker", pointer(q["id"]))]


async def test_a_restart_between_presses_keeps_the_count_and_the_spacing(env):
    _, q = await asked(env)  # the fake dialog never records a dismissal
    await service(env).answer(q["id"], "Both", actor="session:super", human=False)
    answered = time.time()
    escape = ("p-worker", "original", "Escape")

    await service(env).tick(now=answered + 11)  # a fresh daemon, too soon to press
    assert env.provider.keys == [escape]
    await service(env).tick(now=answered + 41)
    assert env.provider.keys == [escape] * 2
    await service(env).tick(now=answered + 71)
    await service(env).tick(now=answered + 101)
    assert env.provider.keys == [escape] * 2
    assert [e["source_kind"] for e in env.escalations] == ["question_delivery"]


async def test_a_delivered_answer_from_an_earlier_claim_raises_no_notice(env):
    svc, q = await asked(env)
    env.provider.on_key = lambda key: env.store.settle(ROOT, "call_1", "error")
    await svc.answer(q["id"], "Both", actor="session:super", human=False)
    await env.db.update_task("t", claim_epoch=8)
    await env.db.update_session("s", last_claim_epoch=8)

    await svc.tick(now=time.time() + svc._resume_grace() + 60)

    assert (await env.db.get_agent_question(q["id"]))["state"] == "delivered"
    assert env.escalations == []


async def test_an_undone_dialog_resolves_and_refuses_a_late_answer(env):
    svc, q = await asked(env)
    env.store.remove(ROOT, "call_1")
    await svc.tick(now=env.now + 11)

    gone = await env.db.get_agent_question(q["id"])
    assert gone["state"] == "resolved" and "no longer exists" in gone["reason"]
    assert not await svc.is_waiting(env.row)
    late = await svc.answer(q["id"], "Both", actor="session:super", human=False)
    assert late["error"] == "question is no longer awaiting an answer"


async def test_an_answer_overtaken_by_the_terminal_is_reported_undelivered(env):
    svc, q = await asked(env)
    env.store.settle(ROOT, "call_1", "completed")  # before AQ's next read

    result = await svc.answer(q["id"], "Both", actor="session:super", human=False)

    assert result["state"] == "resolved"
    assert result["error"].startswith("answer not delivered: native dialog answered")
    assert env.provider.keys == [] and env.provider.sent_nudges == []
