"""Package 6 T-3 — migration inventory commands, waivers, and their storage.

Child plan §3.6 (command surface) and §4.2 (the waiver's security properties).
The acknowledgement is the one mechanism in Package 6 that can move the fleet
past a real problem, so every property that keeps it narrow is pinned here:
attribution comes from the server, the justification has a floor, editing the
source invalidates the waiver, and no shipped agent profile can call it.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from src.commands.playbook_migration_commands import (
    REASON_TOO_SHORT_ERROR,
    PlaybookMigrationCommandsMixin,
)
from src.commands.playbook_v2_commands import PlaybookV2CommandsMixin
from src.database import Database
from src.database.queries.playbook_migration_queries import MIN_ACK_REASON_LENGTH
from src.vault import ensure_default_agent_type_playbooks, ensure_default_playbooks
from tests.pg_dsn import ensure_worker_postgres_dsn

POSTGRES_TEST_DSN = ensure_worker_postgres_dsn()

_GOOD_REASON = "superseded by the router; not migrating"


@pytest.fixture(params=["sqlite", "postgres"])
async def db(request, tmp_path):
    if request.param == "postgres":
        if not POSTGRES_TEST_DSN:
            pytest.skip("POSTGRES_TEST_DSN not set")
        from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter

        database = PostgreSQLDatabaseAdapter(POSTGRES_TEST_DSN)
        await database.initialize()
        await database.reset_for_tests()
    else:
        database = Database(str(tmp_path / "playbook-migration.db"))
        await database.initialize()
    yield database
    await database.close()


class _Playbooks:
    v2_max_artifact_bytes = 1_048_576


class _Config:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self.vault_root = os.path.join(data_dir, "vault")
        self.compiled_root = os.path.join(data_dir, "compiled")
        self.playbooks = _Playbooks()


class _Handler(PlaybookMigrationCommandsMixin):
    """Just the mixin — the surface under test owns no other collaborator."""

    def __init__(self, config, db, reviewed_root: Path) -> None:
        self.config = config
        self.db = db
        self.reviewed_root = reviewed_root

    def _migration_store(self):
        # The compiled V1 tree is irrelevant to the command surface; the
        # inventory's own suite covers it.
        return None

    def _reviewed_fixture_root(self) -> Path:
        # An empty directory unless a test writes a decision record into it,
        # so "was this artifact reviewed?" is answered by the test rather than
        # by whichever fixtures happen to be checked in.
        return self.reviewed_root

    async def _v2_lookups(self):
        # The real `CommandHandler` always has this seam, and the release check
        # now treats it *failing* as unread evidence.  The stub therefore has to
        # answer it rather than be missing it, or every test here would exercise
        # the profile-registry-unavailable path instead of its own subject.
        from src.playbooks.migration import shipped_profile_lookup
        from src.playbooks.validation import RegisteredEventLookup, RegistryContractLookup

        return RegistryContractLookup(), shipped_profile_lookup(), RegisteredEventLookup()


class _ActivationAndReportHandler(PlaybookV2CommandsMixin, _Handler):
    """The public activation and report commands over one real database."""

    async def _v2_lookups(self):
        return await _Handler._v2_lookups(self)

    async def _v2_load_artifact(self, sha: str, playbook_id: str | None = None):
        from src.playbooks.artifact_store import ArtifactStore

        ref = await self.db.get_playbook_artifact(sha)
        if ref is None:
            return None, None, f"Playbook artifact '{sha}' not found"
        if playbook_id is not None and ref.playbook_id != playbook_id:
            return None, None, f"Artifact '{sha}' does not belong to playbook '{playbook_id}'"
        return ref, ArtifactStore(self.config.compiled_root).load(sha), None


@pytest.fixture
def handler(tmp_path, db):
    data_dir = str(tmp_path / "aq")
    os.makedirs(data_dir, exist_ok=True)
    ensure_default_playbooks(data_dir)
    ensure_default_agent_type_playbooks(data_dir)
    reviewed_root = tmp_path / "reviewed"
    reviewed_root.mkdir()
    return _Handler(_Config(data_dir), db, reviewed_root)


def _ack(handler, playbook_id: str, reason: str):
    return handler._cmd_playbook_migration_acknowledge(
        {"playbook_id": playbook_id, "reason": reason}
    )


def _source_path(handler, name: str = "default-pipeline.md") -> Path:
    return Path(handler.config.vault_root) / "system" / "playbooks" / name


# ---------------------------------------------------------------------------
# Inventory command
# ---------------------------------------------------------------------------


async def test_inventory_command_reports_every_shipped_playbook(handler):
    result = await handler._cmd_playbook_migration_inventory({})
    assert result["success"] is True
    ids = {entry["playbook_id"] for entry in result["entries"]}
    assert {
        "default-pipeline",
        "default-assignment-routing",
        "memory-consolidation",
        "coding-reflection",
    } <= ids
    assert sum(result["counts"].values()) == len(result["entries"])
    assert result["blocking"] >= 1


async def test_inventory_filter_keeps_fleet_wide_counts(handler):
    unfiltered = await handler._cmd_playbook_migration_inventory({})
    filtered = await handler._cmd_playbook_migration_inventory({"disposition": "question_required"})
    assert filtered["filtered_by"] == "question_required"
    assert all(e["disposition"] == "question_required" for e in filtered["entries"])
    assert filtered["counts"] == unfiltered["counts"]


async def test_inventory_rejects_an_unknown_disposition(handler):
    result = await handler._cmd_playbook_migration_inventory({"disposition": "probably-fine"})
    assert result["success"] is False
    assert "probably-fine" in result["error"]


# ---------------------------------------------------------------------------
# Acknowledgement
# ---------------------------------------------------------------------------


async def test_acknowledge_requires_a_reason(handler):
    for reason in ("", "too short", "x" * (MIN_ACK_REASON_LENGTH - 1)):
        result = await _ack(handler, "default-pipeline", reason)
        assert result["success"] is False
        assert result["error"] == REASON_TOO_SHORT_ERROR
        assert str(MIN_ACK_REASON_LENGTH) in result["error"]

    result = await _ack(handler, "default-pipeline", _GOOD_REASON)
    assert result["success"] is True


async def test_acknowledge_refuses_an_unknown_playbook(handler):
    result = await _ack(handler, "not-installed", _GOOD_REASON)
    assert result["success"] is False
    assert "not-installed" in result["error"]


async def test_acknowledged_by_is_server_derived(handler):
    """A request may not name its own author (§4.2)."""
    result = await handler._cmd_playbook_migration_acknowledge(
        {
            "playbook_id": "default-pipeline",
            "reason": _GOOD_REASON,
            "acknowledged_by": "root",
        }
    )
    assert result["success"] is True
    assert result["acknowledgement"]["acknowledged_by"] != "root"

    rows = await handler.db.list_playbook_migration_acks()
    assert [row["acknowledged_by"] for row in rows] != ["root"]


async def test_acknowledge_moves_the_entry_to_disabled(handler):
    await _ack(handler, "default-pipeline", _GOOD_REASON)
    inventory = await handler._cmd_playbook_migration_inventory({})
    entry = next(e for e in inventory["entries"] if e["playbook_id"] == "default-pipeline")
    assert entry["disposition"] == "disabled"
    assert entry["acknowledged_by"]
    assert "default-pipeline" not in {
        e["playbook_id"]
        for e in inventory["entries"]
        if e["disposition"] in ("question_required", "invalid")
    }


async def test_editing_the_source_invalidates_the_waiver(handler):
    await _ack(handler, "default-pipeline", _GOOD_REASON)
    path = _source_path(handler)
    path.write_text(path.read_text() + "\n<!-- an edit -->\n", encoding="utf-8")

    inventory = await handler._cmd_playbook_migration_inventory({})
    entry = next(e for e in inventory["entries"] if e["playbook_id"] == "default-pipeline")
    assert entry["disposition"] == "question_required"
    assert entry["acknowledged_by"] is None


async def test_unacknowledge_restores_the_computed_disposition(handler):
    await _ack(handler, "default-pipeline", _GOOD_REASON)
    removed = await handler._cmd_playbook_migration_unacknowledge({"playbook_id": "default-pipeline"})
    assert removed == {"success": True, "playbook_id": "default-pipeline", "removed": 1}

    inventory = await handler._cmd_playbook_migration_inventory({})
    entry = next(e for e in inventory["entries"] if e["playbook_id"] == "default-pipeline")
    assert entry["disposition"] == "question_required"

    again = await handler._cmd_playbook_migration_unacknowledge({"playbook_id": "default-pipeline"})
    assert again["success"] is False


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


async def test_upsert_replaces_rather_than_duplicating(db):
    digest = "sha256:" + hashlib.sha256(b"x").hexdigest()
    for reason in ("the first justification", "the second justification"):
        await db.upsert_playbook_migration_ack(
            playbook_id="pb",
            scope="system",
            scope_identifier="",
            source_sha256=digest,
            reason=reason,
            acknowledged_by="operator",
        )
    rows = await db.list_playbook_migration_acks()
    assert len(rows) == 1
    assert rows[0]["reason"] == "the second justification"


async def test_storage_enforces_the_reason_floor(db):
    with pytest.raises(ValueError):
        await db.upsert_playbook_migration_ack(
            playbook_id="pb",
            scope="system",
            source_sha256="sha256:" + "a" * 64,
            reason="short",
            acknowledged_by="operator",
        )
    assert await db.list_playbook_migration_acks() == []


async def test_scope_identifier_is_part_of_the_key(db):
    digest = "sha256:" + "a" * 64
    for identifier in ("alpha", "beta"):
        await db.upsert_playbook_migration_ack(
            playbook_id="pb",
            scope="project",
            scope_identifier=identifier,
            source_sha256=digest,
            reason="a sufficiently long justification",
            acknowledged_by="operator",
        )
    rows = await db.list_playbook_migration_acks()
    assert {row["scope_identifier"] for row in rows} == {"alpha", "beta"}
    assert await db.delete_playbook_migration_ack(
        playbook_id="pb", scope="project", scope_identifier="alpha"
    )
    rows = await db.list_playbook_migration_acks()
    assert [row["scope_identifier"] for row in rows] == ["beta"]


async def test_delete_reports_a_miss(db):
    assert (
        await db.delete_playbook_migration_ack(playbook_id="nope", scope="system") is False
    )


# ---------------------------------------------------------------------------
# The waivers are operator-only
# ---------------------------------------------------------------------------


def test_write_commands_absent_from_every_shipped_profile():
    """A worker that can waive a broken playbook can disable its own reviewer."""
    import src.profiles as profiles_pkg

    defaults = Path(os.path.dirname(profiles_pkg.__file__)) / "defaults"
    forbidden = (
        "playbook_migration_acknowledge",
        "playbook_migration_unacknowledge",
    )
    offenders = []
    for profile in sorted(defaults.glob("*/profile.md")):
        text = profile.read_text(encoding="utf-8")
        offenders += [
            f"{profile.parent.name}: {name}" for name in forbidden if name in text
        ]
    assert not offenders, offenders


def test_commands_are_registered_on_the_command_handler():
    from src.commands.handler import CommandHandler
    from src.tools.definitions import _TOOL_CATEGORIES

    for name in (
        "playbook_migration_inventory",
        "playbook_migration_acknowledge",
        "playbook_migration_unacknowledge",
        "playbook_cutover_report",
    ):
        assert callable(getattr(CommandHandler, f"_cmd_{name}", None)), name
        assert _TOOL_CATEGORIES.get(name) == "playbook", name


def test_response_models_are_registered():
    from src.api.models.playbook_migration import RESPONSE_MODELS

    for name in (
        "playbook_migration_inventory",
        "playbook_migration_acknowledge",
        "playbook_migration_unacknowledge",
        "playbook_cutover_report",
    ):
        assert name in RESPONSE_MODELS, name


# ---------------------------------------------------------------------------
# Release check — the live-daemon half of the profile fingerprint gate
# ---------------------------------------------------------------------------


class _ReleaseHandler(_Handler):
    """The mixin plus the two collaborators the release check reads.

    ``_v2_lookups`` is the seam ``_release_check_activations`` uses to resolve
    each artifact profile against *this daemon's* registry, so the test can
    move one profile without touching `src/profiles/defaults/`.
    """

    def __init__(self, config, db, profiles) -> None:
        super().__init__(config, db, PIPELINE_FIXTURE.parent)
        from src.config import PlaybooksConfig

        config.playbooks = PlaybooksConfig()
        self._profiles = profiles

    async def _v2_lookups(self):
        from src.playbooks.validation import RegisteredEventLookup, RegistryContractLookup

        return RegistryContractLookup(), self._profiles, RegisteredEventLookup()


class _MovedProfiles:
    """The shipped lookup with one profile's policy replaced or removed."""

    def __init__(self, overrides) -> None:
        from src.playbooks.migration import shipped_profile_lookup

        self._shipped = shipped_profile_lookup()
        self._overrides = overrides

    def policy(self, profile_id):
        if profile_id in self._overrides:
            return self._overrides[profile_id]
        return self._shipped.policy(profile_id)


async def _activate_shipped_pipeline(handler):
    """Store and enable the reviewed `default-pipeline` artifact on ``handler``."""
    from src.playbooks.activation import profile_fingerprint
    from src.playbooks.artifact_store import ArtifactStore
    from src.playbooks.definition import load_definition_json

    fixture = (
        Path(__file__).resolve().parent
        / "fixtures"
        / "playbooks"
        / "v2"
        / "default-pipeline"
        / "artifact.json"
    )
    definition = load_definition_json(fixture.read_text(encoding="utf-8"))
    store = ArtifactStore(handler.config.compiled_root)
    aggregate = profile_fingerprint(dict(definition.compiled_against.profiles))
    ref = store.put(
        definition,
        source_digest=definition.source_hash,
        contract_fingerprint=definition.contract_fingerprint(),
        profile_fingerprint=aggregate,
        compiler_build=definition.compiler_build or "test-build",
        version=definition.version,
    )
    await handler.db.upsert_playbook_artifact(
        ref,
        scope="system",
        profile_fingerprint=aggregate,
        path=store.path_for(ref.artifact_sha256),
        size_bytes=len(store.canonical_bytes(definition)),
    )
    await handler.db.set_playbook_activation(
        playbook_id=ref.playbook_id,
        scope="system",
        scope_identifier="",
        artifact_sha256=ref.artifact_sha256,
        enabled=True,
        activated_by="operator",
        health="ready",
        reasons="[]",
    )
    return definition


# §5.5 — the two reports against real activation and artifact rows
# ---------------------------------------------------------------------------
#
# An activation row names an artifact hash and nothing else, so both reports
# have to join `playbook_artifacts` to say anything about the bytes that are
# live.  Every regression these tests pin was invisible to a double: reading
# `artifact_sha256` off an activation row returns `None` from a real database
# and whatever the double was handed from a fake one.


REPO_ROOT = Path(__file__).resolve().parent.parent
PIPELINE_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "playbooks" / "v2" / "default-pipeline"

#: Not one of the checked-in fixture ids, so `checked` naming it can only mean
#: the activation row itself was read.
PROBE_ID = "live-activation-probe"


def _probe_definition(*, commands: dict[str, str] | None = None):
    """The reviewed pipeline artifact, re-identified as an activation-only probe."""
    from src.playbooks.definition import load_definition_json

    definition = load_definition_json(
        (PIPELINE_FIXTURE / "artifact.json").read_text(encoding="utf-8")
    )
    compiled_against = definition.compiled_against
    if commands is not None:
        compiled_against = compiled_against.model_copy(update={"commands": dict(commands)})
    return definition.model_copy(update={"id": PROBE_ID, "compiled_against": compiled_against})


def _project_probe_definition(*, commands: dict[str, str] | None = None):
    from src.playbooks.definition import ProjectScope

    return _probe_definition(commands=commands).model_copy(
        update={"scope": ProjectScope(project_id="project-a")}
    )


def _current_commands(definition) -> dict[str, str]:
    """This build's execution fingerprints for the commands the artifact uses."""
    from src.commands.contracts import CONTRACTS
    from src.playbooks.migration import current_command_fingerprints

    live = current_command_fingerprints(CONTRACTS)
    return {name: live[name] for name in definition.compiled_against.commands if name in live}


async def _activate(
    handler,
    definition,
    *,
    enabled: bool = True,
    health: str = "ready",
    source_digest: str = "sha256:" + "e" * 64,
    scope: str = "system",
    scope_identifier: str = "",
    reviewed: bool = False,
):
    """Store the artifact bytes and write the artifact + activation rows."""
    from src.commands.contracts import CONTRACTS
    from src.playbooks.artifact_store import ArtifactStore

    store = ArtifactStore(
        handler.config.compiled_root,
        max_artifact_bytes=handler.config.playbooks.v2_max_artifact_bytes,
    )
    ref = store.put(
        definition,
        source_digest=source_digest,
        contract_fingerprint=str(CONTRACTS.registry_fingerprint()),
        profile_fingerprint="",
        compiler_build="test-build",
        version=1,
    )
    path = store.path_for(ref.artifact_sha256)
    await handler.db.upsert_playbook_artifact(
        ref,
        scope=scope,
        scope_identifier=scope_identifier,
        path=path,
        size_bytes=os.path.getsize(path),
    )
    await handler.db.set_playbook_activation(
        playbook_id=definition.id,
        scope=scope,
        scope_identifier=scope_identifier,
        artifact_sha256=ref.artifact_sha256,
        enabled=enabled,
        activated_by="operator",
        health=health,
        reasons="[]",
        reviewed_artifact_sha256=ref.artifact_sha256 if reviewed else None,
        reviewed_by="project-reviewer" if reviewed else None,
    )
    return ref


def _write_review(handler, ref, *, decision: str = "approved") -> None:
    directory = handler.reviewed_root / PROBE_ID
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "review.md").write_text(
        "---\n"
        f"playbook_id: {PROBE_ID}\n"
        f'artifact_sha256: "{ref.artifact_sha256}"\n'
        f'source_sha256: "{ref.source_digest}"\n'
        f"decision: {decision}\n"
        'reviewed_by: "Jack Kern <operator@example.invalid>"\n'
        'reviewed_at: "2026-09-03"\n'
        "---\n\nbody\n",
        encoding="utf-8",
    )


def _artifact_row(report: dict) -> dict:
    return next(row for row in report["artifacts"] if row["playbook_id"] == PROBE_ID)


async def test_release_check_reads_the_artifact_a_live_activation_names(handler):
    """The regression: `artifact_sha256` is not a column on an activation row.

    Reading it there yielded `None` for every row, so every enabled activation
    was skipped and the release check passed by checking nothing.
    """
    definition = _probe_definition()
    await _activate(handler, definition)

    report = await handler._cmd_playbook_release_check({})

    assert PROBE_ID in report["checked"]


async def test_release_check_reports_drift_in_a_live_activation(handler):
    stale_command = min(_probe_definition().compiled_against.commands)
    commands = _current_commands(_probe_definition())
    commands[stale_command] = "sha256:" + "d" * 64
    await _activate(handler, _probe_definition(commands=commands))

    report = await handler._cmd_playbook_release_check({})

    assert report["success"] is False
    row = next(entry for entry in report["stale"] if entry["playbook_id"] == PROBE_ID)
    assert row["origin"] == "activation"
    assert row["dependency"] == stale_command


async def test_release_check_passes_a_live_activation_compiled_against_this_build(handler):
    definition = _probe_definition(commands=_current_commands(_probe_definition()))
    await _activate(handler, definition)

    report = await handler._cmd_playbook_release_check({})

    assert PROBE_ID in report["checked"]
    assert [row for row in report["stale"] if row["playbook_id"] == PROBE_ID] == []


async def test_release_check_ignores_a_disabled_activation(handler):
    commands = _current_commands(_probe_definition())
    commands[min(commands)] = "sha256:" + "d" * 64
    await _activate(handler, _probe_definition(commands=commands), enabled=False)

    report = await handler._cmd_playbook_release_check({})

    assert PROBE_ID not in report["checked"]


async def test_cutover_report_carries_both_hashes_for_a_live_activation(handler):
    """`source_digest` lives on the artifact row, never on the activation row."""
    ref = await _activate(handler, _probe_definition())

    report = await handler._cmd_playbook_cutover_report({})

    row = _artifact_row(report)
    assert row["artifact_sha256"] == ref.artifact_sha256
    assert row["source_sha256"] == ref.source_digest
    assert row["activation_health"] == "ready"
    assert row["scope"] == "system"


async def test_cutover_report_blocks_when_no_review_names_the_live_artifact(handler):
    await _activate(handler, _probe_definition())

    report = await handler._cmd_playbook_cutover_report({})

    row = _artifact_row(report)
    assert row["reviewed_by"] is None and row["reviewed_at"] is None
    assert report["rollback_ready"] is False
    assert report["cutover_eligible"] is False
    assert any("no recorded review" in reason for reason in report["blocking_reasons"])


async def test_cutover_report_joins_the_recorded_review_of_the_live_artifact(handler):
    ref = await _activate(handler, _probe_definition())
    _write_review(handler, ref)

    report = await handler._cmd_playbook_cutover_report({})

    row = _artifact_row(report)
    assert row["reviewed_by"] == "Jack Kern <operator@example.invalid>"
    assert row["reviewed_at"] == "2026-09-03"
    assert not any("no recorded review" in reason for reason in report["blocking_reasons"])


async def test_cutover_report_uses_persisted_project_review_evidence(handler):
    """Project reviews live in the database, not the repository fixture tree."""
    ref = await _activate(
        handler,
        _project_probe_definition(),
        scope="project",
        scope_identifier="project-a",
        reviewed=True,
    )

    report = await handler._cmd_playbook_cutover_report({})

    row = _artifact_row(report)
    assert row["scope"] == "project"
    assert row["scope_identifier"] == "project-a"
    assert row["artifact_sha256"] == ref.artifact_sha256
    assert row["reviewed_by"] == "project-reviewer"
    assert row["reviewed_at"] is not None
    assert not any("no recorded review" in reason for reason in report["blocking_reasons"])


async def test_public_project_activation_persists_review_used_by_cutover_report(handler):
    """Public activation -> database -> report is one exact-hash evidence path."""
    from src.commands.contracts import CONTRACTS
    from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
    from src.config import PlaybooksConfig
    from src.playbooks.artifact_store import ArtifactStore
    from src.profiles.capabilities import DENY_ALL

    handler = _ActivationAndReportHandler(handler.config, handler.db, handler.reviewed_root)
    handler.config.playbooks = PlaybooksConfig(
        v2_api=True,
        v2_storage_enabled=True,
        v2_activation_writes=True,
    )
    definition = _project_probe_definition(
        commands=_current_commands(_project_probe_definition())
    )
    store = ArtifactStore(handler.config.compiled_root)
    ref = store.put(
        definition,
        source_digest="sha256:" + "e" * 64,
        contract_fingerprint=str(CONTRACTS.registry_fingerprint()),
        profile_fingerprint="",
        compiler_build="test-build",
        version=1,
    )
    path = store.path_for(ref.artifact_sha256)
    await handler.db.upsert_playbook_artifact(
        ref,
        scope="project",
        scope_identifier="project-a",
        path=path,
        size_bytes=os.path.getsize(path),
    )
    principal = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=DENY_ALL,
        session_id="supervisor-project-a",
        project_id="project-a",
        elevated=True,
    )

    with principal_context(principal):
        activated = await handler._cmd_playbook_activate(
            {
                "playbook_id": definition.id,
                "artifact_sha256": ref.artifact_sha256,
                "acknowledge_diff": ref.artifact_sha256,
            }
        )
    report = await handler._cmd_playbook_cutover_report({})

    assert activated["blocked"] is False
    row = _artifact_row(report)
    assert row["artifact_sha256"] == ref.artifact_sha256
    assert row["reviewed_by"] == "session:supervisor-project-a"
    assert row["reviewed_at"] is not None


async def test_project_review_is_invalidated_when_activation_bytes_change(handler):
    """Re-activating different bytes without a review cannot reuse old evidence."""
    definition = _project_probe_definition()
    first = await _activate(
        handler,
        definition,
        scope="project",
        scope_identifier="project-a",
        reviewed=True,
    )
    commands = dict(definition.compiled_against.commands)
    commands[min(commands)] = "sha256:" + "f" * 64
    second = await _activate(
        handler,
        _project_probe_definition(commands=commands),
        scope="project",
        scope_identifier="project-a",
    )
    assert second.artifact_sha256 != first.artifact_sha256

    report = await handler._cmd_playbook_cutover_report({})

    row = _artifact_row(report)
    assert row["artifact_sha256"] == second.artifact_sha256
    assert row["reviewed_by"] is None
    assert row["reviewed_at"] is None
    assert any("no recorded review" in reason for reason in report["blocking_reasons"])


async def test_cutover_report_ignores_a_review_of_different_bytes(handler):
    """A review is evidence about specific bytes, not about a playbook id."""
    ref = await _activate(handler, _probe_definition())
    _write_review(handler, ref)
    directory = handler.reviewed_root / PROBE_ID
    directory.joinpath("review.md").write_text(
        directory.joinpath("review.md")
        .read_text(encoding="utf-8")
        .replace(ref.artifact_sha256, "sha256:" + "f" * 64),
        encoding="utf-8",
    )

    report = await handler._cmd_playbook_cutover_report({})

    assert _artifact_row(report)["reviewed_by"] is None
    assert any("no recorded review" in reason for reason in report["blocking_reasons"])


async def test_cutover_report_ignores_a_rejected_review(handler):
    ref = await _activate(handler, _probe_definition())
    _write_review(handler, ref, decision="rejected")

    report = await handler._cmd_playbook_cutover_report({})

    assert _artifact_row(report)["reviewed_by"] is None
    assert any("no recorded review" in reason for reason in report["blocking_reasons"])


async def test_inventory_sees_the_source_drift_the_activation_row_cannot_show(handler):
    """The same join: without it the inventory's drift branches never ran.

    The probe activates `default-pipeline`'s installed source under an artifact
    whose `source_digest` is not that file's hash, which is exactly the
    "recompile and re-review" condition §3.7 must not let through.
    """
    definition = _probe_definition().model_copy(update={"id": "default-pipeline"})
    from src.commands.contracts import CONTRACTS
    from src.playbooks.artifact_store import ArtifactStore

    store = ArtifactStore(handler.config.compiled_root, max_artifact_bytes=1_048_576)
    ref = store.put(
        definition,
        source_digest="sha256:" + "e" * 64,
        contract_fingerprint=str(CONTRACTS.registry_fingerprint()),
        profile_fingerprint="",
        compiler_build="test-build",
        version=1,
    )
    path = store.path_for(ref.artifact_sha256)
    await handler.db.upsert_playbook_artifact(
        ref, scope="system", scope_identifier="", path=path, size_bytes=os.path.getsize(path)
    )
    await handler.db.set_playbook_activation(
        playbook_id="default-pipeline",
        scope="system",
        scope_identifier="",
        artifact_sha256=ref.artifact_sha256,
        enabled=True,
        activated_by="operator",
        health="ready",
        reasons="[]",
    )

    result = await handler._cmd_playbook_migration_inventory({})

    entry = next(e for e in result["entries"] if e["playbook_id"] == "default-pipeline")
    assert entry["artifact"]["artifact_sha256"] == ref.artifact_sha256
    assert any(reason["code"] == "compile_question" for reason in entry["reasons"])


@pytest.mark.parametrize(
    ("health", "enabled", "disposition", "reason_code"),
    [
        ("invalid", True, "invalid", "schema_violation"),
        ("question_required", True, "question_required", "compile_question"),
        ("stale_contract", True, "question_required", "stale_contract"),
        ("unavailable", True, "question_required", "compile_question"),
        ("disabled", False, "disabled", "operator_disabled"),
    ],
)
async def test_inventory_blocks_non_ready_health_from_joined_activation_rows(
    handler, health, enabled, disposition, reason_code
):
    """Every non-ready V2 health state carries a visible migration blocker."""
    source_path = Path(handler.config.vault_root) / "system/playbooks/default-pipeline.md"
    source_digest = "sha256:" + hashlib.sha256(source_path.read_bytes()).hexdigest()
    definition = _probe_definition().model_copy(update={"id": "default-pipeline"})
    await _activate(
        handler,
        definition,
        health=health,
        enabled=enabled,
        source_digest=source_digest,
    )

    result = await handler._cmd_playbook_migration_inventory({})

    entry = next(e for e in result["entries"] if e["playbook_id"] == "default-pipeline")
    assert entry["activation_health"] == health
    assert entry["disposition"] == disposition
    assert reason_code in {reason["code"] for reason in entry["reasons"]}
    assert entry["artifact"] is not None


async def test_inventory_never_reports_ready_without_a_valid_joined_artifact(handler):
    """A dangling activation row cannot satisfy the ready evidence contract."""
    await handler.db.set_playbook_activation(
        playbook_id="default-pipeline",
        scope="system",
        scope_identifier="",
        artifact_sha256=None,
        enabled=True,
        activated_by="operator",
        health="ready",
        reasons="[]",
    )

    result = await handler._cmd_playbook_migration_inventory({})

    entry = next(e for e in result["entries"] if e["playbook_id"] == "default-pipeline")
    assert entry["artifact"] is None
    assert entry["disposition"] == "invalid"
    assert "schema_violation" in {reason["code"] for reason in entry["reasons"]}


@pytest.mark.asyncio
async def test_release_check_rows_carry_the_daemons_live_profile_fingerprints(tmp_path, db):
    """`solid-harbor.54`: the command used to pass no profile fingerprints at all.

    Without them the profile half of `release_check` never ran, so a capability
    change to `reviewer` — which the shipped pipeline depends on only through
    `ensure_task` — could not stale anything.
    """
    from src.playbooks.migration import shipped_profile_fingerprints

    data_dir = str(tmp_path / "aq")
    os.makedirs(data_dir, exist_ok=True)
    ensure_default_playbooks(data_dir)
    handler = _ReleaseHandler(_Config(data_dir), db, _MovedProfiles({}))
    definition = await _activate_shipped_pipeline(handler)

    rows = await handler._release_check_activations()

    assert len(rows) == 1, rows
    assert rows[0]["artifact_profiles"] == dict(definition.compiled_against.profiles)
    assert rows[0]["current_profiles"] == {
        name: shipped_profile_fingerprints()[name]
        for name in definition.compiled_against.profiles
    }

    report = await handler._cmd_playbook_release_check({})
    assert report["success"] is True, report["stale"]


@pytest.mark.asyncio
async def test_release_check_reports_a_moved_delegated_profile(tmp_path, db):
    from src.profiles.capabilities import CapabilityPolicy

    data_dir = str(tmp_path / "aq")
    os.makedirs(data_dir, exist_ok=True)
    ensure_default_playbooks(data_dir)
    moved = _MovedProfiles(
        {"reviewer": CapabilityPolicy.from_namespaces(aq_commands=frozenset({"pr_merge"}))}
    )
    handler = _ReleaseHandler(_Config(data_dir), db, moved)
    await _activate_shipped_pipeline(handler)

    report = await handler._cmd_playbook_release_check({})

    assert report["success"] is False
    row = next(
        r
        for r in report["stale"]
        if r["origin"] == "activation" and r["dependency"] == "reviewer"
    )
    assert row["kind"] == "profile"
    assert row["change"] == "changed"
    assert row["playbook_id"] == "default-pipeline"


async def _drifting_pipeline_handler(tmp_path, db):
    """A handler whose live `default-pipeline` activation is genuinely stale."""
    from src.profiles.capabilities import CapabilityPolicy

    data_dir = str(tmp_path / "aq")
    os.makedirs(data_dir, exist_ok=True)
    ensure_default_playbooks(data_dir)
    moved = _MovedProfiles(
        {"reviewer": CapabilityPolicy.from_namespaces(aq_commands=frozenset({"pr_merge"}))}
    )
    handler = _ReleaseHandler(_Config(data_dir), db, moved)
    await _activate_shipped_pipeline(handler)
    assert (await handler._cmd_playbook_release_check({}))["success"] is False
    return handler


@pytest.mark.asyncio
async def test_release_check_does_not_honour_a_waiver_on_a_live_activation(tmp_path, db):
    """A waiver may not suppress the check for a playbook that is still running.

    `sound-horizon-20`: the acknowledgement writes one row in
    `playbook_migration_acks` and never touches `playbook_activations`, so
    "acknowledged" and "enabled" were true at once.  The check used to skip on
    the waiver alone, which certified a stale artifact the daemon was really
    executing.  The waiver is only a decision about a playbook taken out of
    service, so an enabled activation stays compared.
    """
    handler = await _drifting_pipeline_handler(tmp_path, db)

    result = await _ack(handler, "default-pipeline", _GOOD_REASON)
    assert result["success"] is True, result

    report = await handler._cmd_playbook_release_check({})
    assert report["success"] is False
    row = next(
        r
        for r in report["stale"]
        if r["origin"] == "activation" and r["dependency"] == "reviewer"
    )
    assert row["playbook_id"] == "default-pipeline"


@pytest.mark.asyncio
async def test_release_check_honours_a_waiver_once_the_activation_is_disabled(tmp_path, db):
    """The escape hatch still exists — for a playbook that is no longer live."""
    handler = await _drifting_pipeline_handler(tmp_path, db)
    result = await _ack(handler, "default-pipeline", _GOOD_REASON)
    assert result["success"] is True, result

    await handler.db.set_playbook_activation(
        playbook_id="default-pipeline",
        scope="system",
        scope_identifier="",
        artifact_sha256=None,
        enabled=False,
        activated_by="operator",
        health="ready",
        reasons="[]",
    )

    report = await handler._cmd_playbook_release_check({})
    assert report["success"] is True, report["stale"]
    # The checked-in fixture is still compared — it is held to the shipped
    # profiles, which the waiver says nothing about.  What the disabled
    # activation removes is the row this daemon serves.
    assert [row for row in report["stale"] if row["origin"] == "activation"] == []


# ---------------------------------------------------------------------------
# Live evidence the release check could not read (prime-zenith-66)
# ---------------------------------------------------------------------------
#
# Each of these used to return the payload of a clean fleet — `success: True`,
# the four shipped fixtures in `checked`, no stale rows — from a daemon that had
# read none of its own activations.  The release check is the gate that says
# "every enabled activation was compared"; it may not make that claim about
# evidence it never collected.


class _FailingActivations:
    """A repository whose activation read raises, wrapping a working one."""

    def __init__(self, inner, exc: Exception) -> None:
        self._inner = inner
        self._exc = exc

    def __getattr__(self, item):
        return getattr(self._inner, item)

    async def list_playbook_activations_with_artifacts(self, *args, **kwargs):
        raise self._exc

    async def list_playbook_activations(self, *args, **kwargs):
        raise self._exc


@pytest.mark.asyncio
async def test_release_check_blocks_when_the_activation_query_fails(tmp_path, db):
    data_dir = str(tmp_path / "aq")
    os.makedirs(data_dir, exist_ok=True)
    ensure_default_playbooks(data_dir)
    handler = _ReleaseHandler(_Config(data_dir), db, _MovedProfiles({}))
    await _activate_shipped_pipeline(handler)
    handler.db = _FailingActivations(db, RuntimeError("connection reset"))

    report = await handler._cmd_playbook_release_check({})

    assert report["success"] is False
    assert {row["source"] for row in report["evidence_errors"]} >= {"activations"}
    assert any(
        "activations" in reason and "connection reset" in reason
        for reason in report["blocking_reasons"]
    )


@pytest.mark.asyncio
async def test_release_check_blocks_when_the_artifact_store_is_unavailable(
    tmp_path, db, monkeypatch
):
    data_dir = str(tmp_path / "aq")
    os.makedirs(data_dir, exist_ok=True)
    ensure_default_playbooks(data_dir)
    handler = _ReleaseHandler(_Config(data_dir), db, _MovedProfiles({}))
    await _activate_shipped_pipeline(handler)

    from src.playbooks import artifact_store

    def _boom(*args, **kwargs):
        raise OSError("compiled root is not readable")

    monkeypatch.setattr(artifact_store, "ArtifactStore", _boom)

    report = await handler._cmd_playbook_release_check({})

    assert report["success"] is False
    assert {row["source"] for row in report["evidence_errors"]} >= {"artifact_store"}
    # Every enabled activation is named, not merely the store.
    assert [row["playbook_id"] for row in report["unverified"]] == ["default-pipeline"]
    assert report["unverified"][0]["reason"] == "artifact_store_unavailable"
    # The checked-in fixture of the same name is still compared; what is
    # missing is the *activation* row this daemon serves, and nothing claims
    # otherwise.
    assert [row for row in report["stale"] if row["origin"] == "activation"] == []


@pytest.mark.asyncio
async def test_release_check_blocks_when_an_activated_artifact_cannot_be_loaded(
    tmp_path, db
):
    data_dir = str(tmp_path / "aq")
    os.makedirs(data_dir, exist_ok=True)
    ensure_default_playbooks(data_dir)
    handler = _ReleaseHandler(_Config(data_dir), db, _MovedProfiles({}))
    definition = await _activate_shipped_pipeline(handler)

    from src.playbooks.artifact_store import ArtifactStore

    store = ArtifactStore(handler.config.compiled_root)
    sha = definition.artifact_sha256()
    Path(store.path_for(sha)).unlink()

    report = await handler._cmd_playbook_release_check({})

    assert report["success"] is False
    row = next(r for r in report["unverified"] if r["playbook_id"] == "default-pipeline")
    assert row["reason"] == "artifact_unreadable"
    assert row["artifact_sha256"] == sha
    assert [r for r in report["stale"] if r["origin"] == "activation"] == []


class _FailingLookups(_ReleaseHandler):
    async def _v2_lookups(self):
        raise RuntimeError("profile registry is not loaded")


@pytest.mark.asyncio
async def test_release_check_blocks_when_the_profile_registry_is_unavailable(tmp_path, db):
    """A daemon that cannot read its own profiles may not borrow the shipped ones."""
    data_dir = str(tmp_path / "aq")
    os.makedirs(data_dir, exist_ok=True)
    ensure_default_playbooks(data_dir)
    handler = _FailingLookups(_Config(data_dir), db, _MovedProfiles({}))
    await _activate_shipped_pipeline(handler)

    report = await handler._cmd_playbook_release_check({})

    assert report["success"] is False
    assert {row["source"] for row in report["evidence_errors"]} >= {"profile_registry"}
    row = next(r for r in report["unverified"] if r["playbook_id"] == "default-pipeline")
    assert row["reason"] == "profile_registry_unavailable"


@pytest.mark.asyncio
async def test_release_check_reports_an_enabled_activation_with_no_artifact(tmp_path, db):
    data_dir = str(tmp_path / "aq")
    os.makedirs(data_dir, exist_ok=True)
    ensure_default_playbooks(data_dir)
    handler = _ReleaseHandler(_Config(data_dir), db, _MovedProfiles({}))
    await _activate_shipped_pipeline(handler)
    await handler.db.set_playbook_activation(
        playbook_id="orphan-one",
        scope="system",
        scope_identifier="",
        artifact_sha256=None,
        enabled=True,
        activated_by="operator",
        health="unavailable",
        reasons="[]",
    )

    report = await handler._cmd_playbook_release_check({})

    assert report["success"] is False
    row = next(r for r in report["unverified"] if r["playbook_id"] == "orphan-one")
    assert row["reason"] == "no_active_artifact"


@pytest.mark.asyncio
async def test_a_clean_live_release_check_reports_no_unread_evidence(tmp_path, db):
    data_dir = str(tmp_path / "aq")
    os.makedirs(data_dir, exist_ok=True)
    ensure_default_playbooks(data_dir)
    handler = _ReleaseHandler(_Config(data_dir), db, _MovedProfiles({}))
    await _activate_shipped_pipeline(handler)

    report = await handler._cmd_playbook_release_check({})

    assert report["success"] is True, report["blocking_reasons"]
    assert report["evidence_errors"] == []
    assert report["unverified"] == []
    assert report["blocking_reasons"] == []


@pytest.mark.asyncio
async def test_doctor_release_check_warns_when_the_daemon_cannot_read_activations(
    tmp_path, db
):
    """`_check_stale_artifacts` swallowed the same exception into `activations = []`."""
    from src.config import AppConfig
    from src.doctor.models import DoctorContext, Severity
    from src.doctor.playbook_v2_checks import _check_stale_artifacts

    data_dir = str(tmp_path / "aq")
    os.makedirs(data_dir, exist_ok=True)
    ensure_default_playbooks(data_dir)
    handler = _ReleaseHandler(_Config(data_dir), db, _MovedProfiles({}))
    await _activate_shipped_pipeline(handler)
    handler.db = _FailingActivations(db, RuntimeError("connection reset"))

    result = await _check_stale_artifacts(
        DoctorContext(config=AppConfig(), handler=handler)
    )

    assert result.severity is Severity.WARN
    assert "connection reset" in result.detail or any(
        "connection reset" in reason for reason in result.data.get("blocking_reasons", [])
    )
