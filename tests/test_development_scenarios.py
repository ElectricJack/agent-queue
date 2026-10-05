"""Rev-2 section-6 phase-3 Development acceptance over real Git and PostgreSQL.

The Development adapter runs on the daemon's ``IntegrationService`` remote pass
beside the legacy development publisher, with both loops of the shared
reconciler, over a bare fixture origin and a disposable PostgreSQL database.
Only the forge and the detached runner boundary are substituted: the retained
clone, the shared Git primitives, the existing repair filing, the gate command
and the real job rows and snapshots are the shipped ones.

The tests play the outside world. Legacy rules stay disabled; the only operator
action is the audited per-project engine transfer. Sources are ordinary
completed tasks with real branches, validation is a real finite preset run on a
real detached snapshot, and no recovery, redrive, rebind or settle command ever
runs.

1. **Parked member.** Four completed sources where two collide on one file and a
   third depends on the colliding one. The reconciler seals them in dependency
   order, parks exactly the conflicting revision in the shared journal, files the
   existing repair for it, keeps building the independent member, publishes that
   member on its own, and leaves the parked source and its dependent undelivered
   with their branches.
2. **Validation failure.** Two completed sources whose merged aggregate fails the
   pinned focused check. The failure is preserved: the red head never reaches the
   default branch, the existing repair is filed with the aggregate scope, its
   worker publishes a fix that keeps every source an ancestor, and only the
   rebuilt green generation is published.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import insert, select, update

from src.commands.handler import CommandHandler
from src.config import AppConfig, DatabaseConfig, DiscordConfig
from src.database import Database
from src.database import tables as t
from src.git.github_contracts import GitHubRepositoryBinding
from src.git.manager import GitError, GitManager
from src.integration.development import (
    DevelopmentBusy,
    DevelopmentIntegration,
    DevelopmentPolicy,
    publisher_exclusion,
)
from src.integration.development_adapter import (
    DevelopmentIntegrationAdapter,
    ordered_development_members,
)
from src.integration.development_policy import PinnedDevelopmentPolicy, render_development_policy
from src.integration.development_runtime import (
    DevelopmentFrontierReader,
    DevelopmentPrimitiveAdapters,
    DevelopmentSubjectRuntime,
    DevelopmentTrustedGreen,
    development_repository,
    development_runtime_for,
    retain_candidate,
    transfer_development_engine,
)
from src.integration.engine import EngineRefused, RootEngineOwnership
from src.integration.gitops import GitOperations, SubjectGitAuthority
from src.integration.models import BranchKey
from src.integration.observe import DatabaseObservationReader, IntegrationObserver
from src.integration.ownership import BranchOwnership
from src.integration.service import IntegrationService
from src.integration.subjects import (
    AncestryArgs,
    AncestryQuery,
    MemberRef,
    MergeMembersArgs,
    PrimitivePorts,
    RemoteHead,
    Subject,
    SubjectPhase,
)
from src.jobs.result import build_result
from src.models import Project, RepoConfig, RepoSourceType, TaskStatus
from src.orchestrator import Orchestrator
from src.playbooks.artifact_store import ArtifactStore
from src.playbooks.definition import ProjectScope, load_definition_json, source_digest
from src.playbooks.integration_policy import IntegrationPolicyFacts, policy_from_markdown
from tests.db_fixtures import lease_dsn

PROJECT, REPO = "agent-queue", "aq-dev-repo"
BINDING = GitHubRepositoryBinding(9, "example/dev")
ZERO = "0" * 40

#: A real finite preset. The merged aggregate is judged by ruff over one path,
#: so a member's unused import is a genuine conclusive red.
VALIDATION = ["ruff check checks/"]

#: The only commands a reconciler visit may run here: the existing gate
#: command. Repair filing, sealing, Git and validation are the shared ports.
ADAPTER_COMMANDS = frozenset({"gate_create"})


def git(cwd, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def git_status(cwd, *args: str) -> subprocess.CompletedProcess:
    """A Git probe whose exit status is the answer (no exception on non-zero)."""
    return subprocess.run(["git", *args], cwd=cwd, check=False, capture_output=True, text=True)


def _remote(origin, ref: str) -> str | None:
    return git_status(origin, "rev-parse", "--verify", "-q", ref).stdout.strip() or None


class Clock:
    def __init__(self, now: float):
        self.now = now

    def __call__(self) -> float:
        return self.now


class LocalGit(GitManager):
    """Only the credential boundary is substituted; transport is real Git."""

    def __init__(self, origin: Path):
        super().__init__()
        self.origin = origin
        self.pushes: list[dict] = []
        self.deletes: list[str] = []

    async def aremote_branch_head(self, *, repository, branch):
        assert repository == BINDING
        return _remote(self.origin, f"refs/heads/{branch}")

    async def apush_repository_oid(
        self, store, *, repository, tip_oid, branch, expected_old_oid, authority_deadline=None
    ):
        assert repository == BINDING
        result = await self.arun_git_result(
            [
                "push",
                f"--force-with-lease=refs/heads/{branch}:{expected_old_oid}",
                str(self.origin),
                f"{tip_oid}:refs/heads/{branch}",
            ],
            cwd=store,
        )
        if result.returncode:
            raise GitError(result.stderr)
        self.pushes.append({"branch": branch, "tip_oid": tip_oid, "expected": expected_old_oid})
        return tip_oid

    async def adelete_repository_ref(
        self, store, *, repository, branch, expected_old_oid, authority_deadline=None
    ):
        assert repository == BINDING
        result = await self.arun_git_result(
            [
                "push",
                f"--force-with-lease=refs/heads/{branch}:{expected_old_oid}",
                str(self.origin),
                f":refs/heads/{branch}",
            ],
            cwd=store,
        )
        if result.returncode:
            raise GitError(result.stderr)
        self.deletes.append(branch)


class LocalJobs:
    """Submit through the real job command; run the real preset synchronously.

    Submission is the existing ``job_submit_integration`` command: a detached
    snapshot clone of the retained store at the exact head, a real ``jobs`` row
    and the real idempotency key. Completion executes the job's own preset argv
    in that snapshot and records the real ``build_result`` receipt, which is
    what the producer classifies. Only the detached spawn is synchronous here.
    """

    def __init__(self, handler):
        self.handler, self.completed = handler, []

    async def submit(self, **args):
        response = await self.handler._cmd_job_submit_integration(args)
        assert response["success"], response
        return response["job"]

    async def run(self, job_id: str) -> dict:
        """Execute one submitted job's own argv in its detached snapshot."""
        job = await self.handler.db.get_job(job_id)
        assert job["state"] == "queued", job
        workspace = await self.handler.db.get_workspace(job["workspace_id"])
        completed = await asyncio.to_thread(
            subprocess.run, job["argv"], cwd=workspace.workspace_path,
            capture_output=True, text=True, env=job["contract"]["env"],
        )
        result = build_result(
            job,
            {
                "exit_code": completed.returncode,
                "input_ref": job["input_ref"],
                "input_stability": "stable",
                "stdout": completed.stdout[-4000:],
                "stderr": completed.stderr[-4000:],
            },
        )
        started = await self.handler.db.transition_job(
            job["id"], job["state_version"], "starting", launch_at=job["submitted_at"]
        )
        assert started is not None
        running = await self.handler.db.transition_job(
            job["id"], started["state_version"], "running"
        )
        assert running is not None
        final = await self.handler.db.transition_job(
            job["id"], running["state_version"], result["state"], result=result
        )
        assert final is not None, (job_id, result)
        self.completed.append(final["id"])
        return final


class OriginReads:
    """The observer's and frontier's Git port: exact reads of the bare origin."""

    def __init__(self, origin: Path):
        self.origin = origin

    async def remote_head(self, repository, ref):
        sha = _remote(self.origin, ref)
        if sha is None:
            return RemoteHead(ref=ref, state="absent")
        return RemoteHead(ref=ref, state="present", sha=sha)

    async def is_ancestor(self, repository, ancestor, descendant):
        result = git_status(self.origin, "merge-base", "--is-ancestor", ancestor, descendant)
        return {0: True, 1: False}.get(result.returncode)


class LegacyRulesDisabled:
    """The old development publisher stays off while the reconciler acts."""

    async def tick(self, now):
        return 0

    async def dispatch_due(self, now):
        return 0


class Development:
    """Fixture world plus the service-owned reconciler, wired as the daemon wires it."""

    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.origin, self.work = tmp_path / "origin.git", tmp_path / "work"
        self.clock = Clock(1000.0)
        self.heads: dict[str, str] = {}
        self.commands: list[tuple[str, dict]] = []
        self.service = None
        self.jobs_run: list[str] = []

    # ------------------------------------------------------------------ git

    def init_git(self) -> None:
        git(self.tmp_path, "init", "--bare", "--initial-branch=main", str(self.origin))
        # Every update of the default branch is kept so a test can audit it.
        git(self.origin, "config", "core.logAllRefUpdates", "always")
        git(self.tmp_path, "clone", str(self.origin), str(self.work))
        git(self.work, "config", "user.name", "Development Scenario")
        git(self.work, "config", "user.email", "development@example.test")
        (self.work / "checks").mkdir()
        (self.work / "checks" / "__init__.py").write_text("")
        (self.work / "base.txt").write_text("base\n")
        git(self.work, "add", "-A")
        git(self.work, "commit", "-m", "base")
        self.base = git(self.work, "rev-parse", "HEAD")
        git(self.work, "push", "origin", "main")

    def source(self, name: str, files: dict[str, str], *, base: str | None = None) -> str:
        git(self.work, "switch", "-C", f"aq/{name}", base or self.base)
        for path, text in files.items():
            target = self.work / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        git(self.work, "add", "-A")
        git(self.work, "commit", "-m", f"{name} source")
        self.heads[name] = git(self.work, "rev-parse", "HEAD")
        git(self.work, "push", "-f", "origin", f"HEAD:refs/heads/aq/{name}")
        return self.heads[name]

    def remote(self, ref: str) -> str | None:
        return _remote(self.origin, ref)

    def contains(self, ancestor: str, descendant: str | None) -> bool:
        """Ancestry in the retained store, which holds local and remote commits.

        A built candidate is local until publication, so the origin cannot
        answer for it; the retained clone holds both sides.
        """
        if descendant is None:
            return False
        return (
            git_status(
                self.retained, "merge-base", "--is-ancestor", ancestor, descendant
            ).returncode
            == 0
        )

    def main_history(self) -> list[str]:
        """Every value the default branch ever held, oldest first."""
        log = git(self.origin, "reflog", "show", "--format=%H", "refs/heads/main")
        return list(reversed(log.split()))

    # --------------------------------------------------------------- daemon

    async def open(
        self, *, validation: str = "focused", commands=(), regenerate: str | None = None
    ) -> None:
        self.init_git()
        self.db = db = Database(lease_dsn("development-scenarios.db"))
        await db.initialize()
        source = render_development_policy(
            PROJECT,
            DevelopmentPolicy(
                validation=validation,
                commands=list(commands) or VALIDATION,
                max_batch_size=10,
                interval_seconds=300,
                slot_wait_seconds=0,
                timeout_seconds=60,
                regenerate=regenerate,
            ),
        )
        definition = load_definition_json(
            Path("tests/fixtures/playbooks/v2/root-train/artifact.json").read_text()
        ).model_copy(update={
            "id": f"{PROJECT}-development",
            "scope": ProjectScope(project_id=PROJECT),
            "source_hash": source_digest(source),
            "integration_policy": policy_from_markdown(source),
        })
        self.pinned = PinnedDevelopmentPolicy(definition, source)
        self.artifact_sha = definition.artifact_sha256()
        policy = {
            "development": {
                "route": {
                    "artifact": {
                        "playbook_id": definition.id,
                        "artifact_sha256": self.artifact_sha,
                    }
                }
            }
        }
        await db.create_project(Project(id=PROJECT, name="Development acceptance"))
        await db.create_repo(
            RepoConfig(
                id=REPO,
                project_id=PROJECT,
                source_type=RepoSourceType.CLONE,
                url=str(self.origin),
                default_branch="main",
            )
        )
        await db.update_project(
            PROJECT,
            hierarchical_integration_mode="development",
            integration_repository_id=REPO,
            hierarchical_integration_policy=policy,
        )
        async with db.immediate() as conn:
            await conn.execute(
                insert(t.playbook_artifacts).values(
                    artifact_sha256=self.artifact_sha,
                    playbook_id=definition.id,
                    source_digest=definition.source_hash,
                    contract_fingerprint="sha256:" + "9" * 64,
                    compiler_build="test",
                    path="/test/development.json",
                    created_at=100.0,
                )
            )
        self.git = LocalGit(self.origin)
        self.data_dir = self.tmp_path / "data"
        self.legacy = DevelopmentIntegration(
            db, data_dir=self.data_dir, git=self.git, job_client=None
        )
        self.retained = self.legacy._store_path(await db.get_repo(REPO))
        config = AppConfig(
            discord=DiscordConfig(bot_token="t", guild_id="1"),
            workspace_dir=str(self.tmp_path / "workspaces"),
            database=DatabaseConfig(url=lease_dsn("development-scenarios.db")),
            data_dir=str(self.data_dir),
        )
        config.resources.jobs.enabled = True
        config.resources.test_slots = 2
        store = ArtifactStore(config.compiled_root)
        store.put(
            definition,
            source_digest=definition.source_hash,
            contract_fingerprint=definition.contract_fingerprint(),
            profile_fingerprint="test",
            compiler_build="test",
        )
        store.put_source(self.artifact_sha, source)
        orchestrator = self.orchestrator = Orchestrator(config)
        orchestrator.db = db
        orchestrator.git = self.git
        orchestrator.development_integration = self.legacy
        self.handler = CommandHandler(orchestrator, config)
        self.jobs = LocalJobs(self.handler)

        async def observe(subject):
            return await self.observer.observe(subject)

        async def policy_for(artifact_sha256):
            assert artifact_sha256 == self.artifact_sha
            return self.pinned

        async def retained_repository(subject):
            repo = await db.get_repo(subject.repository_id)
            return await development_repository(
                self.legacy, repo, BINDING, self.pinned.settings
            )

        async def retained_for(subject):
            repository = await retained_repository(subject)
            await retain_candidate(self.git, repository.store, subject)
            return repository

        self.reads = OriginReads(self.origin)
        self.observer = IntegrationObserver(
            DatabaseObservationReader(db),
            self.reads,
            facts_type=IntegrationPolicyFacts,
            clock=self.clock,
        )
        self.adapter = DevelopmentIntegrationAdapter(
            db,
            observe=observe,
            frontier_for=DevelopmentFrontierReader(db, git=self.reads, clock=self.clock),
            policy_for=policy_for,
            repository_for=retained_for,
            shared_ports=PrimitivePorts(),
            job_client=self.jobs,
            clock=self.clock,
        )
        git_operations = GitOperations(
            db,
            git=self.git,
            repository=retained_repository,
            authority=SubjectGitAuthority(
                db,
                trusted_green=DevelopmentTrustedGreen(self.adapter.producer_for),
                clock=self.clock,
            ),
        )
        world = self

        class RecordingCommands:
            async def execute(self, name, args):
                result = await world.handler.execute(name, dict(args))
                world.commands.append((name, dict(args)))
                return result

        DevelopmentPrimitiveAdapters(
            db,
            development=self.legacy,
            git_operations=git_operations,
            commands=RecordingCommands,
            backup_dir=self.legacy.backup_dir,
            frontier_for=self.adapter.frontier_for,
            clock=self.clock,
        ).bind(self.adapter.shared)
        self.runtime = DevelopmentSubjectRuntime(
            db, self.adapter, policy=policy_for, active=True, shadow=True, clock=self.clock
        )
        class IdleOutbox:
            async def dispatch_due(self, now):
                return 0

        self.service = IntegrationService(
            db,
            None,
            None,
            IdleOutbox(),
            development_handler=LegacyRulesDisabled().tick,
            development_subject_runtime=self.runtime,
            clock=self.clock,
        )

    async def close(self) -> None:
        if self.service is not None:
            await self.service.stop()
        if getattr(self, "db", None) is not None:
            await self.db.close()

    # -------------------------------------------------------------- factory

    def factory_runtime(self) -> DevelopmentSubjectRuntime:
        """The daemon's own construction over this world, nothing overridden.

        ``development_runtime_for`` builds every port from the orchestrator's
        existing owners, so this is the wiring an operator's reconciler flag
        gets: the same database, Git manager, retained clone and reviewed
        artifact, with nothing injected in place of the shipped resolver.
        """
        orchestrator = self.orchestrator
        orchestrator.config.integration.reconciler_active = True
        orchestrator.integration_app_client = SimpleNamespace(repository=BINDING)

        # The daemon's artifact loader is synchronous; the factory reads it in
        # a worker thread.
        def load_playbook_artifact(artifact_sha256):
            assert artifact_sha256 == self.artifact_sha
            return self.pinned.definition.model_copy(update={"source": self.pinned.source})

        orchestrator._load_playbook_artifact = load_playbook_artifact
        runtime = development_runtime_for(orchestrator)
        assert runtime is not None, "a reconciler flag on constructs the runtime"
        return runtime

    # -------------------------------------------------------------- sources

    async def add_source(self, task_id: str, *, depends_on: tuple[str, ...] = ()):
        """A completed, pushed source with its recorded base and release edges."""
        branch, head = f"aq/{task_id}", self.heads[task_id]
        async with self.db.immediate() as conn:
            await conn.execute(
                insert(t.tasks).values(
                    id=task_id,
                    project_id=PROJECT,
                    repo_id=REPO,
                    title=task_id,
                    description="",
                    status="COMPLETED",
                    task_type="feature",
                    branch_name=branch,
                    created_at=1.0,
                    updated_at=float(2 + len(task_id)),
                )
            )
            await conn.execute(
                insert(t.task_branch_origins).values(
                    id=f"origin-{task_id}",
                    task_id=task_id,
                    repository_id=REPO,
                    base_sha=self.base,
                    creation_generation=0,
                    reserved=True,
                    created_at=1.0,
                )
            )
            await conn.execute(
                insert(t.task_integration_checkpoints).values(
                    task_id=task_id,
                    repository_id=REPO,
                    branch=branch,
                    checkpoint_sha=head,
                    generation=0,
                    updated_at=1.0,
                )
            )
            for index, required in enumerate(depends_on):
                await conn.execute(
                    insert(t.task_dependencies).values(
                        task_id=task_id,
                        depends_on_task_id=required,
                        dep_type="blocks",
                        description=f"released after {required}",
                    )
                )
        return head

    async def cutover(self) -> Subject:
        """Seed in shadow, then the operator's audited per-project transfer."""
        await self.tick(1)
        [seeded] = await self.subjects()
        assert seeded.engine.value == "legacy", seeded.engine
        shadow = await self.journal(seeded.id)
        assert shadow and {row["mode"] for row in shadow} == {"shadow"}
        assert {row["entry_kind"] for row in shadow} == {"decision"}
        assert self.commands == [], "a shadow visit ran a command"
        versions = {seeded.id: seeded.version}
        moved = await transfer_development_engine(
            self.db,
            PROJECT,
            engine="reconciler",
            expected_versions=versions,
            reason="approved development cutover",
            evidence=("shadow-week", "development-scenarios", "operator-approval"),
            operator_id="human:local-operator",
            clock=self.clock,
        )
        assert moved["outcome"] == "transferred"
        return await self.subject(seeded.id)

    async def collect(self) -> Subject:
        """Take the publication fence on the target and file no writer.

        The collector lease is the shared durable branch owner the publication
        fence reads; it is reserved here the way the existing delivery path
        reserves its publisher, never invented per visit.
        """
        subject = await self.root()
        await BranchOwnership(self.db, clock=self.clock).acquire(
            BranchKey(repository_id=REPO, branch=subject.target_ref), subject.id, "collector"
        )
        return await self.subject(subject.id)

    # ----------------------------------------------------------- reconciler

    async def subjects(self) -> list[Subject]:
        async with self.db._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(t.integration_subjects).order_by(t.integration_subjects.c.created_at)
                    )
                )
                .mappings()
                .all()
            )
        return [Subject.from_row(row) for row in rows]

    async def root(self) -> Subject:
        return (await self.subjects())[0]

    async def subject(self, subject_id: str) -> Subject:
        return Subject.from_row(await self.db.get_integration_subject(subject_id))

    async def sources(self) -> list[Subject]:
        return [s for s in await self.subjects() if s.kind.value == "source"]

    async def journal(self, subject_id: str) -> list[dict]:
        return await self.db.list_integration_subject_journal(subject_id)

    async def tick(self, seconds: float = 61.0) -> None:
        self.clock.now += seconds
        await self.runtime.tick(self.clock.now)
        await self.service.tick(self.clock.now)

    async def run_validation(self) -> None:
        """Run every queued validation job the reconciler has requested."""
        jobs = self.jobs.completed
        while True:
            async with self.db._engine.connect() as conn:
                queued = (
                    (
                        await conn.execute(
                            select(t.jobs.c.id).where(
                                t.jobs.c.project_id == PROJECT,
                                t.jobs.c.owner_kind == "integration",
                                t.jobs.c.state == "queued",
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            pending = [job for job in queued if job not in jobs]
            if not pending:
                return
            for job in pending:
                await self.jobs.run(job)
                self.jobs_run.append(job)

    async def run_until(self, predicate, *, label: str, limit: int = 40) -> None:
        for _ in range(limit):
            if await predicate():
                return
            await self.tick()
            await self.run_validation()
        raise AssertionError(f"reconciler did not reach: {label}\n{await self.describe()}")

    async def phase(self, subject_id: str, phase: SubjectPhase) -> bool:
        return (await self.subject(subject_id)).phase is phase

    async def describe(self) -> str:
        lines = []
        for subject in await self.subjects():
            facts = await self.observer.observe(subject)
            lines.append(
                f"{subject.id} {subject.kind.value} {subject.engine.value} "
                f"{subject.phase.value} due={subject.schedule.next_due_at} "
                f"unknown={facts.unknown} holds={facts.holds}"
            )
            for row in (await self.journal(subject.id))[-12:]:
                lines.append(
                    f"  {row['mode']} {row['entry_kind']} {row['primitive']} "
                    f"{row.get('rule')} {row.get('outcome')} {row['idempotency_key']} "
                    f"{((row['payload'] or {}).get('result') or {}).get('reason') or ''}"
                )
        return "\n".join(lines)

    # --------------------------------------------------------------- workers

    async def repair_worker(self, repair_task_id: str, files: dict[str, str], *, fix: dict[str, str]):
        """An ordinary pool worker: claim, merge the frozen sources, push, close.

        Nothing here knows about subjects. It is the existing task lifecycle:
        an ordinary merge of the exact source revisions named in the repair
        brief, a push of its own branch, and a close.
        """
        repair = await self.db.get_task(repair_task_id)
        assert repair.status.value in {"READY", "ASSIGNED"}, repair.status
        path = self.tmp_path / f"worker-{repair_task_id}"
        git(self.tmp_path, "clone", str(self.origin), str(path))
        # Claiming records the branch's base and a checkpoint, the way the
        # existing claim and checkpoint writers do for any task.
        async with self.db.immediate() as conn:
            await conn.execute(
                insert(t.task_branch_origins).values(
                    id=f"origin-{repair_task_id}",
                    task_id=repair_task_id,
                    repository_id=REPO,
                    base_sha=self.remote("refs/heads/main"),
                    creation_generation=0,
                    reserved=True,
                    created_at=self.clock.now,
                )
            )
            await conn.execute(
                insert(t.task_integration_checkpoints).values(
                    task_id=repair_task_id,
                    repository_id=REPO,
                    branch=repair.branch_name,
                    checkpoint_sha=None,
                    generation=0,
                    updated_at=self.clock.now,
                )
            )
        git(path, "config", "user.name", "Repair Worker")
        git(path, "config", "user.email", "worker@example.test")
        manifest = (await self.db.get_task_meta(repair_task_id, "development_repair_sources")) or []
        branch_name = repair.branch_name
        git(
            path,
            "switch",
            "-C",
            branch_name,
            f"origin/{branch_name}"
            if self.remote(f"refs/heads/{branch_name}")
            else "origin/main",
        )
        for member in manifest:
            merged = git_status(path, "merge", "--no-ff", "--no-edit", member["source_sha"])
            assert merged.returncode == 0, merged.stdout
        for path_, text in {**files, **fix}.items():
            (path / path_).write_text(text)
        git(path, "add", "-A")
        git(path, "commit", "-m", f"resolve development repair {repair_task_id}")
        head = git(path, "rev-parse", "HEAD")
        git(path, "push", "-f", "origin", f"HEAD:refs/heads/{branch_name}")
        for member in manifest:
            # Its own branch keeps every named revision an ancestor.
            assert git_status(
                path, "merge-base", "--is-ancestor", member["source_sha"], head
            ).returncode == 0, member
        await self.db.transition_task(repair_task_id, TaskStatus.IN_PROGRESS)
        await self.db.transition_task(repair_task_id, TaskStatus.COMPLETED)
        async with self.db.immediate() as conn:
            await conn.execute(
                update(t.task_integration_checkpoints)
                .where(t.task_integration_checkpoints.c.task_id == repair_task_id)
                .values(checkpoint_sha=head, updated_at=self.clock.now)
            )
        return head


# ----------------------------------------------------------------- invariants


async def assert_reconciler_only(world: Development) -> None:
    """Zero operator commands: every command is a journalled reconciler decision."""
    for subject in await world.subjects():
        journal = await world.journal(subject.id)
        assert [row["seq"] for row in journal] == sorted(row["seq"] for row in journal)
        decisions = {
            row["subject_version"]: row
            for row in journal
            if row["entry_kind"] == "decision" and row["mode"] == "active"
        }
        for row in journal:
            result = row["payload"].get("result")
            if row["entry_kind"] != "action" or result is None:
                continue
            # The committed decision precedes every action of its visit.
            decision = decisions[row["subject_version"]]
            assert decision["seq"] < row["seq"]
            assert decision["primitive"] == row["primitive"] == result["primitive"]
            assert decision["policy_artifact_sha256"] == subject.policy.artifact_sha256


async def assert_delivery_truth(world: Development, delivered: dict[str, str]) -> None:
    """The default branch holds exactly the delivered sources' revisions."""
    main = world.remote("refs/heads/main")
    for task_id, head in delivered.items():
        assert world.contains(head, main), task_id
    history = world.main_history()
    assert history[0] == world.base


# --------------------------------------------------------------------- tests


@pytest.fixture
async def world(tmp_path):
    development = Development(tmp_path)
    yield development
    await development.close()


COLLIDING = "value = 1\n"

#: A pinned rebuild command. The merge identity journals it, so the factory
#: test can see which settings the shared Git primitive actually resolved.
REGENERATE = "scripts/regenerate-generated.sh --check"


async def test_the_factory_admits_a_pushed_member_and_resolves_the_pinned_retained_store(world):
    """The daemon's own construction, with nothing wired in its place.

    Two wirings this covers can only be seen through the factory: the frontier
    reader's Git port (without it no member is ever ``pushed``, so every seal is
    ``empty``) and the Git primitives' retained repository (without the
    subject's own pinned settings it raises instead of resolving).
    """
    await world.open(regenerate=REGENERATE)
    world.source("alpha", {"checks/alpha.py": "VALUE = 1\n"})
    await world.add_source("alpha")
    # The repository the integration engine owns carries no base checkout, so a
    # checkout-bound Git port would answer unknown for every branch.
    assert (await world.db.get_repo(REPO)).checkout_base_path == ""
    root = await world.cutover()
    runtime = world.factory_runtime()

    frontier = await runtime.adapter.frontier_for(root)
    assert [(member.facts.task_id, member.pushed) for member in frontier.members] == [
        ("alpha", True)
    ]
    assert [member.task_id for member in ordered_development_members(frontier, 10)] == ["alpha"]

    # The shared Git primitives resolve the retained clone through the subject's
    # own pinned artifact, so the merge identity names that rebuild command.
    outcome = await runtime.adapter.shared.invoke(
        root,
        MergeMembersArgs(
            target_ref=root.target_ref,
            base_sha=world.base,
            members=(MemberRef(task_id="alpha", head_sha=world.heads["alpha"], base_sha=world.base),),
        ),
    )
    assert outcome.outcome == "merged", outcome
    [prepared] = [
        row
        for row in await world.journal(root.id)
        if row["primitive"] == "git_merge_members" and row["outcome"] == "prepared"
    ]
    assert prepared["payload"]["regenerate"] == REGENERATE
    ancestry = await runtime.adapter.shared.invoke(
        root,
        AncestryArgs(
            repository_id=REPO,
            queries=(AncestryQuery(ancestor=world.base, descendant=world.base),),
        ),
    )
    assert ancestry.outcome == "facts", ancestry
    assert ancestry.detail["queries"][0]["is_ancestor"] is True
    # Nothing was published: a retained clone is not a delivery source.
    assert world.remote("refs/heads/main") == world.base


async def test_parked_member_holds_only_its_dependents_and_keeps_the_rest_building(world):
    await world.open()
    # alpha and bravo collide on one file; charlie depends on bravo; delta is
    # independent and must not wait for the park.
    world.source("alpha", {"checks/alpha.py": COLLIDING, "shared.txt": "alpha\n"})
    world.source("bravo", {"checks/bravo.py": "VALUE = 2\n", "shared.txt": "bravo\n"})
    world.source("charlie", {"checks/charlie.py": "VALUE = 3\n"})
    world.source("delta", {"checks/delta.py": "VALUE = 4\n"})
    for task_id in ("alpha", "bravo", "delta"):
        await world.add_source(task_id)
    # charlie's only release condition is bravo, the revision that will park.
    await world.add_source("charlie", depends_on=("bravo",))
    root = await world.cutover()
    await world.collect()
    # The collector lease is the publication fence the table requires.
    await world.run_until(
        lambda: world.phase(root.id, SubjectPhase.TESTING), label="independent member merged"
    )
    parked = await world.sources()
    assert [subject.task_id for subject in parked] == ["bravo"]
    assert parked[0].head_sha == world.heads["bravo"]
    assert parked[0].engine.value == "reconciler"

    await world.run_until(lambda: world.phase(root.id, SubjectPhase.DONE), label="published")
    main = world.remote("refs/heads/main")
    assert world.contains(world.heads["alpha"], main)
    assert world.contains(world.heads["delta"], main)
    # The colliding source and the source that depends on it are not delivered.
    for task_id in ("bravo", "charlie"):
        assert not world.contains(world.heads[task_id], main), task_id
    # A park is exact and reversible: the source branch is untouched.
    assert world.remote("refs/heads/aq/bravo") == world.heads["bravo"]
    assert world.remote("refs/heads/aq/charlie") == world.heads["charlie"]
    assert "aq/bravo" not in world.git.deletes

    actions = [
        (row["primitive"], row["outcome"])
        for row in await world.journal(root.id)
        if row["entry_kind"] == "action"
    ]
    assert ("git_merge_members", "conflict") in actions
    assert ("git_publish", "published") in actions
    park_rows = [
        row for row in await world.journal(root.id)
        if row["idempotency_key"].startswith("development:park:")
    ]
    assert [row["payload"]["member"] for row in park_rows] == ["bravo"]
    assert park_rows[0]["payload"]["source_sha"] == world.heads["bravo"]
    # The frozen manifest admitted members in dependency order: charlie waits on
    # bravo, while the independent delta is admitted ahead of it.
    manifest = next(
        row["payload"]
        for row in await world.journal(root.id)
        if row["idempotency_key"] == "development:manifest"
    )
    assert [member["facts"]["task_id"] for member in manifest["members"]] == [
        "alpha", "bravo", "delta", "charlie"
    ]
    assert manifest["members"][3]["dependencies"] == ["bravo"]
    await assert_delivery_truth(world, {
        "alpha": world.heads["alpha"],
        "delta": world.heads["delta"],
    })
    assert {name for name, _args in world.commands} <= ADAPTER_COMMANDS
    await assert_reconciler_only(world)


async def test_validation_failure_is_preserved_and_only_the_repaired_green_publishes(world):
    await world.open()
    world.source("echo", {"checks/echo.py": "import os\n"})
    world.source("foxtrot", {"checks/foxtrot.py": "VALUE = 6\n"})
    for task_id in ("echo", "foxtrot"):
        await world.add_source(task_id)
    root = await world.cutover()
    await world.collect()
    await world.run_until(lambda: world.phase(root.id, SubjectPhase.REPAIRING), label="red")
    red = await world.subject(root.id)
    assert red.head_sha is not None
    assert world.remote("refs/heads/main") == world.base, "a red candidate reached main"

    # The failure is preserved as evidence of the exact head, not discarded.
    observed = [
        row for row in await world.journal(root.id)
        if row["primitive"] == "ci_observe" and row["outcome"] == "red"
    ]
    assert observed, await world.describe()
    assert {row["head_sha"] for row in observed} == {red.head_sha}
    assert all(row["mode"] == "active" for row in observed)

    # The existing repair filing owns the aggregate scope.
    async def aggregate_repair_filed():
        tasks = await world.db.list_tasks(project_id=PROJECT)
        return [task for task in tasks if task.id.startswith("development-repair-")]

    await world.run_until(aggregate_repair_filed, label="aggregate repair filed")
    repair_task_id = (await aggregate_repair_filed())[0].id
    contract = await world.db.get_task_meta(repair_task_id, "development_repair_sources")
    assert {member["task_id"] for member in contract} == {"echo", "foxtrot"}
    assert {member["source_sha"] for member in contract} == {
        world.heads["echo"], world.heads["foxtrot"]
    }
    # Filing is replay-safe: a repeated visit reuses the same repair.
    from src.integration.subjects import Primitive, WriterFileArgs

    args = WriterFileArgs(
        primitive=Primitive.WRITER_FILE,
        role="repair",
        ordinal=1,
        intelligence_class="standard-high",
        budget_seconds=3600,
        brief="Resolve the failing development aggregate.",
    )
    assert (
        await world.adapter.shared.invoke(await world.subject(root.id), args)
    ).outcome == "exists"

    head = await world.repair_worker(
        repair_task_id, {}, fix={"checks/echo.py": "VALUE = 5\n"}
    )
    async def repaired_aggregate():
        current = await world.subject(root.id)
        return (
            current.generation > red.generation and current.head_sha != red.head_sha
        )

    await world.run_until(repaired_aggregate, label="repaired aggregate rebuilt")
    rebuilt = await world.subject(root.id)
    assert rebuilt.generation > red.generation
    # The repair carries both frozen sources, so the rebuild is a new aggregate
    # whose parent is the repair's own head: no frozen revision is rewritten.
    assert world.contains(head, rebuilt.head_sha), "the repair's head was not adopted"
    # The red aggregate is superseded, not rewritten: the repair resolved the
    # same two frozen revisions on its own branch, and the red commit is kept
    # as evidence rather than folded into the new aggregate.
    assert world.contains(world.heads["echo"], rebuilt.head_sha)
    assert world.contains(world.heads["foxtrot"], rebuilt.head_sha)
    await world.run_until(lambda: world.phase(root.id, SubjectPhase.DONE), label="published")
    main = world.remote("refs/heads/main")
    assert main == rebuilt.head_sha
    assert red.head_sha not in world.main_history()
    for task_id in ("echo", "foxtrot"):
        assert world.contains(world.heads[task_id], main), task_id
    await assert_delivery_truth(world, {"echo": world.heads["echo"],
                                        "foxtrot": world.heads["foxtrot"]})
    assert {name for name, _args in world.commands} <= ADAPTER_COMMANDS
    await assert_reconciler_only(world)


async def test_existing_development_state_migrates_read_only_and_rolls_back_without_deleting(
    world, record_property
):
    """The old engine's durable rows are read, never rewritten or discarded.

    One batch the old publisher already delivered and one it parked are the
    installation's durable state. Adoption reads both, delivers only the work
    that is still owed, and an audited rollback returns the project to the old
    engine with every source branch, journal entry and operation row intact.
    """
    await world.open()
    world.source("golf", {"checks/golf.py": "VALUE = 7\n"})
    world.source("hotel", {"checks/hotel.py": "VALUE = 8\n"})
    world.source("india", {"checks/india.py": "VALUE = 9\n"})
    for task_id in ("golf", "hotel", "india"):
        await world.add_source(task_id)
    # The old engine landed golf on the default branch and parked hotel.
    git(world.work, "push", "origin", f"{world.heads['golf']}:refs/heads/main")
    assert world.remote("refs/heads/main") == world.heads["golf"]
    # The existing publisher's own writers record what it already delivered and
    # what it parked, in its own durable operation rows.
    await world.legacy.save({
        "id": "operation-delivered",
        "project_id": PROJECT,
        "repository_id": REPO,
        "target_ref": "refs/heads/main",
        "state": "delivered",
        "created_at": 900.0,
        "manifest": [{"task_id": "golf", "source_sha": world.heads["golf"]}],
        "expected_sha": world.base,
        "prepared_sha": world.heads["golf"],
    })
    await world.legacy.save({
        "id": "operation-parked",
        "project_id": PROJECT,
        "repository_id": REPO,
        "target_ref": "refs/heads/main",
        "state": "parked",
        "created_at": 901.0,
        "manifest": [{"task_id": "hotel", "source_sha": world.heads["hotel"]}],
        "expected_sha": world.base,
    })
    root = await world.cutover()
    subject = await world.root()
    frontier = await world.adapter.frontier_for(subject)
    # The delivered revision is satisfied truth and the parked one is an exact
    # park: neither is offered for redelivery.
    assert frontier.satisfied == frozenset({"golf"})
    assert frontier.parked == frozenset({("hotel", world.heads["hotel"])})
    # The delivered revision is not a member, the parked one is visible but
    # withheld, and only the still-owed work is admissible.
    assert {member.facts.task_id for member in frontier.members} == {"hotel", "india"}
    assert [member.task_id for member in ordered_development_members(frontier, 10)] == ["india"]

    await world.collect()
    await world.run_until(
        lambda: world.phase(root.id, SubjectPhase.DONE), label="owed work delivered"
    )
    main = world.remote("refs/heads/main")
    assert world.contains(world.heads["india"], main)
    # Only the still-owed work was merged; the default branch already held golf.
    builds = [
        [
            member["task_id"]
            for member in (((row["payload"] or {}).get("decision") or {}).get("request") or {}).get(
                "members", []
            )
        ]
        for row in await world.journal(root.id)
        if row["entry_kind"] == "decision" and row["primitive"] == "git_merge_members"
    ]
    assert builds and set(builds[0]) == {"india"}
    # The ref log of the fixture origin records base, the old delivery, then
    # the reconciler's publication.
    assert world.main_history() == [world.base, world.heads["golf"], main]
    # Undivered work keeps its branch; the delivered member's branch is
    # collected through the existing backup-and-log deletion path.
    for task_id in ("golf", "hotel"):
        assert world.remote(f"refs/heads/aq/{task_id}") == world.heads[task_id], task_id
    assert world.remote("refs/heads/aq/india") is None
    logs = list(world.legacy.backup_dir.glob("*.tsv"))
    assert logs, "no deletion log was written"
    assert "aq/india" in logs[0].read_text()
    cleaned = [
        ((row["payload"] or {}).get("result") or {}).get("detail", {}).get("outcomes")
        for row in await world.journal(root.id)
        if row["primitive"] == "cleanup" and row["outcome"] == "clean"
    ]
    assert {"aq/india": "deleted"} in cleaned

    # An audited rollback is the same serialized transfer back to the old engine.
    current = await world.subject(root.id)
    preview = await transfer_development_engine(
        world.db, PROJECT, engine="legacy", expected_versions={}, reason="", dry_run=True
    )
    assert preview["outcome"] == "preview"
    assert preview["current_engines"][current.id] == "reconciler"
    rolled = await transfer_development_engine(
        world.db,
        PROJECT,
        engine="legacy",
        expected_versions={current.id: current.version},
        reason="rollback after scenario proof",
        operator_id="human:local-operator",
        clock=world.clock,
    )
    assert rolled["outcome"] == "transferred"
    after = await world.subject(root.id)
    assert after.engine.value == "legacy"
    assert after.phase is SubjectPhase.DONE, "rollback rewrote subject state"
    transfer_rows = [
        row for row in await world.journal(root.id)
        if row["payload"].get("to") == "legacy"
    ]
    assert transfer_rows, await world.describe()
    assert transfer_rows[-1]["payload"]["operator_id"] == "human:local-operator"
    # Nothing durable was discarded: operations, branches and the subject remain.
    async with world.db._engine.connect() as conn:
        from src.integration.development import operation_rows_on

        operations = await operation_rows_on(conn, [PROJECT])
    assert {row["id"] for row in operations} == {"operation-delivered", "operation-parked"}
    for task_id in ("golf", "hotel"):
        assert world.remote(f"refs/heads/aq/{task_id}") == world.heads[task_id], task_id
    assert world.remote("refs/heads/main") == main
    assert world.commands == [], "migration or rollback ran a command"
    # The exact disposable identities behind this report, read back from the
    # rows the scenario really wrote. Nothing here authorizes production.
    record_property("rollout_evidence", json.dumps({
        "reviewed_development_artifact": world.artifact_sha,
        "project_id": PROJECT,
        "repository_id": REPO,
        "subject_id": root.id,
        "subject_version_before_rollback": current.version,
        "subject_version_after_rollback": after.version,
        "engine_after_rollback": after.engine.value,
        "published_main_sha": main,
        "fixture_base_sha": world.base,
        "sources": {task_id: world.heads[task_id] for task_id in ("golf", "hotel", "india")},
        "retained_source_branches": ["aq/golf", "aq/hotel"],
        "collected_source_branch": "aq/india",
        "deletion_log": str(logs[0]),
        "transfer_operator_id": "human:local-operator",
        "production_authorization": None,
    }, sort_keys=True))


async def test_engine_transfer_serializes_with_a_running_publisher(world):
    """The transfer shares the publisher's own repository fence identity.

    A publisher holds the shared engine lock for its whole publication, outside
    any transfer's transaction. The audited transfer takes the *exclusive* lock
    of the same identity, so it waits for a publisher in flight instead of
    racing it, and a publisher cannot start while a transfer holds it.
    """
    await world.open()
    world.source("kilo", {"checks/kilo.py": "VALUE = 10\n"})
    await world.add_source("kilo")
    root = await world.cutover()
    current = await world.subject(root.id)
    versions = {current.id: current.version}

    holding, release, failure = asyncio.Event(), asyncio.Event(), []

    async def publishing():
        # Exactly what a running legacy or reconciler publisher holds.
        try:
            async with publisher_exclusion(world.db, REPO, subject=current):
                holding.set()
                await release.wait()
        except (DevelopmentBusy, EngineRefused, OSError) as exc:
            # A publisher that cannot take its fence is surfaced, not ignored.
            failure.append(exc)
            holding.set()

    publisher = asyncio.create_task(publishing())
    await asyncio.wait_for(holding.wait(), timeout=30)
    assert not failure, f"the publisher could not take its fence: {failure!r}"
    transfer = asyncio.create_task(
        transfer_development_engine(
            world.db,
            PROJECT,
            engine="legacy",
            expected_versions=versions,
            reason="rollback while a publisher is running",
            operator_id="human:local-operator",
            clock=world.clock,
        )
    )
    await asyncio.sleep(0.5)
    assert not transfer.done(), "the transfer completed while a publisher held the fence"
    # Ownership is untouched: the refused transfer moved nothing.
    assert (await world.subject(root.id)).engine.value == "reconciler"
    release.set()
    result = await transfer
    assert result["outcome"] == "transferred"
    await publisher
    assert (await world.subject(root.id)).engine.value == "legacy"

    # The same identity, the other way: after the rollback the old publisher
    # owns the project, and it cannot get past the fence while the transfer's
    # exclusive lock is held. It proceeds once the fence is gone.
    async def acquiring():
        async with publisher_exclusion(world.db, REPO):
            return "acquired"

    async with RootEngineOwnership(world.db, clock=world.clock).exclusion(REPO):
        blocked = asyncio.create_task(acquiring())
        await asyncio.sleep(0.5)
        assert not blocked.done(), "a publisher started while the transfer fence was held"
    assert await asyncio.wait_for(blocked, timeout=30) == "acquired"


async def publish_intent_row(world, subject, *, intent: str, outcome: str, sha: str | None = None):
    """Append one publish journal row the way the shared publisher writes it.

    The prepare is journalled under its intent key and applied under that key
    plus ``:applied``; a reconciler answer names the intent in its detail.
    """
    return await world.db.append_integration_subject_journal(
        {
            "subject_id": subject.id,
            "entry_kind": "action",
            "mode": "active",
            "idempotency_key": intent if outcome == "prepared" else f"{intent}:applied",
            "policy_artifact_sha256": subject.policy.artifact_sha256,
            "subject_version": subject.version,
            "phase": subject.phase.value,
            "head_sha": sha or subject.head_sha,
            "generation": subject.generation,
            "primitive": "git_publish",
            "outcome": outcome,
            "payload": {"repository_id": REPO, "ref": subject.target_ref},
            "recorded_at": world.clock.now,
        }
    )


async def test_engine_transfer_refuses_an_unresolved_publication_in_both_directions(world):
    """An ambiguous publish write blocks cutover *and* rollback.

    A prewrite can survive a crash. Neither engine may take ownership while the
    newest intent is unresolved: the reconciler must not adopt one, and the old
    publisher must not resume one.
    """
    await world.open()
    world.source("lima", {"checks/lima.py": "VALUE = 11\n"})
    await world.add_source("lima")
    root = await world.cutover()
    current = await world.subject(root.id)
    await publish_intent_row(world, current, intent="git:first-intent", outcome="prepared")

    async def refused(engine):
        """Both directions refuse, and the refusal moves no ownership."""
        live = await world.subject(root.id)
        with pytest.raises(ValueError, match="unresolved development publication"):
            await transfer_development_engine(
                world.db,
                PROJECT,
                engine=engine,
                expected_versions={live.id: live.version},
                reason=f"attempt {engine} transfer with an open publication",
                evidence=("shadow-week", "development-scenarios") if engine == "reconciler" else (),
                clock=world.clock,
            )
        assert (await world.subject(root.id)).engine.value == live.engine.value

    await refused("reconciler")
    await refused("legacy")

    # A confirmation settles only the intent it names. A later prepare is still
    # unresolved behind it, so the subject-level "it published once" answer must
    # not hide the write a crashed publisher left open.
    await publish_intent_row(world, current, intent="git:first-intent", outcome="applied")
    await publish_intent_row(world, current, intent="git:second-intent", outcome="prepared")
    await refused("reconciler")
    await refused("legacy")

    # Confirming the newest intent releases the fence for both directions.
    await publish_intent_row(world, current, intent="git:second-intent", outcome="applied")
    live = await world.subject(root.id)
    rolled = await transfer_development_engine(
        world.db,
        PROJECT,
        engine="legacy",
        expected_versions={live.id: live.version},
        reason="rollback after the publication settled",
        clock=world.clock,
    )
    assert rolled["outcome"] == "transferred"
    assert (await world.subject(root.id)).engine.value == "legacy"

    # A newer prepare after that rollback is unresolved again.
    await publish_intent_row(world, current, intent="git:third-intent", outcome="prepared")
    await refused("reconciler")
