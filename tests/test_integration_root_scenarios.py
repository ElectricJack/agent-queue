"""Rev-2 section-6 root acceptance: the reconciler drives a real Git/PostgreSQL train.

After the operator's audited engine transfer, every train mutation is a
reconciler decision that runs one existing command through the root primitive
adapters, on the daemon's ``IntegrationService`` remote pass beside the legacy
service sources. The tests play only the outside world: repair workers that
claim, push and close (or never claim), the trusted CI producer, and newly
approved work. No operator, recovery or legacy-rule command runs.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import delete, insert, select, update

from src.commands.handler import CommandHandler
from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.config import AppConfig, DatabaseConfig, DiscordConfig
from src.database import Database
from src.database import tables as t
from src.git.github_app import GitHubRepositoryBinding
from src.git.manager import GitManager
from src.integration.accepted_repair import complete_accepted_delegate
from src.integration.candidates import AuditPullRequest, CandidateResolutionInput, CandidateService
from src.integration.ci import (
    CandidateCISubject,
    CIReceiptPayload,
    CIService,
    FailedCIObservation,
    IntegrationCITrust,
    TrustedCIObservation,
    TrustedFixtureObserver,
)
from src.integration.cleanup import IntegrationCleanupService
from src.integration.engine import RootEngineOwnership, root_engine_guard
from src.integration.main_promotion import RootAttestationProof, RootPromotionService
from src.integration.models import BranchKey, Fence, HierarchicalIntegrationPolicy
from src.integration.ownership import BranchOwnership
from src.integration.promotion import PromotionService
from src.integration.release import IntegrationReleaseService
from src.integration.repair import RepairService
from src.integration.review_evidence import ReviewEvidenceProducer
from src.integration.root_adapters import RootPrimitiveAdapters
from src.integration.root_runtime import PinnedRootPolicy, RootObserver, RootSubjectRuntime
from src.integration.scheduler import IntegrationScheduler, TrainService
from src.integration.service import IntegrationService
from src.integration.subjects import PrimitivePorts, RemoteHead, Subject, SubjectPhase, WriterStatus
from src.models import Project, RepoConfig, RepoSourceType, SessionRecord
from src.orchestrator import Orchestrator
from src.playbooks.definition import load_definition_json
from src.playbooks.integration_policy import IntegrationPolicyFacts, policy_from_markdown
from src.profiles.capabilities import CapabilityPolicy
from tests.db_fixtures import lease_dsn
from tests.test_integration_candidates import _AppClient, _LocalPushGit

ROOT = Path(__file__).resolve().parents[1]
POLICY = json.loads((ROOT / "docs/config/agent-queue-train-policy.json").read_text())
BUNDLE = ROOT / "src/prompts/reviewed_playbooks/agent-queue-root-train"
PROJECT, REPO = "agent-queue", "aq-repo"
GITHUB_URL = "https://github.com/example/repo.git"
BINDING = GitHubRepositoryBinding(9, "example/repo")
REQUIRED = POLICY["root"]["required_checks"]
PRIMARY_SECONDS = POLICY["root"]["repair"]["primary_seconds"]
#: The only commands a reconciler visit may run: the phase-one adapters.
ADAPTER_COMMANDS = frozenset(
    {
        "integration_seal",
        "integration_build_candidate",
        "integration_ci_evidence",
        "integration_promote_main",
        "integration_repair_start",
        "integration_repair_dispatch",
        "integration_repair_timeout",
        "integration_release_owner",
        "integration_eject",
        "integration_cleanup",
        "integration_release",
        "gate_create",
    }
)
COLLIDING = "revision='a00000000002'\ndown_revision='a00000000001'\n"


def _git(cwd, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _git_status(cwd, *args: str) -> subprocess.CompletedProcess:
    """A Git probe whose exit status is the answer (no exception on non-zero)."""
    return subprocess.run(["git", *args], cwd=cwd, check=False, capture_output=True, text=True)


class Clock:
    def __init__(self, now: float):
        self.now = now

    def __call__(self) -> float:
        return self.now


class OriginGit(_LocalPushGit):
    """GitManager over the bare fixture origin, addressed by its GitHub identity."""

    def __init__(self, origin):
        super().__init__(origin)
        self.deleted: list[str] = []

    async def acreate_bare_checkout(self, url, path):
        _git(Path(path).parent, "clone", "--bare", str(self.origin), str(path))
        _git(path, "remote", "set-url", "origin", url)

    async def adelete_repository_ref(self, _store, *, repository, branch, expected_old_oid):
        assert repository == BINDING
        _git(self.origin, "update-ref", "-d", f"refs/heads/{branch}", expected_old_oid)
        self.deleted.append(branch)


class OriginReads:
    """The observer's Git port: exact reads of the real bare origin."""

    def __init__(self, origin):
        self.origin = origin

    async def remote_head(self, repository, ref):
        sha = _remote(self.origin, ref)
        if sha is None:
            return RemoteHead(ref=ref, state="absent")
        return RemoteHead(ref=ref, state="present", sha=sha)

    async def is_ancestor(self, repository, ancestor, descendant):
        result = _git_status(self.origin, "merge-base", "--is-ancestor", ancestor, descendant)
        return {0: True, 1: False}.get(result.returncode)


def _remote(origin, ref: str) -> str | None:
    return _git_status(origin, "rev-parse", "--verify", "-q", ref).stdout.strip() or None


class Forge:
    """Source and audit pull requests, as the cleanup and candidate see them."""

    def __init__(self):
        self.prs: dict[int, dict] = {}
        self.audits: dict[str, AuditPullRequest] = {}
        self.markers: set = set()
        self.comments: list[str] = []
        self.closed: list[int] = []

    def open(self, number: int, head: str) -> None:
        self.prs[number] = {
            "repository_numeric_id": 9,
            "repository_full_name": "example/repo",
            "head_sha": head,
            "state": "open",
        }

    async def lookup_audit_pr(self, *, idempotency_key, branch):
        return self.audits.get(idempotency_key)

    async def create_audit_pr(self, **kwargs):
        number = 900 + len(self.audits)
        result = AuditPullRequest(
            url=f"https://github.com/example/repo/pull/{number}",
            number=number,
            head_sha=kwargs["head_sha"],
            head_branch=kwargs["branch"],
            base_branch=kwargs["base_branch"],
            repository_numeric_id=kwargs["repository_numeric_id"],
            repository_full_name=kwargs["repository_full_name"],
            idempotency_key=kwargs["idempotency_key"],
        )
        self.audits[kwargs["idempotency_key"]] = result
        self.open(number, kwargs["head_sha"])
        return result

    async def exact_pull_request(self, *, number):
        return self.prs.get(number)

    async def has_comment_marker(self, *, number, marker):
        return (number, marker) in self.markers

    async def comment_pull_request(self, *, number, marker, body):
        self.markers.add((number, marker))
        self.comments.append(body)

    async def close_pull_request(self, *, number):
        self.prs[number]["state"] = "closed"
        self.closed.append(number)


class CIProducer:
    """The trusted exact-SHA producer behind ``integration_ci_evidence``.

    Real ``CIService`` classifies and records the evidence against the frozen
    required-check set; only the GitHub transport is a fixture. A head with no
    finished run is pending.
    """

    def __init__(self, db, clock):
        self.db, self.clock = db, clock
        self.runs: dict[str, object] = {}
        self.trust = IntegrationCITrust(
            canonical_repository_id=REPO,
            repository_id=9,
            full_name="example/repo",
            producer_id=REQUIRED["producer_id"],
            required_checks={"version": REQUIRED["version"], "names": tuple(REQUIRED["names"])},
        )

    def finish(self, sha: str, conclusion: str, run: int) -> None:
        checks = [
            {
                "name": name,
                "check_run_id": run * 100 + index,
                "check_suite_id": run,
                "producer_app_id": int(REQUIRED["producer_id"]),
                "producer_id": REQUIRED["producer_id"],
                "head_sha": sha,
                "conclusion": conclusion if index == 0 else "success",
            }
            for index, name in enumerate(REQUIRED["names"])
        ]
        workflow = {
            "workflow_run_id": run,
            "run_attempt": 1,
            "check_suite_id": run,
            "head_sha": sha,
            "conclusion": conclusion,
        }
        if conclusion == "success":
            receipt = CIReceiptPayload.model_validate(
                {
                    "schema": "aq.integration-ci-receipt.v1",
                    "canonical_repository_id": REPO,
                    "repository_id": 9,
                    "full_name": "example/repo",
                    "producer_id": REQUIRED["producer_id"],
                    "head_sha": sha,
                    "required_check_set_version": REQUIRED["version"],
                    "checks": checks,
                    "workflow_runs": [workflow],
                }
            )
            self.runs[sha] = TrustedCIObservation(receipt, {run: run + 1000})
        else:
            self.runs[sha] = FailedCIObservation(
                checks=tuple(checks),
                workflow_runs=(workflow,),
                workflow_ids={run: run + 1000},
                conclusion="failure",
            )

    @root_engine_guard("candidate", outcome="stale_subject")
    async def handle_candidate_ci(self, row, _now):
        observation = self.runs.get(row["candidate_sha"])
        if observation is None:
            return {"outcome": "not_green"}
        result = await CIService(
            self.db, self.trust, TrustedFixtureObserver(observation), clock=self.clock
        ).observe_candidate(CandidateCISubject.model_validate(row))
        if result["outcome"] == "green":
            return {"outcome": "published", "evidence_ids": result["evidence_ids"]}
        return result


class LegacyRulesDisabled:
    """The root-train playbook rules stay disabled while the reconciler acts."""

    async def dispatch_due(self, now):
        return 0


class Train:
    """Fixture world plus the service-owned reconciler, wired as the daemon wires it."""

    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.origin, self.work = tmp_path / "origin.git", tmp_path / "work"
        self.clock = Clock(1000.0)
        self.heads: dict[str, str] = {}
        self.commands: list[tuple[str, dict, dict]] = []
        self.live_sessions: set[str] = set()
        self.service = None

    # ------------------------------------------------------------------ git

    def init_git(self) -> None:
        _git(self.tmp_path, "init", "--bare", "--initial-branch=main", str(self.origin))
        # Every update of main is kept, so the test can audit what main held.
        _git(self.origin, "config", "core.logAllRefUpdates", "always")
        _git(self.tmp_path, "clone", str(self.origin), str(self.work))
        _git(self.work, "config", "user.name", "Root Scenario")
        _git(self.work, "config", "user.email", "root@example.test")
        versions = self.work / "migrations" / "versions"
        versions.mkdir(parents=True)
        (self.work / "alembic.ini").write_text("[alembic]\nscript_location = migrations\n")
        (versions / "base.py").write_text("revision='a00000000001'\ndown_revision=None\n")
        (self.work / "base.txt").write_text("base\n")
        _git(self.work, "add", "-A")
        _git(self.work, "commit", "-m", "base")
        self.base = _git(self.work, "rev-parse", "HEAD")
        _git(self.work, "push", "origin", "main")

    def source(self, name: str, files: dict[str, str]) -> str:
        _git(self.work, "switch", "-C", f"aq/{name}", self.base)
        for path, text in files.items():
            target = self.work / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        _git(self.work, "add", "-A")
        _git(self.work, "commit", "-m", f"{name} feature")
        self.heads[name] = _git(self.work, "rev-parse", "HEAD")
        _git(self.work, "push", "-f", "origin", f"HEAD:refs/heads/aq/{name}")
        return self.heads[name]

    def remote(self, ref: str) -> str | None:
        return _remote(self.origin, ref)

    def contains(self, ancestor: str, descendant: str) -> bool:
        return (
            _git_status(self.origin, "merge-base", "--is-ancestor", ancestor, descendant).returncode
            == 0
        )

    def main_history(self) -> list[str]:
        """Every value main ever held, oldest first, from the origin's ref log."""
        log = _git(self.origin, "reflog", "show", "--format=%H", "refs/heads/main")
        return list(reversed(log.split()))

    # ------------------------------------------------------------- daemon

    async def open(self, policy_variant=None) -> None:
        self.init_git()
        self.db = db = Database(lease_dsn("root-scenarios.db"))
        await db.initialize()
        artifact_text = (BUNDLE / "artifact.json").read_text()
        definition = load_definition_json(artifact_text)
        if policy_variant is not None:
            definition = policy_variant(definition)
        self.definition = definition
        policy = json.loads(json.dumps(POLICY))
        route = policy["root"]["route"]
        route["artifact"]["artifact_sha256"] = definition.artifact_sha256()
        self.policy = HierarchicalIntegrationPolicy.model_validate(policy).model_dump(mode="json")
        await db.create_project(Project(id=PROJECT, name="Root acceptance"))
        await db.create_repo(
            RepoConfig(
                id=REPO,
                project_id=PROJECT,
                source_type=RepoSourceType.CLONE,
                url=GITHUB_URL,
                default_branch="main",
            )
        )
        await db.update_project(
            PROJECT,
            hierarchical_integration_mode="train",
            integration_repository_id=REPO,
            hierarchical_integration_policy=self.policy,
            integration_mode="pull_request",
        )
        async with db.immediate() as conn:
            await conn.execute(
                insert(t.playbook_artifacts).values(
                    **route["artifact"],
                    scope="project",
                    scope_identifier=PROJECT,
                    profile_fingerprint="",
                    path=str(BUNDLE / "artifact.json"),
                    size_bytes=len(artifact_text),
                    validation="{}",
                    created_at=1.0,
                )
            )
        self.git = OriginGit(self.origin)
        self.app = _AppClient(self.origin)
        self.app.repository = BINDING
        self.forge = Forge()
        self.ci = CIProducer(db, self.clock)
        self.data_dir = self.tmp_path / "data"
        self.repair = RepairService(db, clock=self.clock)

        async def attestation(subject):
            # The App attestation check run of the exact green subject.
            return RootAttestationProof(
                **subject.model_dump(),
                check_run_id=7001,
                external_id="aq-attestation-v1:" + "9" * 64,
            )

        config = AppConfig(
            discord=DiscordConfig(bot_token="t", guild_id="1"),
            workspace_dir=str(self.tmp_path / "workspaces"),
            database=DatabaseConfig(url=lease_dsn("root-scenarios.db")),
            data_dir=str(self.data_dir),
        )
        orchestrator = Orchestrator(config)
        orchestrator.db = db
        orchestrator.git = self.git
        orchestrator.integration_candidate_service = CandidateService(
            db,
            data_dir=self.data_dir,
            git_manager=self.git,
            app_client=self.app,
            forge_provider=self.forge,
            repair_service=self.repair,
            clock=self.clock,
        )
        orchestrator.repair_service = self.repair
        # The section-6 batch keeps three colliding revisions together for its
        # writer to re-chain, as the continuous-delivery fixture did; the
        # production seal would defer them to later batches instead.
        async def source_admission(member, policy):
            number = int(member["pr_url"].rsplit("/", 1)[-1])
            pull = self.forge.prs[number]
            if pull["state"] != "open":
                return {"reason": "pr_closed"}
            assert pull["head_sha"] == member["source_head"]
            return {"reason": None,
                    "tree": _git(self.origin, "rev-parse", member["source_head"] + "^{tree}"),
                    "checks": {"head_sha": member["source_head"], "state": "green"},
                    "target_sha": self.remote("refs/heads/main"), "observed_at": self.clock()}

        orchestrator.integration_train_service = TrainService(db, admission_reader=source_admission)
        orchestrator.integration_attestation_service = self.ci
        orchestrator.root_promotion_service = RootPromotionService(
            db,
            data_dir=self.data_dir,
            git_manager=self.git,
            app_client=self.app,
            attestation_resolver=attestation,
            clock=self.clock,
        )
        orchestrator.integration_cleanup_service = IntegrationCleanupService(
            db,
            data_dir=self.data_dir,
            git_manager=self.git,
            github_client_factory=lambda _binding: self.app,
            forge_provider=self.forge,
            clock=self.clock,
        )
        orchestrator.integration_release_service = IntegrationReleaseService(db)
        self.handler = CommandHandler(orchestrator, config)
        # A writer's own resolve command builds its service with the pool
        # handoff confirmation: a detached checkout CAS-releases its fence.
        self.writer_ownership = BranchOwnership(
            db, confirm_handoff=self.pool_handoff, clock=self.clock
        )
        self.writer_candidates = CandidateService(
            db,
            data_dir=self.data_dir,
            git_manager=self.git,
            app_client=self.app,
            forge_provider=self.forge,
            branch_ownership=self.writer_ownership,
            repair_service=self.repair,
            clock=self.clock,
        )
        train = self

        class RecordingCommands:
            async def execute(self, name, args):
                result = await train.handler.execute(name, dict(args))
                train.commands.append((name, dict(args), result))
                return result

        self.observer = RootObserver(
            db,
            OriginReads(self.origin),
            facts_type=IntegrationPolicyFacts,
            session_probe=self.session_probe,
            clock=self.clock,
        )
        ports = RootPrimitiveAdapters(
            db, RecordingCommands(), self.observer, clock=self.clock
        ).bind(PrimitivePorts())
        self.runtime = RootSubjectRuntime(
            db,
            self.observer,
            PinnedRootPolicy(self.load_artifact),
            ports,
            active=True,
            shadow=True,
            clock=self.clock,
        )
        self.scheduler = IntegrationScheduler(db, clock=self.clock)
        await self.scheduler.configure(
            project_id=PROJECT, now=0.0, enabled=True, interval_seconds=300
        )
        # The daemon's service owns the single remote pass: the subject loops
        # run beside the legacy service sources and the schedule pass, whose
        # root mutations the engine guard refuses once the reconciler owns the
        # repository.
        self.service = IntegrationService(
            db,
            self.scheduler,
            self.repair,
            LegacyRulesDisabled(),
            subject_runtime=self.runtime,
            clock=self.clock,
        )

    def load_artifact(self, sha):
        assert sha == self.definition.artifact_sha256()
        return self.definition

    async def session_probe(self, session):
        return session["id"] in self.live_sessions

    async def pool_handoff(self, owner):
        workspace = await self.db.get_workspace(owner["workspace_id"])
        if _git_status(workspace.workspace_path, "symbolic-ref", "-q", "HEAD").returncode == 0:
            return False  # still on a branch: the writer has not let go
        async with self.db.immediate() as conn:
            changed = await conn.execute(
                update(t.integration_branch_owners)
                .where(
                    t.integration_branch_owners.c.id == owner["id"],
                    t.integration_branch_owners.c.fence_token == owner["fence_token"],
                )
                .values(
                    handoff_state="released",
                    session_id=None,
                    workspace_id=None,
                    confirmed_workspace_id=owner["workspace_id"],
                )
            )
        return changed.rowcount == 1

    async def close(self) -> None:
        if self.service is not None:
            await self.service.stop()
            await self.db.close()

    # ------------------------------------------------------------- sources

    async def add_source(self, task_id: str, *, number: int, review="approved", labels=()):
        """A completed feature whose PR a human reviewed at its exact head."""
        branch, head = f"aq/{task_id}", self.heads[task_id]
        async with self.db.immediate() as conn:
            generation = (
                await conn.execute(
                    select(t.projects.c.hierarchical_integration_generation).where(
                        t.projects.c.id == PROJECT
                    )
                )
            ).scalar_one()
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
                    pr_url=f"https://github.com/example/repo/pull/{number}",
                    created_at=1.0,
                    updated_at=1.0,
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
            # The source PR's own trusted CI (the policy requires source CI).
            await conn.execute(
                insert(t.integration_source_ci).values(
                    task_id=task_id,
                    repository_id=REPO,
                    source_base=self.base,
                    source_head=head,
                    generation=0,
                    policy_generation=generation,
                    state="green",
                    evidence={"head_sha": head},
                    observed_at=1.0,
                )
            )
            for label in labels:
                await conn.execute(insert(t.task_labels).values(task_id=task_id, label=label))
        self.forge.open(number, head)

        async def local_origin(repository_id):
            # The review poller's Git transport reads the fixture origin.
            repo = await self.db.get_repo(repository_id)
            repo.url = str(self.origin)
            return repo

        reviews = ReviewEvidenceProducer(
            self.db,
            PromotionService(
                self.db,
                data_dir=self.tmp_path / "review-data",
                git_manager=GitManager(),
                repository_resolver=local_origin,
            ),
            clock=self.clock,
        )
        if review is not None:
            assert await reviews.snapshot_from_pull_request(
                task_id, verdict=review, reviewer_login="human", reviewed_sha=head
            )

    async def cutover(self) -> Subject:
        """Legacy seals the first batch, shadow observes it, the operator transfers."""
        due = await self.scheduler.mark_due(PROJECT, 1300.0, "periodic")
        assert due["outcome"] == "due"
        self.clock.now = 1301.0
        sealed = await TrainService(self.db).seal(PROJECT, due["request_id"], 1301.0)
        assert sealed["outcome"] == "sealed"
        await self.tick(1)
        [subject] = await self.subjects()
        # Shadow seeded and journaled a legacy mirror without mutating anything.
        assert subject.engine.value == "legacy" and subject.batch_id == sealed["batch_id"]
        shadow = await self.journal(subject.id)
        assert shadow and {row["mode"] for row in shadow} == {"shadow"}
        assert {row["entry_kind"] for row in shadow} == {"decision"}
        assert self.commands == []
        await RootEngineOwnership(self.db, clock=self.clock).transfer(
            REPO,
            engine="reconciler",
            expected_versions={subject.id: subject.version},
            reason="approved root cutover",
            evidence=("shadow-week", "root-scenarios", "operator-approval"),
            operator_id="human:local-operator",
        )
        return await self.subject(subject.id)

    # ---------------------------------------------------------- reconciler

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

    async def subject(self, subject_id: str) -> Subject:
        return Subject.from_row(await self.db.get_integration_subject(subject_id))

    async def journal(self, subject_id: str) -> list[dict]:
        return await self.db.list_integration_subject_journal(subject_id)

    async def tick(self, seconds: float = 61.0) -> None:
        self.clock.now += seconds
        await self.service.tick(self.clock.now)

    async def run_until(self, predicate, *, label: str, limit: int = 60) -> None:
        for _ in range(limit):
            if await predicate():
                return
            await self.tick()
        raise AssertionError(f"reconciler did not reach: {label}\n{await self.describe()}")

    async def phase(self, subject_id: str, phase: SubjectPhase) -> bool:
        return (await self.subject(subject_id)).phase is phase

    async def describe(self) -> str:
        lines = []
        for subject in await self.subjects():
            facts = await self.observer.observe(subject)
            lines.append(
                f"{subject.id} {subject.engine.value} {subject.phase.value} "
                f"{subject.schedule.wait_reason} unknown={facts.unknown} holds={facts.holds}"
            )
            for row in (await self.journal(subject.id))[-10:]:
                result = (row["payload"].get("result") or {}).get("reason")
                lines.append(
                    f"  {row['mode']} {row['entry_kind']} {row['primitive']} "
                    f"{row.get('rule')} {row.get('outcome')} {result or ''}"
                )
        return "\n".join(lines)

    # ------------------------------------------------------- repair writers

    async def operation(self, batch_id: str) -> dict:
        async with self.db._engine.connect() as conn:
            return dict(
                (
                    await conn.execute(
                        select(t.integration_repair_operations).where(
                            t.integration_repair_operations.c.batch_id == batch_id
                        )
                    )
                )
                .mappings()
                .one()
            )

    async def stage(self, operation_id: str, ordinal: int) -> dict:
        async with self.db._engine.connect() as conn:
            return dict(
                (
                    await conn.execute(
                        select(t.integration_repair_stages).where(
                            t.integration_repair_stages.c.operation_id == operation_id,
                            t.integration_repair_stages.c.ordinal == ordinal,
                        )
                    )
                )
                .mappings()
                .one()
            )

    async def claim(self, task_id: str, batch: dict) -> Writer:
        """A pool worker claims the filed writer and attaches to its fenced ref."""
        path = self.tmp_path / f"writer-{task_id}"
        _git(self.tmp_path, "clone", str(self.origin), str(path))
        _git(path, "config", "user.name", "Repair Writer")
        _git(path, "config", "user.email", "writer@example.test")
        session_id, token, agent = f"session-{task_id}", f"token-{task_id}", f"agent-{task_id}"
        now = self.clock.now
        async with self.db.immediate() as conn:
            await conn.execute(
                insert(t.agents).values(
                    id=agent,
                    name=agent,
                    profile_id="standard-high-claude",
                    state="BUSY",
                    created_at=now,
                )
            )
            await conn.execute(
                update(t.tasks)
                .where(t.tasks.c.id == task_id)
                .values(
                    status="IN_PROGRESS", claim_epoch=1, assigned_agent_id=agent, updated_at=now
                )
            )
            await conn.execute(
                insert(t.workspaces).values(
                    id=f"ws-{task_id}",
                    project_id=PROJECT,
                    workspace_path=str(path),
                    source_type="link",
                    locked_by_task_id=task_id,
                    locked_by_agent_id=agent,
                    locked_at=now,
                    enabled=True,
                    created_at=now,
                )
            )
        await self.db.create_session(
            SessionRecord(
                id=session_id,
                task_id=task_id,
                project_id=PROJECT,
                profile_id="standard-high-claude",
                harness="fake",
                provider="fake",
                name=session_id,
                lifecycle="task",
                state="running",
                work_dir=str(path),
                epoch="scenario",
                instance_token=token,
                started_at=now,
            )
        )
        async with self.db.immediate() as conn:
            await conn.execute(
                update(t.sessions)
                .where(t.sessions.c.id == session_id)
                .values(
                    agent_id=agent, last_claim_epoch=1, claim_phase="active", claim_phase_at=now
                )
            )
        self.live_sessions.add(session_id)
        target = BranchKey(repository_id=REPO, branch=batch["integration_branch"])
        owner = await self.writer_ownership.get_owner(target)
        fence = Fence(target=target, owner_id=task_id, token=owner["fence_token"])
        await self.writer_ownership.attach(
            fence, session_id, f"ws-{task_id}", expected_role="repair"
        )
        return Writer(self, task_id, session_id, token, path, fence)


class Writer:
    """One claimed repair writer: its checkout, session principal and fence."""

    def __init__(self, train, task_id, session_id, token, path, fence):
        self.train, self.task_id, self.session_id = train, task_id, session_id
        self.token, self.path, self.fence = token, path, fence

    def principal(self) -> ExecutionPrincipal:
        return ExecutionPrincipal(
            kind=PrincipalKind.SESSION,
            policy=CapabilityPolicy.from_namespaces(aq_commands=[]),
            session_id=self.session_id,
            session_instance_token=self.token,
            task_id=self.task_id,
            project_id=PROJECT,
            profile_id="standard-high-claude",
        )

    def checkout(self, ref: str) -> str:
        _git(self.path, "fetch", str(self.train.origin), ref)
        _git(self.path, "switch", "--detach", "FETCH_HEAD")
        return _git(self.path, "rev-parse", "HEAD")

    def commit(self, message: str, files: dict[str, str]) -> str:
        for path, text in files.items():
            (self.path / path).write_text(text)
        _git(self.path, "add", "-A")
        _git(self.path, "commit", "-m", message)
        return _git(self.path, "rev-parse", "HEAD")

    async def publish_resolution(self, batch, *, member_ordinal, operation_id, partial):
        """The writer's own resolve command: reserve, fenced push, accept."""
        candidate = self.train.writer_candidates
        resolved = _git(self.path, "rev-parse", "HEAD")
        with principal_context(self.principal()):
            reservation = await candidate.reserve_repair(
                CandidateResolutionInput(
                    batch_id=batch["id"],
                    revision=0,
                    member_ordinal=member_ordinal,
                    operation_id=operation_id,
                    resolved_head_sha=resolved,
                    resolved_tree_sha=_git(self.path, "rev-parse", "HEAD^{tree}"),
                    repair_commit_shas=tuple(
                        _git(
                            self.path,
                            "rev-list",
                            "--first-parent",
                            "--reverse",
                            f"{partial}..{resolved}",
                        ).split()
                    ),
                    fence=self.fence,
                )
            )
            await candidate.push_repair(reservation, self.fence)
            accepted = await candidate.accept_repair(reservation)
        return resolved, accepted

    async def close_accepted(self, commit: str):
        """The accepted writer's task close, then its session ends."""
        with principal_context(self.principal()):
            closed = await complete_accepted_delegate(
                self.train.db,
                self.task_id,
                session_id=self.session_id,
                claim_epoch=1,
                commit=commit,
            )
        await self.stop()
        return closed

    async def close_attached(self, head_sha: str, *, base_sha: str, operation_id: str, stage: int):
        """The attached writer's close: exact fenced completion, then its handoff."""
        train = self.train
        closed = await train.repair.complete_delegate(
            self.task_id,
            operation_id=operation_id,
            stage=stage,
            session_id=self.session_id,
            instance_token=self.token,
            workspace_id=f"ws-{self.task_id}",
            fence_token=self.fence.token,
            head_sha=head_sha,
            commit_proof={"base_sha": base_sha, "head_sha": head_sha, "commits": [head_sha]},
            now=train.clock.now,
        )
        await train.writer_ownership.transfer(self.fence, operation_id, "collector")
        await self.stop()
        return closed

    async def stop(self) -> None:
        train = self.train
        train.live_sessions.discard(self.session_id)
        await train.db.update_session(self.session_id, state="stopped", desired_state="stopped")
        async with train.db.immediate() as conn:
            await conn.execute(
                update(t.workspaces)
                .where(t.workspaces.c.locked_by_task_id == self.task_id)
                .values(locked_by_task_id=None, locked_by_agent_id=None, locked_at=None)
            )


# ----------------------------------------------------------------- invariants


async def assert_reconciler_only(train: Train) -> None:
    """Zero operator commands: every command is a journalled reconciler decision."""
    assert train.commands, "the reconciler ran no command"
    assert {name for name, _args, _result in train.commands} <= ADAPTER_COMMANDS
    for subject in await train.subjects():
        journal = await train.journal(subject.id)
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
        publications = [row for row in journal if row["primitive"] == "git_publish"]
        for row in publications:
            if row["outcome"] == "published":
                # Journal-before-write: the fenced prewrite of the same visit.
                [prewrite] = [
                    entry
                    for entry in publications
                    if entry["outcome"] == "prewrite"
                    and entry["subject_version"] == row["subject_version"]
                ]
                assert prewrite["seq"] < row["seq"]
                fence = prewrite["payload"]["fence"]
                assert fence["owner_id"] == f"root-reconciler:{REPO}"
                assert fence["target"] == {"repository_id": REPO, "branch": "refs/heads/main"}
                assert prewrite["payload"]["require_green"] is True


async def assert_main_held_only_exact_green(train: Train) -> list[str]:
    """Main moved only to exact candidates with conclusive trusted green evidence."""
    history = train.main_history()
    assert history[0] == train.base
    async with train.db._engine.connect() as conn:
        for head in history[1:]:
            revision = (
                (
                    await conn.execute(
                        select(t.integration_candidate_revisions).where(
                            t.integration_candidate_revisions.c.head_sha == head,
                            t.integration_candidate_revisions.c.ci_evidence_id.is_not(None),
                        )
                    )
                )
                .mappings()
                .one()
            )
            evidence = (
                (
                    await conn.execute(
                        select(t.integration_check_evidence).where(
                            t.integration_check_evidence.c.id == revision["ci_evidence_id"]
                        )
                    )
                )
                .mappings()
                .one()
            )
            assert evidence["batch_id"] == revision["batch_id"]
            assert evidence["candidate_revision"] == revision["revision"]
            assert (evidence["conclusion"], evidence["classification"]) == ("success", "conclusive")
            assert evidence["producer_id"] == REQUIRED["producer_id"]
            assert evidence["required_check_version"] == REQUIRED["version"]
            intent = (
                (
                    await conn.execute(
                        select(t.integration_promotion_intents).where(
                            t.integration_promotion_intents.c.root_batch_id == revision["batch_id"],
                            t.integration_promotion_intents.c.state == "committed",
                        )
                    )
                )
                .mappings()
                .one()
            )
            assert intent["prepared_sha"] == head
            assert history[history.index(head) - 1] == intent["expected_target"]
    return history


async def assert_preserved(train: Train, task_id: str, number: int) -> None:
    """A source the train did not deliver keeps its branch, PR and evidence."""
    assert train.remote(f"refs/heads/aq/{task_id}") == train.heads[task_id]
    assert f"aq/{task_id}" not in train.git.deleted
    assert train.forge.prs[number]["state"] == "open"
    assert not train.contains(train.heads[task_id], train.remote("refs/heads/main"))
    async with train.db._engine.connect() as conn:
        receipts = (
            await conn.execute(
                select(t.task_delivery_receipts.c.id).where(
                    t.task_delivery_receipts.c.source_task_id == task_id
                )
            )
        ).all()
        task = (
            await conn.execute(select(t.tasks.c.status).where(t.tasks.c.id == task_id))
        ).scalar_one()
    assert receipts == [] and task == "COMPLETED"


def _alembic_heads(train: Train, sha: str) -> list[str]:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    checkout = train.tmp_path / f"verify-{sha[:12]}"
    _git(train.tmp_path, "clone", "--quiet", str(train.origin), str(checkout))
    _git(checkout, "switch", "--quiet", "--detach", sha)
    config = Config()
    config.set_main_option("script_location", str(checkout / "migrations"))
    return ScriptDirectory.from_config(config).get_heads()


# --------------------------------------------------------------------- tests


@pytest.fixture
async def train(tmp_path):
    world = Train(tmp_path)
    yield world
    await world.close()


async def test_reconciler_repairs_conflicts_and_migrations_through_red_green_promotion_and_next_batch(
    train,
):
    await train.open()
    for name in ("alpha", "bravo", "charlie"):
        train.source(name, {"base.txt": f"{name}\n", f"migrations/versions/{name}.py": COLLIDING})
    train.source("held", {"held.txt": "held\n"})
    train.source("rejected", {"rejected.txt": "rejected\n"})
    for number, name in enumerate(("alpha", "bravo", "charlie"), start=1):
        await train.add_source(name, number=number)
    await train.add_source("held", number=4, labels=("hold:operator",))
    await train.add_source("rejected", number=5, review="rejected")
    subject = await train.cutover()
    batch = await train.db.get_integration_batch(subject.batch_id)
    async with train.db._engine.connect() as conn:
        sealed = (
            (
                await conn.execute(
                    select(t.integration_batch_members.c.task_id)
                    .where(t.integration_batch_members.c.batch_id == batch["id"])
                    .order_by(t.integration_batch_members.c.ordinal)
                )
            )
            .scalars()
            .all()
        )
    assert sealed == ["alpha", "bravo", "charlie"]

    # Construction: alpha applies, bravo and charlie both conflict with it.
    async def conflict_writer_filed():
        current = await train.subject(subject.id)
        return current.phase is SubjectPhase.REPAIRING and current.writer.status.value == "filed"

    await train.run_until(conflict_writer_filed, label="batch conflict writer filed")
    operation = await train.operation(batch["id"])
    first = await train.stage(operation["id"], 0)
    writer = await train.claim(first["repair_task_id"], batch)
    await train.tick()
    claimed = await train.observer.observe(await train.subject(subject.id))
    assert claimed.writer.status is WriterStatus.CLAIMED
    assert claimed.writer.last_push_at is None and claimed.budget.attempts == 0
    partial = writer.checkout(batch["integration_branch"])
    for name in ("bravo", "charlie"):
        merged = _git_status(writer.path, "merge", "--no-ff", "--no-edit", train.heads[name])
        assert merged.returncode == 1, merged.stdout
        writer.commit(f"resolve batch member {name}", {"base.txt": "alpha, bravo and charlie\n"})
    # All three sources used revision a00000000002: keep every migration and
    # re-chain them into one ordered head.
    writer.commit(
        "re-chain the colliding batch migrations",
        {
            "migrations/versions/bravo.py": "revision='a00000000003'\ndown_revision='a00000000002'\n",
            "migrations/versions/charlie.py": "revision='a00000000004'\ndown_revision='a00000000003'\n",
        },
    )
    async with train.db._engine.connect() as conn:
        conflicts = (
            (
                await conn.execute(
                    select(t.integration_candidate_member_results).where(
                        t.integration_candidate_member_results.c.batch_id == batch["id"],
                        t.integration_candidate_member_results.c.result == "conflict",
                    )
                )
            )
            .mappings()
            .all()
        )
    assert [row["member_ordinal"] for row in conflicts] == [1]
    resolved, accepted = await writer.publish_resolution(
        batch, member_ordinal=1, operation_id=operation["id"], partial=partial
    )
    assert accepted.outcome == "accepted"

    # The accepted handoff is the writer letting go: the reconciler rebuilds
    # with it, mirroring the repaired candidate identity, then the writer's
    # own close is accepted and its session ends.
    await train.run_until(lambda: train.phase(subject.id, SubjectPhase.TESTING), label="rebuilt")
    assert await writer.close_accepted(resolved) is not None
    red = await train.subject(subject.id)
    assert red.generation == 0 and red.head_sha == train.remote(batch["integration_branch"])
    assert train.contains(resolved, red.head_sha)

    # Red candidate CI: the reconciler files the successor writer itself.
    train.ci.finish(red.head_sha, "failure", run=21)

    async def successor_filed():
        current = await train.subject(subject.id)
        operation = await train.operation(batch["id"])
        return operation["active_stage"] == 1 and current.writer.status.value == "filed"

    await train.run_until(successor_filed, label="CI repair writer filed")
    second = await train.stage(operation["id"], 1)
    assert second["repair_task_id"] != first["repair_task_id"]
    ci_writer = await train.claim(second["repair_task_id"], batch)
    await train.tick()
    ci_writer.checkout(batch["integration_branch"])
    repaired = ci_writer.commit("repair the red candidate in place", {"fix.txt": "fixed\n"})
    _git(ci_writer.path, "push", str(train.origin), "HEAD:" + batch["integration_branch"])
    await train.tick()
    closed = await ci_writer.close_attached(
        repaired, base_sha=red.head_sha, operation_id=operation["id"], stage=1
    )
    assert closed["outcome"] == "completed"

    async def repaired_built():
        current = await train.subject(subject.id)
        return current.phase is SubjectPhase.TESTING and current.generation == 1

    await train.run_until(repaired_built, label="closed writer's repair rebuilt")
    green = await train.subject(subject.id)
    assert green.head_sha == repaired
    train.ci.finish(green.head_sha, "success", run=22)
    await train.run_until(lambda: train.phase(subject.id, SubjectPhase.DONE), label="published")

    assert train.remote("refs/heads/main") == repaired
    assert _alembic_heads(train, repaired) == ["a00000000004"]
    for name in ("alpha", "bravo", "charlie"):
        assert train.contains(train.heads[name], repaired)
    assert red.head_sha not in train.main_history()
    async with train.db._engine.connect() as conn:
        receipts = (
            (
                await conn.execute(
                    select(t.task_delivery_receipts).where(
                        t.task_delivery_receipts.c.batch_id == batch["id"]
                    )
                )
            )
            .mappings()
            .all()
        )
        evidence = (
            (
                await conn.execute(
                    select(t.integration_check_evidence).where(
                        t.integration_check_evidence.c.batch_id == batch["id"]
                    )
                )
            )
            .mappings()
            .all()
        )
    assert {(row["source_task_id"], row["reviewed_head_sha"]) for row in receipts} == {
        (name, train.heads[name]) for name in ("alpha", "bravo", "charlie")
    }
    assert {row["target_branch"] for row in receipts} == {"refs/heads/main"}
    assert any(
        row["conclusion"] == "failure" and row["candidate_revision"] == 0 for row in evidence
    )
    batch = await train.db.get_integration_batch(batch["id"])
    assert batch["cleanup_state"] == "complete" and batch["final_main_sha"] in {None, repaired}
    assert {"aq/alpha", "aq/bravo", "aq/charlie"} <= set(train.git.deleted)
    assert batch["integration_branch"].removeprefix("refs/heads/") in train.git.deleted
    assert {1, 2, 3} <= set(train.forge.closed)
    outcomes = [
        (row["primitive"], row["outcome"])
        for row in await train.journal(subject.id)
        if row["entry_kind"] == "action"
    ]
    assert outcomes.index(("ci_observe", "red")) < outcomes.index(("ci_observe", "green"))
    assert ("writer_file", "exists") in outcomes and ("git_publish", "published") in outcomes

    # Newly approved work arrives; the released request seeds the next
    # reconciler subject, which seals and builds it on the promoted main.
    train.source("future", {"future.txt": "next batch\n"})
    await train.add_source("future", number=10, review=None)
    # Green GitHub observation precedes the legacy source-CI poller's DB record.
    async with train.db.immediate() as conn:
        await conn.execute(delete(t.integration_source_ci).where(
            t.integration_source_ci.c.task_id == "future",
        ))

    async def next_batch_testing():
        later = [s for s in await train.subjects() if s.id != subject.id and s.batch_id]
        live = [s for s in later if s.phase is not SubjectPhase.DONE]
        return bool(live) and live[-1].phase is SubjectPhase.TESTING

    await train.run_until(next_batch_testing, label="next batch built", limit=80)
    following = [
        s for s in await train.subjects() if s.batch_id and s.phase is SubjectPhase.TESTING
    ][-1]
    assert following.engine.value == "reconciler"
    next_batch = await train.db.get_integration_batch(following.batch_id)
    async with train.db._engine.connect() as conn:
        members = (
            (
                await conn.execute(
                    select(t.integration_batch_members.c.task_id).where(
                        t.integration_batch_members.c.batch_id == next_batch["id"]
                    )
                )
            )
            .scalars()
            .all()
        )
    assert members == ["future"]
    assert train.contains(repaired, following.head_sha)
    assert train.contains(train.heads["future"], following.head_sha)
    train.ci.finish(following.head_sha, "success", run=23)
    await train.run_until(lambda: train.phase(following.id, SubjectPhase.DONE), label="next main")
    assert train.remote("refs/heads/main") == following.head_sha

    history = await assert_main_held_only_exact_green(train)
    assert history == [train.base, repaired, following.head_sha]
    await assert_reconciler_only(train)
    # Explicit human decisions stay binding: the held and rejected sources were
    # never sealed, delivered or cleaned, and their hold/verdict are intact.
    await assert_preserved(train, "held", 4)
    await assert_preserved(train, "rejected", 5)
    async with train.db._engine.connect() as conn:
        labels = (
            (
                await conn.execute(
                    select(t.task_labels.c.label).where(t.task_labels.c.task_id == "held")
                )
            )
            .scalars()
            .all()
        )
        verdict = (
            (
                await conn.execute(
                    select(t.integration_review_evidence.c.verdict).where(
                        t.integration_review_evidence.c.source_task_id == "rejected"
                    )
                )
            )
            .scalars()
            .all()
        )
        members = (
            await conn.execute(
                select(t.integration_batch_members.c.task_id).where(
                    t.integration_batch_members.c.task_id.in_(["held", "rejected"])
                )
            )
        ).all()
    assert labels == ["hold:operator"] and verdict == ["rejected"] and members == []


def eject_unclaimed(definition):
    """A project variant authored as reviewed Markdown: same compiler, no Python.

    Where the shipped table waits for capacity, this policy ejects the earliest
    conflicting member once an unclaimed writer has spent its budget.
    """
    source = (BUNDLE / "source.md").read_text()
    block = re.search(r"^```integration-policy\s*\n(.*?)^```\s*$", source, re.MULTILINE | re.DOTALL)
    table = json.loads(block.group(1))
    root = table["tables"]["root_batch"]
    case = next(case for case in root["cases"] if case["rule"] == "writer-unclaimed-expired")
    case["action"] = "eject-unclaimed"
    case["when"]["operands"].append(
        {
            "type": "exists",
            "value": {"type": "binding_ref", "binding": "s", "path": "conflict_member"},
            "mode": "truthy",
        }
    )
    del root["actions"]["capacity"]
    root["actions"]["eject-unclaimed"] = {
        "primitive": "eject",
        "inputs": {
            "member_task_id": {"type": "binding_ref", "binding": "s", "path": "conflict_member"},
            "reason": {"type": "literal", "value": "writer-unclaimed-after-budget"},
        },
        "outcomes": {
            "ejected": {"kind": "progress", "phase": "building"},
            "not_a_member": {"kind": "backoff", "seconds": 30, "ceiling_seconds": 3600},
            "unknown": {"kind": "backoff", "seconds": 30, "ceiling_seconds": 3600},
        },
    }
    variant = source[: block.start(1)] + json.dumps(table, indent=1) + "\n" + source[block.end(1) :]
    return definition.model_copy(update={"integration_policy": policy_from_markdown(variant)})


async def test_never_claimed_writer_reaches_main_by_policy_ejection_within_budget(train):
    await train.open(policy_variant=eject_unclaimed)
    shipped = load_definition_json((BUNDLE / "artifact.json").read_text())
    assert train.definition.artifact_sha256() != shipped.artifact_sha256()
    for name in ("alpha", "bravo", "charlie"):
        train.source(name, {"base.txt": f"{name}\n", f"migrations/versions/{name}.py": COLLIDING})
    for number, name in enumerate(("alpha", "bravo", "charlie"), start=1):
        await train.add_source(name, number=number)
    subject = await train.cutover()
    assert subject.policy.artifact_sha256 == train.definition.artifact_sha256()

    async def built():
        current = await train.subject(subject.id)
        return current.phase is SubjectPhase.TESTING

    await train.run_until(built, label="built without the unrepaired members", limit=80)
    current = await train.subject(subject.id)
    train.ci.finish(current.head_sha, "success", run=31)
    await train.run_until(lambda: train.phase(subject.id, SubjectPhase.DONE), label="published")

    main = train.remote("refs/heads/main")
    assert main == current.head_sha and train.contains(train.heads["alpha"], main)
    assert _alembic_heads(train, main) == ["a00000000002"]
    journal = await train.journal(subject.id)
    ejections = [
        row for row in journal if row["primitive"] == "eject" and row["entry_kind"] == "action"
    ]
    assert [row["outcome"] for row in ejections] == ["ejected", "ejected"]
    decided = {
        row["subject_version"]: row["payload"]["decision"]["request"]["member_task_id"]
        for row in journal
        if row["primitive"] == "eject" and row["entry_kind"] == "decision"
    }
    assert sorted(decided.values()) == ["bravo", "charlie"]
    operation = await train.operation(subject.batch_id)
    stage = await train.stage(operation["id"], 0)
    # The writer was never claimed: queue time waited out the budget, then the
    # table's ejection line ran, and main moved within the same budget window.
    first_conflict = min(
        row["recorded_at"]
        for row in journal
        if row["primitive"] == "git_merge_members" and row["outcome"] == "conflict"
    )
    published = next(row for row in journal if row["outcome"] == "published")
    assert min(row["recorded_at"] for row in ejections) >= first_conflict + PRIMARY_SECONDS
    assert published["recorded_at"] - first_conflict <= 2 * PRIMARY_SECONDS
    assert stage["deadline_at"] - stage["started_at"] == PRIMARY_SECONDS
    async with train.db._engine.connect() as conn:
        sessions = (
            await conn.execute(select(t.sessions.c.id).where(t.sessions.c.project_id == PROJECT))
        ).all()
        events = (
            (
                await conn.execute(
                    select(t.events.c.task_id, t.events.c.payload).where(
                        t.events.c.event_type == "integration.batch_ejected"
                    )
                )
            )
            .mappings()
            .all()
        )
    assert sessions == []
    assert sorted(row["task_id"] for row in events) == ["bravo", "charlie"]
    assert all(
        json.loads(row["payload"])["reason"] == "writer-unclaimed-after-budget" for row in events
    )
    for event in events:
        audit = json.loads(event["payload"])
        decision = next(
            row for row in journal
            if row["entry_kind"] == "decision" and row["primitive"] == "eject"
            and row["payload"]["decision"]["request"]["member_task_id"] == event["task_id"]
        )
        assert audit["operator_id"] == "service:root-reconciler"
        assert audit["policy_decision"] == {
            "subject_id": subject.id,
            "subject_version": decision["subject_version"],
            "rule": decision["rule"],
            "policy_artifact_sha256": subject.policy.artifact_sha256,
            "playbook_id": subject.policy.playbook_id,
            "decision_seq": decision["seq"],
            "facts_digest": decision["facts_digest"],
        }
    await assert_main_held_only_exact_green(train)
    await assert_reconciler_only(train)
    # Ejection is not rejection: both members keep their branch, PR and review.
    await assert_preserved(train, "bravo", 2)
    await assert_preserved(train, "charlie", 3)


async def test_live_green_candidate_promotes_without_prior_ci_evidence_or_operator_action(train):
    from src.integration.subjects import CIEvidence, CIState

    await train.open()
    train.source("alpha", {"alpha.txt": "alpha\n"})
    await train.add_source("alpha", number=1)
    subject = await train.cutover()
    await train.run_until(lambda: train.phase(subject.id, SubjectPhase.TESTING), label="built")
    current = await train.subject(subject.id)
    async with train.db._engine.connect() as conn:
        evidence = (await conn.execute(select(t.integration_check_evidence).where(
            t.integration_check_evidence.c.batch_id == subject.batch_id
        ))).all()
    assert not evidence
    train.ci.finish(current.head_sha, "success", run=66)

    async def live(snapshot, head):
        observation = train.ci.runs.get(head.sha)
        return CIEvidence(head_sha=head.sha, observed_at=train.clock(),
                          state=CIState.GREEN if observation else CIState.NONE)

    train.observer.candidate_ci = live
    facts = await train.observer.observe(current)
    assert facts.ci_state is CIState.GREEN and facts.ci[0].evidence_id is None
    await train.run_until(lambda: train.phase(subject.id, SubjectPhase.DONE), label="published")
    assert train.remote("refs/heads/main") == current.head_sha
    journal = await train.journal(subject.id)
    assert any(row.get("rule") == "promote-exact-green" for row in journal)
    await assert_reconciler_only(train)
