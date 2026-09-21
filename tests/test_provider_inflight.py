"""In-flight provider failures: requeue instead of pausing into a dead provider.

``docs/specs/provider-failover.md`` D13, the in-flight rows (task
``bold-rapids.4``).  Three tiers:

* the pure decision (:func:`src.providers.inflight.decide`) and the hand-off
  note;
* the WIP checkpoint against real Git -- a bare ``origin`` plus a working
  clone, because every interesting case is a Git question (dirty tree,
  unpushed commits, a push that cannot land);
* the orchestrator on real PostgreSQL with the fake session provider: a
  startup death on a usage dialog, a mid-task ``RATE_LIMIT`` exit of a task
  session and of a pool session, a failed push, pool drain and every provider
  down.  No LLM, no CLI.
"""

from __future__ import annotations

import pathlib
import subprocess
import time
from types import SimpleNamespace

import pytest

from src.commands.handler import CommandHandler
from src.git.manager import GitManager
from src.intelligence_classes import IntelligenceClass
from src.models import (
    Agent,
    AgentProfile,
    AgentState,
    RepoSourceType,
    SessionRecord,
    Task,
    TaskStatus,
    Workspace,
)
from src.providers import inflight
from src.providers.availability import DEGRADED, DISABLED, EXHAUSTED
from src.providers.intent import PREFERRED
from src.sessions.harness_parser import Harness
from src.sessions.provider import SessionDiedDuringStartup, SessionSpec

CLASSES = {
    "standard-high": IntelligenceClass(
        "standard-high",
        "Standard high",
        "",
        {"anthropic": {"model": "claude-opus-5"}, "openai": {"model": "gpt-6"}},
    ),
}

RATE_LIMIT_PANE = "■ You've hit your usage limit. Try again in 2 hours. usage limit reached"


# -- the pure decision --------------------------------------------------------------


class _Availability:
    """The slice of ``ProviderAvailabilityService`` :func:`decide` reads."""

    def __init__(self, *, enforcing=True, down=()):
        self.enforcing = enforcing
        self.down = set(down)

    def is_unavailable(self, provider, now=None):
        return provider in self.down

    def effective_state(self, provider, now=None):
        return EXHAUSTED if provider in self.down else DEGRADED

    def row(self, provider):
        return SimpleNamespace(generation=7)


def _failure(kind, **kw):
    return inflight.ProviderFailure(kind=kind, provider="codex", harness="codex", **kw)


def test_a_signalled_failure_on_a_launchable_provider_is_suspect():
    avail = _Availability()
    assert inflight.decide(avail, _failure("startup_dialog", signal="usage")) == inflight.SUSPECT
    assert inflight.decide(avail, _failure("exit_rate_limit")) == inflight.SUSPECT
    assert inflight.decide(avail, _failure(inflight.LAUNCH_REFUSED)) == inflight.SUSPECT


def test_any_failure_on_an_unavailable_provider_is_tripped():
    avail = _Availability(down={"codex"})
    for kind in ("startup_dialog", "launch_failure", "exit_rate_limit", inflight.SESSION_EXIT):
        assert inflight.decide(avail, _failure(kind)) == inflight.TRIPPED


def test_an_unlabelled_failure_on_a_launchable_provider_is_not_the_providers():
    avail = _Availability()
    assert inflight.decide(avail, _failure("launch_failure")) == inflight.UNATTRIBUTED
    assert inflight.decide(avail, _failure(inflight.SESSION_EXIT)) == inflight.UNATTRIBUTED


def test_observe_mode_attributes_nothing():
    avail = _Availability(enforcing=False, down={"codex"})
    assert inflight.decide(avail, _failure("exit_rate_limit")) == inflight.UNATTRIBUTED
    assert inflight.decide(None, _failure("exit_rate_limit")) == inflight.UNATTRIBUTED
    assert inflight.decide(_Availability(), None) == inflight.UNATTRIBUTED


def test_structured_fields_come_from_the_startup_death():
    exc = SessionDiedDuringStartup(
        "s", detail="quarantine dialog 'usage-limit' matched during startup"
    )
    failure = inflight.ProviderFailure.from_startup_death(
        exc, provider="codex", harness="codex", profile_id="standard-high-codex"
    )
    # No typed signal on the exception: the name map still reads the dialog.
    assert (failure.kind, failure.dialog, failure.signal) == (
        "startup_dialog",
        "usage-limit",
        "usage",
    )
    plain = inflight.ProviderFailure.from_startup_death(
        SessionDiedDuringStartup("s", detail="process died"), provider="codex", harness="codex"
    )
    assert (plain.kind, plain.signal) == ("launch_failure", None)


def test_the_provider_pause_record_names_the_provider_state_and_generation():
    record = inflight.provider_pause_record(
        _Availability(),
        _failure("startup_dialog", signal="usage", dialog="usage-limit"),
        context=inflight.CONTEXT_SUSPECT,
        resume_after=130.0,
        now=100.0,
    )
    assert record["provider"] == "codex"
    assert record["state"] == DEGRADED and record["generation"] == 7
    assert record["context"] == "provider_suspect" and record["signal"] == "usage"


def test_the_hand_off_note_says_where_the_last_worker_stopped():
    checkpoint = inflight.Checkpoint(
        status="pushed", branch="aq/t0", head="a" * 40, wip_commit=True, pushed_branch="aq/t0"
    )
    handoff = inflight.build_handoff(
        task=SimpleNamespace(id="t0", branch_name="aq/t0"),
        session=SimpleNamespace(
            id="sess-1", profile_id="standard-high-codex", harness="codex", model="gpt-6"
        ),
        failure=_failure("exit_rate_limit"),
        verdict="rate_limit",
        reason="rate-limit text in final capture",
        checkpoint=checkpoint,
        disposition=inflight.SUSPECT,
        subtasks=[
            {"ordinal": 1, "title": "parse", "status": "done"},
            {"ordinal": 2, "title": "wire", "status": "in_progress"},
        ],
        now=100.0,
    )
    assert handoff["session_logs"] == "aq session logs sess-1"
    assert handoff["model"] == "gpt-6" and handoff["provider"] == "codex"
    assert handoff["subtasks"]["total"] == 2 and handoff["subtasks"]["settled"] == 1
    text = inflight.handoff_comment(handoff, "t0")
    assert "`standard-high-codex`" in text and "gpt-6" in text
    assert "pushed to origin/aq/t0" in text and inflight.WIP_COMMIT_MESSAGE in text
    assert "2. wire (in_progress)" in text
    assert "aq session logs sess-1" in text


# -- the WIP checkpoint against real Git -------------------------------------------


def _git(args: list[str], cwd) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _make_repo(
    root: pathlib.Path, *, branch: str = "aq/t0", work: pathlib.Path | None = None
) -> tuple[pathlib.Path, pathlib.Path]:
    """A bare origin with ``main``, and a working checkout on *branch*."""
    origin = root / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(origin)],
        check=True,
        capture_output=True,
    )
    work = work or root / "work"
    work.mkdir(parents=True, exist_ok=True)
    _git(["init", "--initial-branch=main"], work)
    _git(["config", "user.name", "Test"], work)
    _git(["config", "user.email", "t@t.com"], work)
    # What ``ensure_git_exclude`` gives every slot: AQ's control files never
    # ride a commit.
    (work / ".git" / "info" / "exclude").write_text("/.aq/\n/.aq-worktree.json\n")
    (work / "README.md").write_text("init\n")
    _git(["add", "-A"], work)
    _git(["commit", "-m", "init"], work)
    _git(["remote", "add", "origin", str(origin)], work)
    _git(["push", "origin", "main"], work)
    _git(["checkout", "-b", branch], work)
    return origin, work


def _do_some_work(work: pathlib.Path) -> str:
    """One commit nobody pushed, plus uncommitted edits and a new file."""
    (work / "done.py").write_text("print('committed')\n")
    _git(["add", "done.py"], work)
    _git(["commit", "-m", "half the work"], work)
    (work / "README.md").write_text("init\nedited\n")
    (work / "new.py").write_text("print('untracked')\n")
    return _git(["rev-parse", "HEAD"], work)


async def test_checkpoint_commits_the_uncommitted_work_and_pushes_the_branch(tmp_path):
    origin, work = _make_repo(tmp_path)
    committed = _do_some_work(work)

    checkpoint = await inflight.checkpoint_workspace(GitManager(), str(work), "t0")

    assert checkpoint.status == "pushed" and not checkpoint.at_risk
    assert checkpoint.wip_commit is True
    assert checkpoint.pushed_branch == "aq/t0"
    remote_tip = _git(["rev-parse", "refs/heads/aq/t0"], origin)
    assert checkpoint.head == remote_tip
    assert _git(["log", "-1", "--format=%s", remote_tip], origin) == inflight.WIP_COMMIT_MESSAGE
    # The earlier, unpushed commit rode along; nothing is left behind.
    assert _git(["merge-base", "--is-ancestor", committed, remote_tip], origin) == ""
    assert _git(["status", "--porcelain"], work) == ""
    assert "new.py" in _git(["ls-tree", "-r", "--name-only", remote_tip], origin)


async def test_checkpoint_of_a_clean_pushed_branch_changes_nothing(tmp_path):
    _origin, work = _make_repo(tmp_path)
    _git(["push", "origin", "aq/t0"], work)
    before = _git(["rev-parse", "HEAD"], work)

    checkpoint = await inflight.checkpoint_workspace(GitManager(), str(work), "t0")

    assert checkpoint.status == "clean" and checkpoint.wip_commit is False
    assert _git(["rev-parse", "HEAD"], work) == before


async def test_a_push_that_cannot_land_is_at_risk_and_keeps_the_commit(tmp_path):
    _origin, work = _make_repo(tmp_path)
    _do_some_work(work)
    _git(["remote", "set-url", "origin", str(tmp_path / "gone.git")], work)

    checkpoint = await inflight.checkpoint_workspace(GitManager(), str(work), "t0")

    assert checkpoint.status == "push_failed" and checkpoint.at_risk
    assert checkpoint.wip_commit is True  # committed locally; nothing discarded
    assert _git(["log", "-1", "--format=%s"], work) == inflight.WIP_COMMIT_MESSAGE


async def test_a_pause_checkpoint_survives_an_ignored_lock_sentinel(tmp_path):
    """The managed exclude ignores ``.agent-queue-lock``; naming it in a
    pathspec made ``git add`` exit 1, so every pause checkpoint of a workspace
    holding the sentinel failed and the failover hold had nothing to restore."""
    from unittest.mock import AsyncMock

    from src.orchestrator.task_checkpoint import CHECKPOINT_META, capture_checkpoint

    _origin, work = _make_repo(tmp_path)
    with (work / ".git" / "info" / "exclude").open("a") as fh:
        fh.write("/.agent-queue-lock\n")
    (work / ".agent-queue-lock").write_text("t0\nagent\n")
    (work / "new.py").write_text("print(1)\n")
    db = SimpleNamespace(set_task_meta=AsyncMock(), add_task_context=AsyncMock())

    await capture_checkpoint(db, GitManager(), "t0", str(work))

    key, saved = db.set_task_meta.await_args.args[1:]
    assert key == CHECKPOINT_META
    tree = _git(["ls-tree", "-r", "--name-only", saved["commit"]], work)
    assert "new.py" in tree and ".agent-queue-lock" not in tree


async def test_a_workspace_that_is_not_git_has_nothing_to_lose(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    checkpoint = await inflight.checkpoint_workspace(GitManager(), str(plain), "t0")
    assert checkpoint.status == "not_git" and not checkpoint.at_risk
    missing = await inflight.checkpoint_workspace(GitManager(), None, "t0")
    assert missing.status == "no_workspace" and not missing.at_risk


# -- the orchestrator on PostgreSQL ---------------------------------------------------


def _codex_harness() -> Harness:
    return Harness(
        id="codex", name="codex", command="codex", prompt_mode="arg", process_names=("codex",)
    )


async def _probe(provider, timeout):
    return "cannot_tell"


@pytest.fixture
async def orch(tmp_path):
    from tests.session_dispatch_helpers import create_session_project, make_session_orch

    orch = await make_session_orch(tmp_path)
    orch.session_spec_builder._intelligence_classes = dict(CLASSES)
    orch.harness_registry.upsert(_codex_harness())
    orch.provider_availability._probe_impl = _probe
    for pid, harness in (
        ("standard-high-claude", "claude"),
        ("standard-high-codex", "codex"),
    ):
        if await orch.db.get_profile(pid) is None:
            await orch.db.create_profile(
                AgentProfile(id=pid, name=pid, harness=harness, default_class="standard-high")
            )
        await orch.db.update_profile(pid, lifecycle="task", enabled=True)
    await create_session_project(orch)
    yield orch
    await orch.wait_for_running_tasks(timeout=5)
    await orch.provider_availability.close()
    await orch.db.close()


async def _task(orch, task_id, *, profile="standard-high-codex", priority=100):
    await orch.db.create_task(
        Task(
            id=task_id,
            project_id="p-1",
            title=task_id,
            description="d",
            status=TaskStatus.READY,
            priority=priority,
            profile_id=profile,
            intelligence_class="standard-high",
            provider_intent=PREFERRED,
        )
    )


async def _cycle(orch) -> None:
    from tests.session_dispatch_helpers import drain_running_tasks

    await orch.run_one_cycle()
    await drain_running_tasks(orch)
    await orch.provider_availability.wait_for_probes()


def _fake(orch):
    from tests.session_dispatch_helpers import fake_provider

    return fake_provider(orch)


async def test_a_startup_usage_dialog_resumes_on_another_provider_with_no_retry_spent(orch):
    """The acceptance criterion, startup half: the Codex account is out of
    usage, every Codex launch dies on the usage dialog; both tasks end up
    running on Claude, retry budget untouched, branch unchanged."""
    fake = _fake(orch)
    fake.script_startup_dialog("codex", "usage-limit", signal="usage")
    await _task(orch, "t0", priority=10)
    await _task(orch, "t1", priority=20)

    for _ in range(4):
        await _cycle(orch)
        if orch.provider_availability.is_unavailable("codex"):
            break
    assert orch.provider_availability.effective_state("codex") == EXHAUSTED
    assert 1 <= len(fake.dialog_deaths) <= 2

    tasks = {t.id: t for t in await orch.db.list_tasks(project_id="p-1")}
    for task in tasks.values():
        assert task.retry_count == 0
        assert task.status in (TaskStatus.READY, TaskStatus.PAUSED)
        if task.status == TaskStatus.PAUSED:
            # The first, uncorroborated signal: a short suspect pause with
            # its cause recorded -- never the flat 60 s launch-failure one.
            pause = await orch.db.get_task_meta(task.id, "provider_pause")
            assert pause["provider"] == "codex" and pause["context"] == "provider_suspect"
            assert task.resume_after <= time.time() + 31
    branches = {tid: t.branch_name for tid, t in tasks.items()}

    # The provider-failover playbook's sweep moves them to Claude -- one
    # pool-width at a time (D15): the urgent one first, the other held with
    # a visible reason until the first has started.
    handler = CommandHandler(orch, orch.config)
    result = await handler.execute("provider_reroute", {})
    assert [d["task_id"] for d in result["moved"]] == ["t0"], result
    assert {d["task_id"]: d["kind"] for d in result["held"]} == {
        "t1": "awaiting_failover_capacity"
    }
    assert await orch.db.get_task_meta("t0", "provider_pause") is None

    deaths = len(fake.dialog_deaths)
    await _cycle(orch)
    started = await orch.db.get_session_for_task("t0")
    assert started is not None and started.harness == "claude"

    result = await handler.execute("provider_reroute", {})
    assert [d["task_id"] for d in result["moved"]] == ["t1"], result
    for _ in range(2):
        await _cycle(orch)
    assert len(fake.dialog_deaths) == deaths  # nothing launched into codex again
    for tid in ("t0", "t1"):
        task = await orch.db.get_task(tid)
        assert task.profile_id == "standard-high-claude"
        assert task.rerouted_from == "standard-high-codex"
        assert task.retry_count == 0
        assert task.branch_name == branches[tid]
        assert await orch.db.get_task_meta(tid, "provider_pause") is None


async def test_an_unattributed_startup_death_keeps_todays_backoff(orch):
    fake = _fake(orch)
    await _task(orch, "t0")
    original = fake.start

    async def dying_start(spec):
        raise SessionDiedDuringStartup(spec.session_name, detail="process died before start")

    fake.start = dying_start
    try:
        await _cycle(orch)
    finally:
        fake.start = original
    task = await orch.db.get_task("t0")
    assert task.status == TaskStatus.PAUSED
    assert task.resume_after > time.time() + 50  # the flat 60 s
    assert await orch.db.get_task_meta("t0", "provider_pause") is None
    assert not orch.provider_availability.is_unavailable("codex")


async def test_observe_mode_keeps_todays_launch_failure_accounting(orch):
    orch.config.provider_failover.mode = "observe"
    fake = _fake(orch)
    fake.script_startup_dialog("codex", "usage-limit", signal="usage")
    await _task(orch, "t0")
    await _cycle(orch)
    task = await orch.db.get_task("t0")
    assert task.status == TaskStatus.PAUSED and task.resume_after > time.time() + 50
    assert await orch.db.get_task_meta("t0", "provider_pause") is None


async def test_a_launch_refused_on_an_unavailable_provider_returns_the_task_to_ready(orch):
    from unittest.mock import AsyncMock, MagicMock

    await _task(orch, "t0")
    await orch.provider_availability.set_state(
        "codex", DISABLED, by="human:test", reason="rotating", until=time.time() + 600
    )
    orch._emit_text_notify = AsyncMock()
    action = MagicMock(task_id="t0", agent_id="a-x", project_id="p-1")
    await orch._fail_session_launch(
        action,
        await orch.db.get_task("t0"),
        "provider codex is disabled",
        notify=False,
        failure=inflight.ProviderFailure(kind=inflight.LAUNCH_REFUSED, provider="codex"),
    )
    task = await orch.db.get_task("t0")
    assert task.status == TaskStatus.READY and task.resume_after is None
    orch._emit_text_notify.assert_not_awaited()


async def test_leaving_paused_drops_the_provider_pause_record(orch):
    await _task(orch, "t0")
    assert await orch.db.transition_task_with_meta(
        "t0",
        TaskStatus.PAUSED,
        meta={"provider_pause": {"provider": "codex"}},
        context="provider_suspect",
        resume_after=time.time() + 30,
    )
    assert (await orch.db.get_task_meta("t0", "provider_pause"))["provider"] == "codex"
    # The ordinary resume-at-deadline path, not the sweep.
    await orch.db.transition_task("t0", TaskStatus.READY, context="resume_timer")
    assert await orch.db.get_task_meta("t0", "provider_pause") is None


# -- mid-task exits ------------------------------------------------------------------


async def _launch_on_codex(orch, task_id: str = "t0", *, git_root=None):
    """Launch *task_id* on Codex; with *git_root*, its workspace is a real repo."""
    workspace = pathlib.Path(orch.config.workspace_dir) / "p-1"
    origin = None
    if git_root is not None:
        origin, _work = _make_repo(git_root, branch=f"aq/{task_id}", work=workspace)
    await _task(orch, task_id)
    await _cycle(orch)
    session = await orch.db.get_session_for_task(task_id)
    assert session is not None and session.harness == "codex"
    return session, workspace, origin


async def _die_on_usage_limit(orch, session) -> None:
    """The session prints the usage limit and exits with its task still open."""
    fake = _fake(orch)
    fake.feed_output(session.name, RATE_LIMIT_PANE, activity=False)
    fake.script_death(session.name)
    await orch.session_reconciler.tick()


async def test_a_mid_task_usage_limit_exit_checkpoints_pushes_and_hands_off(orch, tmp_path):
    """The acceptance criterion, mid-task half: the work is on the branch, the
    retry budget is untouched, the next worker is told where the last one
    stopped -- and once Codex trips the task resumes on Claude."""
    session, workspace, origin = await _launch_on_codex(orch, git_root=tmp_path / "git")
    committed = _do_some_work(workspace)
    mock_git, orch.git = orch.git, GitManager()

    await _die_on_usage_limit(orch, session)

    # The work reached origin as one WIP commit on the task branch.
    tip = _git(["rev-parse", "refs/heads/aq/t0"], origin)
    assert _git(["log", "-1", "--format=%s", tip], origin) == inflight.WIP_COMMIT_MESSAGE
    assert _git(["merge-base", "--is-ancestor", committed, tip], origin) == ""
    assert "new.py" in _git(["ls-tree", "-r", "--name-only", tip], origin)

    task = await orch.db.get_task("t0")
    assert task.retry_count == 0
    # One rate-limit exit is an uncorroborated signal: a short suspect pause
    # with its cause, not 900 s on the same provider.
    assert task.status == TaskStatus.PAUSED and task.resume_after <= time.time() + 31
    pause = await orch.db.get_task_meta("t0", "provider_pause")
    assert pause["provider"] == "codex" and pause["kind"] == "exit_rate_limit"
    assert await orch.db.get_task_meta("t0", "needs_attention") is None
    assert await orch.db.get_workspace_for_task("t0") is None  # released after the push

    handoff = await orch.db.get_task_meta("t0", inflight.HANDOFF_META)
    assert handoff["session_id"] == session.id
    assert handoff["session_logs"] == f"aq session logs {session.id}"
    assert handoff["profile_id"] == "standard-high-codex" and handoff["provider"] == "codex"
    assert handoff["verdict"] == "rate_limit" and handoff["wip_commit"] is True
    assert handoff["checkpoint"] == "pushed" and handoff["head"] == tip
    assert handoff["branch"] == "aq/t0"
    comments = (await orch.db.list_task_comments("t0"))["comments"]
    assert any("Provider failover hand-off" in c["body"] for c in comments)
    assert (await orch.db.get_session(session.id)).sleep_reason == "rate_limit"

    # The next worker's ``aq prime`` shows it.
    from tests.session_dispatch_helpers import prime_bodies, render_prime

    body = prime_bodies(await render_prime(orch, "t0"))["task_context"]
    assert "Provider failover hand-off" in body and f"aq session logs {session.id}" in body

    # Codex trips; the sweep resumes the provider pause at once and moves it.
    # A conversation id carried for a same-provider relaunch belongs to the
    # old CLI, so the move drops it.
    await orch.db.set_task_meta("t0", "session_resume_key", "codex-rollout-1")
    await orch.provider_availability.set_state(
        "codex", DISABLED, by="human:test", reason="out of usage", until=None
    )
    result = await CommandHandler(orch, orch.config).execute("provider_reroute", {})
    assert result["resumed"] == ["t0"] and [d["task_id"] for d in result["moved"]] == ["t0"]
    assert await orch.db.get_task_meta("t0", "session_resume_key") is None
    orch.git = mock_git
    await _cycle(orch)
    relaunched = await orch.db.get_session_for_task("t0")
    assert relaunched.id != session.id and relaunched.harness == "claude"
    task = await orch.db.get_task("t0")
    assert task.retry_count == 0 and task.profile_id == "standard-high-claude"


async def test_a_mid_task_exit_on_a_tripped_provider_goes_straight_back_to_the_queue(
    orch, tmp_path
):
    session, workspace, _origin = await _launch_on_codex(orch, git_root=tmp_path / "git")
    _do_some_work(workspace)
    orch.git = GitManager()
    await orch.provider_availability.set_state(
        "codex", DISABLED, by="human:test", reason="out of usage", until=None
    )
    # Productive death, not a rate limit: attributed because codex is down.
    fake = _fake(orch)
    await orch.db.update_session(session.id, started_at=time.time() - 3600)
    fake.script_death(session.name)
    await orch.session_reconciler.tick()

    task = await orch.db.get_task("t0")
    assert task.status == TaskStatus.READY and task.retry_count == 0
    assert await orch.db.get_task_meta("t0", "provider_pause") is None
    assert await orch.db.get_task_meta("t0", "needs_attention") is None
    handoff = await orch.db.get_task_meta("t0", inflight.HANDOFF_META)
    assert handoff["disposition"] == inflight.TRIPPED and handoff["checkpoint"] == "pushed"
    stopped = await orch.db.get_session(session.id)
    assert stopped.state == "stopped" and stopped.restarts == 0


async def test_a_failed_push_holds_the_task_in_place_and_discards_nothing(orch, tmp_path):
    session, workspace, _origin = await _launch_on_codex(orch, git_root=tmp_path / "git")
    _do_some_work(workspace)
    _git(["remote", "set-url", "origin", str(tmp_path / "unreachable.git")], workspace)
    mock_git, orch.git = orch.git, GitManager()

    await _die_on_usage_limit(orch, session)

    task = await orch.db.get_task("t0")
    # An operator hold: nothing moves it, a human resumes it.
    assert task.status == TaskStatus.PAUSED and task.resume_after is None
    assert task.retry_count == 0
    assert await orch.db.get_task_meta("t0", "needs_attention") == inflight.PUSH_FAILED_ATTENTION
    handoff = await orch.db.get_task_meta("t0", inflight.HANDOFF_META)
    assert handoff["disposition"] == "held" and handoff["checkpoint"] == "push_failed"
    assert handoff["push_error"]
    # Nothing discarded: the WIP commit is on the local branch, and a Git
    # checkpoint the next slot restores was captured before release.
    assert _git(["log", "-1", "--format=%s", "aq/t0"], workspace) == inflight.WIP_COMMIT_MESSAGE
    saved = await orch.db.get_task_meta("t0", "manual_pause_checkpoint")
    assert saved and saved["branch"] == "aq/t0"
    assert _git(["rev-parse", saved["ref"]], workspace)

    # The re-route sweep never touches an operator hold.
    await orch.provider_availability.set_state(
        "codex", DISABLED, by="human:test", reason="out of usage", until=None
    )
    result = await CommandHandler(orch, orch.config).execute("provider_reroute", {})
    assert "t0" not in [d["task_id"] for d in result["moved"]]
    assert (await orch.db.get_task("t0")).profile_id == "standard-high-codex"

    orch.git = mock_git
    await orch.resume_task("t0")
    resumed = await orch.db.get_task("t0")
    assert resumed.status == TaskStatus.READY
    assert await orch.db.get_task_meta("t0", "needs_attention") is None


# -- pool sessions -------------------------------------------------------------------


async def _claimed_pool_session(orch, tmp_path, task_id: str, n: int) -> SessionRecord:
    """A codex pool worker mid-task on *task_id*, as the claim leaves it."""
    fake = _fake(orch)
    agent_id, sid = f"agent-{n}", f"pool-{n}"
    await _task(orch, task_id)
    await orch.db.create_agent(
        Agent(
            id=agent_id,
            name=agent_id,
            profile_id="standard-high-codex",
            state=AgentState.BUSY,
            current_task_id=task_id,
        )
    )
    path = tmp_path / f"pool-ws-{n}"
    path.mkdir()
    await orch.db.create_workspace(
        Workspace(
            id=f"pool-ws-{n}",
            project_id="p-1",
            workspace_path=str(path),
            source_type=RepoSourceType.LINK,
            kind_id="project-repo",
            locked_by_agent_id=agent_id,
            locked_by_task_id=task_id,
        )
    )
    await orch.db.transition_task(
        task_id, TaskStatus.IN_PROGRESS, context="pool_claim", assigned_agent_id=agent_id,
        force=True,
    )
    name = f"p-standard-high-codex--p-1--{n}"
    token = f"tok-{n}"
    await fake.start(
        SessionSpec(session_name=name, work_dir=str(path), command=("codex",), instance_token=token)
    )
    task = await orch.db.get_task(task_id)
    row = SessionRecord(
        id=sid,
        project_id="p-1",
        profile_id="standard-high-codex",
        harness="codex",
        provider="fake",
        name=name,
        lifecycle="pool",
        work_dir=str(path),
        epoch=orch.daemon_epoch,
        instance_token=token,
        started_at=time.time() - 3600,
        task_id=task_id,
        state="running",
        agent_id=agent_id,
        claim_phase="active",
        claim_phase_at=time.time(),
        last_claim_epoch=task.claim_epoch,
    )
    await orch.db.create_session(row)
    return row


async def test_a_pool_session_usage_limit_exit_requeues_without_a_key_quarantine(
    orch, tmp_path
):
    await orch.db.update_profile("standard-high-codex", lifecycle="pool", max_active=2)
    orch.git = GitManager()  # the pool slots here are plain directories: nothing to push
    first = await _claimed_pool_session(orch, tmp_path, "t0", 1)
    await _die_on_usage_limit(orch, first)

    task = await orch.db.get_task("t0")
    assert task.status == TaskStatus.PAUSED and task.resume_after <= time.time() + 31
    assert task.retry_count == 0 and task.assigned_agent_id is None
    assert (await orch.db.get_task_meta("t0", "provider_pause"))["context"] == "provider_suspect"
    # The provider's state governs now -- not a 900 s (project, profile) quarantine.
    assert orch._pool_quarantine == {}
    stopped = await orch.db.get_session(first.id)
    assert stopped.state == "stopped" and stopped.task_id is None
    assert (await orch.db.get_agent("agent-1")).state != AgentState.BUSY
    assert await orch.db.get_workspace_for_task("t0") is None
    assert (await orch.db.get_task_meta("t0", inflight.HANDOFF_META))["session_id"] == first.id

    # A second session corroborates: codex is exhausted, the task goes back to
    # the queue for the sweep, and codex pools size to zero.
    second = await _claimed_pool_session(orch, tmp_path, "t1", 2)
    await _die_on_usage_limit(orch, second)
    assert orch.provider_availability.effective_state("codex") == EXHAUSTED
    task = await orch.db.get_task("t1")
    assert task.status == TaskStatus.READY and task.retry_count == 0
    assert orch._pool_quarantine == {}
    from src.scheduler import PoolKey

    measurement = await orch._measure_pools()
    assert measurement.bounds[PoolKey("standard-high-codex")] == (0, 0)


async def test_pool_sessions_on_an_unavailable_provider_drain_and_busy_ones_are_left_alone(
    orch, tmp_path
):
    await orch.db.update_profile("standard-high-codex", lifecycle="pool", max_active=2)
    orch.config.swarm.enabled = True
    orch.git = GitManager()
    busy = await _claimed_pool_session(orch, tmp_path, "t0", 1)
    idle = await _claimed_pool_session(orch, tmp_path, "t1", 2)
    await orch.db.update_session(idle.id, task_id=None, claim_phase=None)
    await orch.provider_availability.set_state(
        "codex", DISABLED, by="human:test", reason="out of usage", until=None
    )

    await orch._reconcile_pools()
    await orch.wait_for_pool_launches()
    # A busy session still making turns is recovery evidence, not a target.
    row = await orch.db.get_session(busy.id)
    assert row.state == "running" and row.task_id == "t0"
    assert (await orch.db.get_task("t0")).status == TaskStatus.IN_PROGRESS

    # The idle one is told to drain on its next claim.
    handler = CommandHandler(orch, orch.config)
    handler._current_scope = {"kind": "session", "session_id": idle.id, "project_id": "p-1"}
    result = await handler._cmd_task_claim({"next": True})
    assert result["result"] == "drain_requested", result

    # When the busy one then dies on the limit, its task is requeued, not lost.
    await _die_on_usage_limit(orch, busy)
    task = await orch.db.get_task("t0")
    assert task.status == TaskStatus.READY and task.retry_count == 0


async def test_every_provider_down_holds_the_task_and_launches_nothing(orch, tmp_path):
    session, workspace, _origin = await _launch_on_codex(orch, git_root=tmp_path / "git")
    _do_some_work(workspace)
    mock_git, orch.git = orch.git, GitManager()
    for provider in ("claude", "codex"):
        await orch.provider_availability.set_state(
            provider, DISABLED, by="human:test", reason="all down", until=None
        )

    await _die_on_usage_limit(orch, session)
    task = await orch.db.get_task("t0")
    assert task.status == TaskStatus.READY and task.retry_count == 0
    assert (await orch.db.get_task_meta("t0", inflight.HANDOFF_META))["checkpoint"] == "pushed"

    result = await CommandHandler(orch, orch.config).execute("provider_reroute", {})
    assert result["moved"] == []
    assert {d["task_id"]: d["kind"] for d in result["held"]} == {
        "t0": "all_providers_unavailable"
    }
    orch.git = mock_git
    starts = len(_fake(orch).starts)
    for _ in range(3):
        await _cycle(orch)
    assert len(_fake(orch).starts) == starts
    task = await orch.db.get_task("t0")
    assert task.status == TaskStatus.READY and task.profile_id == "standard-high-codex"
    hold = await orch.provider_availability.hold_for(task)
    assert hold["kind"] == "all_providers_unavailable"
