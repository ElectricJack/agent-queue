"""Bounded durable integration reconciliation service."""

from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert

from src.database.tables import (
    integration_batches,
    integration_candidate_revisions,
    integration_promotion_intents,
    integration_repair_operations,
    integration_repair_stages,
    project_integration_schedules,
    projects,
)
from src.integration import outbox as outbox_module
from src.integration.models import (
    DEFAULT_INTEGRATION_MAX_WAIT_SECONDS,
    HierarchicalIntegrationPolicy,
    integration_ci_policy,
    integration_ci_sources,
    integration_max_wait_seconds,
)
from src.integration.service import IntegrationService as _IntegrationService


class IntegrationService(_IntegrationService):
    """Existing synthetic-time scenarios explicitly own their operation clock."""

    def __init__(self, *args, **kwargs):
        self._test_now = 0.0
        kwargs.setdefault("clock", lambda: self._test_now)
        super().__init__(*args, **kwargs)

    async def tick(self, now, **kwargs):
        self._test_now = now
        await super().tick(now, **kwargs)


@pytest.fixture
async def db(tmp_path, reuse_database):
    database = await reuse_database("integration-service.db")
    yield database










async def test_due_schedule_keyset_pages_every_row_once_past_two_hundred(db):
    count = 205
    async with db.immediate() as conn:
        await conn.execute(
            insert(projects),
            [
                {
                    "id": f"p-{ordinal:03d}",
                    "name": f"Project {ordinal}",
                    "status": "ACTIVE",
                    "hierarchical_integration_mode": "train",
                    "created_at": 1.0,
                }
                for ordinal in range(count)
            ],
        )
        await conn.execute(
            insert(project_integration_schedules),
            [
                {
                    "project_id": f"p-{ordinal:03d}",
                    "enabled": True,
                    "interval_seconds": 30,
                    "next_due_at": float(ordinal % 7),
                    "updated_at": 1.0,
                }
                for ordinal in range(count)
            ],
        )
        await conn.execute(
            insert(projects).values(
                id="disabled-project",
                name="Disabled",
                status="ACTIVE",
                hierarchical_integration_mode="disabled",
                created_at=1.0,
            )
        )
        await conn.execute(
            insert(project_integration_schedules).values(
                project_id="disabled-project",
                enabled=True,
                interval_seconds=30,
                next_due_at=0.0,
                updated_at=1.0,
            )
        )

    rows: list[dict] = []
    after = None
    while True:
        page = await db.due_integration_schedule_page(now=10.0, after=after, limit=7)
        if not page:
            break
        rows.extend(page)
        after = (page[-1]["next_due_at"], page[-1]["project_id"])

    assert len(rows) == count
    assert len({row["project_id"] for row in rows}) == count
    assert [(row["next_due_at"], row["project_id"]) for row in rows] == sorted(
        (row["next_due_at"], row["project_id"]) for row in rows
    )
    assert "disabled-project" not in {row["project_id"] for row in rows}
    with pytest.raises(ValueError, match="positive"):
        await db.due_integration_schedule_page(now=10.0, after=None, limit=0)


def test_policy_uses_compatible_rebuild_and_cleanup_defaults():
    artifact = {
        "playbook_id": "root-integration",
        "artifact_sha256": "sha256:" + "a" * 64,
        "schema_generation": 2,
        "contract_fingerprint": "sha256:" + "b" * 64,
        "source_digest": "sha256:" + "c" * 64,
        "compiler_build": "test",
        "compiled_at": "2026-09-05T00:00:00Z",
        "version": 1,
    }
    boundary = {
        "required_checks": {"version": "v1", "names": ["unit"], "producer_id": "forge"},
        "repair": {"debug_intelligence_class": "debug-high"},
        "route": {
            "playbook_id": "root-integration",
            "scope": "project",
            "scope_identifier": "p",
            "artifact": artifact,
        },
    }
    values = {
        "parent": boundary,
        "root": boundary,
        "branchless_parent": "verifier",
        "on_failed_child": "block",
    }
    policy = HierarchicalIntegrationPolicy.model_validate(values)

    assert policy.on_main_moved == "rebuild"
    assert policy.cleanup.model_dump() == {
        "max_attempts": 5,
        "retry_base_seconds": 30.0,
        "retry_max_seconds": 3600.0,
        "successful_source_refs": "delete",
        "failed_work_retention_seconds": 604800,
    }
    with pytest.raises(ValueError, match="retry_max_seconds"):
        HierarchicalIntegrationPolicy.model_validate(
            {
                **values,
                "cleanup": {"retry_base_seconds": 31.0, "retry_max_seconds": 30.0},
            }
        )



def _minimal_policy_values() -> dict:
    artifact = {
        "playbook_id": "root-integration",
        "artifact_sha256": "sha256:" + "a" * 64,
        "schema_generation": 2,
        "contract_fingerprint": "sha256:" + "b" * 64,
        "source_digest": "sha256:" + "c" * 64,
        "compiler_build": "test",
        "compiled_at": "2026-09-05T00:00:00Z",
        "version": 1,
    }
    boundary = {
        "required_checks": {"version": "v1", "names": ["unit"], "producer_id": "forge"},
        "repair": {"debug_intelligence_class": "debug-high"},
        "route": {
            "playbook_id": "root-integration",
            "scope": "project",
            "scope_identifier": "p",
            "artifact": artifact,
        },
    }
    return {
        "parent": boundary,
        "root": boundary,
        "branchless_parent": "verifier",
        "on_failed_child": "block",
    }


def test_policy_max_wait_defaults_to_one_hour_without_changing_frozen_snapshots():
    values = _minimal_policy_values()
    policy = HierarchicalIntegrationPolicy.model_validate(values)
    dumped = policy.model_dump(mode="json")

    assert DEFAULT_INTEGRATION_MAX_WAIT_SECONDS == 3600.0
    assert outbox_module.DEFAULT_MAX_WAIT_SECONDS == DEFAULT_INTEGRATION_MAX_WAIT_SECONDS
    assert policy.max_wait_seconds == DEFAULT_INTEGRATION_MAX_WAIT_SECONDS
    # Batches, operations and stages compare stored snapshots by dict
    # equality, so a default policy must dump exactly as it did before.
    assert "max_wait_seconds" not in dumped
    assert "max_wait_seconds" not in policy.model_dump()
    assert "max_wait_seconds" not in json.loads(policy.model_dump_json())
    assert HierarchicalIntegrationPolicy.model_validate(dumped).model_dump(mode="json") == dumped
    explicit = HierarchicalIntegrationPolicy.model_validate({**values, "max_wait_seconds": 3600})
    assert explicit == policy
    assert explicit.model_dump(mode="json") == dumped


def test_policy_max_wait_round_trips_a_configured_bound():
    policy = HierarchicalIntegrationPolicy.model_validate(
        {**_minimal_policy_values(), "max_wait_seconds": 120}
    )

    assert policy.max_wait_seconds == 120.0
    dumped = policy.model_dump(mode="json")
    assert dumped["max_wait_seconds"] == 120.0
    assert HierarchicalIntegrationPolicy.model_validate(dumped) == policy


def test_optional_train_policy_preserves_frozen_snapshots():
    policy = HierarchicalIntegrationPolicy.model_validate(_minimal_policy_values())
    frozen = policy.model_dump(mode="json")

    assert policy.train is None
    assert "train" not in frozen
    assert "train" not in policy.model_dump()
    assert "train" not in json.loads(policy.model_dump_json())
    assert HierarchicalIntegrationPolicy.model_validate(frozen) == policy
    assert HierarchicalIntegrationPolicy.model_validate(frozen).model_dump(mode="json") == frozen


@pytest.mark.parametrize("train", [{}, {"cadence_seconds": 90, "settling_cap_seconds": 600}])
def test_train_policy_defaults_and_configured_timing_round_trip(train):
    policy = HierarchicalIntegrationPolicy.model_validate({**_minimal_policy_values(), "train": train})
    assert policy.train.cadence_seconds == train.get("cadence_seconds", 300)
    assert policy.train.settling_cap_seconds == train.get("settling_cap_seconds", 1800)
    assert HierarchicalIntegrationPolicy.model_validate(policy.model_dump(mode="json")) == policy


@pytest.mark.parametrize("field", ["cadence_seconds", "settling_cap_seconds"])
@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), None, "soon"])
def test_train_policy_requires_positive_seconds(field, value):
    with pytest.raises(ValueError, match=field):
        HierarchicalIntegrationPolicy.model_validate(
            {**_minimal_policy_values(), "train": {field: value}}
        )


@pytest.mark.parametrize(
    "max_wait", [0, -1, float("inf"), float("-inf"), float("nan"), None, "soon"]
)
def test_policy_max_wait_must_be_finite_and_positive(max_wait):
    with pytest.raises(ValueError, match="max_wait_seconds"):
        HierarchicalIntegrationPolicy.model_validate(
            {**_minimal_policy_values(), "max_wait_seconds": max_wait}
        )


def test_project_max_wait_falls_back_to_the_default_without_a_hierarchical_policy():
    configured = {**_minimal_policy_values(), "max_wait_seconds": 90}

    assert integration_max_wait_seconds(configured) == 90.0
    assert integration_max_wait_seconds(_minimal_policy_values()) == 3600.0
    # Unconfigured and development-mode projects cannot name a bound; a
    # stored policy that no longer validates must not become unbounded.
    assert integration_max_wait_seconds(None) == 3600.0
    assert integration_max_wait_seconds({"validation": "none", "commands": []}) == 3600.0
    assert integration_max_wait_seconds({**configured, "max_wait_seconds": 0}) == 3600.0
    assert integration_max_wait_seconds("not a policy") == 3600.0


CI_COMMANDS = {"unit": "aq test tests/test_x.py"}


def test_ci_source_is_unset_by_default_without_changing_frozen_snapshots():
    values = _minimal_policy_values()
    policy = HierarchicalIntegrationPolicy.model_validate(values)

    assert policy.ci is None
    # In-flight batches compare stored snapshots by dict equality.
    assert "ci" not in policy.model_dump(mode="json")
    assert "ci" not in json.loads(policy.model_dump_json())
    assert integration_ci_policy(values) is None
    assert integration_ci_sources(values) == {
        "root": "hosted", "epic": "hosted", "promotion": "hosted", "origin": "default"}


def test_ci_source_round_trips_per_target_kind_overrides():
    values = {**_minimal_policy_values(), "ci": {
        "source": "local", "promotion": "hosted", "commands": CI_COMMANDS,
        "queue_seconds": 30, "run_seconds": 60,
    }}
    policy = HierarchicalIntegrationPolicy.model_validate(values)

    assert policy.ci.source_for("root") == policy.ci.source_for("epic") == "local"
    assert policy.ci.source_for("promotion") == "hosted"
    assert policy.ci.local_kinds() == ("root", "epic")
    dumped = policy.model_dump(mode="json")
    assert dumped["ci"]["commands"] == CI_COMMANDS
    assert HierarchicalIntegrationPolicy.model_validate(dumped) == policy
    assert integration_ci_policy(values) == policy.ci
    assert integration_ci_sources(values) == {
        "root": "local", "epic": "local", "promotion": "hosted", "origin": "policy"}


def test_hybrid_promotion_reads_hosted_checks():
    ci = HierarchicalIntegrationPolicy.model_validate({
        **_minimal_policy_values(), "ci": {"source": "hybrid", "commands": CI_COMMANDS},
    }).ci

    # A promotion step has no candidate gate, only the PR checks GitHub enforces.
    assert ci.source_for("promotion") == "hybrid"
    assert ci.local_kinds() == ("root", "epic")
    assert ci.requires_hosted("root") and ci.requires_hosted("promotion")
    assert not ci.requires_hosted("epic")


@pytest.mark.parametrize("requirements,root,epic", [
    ({"root": False}, False, False), ({"epic": True}, True, True),
    ({"root": False, "epic": True}, False, True),
])
def test_hybrid_hosted_requirements_are_explicit_per_boundary(requirements, root, epic):
    policy = HierarchicalIntegrationPolicy.model_validate({
        **_minimal_policy_values(), "ci": {"source": "hybrid", "commands": CI_COMMANDS,
                                          "hosted_attestation": requirements},
    })
    assert policy.ci.requires_hosted("root") is root
    assert policy.ci.requires_hosted("epic") is epic
    assert policy.model_dump(mode="json")["ci"]["hosted_attestation"] == requirements


def test_ci_hosted_requirement_defaults_preserve_existing_snapshot_shape():
    values = {**_minimal_policy_values(), "ci": {
        "source": "hybrid", "commands": CI_COMMANDS,
    }}
    policy = HierarchicalIntegrationPolicy.model_validate(values)
    assert "hosted_attestation" not in policy.model_dump(mode="json")["ci"]


@pytest.mark.parametrize("ci, error", [
    ({"source": "local"}, "requires ci.commands"),
    ({"source": "local", "commands": {"lint": "ruff check src"}},
     "no command for required check(s) unit"),
    ({"source": "hosted", "epic": "hybrid", "commands": {"lint": "ruff check src"}},
     "ci source for epic is hybrid"),
    ({"source": "local", "commands": {"unit": "sleep 1"}}, "not a finite job command"),
    ({"source": "github"}, "source"),
    ({"source": "local", "commands": CI_COMMANDS, "runner": "box"}, "runner"),
])
def test_local_ci_source_must_run_every_required_check(ci, error):
    with pytest.raises(ValueError, match=re.escape(error)):
        HierarchicalIntegrationPolicy.model_validate({**_minimal_policy_values(), "ci": ci})


def test_hosted_ci_source_needs_no_local_commands():
    policy = HierarchicalIntegrationPolicy.model_validate(
        {**_minimal_policy_values(), "ci": {"source": "hosted"}})

    assert policy.ci.local_kinds() == ()


def test_stored_ci_block_that_no_longer_validates_reads_hosted():
    stale = {**_minimal_policy_values(), "ci": {"source": "local", "commands": {}}}

    assert integration_ci_policy(stale) is None
    assert integration_ci_sources(stale)["origin"] == "default"
    assert integration_ci_policy(None) is None and integration_ci_policy("x") is None


@pytest.mark.parametrize(
    ("scope", "scope_identifier"),
    [
        ("system", "not-canonical"),
        ("project", ""),
        ("agent_type", "worker"),
        ("supervisor", "supervisor-p"),
    ],
)
def test_integration_route_rejects_noncanonical_or_unsupported_scope(
    scope, scope_identifier
):
    artifact = {
        "playbook_id": "hierarchical-delivery",
        "artifact_sha256": "sha256:" + "a" * 64,
        "schema_generation": 2,
        "contract_fingerprint": "sha256:" + "b" * 64,
        "source_digest": "sha256:" + "c" * 64,
        "compiler_build": "test",
        "version": 1,
    }

    with pytest.raises(ValueError, match="integration routes"):
        HierarchicalIntegrationPolicy.model_validate(
            {
                "parent": {
                    "required_checks": {
                        "version": "v1",
                        "names": ["unit"],
                        "producer_id": "forge",
                    },
                    "repair": {"debug_intelligence_class": "debug-high"},
                    "route": {
                        "playbook_id": "hierarchical-delivery",
                        "scope": scope,
                        "scope_identifier": scope_identifier,
                        "artifact": artifact,
                    },
                },
                "root": {
                    "required_checks": {
                        "version": "v1",
                        "names": ["unit"],
                        "producer_id": "forge",
                    },
                    "repair": {"debug_intelligence_class": "debug-high"},
                    "route": {
                        "playbook_id": "hierarchical-delivery",
                        "scope": scope,
                        "scope_identifier": scope_identifier,
                        "artifact": artifact,
                    },
                },
                "branchless_parent": "skip",
                "on_failed_child": "block",
            }
        )


async def test_reconciliation_pages_select_only_current_work_and_keep_intent_kind(db):
    async with db.immediate() as conn:
        for ordinal, lifecycle in enumerate(("testing", "testing", "promoted")):
            batch_id = f"batch-{ordinal}"
            await conn.execute(
                insert(integration_batches).values(
                    id=batch_id,
                    project_id=f"p-{ordinal}",
                    repository_id="repo",
                    request_id=f"request-{ordinal}",
                    source_manifest_digest=f"manifest-{ordinal}",
                    base_sha="a" * 40,
                    lifecycle=lifecycle,
                    current_revision=0,
                    integration_branch=f"refs/heads/integration/{ordinal}",
                    policy_snapshot={},
                    artifact_snapshot={},
                    cleanup_state="pending",
                    created_at=1.0,
                    updated_at=1.0,
                )
            )
            await conn.execute(
                insert(integration_candidate_revisions).values(
                    batch_id=batch_id,
                    revision=0,
                    construction_base_sha="a" * 40,
                    head_sha=chr(ord("b") + ordinal) * 40,
                    state="testing",
                    created_at=1.0,
                    updated_at=float(ordinal),
                )
            )
            await conn.execute(
                insert(integration_repair_operations).values(
                    id=f"operation-{ordinal}",
                    target_kind="batch",
                    batch_id=batch_id,
                    episode_id=batch_id,
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
                insert(integration_repair_stages).values(
                    operation_id=f"operation-{ordinal}",
                    ordinal=0,
                    policy={},
                    starting_sha="a" * 40,
                    deadline_at=float(ordinal + 1),
                    state="active" if ordinal < 2 else "passed",
                )
            )

        common = {
            "receipt_id": "receipt",
            "source_head": "b" * 40,
            "source_base": "a" * 40,
            "repository_id": "repo",
            "target_branch": "main",
            "expected_target": "a" * 40,
            "fence_owner_id": "owner",
            "fence_token": 1,
            "state": "prepared",
            "created_at": 1.0,
        }
        await conn.execute(
            insert(integration_promotion_intents).values(
                id="child-intent",
                domain_key="child-domain",
                intent_kind="child",
                updated_at=1.0,
                **common,
            )
        )
        await conn.execute(
            insert(integration_promotion_intents).values(
                id="root-intent",
                domain_key="root-domain",
                intent_kind="root",
                root_batch_id="batch-0",
                root_candidate_revision=0,
                project_id="p-0",
                project_lease_owner_id="owner",
                project_lease_fence_token=1,
                branch_fence_owner_id="owner",
                branch_fence_token=1,
                ci_evidence_id="ci",
                updated_at=2.0,
                **{**common, "target_branch": "release"},
            )
        )

    repairs = await db.due_integration_repair_stage_page(now=10.0, after=None, limit=1)
    assert [(row["operation_id"], row["stage"]) for row in repairs] == [
        ("operation-0", 0)
    ]
    repairs += await db.due_integration_repair_stage_page(
        now=10.0,
        after=(repairs[-1]["deadline_at"], repairs[-1]["operation_id"], 0),
        limit=1,
    )
    assert [row["operation_id"] for row in repairs] == ["operation-0", "operation-1"]

    candidates = await db.pending_candidate_ci_page(after=None, limit=1)
    candidates += await db.pending_candidate_ci_page(
        after=(
            candidates[-1]["updated_at"],
            candidates[-1]["batch_id"],
            candidates[-1]["revision"],
        ),
        limit=2,
    )
    assert [row["batch_id"] for row in candidates] == ["batch-0", "batch-1"]

    intents = await db.unresolved_integration_intent_page(after=None, limit=1)
    intents += await db.unresolved_integration_intent_page(
        after=(intents[-1]["updated_at"], intents[-1]["id"]), limit=1
    )
    assert [(row["id"], row["intent_kind"]) for row in intents] == [
        ("child-intent", "child"),
        ("root-intent", "root"),
    ]


async def test_all_reconciliation_keysets_page_past_two_hundred_rows(db):
    count = 205
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_batches),
            [
                {
                    "id": f"bulk-batch-{ordinal:03d}",
                    "project_id": f"bulk-project-{ordinal:03d}",
                    "repository_id": "repo",
                    "request_id": f"bulk-request-{ordinal:03d}",
                    "source_manifest_digest": f"bulk-manifest-{ordinal:03d}",
                    "base_sha": "a" * 40,
                    "lifecycle": "testing",
                    "current_revision": 0,
                    "integration_branch": f"refs/heads/integration/bulk-{ordinal:03d}",
                    "policy_snapshot": {},
                    "artifact_snapshot": {},
                    "cleanup_state": "pending",
                    "created_at": 1.0,
                    "updated_at": 1.0,
                }
                for ordinal in range(count)
            ],
        )
        await conn.execute(
            insert(integration_candidate_revisions),
            [
                {
                    "batch_id": f"bulk-batch-{ordinal:03d}",
                    "revision": 0,
                    "construction_base_sha": "a" * 40,
                    "head_sha": "b" * 40,
                    "state": "testing",
                    "created_at": 1.0,
                    "updated_at": float(ordinal % 11),
                }
                for ordinal in range(count)
            ],
        )
        await conn.execute(
            insert(integration_repair_operations),
            [
                {
                    "id": f"bulk-operation-{ordinal:03d}",
                    "target_kind": "batch",
                    "batch_id": f"bulk-batch-{ordinal:03d}",
                    "episode_id": f"bulk-batch-{ordinal:03d}",
                    "active_stage": 0,
                    "state": "active",
                    "policy_snapshot": {},
                    "artifact_snapshot": {},
                    "required_check_version": "v1",
                    "created_at": 1.0,
                    "updated_at": 1.0,
                }
                for ordinal in range(count)
            ],
        )
        await conn.execute(
            insert(integration_repair_stages),
            [
                {
                    "operation_id": f"bulk-operation-{ordinal:03d}",
                    "ordinal": 0,
                    "policy": {},
                    "starting_sha": "a" * 40,
                    "deadline_at": float(ordinal % 13),
                    "state": "active",
                }
                for ordinal in range(count)
            ],
        )
        await conn.execute(
            insert(integration_promotion_intents),
            [
                {
                    "id": f"bulk-intent-{ordinal:03d}",
                    "domain_key": f"bulk-domain-{ordinal:03d}",
                    "receipt_id": f"bulk-receipt-{ordinal:03d}",
                    "source_head": "b" * 40,
                    "source_base": "a" * 40,
                    "repository_id": "repo",
                    "target_branch": f"refs/heads/bulk-{ordinal:03d}",
                    "expected_target": "a" * 40,
                    "fence_owner_id": "owner",
                    "fence_token": 1,
                    "state": "prepared",
                    "intent_kind": "child",
                    "created_at": 1.0,
                    "updated_at": float(ordinal % 17),
                }
                for ordinal in range(count)
            ],
        )

    repair_rows = []
    after_repair = None
    candidate_rows = []
    after_candidate = None
    intent_rows = []
    after_intent = None
    while True:
        page = await db.due_integration_repair_stage_page(
            now=20.0, after=after_repair, limit=9
        )
        if not page:
            break
        repair_rows.extend(page)
        last = page[-1]
        after_repair = (last["deadline_at"], last["operation_id"], last["stage"])
    while True:
        page = await db.pending_candidate_ci_page(after=after_candidate, limit=9)
        if not page:
            break
        candidate_rows.extend(page)
        last = page[-1]
        after_candidate = (last["updated_at"], last["batch_id"], last["revision"])
    while True:
        page = await db.unresolved_integration_intent_page(after=after_intent, limit=9)
        if not page:
            break
        intent_rows.extend(page)
        last = page[-1]
        after_intent = (last["updated_at"], last["id"])

    assert len({row["operation_id"] for row in repair_rows}) == count
    assert len({row["batch_id"] for row in candidate_rows}) == count
    assert len({row["id"] for row in intent_rows}) == count




















# --- Bounded sources and items (phase-0 item 8) -----------------------------


class _QuietDB:
    async def due_integration_schedule_page(self, **kwargs):
        return []

    async def due_integration_repair_stage_page(self, **kwargs):
        return []

    async def pending_candidate_ci_page(self, **kwargs):
        return []

    async def unresolved_integration_intent_page(self, **kwargs):
        return []












@pytest.mark.parametrize(
    "kwargs",
    [
        {"source_timeout_seconds": 0},
        {"item_timeout_seconds": -1},
        {"source_timeouts": {"GitHub PR reviews": 0}},
        {"source_timeouts": {"GitHub PR reviews": True}},
    ],
)
def test_service_refuses_nonpositive_budgets(kwargs):
    with pytest.raises(ValueError, match="timeout must be positive"):
        _IntegrationService(None, None, **kwargs)


def test_integration_config_validates_and_loads_service_budgets(tmp_path):
    from src.config import IntegrationConfig, load_config

    defaults = IntegrationConfig()
    assert defaults.service_source_timeout_seconds == 300.0
    assert defaults.service_item_timeout_seconds == 60.0
    assert defaults.service_source_timeouts == {}
    assert defaults.validate() == []
    bad = IntegrationConfig(
        service_source_timeout_seconds=0,
        service_item_timeout_seconds="1",
        service_source_timeouts={"": 5},
    )
    assert {error.field for error in bad.validate()} == {
        "service_source_timeout_seconds",
        "service_item_timeout_seconds",
        "service_source_timeouts",
    }

    path = tmp_path / "config.yaml"
    path.write_text(
        "discord:\n  bot_token: t\n  guild_id: '1'\n"
        "database:\n  url: postgresql+asyncpg://test:test@localhost/test\n"
        "integration:\n"
        "  service_source_timeout_seconds: 120\n"
        "  service_item_timeout_seconds: 30\n"
        "  service_source_timeouts:\n"
        "    GitHub PR reviews: 600\n"
    )
    loaded = load_config(str(path)).integration
    assert loaded.service_source_timeout_seconds == 120
    assert loaded.service_item_timeout_seconds == 30
    assert loaded.service_source_timeouts == {"GitHub PR reviews": 600}


# --- Refused repair dispatch retried from durable state (phase-0 item 3) ----


class _DispatchDB(_QuietDB):
    def __init__(self, rows):
        self.rows = rows

    async def refused_integration_repair_dispatch_page(self, *, after, limit):
        rows = [
            row for row in self.rows
            if after is None or (row["operation_id"], row["ordinal"]) > tuple(after)
        ]
        return [dict(row) for row in rows[:limit]]














async def test_refused_dispatch_page_selects_unlaunched_writers_under_any_policy(db):
    from src.database.tables import task_metadata
    from src.models import Project, Task, TaskStatus

    await db.create_project(Project(id="p", name="Project"))
    cases = {
        # operation: (operation state, stage, stage state, writer, task status, held, revision)
        "op-none": ("active", 1, "active", None, None, False, "testing"),
        "op-escalated": ("escalated", 1, "active", None, None, False, "testing"),
        "op-paused": ("active", 1, "active", "repair_delegate", "PAUSED", False, "testing"),
        "op-ready": ("active", 1, "active", "repair_delegate", "READY", False, "testing"),
        "op-held": ("active", 1, "active", "repair_delegate", "PAUSED", True, "testing"),
        "op-verifier": ("active", 1, "active", "existing_verifier", "PAUSED", False, "testing"),
        "op-human": ("human_required", 1, "active", None, None, False, "testing"),
        "op-recovery": ("escalated", 1, "expired", None, None, False, "testing"),
        # A built stage-0 candidate still publishing belongs to its collector.
        "op-publishing": ("active", 0, "active", None, None, False, "built"),
    }
    for operation_id, case in cases.items():
        op_state, ordinal, stage_state, writer_kind, status, held, revision_state = case
        batch_id = f"batch-{operation_id}"
        task_id = None
        if status is not None:
            task_id = f"task-{operation_id}"
            await db.create_task(Task(
                id=task_id, project_id="p", title="Repair", description="",
                status=TaskStatus(status),
            ))
        async with db.immediate() as conn:
            if held:
                await conn.execute(insert(task_metadata).values(
                    task_id=task_id, key="manual_pause", value="{}"
                ))
            await conn.execute(insert(integration_batches).values(
                id=batch_id, project_id=f"p-{operation_id}", repository_id="repo",
                request_id=f"request-{operation_id}", source_manifest_digest="manifest",
                base_sha="a" * 40, lifecycle="repairing", current_revision=0,
                integration_branch=f"aq/integration/{operation_id}", policy_snapshot={},
                artifact_snapshot={}, cleanup_state="pending", created_at=1.0, updated_at=1.0,
            ))
            await conn.execute(insert(integration_candidate_revisions).values(
                batch_id=batch_id, revision=0, construction_base_sha="a" * 40,
                head_sha="b" * 40, state=revision_state, created_at=1.0, updated_at=1.0,
            ))
            await conn.execute(insert(integration_repair_operations).values(
                id=operation_id, target_kind="batch", batch_id=batch_id, episode_id=batch_id,
                active_stage=ordinal, state=op_state, policy_snapshot={},
                artifact_snapshot={}, required_check_version="v1",
                created_at=1.0, updated_at=1.0,
            ))
            await conn.execute(insert(integration_repair_stages).values(
                operation_id=operation_id, ordinal=ordinal,
                policy={"on_exhausted": "human"}, starting_sha="a" * 40,
                repair_task_id=task_id, writer_kind=writer_kind, state=stage_state,
            ))

    first = await db.refused_integration_repair_dispatch_page(after=None, limit=2)
    rest = await db.refused_integration_repair_dispatch_page(
        after=(first[-1]["operation_id"], first[-1]["ordinal"]), limit=10
    )
    selected = {row["operation_id"]: row for row in first + rest}
    assert sorted(selected) == ["op-escalated", "op-none", "op-paused"]
    assert selected["op-paused"]["task_status"] == "PAUSED"
    assert selected["op-paused"]["repair_task_id"] == "task-op-paused"




async def test_subject_pass_runs_each_runtime_and_isolates_failed_maintenance():
    calls = []

    def runtime(name):
        async def tick(now):
            calls.append((name, now))
        return SimpleNamespace(tick=tick, stop=AsyncMock())

    async def failed(now):
        calls.append(("failed", now))
        raise RuntimeError("temporary failure")

    async def later(now):
        calls.append(("later", now))

    async def dispatch(now):
        calls.append(("outbox", now))

    service = IntegrationService(
        object(), SimpleNamespace(dispatch_due=dispatch),
        subject_runtime=runtime("root"), parent_subject_runtime=runtime("parent"),
        development_subject_runtime=runtime("development"),
        maintenance={"failed": failed, "later": later}, clock=lambda: 100,
    )
    await service.tick(100)
    assert calls == [(name, 100) for name in
                     ("root", "parent", "development", "failed", "later", "outbox")]


async def test_active_train_replaces_the_runtimes_and_never_dispatches_legacy_events():
    """git_first: active runs one train; pending outbox rows reach no old writer."""
    calls = []

    async def train_tick(now):
        calls.append(("train", now))
        return {"started": [], "running": [], "skipped": []}

    async def maintenance(now):
        calls.append(("maintenance", now))

    outbox = SimpleNamespace(dispatch_due=AsyncMock())
    for _restart in range(2):
        train = SimpleNamespace(tick=train_tick, stop=AsyncMock())
        service = IntegrationService(object(), outbox, train=train,
                                     maintenance={"cleanup": maintenance}, clock=lambda: 100)
        await service.tick(100)
        await service.stop()
        train.stop.assert_awaited_once()
    assert calls == [("train", 100), ("maintenance", 100)] * 2
    outbox.dispatch_due.assert_not_awaited()


@pytest.mark.parametrize(
    "runtime", ["subject_runtime", "parent_subject_runtime", "development_subject_runtime"]
)
def test_train_never_runs_beside_a_subject_runtime(runtime):
    with pytest.raises(ValueError, match="replaces the subject runtimes"):
        _IntegrationService(None, None, train=object(), **{runtime: object()})


async def test_background_subject_pass_does_not_overlap_and_stop_cancels_it():
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def tick(now):
        calls.append(now)
        entered.set()
        await release.wait()

    runtime = SimpleNamespace(tick=tick, stop=AsyncMock())
    service = IntegrationService(
        object(), SimpleNamespace(dispatch_due=AsyncMock()),
        subject_runtime=runtime, clock=lambda: 100,
    )
    await service.tick(100, background=True)
    await entered.wait()
    await service.tick(101, background=True)
    assert calls == [100]
    await service.stop()
    runtime.stop.assert_awaited_once()
    assert service._reconciliation_task is None


async def test_timed_out_subject_does_not_starve_other_subject_kinds():
    cancelled = asyncio.Event()

    async def hung(now):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    later = SimpleNamespace(tick=AsyncMock(), stop=AsyncMock())
    service = IntegrationService(
        object(), SimpleNamespace(dispatch_due=AsyncMock()),
        subject_runtime=SimpleNamespace(tick=hung, stop=AsyncMock()),
        parent_subject_runtime=later, source_timeout_seconds=0.01,
    )
    await service.tick(100)
    assert cancelled.is_set()
    later.tick.assert_awaited_once()
    service._outbox.dispatch_due.assert_awaited_once()


async def test_uncancellable_maintenance_is_not_started_twice(monkeypatch):
    from src.integration import service as module
    monkeypatch.setattr(module, "CANCEL_GRACE_SECONDS", 0.01)
    release = asyncio.Event()
    calls = []

    async def stubborn(now):
        calls.append(now)
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()

    service = IntegrationService(
        object(), SimpleNamespace(dispatch_due=AsyncMock()),
        maintenance={"stubborn": stubborn}, source_timeout_seconds=0.01,
    )
    await service.tick(100)
    await service.tick(101)
    assert len(calls) == 1
    release.set()
    for task in tuple(service._unwinding["stubborn"]):
        await task
    await service.tick(102)
    assert len(calls) == 2
    await service.stop()
