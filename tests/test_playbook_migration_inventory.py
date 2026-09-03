"""Package 6 T-1/T-2 — V1 playbook inventory and migration readiness.

Child plan: ``docs/superpowers/plans/2026-09-01-playbook-v2-migration-artifacts.md``
§3.2 (types), §3.3 (disposition rules), §5.1 (this suite).

Every case here exercises ``src.playbooks.migration.build_inventory`` against a
real on-disk vault.  The entry point is read-only by contract, so the doubles
below raise on any write and the suite fails loudly if that ever stops being
true.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import ClassVar

import pytest

from src.playbooks.migration import (
    REASON_CODES,
    MigrationInventory,
    MigrationReason,
    build_inventory,
)
from src.vault import ensure_default_agent_type_playbooks, ensure_default_playbooks

# Reason codes each test below proves reachable.  ``test_reason_codes_are_closed``
# asserts the union covers ``REASON_CODES`` — an unreachable code is a dead
# branch and a stale operator-facing promise.
_EXERCISED: set[str] = set()


def _seen(*codes: str) -> None:
    _EXERCISED.update(codes)


# ---------------------------------------------------------------------------
# Vault helpers
# ---------------------------------------------------------------------------


def _vault_root(tmp_path) -> str:
    ensure_default_playbooks(str(tmp_path))
    ensure_default_agent_type_playbooks(str(tmp_path))
    return str(tmp_path / "vault")


def _write_playbook(vault_root, rel_dir: str, name: str, body: str) -> str:
    import os

    directory = os.path.join(vault_root, rel_dir)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(body)
    return path


def _source(playbook_id: str, *, enabled: bool | None = None, scope: str | None = None) -> str:
    lines = ["---", f"id: {playbook_id}", "triggers:", "  - task.completed"]
    if scope is not None:
        lines.append(f"scope: {scope}")
    if enabled is not None:
        lines.append(f"enabled: {str(enabled).lower()}")
    lines += ["---", "", "# Prose", "", "Do the thing.", ""]
    return "\n".join(lines)


def _append(path: str, text: str) -> None:
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(text)


def _entry(inv: MigrationInventory, playbook_id: str):
    matches = [e for e in inv.entries if e.playbook_id == playbook_id]
    assert matches, f"{playbook_id} missing from {[e.playbook_id for e in inv.entries]}"
    assert len(matches) == 1, f"{playbook_id} appears {len(matches)} times"
    return matches[0]


def _codes(entry) -> set[str]:
    return {reason.code for reason in entry.reasons}


# ---------------------------------------------------------------------------
# Read-only doubles
# ---------------------------------------------------------------------------


class ExplodingStore:
    """A ``CompiledPlaybookStore`` whose every write raises."""

    def __init__(self, playbooks: list | None = None) -> None:
        self._playbooks = playbooks or []

    def list_all(self):
        return list(self._playbooks)

    def save(self, *a, **k):  # pragma: no cover - must never be called
        raise AssertionError("build_inventory wrote to the compiled store")

    def delete(self, *a, **k):  # pragma: no cover - must never be called
        raise AssertionError("build_inventory deleted from the compiled store")


class ExplodingDatabase:
    """A ``Database`` double that refuses every statement."""

    async def execute(self, *a, **k):  # pragma: no cover - must never be called
        raise AssertionError("build_inventory touched the database")

    async def commit(self, *a, **k):  # pragma: no cover - must never be called
        raise AssertionError("build_inventory committed")


class StubAckRepo:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []

    async def list_acks(self) -> list[dict]:
        return list(self.rows)


class StubActivationRepo:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []

    async def list_playbook_activations(self, *, enabled_only: bool = False) -> list[dict]:
        return [r for r in self.rows if not enabled_only or r.get("enabled")]


class StubPendingRepo:
    def __init__(self, counts: dict[str, int] | None = None) -> None:
        self.counts = counts or {}

    async def list_pending_events(self, **kwargs) -> list[dict]:
        return [
            {"playbook_id": pid, "pending_event_id": f"{pid}-{i}", "received_at": 1.0}
            for pid, n in self.counts.items()
            for i in range(n)
        ]


class StubContractRegistry:
    def __init__(self, fingerprint: str = "sha256:" + "a" * 64) -> None:
        self._fingerprint = fingerprint

    def registry_fingerprint(self) -> str:
        return self._fingerprint

    def names(self):
        return frozenset()


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _inventory(vault_root, **kwargs) -> MigrationInventory:
    kwargs.setdefault("store", ExplodingStore())
    kwargs.setdefault("contract_registry", StubContractRegistry())
    return await build_inventory(vault_root=vault_root, **kwargs)


# ---------------------------------------------------------------------------
# 1 — closed reason-code set
# ---------------------------------------------------------------------------


def test_reason_code_set_is_closed():
    with pytest.raises(ValueError):
        MigrationReason(code="not_a_code", message="x")
    # A valid one constructs.
    MigrationReason(code="operator_disabled", message="x")
    _seen("operator_disabled")


# ---------------------------------------------------------------------------
# 2 — enumeration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enumerates_all_four_shipped_playbooks(tmp_path):
    inv = await _inventory(_vault_root(tmp_path))
    assert {e.playbook_id for e in inv.entries} == {
        "default-pipeline",
        "default-assignment-routing",
        "memory-consolidation",
        "coding-reflection",
    }


@pytest.mark.asyncio
async def test_bundled_path_recorded_for_shipped_sources(tmp_path):
    inv = await _inventory(_vault_root(tmp_path))
    pipeline = _entry(inv, "default-pipeline")
    assert pipeline.source.vault_rel_path == "system/playbooks/default-pipeline.md"
    assert pipeline.source.bundled_rel_path == (
        "src/prompts/default_playbooks/default-pipeline.md"
    )
    assert pipeline.source.source_sha256.startswith("sha256:")


@pytest.mark.asyncio
async def test_project_authored_playbook_has_no_bundled_path(tmp_path):
    vault_root = _vault_root(tmp_path)
    _write_playbook(vault_root, "projects/demo/playbooks", "local.md", _source("local-pb"))
    inv = await _inventory(vault_root)
    entry = _entry(inv, "local-pb")
    assert entry.scope == "project"
    assert entry.scope_identifier == "demo"
    assert entry.source.bundled_rel_path is None


# ---------------------------------------------------------------------------
# 3 — read-only invariant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inventory_is_read_only(tmp_path):
    vault_root = _vault_root(tmp_path)
    inv = await build_inventory(
        vault_root=vault_root,
        store=ExplodingStore(),
        contract_registry=StubContractRegistry(),
        db=ExplodingDatabase(),
    )
    assert inv.entries


# ---------------------------------------------------------------------------
# 4 — embedded action block
# ---------------------------------------------------------------------------


def _embedded_fence_line() -> int:
    """Independently locate the action-graph fence in the shipped pipeline.

    The child plan's §5.1 case 4 pinned line 41; the shipped file has moved
    since the plan was drafted, so the assertion recomputes the expected line
    rather than carrying a stale literal (recorded in §2's reconciliation).
    """
    import os

    import src.vault as vault_mod

    path = os.path.join(
        os.path.dirname(vault_mod.__file__),
        "prompts",
        "default_playbooks",
        "default-pipeline.md",
    )
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    for index, line in enumerate(lines):
        if line.strip() != "```json":
            continue
        closing = next(
            (j for j in range(index + 1, len(lines)) if lines[j].strip() == "```"), None
        )
        if closing is None:
            continue
        try:
            payload = json.loads("\n".join(lines[index + 1 : closing]))
        except ValueError:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("rules"), list):
            return index + 1
    raise AssertionError("no action-graph fence found in default-pipeline.md")


@pytest.mark.asyncio
async def test_embedded_action_block_is_question_required(tmp_path):
    inv = await _inventory(_vault_root(tmp_path))
    entry = _entry(inv, "default-pipeline")
    assert entry.has_embedded_action_block is True
    assert entry.disposition == "question_required"
    assert "embedded_action_block" in _codes(entry)
    reason = next(r for r in entry.reasons if r.code == "embedded_action_block")
    assert reason.source_line == _embedded_fence_line()
    _seen("embedded_action_block")


@pytest.mark.asyncio
async def test_output_shape_fences_are_not_action_blocks(tmp_path):
    """``memory-consolidation.md`` has ```json fences that are output examples."""
    inv = await _inventory(_vault_root(tmp_path))
    entry = _entry(inv, "memory-consolidation")
    assert entry.has_embedded_action_block is False
    assert "embedded_action_block" not in _codes(entry)


# ---------------------------------------------------------------------------
# 5 — scope conflict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scope_conflict_detected(tmp_path):
    inv = await _inventory(_vault_root(tmp_path))
    entry = _entry(inv, "coding-reflection")
    assert entry.scope == "agent_type"
    assert entry.scope_identifier == "claude-opus"
    assert entry.disposition == "question_required"
    reason = next(r for r in entry.reasons if r.code == "scope_conflict")
    assert "agent-type:coding" in reason.message
    assert "claude-opus" in reason.message
    _seen("scope_conflict")


# ---------------------------------------------------------------------------
# 6 — duplicate id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_id_is_invalid(tmp_path):
    vault_root = _vault_root(tmp_path)
    _write_playbook(vault_root, "system/playbooks", "dup-a.md", _source("dup"))
    _write_playbook(vault_root, "system/playbooks", "dup-b.md", _source("dup"))
    inv = await _inventory(vault_root)
    entry = _entry(inv, "dup")
    assert entry.disposition == "invalid"
    reason = next(r for r in entry.reasons if r.code == "duplicate_playbook_id")
    assert "system/playbooks/dup-a.md" in reason.message
    assert "system/playbooks/dup-b.md" in reason.message
    _seen("duplicate_playbook_id")


@pytest.mark.asyncio
async def test_missing_id_is_invalid(tmp_path):
    vault_root = _vault_root(tmp_path)
    _write_playbook(vault_root, "system/playbooks", "noid.md", "---\ntriggers: []\n---\n\nprose\n")
    inv = await _inventory(vault_root)
    entry = _entry(inv, "noid")
    assert entry.disposition == "invalid"
    assert "source_unreadable" in _codes(entry)
    _seen("source_unreadable")


@pytest.mark.asyncio
async def test_compiled_artifact_without_source_is_invalid(tmp_path):
    class Compiled:
        id = "ghost"
        kind = ""
        version = 3
        enabled = True
        nodes: ClassVar[dict] = {}

    vault_root = _vault_root(tmp_path)
    inv = await _inventory(vault_root, store=ExplodingStore([("system", None, Compiled())]))
    entry = _entry(inv, "ghost")
    assert entry.disposition == "invalid"
    assert "source_unreadable" in _codes(entry)
    assert entry.v1_version == 3


# ---------------------------------------------------------------------------
# 7 — disabled needs an acknowledgement unless the frontmatter says so
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_frontmatter_disabled_needs_no_ack(tmp_path):
    vault_root = _vault_root(tmp_path)
    _write_playbook(
        vault_root, "system/playbooks", "off.md", _source("switched-off", enabled=False)
    )
    inv = await _inventory(vault_root)
    entry = _entry(inv, "switched-off")
    assert entry.disposition == "disabled"
    assert "operator_disabled" in _codes(entry)
    assert entry.acknowledged_by is None
    _seen("operator_disabled")


@pytest.mark.asyncio
async def test_ack_moves_question_required_to_disabled(tmp_path):
    vault_root = _vault_root(tmp_path)
    body = _source("needs-ack")
    path = _write_playbook(vault_root, "system/playbooks", "needs-ack.md", body)

    # No ack yet: never compiled, so it cannot be `ready`.
    inv = await _inventory(vault_root)
    assert _entry(inv, "needs-ack").disposition == "question_required"

    ack = {
        "playbook_id": "needs-ack",
        "scope": "system",
        "scope_identifier": "",
        "source_sha256": _sha(body),
        "reason": "cannot migrate before the compiler lands",
        "acknowledged_by": "operator",
        "acknowledged_at": 1788400000.0,
    }
    inv = await _inventory(vault_root, ack_repo=StubAckRepo([ack]))
    entry = _entry(inv, "needs-ack")
    assert entry.disposition == "disabled"
    assert entry.acknowledged_by == "operator"
    assert entry.acknowledged_at == 1788400000.0

    # Editing the source invalidates the ack.
    _append(path, "\nOne more sentence.\n")
    inv = await _inventory(vault_root, ack_repo=StubAckRepo([ack]))
    assert _entry(inv, "needs-ack").disposition == "question_required"


# ---------------------------------------------------------------------------
# 8 — blocking()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blocking_excludes_acknowledged_and_frontmatter_disabled(tmp_path):
    vault_root = _vault_root(tmp_path)
    body = _source("waived")
    _write_playbook(vault_root, "system/playbooks", "waived.md", body)
    _write_playbook(vault_root, "system/playbooks", "off.md", _source("off-pb", enabled=False))
    ack = {
        "playbook_id": "waived",
        "scope": "system",
        "scope_identifier": "",
        "source_sha256": _sha(body),
        "reason": "waived until package seven",
        "acknowledged_by": "operator",
        "acknowledged_at": 1.0,
    }
    inv = await _inventory(vault_root, ack_repo=StubAckRepo([ack]))
    blocking = {e.playbook_id for e in inv.blocking()}
    assert "waived" not in blocking
    assert "off-pb" not in blocking
    assert "default-pipeline" in blocking


# ---------------------------------------------------------------------------
# 9 — superseded rules
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_superseded_rules_not_reported_missing(tmp_path):
    class Compiled:
        id = "default-pipeline"
        kind = "pipeline"
        scope = "system"
        version = 7
        enabled = True
        nodes: ClassVar[dict] = {
            "task-created-routing": object(),
            "per-task-review": object(),
        }

    vault_root = _vault_root(tmp_path)
    inv = await _inventory(vault_root, store=ExplodingStore([("system", None, Compiled())]))
    entry = _entry(inv, "default-pipeline")
    codes = _codes(entry)
    assert "superseded_rule" in codes
    assert not {c for c in codes if c.startswith("unknown_")}
    reason = next(r for r in entry.reasons if r.code == "superseded_rule")
    assert "task-created-routing" in reason.message
    _seen("superseded_rule")


# ---------------------------------------------------------------------------
# 10 — activation health, stale contracts and readiness
# ---------------------------------------------------------------------------


def _ready_activation(playbook_id: str, fingerprint: str, **overrides) -> dict:
    row = {
        "playbook_id": playbook_id,
        "scope": "system",
        "scope_identifier": "",
        "enabled": True,
        "health": "ready",
        "artifact_sha256": "sha256:" + "b" * 64,
        "contract_fingerprint": fingerprint,
        "source_digest": "sha256:" + "c" * 64,
        "schema_generation": 2,
        "compiler_build": "test",
        "version": 1,
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_ready_when_activation_matches_registry(tmp_path):
    vault_root = _vault_root(tmp_path)
    body = _source("migrated")
    _write_playbook(vault_root, "system/playbooks", "migrated.md", body)
    registry = StubContractRegistry()
    activation = _ready_activation("migrated", registry.registry_fingerprint())
    activation["source_digest"] = _sha(body)
    inv = await _inventory(
        vault_root,
        contract_registry=registry,
        activation_repo=StubActivationRepo([activation]),
    )
    entry = _entry(inv, "migrated")
    assert entry.disposition == "ready"
    assert entry.reasons == ()
    assert entry.activation_health == "ready"
    assert entry.artifact is not None
    assert entry.artifact.artifact_sha256 == activation["artifact_sha256"]


@pytest.mark.asyncio
async def test_stale_contract_is_question_required(tmp_path):
    vault_root = _vault_root(tmp_path)
    body = _source("drifted")
    _write_playbook(vault_root, "system/playbooks", "drifted.md", body)
    registry = StubContractRegistry()
    activation = _ready_activation("drifted", "sha256:" + "d" * 64)
    activation["source_digest"] = _sha(body)
    inv = await _inventory(
        vault_root,
        contract_registry=registry,
        activation_repo=StubActivationRepo([activation]),
    )
    entry = _entry(inv, "drifted")
    assert entry.disposition == "question_required"
    assert "stale_contract" in _codes(entry)
    _seen("stale_contract")


@pytest.mark.asyncio
async def test_unhealthy_activation_reasons(tmp_path):
    vault_root = _vault_root(tmp_path)
    registry = StubContractRegistry()
    cases = {
        "bad-schema": ("invalid_artifact", "schema_violation", "invalid"),
        "no-command": ("unknown_command", "unknown_command", "invalid"),
        "no-event": ("unknown_event", "unknown_event", "invalid"),
        "no-profile": ("unknown_profile", "unknown_profile", "invalid"),
        "no-cap": ("capability_not_declared", "capability_not_declared", "question_required"),
        "unbound": ("binding_unassigned", "binding_unassigned", "invalid"),
        "nested": ("nested_loop_rejected", "nested_loop_rejected", "invalid"),
    }
    rows = []
    for playbook_id, (health, _code, _disp) in cases.items():
        body = _source(playbook_id)
        _write_playbook(vault_root, "system/playbooks", f"{playbook_id}.md", body)
        row = _ready_activation(playbook_id, registry.registry_fingerprint(), health=health)
        row["source_digest"] = _sha(body)
        rows.append(row)
    inv = await _inventory(
        vault_root,
        contract_registry=registry,
        activation_repo=StubActivationRepo(rows),
    )
    for playbook_id, (_health, code, disposition) in cases.items():
        entry = _entry(inv, playbook_id)
        assert code in _codes(entry), playbook_id
        assert entry.disposition == disposition, playbook_id
    _seen(*(code for _h, code, _d in cases.values()))


@pytest.mark.asyncio
async def test_source_edited_since_activation_asks_a_question(tmp_path):
    vault_root = _vault_root(tmp_path)
    _write_playbook(vault_root, "system/playbooks", "edited.md", _source("edited"))
    registry = StubContractRegistry()
    activation = _ready_activation("edited", registry.registry_fingerprint())
    inv = await _inventory(
        vault_root,
        contract_registry=registry,
        activation_repo=StubActivationRepo([activation]),
    )
    entry = _entry(inv, "edited")
    assert entry.disposition == "question_required"
    assert "compile_question" in _codes(entry)
    _seen("compile_question")


# ---------------------------------------------------------------------------
# 11 — pending-event surfacing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_events_surfaced_per_entry(tmp_path):
    vault_root = _vault_root(tmp_path)
    inv = await _inventory(
        vault_root, pending_repo=StubPendingRepo({"default-pipeline": 3})
    )
    assert _entry(inv, "default-pipeline").pending_events == 3
    assert _entry(inv, "memory-consolidation").pending_events == 0
    assert inv.to_dict()["pending_events_total"] == 3


# ---------------------------------------------------------------------------
# 12 — stable serialisation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_to_dict_is_stable(tmp_path):
    vault_root = _vault_root(tmp_path)
    frozen = 1788400000.0
    first = await _inventory(vault_root, now=frozen)
    second = await _inventory(vault_root, now=frozen)
    assert json.dumps(first.to_dict(), sort_keys=False) == json.dumps(
        second.to_dict(), sort_keys=False
    )
    live = await _inventory(vault_root)
    assert live.generated_at != frozen
    a = first.to_dict()
    b = live.to_dict()
    a.pop("generated_at")
    b.pop("generated_at")
    assert json.dumps(a, sort_keys=False) == json.dumps(b, sort_keys=False)


@pytest.mark.asyncio
async def test_entries_are_sorted_and_by_disposition_filters(tmp_path):
    vault_root = _vault_root(tmp_path)
    _write_playbook(vault_root, "projects/zeta/playbooks", "z.md", _source("z-pb"))
    _write_playbook(vault_root, "projects/alpha/playbooks", "a.md", _source("a-pb"))
    inv = await _inventory(vault_root)
    keys = [(e.scope, e.scope_identifier or "", e.playbook_id) for e in inv.entries]
    assert keys == sorted(keys)
    questioned = inv.by_disposition("question_required")
    assert {e.playbook_id for e in questioned} <= {e.playbook_id for e in inv.entries}
    assert all(e.disposition == "question_required" for e in questioned)


@pytest.mark.asyncio
async def test_generated_at_defaults_to_now(tmp_path):
    before = time.time()
    inv = await _inventory(_vault_root(tmp_path))
    assert before <= inv.generated_at <= time.time()


# ---------------------------------------------------------------------------
# Closure check — must be the last test in the module
# ---------------------------------------------------------------------------


def test_zz_every_reason_code_is_exercised():
    missing = set(REASON_CODES) - _EXERCISED
    assert not missing, f"reason codes never produced by any test: {sorted(missing)}"
