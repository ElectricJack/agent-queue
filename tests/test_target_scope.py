"""Per-project elevated tokens may run target-keyed commands (azure-journey-72).

The bug: ``check_command_scope`` injected ``project_id`` into every
per-project elevated call, and every ``CommandArgs`` model sets
``extra="forbid"``, so ``aq integration reserve-owner`` died with

    invalid reservation request: 1 validation error for
    IntegrationReserveOwnerArgs
    project_id  Extra inputs are not permitted [type=extra_forbidden]

for the one caller the runbooks give that recovery to.  The fix stops injecting
a key the contract forbids and moves the project's isolation onto the target:
:mod:`src.api.target_scope` resolves the row the command names and compares its
project with the token's, failing closed whenever it cannot.

Under test are the two layers that failed to compose — the pure scope gate and
the request-level gate that has a database — plus one end-to-end dispatch
through ``CommandHandler.execute`` for the exact reproduction.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy import insert

from src.api.auth import LOCAL_SCOPE, RequestScope
from src.api.scope import _forbids_project_id, check_command_scope, check_request_scope
from src.api.target_scope import (
    TARGET_REFERENCE_NAMES,
    TARGET_REFERENCE_SUFFIX,
    TARGET_RESOLVERS,
    VALUE_ARGUMENTS,
    target_references,
    target_scope_error,
    unresolvable_targets,
)
from src.commands.contracts import CONTRACTS
from src.commands.contracts.models import CommandArgs
from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.database.tables import (
    gates,
    integration_batch_members,
    integration_batches,
    integration_branch_owners,
    integration_candidate_member_results,
    integration_candidate_resolutions,
    integration_candidate_revisions,
    integration_parent_episodes,
    integration_promotion_intents,
    integration_repair_operations,
    integration_repair_stages,
    integration_review_evidence,
    integration_subjects,
    playbook_artifacts,
    workspaces,
)
from src.models import (
    AgentProfile,
    Project,
    RepoConfig,
    RepoSourceType,
    SessionRecord,
    Task,
)
from src.profiles.capabilities import DENY_ALL

SUPERVISOR = "sup-p"
SUPERVISOR_GLOBAL = "sup-global"


def _scope(project_id: str | None, *, elevated: bool = True, session_id: str = SUPERVISOR):
    return RequestScope(
        kind="session", session_id=session_id, task_id=None, project_id=project_id,
        elevated=elevated,
    )


def _elevated_scope_envelope(project_id: str | None) -> dict:
    """What ``/api/execute`` forwards to ``execute`` as the trusted envelope."""
    return {
        "kind": "session",
        "session_id": SUPERVISOR_GLOBAL if project_id is None else SUPERVISOR,
        "task_id": None,
        "project_id": project_id,
        "elevated": True,
    }


def _principal(project_id: str | None) -> ExecutionPrincipal:
    return ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=DENY_ALL,
        session_id=SUPERVISOR,
        project_id=project_id,
        elevated=True,
    )


@pytest.fixture
async def db(reuse_database):
    """Two projects, each with a task, a repo, a batch, a parent operation,
    a candidate reservation, a branch owner row and an integration subject/gate."""
    database = await reuse_database("target-scope")
    await database.create_project(Project(id="p", name="Project"))
    await database.create_project(Project(id="other", name="Other project"))
    await database.create_profile(
        AgentProfile(
            id="supervisor",
            name="Supervisor",
            harness="codex",
            lifecycle="named",
            aq_commands=[],
            harness_tools=[],
            plugin_tools=[],
            needs_workspace=False,
        )
    )
    for session_id, project_id in (
        (SUPERVISOR, "p"),
        (SUPERVISOR_GLOBAL, None),
        ("delegate", "p"),
    ):
        await database.create_session(
            SessionRecord(
                id=session_id,
                project_id=project_id,
                profile_id="supervisor",
                harness="codex",
                provider="fake",
                name=session_id,
                lifecycle="named",
                work_dir=f"/tmp/{session_id}",
                epoch="epoch",
                instance_token=f"token-{session_id}",
                started_at=time.time(),
                state="running",
                desired_state="running",
            )
        )
    for task_id, project_id in (("own", "p"), ("foreign", "other")):
        await database.create_task(
            Task(id=task_id, project_id=project_id, title=task_id, description="")
        )
    for repo_id, project_id in (("repo-p", "p"), ("repo-other", "other")):
        await database.create_repo(
            RepoConfig(
                id=repo_id,
                project_id=project_id,
                url=f"https://example.test/{repo_id}.git",
                source_type=RepoSourceType.CLONE,
            )
        )
    async with database._engine.begin() as conn:
        artifact = "sha256:" + "5" * 64
        await conn.execute(
            insert(playbook_artifacts).values(
                artifact_sha256=artifact,
                playbook_id="parent-integration",
                source_digest="sha256:" + "6" * 64,
                contract_fingerprint="sha256:" + "7" * 64,
                compiler_build="test",
                path="/test/parent.json",
                created_at=1.0,
            )
        )
        for project_id, task_id, repository_id in (
            ("p", "own", "repo-p"),
            ("other", "foreign", "repo-other"),
        ):
            await conn.execute(
                insert(gates).values(
                    id=f"gate-{project_id}",
                    project_id=project_id,
                    gate_type="human",
                    title="Hold parent integration",
                    created_at=1.0,
                )
            )
            await conn.execute(
                insert(integration_subjects).values(
                    id=f"subject-{project_id}",
                    project_id=project_id,
                    repository_id=repository_id,
                    kind="parent_episode",
                    subject_key=task_id,
                    phase="building",
                    policy_playbook_id="parent-integration",
                    policy_artifact_sha256=artifact,
                    task_id=task_id,
                    gate_id=f"gate-{project_id}",
                    due_set_at=1.0,
                    max_wait_seconds=3600,
                    created_at=1.0,
                    updated_at=1.0,
                )
            )
        await conn.execute(
            insert(workspaces).values(
                id="ws",
                project_id="p",
                workspace_path="/tmp/ws",
                source_type="link",
                enabled=True,
                created_at=1.0,
            )
        )
        for batch_id, project_id, repository_id in (
            ("batch-p", "p", "repo-p"),
            ("batch-other", "other", "repo-other"),
        ):
            await conn.execute(
                insert(integration_batches).values(
                    id=batch_id,
                    project_id=project_id,
                    repository_id=repository_id,
                    request_id="req",
                    trigger="manual",
                    source_manifest_digest="sha256:" + "4" * 64,
                    base_sha="a" * 40,
                    lifecycle="sealing",
                    current_revision=0,
                    integration_branch=f"refs/heads/aq/integration/{project_id}/1",
                    policy_snapshot={},
                    artifact_snapshot={},
                    cleanup_state="pending",
                    created_at=1.0,
                    updated_at=1.0,
                )
            )
        # A parent-targeted repair operation reaches its project through the
        # task its ``target_kind`` names, which is the hop that is easy to get
        # wrong and the reason the resolver is shared with the handler.
        for task_id, repository_id in (("own", "repo-p"), ("foreign", "repo-other")):
            await conn.execute(
                insert(integration_parent_episodes).values(
                    id=f"episode-{task_id}",
                    parent_task_id=task_id,
                    repository_id=repository_id,
                    generation=1,
                    pre_collection_checkpoint_sha="b" * 40,
                    created_at=1.0,
                )
            )
        for operation_id, task_id in (("op-p", "own"), ("op-other", "foreign")):
            await conn.execute(
                insert(integration_repair_operations).values(
                    id=operation_id,
                    target_kind="parent",
                    batch_id=None,
                    parent_task_id=task_id,
                    episode_id=f"episode-{task_id}",
                    active_stage=0,
                    state="active",
                    policy_snapshot={},
                    artifact_snapshot={},
                    required_check_version="v1",
                    created_at=1.0,
                    updated_at=1.0,
                )
            )
        await conn.execute(
            insert(integration_repair_operations).values(
                id="op-missing-batch",
                target_kind="batch",
                batch_id="missing-batch",
                episode_id="missing-episode",
                active_stage=0,
                state="active",
                policy_snapshot={},
                artifact_snapshot={},
                required_check_version="v1",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        # A candidate reservation hangs off a built revision's member result,
        # which hangs off the batch's reviewed member.  Seeding the chain is the
        # price of proving ``reservation_id`` against a real row rather than a
        # mock: it is one of the four families the task names.
        for batch_id, task_id, repository_id in (
            ("batch-p", "own", "repo-p"),
            ("batch-other", "foreign", "repo-other"),
        ):
            await conn.execute(
                insert(integration_review_evidence).values(
                    id=f"evidence-{batch_id}",
                    source_task_id=task_id,
                    repository_id=repository_id,
                    source_base="a" * 40,
                    reviewed_head_sha="b" * 40,
                    reviewed_tree_sha="c" * 40,
                    review_kind="source",
                    generation=1,
                    verdict="approved",
                    evidence={},
                    created_at=1.0,
                )
            )
            await conn.execute(
                insert(integration_batch_members).values(
                    batch_id=batch_id,
                    ordinal=0,
                    task_id=task_id,
                    repository_id=repository_id,
                    source_base_sha="a" * 40,
                    reviewed_head_sha="b" * 40,
                    reviewed_tree_sha="c" * 40,
                    review_evidence_id=f"evidence-{batch_id}",
                    review_evidence={},
                )
            )
            await conn.execute(
                insert(integration_candidate_revisions).values(
                    batch_id=batch_id,
                    revision=0,
                    construction_base_sha="a" * 40,
                    source_manifest={},
                    next_member_ordinal=1,
                    head_sha="b" * 40,
                    state="testing",
                    created_at=1.0,
                    updated_at=1.0,
                )
            )
            await conn.execute(
                insert(integration_candidate_member_results).values(
                    batch_id=batch_id,
                    revision=0,
                    member_ordinal=0,
                    input_head_sha="b" * 40,
                    input_tree_sha="c" * 40,
                    result="applied",
                    generated_squash_sha="d" * 40,
                    created_at=1.0,
                    updated_at=1.0,
                )
            )
        for operation_id in ("op-p", "op-other"):
            await conn.execute(
                insert(integration_repair_stages).values(
                    operation_id=operation_id,
                    ordinal=0,
                    policy={},
                    starting_sha="b" * 40,
                    attempts=0,
                    state="active",
                )
            )
        for reservation_id, batch_id, project_id, operation_id, repository_id in (
            ("res-p", "batch-p", "p", "op-p", "repo-p"),
            ("res-other", "batch-other", "other", "op-other", "repo-other"),
        ):
            await conn.execute(
                insert(integration_candidate_resolutions).values(
                    id=reservation_id,
                    batch_id=batch_id,
                    revision=0,
                    member_ordinal=0,
                    operation_id=operation_id,
                    operation_episode_id="episode",
                    stage_ordinal=0,
                    stage_deadline_at=1000.0,
                    project_id=project_id,
                    repair_task_id="own",
                    repair_session_id="delegate",
                    repair_session_instance_token="token",
                    repair_workspace_id="ws",
                    repair_workspace_path="/tmp/ws",
                    repository_id=repository_id,
                    branch="refs/heads/aq/task",
                    target_branch="refs/heads/main",
                    target_kind="qualified",
                    fence_owner_id="owner",
                    fence_token=1,
                    partial_head_sha="c" * 40,
                    source_base_sha="a" * 40,
                    source_head_sha="b" * 40,
                    resolved_head_sha="d" * 40,
                    resolved_tree_sha="e" * 40,
                    repair_commit_shas=["d" * 40],
                    state="reserved",
                    created_at=1.0,
                    updated_at=1.0,
                )
            )
        for row_id, repository_id in (
            ("owner-p", "repo-p"),
            ("owner-other", "repo-other"),
            ("owner-missing-repo", "missing-repo"),
        ):
            await conn.execute(
                insert(integration_branch_owners).values(
                    id=row_id,
                    repository_id=repository_id,
                    ref="refs/heads/aq/task",
                    owner_id="agent",
                    owner_role="producer",
                    fence_token=1,
                    handoff_state="attached",
                    created_at=1.0,
                    updated_at=1.0,
                )
            )
        for intent_id, project_id, repository_id in (
            ("intent-p", "p", "repo-p"),
            ("intent-other", "other", "repo-other"),
            ("intent-null-p", None, "repo-p"),
            ("intent-null-other", None, "repo-other"),
            ("intent-null-missing-repo", None, "missing-repo"),
        ):
            await conn.execute(
                insert(integration_promotion_intents).values(
                    id=intent_id,
                    domain_key=intent_id,
                    project_id=project_id,
                    receipt_id=f"receipt-{intent_id}",
                    source_head="b" * 40,
                    source_base="a" * 40,
                    repository_id=repository_id,
                    target_branch="refs/heads/main",
                    expected_target="c" * 40,
                    fence_owner_id="owner",
                    fence_token=1,
                    state="conflict",
                    created_at=1.0,
                    updated_at=1.0,
                )
            )
    return database


# ---------------------------------------------------------------------------
# The reproduction: reserve-owner under a per-project supervisor token
# ---------------------------------------------------------------------------


async def test_reserve_owner_on_its_own_task_reaches_the_handler(
    db, command_handler_factory, monkeypatch
):
    """The exact call that failed, end to end through ``execute``."""
    handler = await command_handler_factory()
    handler.orchestrator.db = db
    handler.config.security.capability_enforcement = "off"
    reserve = AsyncMock(return_value={"outcome": "acquired", "task_id": "own"})
    monkeypatch.setattr(
        "src.integration.canonical_reservation.reserve_canonical_task_branch", reserve
    )

    args = {"task_id": "own"}
    assert await check_request_scope("integration_reserve_owner", args, _scope("p"), db=db) is None
    assert "project_id" not in args, "the gate must not write a key the contract forbids"

    with principal_context(_principal("p")):
        result = await handler.execute(
            "integration_reserve_owner",
            {"task_id": "own", "_scope": _elevated_scope_envelope("p")},
        )

    assert "error" not in result, result
    assert result["outcome"] == "acquired"
    reserve.assert_awaited_once_with(db, "own")

    # Negative control: the handler still validates its own args, so the pass
    # above is the gate no longer injecting a forbidden key — not the contract
    # being loosened to accommodate it.
    with principal_context(_principal("p")):
        sloppy = await handler.execute(
            "integration_reserve_owner",
            {"task_id": "own", "not_an_arg": 1, "_scope": _elevated_scope_envelope("p")},
        )
    assert "Extra inputs are not permitted" in sloppy["error"]


async def test_reserve_owner_on_a_foreign_task_is_refused_at_the_scope_layer(db):
    args = {"task_id": "foreign"}

    error = await check_request_scope("integration_reserve_owner", args, _scope("p"), db=db)

    assert error is not None
    assert "another project" in error
    assert args == {"task_id": "foreign"}, "a refused call is given no injected key"


#: The four families the task names, each on this project's row and on another
#: project's, so "resolves the target" is proven per resolver rather than once.
_FOREIGN_TARGETS = [
    ("integration_reserve_owner", {"task_id": "foreign"}),
    ("integration_reopen_collection", {"task_id": "foreign", "reason": "recover"}),
    ("integration_reevaluate_repair", {"operation_id": "op-other"}),
    ("integration_release", {"batch_id": "batch-other"}),
    ("integration_recover_candidate_member", {"reservation_id": "res-other"}),
    ("integration_release_owner", {"owner_row_id": "owner-other"}),
    ("integration_eject", {"batch_id": "batch-other", "task_id": "foreign", "reason": "x"}),
    ("integration_release_held_gate", {"subject_id": "subject-other", "gate_id": "gate-p"}),
    ("integration_release_held_gate", {"subject_id": "subject-p", "gate_id": "gate-other"}),
]
_OWN_TARGETS = [
    ("integration_reserve_owner", {"task_id": "own"}),
    ("integration_reopen_collection", {"task_id": "own", "reason": "recover"}),
    ("integration_reevaluate_repair", {"operation_id": "op-p"}),
    ("integration_release", {"batch_id": "batch-p"}),
    ("integration_recover_candidate_member", {"reservation_id": "res-p"}),
    ("integration_release_owner", {"owner_row_id": "owner-p"}),
    ("integration_eject", {"batch_id": "batch-p", "task_id": "own", "reason": "x"}),
    ("integration_release_held_gate", {"subject_id": "subject-p", "gate_id": "gate-p"}),
]


@pytest.mark.parametrize(("command", "args"), _FOREIGN_TARGETS)
async def test_a_foreign_target_is_refused_whichever_key_names_it(db, command, args):
    error = await check_request_scope(command, dict(args), _scope("p"), db=db)

    assert error is not None, f"{command} admitted {args}"
    assert "another project" in error


@pytest.mark.parametrize(("command", "args"), _OWN_TARGETS)
async def test_the_same_call_on_this_projects_target_is_admitted(db, command, args):
    assert await check_request_scope(command, dict(args), _scope("p"), db=db) is None


async def test_held_gate_release_still_requires_a_human_after_project_scope_passes(
    db, command_handler_factory, monkeypatch
):
    handler = await command_handler_factory()
    handler.orchestrator.db = db
    handler.config.security.capability_enforcement = "off"
    release = AsyncMock()
    monkeypatch.setattr("src.integration.gates.GatePrimitives.release_hold", release)
    args = {"subject_id": "subject-p", "gate_id": "gate-p"}
    before = await db.get_integration_subject("subject-p")
    assert await check_request_scope(
        "integration_release_held_gate", args, _scope("p"), db=db
    ) is None

    with principal_context(_principal("p")):
        result = await handler.execute(
            "integration_release_held_gate", {**args, "_scope": _elevated_scope_envelope("p")}
        )

    assert result["success"] is False
    assert result["outcome"] == "unauthorized"
    assert result["error"] == "a verified human operator is required"
    release.assert_not_awaited()
    assert await db.get_integration_subject("subject-p") == before


# ---------------------------------------------------------------------------
# The whole surface: no injected key a contract forbids, for any command
# ---------------------------------------------------------------------------


def _commands_without_project_id() -> list[str]:
    return sorted(name for name in CONTRACTS.names() if _forbids_project_id(name))


def test_the_derivation_is_not_vacuously_small():
    derived = set(_commands_without_project_id())
    # The families the task names, and the registry's size today.
    assert {
        "integration_reserve_owner",
        "integration_release_owner",
        "integration_reopen_collection",
    } <= derived
    assert len(derived) > 40


def test_extra_forbid_is_untouched_on_every_contract():
    """The mechanism is target resolution, never a looser model."""
    for name in CONTRACTS.names():
        model = CONTRACTS.get(name).contract.execution.args_model
        assert issubclass(model, CommandArgs)
        assert model.model_config.get("extra") == "forbid", name


def test_the_scope_gate_injects_project_id_only_where_the_contract_declares_it():
    """No contract that lacks the field ever has it written into its args.

    Some of these commands are refused earlier by another gate (the
    conversation inbox is global-only), which is why the assertion is about the
    injection rather than about the verdict.
    """
    scope = _scope("p")

    for name in _commands_without_project_id():
        args: dict = {}
        check_command_scope(name, args, scope)
        assert "project_id" not in args, name

    declared: dict = {}
    assert check_command_scope("integration_release_stale_owners", declared, scope) is None
    assert declared == {"project_id": "p"}


def _plausible_args(command: str) -> dict:
    """*command*'s required arguments, filled with ids this project's rows own.

    Optional id arguments are left out: the contract says what a call must
    carry, and a real caller supplies what it has.
    """
    known = {
        "depends_on": ["own"],
        "task_id": "own",
        "parent_task_id": "own",
        "child_task_id": "own",
        "source_task_id": "own",
        "parent_id": "own",
        "operation_id": "op-p",
        "batch_id": "batch-p",
        "reservation_id": "res-p",
        "owner_row_id": "owner-p",
        "intent_id": "intent-p",
        "session_id": SUPERVISOR,
        "repository_id": "repo-p",
        "subject_id": "subject-p",
        "gate_id": "gate-p",
    }
    model = CONTRACTS.get(command).contract.execution.args_model
    return {
        name: known.get(name, f"value-{name}")
        for name, spec in model.model_fields.items()
        if spec.is_required()
    }


@pytest.mark.parametrize("command", _commands_without_project_id())
async def test_no_target_keyed_command_dies_on_an_injected_project_id(db, command):
    """Every registered command whose args model lacks ``project_id``, run under
    a per-project elevated scope.

    Whatever the scope layer answers, the args it hands on must survive the
    contract's own ``extra="forbid"``.  A refusal is a legitimate answer — a
    global-only surface, or a target this module cannot resolve to an owner —
    but an injected key is not, and that is the bug this task closed.
    """
    args = _plausible_args(command)

    error = await check_request_scope(command, args, _scope("p"), db=db)

    if error is not None:
        assert error.startswith("out of scope"), error
        return
    model = CONTRACTS.get(command).contract.execution.args_model
    try:
        model.model_validate(args)
    except ValidationError as exc:
        forbidden = [
            item["loc"] for item in exc.errors() if item["type"] == "extra_forbidden"
        ]
        assert not forbidden, f"{command} refuses scope-injected {forbidden}"


#: Commands whose required target references this module deliberately cannot
#: resolve, so a per-project supervisor is refused rather than admitted on a
#: partial check.  Each is here for a stated reason; a new one is a gap in
#: :data:`TARGET_RESOLVERS`, not an oversight in the policy.
_UNRESOLVED_TARGETS = {
    "escalation_apply_reply": ["target_id"],  # an action's target is polymorphic
    "integration_repair_start": ["trigger_id"],  # a daemon event, not a project row
    "integration_transfer_owner": ["next_owner_id"],  # an agent, resolved in-handler
    "report_brief": ["request_id"],  # report requests carry no project
    "report_get": ["report_id"],  # reports are keyed by scope, not by project
    "report_request": ["request_id"],
    "report_submit": ["request_id"],
    "supervisor_inbox_reply": ["conversation_id", "input_id"],  # daemon-internal
}


def test_the_refused_surface_is_exactly_the_one_that_is_declared():
    """Guard the guard: a new command cannot inherit an unverifiable target.

    Every ``_id``-shaped argument a contract *requires* must be resolvable here
    or be declared a value in :data:`VALUE_ARGUMENTS`.  Anything else makes
    :func:`target_scope_error` refuse the command, which is the safe answer but
    not a working one — so the refusals are pinned as an exact set.
    """
    unresolvable: dict[str, list[str]] = {}
    for name in _commands_without_project_id():
        model = CONTRACTS.get(name).contract.execution.args_model
        args = {
            field: "x"
            for field, spec in model.model_fields.items()
            if spec.is_required() and field not in VALUE_ARGUMENTS
        }
        missing = unresolvable_targets(args)
        if missing:
            unresolvable[name] = missing
    assert unresolvable == _UNRESOLVED_TARGETS


def test_the_resolver_table_names_only_row_references():
    """A resolver key must be a reference :func:`target_references` recognises."""
    for name in TARGET_RESOLVERS:
        assert name.endswith(TARGET_REFERENCE_SUFFIX) or name in TARGET_REFERENCE_NAMES, name
    assert not set(TARGET_RESOLVERS) & VALUE_ARGUMENTS


# ---------------------------------------------------------------------------
# The policy itself
# ---------------------------------------------------------------------------


async def test_a_command_naming_no_target_is_refused(db):
    error = await target_scope_error("list_projects", {}, "p", db=db)

    assert error is not None
    assert "names no project-owned target" in error


@pytest.mark.parametrize("project_id", ["p", "other"])
async def test_provider_preview_resolves_its_nested_project_target(db, project_id):
    args = {
        "provider": "codex",
        "receive_new_work": {"project_id": project_id, "mode": "prefer"},
    }
    error = await check_request_scope(
        "provider_allocation_preview", args, _scope("p"), db=db
    )

    assert "project_id" not in args
    if project_id == "p":
        assert error is None
    else:
        assert "receive_new_work.project_id belongs to other" in error


@pytest.mark.parametrize("command", ["list_projects", "provider_allocation_status"])
async def test_other_commands_cannot_borrow_the_provider_preference_target(db, command):
    args = {"receive_new_work": {"project_id": "p", "mode": "prefer"}}

    error = await target_scope_error(command, args, "p", db=db)

    assert error is not None
    assert "names no project-owned target" in error


async def test_provider_preview_does_not_search_arbitrary_nested_values(db):
    args = {"changes": {"receive_new_work": {"project_id": "p", "mode": "prefer"}}}

    error = await target_scope_error("provider_allocation_preview", args, "p", db=db)

    assert error is not None
    assert "names no project-owned target" in error


@pytest.mark.parametrize("command", ["provider_allocation_preview", "list_projects"])
async def test_a_dotted_argument_cannot_impersonate_the_nested_project_target(db, command):
    error = await target_scope_error(
        command, {"receive_new_work.project_id": "p"}, "p", db=db
    )

    assert error is not None
    assert "names no project-owned target" in error


async def test_a_target_that_cannot_be_resolved_to_an_owner_is_refused(db):
    error = await target_scope_error(
        "integration_transfer_owner", {"next_owner_id": "agent-1"}, "p", db=db
    )

    assert error is not None
    assert "cannot be resolved" in error
    assert "next_owner_id" in error


async def test_a_missing_database_refuses_rather_than_admits():
    error = await target_scope_error(
        "integration_reserve_owner", {"task_id": "own"}, "p", db=None
    )

    assert error is not None
    assert "without a database" in error


async def test_a_target_row_that_is_gone_names_no_target(db):
    """An id that matches no row is never a pass: the call names no target.

    Admitting it trusted the handler to read the id exactly as this check did;
    one that strips or splits ``"foreign,"`` acted on a row nobody looked up.
    """
    error = await target_scope_error(
        "integration_reserve_owner", {"task_id": "gone"}, "p", db=db
    )

    assert error == (
        "out of scope: integration_reserve_owner names no project-owned target "
        "(task_id 'gone' match no row), so a token scoped to project p may not run it"
    )


async def test_a_gone_row_beside_this_projects_target_is_left_to_the_handler(db):
    """An unmatched id next to a resolving one adds no project claim."""
    assert (
        await target_scope_error(
            "add_dependency", {"task_id": "own", "depends_on": ["gone"]}, "p", db=db
        )
        is None
    )


async def test_a_session_with_null_project_is_refused(db):
    error = await check_request_scope(
        "integration_repair_close_current",
        {"operation_id": "op-p", "session_id": SUPERVISOR_GLOBAL},
        _scope("p"),
        db=db,
    )

    assert error == (
        "out of scope: integration_repair_close_current targets a row owned by no project"
    )


@pytest.mark.parametrize(
    "command", ["integration_reconcile_promotion", "integration_push_conflict_resolution"]
)
@pytest.mark.parametrize(
    ("intent_id", "expected_error"),
    [
        ("intent-p", None),
        ("intent-other", "targets another project (intent_id belongs to other)"),
        ("intent-null-p", None),
        ("intent-null-other", "targets another project (intent_id belongs to other)"),
        ("intent-null-missing-repo", "targets a row owned by no project"),
    ],
)
async def test_promotion_intent_ownership_uses_repository_when_project_is_null(
    db, command, intent_id, expected_error
):
    error = await check_request_scope(command, {"intent_id": intent_id}, _scope("p"), db=db)

    assert error == (None if expected_error is None else f"out of scope: {command} {expected_error}")


@pytest.mark.parametrize("argument", sorted(TARGET_RESOLVERS))
async def test_a_missing_target_names_no_target_for_every_resolver(db, argument):
    if argument == "receive_new_work.project_id":
        command = "provider_allocation_preview"
        args = {"receive_new_work": {"project_id": "gone", "mode": "prefer"}}
    else:
        command = "missing_target"
        args = {argument: "gone"}
    error = await target_scope_error(command, args, "p", db=db)

    assert error is not None
    assert f"{argument} 'gone' match no row" in error




async def test_a_missing_task_reaches_the_handler_and_returns_not_found(
    db, command_handler_factory
):
    handler = await command_handler_factory()
    handler.orchestrator.db = db
    handler.config.security.capability_enforcement = "off"

    with principal_context(_principal("p")):
        result = await handler.execute(
            "integration_reserve_owner",
            {"task_id": "gone", "_scope": _elevated_scope_envelope("p")},
        )

    assert result["success"] is False
    assert result["outcome"] == "not_found"
    assert result["error"] == "task does not exist"


# --- provider_reroute / provider_reroute_undo (quick-crest-28) -------------------
#
# Neither command's contract declares the task ids its handler reads, and the
# handler splits and strips them.  The gate validates against the handler's own
# model first, so the ids the target check resolves are the ids that move.


@pytest.mark.parametrize(
    "task_id",
    [
        "foreign,",
        " foreign",
        "foreign ",
        "own foreign",
        "own,foreign",
        ["foreign,"],
        ["own", " foreign"],
    ],
)
async def test_provider_reroute_refuses_a_foreign_task_however_it_is_spelled(db, task_id):
    args = {"task_id": task_id}

    error = await check_request_scope("provider_reroute", args, _scope("p"), db=db)

    assert error == (
        "out of scope: provider_reroute targets another project (task_id belongs to other)"
    )


@pytest.mark.parametrize(
    ("command", "args", "refused"),
    [
        ("provider_reroute", {"task_ids": ["foreign"]}, "task_ids"),
        ("provider_reroute", {"task_ids": ["foreign"], "job_id": "made-up"}, "task_ids"),
        ("provider_reroute", {"task_id": "own", "project_id": "p"}, "project_id"),
        ("provider_reroute", {"task_id": 5}, "task_id"),
        ("provider_reroute_undo", {"task_ids": ["foreign"]}, "task_ids"),
        ("provider_reroute_undo", {"batch": "b-1"}, "batch"),
    ],
)
async def test_reroute_arguments_no_model_declares_are_refused_before_the_target_check(
    db, command, args, refused
):
    error = await check_request_scope(command, dict(args), _scope("p"), db=db)

    assert error is not None
    assert error.startswith(f"out of scope: invalid {command} arguments (")
    assert f"{refused}:" in error


@pytest.mark.parametrize("task_id", ["own,", " own", ["own, "]])
async def test_provider_reroute_forwards_the_ids_the_target_check_resolved(db, task_id):
    args = {"task_id": task_id, "dry_run": True, "provider": None}

    assert await check_request_scope("provider_reroute", args, _scope("p"), db=db) is None
    assert args == {"task_id": ["own"], "dry_run": True}


@pytest.mark.parametrize("task_id", ["gone", "gone,", ["gone", "also-gone"]])
async def test_provider_reroute_naming_only_unknown_tasks_is_refused(db, task_id):
    error = await check_request_scope(
        "provider_reroute", {"task_id": task_id}, _scope("p"), db=db
    )

    assert error is not None
    assert "names no project-owned target" in error


async def test_a_per_project_provider_reroute_sweep_names_no_target(db):
    error = await check_request_scope("provider_reroute", {}, _scope("p"), db=db)

    assert error is not None
    assert "names no project-owned target" in error


async def test_provider_reroute_undo_forwards_canonical_ids_to_its_handler(db):
    """No contract, so the gate injects ``project_id``; the handler confines."""
    args = {"task_id": "foreign,"}

    assert await check_request_scope("provider_reroute_undo", args, _scope("p"), db=db) is None
    assert args == {"task_id": ["foreign"], "project_id": "p"}


async def test_the_local_operator_sees_invalid_reroute_arguments_from_the_handler():
    args = {"task_ids": ["anything"]}

    assert await check_request_scope("provider_reroute", args, LOCAL_SCOPE) is None
    assert args == {"task_ids": ["anything"]}


async def test_every_target_named_is_checked_not_just_the_first(db):
    """The batch is this project's; the task is not.  Both are checked."""
    error = await target_scope_error(
        "integration_eject", {"batch_id": "batch-p", "task_id": "foreign"}, "p", db=db
    )

    assert error is not None
    assert "task_id belongs to other" in error


async def test_a_list_valued_reference_is_checked_element_by_element(db):
    assert (
        await target_scope_error(
            "add_dependency", {"task_id": "own", "depends_on": ["foreign"]}, "p", db=db
        )
        is not None
    )
    assert (
        await target_scope_error(
            "add_dependency", {"task_id": "own", "depends_on": ["own"]}, "p", db=db
        )
        is None
    )


def test_target_references_classify_by_argument_name():
    args = {
        "task_id": "t",
        "depends_on": ["a", "b"],
        "external_message_id": "discord-1",
        "fence": 7,
        "reason": "why",
        "session_id": None,
        "owner": "agent",
    }

    assert target_references(args) == {"task_id": ["t"], "depends_on": ["a", "b"]}


# ---------------------------------------------------------------------------
# Scopes that must not move
# ---------------------------------------------------------------------------


async def test_the_global_supervisor_is_unaffected(db):
    """``project_id=None`` returns before any of this: it owns every project."""
    args = {"task_id": "foreign"}

    assert await check_request_scope("integration_reserve_owner", args, _scope(None), db=db) is None
    assert "project_id" not in args


async def test_a_contract_that_declares_project_id_still_gets_it_injected(db):
    args: dict = {}

    error = await check_request_scope(
        "integration_release_stale_owners", args, _scope("p"), db=db
    )

    assert error is None
    assert args == {"project_id": "p"}


async def test_a_foreign_project_id_is_still_a_mismatch_on_a_declaring_contract(db):
    args = {"project_id": "other"}

    assert (
        await check_request_scope("integration_release_stale_owners", args, _scope("p"), db=db)
        == "out of scope: project_id mismatch"
    )


async def test_a_worker_token_still_gets_the_injected_id_triple():
    """The agent branch is unchanged: handlers there read the injected key."""
    args = {"task_id": "own"}

    assert check_command_scope("task_show", args, _scope("p", elevated=False)) is None
    assert args == {"task_id": "own", "project_id": "p", "session_id": SUPERVISOR}


def test_the_local_operator_bypasses_both_gates():
    """Loopback is trusted: no injection to make and no target to resolve."""
    args: dict = {}

    assert check_command_scope("integration_reserve_owner", args, RequestScope(kind="local")) is None
    assert args == {}
