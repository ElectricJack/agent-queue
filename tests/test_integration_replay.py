"""Recorded replay and deterministic crash checks before the git-first cutover.

Plan ``projects/agent-queue/plans/2026-10-04-git-first-integration-plan-2.md``
sections 4, 6 and 7. The stage-2 modules (lock, checks, reviews, batches,
repair, train) are being built on sibling branches; until they land here, this
file drives minimal reference seams named as the plan names them. The seams own
no delivery fact. Every decision reads the stage-1 Git facts (``git_truth``,
``source_trailer``, ``provenance``) from one fetched snapshot of a real bare
remote, and every mutation is an ordinary command recorded by ``Commands``.
None of them is a removed recovery control.

There are two kinds of evidence, and they are never confused:

* Reconstructed cases rebuild each stall family from the spec's description and
  must settle through ordinary visits.
* Historical cases replay operator-supplied sanitized captures from
  ``tests/fixtures/integration_replay/<case>/``. A missing capture is
  an explicit GAP: the case skips with that reason, and the report lists it as a
  gap, never as a historical pass.

Crashes are injected at named boundaries (``Faults``). A restart is a fresh
``Train`` over the same durable state (tasks, batches, leases, checks and
reviews stand in for their tables), and every case records remote OIDs and task
counts.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import pytest

from src.commands.handler import CommandHandler
from src.git.manager import GitError, GitManager
from src.integration.delivery_truth import DeliveryRequest, DeliveryState
from src.integration.git_truth import (
    GitTruth,
    commits_added,
    epic_complete,
    repair_progress,
)
from src.integration.provenance import CompletedSource, CompletionIdentity, GitProvenance
from src.integration.source_trailer import (
    parse_source_trailers,
    source_identity,
    with_source_trailers,
)

ZERO = "0" * 40
P, R = "p", "r"
MAIN = "refs/heads/main"
GENERATED = "generated/"
ESCALATE_AFTER = 3  # repair attempts before a new attempt is escalated

# The spec's recorded stalls. Both no-progress escalations are named separately.
HISTORICAL_STALLS = (
    "crisp-horizon-90", "vivid-quest-44", "epic-verifiers", "stale-branch-owners",
    "brisk-beacon-64", "swift-delta-90",
)

# Plan section 5: retired candidate, repair, recovery, subject and engine
# controls, as CommandHandler ids (``sweep`` dispatches development_sweep).
REMOVED_CONTROLS = frozenset({"integration_development_sweep"} | {
    "integration_" + name.replace("-", "_") for name in (
        "record-noop", "resolve-candidate-member", "reconcile-unmaterialized",
        "waive-history", "reevaluate-repair", "settle-delivered-batch", "retry-cleanup",
        "release-owner", "reserve-owner", "release-stale-owners", "adopt-legacy-deliveries",
        "bind-legacy-repositories", "close-delivered-pr", "clear-stale-request",
        "redrive-root", "materialize-root", "authorize-root", "redrive-child",
        "reopen-collection", "rebind-reused-identity", "rebind-repair",
        "recover-parent-head", "recover-preserved-repair", "rebind-detached-repair",
        "release-delegates", "recover-candidate-member", "adopt", "settle-parked",
        "migrate-provenance", "cancel-preserving", "release-held-gate",
        "engine-transfer", "development-engine-transfer", "shadow-report",
    )
})
# Removed controls whose handler is already gone from this checkout. Every other
# id must resolve to a CommandHandler method, so a misspelled id fails instead
# of silently guarding nothing; retiring a handler moves its id here.
RETIRED_CONTROLS = frozenset({
    "integration_adopt", "integration_adopt_legacy_deliveries",
    "integration_bind_legacy_repositories", "integration_cancel_preserving",
    "integration_clear_stale_request", "integration_development_sweep",
    "integration_materialize_root", "integration_migrate_provenance",
    "integration_rebind_reused_identity", "integration_reconcile_unmaterialized",
    "integration_release_delegates",
    "integration_retry_cleanup", "integration_settle_delivered_batch",
    "integration_settle_parked", "integration_shadow_report", "integration_waive_history",
})


# -- Replay report ------------------------------------------------------------

CASES: dict[str, dict] = {}


def replay_case(name: str, family: str, *assertions: str):
    """Register a reconstructed case, its family and the assertions it must check."""
    def register(fn):
        CASES[name] = {"family": family, "assertions": assertions, "test": fn.__name__}
        fn.replay_case = name
        return fn
    return register


class Replay:
    """Checks one case's declared assertions; ``done`` requires every one of them."""

    def __init__(self, name: str, key: str | None = None):
        self.name, self.declared, self.checked = name, CASES[name]["assertions"], []
        self.key = key or name

    def check(self, assertion: str, ok) -> None:
        assert assertion in self.declared, f"{self.name}: undeclared assertion {assertion!r}"
        assert ok, f"{self.name}: {assertion}"
        self.checked.append(assertion)

    async def done(self, env: Env) -> dict:
        """Every declared assertion checked; record remote OIDs, counts and commands."""
        env.commands.assert_no_removed()
        missing = [a for a in self.declared if a not in self.checked]
        assert not missing, f"{self.name}: declared but unchecked {missing}"
        listing = await git_out(env.git, env.root, "ls-remote", str(env.remote), "refs/heads/*")
        evidence = {
            "case": self.key, "assertions": self.checked, "counts": env.counts(),
            "remote": {ref: oid for oid, ref in (row.split() for row in listing.splitlines())
                       if "/aq-provenance/" not in ref},
            "commands": [command_id for command_id, _ in env.commands.log],
        }
        if out := os.environ.get("AQ_REPLAY_EVIDENCE"):
            Path(out).mkdir(parents=True, exist_ok=True)
            (Path(out) / f"{self.key}.json").write_text(json.dumps(evidence, indent=2))
        return evidence


@pytest.fixture
def replay(request):
    callspec = getattr(request.node, "callspec", None)
    name = request.function.replay_case
    return Replay(name, f"{name}[{callspec.id}]" if callspec else name)


@pytest.fixture(autouse=True)
def removed_controls_refuse(monkeypatch):
    """Any dispatch of a removed control through CommandHandler fails the case."""
    present = {c for c in REMOVED_CONTROLS if hasattr(CommandHandler, f"_cmd_{c}")}
    assert present | RETIRED_CONTROLS == REMOVED_CONTROLS, (
        f"unresolved removed controls: {sorted(REMOVED_CONTROLS - present - RETIRED_CONTROLS)}")
    assert not present & RETIRED_CONTROLS, f"retired but present: {sorted(present & RETIRED_CONTROLS)}"
    invoked: list[str] = []
    for command_id in present:
        async def refuse(self, args, _id=command_id):
            invoked.append(_id)
            raise AssertionError(f"removed recovery control invoked: {_id}")
        monkeypatch.setattr(CommandHandler, f"_cmd_{command_id}", refuse)
    yield invoked
    assert not invoked


# -- Crash injection and the command audit -----------------------------------

class Crash(Exception):
    """Injected process death at a named boundary; durable state survives it."""


class Faults:
    """Named boundaries: crash there once, or run an outside action there once."""

    def __init__(self):
        self.actions: dict[str, object] = {}
        self.seen: list[str] = []

    def crash(self, point: str) -> None:
        self.actions[point] = None

    def on(self, point: str, action) -> None:
        self.actions[point] = action

    async def __call__(self, point: str) -> None:
        self.seen.append(point)
        if point not in self.actions:
            return
        action = self.actions.pop(point)
        if action is None:
            raise Crash(point)
        await action()


class Commands:
    """Ordinary command ids the seams dispatch, in order: the audit the plan matches."""

    def __init__(self):
        self.log: list[tuple[str, dict]] = []

    def __call__(self, command_id: str, **args) -> None:
        if command_id in REMOVED_CONTROLS:
            raise AssertionError(f"removed recovery control invoked: {command_id}")
        self.log.append((command_id, args))

    def count(self, command_id: str) -> int:
        return sum(1 for logged, _ in self.log if logged == command_id)

    def assert_no_removed(self) -> None:
        assert not {logged for logged, _ in self.log} & REMOVED_CONTROLS


# -- Reference seams, named as the plan names them -----------------------------

class RemoteMoved(Exception):
    """The remote ref is not at the captured expected-old OID."""


class LeaseRefused(Exception):
    """Holder, fence or expiry do not authorize this managed publication."""


@dataclass(frozen=True)
class Lease:
    ref: str
    holder: str
    fence: int
    expires_at: float


class BranchLock:
    """Expiring per-ref lease. Holder, fence and expiry are checked with the push."""

    def __init__(self, clock):
        self.clock = clock
        self.leases: dict[str, Lease] = {}
        self.fences: dict[str, int] = {}
        self.mutex: dict[str, asyncio.Lock] = {}

    def acquire(self, ref: str, holder: str, ttl: float = 60.0) -> Lease | None:
        current = self.leases.get(ref)
        if current is not None and current.expires_at > self.clock():
            if current.holder != holder:
                return None
            lease = replace(current, expires_at=self.clock() + ttl)
        else:
            # Expired means free: a new holder gets a new fence, no stop proof.
            self.fences[ref] = self.fences.get(ref, 0) + 1
            lease = Lease(ref, holder, self.fences[ref], self.clock() + ttl)
        self.leases[ref] = lease
        return lease

    def live(self, lease: Lease) -> bool:
        current = self.leases.get(lease.ref)
        return current is not None and current.expires_at > self.clock() and (
            current.holder, current.fence) == (lease.holder, lease.fence)

    def release(self, lease: Lease) -> None:
        if self.live(lease):
            del self.leases[lease.ref]

    async def publish(
        self, git: GitManager, store: Path, lease: Lease, oid: str, *,
        expected_old: str, replace_ref: bool = False,
    ) -> None:
        """One short critical section: lease check, then the expected-old push."""
        async with self.mutex.setdefault(lease.ref, asyncio.Lock()):
            if not self.live(lease):
                raise LeaseRefused(f"{lease.holder} fence {lease.fence} does not hold {lease.ref}")
            try:
                await git.apush_validated_ref(
                    str(store), oid, lease.ref.removeprefix("refs/heads/"),
                    force_with_lease=replace_ref, expected_old_oid=expected_old,
                )
            except GitError as exc:
                if "expected target" in str(exc):
                    raise RemoteMoved(lease.ref) from exc
                raise


class Checks:
    """Exact-SHA check cache. The latest attempt of every required name decides."""

    def __init__(self, required=("ci",)):
        self.required = tuple(required)
        self.runs: dict[str, dict[str, dict[int, str]]] = {}
        self.requested: list[str] = []
        self.unavailable: set[str] = set()

    def request(self, sha: str) -> None:
        if sha not in self.requested:
            self.requested.append(sha)

    def report(self, sha: str, state: str, *, name: str | None = None, attempt: int = 1):
        for check in [name] if name else self.required:
            self.runs.setdefault(sha, {}).setdefault(check, {})[attempt] = state

    def latest(self, sha: str) -> list[str]:
        runs = self.runs.get(sha, {})
        return [runs[n][max(runs[n])] if runs.get(n) else "missing" for n in self.required]

    def green(self, sha: str) -> bool | None:
        if sha in self.unavailable:
            return None
        return all(state == "success" for state in self.latest(sha))

    def failed(self, sha: str) -> bool:
        return "failure" in self.latest(sha)


class TreeReviews:
    """Authorized verdict per repository and tree; never inherited by another tree."""

    def __init__(self):
        self.verdicts: dict[tuple[str, str], str] = {}
        self.requested: list[str] = []

    def verdict(self, tree: str) -> str | None:
        return self.verdicts.get((R, tree))

    def request(self, tree: str) -> None:
        if tree not in self.requested and self.verdict(tree) is None:
            self.requested.append(tree)

    def decide(self, tree: str, verdict: str) -> None:
        self.verdicts[(R, tree)] = verdict


@dataclass
class Batch:
    id: str
    target: str
    members: tuple[tuple[str, str], ...]
    base: str | None = None
    candidate: str | None = None
    state: str = "open"  # open | delivered | aborted
    repair_attempts: int = 0
    no_progress: bool = False  # a stored legacy fact; observed green outranks it

    @property
    def ref(self) -> str:
        return f"refs/heads/aq/batches/{self.id}"


class BatchStore:
    """Frozen ordered members per target; an aborted batch is never refrozen."""

    def __init__(self):
        self.batches: dict[str, Batch] = {}

    def open_for(self, target: str) -> Batch | None:
        return next((b for b in self.batches.values()
                     if b.target == target and b.state == "open"), None)

    def freeze(self, target: str, members: tuple[tuple[str, str], ...]) -> Batch:
        key = json.dumps([target, members])
        batch_id = hashlib.sha256(key.encode()).hexdigest()
        return self.batches.setdefault(batch_id, Batch(batch_id, target, members))

    def aborted(self, target: str) -> set[tuple[str, str]]:
        """Exact members of aborted batches: operator intent, never refrozen.

        A reopened task carries a new source and is a new member.
        """
        return {member for b in self.batches.values()
                if b.target == target and b.state == "aborted" for member in b.members}


@dataclass
class Task:
    id: str
    parent: str | None = None
    epic: bool = False
    status: str = "IN_PROGRESS"
    completion: str | None = None
    hold: bool = False
    review_required: bool = False
    repair_key: str | None = None  # "<batch id>:<attempt>" for ordinary repair tasks
    base: str | None = None  # the recorded source base, for whole-patch proof


class Repair:
    """Ordinary repair allocation recovered by batch and attempt identity."""

    @staticmethod
    async def allocate(env: Env, batch: Batch, faults: Faults, reason: str) -> Task:
        for task in env.tasks.values():
            if task.repair_key and task.repair_key.startswith(batch.id + ":") and (
                task.status != "COMPLETED"
            ):
                # A restarted or concurrent visit recovers the open writer.
                attempt = int(task.repair_key.rsplit(":", 1)[1])
                batch.repair_attempts = max(batch.repair_attempts, attempt)
                return task
        attempt = batch.repair_attempts + 1
        # The counter and the stored no-progress fact drive escalation and
        # priority only; they never gate, and only a red head reaches them.
        escalate = attempt > ESCALATE_AFTER or batch.no_progress
        task = Task(f"repair-{batch.id[:8]}-{attempt}", repair_key=f"{batch.id}:{attempt}")
        env.commands("task_create", task_id=task.id, reason=reason,
                     idempotency_key=task.repair_key,
                     priority="escalated" if escalate else "normal")
        if escalate:
            env.commands("batch_escalate", batch=batch.id, attempt=attempt)
        env.tasks[task.id] = task
        await faults("after_repair_create")
        batch.repair_attempts = attempt  # the counter moves on allocation, not per visit
        return task


# -- A real remote, a worker clone and the publisher store --------------------

async def git_out(git: GitManager, cwd: Path, *args: str, check: bool = True) -> str:
    result = await git.arun_git_result(list(args), cwd=str(cwd))
    if check and result.returncode:
        raise GitError(f"git {' '.join(args[:2])} failed: {result.stderr}")
    return (result.stdout or "").strip()


class Env:
    """Durable state that survives a restart, and the Git sites the cases drive."""

    def __init__(self, root: Path):
        self.git = GitManager()
        self.root, self.remote = root, root / "remote.git"
        self.work, self.store = root / "work", root / "store"
        self.now = [1_000.0]
        self.tasks: dict[str, Task] = {}
        self.batches = BatchStore()
        self.lock = BranchLock(lambda: self.now[0])
        self.checks = Checks()
        self.reviews = TreeReviews()
        self.commands = Commands()
        self.holds: set[str] = set()
        self.review_targets: set[str] = set()
        self.base = ""

    async def setup(self, bundle: Path | None = None) -> None:
        if bundle is None:
            await git_out(self.git, self.root, "init", "--bare", "-b", "main", str(self.remote))
        else:
            await git_out(self.git, self.root, "clone", "--mirror", str(bundle), str(self.remote))
        for site in (self.work, self.store):
            await git_out(self.git, self.root, "init", "-b", "main", str(site))
            for key, value in (("user.name", "Tester"), ("user.email", "test@example.com"),
                               ("commit.gpgsign", "false")):
                await git_out(self.git, site, "config", key, value)
            await git_out(self.git, site, "remote", "add", "origin", str(self.remote))
        if bundle is not None:
            self.base = await self.remote_oid(MAIN)
            return
        self.base = await self.commit("main", {"seed": "seed\n", "inputs/a": "a\n",
                                               "generated/index.txt": "inputs/a\n"},
                                      "initial", start="HEAD", push_to=MAIN)

    async def new_store(self, name: str) -> Path:
        """A restarted or concurrent train's own publisher checkout."""
        site = self.root / name
        await git_out(self.git, self.root, "init", "-b", "main", str(site))
        for key, value in (("user.name", "Tester"), ("user.email", "test@example.com"),
                           ("commit.gpgsign", "false")):
            await git_out(self.git, site, "config", key, value)
        await git_out(self.git, site, "remote", "add", "origin", str(self.remote))
        return site

    def advance(self, seconds: float) -> None:
        self.now[0] += seconds

    async def w(self, *args: str) -> str:
        return await git_out(self.git, self.work, *args)

    async def commit(self, branch: str, files: dict[str, str | None], message: str = "work",
                     *, start: str | None = None, push_to: str | None = None) -> str:
        """Commit in the worker clone and publish it like a worker push."""
        if start != "HEAD":
            await self.w("fetch", "--prune", "origin")
            await self.w("checkout", "-B", branch, start or f"origin/{branch}")
        oid = await self.stage(files, message)
        await self.w("push", "-f", "origin", f"HEAD:{push_to or 'refs/heads/' + branch}")
        return oid

    async def stage(self, files: dict[str, str | None], message: str) -> str:
        """Commit in the worker clone's current checkout without pushing."""
        for name, content in files.items():
            path = self.work / name
            if content is None:
                path.unlink()
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        await self.w("add", "-A")
        await self.w("commit", "-m", message)
        return await self.w("rev-parse", "HEAD")

    def add(self, task_id: str, *, parent: str | None = None, epic: bool = False, **kw) -> Task:
        self.tasks[task_id] = task = Task(task_id, parent, epic, **kw)
        return task

    async def epic(self, task_id: str, *, parent: str | None = None, **kw) -> Task:
        start = f"origin/epic/{parent}" if parent else "origin/main"
        await self.w("fetch", "--prune", "origin")
        await self.w("push", "origin", f"{start}:refs/heads/epic/{task_id}")
        return self.add(task_id, parent=parent, epic=True, **kw)

    async def complete(self, task_id: str, source: str, completion: str = "close-1") -> None:
        """Ordinary worker close: retain the exact pushed source, then close."""
        await GitProvenance(self.git, str(self.work), repository_url=str(self.remote)).write_completion(
            CompletedSource(CompletionIdentity(P, R, task_id, completion), source), artifact=True,
        )
        task = self.tasks[task_id]
        task.status, task.completion = "COMPLETED", completion
        self.commands("task_close", task_id=task_id, outcome="pass")

    async def remote_oid(self, ref: str) -> str | None:
        out = await git_out(self.git, self.root, "ls-remote", str(self.remote), ref)
        return out.split()[0] if out else None

    async def contains(self, ancestor: str, ref: str = MAIN) -> bool:
        await git_out(self.git, self.root, "--git-dir", str(self.remote), "rev-parse", ancestor)
        result = await self.git.arun_git_result(
            ["--git-dir", str(self.remote), "merge-base", "--is-ancestor", ancestor, ref],
            cwd=str(self.root),
        )
        return result.returncode == 0

    async def tree(self, oid: str) -> str:
        return await git_out(self.git, self.root, "--git-dir", str(self.remote),
                             "rev-parse", oid + "^{tree}")

    def target_of(self, task: Task) -> str:
        return f"refs/heads/epic/{task.parent}" if task.parent else MAIN

    def epic_of(self, ref: str) -> Task | None:
        task = self.tasks.get(ref.removeprefix("refs/heads/epic/"))
        return task if ref.startswith("refs/heads/epic/") and task and task.epic else None

    def request(self, task: Task, ref: str) -> DeliveryRequest:
        return DeliveryRequest(
            P, R, ref, task.id, 1.0, f"legacy-{task.id}", branch_name=f"task/{task.id}",
            completion_id=task.completion, claim_epoch=1, has_recorded_source=True,
            task_status=task.status,
        )

    def counts(self) -> dict[str, int]:
        repairs = sum(1 for t in self.tasks.values() if t.repair_key)
        return {"tasks": len(self.tasks), "repairs": repairs,
                "batches": len(self.batches.batches)}


@pytest.fixture
async def env(tmp_path):
    environment = Env(tmp_path)
    await environment.setup()
    return environment


# -- The reduced train ---------------------------------------------------------

def regenerate(checkout: Path) -> None:
    inputs = sorted(p.relative_to(checkout).as_posix() for p in (checkout / "inputs").rglob("*")
                    if p.is_file())
    (checkout / "generated" / "index.txt").write_text("".join(f"{p}\n" for p in inputs))


def migration_heads(checkout: Path) -> set[str]:
    revisions, downs = set(), set()
    for path in (checkout / "migrations").glob("*.txt") if (checkout / "migrations").exists() else ():
        fields = dict(line.split(": ", 1) for line in path.read_text().splitlines())
        revisions.add(fields["revision"])
        downs.add(fields.get("down", ""))
    return revisions - downs


class Train:
    """One level-triggered visit: fetch once, then each target independently."""

    def __init__(self, env: Env, *, holder: str = "train", faults: Faults | None = None,
                 store: Path | None = None):
        self.env, self.holder, self.store = env, holder, store or env.store
        self.faults = faults or Faults()
        self.truth = GitTruth(env.git)  # the immutable-fact cache dies with the process
        self.repair_progress: list[tuple[str, list[str]]] = []

    def targets(self) -> list[str]:
        def depth(task: Task) -> int:
            return 0 if task.parent is None else 1 + depth(self.env.tasks[task.parent])
        epics = sorted((t for t in self.env.tasks.values() if t.epic), key=depth, reverse=True)
        return [f"refs/heads/epic/{t.id}" for t in epics] + [MAIN]

    def children(self, ref: str) -> list[Task]:
        return sorted((t for t in self.env.tasks.values()
                       if not t.repair_key and self.env.target_of(t) == ref), key=lambda t: t.id)

    async def visit(self) -> dict[str, str]:
        env = self.env
        snapshot = await self.truth.snapshot(
            str(self.store), project_id=P, repository_id=R,
            repository_url=str(env.remote), target_ref=MAIN,
        )
        outcome = {}
        for ref in self.targets():
            view = snapshot if ref == MAIN else snapshot.for_target(ref)
            outcome[ref] = await self.target(view, ref)
        return outcome

    async def target(self, view, ref: str) -> str:
        env = self.env
        if view.observation.error or view.target_oid is None:
            return "unknown"
        if ref in env.holds:
            return "held"
        lease = env.lock.acquire(ref, self.holder)
        if lease is None:
            return "locked"
        # A crash propagates without release: a dead process keeps its lease
        # until it expires.
        outcome = await self._target(view, ref, lease)
        env.lock.release(lease)
        return outcome

    async def _target(self, view, ref: str, lease: Lease) -> str:
        env = self.env
        pending = []
        for child in self.children(ref):
            if child.status != "COMPLETED":
                continue
            proof = await view.is_delivered(env.request(child, ref), source_base=child.base)
            if proof.state == DeliveryState.UNKNOWN:
                return "unknown"
            if proof.state == DeliveryState.PENDING:
                pending.append((child.id, proof.source_oid))
        aborted = env.batches.aborted(ref)
        withheld = [member for member in pending if member in aborted]
        pending = [member for member in pending if member not in aborted]
        batch = env.batches.open_for(ref)
        if batch is None:
            if not pending:
                return "aborted" if withheld else await self._epic(view, ref)
            batch = env.batches.freeze(ref, tuple(pending))
            if batch.state != "open":
                # Aborted members never reach a freeze, so this batch settled:
                # this visit's snapshot predates the push.
                return "stale"
            env.commands("batch_freeze", target=ref, batch=batch.id, members=batch.members)
        return await self._advance(view, ref, batch, lease, pending)

    async def _advance(self, view, ref, batch: Batch, lease: Lease, pending) -> str:
        env, git = self.env, self.env.git
        members = {task_id for task_id, _ in batch.members}
        if not members & {task_id for task_id, _ in pending}:
            # Every member is in the target (ancestor, trailer, patch or tree),
            # whatever the stored batch intent says: settle, never re-push.
            return await self._settle(batch, ref)
        heads = view.observation.source_heads
        observed = heads.get("refs/remotes/origin/" + batch.ref.removeprefix("refs/heads/"))
        if observed and observed != batch.candidate:
            if await self._carries(view, batch, observed):
                if batch.candidate:
                    added = await commits_added(git, str(self.store), batch.candidate, observed)
                    self.repair_progress.append((batch.id, added))
                batch.candidate, batch.base = observed, view.target_oid
            else:
                batch.candidate = None  # an outside push is movement: rebuild over it
        if batch.candidate is None or observed is None or batch.base != view.target_oid:
            built = await self._build(batch, view.target_oid)
            if built is None:
                await Repair.allocate(env, batch, self.faults, "candidate conflict")
                return "repair"
            await self.faults("before_candidate_push")
            candidate_lease = env.lock.acquire(batch.ref, self.holder)
            if candidate_lease is None:
                return "candidate_locked"
            try:
                await env.lock.publish(git, self.store, candidate_lease, built,
                                       expected_old=observed or ZERO,
                                       replace_ref=observed is not None)
            except RemoteMoved:
                env.lock.release(candidate_lease)
                return "candidate_moved"
            env.commands("candidate_publish", batch=batch.id, candidate=built)
            await self.faults("after_candidate_push")  # pushed, not yet recorded
            env.lock.release(candidate_lease)
            # Recorded only after the push: a refused push leaves the batch stale.
            batch.candidate, batch.base = built, view.target_oid
        sha = batch.candidate
        # Observe trusted green before any repair, counter or no-progress fact.
        green = env.checks.green(sha)
        if green is None:
            return "checks_unavailable"
        if not green:
            env.checks.request(sha)
            if env.checks.failed(sha):
                await Repair.allocate(env, batch, self.faults, "candidate checks failed")
                return "repair"
            return "checks_pending"
        if ref in env.review_targets:
            tree = await git.atree_sha(str(self.store), sha)
            verdict = env.reviews.verdict(tree)
            if verdict is None:
                env.reviews.request(tree)
                return "review_pending"
            if verdict != "approved":
                return "rejected"
        await self.faults("before_target_push")
        try:
            await env.lock.publish(git, self.store, lease, sha, expected_old=view.target_oid)
        except RemoteMoved:
            return "target_moved"
        env.commands("target_publish", target=ref, candidate=sha, expected_old=view.target_oid)
        await self.faults("after_target_push")
        return await self._settle(batch, ref)

    async def _settle(self, batch: Batch, ref: str) -> str:
        await self.faults("before_settlement")
        batch.state = "delivered"
        self.env.commands("batch_settle", batch=batch.id, target=ref)
        return "delivered"

    async def _carries(self, view, batch: Batch, oid: str) -> bool:
        """A moved candidate is progress when it is on the target and has every member."""
        env = self.env
        if not await env.git.ais_ancestor(str(self.store), view.target_oid, oid):
            return False
        candidate = view.for_target(batch.ref)
        for task_id, _source in batch.members:
            task = env.tasks[task_id]
            proof = await candidate.is_delivered(env.request(task, batch.ref), source_base=task.base)
            if proof.state != DeliveryState.CONTAINED:
                return False
        return True

    async def _build(self, batch: Batch, target_oid: str) -> str | None:
        """The shared merge path: exact sources, AQ-Source trailers, regenerate."""
        env, store = self.env, self.store
        await git_out(env.git, store, "checkout", "--force", "--detach", target_oid)
        for task_id, source in batch.members:
            message = with_source_trailers(f"Merge {task_id} into {batch.target}",
                                           [source_identity(task_id, source)])
            result = await env.git.arun_git_result(
                ["merge", "--no-ff", "-m", message, source], cwd=str(store))
            if not result.returncode:
                continue
            conflicted = (await git_out(env.git, store, "diff", "--name-only",
                                        "--diff-filter=U")).splitlines()
            if conflicted and all(path.startswith(GENERATED) for path in conflicted):
                regenerate(store)
                await git_out(env.git, store, "add", "-A")
                await git_out(env.git, store, "commit", "--no-edit", "--cleanup=strip")
                continue
            await git_out(env.git, store, "merge", "--abort")
            return None
        regenerate(store)
        if await git_out(env.git, store, "status", "--porcelain"):
            await git_out(env.git, store, "commit", "-am", "Regenerate generated files")
        if len(migration_heads(store)) > 1:
            return None  # migration-head conflict: an ordinary repair for this batch
        return await git_out(env.git, store, "rev-parse", "HEAD")

    async def _epic(self, view, ref: str) -> str:
        """An epic completes as ordinary work when Git, checks and review say so."""
        env = self.env
        epic = env.epic_of(ref)
        children = self.children(ref)
        if epic is None or epic.status == "COMPLETED" or not children:
            return "idle"
        head = view.target_oid
        green = env.checks.green(head)
        tree = await env.git.atree_sha(str(self.store), head)
        verdict = env.reviews.verdict(tree)
        ready = await epic_complete(
            view, [env.request(child, ref) for child in children],
            green_oid=head if green else None,
            approved_tree=tree if verdict == "approved" else None,
            review_required=epic.review_required, held=epic.hold,
            source_bases={child.id: child.base for child in children if child.base},
        )
        if ready is None:
            return "unknown"
        if not ready:
            if green is False:
                env.checks.request(head)
            elif green and epic.review_required and verdict is None:
                env.reviews.request(tree)
            return "epic_waiting"
        await GitProvenance(env.git, str(self.store), repository_url=str(env.remote)).write_completion(
            CompletedSource(CompletionIdentity(P, R, epic.id, "train-1"), head), artifact=True,
        )
        epic.status, epic.completion = "COMPLETED", "train-1"
        env.commands("task_close", task_id=epic.id, outcome="pass")
        return "epic_completed"


async def child(env: Env, task_id: str, files: dict[str, str | None], *,
                parent: str | None = None, close: bool = True, **kw) -> str:
    """A worker branch from the current target head, pushed and (by default) closed."""
    target = f"epic/{parent}" if parent else "main"
    base = await env.remote_oid(f"refs/heads/{target}")
    env.add(task_id, parent=parent, base=base, **kw)
    source = await env.commit(f"task/{task_id}", files, f"{task_id} work",
                              start=f"origin/{target}")
    if close:
        await env.complete(task_id, source)
    return source


async def outside_push(env: Env, ref: str, files: dict[str, str | None], message: str) -> str:
    """Somebody else's ordinary push to a target (a human merge, another train)."""
    branch = ref.removeprefix("refs/heads/")
    return await env.commit(f"outside/{branch}", files, message, start=f"origin/{branch}",
                            push_to=ref)


async def outside_merge(env: Env, ref: str, *sources: str) -> str:
    """A legacy or human merge of exact sources into a target, without trailers."""
    branch = ref.removeprefix("refs/heads/")
    await env.w("fetch", "--prune", "origin")
    await env.w("checkout", "-B", f"outside/{branch}", f"origin/{branch}")
    for source in sources:
        await env.w("merge", "--no-ff", "-m", f"Merge {source[:12]}", source)
    await env.w("push", "origin", f"HEAD:{ref}")
    return await env.w("rev-parse", "HEAD")


async def delete_ref(env: Env, ref: str) -> None:
    """Somebody deletes a branch on the remote (a cleanup job, a human)."""
    await env.w("push", "origin", f":{ref}")


async def repair_push(env: Env, batch: Batch, holder: str, files: dict[str, str | None],
                      *, start: str | None = None, lease: Lease | None = None) -> str:
    """An ordinary repair writer: lease the candidate ref, push a fix on top of it."""
    observed = await env.remote_oid(batch.ref)
    branch = batch.ref.removeprefix("refs/heads/")
    await env.w("fetch", "--prune", "origin")
    await env.w("checkout", "-B", f"repair/{holder}", start or f"origin/{branch}")
    fix = await env.stage(files, f"repair by {holder}")
    lease = lease or env.lock.acquire(batch.ref, holder)
    assert lease is not None, f"{holder} could not lease {batch.ref}"
    await env.lock.publish(env.git, env.work, lease, fix, expected_old=observed or ZERO,
                           replace_ref=observed is not None)
    env.lock.release(lease)
    return fix


def green_all(env: Env, *shas: str, attempt: int = 1) -> None:
    for sha in shas:
        env.checks.report(sha, "success", attempt=attempt)


def producer(env: Env, *, skip=lambda sha: False) -> None:
    """A trusted check producer: every requested SHA with no run yet succeeds."""
    for sha in env.checks.requested:
        if sha not in env.checks.runs and sha not in env.checks.unavailable and not skip(sha):
            green_all(env, sha)


QUIET = {"idle", "held", "aborted", "rejected", "review_pending", "locked",
         "checks_unavailable", "epic_waiting"}


async def deliver(env: Env, train: Train | None = None, *, visits: int = 12,
                  produce: bool = True, skip=lambda sha: False) -> list[dict]:
    """Visit until nothing moves; a passing producer answers requested checks."""
    train = train or Train(env)
    history: list[dict] = []
    for _ in range(visits):
        history.append(await train.visit())
        if produce:
            producer(env, skip=skip)
        if len(history) > 1 and history[-1] == history[-2] and set(history[-1].values()) <= (
            QUIET | {"checks_pending"}
        ):
            break
    return history


# -- Recorded stall families, reconstructed -----------------------------------

@replay_case(
    "brisk-beacon-64", "no-progress escalation on a root batch",
    "15/15 green read before any counter", "promoted despite counter and no-progress fact",
    "green consumes no attempt and raises no escalation",
    "no repair allocated", "every child contained in main",
)
async def test_brisk_beacon_64_green_root_head_promotes_despite_no_progress(env, replay):
    env.checks = Checks(required=tuple(f"check-{n}" for n in range(15)))
    sources = [await child(env, f"bb-{n}", {f"src/bb{n}.py": f"{n}\n"}) for n in range(2)]
    history = await deliver(env, produce=False)
    batch = env.batches.open_for(MAIN)
    assert history[-1][MAIN] == "checks_pending"
    # The recorded escalation: a high counter and a stored "no progress" fact.
    batch.repair_attempts, batch.no_progress = 15, True
    green_all(env, batch.candidate)

    outcome = await Train(env).visit()

    replay.check("15/15 green read before any counter",
                 outcome[MAIN] == "delivered" and env.checks.latest(batch.candidate) == ["success"] * 15)
    replay.check("promoted despite counter and no-progress fact",
                 await env.remote_oid(MAIN) == batch.candidate)
    replay.check("green consumes no attempt and raises no escalation",
                 batch.repair_attempts == 15 and env.commands.count("batch_escalate") == 0)
    replay.check("no repair allocated", env.counts()["repairs"] == 0)
    replay.check("every child contained in main", all([await env.contains(s) for s in sources]))
    await replay.done(env)



@replay_case(
    "swift-delta-90", "no-progress escalation on an epic batch",
    "15/15 green epic candidate contains every child",
    "promoted despite counter and no-progress fact",
    "green consumes no attempt and raises no escalation", "epic completes and reaches main",
    "no repair allocated",
)
async def test_swift_delta_90_green_epic_head_promotes_despite_no_progress(env, replay):
    env.checks = Checks(required=tuple(f"check-{n}" for n in range(15)))
    await env.epic("sd")
    epic_ref = "refs/heads/epic/sd"
    sources = [await child(env, f"sd-{n}", {f"src/sd{n}.py": f"{n}\n"}, parent="sd")
               for n in range(3)]
    await deliver(env, produce=False)
    batch = env.batches.open_for(epic_ref)
    batch.repair_attempts, batch.no_progress = 15, True
    green_all(env, batch.candidate)

    train = Train(env)
    first = await train.visit()
    replay.check("15/15 green epic candidate contains every child",
                 first[epic_ref] == "delivered" and all(
                     [await env.contains(s, epic_ref) for s in sources]))
    replay.check("promoted despite counter and no-progress fact",
                 await env.remote_oid(epic_ref) == batch.candidate)
    replay.check("green consumes no attempt and raises no escalation",
                 batch.repair_attempts == 15 and env.commands.count("batch_escalate") == 0)
    await deliver(env, train)  # the epic closes only through the train
    replay.check("epic completes and reaches main",
                 env.tasks["sd"].status == "COMPLETED" and all(
                     [await env.contains(s) for s in sources]))
    replay.check("no repair allocated", env.counts()["repairs"] == 0)
    await replay.done(env)


@replay_case(
    "crisp-horizon-90", "duplicate delegate-close audits broke a uniqueness check",
    "every child is an ancestor of the epic head", "no batch for contained children",
    "epic completes as an ordinary close", "epic reaches main",
)
async def test_crisp_horizon_90_children_already_in_epic_complete_it(env, replay):
    await env.epic("ch")
    epic_ref = "refs/heads/epic/ch"
    sources = [await child(env, f"ch-{n}", {f"src/ch{n}.py": f"{n}\n"}, parent="ch")
               for n in range(2)]
    # A legacy train merged both children and then recorded duplicate audits.
    head = await outside_merge(env, epic_ref, *sources)

    train = Train(env)
    await deliver(env, train)

    replay.check("every child is an ancestor of the epic head",
                 all([await env.contains(s, head) for s in sources]))
    replay.check("no batch for contained children",
                 all(b.target != epic_ref for b in env.batches.batches.values()))
    replay.check("epic completes as an ordinary close",
                 env.tasks["ch"].status == "COMPLETED"
                 and env.commands.count("task_close") == 3)
    replay.check("epic reaches main", await env.contains(head))
    await replay.done(env)


@replay_case(
    "vivid-quest-44", "cumulative repair list and an owner reserved with no expiry",
    "stale owner lease expires without stop proof",
    "one repair task for the failing candidate",
    "repair commits read from rev-list including merged commits",
    "batch delivers the repaired candidate",
)
async def test_vivid_quest_44_repair_progress_from_git_and_expiring_owner(env, replay):
    stale = env.lock.acquire(MAIN, "legacy-owner", ttl=600)
    await child(env, "vq", {"src/vq.py": "broken\n"})
    train = Train(env)
    locked = await train.visit()
    env.advance(601)
    resumed = await train.visit()
    replay.check("stale owner lease expires without stop proof",
                 locked[MAIN] == "locked" and resumed[MAIN] != "locked"
                 and not env.lock.live(stale) and env.lock.fences[MAIN] == 2)
    await deliver(env, train, produce=False)
    batch = env.batches.open_for(MAIN)
    env.checks.report(batch.candidate, "failure")
    assert (await train.visit())[MAIN] == "repair"
    assert (await train.visit())[MAIN] == "repair"  # recovered, not re-allocated
    (repair,) = [t for t in env.tasks.values() if t.repair_key]
    replay.check("one repair task for the failing candidate",
                 env.counts()["repairs"] == 1 and batch.repair_attempts == 1)

    # The repair writer adds a fix and merges a side branch on the candidate.
    await env.w("fetch", "--prune", "origin")
    await env.w("checkout", "-B", "repair", "origin/" + batch.ref.removeprefix("refs/heads/"))
    fix = await env.stage({"src/vq.py": "fixed\n"}, "fix vq")
    await env.w("checkout", "-b", "side")
    side = await env.stage({"src/vq_test.py": "ok\n"}, "cover vq")
    await env.w("checkout", "repair")
    await env.w("merge", "--no-ff", "-m", "merge side", "side")
    merged = await env.w("rev-parse", "HEAD")
    lease = env.lock.acquire(batch.ref, "repair-writer")
    await env.lock.publish(env.git, env.work, lease, merged, expected_old=batch.candidate)
    env.lock.release(lease)
    repair.status = "COMPLETED"
    env.commands("task_close", task_id=repair.id, outcome="pass")

    await deliver(env, train)

    replay.check("repair commits read from rev-list including merged commits",
                 train.repair_progress == [(batch.id, train.repair_progress[0][1])]
                 and set(train.repair_progress[0][1]) == {fix, side, merged})
    replay.check("batch delivers the repaired candidate",
                 await env.remote_oid(MAIN) == merged and batch.state == "delivered")
    await replay.done(env)


@replay_case(
    "epic-verifiers", "parent review coupled to a verification generation",
    "review requested by tree SHA", "approved tree completes epic with no prior generation",
    "epic reaches main",
)
async def test_epic_verifiers_review_verdict_keyed_by_tree(env, replay):
    await env.epic("ev", review_required=True)
    source = await child(env, "ev-1", {"src/ev.py": "1\n"}, parent="ev")
    train = Train(env)
    history = await deliver(env, train)
    head = await env.remote_oid("refs/heads/epic/ev")
    tree = await env.tree(head)
    replay.check("review requested by tree SHA",
                 history[-1]["refs/heads/epic/ev"] == "epic_waiting"
                 and env.reviews.requested == [tree])

    env.reviews.decide(tree, "approved")
    await deliver(env, train)
    replay.check("approved tree completes epic with no prior generation",
                 env.tasks["ev"].status == "COMPLETED" and len(env.reviews.verdicts) == 1)
    replay.check("epic reaches main", await env.contains(source) and await env.contains(head))
    await replay.done(env)


@replay_case(
    "stale-branch-owners", "workspace identity compared against reused slots",
    "a reused slot is a different holder", "train waits only until expiry",
    "the old incarnation's push is refused", "child delivered to the epic",
)
async def test_stale_branch_owner_lease_is_per_session_and_expires(env, replay):
    await env.epic("so")
    epic_ref = "refs/heads/epic/so"
    source = await child(env, "so-1", {"src/so.py": "1\n"}, parent="so")
    old = env.lock.acquire(epic_ref, "slot-3/session-a", ttl=120)
    train = Train(env)
    locked = await train.visit()
    replay.check("a reused slot is a different holder",
                 env.lock.acquire(epic_ref, "slot-3/session-b") is None)
    env.advance(121)
    resumed = await train.visit()
    replay.check("train waits only until expiry",
                 locked[epic_ref] == "locked" and resumed[epic_ref] != "locked"
                 and env.lock.fences[epic_ref] == 2)
    await deliver(env, train)
    head = await env.remote_oid(epic_ref)
    stray = await env.commit("stray", {"src/stray.py": "x\n"}, "late write",
                             start="origin/epic/so", push_to="refs/heads/stray")
    with pytest.raises(LeaseRefused):
        await env.lock.publish(env.git, env.work, old, stray, expected_old=head)
    replay.check("the old incarnation's push is refused",
                 await env.remote_oid(epic_ref) == head)
    replay.check("child delivered to the epic", await env.contains(source, epic_ref))
    await replay.done(env)


# -- Section 4 cutover scenarios ----------------------------------------------

@replay_case(
    "squash-delivery", "cutover",
    "squash commit proves delivery by whole patch", "no batch frozen", "main not pushed by train",
)
async def test_squash_merged_child_is_delivered_without_a_batch(env, replay):
    await child(env, "sq", {"src/sq.py": "one\n"}, close=False)
    source = await env.commit("task/sq", {"src/sq_two.py": "two\n"}, "second commit")
    await env.complete("sq", source)
    squash = await outside_push(env, MAIN, {"src/sq.py": "one\n", "src/sq_two.py": "two\n"},
                                "Squash merge sq (#12)")

    history = await deliver(env)

    replay.check("squash commit proves delivery by whole patch",
                 not await env.contains(source) and history[-1][MAIN] == "idle")
    replay.check("no batch frozen", env.counts()["batches"] == 0)
    replay.check("main not pushed by train",
                 await env.remote_oid(MAIN) == squash and not env.commands.count("target_publish"))
    await replay.done(env)


@replay_case(
    "multi-commit-partial-match", "cutover",
    "one commit's patch on main is not delivery", "batch delivers the whole source",
    "source is an ancestor of main",
)
async def test_partial_multi_commit_patch_match_stays_pending(env, replay):
    first = await child(env, "mc", {"src/mc.py": "one\n"}, close=False)
    source = await env.commit("task/mc", {"src/mc_two.py": "two\n"}, "second commit")
    await env.complete("mc", source)
    await outside_push(env, MAIN, {"src/mc.py": "one\n"}, "cherry-pick of the first commit only")

    train = Train(env)
    first_visit = await train.visit()
    replay.check("one commit's patch on main is not delivery",
                 first_visit[MAIN] == "checks_pending" and not await env.contains(first)
                 and env.counts()["batches"] == 1)
    await deliver(env, train)
    replay.check("batch delivers the whole source",
                 env.commands.count("target_publish") == 1
                 and env.batches.batches[next(iter(env.batches.batches))].state == "delivered")
    replay.check("source is an ancestor of main", await env.contains(source))
    await replay.done(env)


@replay_case(
    "dead-worker-lease", "cutover",
    "dead holder blocks the candidate ref only until expiry",
    "open repair task recovered, not re-allocated", "dead holder's fence is refused",
    "repaired candidate delivered",
)
async def test_dead_worker_lease_expires_and_repair_is_recovered(env, replay):
    await child(env, "dw", {"src/dw.py": "broken\n"})
    train = Train(env)
    await deliver(env, train, produce=False)
    batch = env.batches.open_for(MAIN)
    env.checks.report(batch.candidate, "failure")
    assert (await train.visit())[MAIN] == "repair"
    dead = env.lock.acquire(batch.ref, "worker-a/session-1", ttl=120)  # then the worker dies

    await outside_push(env, MAIN, {"src/other.py": "x\n"}, "unrelated change")
    blocked = await train.visit()
    env.advance(121)
    rebuilt = await train.visit()
    replay.check("dead holder blocks the candidate ref only until expiry",
                 blocked[MAIN] == "candidate_locked" and rebuilt[MAIN] == "checks_pending")
    env.checks.report(batch.candidate, "failure")
    assert (await train.visit())[MAIN] == "repair"
    (repair,) = [t for t in env.tasks.values() if t.repair_key]
    replay.check("open repair task recovered, not re-allocated",
                 env.counts()["repairs"] == 1 and batch.repair_attempts == 1)

    head = batch.candidate
    fix = await repair_push(env, batch, "worker-b/session-2", {"src/dw.py": "fixed\n"})
    with pytest.raises(LeaseRefused):
        await env.lock.publish(env.git, env.work, dead, head, expected_old=fix,
                               replace_ref=True)
    replay.check("dead holder's fence is refused", await env.remote_oid(batch.ref) == fix)
    repair.status = "COMPLETED"
    env.commands("task_close", task_id=repair.id, outcome="pass")
    await deliver(env, train)
    replay.check("repaired candidate delivered",
                 await env.remote_oid(MAIN) == fix and batch.state == "delivered")
    await replay.done(env)


@replay_case(
    "green-on-repair-entry", "cutover",
    "repair writer's commits read from Git", "green read before counter and no-progress fact",
    "promoted with the open repair's head", "no second repair",
)
async def test_green_on_repair_entry_promotes_despite_counter(env, replay):
    await child(env, "gr", {"src/gr.py": "broken\n"})
    train = Train(env)
    await deliver(env, train, produce=False)
    batch = env.batches.open_for(MAIN)
    start = batch.candidate
    env.checks.report(start, "failure")
    assert (await train.visit())[MAIN] == "repair"
    fix = await repair_push(env, batch, "repair-writer", {"src/gr.py": "fixed\n"})
    # The legacy escalation stored on entry: a high counter and "no progress".
    batch.repair_attempts, batch.no_progress = 15, True
    green_all(env, fix)

    outcome = await train.visit()
    replay.check("repair writer's commits read from Git",
                 train.repair_progress == [(batch.id, [fix])]
                 and repair_progress(start, fix, green=True))
    replay.check("green read before counter and no-progress fact",
                 outcome[MAIN] == "delivered" and batch.repair_attempts == 15
                 and env.commands.count("batch_escalate") == 0)
    replay.check("promoted with the open repair's head", await env.remote_oid(MAIN) == fix)
    replay.check("no second repair", env.counts()["repairs"] == 1)
    await replay.done(env)


@replay_case(
    "checks-rerun", "cutover",
    "a failed attempt rerun green is green", "a green attempt rerun red is red",
    "rerun green promotes without repair",
)
async def test_checks_rerun_latest_attempt_decides(env, replay):
    env.checks = Checks(required=("ci", "lint"))
    await child(env, "rr", {"src/rr.py": "1\n"})
    train = Train(env)
    await deliver(env, train, produce=False)
    sha = env.batches.open_for(MAIN).candidate
    env.checks.report(sha, "success", name="lint")
    env.checks.report(sha, "success", name="ci", attempt=1)
    env.checks.report(sha, "failure", name="ci", attempt=2)
    replay.check("a green attempt rerun red is red",
                 env.checks.failed(sha) and not env.checks.green(sha))
    env.checks.report(sha, "success", name="ci", attempt=3)
    replay.check("a failed attempt rerun green is green", env.checks.green(sha))
    outcome = await train.visit()
    replay.check("rerun green promotes without repair",
                 outcome[MAIN] == "delivered" and env.counts()["repairs"] == 0
                 and await env.remote_oid(MAIN) == sha)
    await replay.done(env)


@replay_case(
    "non-holder-and-stale-fence", "cutover",
    "a non-holder's managed push is refused", "a stale fence of the same holder is refused",
    "the current fence publishes with expected-old", "a moved remote refuses the current fence",
)
async def test_non_holder_and_stale_fence_cannot_publish(env, replay):
    head = await env.remote_oid(MAIN)
    change = await env.commit("side", {"src/nh.py": "1\n"}, start="origin/main",
                              push_to="refs/heads/side")
    held = env.lock.acquire(MAIN, "train-a", ttl=30)
    forged = Lease(MAIN, "train-b", held.fence, held.expires_at)
    with pytest.raises(LeaseRefused):
        await env.lock.publish(env.git, env.work, forged, change, expected_old=head)
    replay.check("a non-holder's managed push is refused", await env.remote_oid(MAIN) == head)

    env.advance(31)
    current = env.lock.acquire(MAIN, "train-a", ttl=30)
    with pytest.raises(LeaseRefused):
        await env.lock.publish(env.git, env.work, held, change, expected_old=head)
    replay.check("a stale fence of the same holder is refused",
                 current.fence == held.fence + 1 and await env.remote_oid(MAIN) == head)

    moved = await outside_push(env, MAIN, {"src/other.py": "x\n"}, "outside change")
    with pytest.raises(RemoteMoved):
        await env.lock.publish(env.git, env.work, current, change, expected_old=head)
    replay.check("a moved remote refuses the current fence", await env.remote_oid(MAIN) == moved)
    await env.w("fetch", "origin")
    await env.w("checkout", "-B", "side", change)
    await env.w("merge", "--no-ff", "-m", "merge main", "origin/main")
    merged = await env.w("rev-parse", "HEAD")
    await env.lock.publish(env.git, env.work, current, merged, expected_old=moved)
    replay.check("the current fence publishes with expected-old",
                 await env.remote_oid(MAIN) == merged)
    await replay.done(env)


@replay_case(
    "reopen-old-trailers", "cutover",
    "the first completion delivered with its trailer",
    "an old trailer does not prove the reopened source",
    "a new batch delivers the reopened source",
)
async def test_old_trailer_after_reopen_does_not_prove_new_source(env, replay):
    old = await child(env, "ro", {"src/ro.py": "1\n"})
    train = Train(env)
    await deliver(env, train)
    message = await git_out(env.git, env.root, "--git-dir", str(env.remote), "log", "-1",
                            "--format=%B", "--first-parent", MAIN)
    replay.check("the first completion delivered with its trailer",
                 source_identity("ro", old) in parse_source_trailers(message))

    task = env.tasks["ro"]
    task.status = "IN_PROGRESS"
    env.commands("task_reopen", task_id="ro")
    new = await env.commit("task/ro", {"src/ro.py": "2\n"}, "reopened work")
    await env.complete("ro", new, completion="close-2")
    pending = await train.visit()
    replay.check("an old trailer does not prove the reopened source",
                 pending[MAIN] == "checks_pending" and not await env.contains(new)
                 and env.counts()["batches"] == 2)
    await deliver(env, train)
    replay.check("a new batch delivers the reopened source",
                 await env.contains(new) and env.commands.count("target_publish") == 2)
    await replay.done(env)


@replay_case(
    "changed-tree-review", "cutover",
    "review requested by candidate tree", "an approval does not cover a changed tree",
    "the approved tree is delivered",
)
async def test_review_does_not_transfer_to_a_changed_tree(env, replay):
    env.review_targets.add(MAIN)
    source = await child(env, "ct", {"src/ct.py": "1\n"})
    train = Train(env)
    first = await deliver(env, train)
    batch = env.batches.open_for(MAIN)
    old_tree = await env.tree(batch.candidate)
    replay.check("review requested by candidate tree",
                 first[-1][MAIN] == "review_pending" and env.reviews.requested == [old_tree])
    env.reviews.decide(old_tree, "approved")
    await outside_push(env, MAIN, {"src/other.py": "x\n"}, "target moved after approval")

    second = await deliver(env, train)
    new_tree = await env.tree(batch.candidate)
    replay.check("an approval does not cover a changed tree",
                 second[-1][MAIN] == "review_pending" and new_tree != old_tree
                 and env.reviews.requested == [old_tree, new_tree])
    env.reviews.decide(new_tree, "approved")
    await deliver(env, train)
    replay.check("the approved tree is delivered",
                 await env.contains(source) and await env.tree(MAIN) == new_tree)
    await replay.done(env)


@replay_case(
    "moving-target", "cutover",
    "the expected-old push refuses a moved target", "the outside commit is kept",
    "the rebuilt candidate delivers",
)
async def test_moving_target_refuses_then_rebuilds(env, replay):
    source = await child(env, "mt", {"src/mt.py": "1\n"})
    faults = Faults()
    moved: list[str] = []

    async def move():
        moved.append(await outside_push(env, MAIN, {"src/other.py": "x\n"}, "moved"))
    faults.on("before_target_push", move)
    train = Train(env, faults=faults)
    history = await deliver(env, train)
    replay.check("the expected-old push refuses a moved target",
                 any(visit[MAIN] == "target_moved" for visit in history))
    replay.check("the outside commit is kept", await env.contains(moved[0]))
    replay.check("the rebuilt candidate delivers",
                 await env.contains(source) and env.commands.count("target_publish") == 1
                 and env.commands.count("candidate_publish") == 2)
    await replay.done(env)


@replay_case(
    "nested-epics", "cutover",
    "the child reaches the inner epic", "the inner epic completes into the outer epic",
    "the outer epic completes into main",
)
async def test_nested_epics_complete_bottom_up(env, replay):
    await env.epic("ne")
    await env.epic("ne-in", parent="ne")
    source = await child(env, "ne-c", {"src/ne.py": "1\n"}, parent="ne-in")
    await deliver(env, Train(env), visits=20)
    replay.check("the child reaches the inner epic",
                 await env.contains(source, "refs/heads/epic/ne-in"))
    replay.check("the inner epic completes into the outer epic",
                 env.tasks["ne-in"].status == "COMPLETED"
                 and await env.contains(source, "refs/heads/epic/ne"))
    replay.check("the outer epic completes into main",
                 env.tasks["ne"].status == "COMPLETED" and await env.contains(source))
    await replay.done(env)


@replay_case(
    "generated-conflict", "cutover",
    "a generated-only conflict is regenerated", "the trailer stays the last paragraph",
    "no repair allocated",
)
async def test_generated_file_conflict_is_regenerated(env, replay):
    one = await child(env, "gc-1", {"inputs/b": "b\n", "generated/index.txt": "inputs/a\ninputs/b\n"})
    two = await child(env, "gc-2", {"inputs/c": "c\n", "generated/index.txt": "inputs/a\ninputs/c\n"})
    await deliver(env)
    index = await git_out(env.git, env.root, "--git-dir", str(env.remote), "show",
                          f"{MAIN}:generated/index.txt")
    replay.check("a generated-only conflict is regenerated",
                 index.splitlines() == ["inputs/a", "inputs/b", "inputs/c"]
                 and await env.contains(one) and await env.contains(two))
    messages = await git_out(env.git, env.root, "--git-dir", str(env.remote), "log",
                             "--format=%B%x00", "--merges", MAIN)
    found = set().union(*(parse_source_trailers(m) for m in messages.split("\0")))
    replay.check("the trailer stays the last paragraph",
                 {source_identity("gc-1", one), source_identity("gc-2", two)} <= found)
    replay.check("no repair allocated", env.counts()["repairs"] == 0)
    await replay.done(env)


@replay_case(
    "migration-conflict", "cutover",
    "two migration heads go to an ordinary repair",
    "the repair writer's candidate is adopted from Git", "main has one migration head",
)
async def test_migration_head_conflict_is_an_ordinary_repair(env, replay):
    await outside_push(env, MAIN, {"migrations/a0.txt": "revision: a0\ndown: none\n"}, "a0")
    one = await child(env, "mg-1", {"migrations/a1.txt": "revision: a1\ndown: a0\n"})
    two = await child(env, "mg-2", {"migrations/a2.txt": "revision: a2\ndown: a0\n"})
    train = Train(env)
    first = await train.visit()
    again = await train.visit()
    replay.check("two migration heads go to an ordinary repair",
                 first[MAIN] == again[MAIN] == "repair" and env.counts()["repairs"] == 1
                 and await env.remote_oid(env.batches.open_for(MAIN).ref) is None)

    batch = env.batches.open_for(MAIN)
    await env.w("fetch", "origin")
    await env.w("checkout", "-B", "repair/mg", "origin/main")
    for task_id, source in batch.members:
        await env.w("merge", "--no-ff", "-m", with_source_trailers(
            f"Merge {task_id}", [source_identity(task_id, source)]), source)
    fix = await repair_push(env, batch, "repair-writer",
                            {"migrations/a2.txt": "revision: a2\ndown: a1\n"}, start="HEAD")
    repair = next(t for t in env.tasks.values() if t.repair_key)
    repair.status = "COMPLETED"
    env.commands("task_close", task_id=repair.id, outcome="pass")
    await deliver(env, train)
    replay.check("the repair writer's candidate is adopted from Git",
                 await env.remote_oid(MAIN) == fix and env.commands.count("candidate_publish") == 0)
    await env.w("fetch", "origin")
    await env.w("checkout", "--detach", "origin/main")
    replay.check("main has one migration head",
                 migration_heads(env.work) == {"a2"} and await env.contains(one) and await env.contains(two))
    await replay.done(env)


# -- Deterministic crashes and concurrency ------------------------------------

CRASH_POINTS = ("before_candidate_push", "after_candidate_push", "before_target_push",
                "after_target_push", "before_settlement")


async def trailer_counts(env: Env, since: str, ref: str = MAIN) -> dict:
    log = await git_out(env.git, env.root, "--git-dir", str(env.remote), "log",
                        "--first-parent", "--format=%B%x00", f"{since}..{ref}")
    counts: dict = {}
    for message in log.split("\0"):
        for identity in parse_source_trailers(message):
            counts[identity] = counts.get(identity, 0) + 1
    return counts


@replay_case(
    "crash-restart", "cutover",
    "the crash leaves the remote at its boundary",
    "a restarted train waits for the dead lease, then delivers", "each push happens once",
    "every child is in main exactly once",
)
@pytest.mark.parametrize("point", CRASH_POINTS)
async def test_crash_at_each_boundary_restarts_cleanly(env, replay, point):
    sources = {f"cr-{n}": await child(env, f"cr-{n}", {f"src/cr{n}.py": f"{n}\n"})
               for n in range(2)}
    faults = Faults()
    faults.crash(point)
    with pytest.raises(Crash):
        await deliver(env, Train(env, holder="train/pid-1", faults=faults))
    batch = env.batches.open_for(MAIN)
    candidate, main = await env.remote_oid(batch.ref), await env.remote_oid(MAIN)
    expected = {
        "before_candidate_push": (None, env.base), "after_candidate_push": ("ref", env.base),
        "before_target_push": ("ref", env.base), "after_target_push": ("ref", candidate),
        "before_settlement": ("ref", candidate),
    }[point]
    replay.check("the crash leaves the remote at its boundary",
                 (candidate if expected[0] is None else "ref" if candidate else None,
                  main) == expected and batch.state == "open")

    restarted = Train(env, holder="train/pid-2")
    blocked = await restarted.visit()
    env.advance(61)
    await deliver(env, restarted)
    replay.check("a restarted train waits for the dead lease, then delivers",
                 blocked[MAIN] == "locked" and batch.state == "delivered")
    replay.check("each push happens once",
                 env.commands.count("candidate_publish") == 1
                 and env.commands.count("target_publish") == 1
                 and env.counts() == {"tasks": 2, "repairs": 0, "batches": 1})
    counts = await trailer_counts(env, env.base)
    replay.check("every child is in main exactly once",
                 counts == {source_identity(t, s): 1 for t, s in sources.items()})
    await replay.done(env)


@replay_case(
    "crash-after-repair-create", "cutover",
    "the crash leaves one repair task", "the restart recovers it and its attempt",
    "no duplicate repair after restart",
)
async def test_crash_after_repair_create_recovers_the_task(env, replay):
    await child(env, "ca", {"src/ca.py": "broken\n"})
    await deliver(env, produce=False)
    batch = env.batches.open_for(MAIN)
    env.checks.report(batch.candidate, "failure")
    faults = Faults()
    faults.crash("after_repair_create")
    with pytest.raises(Crash):
        await Train(env, holder="train/pid-1", faults=faults).visit()
    replay.check("the crash leaves one repair task",
                 env.counts()["repairs"] == 1 and batch.repair_attempts == 0)
    env.advance(61)
    restarted = Train(env, holder="train/pid-2")
    outcome = await restarted.visit()
    replay.check("the restart recovers it and its attempt",
                 outcome[MAIN] == "repair" and batch.repair_attempts == 1)
    await restarted.visit()
    replay.check("no duplicate repair after restart",
                 env.counts()["repairs"] == 1 and env.commands.count("task_create") == 1)
    await replay.done(env)


@replay_case(
    "candidate-ref-loss", "cutover",
    "a lost candidate ref is rebuilt from exact sources",
    "the rebuilt candidate has the same tree", "delivered once",
)
async def test_lost_candidate_ref_is_rebuilt(env, replay):
    source = await child(env, "cl", {"src/cl.py": "1\n"})
    train = Train(env)
    await deliver(env, train, produce=False)
    batch = env.batches.open_for(MAIN)
    tree = await env.tree(batch.candidate)
    await delete_ref(env, batch.ref)
    outcome = await train.visit()
    replay.check("a lost candidate ref is rebuilt from exact sources",
                 outcome[MAIN] == "checks_pending" and env.commands.count("candidate_publish") == 2
                 and await env.remote_oid(batch.ref) == batch.candidate)
    replay.check("the rebuilt candidate has the same tree", await env.tree(batch.candidate) == tree)
    await deliver(env, train)
    replay.check("delivered once",
                 await env.contains(source) and env.commands.count("target_publish") == 1)
    await replay.done(env)


@replay_case(
    "aborted-after-ref-loss", "cutover",
    "an aborted batch is not recreated after ref loss", "no candidate is re-pushed",
    "main is unchanged",
)
async def test_aborted_batch_is_not_recreated_after_ref_loss(env, replay):
    await child(env, "ab", {"src/ab.py": "1\n"})
    train = Train(env)
    await deliver(env, train, produce=False)
    batch = env.batches.open_for(MAIN)
    batch.state = "aborted"
    env.commands("batch_abort", batch=batch.id)
    await delete_ref(env, batch.ref)
    history = await deliver(env, train)
    replay.check("an aborted batch is not recreated after ref loss",
                 {visit[MAIN] for visit in history} == {"aborted"}
                 and env.counts()["batches"] == 1)
    replay.check("no candidate is re-pushed",
                 env.commands.count("candidate_publish") == 1
                 and await env.remote_oid(batch.ref) is None)
    replay.check("main is unchanged", await env.remote_oid(MAIN) == env.base)
    await replay.done(env)


@replay_case(
    "aborted-members-not-refrozen", "cutover",
    "a new child freezes without the aborted member", "the new child is delivered",
    "the aborted member stays out of the target",
)
async def test_aborted_members_are_not_refrozen_with_a_new_child(env, replay):
    withheld = await child(env, "an", {"src/an.py": "1\n"})
    train = Train(env)
    await deliver(env, train, produce=False)
    aborted = env.batches.open_for(MAIN)
    aborted.state = "aborted"
    env.commands("batch_abort", batch=aborted.id)
    fresh = await child(env, "ao", {"src/ao.py": "1\n"})
    history = await deliver(env, train)
    batches = list(env.batches.batches.values())
    replay.check("a new child freezes without the aborted member",
                 len(batches) == 2 and batches[-1].members == (("ao", fresh),))
    replay.check("the new child is delivered",
                 batches[-1].state == "delivered" and await env.contains(fresh))
    replay.check("the aborted member stays out of the target",
                 not await env.contains(withheld) and aborted.state == "aborted"
                 and history[-1][MAIN] == "aborted")
    await replay.done(env)


@replay_case(
    "escalation-never-gates", "control",
    "a red head past the budget escalates once", "an ordinary repair is still allocated",
    "the open repair is recovered without a second escalation",
    "the repaired green head promotes and consumes no attempt",
)
async def test_counter_escalates_a_red_head_but_never_gates(env, replay):
    """Control for the no-progress families: the counter is live, read only on red."""
    await child(env, "eg", {"src/eg.py": "broken\n"})
    train = Train(env)
    await deliver(env, train, produce=False)
    batch = env.batches.open_for(MAIN)
    batch.repair_attempts, batch.no_progress = ESCALATE_AFTER, True
    env.checks.report(batch.candidate, "failure")

    outcome = await train.visit()
    replay.check("a red head past the budget escalates once",
                 env.commands.count("batch_escalate") == 1 and any(
                     args.get("priority") == "escalated"
                     for command_id, args in env.commands.log if command_id == "task_create"))
    replay.check("an ordinary repair is still allocated",
                 outcome[MAIN] == "repair" and env.counts()["repairs"] == 1
                 and batch.repair_attempts == ESCALATE_AFTER + 1)
    again = await train.visit()
    replay.check("the open repair is recovered without a second escalation",
                 again[MAIN] == "repair" and env.counts()["repairs"] == 1
                 and env.commands.count("batch_escalate") == 1)
    fix = await repair_push(env, batch, "repair-writer", {"src/eg.py": "fixed\n"})
    green_all(env, fix)
    done = await train.visit()
    replay.check("the repaired green head promotes and consumes no attempt",
                 done[MAIN] == "delivered" and await env.remote_oid(MAIN) == fix
                 and batch.repair_attempts == ESCALATE_AFTER + 1)
    await replay.done(env)


@replay_case(
    "simultaneous-visits", "cutover",
    "a visit inside another's critical section is locked out", "each push happens once",
    "a free race between two trains still pushes once", "every child delivered",
)
async def test_simultaneous_visits_publish_once(env, replay):
    first_sources = [await child(env, f"sv-{n}", {f"src/sv{n}.py": f"{n}\n"}) for n in range(2)]
    other = Train(env, holder="train-b", store=await env.new_store("store-b"))
    inner: list[str] = []

    async def overlap():
        inner.append((await other.visit())[MAIN])
    faults = Faults()
    faults.on("before_candidate_push", overlap)
    faults.on("before_target_push", overlap)
    # Deterministic interleaving: the second train visits from inside the
    # first one's critical sections.
    await deliver(env, Train(env, holder="train-a", faults=faults))
    replay.check("a visit inside another's critical section is locked out",
                 inner == ["locked", "locked"])
    replay.check("each push happens once",
                 env.commands.count("candidate_publish") == 1
                 and env.commands.count("target_publish") == 1)

    # Free race: whichever train fetches first wins the lease; the loser
    # either sees the lease or the settled state, never a second push.
    race_sources = [await child(env, f"sr-{n}", {f"src/sr{n}.py": f"{n}\n"}) for n in range(2)]
    racers = (Train(env, holder="race-a"), Train(env, holder="race-b",
                                                 store=await env.new_store("store-c")))
    for _ in range(8):
        await asyncio.gather(*(train.visit() for train in racers))
        producer(env)
        if env.commands.count("batch_settle") == 2:
            break
    replay.check("a free race between two trains still pushes once",
                 env.commands.count("candidate_publish") == 2
                 and env.commands.count("target_publish") == 2
                 and env.counts()["batches"] == 2)
    replay.check("every child delivered",
                 all([await env.contains(s) for s in first_sources + race_sources]))
    await replay.done(env)


@replay_case(
    "hold-and-rejection", "cutover",
    "an explicit hold outranks green", "releasing the hold delivers",
    "a required rejection outranks green", "no repair for a rejection",
)
async def test_hold_and_rejection_outrank_green(env, replay):
    await child(env, "ho", {"src/ho.py": "1\n"})
    train = Train(env)
    await deliver(env, train, produce=False)
    green_all(env, env.batches.open_for(MAIN).candidate)
    env.holds.add(MAIN)
    held = await train.visit()
    replay.check("an explicit hold outranks green",
                 held[MAIN] == "held" and await env.remote_oid(MAIN) == env.base)
    env.holds.discard(MAIN)
    replay.check("releasing the hold delivers", (await train.visit())[MAIN] == "delivered")

    head = await env.remote_oid(MAIN)
    env.review_targets.add(MAIN)
    await child(env, "rj", {"src/rj.py": "1\n"})
    await deliver(env, train, produce=False)
    candidate = env.batches.open_for(MAIN).candidate
    green_all(env, candidate)
    assert (await train.visit())[MAIN] == "review_pending"
    env.reviews.decide(await env.tree(candidate), "rejected")
    rejected = await deliver(env, train)
    replay.check("a required rejection outranks green",
                 rejected[-1][MAIN] == "rejected" and env.checks.green(candidate)
                 and await env.remote_oid(MAIN) == head)
    replay.check("no repair for a rejection", env.counts()["repairs"] == 0)
    await replay.done(env)


@replay_case(
    "unavailable-ci-other-target", "cutover",
    "an unavailable provider blocks only its own target",
    "the other target delivers in the same visit", "nothing unobserved is treated as green",
    "the blocked target delivers once checks return",
)
async def test_unavailable_ci_blocks_one_target_while_another_advances(env, replay):
    await env.epic("uc")
    epic_ref = "refs/heads/epic/uc"
    epic_source = await child(env, "uc-1", {"src/uc.py": "1\n"}, parent="uc")
    root_source = await child(env, "rt-1", {"src/rt.py": "1\n"})
    train = Train(env)
    await deliver(env, train, produce=False)
    epic_candidate = env.batches.open_for(epic_ref).candidate
    env.checks.unavailable.add(epic_candidate)
    green_all(env, env.batches.open_for(MAIN).candidate)
    epic_head = await env.remote_oid(epic_ref)

    outcome = await train.visit()
    replay.check("an unavailable provider blocks only its own target",
                 outcome[epic_ref] == "checks_unavailable")
    replay.check("the other target delivers in the same visit",
                 outcome[MAIN] == "delivered" and await env.contains(root_source))
    replay.check("nothing unobserved is treated as green",
                 env.checks.green(epic_candidate) is None
                 and await env.remote_oid(epic_ref) == epic_head)
    env.checks.unavailable.discard(epic_candidate)
    await deliver(env, train)
    replay.check("the blocked target delivers once checks return",
                 await env.contains(epic_source, epic_ref))
    await replay.done(env)


@replay_case(
    "slow-ci", "cutover",
    "a slow check run stays pending without a repair or failure",
    "another target advances while the slow run waits",
    "the lease is released between visits, never held across the wait",
    "the late success delivers once",
)
async def test_slow_ci_waits_without_repair(env, replay):
    await env.epic("sl")
    epic_ref = "refs/heads/epic/sl"
    source = await child(env, "sl-1", {"src/sl.py": "1\n"}, parent="sl")
    train = Train(env)
    await deliver(env, train, produce=False)
    batch = env.batches.open_for(epic_ref)
    epic_head = await env.remote_oid(epic_ref)
    waiting, roots = [], []
    for n in range(4):
        env.advance(3600)  # hours of silence are not a failure and not no-progress
        roots.append(await child(env, f"rt-{n}", {f"src/rt{n}.py": f"{n}\n"}))
        waiting.append((await train.visit())[epic_ref])
        producer(env, skip=lambda sha: sha == batch.candidate)  # the slow run never answers
        waiting.append((await train.visit())[epic_ref])
    replay.check("a slow check run stays pending without a repair or failure",
                 set(waiting) == {"checks_pending"} and env.counts()["repairs"] == 0
                 and await env.remote_oid(epic_ref) == epic_head)
    replay.check("another target advances while the slow run waits",
                 all([await env.contains(root) for root in roots]))
    other = env.lock.acquire(epic_ref, "another-train")
    replay.check("the lease is released between visits, never held across the wait",
                 other is not None)
    env.lock.release(other)
    green_all(env, batch.candidate)
    await deliver(env, train)

    def published(command: str, **match) -> int:
        return sum(1 for command_id, args in env.commands.log if command_id == command
                   and all(args.get(key) == value for key, value in match.items()))
    replay.check("the late success delivers once",
                 await env.contains(source, epic_ref)
                 and published("target_publish", target=epic_ref, candidate=batch.candidate) == 1
                 and published("candidate_publish", batch=batch.id) == 1)
    await replay.done(env)


# -- Historical captures -------------------------------------------------------
#
# The operator or supervisor captures an unheld live incident as a sanitized
# directory: ``remote.bundle`` (every ref the train saw) and ``capture.json``
# (task rows, exact-SHA check results, holds, review targets and the expected
# outcome). Workers only consume these. A family without a capture is a GAP: its
# reconstructed case above never stands in for a historical pass.

CAPTURES = Path(__file__).parent / "fixtures" / "integration_replay"
CAPTURE_FORMAT = 1


async def write_capture(env: Env, directory: Path, name: str, *,
                        sources: dict[str, str], expect: dict) -> None:
    """Record the observed source/ref/check inputs and the expected outcome."""
    directory.mkdir(parents=True, exist_ok=True)
    await git_out(env.git, env.root, "--git-dir", str(env.remote), "bundle", "create",
                  str(directory / "remote.bundle"), "--all")
    (directory / "capture.json").write_text(json.dumps({
        "format": CAPTURE_FORMAT, "case": name,
        "tasks": [asdict(task) for task in env.tasks.values()],
        "sources": sources, "required": list(env.checks.required),
        "checks": {sha: {check: {str(n): state for n, state in attempts.items()}
                         for check, attempts in runs.items()}
                   for sha, runs in env.checks.runs.items()},
        "unavailable": sorted(env.checks.unavailable), "holds": sorted(env.holds),
        "review_targets": sorted(env.review_targets),
        "reviews": [[tree, verdict] for (_repo, tree), verdict in env.reviews.verdicts.items()],
        "expect": expect,
    }, indent=2, sort_keys=True))


async def replay_capture(root: Path, directory: Path) -> tuple[Env, dict, list[dict]]:
    """Rebuild the recorded inputs exactly; no producer runs, nothing is synthesized."""
    data = json.loads((directory / "capture.json").read_text())
    assert data["format"] == CAPTURE_FORMAT, f"unknown capture format {data['format']}"
    root.mkdir(parents=True, exist_ok=True)
    env = Env(root)
    env.checks = Checks(data["required"])
    await env.setup(bundle=directory / "remote.bundle")
    env.tasks = {row["id"]: Task(**row) for row in data["tasks"]}
    env.checks.runs = {sha: {check: {int(n): state for n, state in attempts.items()}
                             for check, attempts in runs.items()}
                       for sha, runs in data["checks"].items()}
    env.checks.unavailable = set(data["unavailable"])
    env.holds, env.review_targets = set(data["holds"]), set(data["review_targets"])
    for tree, verdict in data["reviews"]:
        env.reviews.decide(tree, verdict)
    return env, data, await deliver(env, produce=False)


async def assert_capture_outcome(env: Env, data: dict, history: list[dict]) -> None:
    expect = data["expect"]
    assert expect.get("delivered") or expect.get("outcomes"), "capture expects nothing"
    for task_id, ref in expect.get("delivered", []):
        assert await env.contains(data["sources"][task_id], ref), f"{task_id} not in {ref}"
    for ref, outcome in expect.get("outcomes", {}).items():
        assert history[-1][ref] == outcome, f"{ref}: {history[-1][ref]} != {outcome}"
    assert env.counts()["repairs"] <= expect.get("max_repairs", 0)
    env.commands.assert_no_removed()


def capture_status(name: str) -> str:
    return "captured" if (CAPTURES / name / "capture.json").is_file() else "gap"


@pytest.mark.parametrize("name", HISTORICAL_STALLS)
async def test_historical_capture_replays(tmp_path, name):
    if capture_status(name) == "gap":
        pytest.skip(f"GAP: no sanitized capture of {name} was provided; its reconstructed "
                    "case is not a historical pass")
    env, data, history = await replay_capture(tmp_path, CAPTURES / name)
    assert data["case"] == name
    await assert_capture_outcome(env, data, history)


async def test_capture_round_trip_preserves_recorded_inputs(env, tmp_path):
    """The loader replays recorded inputs exactly: same candidate SHA, same checks."""
    source = await child(env, "rt", {"src/rt.py": "1\n"})
    await deliver(env, produce=False)
    candidate = env.batches.open_for(MAIN).candidate
    green_all(env, candidate)  # the check result observed for that exact SHA
    await write_capture(env, tmp_path / "capture", "round-trip", sources={"rt": source},
                        expect={"delivered": [["rt", MAIN]], "outcomes": {MAIN: "idle"}})
    replayed, data, history = await replay_capture(tmp_path / "replay", tmp_path / "capture")
    await assert_capture_outcome(replayed, data, history)
    # The recorded candidate was adopted, not rebuilt: its checks still apply.
    assert replayed.commands.count("candidate_publish") == 0
    assert replayed.commands.log[0] == ("batch_freeze", replayed.commands.log[0][1])
    assert await replayed.remote_oid(MAIN) == candidate

    # A capture with no check result for the candidate stays pending: nothing
    # unobserved is turned into a pass.
    (tmp_path / "capture" / "capture.json").write_text(
        (tmp_path / "capture" / "capture.json").read_text().replace(candidate, "0" * 40))
    stalled, data, history = await replay_capture(tmp_path / "stalled", tmp_path / "capture")
    assert history[-1][MAIN] == "checks_pending"
    assert not await stalled.contains(source)


# -- The replay report ---------------------------------------------------------

REQUIRED_SCENARIOS = HISTORICAL_STALLS + (
    "squash-delivery", "dead-worker-lease", "green-on-repair-entry",
    "non-holder-and-stale-fence", "multi-commit-partial-match", "reopen-old-trailers",
    "checks-rerun", "changed-tree-review", "moving-target", "nested-epics",
    "generated-conflict", "migration-conflict", "crash-restart", "crash-after-repair-create",
    "candidate-ref-loss", "aborted-after-ref-loss", "simultaneous-visits",
    "hold-and-rejection", "unavailable-ci-other-target", "slow-ci",
    "aborted-members-not-refrozen", "escalation-never-gates",
)


def replay_report() -> dict:
    """Case names and assertions; historical status comes only from a capture."""
    return {
        "cases": [{"name": name, "family": case["family"], "test": case["test"],
                   "assertions": list(case["assertions"])} for name, case in CASES.items()],
        "historical": {name: {"reconstructed": CASES[name]["test"] if name in CASES else None,
                              "capture": capture_status(name)}
                       for name in HISTORICAL_STALLS},
    }


def test_replay_report_lists_cases_assertions_and_gaps():
    report = replay_report()
    names = [case["name"] for case in report["cases"]]
    assert sorted(set(REQUIRED_SCENARIOS) - set(names)) == []
    assert all(case["assertions"] for case in report["cases"])
    for name, row in report["historical"].items():
        assert row["reconstructed"], name
        # A gap is reported as a gap: test_historical_capture_replays skips it.
        assert row["capture"] in {"gap", "captured"}, name
    if out := os.environ.get("AQ_REPLAY_EVIDENCE"):
        Path(out).mkdir(parents=True, exist_ok=True)
        (Path(out) / "report.json").write_text(json.dumps(report, indent=2))



async def test_removed_control_guard_fires(removed_controls_refuse):
    """The guard is live: a removed control fails through both dispatch paths."""
    handler = object.__new__(CommandHandler)
    with pytest.raises(AssertionError, match="removed recovery control"):
        await handler._cmd_integration_record_noop({})
    assert removed_controls_refuse == ["integration_record_noop"]
    removed_controls_refuse.clear()
    with pytest.raises(AssertionError, match="removed recovery control"):
        Commands()("integration_adopt")
