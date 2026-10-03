"""Shadow-versus-legacy decision comparison: completeness, gates and read-only reads."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, insert

from src.commands.contracts.integration import (
    IntegrationShadowReportArgs,
    register_integration_contracts,
)
from src.commands.contracts.registry import ContractRegistry
from src.database import Database
from src.database import tables as t
from src.integration.shadow_report import (
    ACTION_CLASSES,
    COVERAGE_LIMITS,
    LEGACY_LEDGER,
    LEGACY_ONLY_CLASSES,
    SHADOW_OBSERVATION_REQUIRED_SECONDS,
    VERDICT_AGREE,
    VERDICT_DIVERGENT,
    VERDICT_MISSING,
    LegacyDecision,
    ObservationWindow,
    ShadowObservation,
    ShadowReportReader,
    action_class,
    build_report,
    compare,
)
from src.integration.subjects import (
    PolicyArtifactPin,
    Primitive,
    Subject,
    SubjectKind,
    SubjectPhase,
    SubjectSchedule,
)
from tests.db_fixtures import lease_dsn

DAY = 24 * 60 * 60
START = 1_700_000_000.0
ARTIFACT = "sha256:" + "1" * 64
OTHER_ARTIFACT = "sha256:" + "9" * 64
BASE, HEAD = (char * 40 for char in "ab")
WEEK = ObservationWindow(start=START, end=START + 8 * DAY)


def window(seconds: float = 8 * DAY) -> ObservationWindow:
    return ObservationWindow(start=START, end=START + seconds)


def legacy(key="sealed_batch", identity="batch", batch_id="batch", action="seal", day=1.0, **kw):
    return LegacyDecision(
        source=key,
        identity=identity,
        batch_id=batch_id,
        action_class=action,
        recorded_at=START + day * DAY,
        **kw,
    )


def shadow(
    seq=1,
    subject_id="subject",
    primitive=Primitive.SEAL.value,
    day=1.0,
    artifact=ARTIFACT,
    unknown=(),
    batch_id="batch",
    rule="rule",
):
    return ShadowObservation(
        seq=seq,
        subject_id=subject_id,
        subject_version=0,
        visit_id=f"shadow:{seq}",
        phase="admitting",
        rule=rule,
        primitive=primitive,
        action_class=action_class(primitive),
        facts_digest="sha256:" + "2" * 64,
        policy_artifact_sha256=artifact,
        recorded_at=START + day * DAY,
        unknown=tuple(unknown),
        batch_id=batch_id,
    )


def test_action_classes_cover_every_primitive_exactly_once():
    assert set(ACTION_CLASSES) == {primitive.value for primitive in Primitive}
    assert action_class(None) == "unmapped"
    assert action_class("not_a_primitive") == "unmapped"
    assert action_class(Primitive.GIT_PUBLISH.value) == "promote"


def test_every_legacy_source_declares_a_real_time_column_and_known_class():
    for source in LEGACY_LEDGER:
        assert source.time_column in source.table.c, source.key
        assert all(column in source.table.c for column in source.id_columns), source.key
        known = set(ACTION_CLASSES.values()) | LEGACY_ONLY_CLASSES
        assert source.action_class in known, source.key
        assert source.description


def test_a_window_must_end_after_it_starts():
    with pytest.raises(ValueError, match="end after it starts"):
        ObservationWindow(start=START, end=START)


def test_every_legacy_decision_appears_exactly_once_in_the_table():
    decisions = [
        legacy(day=1.0),
        legacy(key="candidate_build", identity="batch:0", action="build", day=2.0),
        legacy(key="main_promotion", identity="p1", action="promote", day=3.0),
        legacy(key="cleanup_item", identity="batch:remote_ref:x", action="cleanup", day=4.0),
    ]
    observations = [
        shadow(seq=1, primitive=Primitive.SEAL.value, day=1.0),
        shadow(seq=2, primitive=Primitive.GIT_MERGE_MEMBERS.value, day=2.0),
        shadow(seq=3, primitive=Primitive.GIT_PUBLISH.value, day=3.0),
        shadow(seq=4, primitive=Primitive.CLEANUP.value, day=4.0),
    ]
    report = compare("p", WEEK, decisions, observations, engines={"subject": "legacy"})
    identities = [f"{row.legacy.source}:{row.legacy.identity}" for row in report.rows]
    assert identities == [
        "sealed_batch:batch",
        "candidate_build:batch:0",
        "main_promotion:p1",
        "cleanup_item:batch:remote_ref:x",
    ]
    assert len(identities) == len(set(identities)) == len(decisions)
    assert report.agreements == 4 and report.missing_comparisons == 0
    assert report.unexplained_batches == () and report.unexplained == 0
    assert report.cleared_for_review is False  # Recorded shadow span is under one week.
    markdown = report.render_markdown()
    assert all(identity in markdown for identity in identities)


def test_a_legacy_decision_outside_the_window_is_not_compared():
    two_days = window(seconds=2 * DAY)
    report = compare(
        "p",
        two_days,
        [
            legacy(day=1.0),
            legacy(key="main_promotion", identity="p1", action="promote", day=7.0),
        ],
        [shadow(seq=1, day=1.0)],
        engines={"subject": "legacy"},
    )
    assert report.legacy_decisions == 1
    assert two_days.complete is False
    assert any("required" in reason for reason in report.blocking_reasons)


def test_a_legacy_decision_the_new_engine_never_chose_is_named_not_dropped():
    report = compare(
        "p",
        WEEK,
        [legacy(key="candidate_publish", identity="batch:0", action="publish", day=2.0)],
        [shadow(seq=1, day=1.0)],
        engines={"subject": "legacy"},
    )
    assert report.legacy_decisions == 1
    assert report.rows[0].verdict == VERDICT_DIVERGENT
    assert report.rows[0].no_route is True
    assert report.unrouted_decisions == 1 and report.agreements == 0
    assert any("no route in the pinned" in reason for reason in report.blocking_reasons)
    assert "`candidate_publish:batch:0`" in report.render_markdown()
    assert report.render_markdown().count("candidate_publish:batch:0") == 2


def test_an_observed_subject_that_chose_another_class_is_a_named_divergence():
    report = compare(
        "p",
        WEEK,
        [legacy(key="other_batch", identity="batch2", batch_id="batch2", action="promote", day=2.0)],
        [
            shadow(seq=1, day=1.0),
            shadow(seq=2, subject_id="other", batch_id="batch2",
                    primitive=Primitive.GIT_PUBLISH.value, day=3.0),
        ],
        engines={"subject": "legacy", "other": "legacy"},
    )
    row = next(row for row in report.rows if row.legacy.batch_id == "batch2")
    assert row.verdict == VERDICT_AGREE
    unobserved = compare(
        "p",
        WEEK,
        [legacy(key="sealed_batch", identity="ghost", batch_id="ghost", action="seal", day=2.0)],
        [shadow(seq=1, day=1.0)],
        engines={"subject": "legacy"},
    )
    assert unobserved.rows[0].verdict == VERDICT_MISSING
    assert unobserved.rows[0].no_route is False
    assert unobserved.missing_comparisons == 1
    # The same class elsewhere in the window turns an unobserved subject into a
    # readable divergence rather than a silently missing comparison.
    divergence = compare(
        "p",
        WEEK,
        [legacy(key="sealed_batch", identity="batch2", batch_id="batch2", action="seal", day=2.0)],
        [
            shadow(seq=1, day=1.0),
            shadow(seq=2, subject_id="other", batch_id="batch2",
                    primitive=Primitive.GIT_MERGE_MEMBERS.value, day=3.0),
        ],
        engines={"subject": "legacy", "other": "legacy"},
    )
    assert divergence.rows[0].verdict == VERDICT_DIVERGENT
    assert divergence.divergences == 1
    assert divergence.missing_comparisons == 0


def test_a_batch_touched_in_the_window_with_no_accounted_decision_is_unexplained():
    report = compare(
        "p",
        WEEK,
        [legacy(day=1.0)],
        [shadow(seq=1, day=1.0)],
        touched_batches={"batch": START + 2 * DAY, "quiet-batch": START + 2 * DAY},
        engines={"subject": "legacy"},
    )
    assert report.unexplained_batches == ("quiet-batch",)
    assert any("no accounted decision" in reason for reason in report.blocking_reasons)


def test_unknown_observations_block_until_an_operator_names_the_sequence():
    sensitive = shadow(seq=7, unknown=("publisher_authority_unavailable",))
    decisions = [legacy(day=1.0)]
    blocking = compare("p", WEEK, decisions, [sensitive], engines={"subject": "legacy"})
    assert blocking.unknown_observations == 1
    assert any("unknown is not a successful action" in reason for reason in blocking.blocking_reasons)
    acknowledged = compare(
        "p", WEEK, decisions, [sensitive],
        engines={"subject": "legacy"}, acknowledged_unknowns=[7],
    )
    assert acknowledged.unknown_observations == 0
    assert acknowledged.acknowledged_unknowns[0]["seq"] == 7
    assert acknowledged.cleared_for_review is False  # Acknowledgement cannot waive time.


def test_bookkeeping_observations_are_reported_but_never_block():
    report = compare(
        "p",
        WEEK,
        [legacy(day=1.0)],
        [shadow(seq=1, day=1.0), shadow(seq=2, primitive=Primitive.WAIT.value, day=2.0)],
        engines={"subject": "legacy"},
    )
    assert report.cleared_for_review is False  # Recorded shadow span is under one week.
    assert report.unknown_observations == 0
    assert report.shadow_only == (
        {"subject_id": "subject", "action_class": "observe", "count": 1},
    )


def test_a_mixed_artifact_window_blocks_and_names_every_pin():
    report = compare(
        "p",
        WEEK,
        [legacy(day=1.0)],
        [shadow(seq=1, day=1.0), shadow(seq=2, artifact=OTHER_ARTIFACT, day=2.0)],
        engines={"subject": "legacy"},
    )
    assert report.policy_artifacts == (ARTIFACT, OTHER_ARTIFACT)
    assert any("one window per artifact" in reason for reason in report.blocking_reasons)


def test_a_window_shorter_than_the_observation_period_is_never_rounded_up():
    short = compare(
        "p", window(seconds=SHADOW_OBSERVATION_REQUIRED_SECONDS - 1),
        [legacy(day=1.0)], [shadow(seq=1, day=1.0)], engines={"subject": "legacy"},
    )
    assert short.window.complete is False
    assert any("observation window covers" in reason for reason in short.blocking_reasons)
    exact = compare(
        "p", window(seconds=SHADOW_OBSERVATION_REQUIRED_SECONDS),
        [legacy(day=1.0)], [shadow(seq=1, day=0), shadow(seq=2, day=7)],
        engines={"subject": "legacy"},
    )
    assert exact.window.complete is True and exact.cleared_for_review is True


def test_an_empty_window_blocks_and_proves_nothing():
    report = compare("p", WEEK, [], [], engines={})
    assert report.legacy_decisions == 0
    assert report.first_observed_at is None and report.observed_span_seconds == 0.0
    assert any("no shadow decision" in reason for reason in report.blocking_reasons)
    assert "the window proves nothing" in report.render_markdown()


def test_a_reconciler_owned_root_breaks_the_exclusive_ownership_gate():
    report = compare(
        "p", WEEK, [legacy(day=1.0)], [shadow(seq=1, day=1.0)],
        engines={"subject": "reconciler"},
    )
    assert report.reconciler_owned_subjects == ("subject",)
    assert any("exclusive ownership" in reason for reason in report.blocking_reasons)


def test_the_report_is_deterministic_and_names_its_own_sources_and_limits():
    report = compare(
        "p", WEEK, [legacy(day=1.0)], [shadow(seq=1, day=1.0)], engines={"subject": "legacy"}
    )
    assert compare(
        "p", WEEK, [legacy(day=1.0)], [shadow(seq=1, day=1.0)], engines={"subject": "legacy"}
    ).digest == report.digest
    document = report.as_dict()
    assert document["report"] == "integration_shadow_comparison"
    assert document["digest"] == report.digest
    assert json.loads(json.dumps(document))["rows"] == document["rows"]
    assert [source["key"] for source in document["sources"]] == [s.key for s in LEGACY_LEDGER]
    assert document["coverage_limits"] == list(COVERAGE_LIMITS)
    assert document["window"]["required_seconds"] == SHADOW_OBSERVATION_REQUIRED_SECONDS
    assert document["cleared_for_review"] is False
    assert any("recorded shadow" in reason for reason in document["blocking_reasons"])
    commands = "\n".join(document["operator_commands"])
    assert "aq integration engine-transfer" in commands
    assert f"--evidence shadow-comparison:{report.digest}" in commands
    assert "aq restart --no-dashboard" in "\n".join(document["rollback_commands"])
    assert "reconciler_active: false" in "\n".join(document["rollback_commands"])


def test_the_report_never_runs_a_transfer_and_always_records_a_signature():
    report = compare(
        "p", WEEK, [legacy(day=1.0)], [shadow(seq=1, day=1.0)], engines={"subject": "legacy"}
    )
    markdown = report.render_markdown()
    assert markdown.startswith("# Shadow comparison report")
    assert "## Signature" in markdown
    assert "Approved for cutover by: ______________" in markdown
    assert f"Report digest: {report.digest}" in markdown
    for command in report.operator_commands():
        assert command.startswith(("#", "aq "))


def test_args_reject_an_inverted_window_and_negative_acknowledgements():
    with pytest.raises(ValueError, match="until must be after since"):
        IntegrationShadowReportArgs(project_id="p", since=2.0, until=1.0)
    with pytest.raises(ValueError, match="nonnegative journal sequences"):
        IntegrationShadowReportArgs(
            project_id="p", since=1.0, until=2.0, acknowledge_unknown=(-1,)
        )
    assert IntegrationShadowReportArgs(
        project_id="p", since=1.0, until=2.0, acknowledge_unknown=(3,)
    ).acknowledge_unknown == (3,)


def test_the_contract_is_registered_as_a_read_only_operator_control():
    registry = ContractRegistry()
    register_integration_contracts(registry)
    registration = registry.get("integration_shadow_report")
    assert registration is not None
    contract = registration.contract
    assert contract.execution.capability == "integration_shadow_report"
    assert contract.execution.side_effect.value == "read"
    assert contract.execution.retry_safe is True
    assert {outcome.name for outcome in contract.execution.outcomes} == {"report", "refused"}


def test_the_cli_sends_the_explicit_window_and_writes_the_artifact(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from src.cli import app as cli_app
    from src.cli import integration as cli_integration

    captured: dict = {}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, command, args):
            captured["command"] = command
            captured["args"] = args
            return {"markdown": "# report\n", "digest": "sha256:x"}

    monkeypatch.setattr(cli_integration, "_get_client", lambda *a, **kw: Client())
    output = tmp_path / "shadow.md"
    result = CliRunner().invoke(
        cli_app.cli,
        ["integration", "shadow-report", "p", "--since", "1000",
         "--acknowledge-unknown", "7", "--output", str(output)],
        obj={"api_url": None},
    )
    assert result.exit_code == 0, result.output
    assert captured["command"] == "integration_shadow_report"
    assert captured["args"]["since"] == 1000.0
    assert captured["args"]["until"] > 1000.0
    assert captured["args"]["acknowledge_unknown"] == [7]
    assert output.read_text() == "# report\n"


def test_the_cli_refuses_an_inverted_window_before_any_request(monkeypatch):
    from click.testing import CliRunner

    from src.cli import app as cli_app
    from src.cli import integration as cli_integration

    def _never_called(*args, **kwargs):
        raise AssertionError("an inverted window must not reach the daemon")

    monkeypatch.setattr(cli_integration, "_get_client", _never_called)
    result = CliRunner().invoke(
        cli_app.cli,
        ["integration", "shadow-report", "p", "--since", "2000", "--until", "1000"],
        obj={"api_url": None},
    )
    assert result.exit_code != 0
    assert "--until must be after --since" in result.output


async def _seed(db):
    async with db._engine.begin() as conn:
        await conn.execute(
            insert(t.projects).values(
                id="p", name="project", status="ACTIVE", created_at=1,
                hierarchical_integration_policy={}, hierarchical_integration_generation=1,
            )
        )
        await conn.execute(
            insert(t.repos).values(
                id="repo", project_id="p", url="https://example/repo.git",
                default_branch="main", checkout_base_path="/checkout", source_type="clone",
            )
        )
        await conn.execute(
            insert(t.playbook_artifacts).values(
                artifact_sha256=ARTIFACT, playbook_id="root-train",
                source_digest="sha256:" + "2" * 64, contract_fingerprint="sha256:" + "3" * 64,
                compiler_build="test", path="/test/root.json", created_at=1,
            )
        )
        await conn.execute(
            insert(t.integration_batches).values(
                id="batch", project_id="p", repository_id="repo", request_id="request",
                source_manifest_digest="sha256:" + "4" * 64, base_sha=BASE,
                lifecycle="testing", current_revision=0, integration_branch="aq/batch",
                policy_snapshot={}, artifact_snapshot={}, cleanup_state="pending",
                created_at=START, updated_at=START + DAY,
            )
        )
        await conn.execute(
            insert(t.integration_candidate_revisions).values(
                batch_id="batch", revision=0, construction_base_sha=BASE,
                next_member_ordinal=0, head_sha=HEAD, state="green",
                created_at=START, updated_at=START + DAY,
            )
        )
        await conn.execute(
            insert(t.integration_candidate_publications).values(
                batch_id="batch", revision=0, state="pr_published", repository_id="repo",
                repository_numeric_id=1, repository_full_name="acme/widgets",
                base_ref="refs/heads/main", head_ref="refs/heads/aq/batch", head_sha=HEAD,
                expected_old_sha=BASE, idempotency_key="pub-0", pr_number=7,
                pr_url="https://example/pull/7", created_at=START, updated_at=START + 2 * DAY,
            )
        )
        await conn.execute(
            insert(t.integration_branch_owners).values(
                id="owner", repository_id="repo", ref="aq/batch", owner_id="collector",
                owner_role="collector", fence_token=1, handoff_state="released",
                created_at=START, updated_at=START + 3 * DAY,
            )
        )
    subject = Subject(
        id="subject", project_id="p", repository_id="repo", kind=SubjectKind.ROOT_BATCH,
        subject_key="root_batch:repo:request",
        policy=PolicyArtifactPin(playbook_id="root-train", artifact_sha256=ARTIFACT),
        batch_id="batch", phase=SubjectPhase.BUILDING,
        schedule=SubjectSchedule.progress(now=START, max_wait_seconds=600),
        created_at=START, updated_at=START,
    )
    await db.ensure_integration_subject(subject.to_row())
    for version, rule, phase, primitive, day in (
        (0, "admit-frontier", "admitting", Primitive.SEAL.value, 0.5),
        (1, "construct", "building", Primitive.GIT_MERGE_MEMBERS.value, 1.0),
    ):
        await db.append_integration_subject_journal(
            {
                "subject_id": "subject", "entry_kind": "decision",
                "idempotency_key": f"shadow:{version}:decision",
                "visit_id": f"shadow:{version}", "mode": "shadow",
                "policy_artifact_sha256": ARTIFACT, "subject_version": version,
                "phase": phase, "head_sha": None, "generation": 0, "rule": rule,
                "primitive": primitive, "facts_digest": "sha256:" + "5" * 64,
                "outcome": None, "payload": {"decision": {}, "facts": {"unknown": []}},
                "recorded_at": START + day * DAY,
            }
        )


@pytest.fixture
async def seeded_db():
    db = Database(lease_dsn("shadow-report"))
    await db.initialize()
    try:
        await _seed(db)
        yield db
    finally:
        await db.close()


async def test_the_reader_joins_both_arms_over_real_tables_and_stays_read_only(seeded_db):
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(seeded_db._engine.sync_engine, "before_cursor_execute", record)
    try:
        report = await build_report(
            "p", seeded_db, since=START, until=START + 8 * DAY
        )
    finally:
        event.remove(seeded_db._engine.sync_engine, "before_cursor_execute", record)
    assert "SET TRANSACTION READ ONLY" in statements
    assert all(
        statement.lstrip().upper().startswith(("SELECT", "SET TRANSACTION READ ONLY"))
        for statement in statements
    )
    sources = {row.legacy.source: row.legacy.action_class for row in report.rows}
    assert sources == {
        "sealed_batch": "seal",
        "candidate_build": "build",
        "candidate_publish": "publish",
        "ownership_release": "ownership",
    }
    build_row = next(row for row in report.rows if row.legacy.source == "candidate_build")
    assert build_row.verdict == VERDICT_AGREE
    assert [observation.primitive for observation in build_row.shadow] == [
        Primitive.GIT_MERGE_MEMBERS.value
    ]
    # The shipped root-train table has no route for publishing a candidate branch
    # or releasing branch ownership, so both are named rather than dropped.
    assert {row.legacy.source for row in report.rows if row.no_route} == {
        "candidate_publish",
        "ownership_release",
    }
    assert report.policy_artifacts == (ARTIFACT,)
    assert report.subject_ids == ("subject",)
    assert report.unexplained_batches == ()
    assert report.unrouted_decisions == 2
    assert any("no route in the pinned" in reason for reason in report.blocking_reasons)


async def test_the_reader_ignores_another_project_and_active_mode_entries(seeded_db):
    snapshot = await ShadowReportReader(seeded_db).read("other-project")
    assert snapshot.legacy == () and snapshot.observations == ()
    active = await ShadowReportReader(seeded_db).read("p")
    assert {observation.visit_id for observation in active.observations} == {
        "shadow:0",
        "shadow:1",
    }
    async with seeded_db._engine.begin() as conn:
        await conn.execute(
            insert(t.integration_subject_journal).values(
                subject_id="subject", entry_kind="decision",
                idempotency_key="active:1:decision", visit_id="active:1", mode="active",
                policy_artifact_sha256=ARTIFACT, subject_version=0, phase="building",
                generation=0, rule="construct", primitive=Primitive.SEAL.value,
                facts_digest="sha256:" + "6" * 64, payload={}, recorded_at=START + 2 * DAY,
            )
        )
    after = await ShadowReportReader(seeded_db).read("p")
    assert {observation.visit_id for observation in after.observations} == {
        "shadow:0",
        "shadow:1",
    }


async def test_the_ledger_reads_never_write_or_lock_a_subject(seeded_db):
    before = await seeded_db.get_integration_subject("subject")
    await build_report("p", seeded_db, since=START, until=START + 8 * DAY)
    assert await seeded_db.get_integration_subject("subject") == before
    assert len(await seeded_db.list_integration_subject_journal("subject")) == 2


async def test_the_command_requires_a_project_and_an_operator_principal():
    from src.commands.integration_commands import IntegrationCommandsMixin
    from src.commands.principal import ExecutionPrincipal, principal_context

    handler = IntegrationCommandsMixin()
    handler.db = SimpleNamespace(get_project=AsyncMock(return_value=None))
    assert (
        await handler._cmd_integration_shadow_report(
            {"project_id": "missing", "since": START, "until": START + 8 * DAY}
        )
    ) == {"success": False, "outcome": "refused", "error": "project does not exist"}
    handler.db = SimpleNamespace(
        get_project=AsyncMock(return_value=SimpleNamespace(id="p")),
    )
    with principal_context(ExecutionPrincipal.service("shadow report")):
        refused = await handler._cmd_integration_shadow_report(
            {"project_id": "p", "since": START, "until": START + 8 * DAY}
        )
    assert refused["outcome"] == "refused"
    assert "operator" in refused["error"]


async def test_the_command_returns_the_whole_artifact_and_writes_nothing(seeded_db):
    from src.commands.integration_commands import IntegrationCommandsMixin

    handler = IntegrationCommandsMixin()
    handler.db = seeded_db
    before = await seeded_db.get_integration_subject("subject")
    result = await handler._cmd_integration_shadow_report(
        {"project_id": "p", "since": START, "until": START + 8 * DAY}
    )
    assert result["success"] is True and result["outcome"] == "report"
    assert result["legacy_decisions"] == 4
    assert result["agreements"] == 2 and result["unrouted_decisions"] == 2
    assert result["divergences"] == 2 and result["missing_comparisons"] == 0
    assert result["cleared_for_review"] is False
    assert result["digest"].startswith("sha256:")
    assert result["report"]["sources"][0]["key"] == "sealed_batch"
    assert result["markdown"].startswith("# Shadow comparison report")
    assert await seeded_db.get_integration_subject("subject") == before

def test_requested_week_cannot_substitute_for_recorded_shadow_time():
    report = compare(
        'p', WEEK, [legacy(day=0), legacy(identity='late', day=7)],
        [shadow(day=1)], engines={'subject': 'legacy'},
    )
    assert report.observed_span_seconds == 7 * DAY
    assert not report.cleared_for_review
    assert any('recorded shadow' in reason for reason in report.blocking_reasons)


def test_report_digest_binds_ownership_and_unknown_acknowledgement():
    observations = [shadow(seq=1, day=0), shadow(seq=2, day=7, unknown=('missing',))]
    args = ('p', WEEK, [legacy(day=1)], observations)
    unacknowledged = compare(*args, engines={'subject': 'legacy'})
    acknowledged = compare(*args, engines={'subject': 'legacy'}, acknowledged_unknowns=[2])
    transferred = compare(*args, engines={'subject': 'reconciler'}, acknowledged_unknowns=[2])
    assert acknowledged.cleared_for_review
    assert not transferred.cleared_for_review
    assert len({unacknowledged.digest, acknowledged.digest, transferred.digest}) == 3
