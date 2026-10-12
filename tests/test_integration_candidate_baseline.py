"""A red candidate is judged against the target's own checks, with real evidence rows.

Every classification here is made from rows in ``integration_check_evidence``
written by a real ``ExactChecks`` refresh against a provider that names the exact
commit, so the comparison cannot pass on a fabricated verdict: a cached row the
cache cannot read is not evidence.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from src.database.tables import integration_batches, integration_check_evidence
from src.integration.batches import Batch, BatchMember, BatchObservation, candidate_ref
from src.integration.candidate_baseline import (
    BASELINE_BLOCKER,
    CandidateBaselineService,
    UnrecordedBaseline,
    compare,
    red_brief,
    target_baseline,
    target_head,
)
from src.integration.checks import (
    ChecksResult,
    ChecksState,
    CommitCheck,
    Conclusion,
    ExactChecks,
    HostedChecks,
    RequiredChecks,
)
from src.integration.ci_producers import HostedCIProducer, ProducerRequest
from src.integration.runtime_contracts import HeadIdentity
from src.integration.train import CandidateChecks, IntegrationTrain, TrainLane, TrainTarget

TARGET, CANDIDATE, OTHER = "a" * 40, "b" * 40, "c" * 40
MEMBERS = (BatchMember(task_id="t-1", source_sha=CANDIDATE, source_base_sha=TARGET),)
ROOT = TrainTarget("p", "r", "refs/heads/main")
REQUIRED = RequiredChecks(version="v1", names=("unit", "lint"), producer_id="15368")


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class Provider:
    """Exact-commit rows for whichever shas the test declares, plus suite ids."""

    compares_targets = True

    required = REQUIRED

    def __init__(self, verdicts: dict[str, dict[str, str]], *, suite_id: int = 21):
        self.verdicts, self.suite_id = verdicts, suite_id
        self.requests, self.rerequests = [], []

    async def request(self, head: HeadIdentity) -> ProducerRequest:
        self.requests.append(head)
        return ProducerRequest(outcome="requested", requested_at=1000.0)

    async def observe(self, head: HeadIdentity, *, now: float) -> tuple[CommitCheck, ...]:
        rows = []
        for name, conclusion in self.verdicts.get(head.sha, {}).items():
            rows.append(CommitCheck(
                repository_id=head.repository_id, sha=head.sha, name=name,
                producer_id=REQUIRED.producer_id, required_check_version=REQUIRED.version,
                conclusion=Conclusion(conclusion), classification="conclusive",
                observed_at=now, detail={"check_suite_id": self.suite_id},
            ))
        return tuple(rows)

    async def rerequest(self, head: HeadIdentity, suites: tuple[int, ...]) -> tuple[int, ...]:
        self.rerequests.append((head.sha, suites))
        return suites


class Service:
    """One exact candidate under test on a target the visit supplies."""

    def __init__(self, candidate=CANDIDATE, target=TARGET, states=("testing", "testing")):
        self.candidate, self.target, self.states = candidate, target, list(states)
        self.calls = 0

    async def visit(self, batch, members, snapshot):
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        self.calls += 1
        return BatchObservation(state, self.candidate, self.target)

    async def repair_authorized(self, batch, members, head_sha):
        return True


class Repair:
    async def settle_green(self, batch_id, head_sha):
        pass

    def __init__(self):
        self.calls = []

    async def allocate(self, batch_id, **kwargs):
        self.calls.append((batch_id, kwargs))
        return {"outcome": "filed", "task_id": f"repair-{batch_id}", "attempts": 1}


def batch(batch_id="batch-1", attempts=0):
    return Batch(id=batch_id, project_id="p", repository_id="r", target_ref="refs/heads/main",
                 repair_attempt_count=attempts)


def train(checks, *, baseline=None, service=None, repair=None, frozen_batch=None,
          sync_default_branch=None):
    """One target, one open batch, the given checks cache and baseline service."""
    from src.integration.train import BatchSelection

    async def snapshot():
        return SimpleNamespace(target_oid=TARGET)

    async def resolve(_batch, _candidate_sha):
        return checks

    lane = TrainLane(snapshot=snapshot, service=service or Service(),
                     checks=CandidateChecks(resolve), sync_default_branch=sync_default_branch)

    class Batches:
        async def open_batch(self, target, snapshot_, service_):
            return BatchSelection(frozen_batch or batch(), MEMBERS)

        async def settle(self, batch_, observation):
            return None

    async def lane_for(target):
        assert target == ROOT
        return lane

    async def targets(_now):
        return [ROOT]

    return IntegrationTrain(
        targets=SimpleNamespace(targets=targets),
        batches=Batches(),
        lane_for=lane_for,
        repair=repair or Repair(),
        baseline=baseline,
        clock=lambda: 1000.0,
    )


@pytest.fixture
async def db(reuse_database):
    from src.models import Project, RepoConfig, RepoSourceType

    database = await reuse_database("candidatebaseline.db")
    await database.create_project(Project(id="p", name="Project"))
    await database.create_repo(RepoConfig(id="r", project_id="p", source_type=RepoSourceType.LINK))
    yield database


async def frozen(database, batch_id="batch-baseline", attempts=0):
    from src.integration.batches import BatchStore

    store = BatchStore(database)
    await store.freeze(
        Batch(id=batch_id, project_id="p", repository_id="r", target_ref="refs/heads/main",
              repair_attempt_count=attempts),
        MEMBERS, trees={"t-1": "d" * 40},
    )
    return await store.get(batch_id)


async def rows(database, sha):
    async with database._engine.connect() as conn:
        return (
            await conn.execute(select(integration_check_evidence).where(
                integration_check_evidence.c.sha == sha
            ))
        ).mappings().all()


# ------------------------------------------------------------------ the comparison


def test_a_failure_the_target_also_has_is_pre_existing():
    candidate = ChecksResult(
        repository_id="r", sha=CANDIDATE, required=REQUIRED, state=ChecksState.RED,
        checks=(
            CommitCheck(repository_id="r", sha=CANDIDATE, name="unit", producer_id="15368",
                        required_check_version="v1", conclusion=Conclusion.FAILURE,
                        classification="conclusive", observed_at=1),
            CommitCheck(repository_id="r", sha=CANDIDATE, name="lint", producer_id="15368",
                        required_check_version="v1", conclusion=Conclusion.SUCCESS,
                        classification="conclusive", observed_at=1),
        ),
    )
    target = ChecksResult(
        repository_id="r", sha=TARGET, required=REQUIRED, state=ChecksState.RED,
        checks=(
            CommitCheck(repository_id="r", sha=TARGET, name="unit", producer_id="15368",
                        required_check_version="v1", conclusion=Conclusion.FAILURE,
                        classification="conclusive", observed_at=1),
            CommitCheck(repository_id="r", sha=TARGET, name="lint", producer_id="15368",
                        required_check_version="v1", conclusion=Conclusion.SUCCESS,
                        classification="conclusive", observed_at=1),
        ),
    )
    baseline = compare(candidate, target)
    assert (baseline.state, baseline.repair) == ("pre_existing", False)
    assert baseline.pre_existing == ("unit",) and baseline.failing == ("unit",)
    assert "pre-existing failures this repair does not own" in red_brief(baseline, CANDIDATE)


@pytest.mark.parametrize(
    ("target_conclusion", "state", "repairable"),
    [
        ("success", "repairable", ("unit",)),
        ("pending", "unproven", ()),
        ("cancelled", "unproven", ()),
    ],
)
def test_only_a_failure_the_target_passes_is_repairable(target_conclusion, state, repairable):
    def row(sha, name, conclusion):
        return CommitCheck(
            repository_id="r", sha=sha, name=name, producer_id="15368",
            required_check_version="v1", conclusion=Conclusion(conclusion),
            classification="conclusive", observed_at=1,
        )

    candidate = ChecksResult(
        repository_id="r", sha=CANDIDATE, required=REQUIRED, state=ChecksState.RED,
        checks=(row(CANDIDATE, "unit", "failure"), row(CANDIDATE, "lint", "success")),
    )
    target = ChecksResult(
        repository_id="r", sha=TARGET, required=REQUIRED, state=ChecksState.GREEN,
        checks=(row(TARGET, "unit", target_conclusion), row(TARGET, "lint", "success")),
    )
    baseline = compare(candidate, target)
    assert baseline.state == state and baseline.repairable == repairable
    # A target that has not decided the check never makes it repairable.
    assert baseline.repair is bool(repairable)


def test_a_mixed_candidate_repairs_only_what_the_target_passes():
    def row(sha, name, conclusion):
        return CommitCheck(
            repository_id="r", sha=sha, name=name, producer_id="15368",
            required_check_version="v1", conclusion=Conclusion(conclusion),
            classification="conclusive", observed_at=1,
        )

    candidate = ChecksResult(
        repository_id="r", sha=CANDIDATE, required=REQUIRED, state=ChecksState.RED,
        checks=(row(CANDIDATE, "unit", "failure"), row(CANDIDATE, "lint", "failure")),
    )
    target = ChecksResult(
        repository_id="r", sha=TARGET, required=REQUIRED, state=ChecksState.RED,
        checks=(row(TARGET, "unit", "failure"), row(TARGET, "lint", "success")),
    )
    baseline = compare(candidate, target)
    assert (baseline.state, baseline.repairable, baseline.pre_existing) == (
        "repairable", ("lint",), ("unit",))
    brief = red_brief(baseline, CANDIDATE)
    assert "  - lint" in brief.split("pre-existing")[0]
    assert "  - unit" in brief.split("pre-existing")[1]


# ------------------------------------------------- the train never files that repair


@pytest.mark.parametrize("candidate_conclusion", ["failure", "missing"])
@pytest.mark.parametrize("target_verdict", [
    {"unit": "missing", "lint": "missing"},
    {"unit": "failure", "lint": "missing"},
])
async def test_missing_target_checks_leave_candidate_failures_repairable(
    db, candidate_conclusion, target_verdict,
):
    """An incomplete required set cannot serve as the target's baseline."""
    frozen_batch = await frozen(db, "batch-missing-baseline")
    provider = Provider({
        CANDIDATE: {"unit": candidate_conclusion, "lint": "failure"},
        TARGET: target_verdict,
    })
    checks = ExactChecks(db, provider, clock=Clock())
    # Exercise the cached RED path too: target_baseline does not refresh it.
    target = await checks.refresh(target_head(frozen_batch, TARGET))
    assert target.state is ChecksState.RED
    repair = Repair()
    t = train(checks, baseline=CandidateBaselineService(db, clock=Clock()), repair=repair,
              frozen_batch=frozen_batch)

    visit = await t.visit(ROOT)

    assert visit.state == "repair" and visit.checks == "red"
    assert len(repair.calls) == 1
    brief = repair.calls[0][1]["brief"]
    assert "target baseline is unavailable" in brief
    assert "  - unit" in brief and "  - lint" in brief
    assert visit.detail["baseline"] == {
        "state": "unavailable", "target_sha": TARGET, "target_checks": "red",
        "repairable_checks": ["lint", "unit"], "pre_existing_checks": [],
        "unproven_checks": [], "reason": "target_required_checks_missing",
        "missing_target_checks": sorted(
            name for name, conclusion in target_verdict.items() if conclusion == "missing"
        ),
    }
    assert provider.rerequests == []
    async with db._engine.connect() as conn:
        assert (await conn.execute(select(integration_batches.c.baseline_observations).where(
            integration_batches.c.id == frozen_batch.id))).scalar_one() == 0


@pytest.mark.parametrize("target_conclusion", ["missing", "failure"])
async def test_hosted_target_baseline_distinguishes_absent_checks_from_real_failure(
    db, target_conclusion,
):
    """An unrelated main push run cannot establish a pre-existing required-check failure."""
    from src.integration.status import IntegrationStatusService
    from tests.test_integration_ci_producers import github

    candidate_client, trust = github(
        app=False, names=("unit",), conclusion="failure",
    )
    for record in [*candidate_client.checks, *candidate_client.workflows, *candidate_client.jobs]:
        record["head_sha"] = CANDIDATE
    candidate_client.workflows[0]["id"] = 32
    for job in candidate_client.jobs:
        job["run_id"] = 32
    target_client, _ = github(
        app=False, names=("unit",),
        conclusion="success" if target_conclusion == "missing" else "failure",
    )
    if target_conclusion == "missing":
        target_client.workflows[0].update(name="Main attestation", workflow_id=302)
        for record in [*target_client.checks, *target_client.jobs]:
            record["name"] = "unattested-ci / " + record["name"]

    async def paged_items(path, *, key):
        client = candidate_client if CANDIDATE in path or "/runs/32/" in path else target_client
        return await client.paged_items(path, key=key)

    client = SimpleNamespace(
        credential_identity=candidate_client.credential_identity,
        paged_items=AsyncMock(side_effect=paged_items),
        rerequest_check_suite=AsyncMock(),
    )
    trust = trust.model_copy(update={"canonical_repository_id": "r"})
    checks = ExactChecks(db, HostedChecks(HostedCIProducer(client, trust)), clock=Clock())
    frozen_batch = await frozen(db, "batch-hand-pushed-target")
    repair = Repair()
    t = train(checks, baseline=CandidateBaselineService(db, clock=Clock()), repair=repair,
              frozen_batch=frozen_batch)

    await t.tick()
    await t.drain()
    status = await IntegrationStatusService(db, git_first="active", train=t).train_status("p")

    [visit] = status["targets"]
    assert visit["checks"] == "red"
    baseline = visit["detail"]["baseline"]
    assert baseline["target_sha"] == TARGET and baseline["target_checks"] == "red"
    if target_conclusion == "missing":
        assert len(repair.calls) == 1 and visit["state"] == "repair"
        assert baseline["state"] == "unavailable"
        assert baseline["reason"] == "target_required_checks_missing"
        assert baseline["missing_target_checks"] == ["unit"]
        assert baseline["repairable_checks"] == ["unit"]
        assert baseline["pre_existing_checks"] == []
        client.rerequest_check_suite.assert_not_awaited()
    else:
        assert repair.calls == [] and visit["state"] == "preexisting"
        assert baseline["state"] == "pre_existing"
        assert baseline["repairable_checks"] == []
        assert baseline["pre_existing_checks"] == ["unit"]
        assert "reason" not in baseline
        client.rerequest_check_suite.assert_awaited_once_with(21)
    [stored_batch] = status["batches"]
    assert stored_batch["detail"]["baseline"] == baseline
    target_rows = await rows(db, TARGET)
    assert len(target_rows) == 1
    assert target_rows[0]["conclusion"] == target_conclusion and target_rows[0]["run_id"] == "31"
    candidate_rows = await rows(db, CANDIDATE)
    assert len(candidate_rows) == 1
    assert candidate_rows[0]["conclusion"] == "failure" and candidate_rows[0]["run_id"] == "32"


@pytest.mark.parametrize("baseline_kind", ["mixed", "missing", "unwired"])
async def test_hosted_mixed_checks_reach_status_baseline_and_filed_repair_brief(db, baseline_kind):
    """Observe both exact heads, cache the jobs, then read the actual filed task."""
    from src.integration.repair import OrdinaryRepairService
    from src.integration.status import IntegrationStatusService
    from tests.test_integration_ci_producers import github

    names = ("unit", "lint", "e2e")
    candidate_client, trust = github(
        app=False, names=names, job_conclusions=("failure", "failure", "success"),
        run_conclusion="failure",
    )
    for record in [*candidate_client.checks, *candidate_client.workflows, *candidate_client.jobs]:
        record["head_sha"] = CANDIDATE
    candidate_client.workflows[0]["id"] = 32
    for index, job in enumerate(candidate_client.jobs):
        job["run_id"] = 32
        job["html_url"] = f"https://github.com/acme/widgets/actions/runs/32/job/{job['id']}"
        # Test IDs are already available in the authenticated check listing;
        # obtaining the brief must not add per-check or log reads.
        if index < 2:
            node = f"tests/test_{names[index]}.py::test_failure[param with spaces]"
            candidate_client.checks[index]["output"] = {
                "summary": f"FAILED {node} - AssertionError",
                "text": f"ERROR {node} - teardown failed",
            }

    target_client, _ = github(
        app=False, names=names, job_conclusions=("failure", "success", "success"),
        run_conclusion="failure",
    )
    if baseline_kind == "missing":
        target_client.workflows[0]["conclusion"] = "success"
        for record in [*target_client.checks, *target_client.jobs]:
            record["name"] = "unrelated / " + record["name"]

    async def paged_items(path, *, key):
        source = candidate_client if CANDIDATE in path or "/runs/32/" in path else target_client
        return await source.paged_items(path, key=key)

    client = SimpleNamespace(
        credential_identity=candidate_client.credential_identity,
        paged_items=AsyncMock(side_effect=paged_items),
    )
    trust = trust.model_copy(update={"canonical_repository_id": "r"})
    checks = ExactChecks(db, HostedChecks(HostedCIProducer(client, trust)), clock=Clock())
    frozen_batch = await frozen(db, f"batch-mixed-{baseline_kind}")
    repairs = OrdinaryRepairService(db)
    baseline = (CandidateBaselineService(db, clock=Clock())
                if baseline_kind != "unwired" else None)
    t = train(checks, baseline=baseline, repair=repairs, frozen_batch=frozen_batch)

    await t.tick()
    await t.drain()
    status = await IntegrationStatusService(db, git_first="active", train=t).train_status("p")

    [visit] = status["targets"]
    assert visit["state"] == "repair" and visit["checks"] == "red"
    assert {row["check_name"]: row["conclusion"] for row in visit["check_runs"]} == {
        "unit": "failure", "lint": "failure", "e2e": "success",
    }
    classified = visit["detail"]["baseline"]
    if baseline_kind == "mixed":
        assert classified["repairable_checks"] == ["lint"]
        assert classified["pre_existing_checks"] == ["unit"]
        assert {row["check_name"]: row["conclusion"] for row in await rows(db, TARGET)} == {
            "unit": "failure", "lint": "success", "e2e": "success",
        }
    else:
        assert sorted(classified["repairable_checks"]) == ["lint", "unit"]
        assert classified["pre_existing_checks"] == []
    assert classified["unproven_checks"] == []

    task = await db.get_task(visit["repair"]["task_id"])
    brief = task.description.partition("\n\nRepair the observed head on")[0]
    assert brief.startswith(f"Required checks are red on candidate {CANDIDATE}")
    assert {line.strip()[2:] for line in brief.splitlines() if line.startswith("  - ")} == {
        "unit", "lint",
    }
    for index, name in enumerate(names[:2]):
        assert candidate_client.jobs[index]["html_url"] in brief
        node = f"tests/test_{name}.py::test_failure[param with spaces]"
        assert brief.count(f"Failing test: {node}") == 1
    assert "e2e" not in brief
    if baseline_kind == "mixed":
        assert "  - lint" in brief.split("pre-existing")[0]
        assert "  - unit" in brief.split("pre-existing")[1]
    else:
        assert "target baseline is unavailable" in brief
    assert (await repairs.input(task.id))["starting_sha"] == CANDIDATE
    listings = [call.kwargs["key"] for call in client.paged_items.await_args_list]
    assert listings.count("check_runs") == (1 if baseline_kind == "unwired" else 2)


async def test_failed_workflow_without_failed_required_jobs_reports_named_blocker(db):
    from src.integration.status import IntegrationStatusService
    from tests.test_integration_ci_producers import github

    client, trust = github(app=False, names=("unit", "lint"), run_conclusion="failure")
    for record in [*client.checks, *client.workflows, *client.jobs]:
        record["head_sha"] = CANDIDATE
    trust = trust.model_copy(update={"canonical_repository_id": "r"})
    checks = ExactChecks(db, HostedChecks(HostedCIProducer(client, trust)), clock=Clock())
    frozen_batch = await frozen(db, "batch-unknown-failures")
    repair = Repair()
    t = train(checks, baseline=CandidateBaselineService(db, clock=Clock()), repair=repair,
              frozen_batch=frozen_batch)

    await t.tick()
    await t.drain()
    status = await IntegrationStatusService(db, git_first="active", train=t).train_status("p")

    [visit] = status["targets"]
    assert visit["state"] == "blocked" and visit["checks"] == "red"
    assert all(row["conclusion"] == "success" for row in visit["check_runs"])
    assert visit["detail"]["blocker"] == "candidate_failing_checks_unknown"
    assert "candidate_failing_checks_unknown" in {blocker["code"] for blocker in status["blockers"]}
    assert repair.calls == []
    async with db._engine.connect() as conn:
        assert (await conn.execute(select(integration_batches.c.repair_attempt_count).where(
            integration_batches.c.id == frozen_batch.id,
        ))).scalar_one() == 0


async def test_a_pre_existing_failure_files_no_repair_and_rerequests(db):
    """The reported defect: a red candidate whose failing check also fails on main."""
    frozen_batch = await frozen(db, "batch-preexisting")
    provider = Provider({CANDIDATE: {"unit": "failure", "lint": "success"},
                         TARGET: {"unit": "failure", "lint": "success"}})
    checks = ExactChecks(db, provider, clock=Clock())
    baseline_service = CandidateBaselineService(db, clock=Clock())
    repair = Repair()
    t = train(checks, baseline=baseline_service, repair=repair, frozen_batch=frozen_batch)

    visit = await t.visit(ROOT)

    assert visit.state == "preexisting" and visit.checks == "red"
    assert visit.repair is None and repair.calls == []
    # No attempt was counted and no task exists for a failure the target already has.
    async with db._engine.connect() as conn:
        assert (await conn.execute(select(integration_batches.c.repair_attempt_count).where(
            integration_batches.c.id == "batch-preexisting"))).scalar_one() == 0
    assert visit.detail["baseline"] == {
        "state": "pre_existing", "target_sha": TARGET, "target_checks": "red",
        "repairable_checks": [], "pre_existing_checks": ["unit"], "unproven_checks": [],
    }
    assert visit.detail["re_request"]["outcome"] == "requested"
    # The re-request addressed this candidate's own suite, nothing else.
    assert provider.rerequests == [(CANDIDATE, (21,))]
    assert [row["sha"] for row in await rows(db, TARGET)] == [TARGET, TARGET]


async def test_root_preexisting_failure_never_considers_default_sync(db):
    frozen_batch = await frozen(db, "batch-root-preexisting")
    provider = Provider({CANDIDATE: {"unit": "failure", "lint": "success"},
                         TARGET: {"unit": "failure", "lint": "success"}})
    checks = ExactChecks(db, provider, clock=Clock())
    sync = AsyncMock(return_value={"ref": "refs/heads/main", "sha": OTHER, "checks": ["unit"]})
    repair = Repair()
    t = train(checks, baseline=CandidateBaselineService(db, clock=Clock()), repair=repair,
              frozen_batch=frozen_batch, sync_default_branch=sync)
    for _ in range(3):
        visit = await t.visit(ROOT)
        assert visit.state == "preexisting" and visit.repair is None
    assert visit.detail["re_request"]["blocker"] == BASELINE_BLOCKER
    sync.assert_not_awaited()
    assert repair.calls == []


async def test_the_target_baseline_is_obtained_before_anything_is_classified(db):
    """A target with no checks for its sha cannot decide anything yet."""
    frozen_batch = await frozen(db, "batch-nobaseline")
    provider = Provider({CANDIDATE: {"unit": "failure", "lint": "success"}})
    checks = ExactChecks(db, provider, clock=Clock())
    repair = Repair()
    t = train(checks, baseline=CandidateBaselineService(db, clock=Clock()), repair=repair,
              frozen_batch=frozen_batch)

    # The candidate's own refresh caches its rows; the target has none yet.
    first = await t.visit(ROOT)
    assert first.state == "preexisting" and repair.calls == []
    assert first.detail["baseline"]["state"] == "unproven"
    assert first.detail["baseline"]["unproven_checks"] == ["unit"]
    assert [head.sha for head in provider.requests] == [CANDIDATE, TARGET]
    # Two visits, two observations of the same unrepairable red, one re-request
    # under backoff: the second does not ask again.
    second = await t.visit(ROOT)
    assert second.detail["re_request"]["outcome"] == "waiting"
    assert len(provider.rerequests) == 1

    # Once the target's own run lands, the same failure is pre-existing, not
    # attributable to the batch.
    provider.verdicts[TARGET] = {"unit": "failure", "lint": "success"}
    third = await t.visit(ROOT)
    assert third.detail["baseline"]["state"] == "pre_existing"
    assert repair.calls == []


async def test_a_repairable_candidate_still_files_its_repair_with_a_scoped_brief(db):
    frozen_batch = await frozen(db, "batch-repairable")
    provider = Provider({CANDIDATE: {"unit": "failure", "lint": "failure"},
                         TARGET: {"unit": "failure", "lint": "success"}})
    checks = ExactChecks(db, provider, clock=Clock())
    repair = Repair()
    t = train(checks, baseline=CandidateBaselineService(db, clock=Clock()), repair=repair,
              frozen_batch=frozen_batch)

    visit = await t.visit(ROOT)

    assert visit.state == "repair"
    [(batch_id, args)] = repair.calls
    assert batch_id == "batch-repairable" and args["head_sha"] == CANDIDATE
    brief = args["brief"]
    assert brief.startswith(f"Required checks are red on candidate {CANDIDATE}")
    assert "  - lint" in brief.split("pre-existing")[0]
    assert "  - unit" in brief.split("pre-existing")[1]
    assert provider.rerequests == []


async def test_a_train_without_a_baseline_service_repairs_exactly_as_before(db):
    """The default claims nothing, so no behaviour changes without the wiring."""
    frozen_batch = await frozen(db, "batch-unwired")
    provider = Provider({CANDIDATE: {"unit": "failure", "lint": "success"},
                         TARGET: {"unit": "failure", "lint": "success"}})
    checks = ExactChecks(db, provider, clock=Clock())
    repair = Repair()
    t = train(checks, repair=repair, frozen_batch=frozen_batch)

    visit = await t.visit(ROOT)

    assert visit.state == "repair" and len(repair.calls) == 1
    brief = repair.calls[0][1]["brief"]
    assert "target baseline is unavailable" in brief
    assert "  - unit" in brief and "  - lint" not in brief
    assert UnrecordedBaseline is not t.baseline


async def test_a_local_lane_with_no_target_baseline_files_its_repair(db):
    """Local validation jobs exist only for candidates, so nothing is claimed."""
    frozen_batch = await frozen(db, "batch-local")
    provider = Provider({CANDIDATE: {"unit": "failure", "lint": "success"}})
    provider.compares_targets = False
    checks = ExactChecks(db, provider, clock=Clock())
    repair = Repair()
    t = train(checks, baseline=CandidateBaselineService(db, clock=Clock()), repair=repair,
              frozen_batch=frozen_batch)

    visit = await t.visit(ROOT)

    assert visit.state == "repair" and len(repair.calls) == 1
    assert TARGET not in [head.sha for head in provider.requests]


async def test_the_bound_names_a_blocker_instead_of_rerequesting_forever(db, caplog):
    from src.integration.candidate_baseline import BASELINE_ATTEMPTS

    frozen_batch = await frozen(db, "batch-bound")
    provider = Provider({CANDIDATE: {"unit": "failure", "lint": "success"},
                         TARGET: {"unit": "failure", "lint": "success"}})
    clock = Clock()
    checks = ExactChecks(db, provider, clock=clock)
    t = train(checks, baseline=CandidateBaselineService(db, clock=clock, backoff_seconds=1.0),
              repair=Repair(), frozen_batch=frozen_batch)

    for _ in range(BASELINE_ATTEMPTS):
        clock.now += 10  # past every deadline, so the bound is what ends the loop
        visit = await t.visit(ROOT)
    assert visit.state == "preexisting"
    assert visit.detail["re_request"] == {
        "outcome": "blocked", "blocker": BASELINE_BLOCKER, "observations": BASELINE_ATTEMPTS,
        "reruns": BASELINE_ATTEMPTS - 1, "due_at": None,
    }
    assert len(provider.rerequests) == BASELINE_ATTEMPTS - 1
    assert BASELINE_BLOCKER in caplog.text

    # A repaired head is a new identity: the bound starts from zero again.
    async with db._engine.begin() as conn:
        await conn.execute(update(integration_batches).where(
            integration_batches.c.id == "batch-bound"
        ).values(baseline_candidate_sha=None))
    again = await t.visit(ROOT)
    assert again.detail["re_request"]["outcome"] == "requested"
    assert again.detail["re_request"]["observations"] == 1


async def test_the_operator_sees_the_preexisting_blocker_and_its_evidence(db):
    from src.integration.status import IntegrationStatusService

    frozen_batch = await frozen(db, "batch-status")
    provider = Provider({CANDIDATE: {"unit": "failure", "lint": "success"},
                         TARGET: {"unit": "failure", "lint": "success"}})
    clock = Clock()
    checks = ExactChecks(db, provider, clock=clock)
    t = train(checks, baseline=CandidateBaselineService(db, clock=clock),
              frozen_batch=frozen_batch)
    await t.tick()
    await t.drain()

    status = await IntegrationStatusService(db, git_first="active", train=t).train_status("p")

    [blocker] = [item for item in status["blockers"] if item.get("ref") == "batch-status"]
    assert blocker["code"] == "checks_preexisting"
    assert blocker["evidence"]["pre_existing_checks"] == ["unit"]
    assert blocker["candidate_sha"] == CANDIDATE


async def test_the_baseline_bound_must_be_positive_and_ordered(db):
    for kwargs in ({"attempts": 0}, {"backoff_seconds": 0.0}, {"backoff_max_seconds": 1.0}):
        with pytest.raises(ValueError):
            CandidateBaselineService(db, **kwargs)


async def test_an_uncomparable_target_is_never_treated_as_green(db):
    """No rows for the target sha at all: nothing is attributable to the batch."""
    provider = Provider({CANDIDATE: {"unit": "failure", "lint": "success"}})
    checks = ExactChecks(db, provider, clock=Clock())
    red = await checks.refresh(
        HeadIdentity(repository_id="r", ref=candidate_ref("batch-x"), sha=CANDIDATE, generation=0)
    )
    baseline = await target_baseline(
        batch(), candidate=red, target_sha=TARGET, checks=checks
    )
    assert baseline.state == "unproven" and baseline.repair is False
    assert (baseline.target_sha, baseline.unproven) == (TARGET, ("unit",))
    # No target sha at all is never a comparison either.
    assert (await target_baseline(
        batch(), candidate=red, target_sha=None, checks=checks
    )).unavailable
    assert (await target_baseline(
        batch(), candidate=red, target_sha=TARGET, checks=None
    )).unavailable
    assert target_head(batch(), TARGET) == HeadIdentity(
        repository_id="r", ref="refs/heads/main", sha=TARGET, generation=0
    )


async def test_only_this_heads_own_suites_are_rerequested(db):
    """A foreign sha's suite id is never in this cache, so it is never addressed."""
    provider = Provider({CANDIDATE: {"unit": "failure", "lint": "success"}, OTHER: {}})
    checks = ExactChecks(db, provider, clock=Clock())
    await checks.refresh(
        HeadIdentity(repository_id="r", ref=candidate_ref("b"), sha=CANDIDATE, generation=0)
    )
    suites = await checks.rerequest(
        HeadIdentity(repository_id="r", ref=candidate_ref("b"), sha=CANDIDATE, generation=0),
        ("unit", "lint"),
    )
    assert suites == (21,)
    assert provider.rerequests == [(CANDIDATE, (21,))]
    # Nothing cached under this sha means nothing is addressed.
    assert await checks.rerequest(
        HeadIdentity(repository_id="r", ref="refs/heads/main", sha=OTHER, generation=0), ("unit",)
    ) == ()
