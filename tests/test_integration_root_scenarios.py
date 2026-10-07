"""Rev-2 section-6 root acceptance: the reconciler drives a real Git/PostgreSQL train.

Every train mutation is a reconciler decision that runs one existing command
through the root primitive adapters on the daemon's ``IntegrationService``
subject pass. The tests play only the outside world: repair workers that
claim, push and close (or never claim), the trusted CI producer, and newly
approved work. No operator, recovery or legacy-rule command runs.
"""

from __future__ import annotations

import itertools
import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, insert, select, update

from src.commands.handler import CommandHandler
from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.config import AppConfig, DatabaseConfig, DiscordConfig
from src.database import Database
from src.database import tables as t
from src.git.github_app import GitHubRepositoryBinding
from src.git.manager import GitManager
from src.integration.promotion_contracts import AuditPullRequest
from src.integration.candidates import CandidateService
from src.integration.ci import (
    CandidateCISubject,
    CIReceiptPayload,
    CIService,
    FailedCIObservation,
    IntegrationCITrust,
    TrustedCIObservation,
    TrustedFixtureObserver,
    failed_run_verdict,
)
from src.integration.cleanup import IntegrationCleanupService
from src.integration.engine import root_engine_guard
from src.integration.promotion_contracts import RootAttestationProof
from src.integration.main_promotion import RootPromotionService
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
from src.integration.runtime_contracts import (
    PrimitivePorts,
    RemoteHead,
    Subject,
    SubjectPhase,
    WriterStatus,
)
from src.models import Project, RepoConfig, RepoSourceType, SessionRecord, TaskCompletion
from src.orchestrator import Orchestrator
from src.playbooks.definition import load_definition_json
from src.playbooks.integration_policy import IntegrationPolicyFacts, policy_from_markdown
from src.profiles.capabilities import CapabilityPolicy
from tests.db_fixtures import lease_dsn
from tests.test_integration_candidates import _AppClient, _LocalPushGit
from tests.test_integration_git_truth import repository as git_first_repository  # noqa: F401

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


@pytest.fixture
async def git_first_case(git_first_repository):  # noqa: F811 - imported pytest fixture
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from src.integration.observe import ObservationRows
    from src.integration.parent_adapters import ParentPolicyFacts
    from src.integration.parent_subjects import ParentChildFacts
    from src.integration.shadow import GitFirstDiagnostics
    from src.integration.runtime_contracts import (
        CIEvidence,
        CIState,
        MemberFacts,
        PolicyArtifactPin,
        SubjectEngine,
        SubjectKind,
        SubjectSchedule,
        WriterLease,
    )

    repo = git_first_repository
    head = await repo.commit("child")
    request = await repo.retain(head)
    await repo.publish()
    subject = Subject(
        id="diagnostic", project_id="p", repository_id="r", task_id="epic",
        kind=SubjectKind.PARENT_EPISODE, subject_key="parent_episode:r:epic:0",
        parent_episode_id="episode", engine=SubjectEngine.RECONCILER,
        phase=SubjectPhase.TESTING,
        policy=PolicyArtifactPin(playbook_id="fixture", artifact_sha256="sha256:" + "1" * 64),
        target_ref="refs/heads/main", head_sha=head,
        schedule=SubjectSchedule.progress(now=10, max_wait_seconds=60),
        created_at=10, updated_at=10,
    )
    facts = ParentPolicyFacts(
        subject_id=subject.id, subject_version=0, kind=subject.kind, phase=subject.phase,
        observed_at=10, head=subject.head, readiness="waiting", no_progress=True,
        children=(ParentChildFacts(task_id="task", status="COMPLETED"),),
        members=(MemberFacts(task_id="task", head_sha=head, base_sha=repo.base),),
        writer=WriterLease(status=WriterStatus.WORKING, task_id="repair"),
        ci=(CIEvidence(head_sha=head, state=CIState.GREEN),),
    )
    rows = {
        "tasks": ({"id": "task", "project_id": "p", "repo_id": "r", "updated_at": 1,
                   "legacy_completion_id": "legacy-1", "status": "COMPLETED"},),
        "integration_repair_operations": ({"id": "op", "episode_id": "episode",
            "policy_snapshot": {"parent": {"admission": "authorized"}}},),
        "integration_repair_stages": ({"ordinal": 0, "created_at": 1,
            "repair_task_id": "repair", "starting_sha": head},),
        "integration_branch_owners": ({"repository_id": "r", "ref": "main",
            "owner_id": "dead-writer", "fence_token": 1, "expires_at": 9,
            "handoff_state": "attached"},),
    }
    snapshot = ObservationRows(subject, {}, {
        "default_branch": "main", "checkout_base_path": str(repo.path), "url": str(repo.remote),
    }, rows)
    reader = SimpleNamespace(read=AsyncMock(return_value=snapshot))
    db = SimpleNamespace(get_task_completion=AsyncMock(return_value=TaskCompletion(
        id=request.completion_id, task_id="task", outcome="pass", completed_at=1, commits=[head],
    )))
    return SimpleNamespace(repo=repo, subject=subject, facts=facts, snapshot=snapshot,
                           reader=reader, diagnostics=GitFirstDiagnostics(db, repo.git, reader))


@pytest.mark.parametrize("agreement", [False, True])
async def test_git_first_shadow_four_families_use_fetched_inputs(git_first_case, caplog, agreement):
    from unittest.mock import AsyncMock
    from src.integration.parent_subjects import ParentChildFacts, ParentVerificationFacts

    case = git_first_case
    facts = case.facts
    if agreement:
        facts = facts.model_copy(update={
            "readiness": "ready", "no_progress": False,
            "children": (ParentChildFacts(task_id="task", status="COMPLETED",
                                          selected_receipt_id="receipt"),),
            "verification": ParentVerificationFacts(episode_id="episode", operation_id="op",
                                                     generation=0, head_sha=case.subject.head_sha,
                                                     status="passed"),
        })
        case.snapshot.rows["integration_branch_owners"][0]["handoff_state"] = "released"
    before = facts.model_dump()
    before_refs = await case.repo.git._arun(["show-ref"], cwd=str(case.repo.remote))
    case.repo.git.afetch_origin = AsyncMock(wraps=case.repo.git.afetch_origin)
    caplog.set_level("INFO", logger="src.integration.shadow")
    await case.diagnostics(case.subject, facts)
    evidence = {record.git_first["family"]: record.git_first for record in caplog.records
                if hasattr(record, "git_first")}
    assert set(evidence) == {"child_delivery", "epic_readiness", "repair_progress", "lease_eligibility"}
    expected = "agreement" if agreement else "disagreement"
    assert {row["classification"] for row in evidence.values()} == {expected}
    assert {row["proposed"] for row in evidence.values()} == {True}
    assert evidence["repair_progress"]["start_oid"] == evidence["repair_progress"]["head_oid"]
    assert evidence["repair_progress"]["green"]  # green ends an unchanged-head repair
    assert evidence["lease_eligibility"]["input_kind"] == "non_git"
    assert facts.model_dump() == before
    assert case.repo.git.afetch_origin.await_count == 1
    assert await case.repo.git._arun(["show-ref"], cwd=str(case.repo.remote)) == before_refs


async def test_git_first_shadow_unavailable_fetch_is_unknown_but_lease_is_non_git(git_first_case, caplog):
    from unittest.mock import AsyncMock
    from src.git.manager import GitError

    case = git_first_case
    case.repo.git.afetch_origin = AsyncMock(side_effect=GitError("origin unavailable"))
    await case.diagnostics(case.subject, case.facts)
    evidence = {record.git_first["family"]: record.git_first for record in caplog.records
                if hasattr(record, "git_first")}
    for family in ("child_delivery", "epic_readiness", "repair_progress"):
        assert evidence[family]["classification"] == "unknown"
        assert evidence[family]["proposed"] is None
    assert evidence["lease_eligibility"]["classification"] == "disagreement"


async def test_git_first_shadow_missing_expiry_and_active_off(git_first_case, caplog):
    from src.config import IntegrationConfig
    from src.integration.shadow import diagnostics_for
    case = git_first_case
    case.snapshot.rows["integration_branch_owners"][0]["expires_at"] = None
    await case.diagnostics(case.subject, case.facts)
    lease = next(record.git_first for record in caplog.records
                 if getattr(record, "git_first", {}).get("family") == "lease_eligibility")
    assert lease["classification"] == "unknown" and lease["proposed"] is None
    assert diagnostics_for(IntegrationConfig(git_first="active"), None, case.repo.git) is None


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

    def finish(self, sha: str, conclusion: str, run: int, *, job_conclusions=None,
               run_conclusion=None) -> None:
        """Finish the exact head's run: ``conclusion`` for the first required
        check and for the run, or the per-job and run conclusions of a real
        shape (a runner outage cancels jobs and concludes the run ``failure``).
        """
        names = list(REQUIRED["names"])
        jobs = list(job_conclusions or (conclusion, *("success",) * (len(names) - 1)))
        checks = [
            {
                "name": name,
                "check_run_id": run * 100 + index,
                "check_suite_id": run,
                "producer_app_id": int(REQUIRED["producer_id"]),
                "producer_id": REQUIRED["producer_id"],
                "head_sha": sha,
                "conclusion": job,
            }
            for index, (name, job) in enumerate(zip(names, jobs))
        ]
        workflow = {
            "workflow_run_id": run,
            "run_attempt": 1,
            "check_suite_id": run,
            "head_sha": sha,
            "conclusion": conclusion if run_conclusion is None else run_conclusion,
        }
        if all(job == "success" for job in jobs) and workflow["conclusion"] == "success":
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
            verdict = failed_run_verdict(checks, [workflow])
            self.runs[sha] = FailedCIObservation(
                checks=tuple(checks),
                workflow_runs=(workflow,),
                workflow_ids={run: run + 1000},
                conclusion="cancelled" if verdict == "cancelled" else "failure",
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
            shadow=False,
            clock=self.clock,
        )
        self.scheduler = IntegrationScheduler(db, clock=self.clock)
        await self.scheduler.configure(
            project_id=PROJECT, now=0.0, enabled=True, interval_seconds=300
        )
        self.service = IntegrationService(
            db, LegacyRulesDisabled(), subject_runtime=self.runtime, clock=self.clock,
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
        """The first active visit seeds and seals a subject without legacy services."""
        due = await self.scheduler.mark_due(PROJECT, 1300.0, "periodic")
        assert due["outcome"] == "due"
        self.clock.now = 1301.0
        await self.tick(1)
        [subject] = await self.subjects()
        assert subject.engine.value == "reconciler" and subject.batch_id is not None
        journal = await self.journal(subject.id)
        assert journal and {row["mode"] for row in journal} == {"active"}
        assert [name for name, _, _ in self.commands] == ["integration_seal"]
        return subject

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
        for name, _args, result in self.commands[-4:]:
            lines.append(f"command {name}: {result}")
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
        """The writer's command derives authority and never continues legacy construction."""
        resolved = _git(self.path, "rev-parse", "HEAD")
        orchestrator = self.train.handler.orchestrator
        previous = orchestrator.integration_candidate_service
        orchestrator.integration_candidate_service = self.train.writer_candidates
        args = {
            "resolved_head_sha": resolved,
            "resolved_tree_sha": _git(self.path, "rev-parse", "HEAD^{tree}"),
            "repair_commit_shas": _git(
                self.path, "rev-list", "--first-parent", "--reverse", f"{partial}..{resolved}"
            ).split(),
            "claim_epoch": 1,
        }
        try:
            with principal_context(self.principal()):
                accepted = await self.train.handler._cmd_integration_resolve_candidate_member(args)
                assert accepted["success"], accepted
                assert accepted["continuation"] is None
                replay = await self.train.handler._cmd_integration_resolve_candidate_member(args)
                assert replay["success"], replay
                assert replay["continuation"] is None
        finally:
            orchestrator.integration_candidate_service = previous
        return resolved, accepted

    async def close_accepted(self, commit: str):
        """The accepted writer closes through the production command."""
        with principal_context(self.principal()):
            closed = await self.train.handler._cmd_task_close({
                "task_id": self.task_id, "session_id": self.session_id, "claim_epoch": 1,
                "commit": commit, "outcome": "pass", "summary": "Accepted candidate repair",
            })
        assert closed["success"], closed
        assert await self.train.db.get_workspace_for_task(self.task_id) is None
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
    """Zero operator commands: every command is an ordinary reconciler adapter call."""
    assert train.commands, "the reconciler ran no command"
    assert {name for name, _args, _result in train.commands} <= ADAPTER_COMMANDS


async def assert_main_held_only_exact_green(train: Train) -> list[str]:
    """Main moved only to exact SHAs the trusted producer reported green.

    Read from the producer's live runs, never from a stored evidence row: each
    promotion is a fast-forward of the head it replaced.
    """
    history = train.main_history()
    assert history[0] == train.base
    for previous, head in itertools.pairwise(history):
        assert isinstance(train.ci.runs.get(head), TrustedCIObservation), head
        assert train.contains(previous, head)
    return history


async def assert_preserved(train: Train, task_id: str, number: int) -> None:
    """A source the train did not deliver keeps its branch, PR and evidence."""
    assert train.remote(f"refs/heads/aq/{task_id}") == train.heads[task_id]
    assert f"aq/{task_id}" not in train.git.deleted
    assert train.forge.prs[number]["state"] == "open"
    assert not train.contains(train.heads[task_id], train.remote("refs/heads/main"))
    async with train.db._engine.connect() as conn:
        task = (
            await conn.execute(select(t.tasks.c.status).where(t.tasks.c.id == task_id))
        ).scalar_one()
    assert task == "COMPLETED"


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
    resolved, accepted = await writer.publish_resolution(
        batch, member_ordinal=1, operation_id=operation["id"], partial=partial
    )
    assert accepted["outcome"] == "accepted"

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
    assert {"aq/alpha", "aq/bravo", "aq/charlie"} <= set(train.git.deleted)
    assert batch["integration_branch"].removeprefix("refs/heads/") in train.git.deleted
    assert {1, 2, 3} <= set(train.forge.closed)

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
    assert train.contains(repaired, following.head_sha)
    assert train.contains(train.heads["future"], following.head_sha)
    for name in ("held", "rejected"):
        assert not train.contains(train.heads[name], following.head_sha)
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
    assert labels == ["hold:operator"]


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
    base = train.remote("refs/heads/main")

    async def ejection_events():
        async with train.db._engine.connect() as conn:
            return (
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

    # Observed on the train clock: when the ejection event appears and when
    # main moves, measured from cutover, the earliest the conflict can exist.
    started, seen = train.clock.now, {}

    async def built():
        if "ejected" not in seen and await ejection_events():
            seen["ejected"] = train.clock.now
        current = await train.subject(subject.id)
        return current.phase is SubjectPhase.TESTING

    async def published():
        if train.remote("refs/heads/main") != base:
            seen.setdefault("published", train.clock.now)
        return await train.phase(subject.id, SubjectPhase.DONE)

    await train.run_until(built, label="built without the unrepaired members", limit=80)
    current = await train.subject(subject.id)
    train.ci.finish(current.head_sha, "success", run=31)
    await train.run_until(published, label="published")

    main = train.remote("refs/heads/main")
    assert main == current.head_sha and train.contains(train.heads["alpha"], main)
    assert _alembic_heads(train, main) == ["a00000000002"]
    # The writer was never claimed: queue time waited out the budget, then the
    # table's ejection line ran, and main moved within the same budget window.
    assert seen["ejected"] >= started + PRIMARY_SECONDS
    assert seen["published"] - started <= 2 * PRIMARY_SECONDS
    events = await ejection_events()
    async with train.db._engine.connect() as conn:
        sessions = (
            await conn.execute(select(t.sessions.c.id).where(t.sessions.c.project_id == PROJECT))
        ).all()
    assert sessions == []
    assert sorted(row["task_id"] for row in events) == ["bravo", "charlie"]
    assert all(
        json.loads(row["payload"])["reason"] == "writer-unclaimed-after-budget" for row in events
    )
    await assert_main_held_only_exact_green(train)
    await assert_reconciler_only(train)
    # Ejection is not rejection: both members keep their branch, PR and review.
    await assert_preserved(train, "bravo", 2)
    await assert_preserved(train, "charlie", 3)


async def test_live_green_candidate_promotes_without_prior_ci_evidence_or_operator_action(train):
    from src.integration.runtime_contracts import CIEvidence, CIState

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


async def test_a_stale_cleanup_count_still_releases_the_lease_and_seals_the_next_request(
    train,
):
    """Cleanup progress must never withhold what delivery already earned.

    Batch 288750f6 answered ``invariant_error`` on every tick with all of its
    items complete, so the project lease and the sweep request stayed held and
    no later batch could seal.
    """
    from src.integration.runtime_contracts import CIEvidence, CIState

    await train.open()
    service = train.handler.orchestrator.integration_cleanup_service
    materialize = service.materialize

    async def stale_item_count(batch_id, *, now=None):
        # The incident exactly: a persisted count that disagrees with what
        # materialization found, while every item is already complete.
        result = await materialize(batch_id, now=now)
        return result.model_copy(update={"item_count": result.item_count + 1})

    service.materialize = stale_item_count
    train.source("alpha", {"alpha.txt": "alpha\n"})
    await train.add_source("alpha", number=1)
    subject = await train.cutover()
    await train.run_until(lambda: train.phase(subject.id, SubjectPhase.TESTING), label="built")
    current = await train.subject(subject.id)
    train.ci.finish(current.head_sha, "success", run=66)

    async def live(snapshot, head):
        observation = train.ci.runs.get(head.sha)
        return CIEvidence(
            head_sha=head.sha,
            observed_at=train.clock(),
            state=CIState.GREEN if observation else CIState.NONE,
        )

    train.observer.candidate_ci = live
    await train.run_until(
        lambda: train.phase(subject.id, SubjectPhase.DONE), label="first batch delivered"
    )
    assert train.remote("refs/heads/main") == current.head_sha
    assert train.contains(train.heads["alpha"], current.head_sha)
    outcomes = [
        (row["primitive"], row["outcome"])
        for row in await train.journal(subject.id)
        if row["entry_kind"] == "action"
    ]
    assert ("cleanup", "clean") in outcomes
    batch = await train.db.get_integration_batch(subject.batch_id)
    assert batch["cleanup_state"] == "complete"
    async with train.db._engine.connect() as conn:
        leases = (await conn.execute(select(t.project_integration_leases))).all()
        schedule = (
            (
                await conn.execute(
                    select(t.project_integration_schedules).where(
                        t.project_integration_schedules.c.project_id == PROJECT
                    )
                )
            )
            .mappings()
            .one()
        )
    assert leases == []
    assert schedule["outstanding_request_id"] is None

    # Newly approved work arrives: the released request must be able to seal it.
    train.source("bravo", {"bravo.txt": "next batch\n"})
    await train.add_source("bravo", number=2)
    # Green GitHub observation precedes the legacy source-CI poller's DB record.
    async with train.db.immediate() as conn:
        await conn.execute(
            delete(t.integration_source_ci).where(t.integration_source_ci.c.task_id == "bravo")
        )

    async def next_batch_testing():
        later = [s for s in await train.subjects() if s.id != subject.id and s.batch_id]
        live = [s for s in later if s.phase is not SubjectPhase.DONE]
        return bool(live) and live[-1].phase is SubjectPhase.TESTING

    await train.run_until(next_batch_testing, label="next batch sealed and built", limit=80)
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
    assert members == ["bravo"]
    assert train.contains(train.heads["alpha"], following.head_sha)
    assert train.contains(train.heads["bravo"], following.head_sha)
    train.ci.finish(following.head_sha, "success", run=23)
    await train.run_until(lambda: train.phase(following.id, SubjectPhase.DONE), label="next main")
    assert train.remote("refs/heads/main") == following.head_sha
    await assert_reconciler_only(train)


@pytest.mark.parametrize("outcome", ["pass", "fail"])
@pytest.mark.parametrize("lifecycle", ["task", "pool"])
async def test_reconciler_delegate_task_close_records_stop_and_resumes(train, outcome, lifecycle):
    """Actual task close, without manual owner transfer or workspace unlocking."""
    await train.open()
    train.source("alpha", {"feature.txt": "feature\n"})
    await train.add_source("alpha", number=1)
    subject = await train.cutover()
    await train.run_until(lambda: train.phase(subject.id, SubjectPhase.TESTING), label="built")
    red = await train.subject(subject.id)
    train.ci.finish(red.head_sha, "failure", run=31)

    async def filed():
        return (await train.subject(subject.id)).writer.status is WriterStatus.FILED

    await train.run_until(filed, label="red CI repair filed")
    batch = await train.db.get_integration_batch(subject.batch_id)
    operation = await train.operation(batch["id"])
    stage = await train.stage(operation["id"], 0)
    writer = await train.claim(stage["repair_task_id"], batch)
    await train.db.update_session(writer.session_id, lifecycle=lifecycle)
    writer.checkout(batch["integration_branch"])
    head = red.head_sha
    if outcome == "pass":
        head = writer.commit("repair red candidate", {"fix.txt": "fixed\n"})
        with principal_context(writer.principal()):
            refused = await train.handler._cmd_task_close({
                "task_id": writer.task_id, "session_id": writer.session_id, "claim_epoch": 1,
                "outcome": outcome, "summary": "Unpushed repair", "commit": head,
            })
        assert not refused["success"], refused
        assert (await train.db.get_task(writer.task_id)).status.value == "IN_PROGRESS"
        assert (await train.writer_ownership.get_owner(writer.fence.target))["handoff_state"] == "attached"
        assert (await train.subject(subject.id)).writer.stop_proof is None
        _git(writer.path, "push", str(train.origin), "HEAD:" + batch["integration_branch"])
    with principal_context(writer.principal()):
        stale = await train.handler._cmd_task_close({
            "task_id": writer.task_id, "session_id": writer.session_id, "claim_epoch": 0,
            "outcome": outcome, "summary": "Stale claim", "commit": head,
        })
        assert not stale["success"] and stale["result"] == "stale_claim", stale
        assert (await train.db.get_task(writer.task_id)).status.value == "IN_PROGRESS"
        result = await train.handler._cmd_task_close({
            "task_id": writer.task_id, "session_id": writer.session_id, "claim_epoch": 1,
            "outcome": outcome, "work_outcome": "shipped" if outcome == "pass" else "blocked",
            "summary": "Repaired candidate" if outcome == "pass" else "Cannot repair candidate",
            "commit": head,
        })
    assert result["success"], result
    task = await train.db.get_task(writer.task_id)
    assert task.status.value == ("COMPLETED" if outcome == "pass" else "BLOCKED")
    assert await train.db.get_workspace_for_task(writer.task_id) is None
    owner = await train.writer_ownership.get_owner(writer.fence.target)
    assert owner["handoff_state"] == "released"
    current = await train.subject(subject.id)
    assert current.writer.stop_proof["stop_proof"]["kind"] == "accepted_handoff"
    facts = await train.observer.observe(current)
    assert facts.writer.status is WriterStatus.STOPPED, facts
    assert not any("writer_stop_unproven" in reason for reason in facts.unknown)
    # The process is still alive: relinquished authority, not process death,
    # is the close proof. No test helper stops it or releases its checkout.
    assert writer.session_id in train.live_sessions
    if outcome == "pass":
        async def rebuilt():
            current = await train.subject(subject.id)
            return current.phase is SubjectPhase.TESTING and current.head_sha == head

        await train.run_until(rebuilt, label="new head observed")
        train.ci.finish(head, "success", run=32)
        await train.run_until(lambda: train.phase(subject.id, SubjectPhase.DONE), label="promoted")
        assert train.remote("refs/heads/main") == head
    else:
        async def successor():
            current = await train.subject(subject.id)
            return current.writer.status is WriterStatus.FILED and current.writer.task_id != task.id

        await train.run_until(successor, label="blocked writer replaced", limit=90)


async def test_main_moved_after_build_rebuilds_once_then_red_ci_routes_to_repair(train):
    """Live 2026-10-04 subject d3fb5c5b: main moved under a built candidate.

    ``git_merge_members`` answered ``merged`` with the already-built revision
    on the old base, so ``base-moved`` re-fired every visit and red CI was
    never reached.  Now the candidate is rebuilt onto observed main once.
    """
    from src.integration.runtime_contracts import CIEvidence, CIState

    await train.open()
    train.source("alpha", {"alpha.txt": "alpha\n"})
    await train.add_source("alpha", number=1)
    subject = await train.cutover()
    await train.run_until(lambda: train.phase(subject.id, SubjectPhase.TESTING), label="built")
    stale = await train.subject(subject.id)

    _git(train.work, "switch", "-C", "main", train.base)
    (train.work / "unrelated.txt").write_text("moved\n")
    _git(train.work, "add", "-A")
    _git(train.work, "commit", "-m", "main moved")
    _git(train.work, "push", "origin", "HEAD:refs/heads/main")
    moved = train.remote("refs/heads/main")

    async def reported(snapshot, head):
        observation = train.ci.runs.get(head.sha)
        state = CIState.NONE
        if observation is not None:
            state = CIState.GREEN if hasattr(observation, "receipt") else CIState.RED
        return CIEvidence(head_sha=head.sha, observed_at=train.clock(), state=state)

    train.observer.candidate_ci = reported
    for _ in range(4):
        await train.tick()
    current = await train.subject(subject.id)
    assert current.base_sha == moved
    assert current.head_sha != stale.head_sha
    assert current.generation == stale.generation + 1
    assert not (await train.observer.observe(current)).base_moved
    fired = [row for row in await train.journal(subject.id) if row.get("rule") == "base-moved"]
    assert 0 < len(fired) <= 4

    for _ in range(3):
        await train.tick()
    again = [row for row in await train.journal(subject.id) if row.get("rule") == "base-moved"]
    assert len(again) == len(fired)

    train.ci.finish(current.head_sha, "failure", run=77)
    await train.run_until(
        lambda: train.phase(subject.id, SubjectPhase.REPAIRING), label="red routes to repair"
    )


async def test_a_runner_outage_retries_while_a_failed_job_still_files_repair(train):
    """Live 2026-10-05 batch 68d2f31e: run 37367576038, conclusion failure.

    Twelve required jobs were cancelled because no hosted runner was acquired,
    and the run still concluded ``failure``; only ``Tests (default-7/8)`` had
    really failed. The cancellations must neither hide that failure nor, on a
    run with no failure at all, open a repair stage and spend its budget.
    """
    from src.integration.runtime_contracts import CIState

    async def repair_stages(batch_id):
        async with train.db._engine.connect() as conn:
            return (
                (
                    await conn.execute(
                        select(t.integration_repair_stages)
                        .join(
                            t.integration_repair_operations,
                            t.integration_repair_operations.c.id
                            == t.integration_repair_stages.c.operation_id,
                        )
                        .where(t.integration_repair_operations.c.batch_id == batch_id)
                    )
                )
                .mappings()
                .all()
            )

    await train.open()
    train.source("alpha", {"alpha.txt": "alpha\n"})
    await train.add_source("alpha", number=1)
    subject = await train.cutover()
    await train.run_until(lambda: train.phase(subject.id, SubjectPhase.TESTING), label="built")
    built = await train.subject(subject.id)
    batch = await train.db.get_integration_batch(subject.batch_id)
    names = REQUIRED["names"]

    # The outage's own shape: every cancelled job, no failed job, run failure.
    train.ci.finish(
        built.head_sha, "success", run=41,
        job_conclusions=["success"] * 2 + ["cancelled"] * (len(names) - 2),
        run_conclusion="failure",
    )

    async def observed_infra():
        facts = await train.observer.observe(await train.subject(subject.id))
        return facts.ci_state is CIState.INFRA

    await train.run_until(observed_infra, label="cancelled jobs are infrastructure")

    # Nothing to repair: the candidate is retried on its exact head, and the
    # construction stage is never handed a writer or an attempt.
    current = await train.subject(subject.id)
    assert current.head_sha == built.head_sha
    assert current.phase is SubjectPhase.TESTING
    stages = await repair_stages(subject.batch_id)
    assert [stage["repair_task_id"] for stage in stages] == [None]
    assert [stage["attempts"] for stage in stages] == [0]
    async with train.db._engine.connect() as conn:
        evidence = (
            (
                await conn.execute(
                    select(t.integration_check_evidence).where(
                        t.integration_check_evidence.c.batch_id == subject.batch_id
                    )
                )
            )
            .mappings()
            .all()
        )
    assert {row["conclusion"] for row in evidence} == {"cancelled"}
    assert batch["tested_candidate_sha"] is None

    # The failed shard is conclusive: a real failure outranks its cancellations.
    train.ci.finish(
        built.head_sha, "success", run=42,
        job_conclusions=["failure"] + ["success"] * 3 + ["cancelled"] * (len(names) - 4),
        run_conclusion="failure",
    )

    async def repair_writer_filed():
        current = await train.subject(subject.id)
        return (
            current.phase is SubjectPhase.REPAIRING
            and current.writer.status is WriterStatus.FILED
        )

    await train.run_until(repair_writer_filed, label="failed job files repair")
    filed = [stage for stage in await repair_stages(subject.batch_id) if stage["repair_task_id"]]
    assert len(filed) == 1 and filed[0]["state"] == "active"
    assert train.remote("refs/heads/main") == train.base


async def delegate_after_its_own_promotion(train, lifecycle="pool"):
    """A delegate whose batch reached main while it still holds its claim."""
    await train.open()
    for name in ("alpha", "bravo"):
        train.source(name, {"base.txt": f"{name}\n"})
    for number, name in enumerate(("alpha", "bravo"), start=1):
        await train.add_source(name, number=number)
    subject = await train.cutover()

    async def conflict_filed():
        current = await train.subject(subject.id)
        return current.phase is SubjectPhase.REPAIRING and current.writer.status is WriterStatus.FILED

    await train.run_until(conflict_filed, label="batch conflict writer filed")
    batch = await train.db.get_integration_batch(subject.batch_id)
    operation = await train.operation(batch["id"])
    stage = await train.stage(operation["id"], 0)
    writer = await train.claim(stage["repair_task_id"], batch)
    await train.db.update_session(writer.session_id, lifecycle=lifecycle)
    partial = writer.checkout(batch["integration_branch"])
    merged = _git_status(writer.path, "merge", "--no-ff", "--no-edit", train.heads["bravo"])
    assert merged.returncode == 1, merged.stdout
    resolved = writer.commit("resolve batch member bravo", {"base.txt": "alpha and bravo\n"})
    _git(writer.path, "push", str(train.origin), "HEAD:" + batch["integration_branch"])
    _, accepted = await writer.publish_resolution(
        batch, member_ordinal=1, operation_id=operation["id"], partial=partial
    )
    assert accepted["outcome"] == "accepted"

    # Acceptance already returned the branch to the collector, so the reconciler
    # rebuilds, goes green and promotes with the delegate still holding its claim.
    await train.run_until(lambda: train.phase(subject.id, SubjectPhase.TESTING), label="rebuilt")
    rebuilt = await train.subject(subject.id)
    train.ci.finish(rebuilt.head_sha, "success", run=51)
    await train.run_until(lambda: train.phase(subject.id, SubjectPhase.DONE), label="promoted")
    assert train.remote("refs/heads/main") == rebuilt.head_sha
    assert train.contains(resolved, rebuilt.head_sha)
    assert (await train.db.get_task(writer.task_id)).status.value == "IN_PROGRESS"
    return SimpleNamespace(
        subject=subject, batch=batch, operation=operation, writer=writer,
        resolved=resolved, promoted=rebuilt,
    )


async def close_delegate(train, world, outcome="pass"):
    with principal_context(world.writer.principal()):
        return await train.handler._cmd_task_close({
            "task_id": world.writer.task_id, "session_id": world.writer.session_id,
            "claim_epoch": 1, "commit": world.resolved, "outcome": outcome,
            "work_outcome": "shipped" if outcome == "pass" else "blocked",
            "summary": "Accepted candidate repair",
        })


@pytest.mark.parametrize("lifecycle", ["task", "pool"])
@pytest.mark.parametrize("outcome", ["pass", "fail"])
async def test_delegate_close_after_its_own_batch_promotes_is_delivered(
    train, lifecycle, outcome
):
    """Batch 6718770a: the reconciler promoted before its delegate ever closed."""
    world = await delegate_after_its_own_promotion(train, lifecycle)
    writer = world.writer

    closed = await close_delegate(train, world, outcome)

    assert closed["success"], closed.get("feedback") or closed.get("issues") or closed
    assert closed["completion_source"] == world.resolved
    assert (await train.db.get_task(writer.task_id)).status.value == (
        "COMPLETED" if outcome == "pass" else "BLOCKED"
    )
    assert await train.db.get_workspace_for_task(writer.task_id) is None
    session = await train.db.get_session(writer.session_id)
    if lifecycle == "pool":
        # The claim goes with the close; the pool keeps its slot reservation.
        assert session.task_id is None and session.claim_phase is None
        held = await train.db.get_workspace(f"ws-{writer.task_id}")
        assert held.locked_by_task_id is None
        assert held.locked_by_agent_id == f"agent-{writer.task_id}"
    completions = await train.db.get_task_completions(writer.task_id)
    assert len(completions) == 1 and completions[0].commits == [world.resolved]
    assert _git_status(writer.path, "symbolic-ref", "-q", "HEAD").returncode != 0
    # A done subject refuses writer writes, so the stop record is the stage's.
    receipt = (await train.stage(world.operation["id"], 0))["dossier"][
        "reconciler_delivered_completion"
    ]
    assert receipt["kind"] == "promoted_delivery"
    assert receipt["head_sha"] == world.resolved
    assert receipt["promoted_head_sha"] == world.promoted.head_sha
    assert receipt["promotion_intent_id"]
    assert receipt["claim_epoch"] == 1 and receipt["outcome"] == outcome
    await assert_reconciler_only(train)


@pytest.mark.parametrize(
    "broken",
    ["lifecycle", "final_main", "revision", "resolution", "local_commit", "workspace", "ancestry"],
)
async def test_delegate_close_after_promotion_needs_its_whole_delivery_proof(
    train, broken, monkeypatch
):
    """A delivery claim with one missing link still gets today's refusal."""
    world = await delegate_after_its_own_promotion(train)
    writer = world.writer
    if broken == "ancestry":
        # Git cannot answer reachability: an unproved head is not a delivered one.
        monkeypatch.setattr(train.git, "ais_ancestor", AsyncMock(return_value=None))
    async with train.db.immediate() as conn:
        if broken == "lifecycle":
            await conn.execute(
                update(t.integration_batches).values(lifecycle="cleanup_pending")
            )
        elif broken == "final_main":
            await conn.execute(update(t.integration_batches).values(final_main_sha="f" * 40))
        elif broken == "revision":
            await conn.execute(update(t.integration_candidate_revisions).values(state="green"))
        elif broken == "resolution":
            await conn.execute(
                update(t.integration_candidate_resolutions).values(
                    state="rejected",
                    rejection_evidence={"reason": "test", "observed_at": 1.0},
                )
            )
        elif broken == "local_commit":
            # Work the reconciler never published: it is not the delivered head.
            writer.commit("post-acceptance work", {"stray.txt": "stray\n"})
        elif broken == "workspace":
            await conn.execute(
                update(t.workspaces).values(locked_by_task_id=None, locked_by_agent_id=None)
            )

    closed = await close_delegate(train, world)

    assert not closed.get("success"), closed
    assert closed.get("feedback"), closed
    assert (await train.db.get_task(writer.task_id)).status.value == "IN_PROGRESS"
    session = await train.db.get_session(writer.session_id)
    assert session.task_id == writer.task_id and session.claim_phase == "active"
    assert await train.db.get_task_completions(writer.task_id) == []
    held = await train.db.get_workspace(f"ws-{writer.task_id}")
    assert held.locked_by_task_id == (None if broken == "workspace" else writer.task_id)
    assert "reconciler_delivered_completion" not in (
        await train.stage(world.operation["id"], 0)
    )["dossier"]
    if broken == "local_commit":
        assert closed["feedback"] == "Repair workspace differs from its delivered head"
    elif broken == "workspace":
        assert closed["feedback"] == (
            "Repair close requires the original live claim and workspace"
        )
    else:
        # An unproved delivery keeps the ordinary repair-close refusal.
        assert closed.get("retired") or closed["feedback"] == (
            "Repair stage is no longer active; close is stale."
        )


@pytest.mark.parametrize("mirror_before_move", [False, True])
@pytest.mark.parametrize("interrupt_publication", [False, True])
async def test_accepted_ci_repair_rebuilt_on_moved_main_is_pushed_before_testing(
    train, mirror_before_move, interrupt_publication, monkeypatch
):
    await train.open()
    train.source("alpha", {"feature.txt": "feature\n"})
    await train.add_source("alpha", number=1)
    subject = await train.cutover()
    await train.run_until(lambda: train.phase(subject.id, SubjectPhase.TESTING), label="built")
    red = await train.subject(subject.id)
    train.ci.finish(red.head_sha, "failure", run=91)

    async def filed():
        return (await train.subject(subject.id)).writer.status is WriterStatus.FILED

    await train.run_until(filed, label="first repair filed")
    batch = await train.db.get_integration_batch(subject.batch_id)
    operation = await train.operation(batch["id"])
    stage = await train.stage(operation["id"], 0)
    writer = await train.claim(stage["repair_task_id"], batch)
    writer.checkout(batch["integration_branch"])
    repaired = writer.commit("repair candidate", {"fix.txt": "fixed\n"})
    _git(writer.path, "push", str(train.origin), "HEAD:" + batch["integration_branch"])
    await writer.close_accepted(repaired)
    if mirror_before_move:
        await train.run_until(
            lambda: train.phase(subject.id, SubjectPhase.TESTING), label="accepted repair built"
        )
    # Main moves after the successful writer has spent its original budget.
    train.clock.now += PRIMARY_SECONDS + 1

    _git(train.work, "switch", "-C", "main", train.base)
    (train.work / "unrelated.txt").write_text("moved\n")
    _git(train.work, "add", "-A")
    _git(train.work, "commit", "-m", "main moved after repair")
    _git(train.work, "push", "origin", "HEAD:refs/heads/main")
    moved = train.remote("refs/heads/main")

    service = train.handler.orchestrator.integration_candidate_service
    publish = service._publish
    interrupted = False

    async def publish_once(state, revision, store):
        nonlocal interrupted
        if interrupt_publication and not interrupted and revision["construction_base_sha"] == moved:
            interrupted = True
            return {**revision, "publication_wait": True}
        return await publish(state, revision, store)

    monkeypatch.setattr(service, "_publish", publish_once)
    # A hosted provider would reject an absent commit. It must never be queried
    # until the candidate has reached the remote, even after a partial build.
    unpublished_ci_reads = []

    async def live_ci(snapshot, head):
        from src.integration.runtime_contracts import CIEvidence, CIState

        if train.remote(head.ref) != head.sha:
            unpublished_ci_reads.append(head.sha)
            raise RuntimeError("hosted commit is unpublished")
        return CIEvidence(head_sha=head.sha, state=CIState.NONE, observed_at=train.clock())

    train.observer.candidate_ci = live_ci

    async def rebuilt():
        current = await train.subject(subject.id)
        return (
            current.phase is SubjectPhase.TESTING
            and current.base_sha == moved
            and train.remote(batch["integration_branch"]) == current.head_sha
        )

    await train.run_until(rebuilt, label="repair rebuilt on moved main")
    assert interrupted == interrupt_publication
    assert unpublished_ci_reads == []
    train.observer.candidate_ci = None
    current = await train.subject(subject.id)
    assert train.remote(batch["integration_branch"]) == current.head_sha
    assert train.contains(repaired, current.head_sha)
    assert train.contains(moved, current.head_sha)
    train.ci.finish(current.head_sha, "success", run=92)
    await train.run_until(lambda: train.phase(subject.id, SubjectPhase.DONE), label="promoted")
    assert train.remote("refs/heads/main") == current.head_sha
    assert (await train.operation(batch["id"]))["active_stage"] == 0


@pytest.mark.parametrize("revision_state", ["testing", "green", "red"])
async def test_main_moved_member_conflict_enters_repair_from_ci_lifecycle(train, revision_state):
    """A successor revision owns construction even when its predecessor reached CI."""
    await train.open()
    train.source("alpha", {"alpha.txt": "alpha\n"})
    await train.add_source("alpha", number=1)
    subject = await train.cutover()
    await train.run_until(lambda: train.phase(subject.id, SubjectPhase.TESTING), label="built")
    async with train.db.immediate() as conn:
        await conn.execute(
            update(t.integration_candidate_revisions)
            .where(
                t.integration_candidate_revisions.c.batch_id == subject.batch_id,
                t.integration_candidate_revisions.c.revision == 0,
            )
            .values(state=revision_state)
        )

    _git(train.work, "switch", "-C", "main", train.base)
    (train.work / "alpha.txt").write_text("moved\n")
    _git(train.work, "add", "-A")
    _git(train.work, "commit", "-m", "main overlaps member")
    _git(train.work, "push", "origin", "HEAD:refs/heads/main")
    moved = train.remote("refs/heads/main")

    await train.run_until(
        lambda: train.phase(subject.id, SubjectPhase.REPAIRING),
        label="member conflict reaches repairing",
    )
    batch = await train.db.get_integration_batch(subject.batch_id)
    assert (batch["current_revision"], batch["lifecycle"]) == (1, "repairing")
    operation = await train.operation(batch["id"])
    stage = await train.stage(operation["id"], 0)
    assert stage["repair_task_id"]
    async with train.db._engine.connect() as conn:
        revisions = (await conn.execute(
            select(t.integration_candidate_revisions)
            .where(t.integration_candidate_revisions.c.batch_id == batch["id"])
            .order_by(t.integration_candidate_revisions.c.revision)
        )).mappings().all()
        conflict = (await conn.execute(
            select(t.integration_candidate_member_results)
            .where(
                t.integration_candidate_member_results.c.batch_id == batch["id"],
                t.integration_candidate_member_results.c.revision == 1,
                t.integration_candidate_member_results.c.result == "conflict",
            )
        )).mappings().one()
        writer_status = (await conn.execute(
            select(t.tasks.c.status).where(t.tasks.c.id == stage["repair_task_id"])
        )).scalar_one()
    assert revisions[0]["state"] == "superseded"
    assert revisions[1]["construction_base_sha"] == moved
    assert conflict["member_ordinal"] == 0
    assert writer_status == "READY"


async def test_main_moved_after_build_rebuild_conflict_routes_subject_to_repairing(train):
    """Live 2026-10-04 subject d3fb5c5b: the rebuild of a moved main conflicted.

    A rebuild that preserves an accepted CI repair on the new main conflicts
    instead of answering ``built``.  That conflict names no batch member, so
    the adapter answered ``unknown`` and the subject stayed in testing, backoff
    after backoff, while the batch re-dispatched its repair.  The frozen
    conflict is the durable fact, so the adapter answers ``conflict`` and the
    subject reaches repairing with no operator edit.
    """
    await train.open()
    train.source("alpha", {"feature.txt": "feature\n"})
    await train.add_source("alpha", number=1)
    subject = await train.cutover()
    batch = await train.db.get_integration_batch(subject.batch_id)
    await train.run_until(lambda: train.phase(subject.id, SubjectPhase.TESTING), label="built")
    red = await train.subject(subject.id)

    # A real red CI repair round first: its accepted commits are the history
    # the later rebuild has to preserve.
    train.ci.finish(red.head_sha, "failure", run=31)

    async def repair_writer_filed():
        return (await train.subject(subject.id)).writer.status is WriterStatus.FILED

    await train.run_until(repair_writer_filed, label="red CI repair filed")
    operation = await train.operation(batch["id"])
    stage = await train.stage(operation["id"], 0)
    writer = await train.claim(stage["repair_task_id"], batch)
    writer.checkout(batch["integration_branch"])
    repaired = writer.commit("repair red candidate", {"fix.txt": "fixed\n"})
    _git(writer.path, "push", str(train.origin), "HEAD:" + batch["integration_branch"])
    with principal_context(writer.principal()):
        closed = await train.handler._cmd_task_close({
            "task_id": writer.task_id, "session_id": writer.session_id, "claim_epoch": 1,
            "outcome": "pass", "work_outcome": "shipped", "summary": "Repaired candidate",
            "commit": repaired,
        })
    assert closed["success"], closed
    await writer.stop()

    async def repaired_built():
        current = await train.subject(subject.id)
        return current.phase is SubjectPhase.TESTING and current.head_sha == repaired

    await train.run_until(repaired_built, label="closed writer's repair rebuilt")

    # main moves onto the very line that repair wrote, so preserving it on the
    # new main cannot be merged.
    _git(train.work, "switch", "-C", "main", train.base)
    (train.work / "fix.txt").write_text("new main\n")
    _git(train.work, "add", "-A")
    _git(train.work, "commit", "-m", "advance main with overlap")
    _git(train.work, "push", "origin", "HEAD:refs/heads/main")
    moved = train.remote("refs/heads/main")

    await train.run_until(
        lambda: train.phase(subject.id, SubjectPhase.REPAIRING),
        label="rebuild conflict reaches repairing",
    )
    # The rebuild answered the conflict it filed; it never degraded to unknown.
    answered = [
        row
        for row in await train.journal(subject.id)
        if row["entry_kind"] == "action" and row["primitive"] == "git_merge_members"
    ]
    assert not [row for row in answered if row["outcome"] == "unknown"]
    assert answered[-1]["outcome"] == "conflict"
    assert answered[-1]["payload"]["result"]["detail"] == {"member": None, "files": []}
    frozen = (await train.stage(operation["id"], 0))["dossier"]["candidate_rebuild_conflict"]
    assert (frozen["candidate_sha"], frozen["new_base_sha"]) == (repaired, moved)
    assert frozen["resolution"]["parents"] == [repaired, moved]
    assert (await train.db.get_integration_batch(subject.batch_id))["lifecycle"] == "repairing"
    # The batch re-dispatched its own delegate for the frozen conflict; nobody
    # edited the subject, the batch or the writer to get here.
    async with train.db._engine.connect() as conn:
        delegate = (
            (
                await conn.execute(
                    select(t.tasks.c.status).where(t.tasks.c.id == stage["repair_task_id"])
                )
            )
            .scalar_one()
        )
    assert delegate == "READY"
