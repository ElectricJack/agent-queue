"""Physical-line ownership and table integration ratchets.

During replacement the *fixed* recorded baseline permits 15,000 overlapping
lines. The retired batch exception stays historical; the separately approved
hand-integration reconciliation has an exact cap. Set the manifest
phase to s4_modules when retiring the legacy modules; that immediately enforces
the final caps, with no allowance to carry forward.
"""

from __future__ import annotations

import ast
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import runpy
import sys

import pytest

from tests import integration_ownership as ownership

ROOT = Path(__file__).resolve().parents[1]
# Source inspection reads these trees rather than importing them, so declare
# their generator data inputs explicitly.
BAZEL_DATA = ("src/", "dashboard/src/", "tests/integration_ownership.json")


@pytest.fixture(autouse=True)
def _pg_backend():
    """Pure source inspection: do not allocate a test database."""


@pytest.fixture(scope="module")
def manifest():
    return ownership.load_manifest()


@pytest.fixture(scope="module")
def report(manifest):
    return ownership.inventory(ROOT, manifest)


def test_fixed_baseline_and_allowance(manifest):
    baseline = manifest["baseline"]
    assert baseline["revision"] == ownership.PINNED_REVISION
    assert manifest["landing"]["revision"] == ownership.LANDING_REVISION
    assert baseline["groups"]["engine"] == 82_627
    assert baseline["groups"]["companions"] == 15_818
    assert baseline["groups"]["policy"] == 478
    assert baseline["groups"]["guards"] == 639
    assert baseline["declaration_lines"] == 2_086
    assert baseline["total"] == ownership.BASELINE_OWNED_LINES == 140_499
    assert ownership.BASELINE_ENGINE_LINES == 82_627
    assert sum(baseline["groups"].values()) == sum(baseline["files"].values()) == 140_499
    assert ownership.digest(json.dumps(baseline, sort_keys=True)) == (
        "8b21a5fc2d1eba6732c2c3dd2abdf9d3025b38e55f54420415482c67553ee763"
    )
    assert ownership.REPLACEMENT_ALLOWANCE == 15_000
    assert (ownership.FINAL_ENGINE_CAP, ownership.FINAL_TOTAL_CAP,
            ownership.FINAL_TABLE_CAP, ownership.TARGET_TABLES) == (11_999, 19_999, 10, 5)


def test_integration_size_and_table_budget(report, manifest):
    assert ownership.budget_errors(report, manifest) == []


def test_pinned_table_predicate_names_all_45(manifest):
    names = set(manifest["baseline"]["tables"])
    assert len(names) == 45
    assert ownership.EXTRA_TABLES <= names
    assert all(ownership.integration_table(name) for name in names)
    assert ownership.RETAINED_TABLES <= names
    assert not ownership.integration_table("tasks")
    assert not ownership.integration_table("op_events")


def test_replacement_adds_no_new_integration_storage_category(report, manifest):
    # The approved author-close category remains visible and charged. No
    # unreviewed category inherits this exact feature disposition.
    assert manifest["storage_dispositions"] == {
        "integration_close_attempts": {
            "task": "stark-willow-95.15",
            "authority": "Approved fast-smooth-integration section 5 and current task close "
                         "contract, 2026-10-21",
            "purpose": "Version-fenced retained author candidate and wait evidence, never delivery "
                       "truth. Additive table charged; no cap increase.",
        },
    }
    approved = set(manifest["storage_dispositions"]) if manifest["phase"] == "transition" else set()
    assert report["tables"].keys() <= manifest["baseline"]["tables"].keys() | approved, (
        "Replacement reshapes the existing five tables; a renamed journal or "
        "new storage category needs a design disposition, not unused table allowance."
    )


def test_inventory_covers_named_shared_surfaces(report, manifest):
    # Evidence assertions protect against returning to the known lower bound.
    required = {
        "src/orchestrator/core.py", "src/commands/task_commands.py",
        "src/api/models/task.py", "src/database/tables.py", "src/git/manager.py",
        "src/prime/sections.py", "src/sessions/reconciler.py",
        "src/database/queries/claim_queries.py", "src/database/queries/hierarchy_queries.py",
        "src/commands/contracts/builtin.py", "src/api/scope.py", "src/api/target_scope.py",
        "src/tools/definitions.py", "dashboard/src/components/EpicDelivery.tsx",
        "dashboard/src/components/epicDeliveryFormat.ts",
        "dashboard/src/pages/TaskDetail.tsx",
        "src/prompts/integration_playbooks/root-train.md",
    }
    assert required <= report["spans"].keys()
    assert all(manifest["exclusions"].values())
    assert all(entry["reason"] for entry in manifest["storage_sinks"].values())
    assert {entry["disposition"] for entry in manifest["storage_sinks"].values()} <= {
        "ordinary", "intent", "retire",
    }
    assert "src/database/tables.py::jobs['integration_operation_id']" in manifest["shared_state"]
    assert "src/database/tables.py::playbook_pending_events['protected']" in manifest["shared_state"]


def test_recorded_ownership_report_and_landing_delta(manifest):
    recorded = json.loads((ROOT / "tests/integration_ownership_report.json").read_text())
    assert recorded["complete_baseline"] == manifest["baseline"]
    assert recorded["actual_landing"] == manifest["landing"]
    assert recorded["shared_definitions"] == manifest["shared"]
    assert recorded["transition"]["original_owned_total_cap"] == 155_499
    assert recorded["transition"]["owned_total_cap"] == ownership.transition_owned_cap(manifest)
    assert recorded["transition"]["batch_exception"] is None
    assert "transition_exception" not in manifest
    assert recorded["transition"]["expires_at"] == "s4_modules"
    assert recorded["landing_delta"]["owned_lines"] == -2_431
    assert recorded["landing_delta"]["remaining_owned_allowance"] == 17_431


def _fixture_manifest(root: Path) -> dict:
    ownership.sources.cache_clear()
    sources = ownership.sources(root)
    fingerprints = [unit.fingerprint for source in sources.values() for unit in source.units
                    if source.path.startswith("src/integration/")]
    return {
        "phase": "transition", "dedicated": {"engine": ["src/integration/*.py"]},
        "shared": {}, "exclusions": {}, "owned_fingerprints": fingerprints,
        "owned_file_fingerprints": [hashlib.sha256(source.data).hexdigest()
                                    for source in sources.values()],
        "storage_sinks": {}, "baseline": {"groups": {"engine": 100}, "total": 100},
    }


def _write(root: Path, path: str, text: str) -> Path:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    ownership.sources.cache_clear()
    return target


def test_physical_lines_include_comments_blanks_and_union_overlaps(tmp_path):
    _write(tmp_path, "src/integration/helper.py",
           "# attached comment\n\ndef integration_helper():\n    return 1\n\n# no newline")
    manifest = _fixture_manifest(tmp_path)
    result = ownership.inventory(tmp_path, manifest)
    assert result["total"] == 5  # splitlines() would incorrectly return 6
    assert result["groups"]["engine"] == 5
    source = ownership.sources(tmp_path)["src/integration/helper.py"]
    manifest["shared"] = {unit.key: {} for unit in source.units}
    assert ownership.inventory(tmp_path, manifest)["total"] == 5


def test_moving_owned_definition_outside_engine_does_not_lower_total(tmp_path):
    text = "# comment\n\ndef pure_helper(value):\n    # no ownership keyword\n    return value + 1\n"
    original = _write(tmp_path, "src/integration/helper.py", text)
    manifest = _fixture_manifest(tmp_path)
    before = ownership.inventory(tmp_path, manifest)
    original.unlink()
    _write(tmp_path, "src/git/renamed.py", text)
    after = ownership.inventory(tmp_path, manifest)
    assert before["total"] == after["total"] == 5
    assert after["spans"]["src/git/renamed.py"] == [1, 2, 3, 4, 5]


def test_whole_file_move_preserves_docstring_imports_and_trailing_blanks(tmp_path):
    text = '\"\"\"Owned module docs.\"\"\"\nimport os\n\ndef pure_helper():\n    return 1\n\n'
    original = _write(tmp_path, "src/integration/helper.py", text)
    manifest = _fixture_manifest(tmp_path)
    before = ownership.inventory(tmp_path, manifest)
    original.unlink()
    _write(tmp_path, "src/git/moved.py", text)
    after = ownership.inventory(tmp_path, manifest)
    assert before["total"] == after["total"] == 6


def test_extracting_short_owned_definition_still_charges_it(tmp_path):
    original = _write(tmp_path, "src/integration/helper.py",
                      "def pure_helper():\n    return 12345\n\n# module footer\n")
    manifest = _fixture_manifest(tmp_path)
    original.write_text("# module footer\n")
    _write(tmp_path, "src/commands/moved.py", "def pure_helper():\n    return 12345\n")
    assert ownership.inventory(tmp_path, manifest)["spans"]["src/commands/moved.py"] == [1, 2]


@pytest.mark.parametrize("text", [
    "from src.integration.lock import acquire as renamed\n\n"
    "def new_helper():\n    return renamed()\n",
    "from sqlalchemy import select\n\n"
    "def new_helper(db):\n    return db.execute(select(project_integration_leases))\n",
    "REGISTRY = {'integration_hidden': hidden_handler}\n",
    "def integration_hidden():\n    return 1\n",
])
def test_unknown_owned_definitions_fail_coverage(tmp_path, text):
    manifest = _fixture_manifest(tmp_path)
    _write(tmp_path, "src/git/new_adapter.py", text)
    result = ownership.inventory(tmp_path, manifest)
    assert result["unknown"]
    assert result["total"] > 0  # omissions are charged even when coverage fails


@pytest.mark.parametrize("text", [
    "async def replay(db):\n"
    "    await db.set_task_meta('task', 'ref_updates', {'expected_oid': 'x', 'new_oid': 'y'})\n",
    "def replay(conn):\n    conn.execute(insert(op_events).values(ref='main', new_oid='y'))\n",
    "async def replay(db):\n    await db.log_event('ref_changed', payload='replay')\n",
    "from src.database.tables import task_metadata as hidden\n"
    "def replay(conn):\n    conn.execute(insert(hidden).values(key='ref_updates'))\n",
    "def replay(conn):\n    conn.execute(text('INSERT INTO op_events VALUES (1)'))\n",
    "def replay(conn):\n    conn.execute(insert(system_config).values(key='ref_updates'))\n",
    "from src.database.tables import system_config as hidden\n"
    "def replay(conn):\n    conn.execute(update(hidden).values(value='replay'))\n",
    "def replay(conn):\n    conn.execute(text('DELETE FROM system_config'))\n",
    "from src.database.tables import events as hidden\n"
    "def replay(conn):\n    conn.execute(insert(hidden).values(payload='ref_updates'))\n",
])
def test_hidden_journal_writers_fail_without_integration_name(tmp_path, text):
    manifest = _fixture_manifest(tmp_path)
    _write(tmp_path, "src/database/queries/innocent.py", text)
    assert ownership.inventory(tmp_path, manifest)["unknown_sinks"]


def test_table_predicate_uses_ast_declarations_and_sql_names():
    text = '''
from sqlalchemy import Table as T
not_a_table = "integration_fake"
# integration_comment = Table("integration_comment", metadata)
renamed = T("project_integration_schedules", metadata)
other = T("project_integration_leases", metadata)
normal = T("tasks", metadata)
prefixed = T("integration_new", metadata)
'''
    assert set(ownership.table_declarations(ast.parse(text))) == {
        "project_integration_schedules", "project_integration_leases", "integration_new",
    }


def test_new_table_in_another_file_is_counted(tmp_path):
    manifest = _fixture_manifest(tmp_path)
    _write(tmp_path, "src/git/schema.py", 'x = Table("integration_new", metadata)\n')
    assert "integration_new" in ownership.inventory(tmp_path, manifest)["tables"]


def test_pure_helper_imported_back_into_engine_requires_ownership(tmp_path):
    manifest = _fixture_manifest(tmp_path)
    _write(tmp_path, "src/git/hidden.py", "def pure_helper():\n    return 1\n")
    _write(tmp_path, "src/integration/use.py",
           "from src.git.hidden import pure_helper as renamed\n"
           "def run():\n    return renamed()\n")
    result = ownership.inventory(tmp_path, manifest)
    assert any("src/git/hidden.py::pure_helper" in key for key in result["unknown"])
    assert result["spans"]["src/git/hidden.py"] == [1, 2]


def test_alias_of_shared_owned_symbol_discovers_new_caller(tmp_path):
    manifest = _fixture_manifest(tmp_path)
    _write(tmp_path, "src/git/shared.py", "def publish_expected():\n    return 1\n")
    manifest["shared"] = {"src/git/shared.py::publish_expected": {}}
    _write(tmp_path, "src/commands/new.py",
           "from src.git.shared import publish_expected as renamed\n"
           "def handler():\n    return renamed()\n")
    result = ownership.inventory(tmp_path, manifest)
    assert any("src/commands/new.py::handler" in key for key in result["unknown"])


def test_existing_generic_storage_call_requires_review_when_context_changes(tmp_path):
    manifest = _fixture_manifest(tmp_path)
    _write(tmp_path, "src/commands/ordinary.py",
           "async def save(db, task):\n    key = 'note'\n"
           "    await db.set_task_meta(task, key, 'value')\n")
    manifest["storage_sinks"] = ownership.sources(tmp_path)["src/commands/ordinary.py"].sinks
    _write(tmp_path, "src/commands/ordinary.py",
           "async def save(db, task):\n    key = 'ref_updates'\n"
           "    await db.set_task_meta(task, key, 'value')\n")
    assert ownership.inventory(tmp_path, manifest)["unknown_sinks"]


def test_renamed_owned_table_cannot_hide_from_table_budget(tmp_path):
    manifest = _fixture_manifest(tmp_path)
    _write(tmp_path, "src/database/innocent.py", 'renamed = Table("ref_updates", metadata)\n')
    _write(tmp_path, "src/integration/use.py", "from src.database.innocent import renamed\n")
    assert "ref_updates" in ownership.inventory(tmp_path, manifest)["tables"]


def test_generic_table_in_mixed_import_keeps_integration_schema_charged(tmp_path):
    manifest = _fixture_manifest(tmp_path)
    _write(tmp_path, "src/database/tables.py",
           'human_asks = Table("human_asks", metadata)\n'
           'integration_subjects = Table("integration_subjects", metadata)\n')
    _write(tmp_path, "src/integration/use.py",
           "from src.database.tables import human_asks, integration_subjects\n"
           "def apply():\n    return human_asks, integration_subjects\n")
    manifest["generic_dependencies"] = {
        "src/database/tables.py::human_asks": "Shared human decision evidence.",
    }
    manifest["shared"] = {"src/database/tables.py::integration_subjects": {}}
    result = ownership.inventory(tmp_path, manifest)
    assert result["unknown"] == []
    assert set(result["tables"]) == {"integration_subjects"}
    assert result["spans"]["src/database/tables.py"] == [2]
    assert result["spans"]["src/integration/use.py"] == [1, 2, 3]


def test_human_ask_evidence_keeps_source_authority_charged(report, manifest):
    for name in ("human_asks", "ask_decision_previews", "ask_decisions"):
        assert f"src/database/tables.py::{name}" in manifest["generic_dependencies"]
        assert name not in report["tables"]
    path = "src/asks/sources.py"
    source = ownership.python_source(path, (ROOT / path).read_bytes())
    units = {unit.name: unit for unit in source.units}
    for name in ("IntegrationAdapter", "authorize_source"):
        assert f"{path}::{name}" in manifest["shared"]
        assert set(range(units[name].start, units[name].end + 1)) <= set(report["spans"][path])


def test_ingest_storage_keeps_outbox_adapter_charged(report, manifest):
    for name in ("ingest_batches", "ingest_batch_members", "ingest_batch_receipts"):
        assert f"src/database/tables.py::{name}" in manifest["generic_dependencies"]
        assert name not in report["tables"]
    path = "src/integration/outbox.py"
    source = ownership.python_source(path, (ROOT / path).read_bytes())
    adapter = next(unit for unit in source.units if unit.name == "enqueue_batch_event")
    assert set(range(adapter.start, adapter.end + 1)) <= set(report["spans"][path])
    assert "integration_outbox" in report["tables"]


def test_operator_decision_storage_keeps_stack_authorization_charged(report, manifest):
    assert "src/database/tables.py::operator_decisions" in manifest["generic_dependencies"]
    assert "operator_decisions" not in report["tables"]
    path = "src/integration/stacked_branches.py"
    assert report["spans"][path] == list(range(1, (ROOT / path).read_bytes().count(b"\n") + 1))


def test_database_recovery_keeps_shared_integration_primitives_charged(report, manifest):
    # These operations-only entry points reuse owned config loaders. Their
    # callers do not acquire integration ownership just by reading config.
    for path, name in (
        ("src/cli/daemon.py", "_database_matches_compose"),
        ("src/cli/doctor.py", "_offline_doctor"),
    ):
        assert f"{path}::{name}" in manifest["exclusions"]
        source = ownership.python_source(path, (ROOT / path).read_bytes())
        unit = next(unit for unit in source.units if unit.name == name)
        assert not set(range(unit.start, unit.end + 1)) & set(report["spans"].get(path, []))
    # Keep the existing shared ownership, including config and schema recovery.
    for path, name in (
        ("src/config.py", "load_config"),
        ("src/cli/db.py", "_load_config"),
        ("src/cli/daemon.py", "_backup_database"),
        ("src/cli/daemon.py", "_database_is_at_head"),
    ):
        assert f"{path}::{name}" in manifest["shared"]
        source = ownership.python_source(path, (ROOT / path).read_bytes())
        unit = next(unit for unit in source.units if unit.name == name)
        assert set(range(unit.start, unit.end + 1)) <= set(report["spans"][path])


def test_transition_budget_cannot_reset_to_current_counts(manifest):
    copy = deepcopy(manifest)
    copy["phase"] = "transition"
    copy.pop("transition_exception", None)
    copy.pop("hand_integration_reconciliation", None)
    baseline = copy["baseline"]
    result = {"groups": {"engine": baseline["groups"]["engine"] + 15_001},
              "total": baseline["total"] + 15_001,
              "tables": baseline["tables"], "legacy_sinks": []}
    errors = ownership.budget_errors(result, copy)
    assert len(errors) == 2
    assert all("exceeds" in error for error in errors)
    # Even editing the snapshot cannot silently make a failing build pass.
    copy["baseline"]["total"] += 99_999
    copy["baseline"]["groups"]["engine"] += 99_999
    assert ownership.budget_errors(result, copy) == errors


@pytest.mark.parametrize("phase", ["s4_modules", "final"])
def test_allowance_expires_at_module_retirement(manifest, phase):
    copy = deepcopy(manifest)
    copy["phase"] = phase
    result = {"groups": {"engine": 12_000}, "total": 20_000,
              "tables": {str(n): None for n in range(11)}, "legacy_sinks": []}
    assert len(ownership.budget_errors(result, copy)) == 3
    result.update(groups={"engine": 11_999}, total=19_999,
                  tables={str(n): None for n in range(10)})
    assert ownership.budget_errors(result, copy) == []
    result["legacy_sinks"] = ["src/foo.py::ref_replay"]
    assert ownership.budget_errors(result, copy)


def test_size_ratchet_runs_in_default_ci_and_selection_catalogue():
    catalogue = json.loads((ROOT / "tests/selection_catalogue.json").read_text())
    assert "tests/test_integration_size.py" in json.dumps(catalogue)
    workflow = (ROOT / ".github/workflows/tests.yml").read_text()
    assert "pytest tests/" in workflow
    assert "--ignore=tests/test_integration_size.py" not in workflow
    assert not getattr(test_integration_size_and_table_budget, "pytestmark", [])


def test_ingestion_evidence_keeps_release_and_reviewer_fences_charged(report, manifest):
    generic = {
        "src/asks/ingestion.py": ("approved_packet",),
        "src/asks/operations.py": ("operation",),
        "src/asks/batch_scope.py": ("scope_source", "scope_ask"),
        "src/api/scope.py": ("held_task_for_session", "fidelity_review_task_for_session"),
        "src/database/queries/proposal_queries.py": ("detect_cycles",),
        "src/database/queries/ingest_batch_queries.py": (
            "get_batch", "members", "live_digest", "passing_receipt",
        ),
        "src/task_graph/batch_digest.py": (
            "normalize_description", "canonical_batch_structure", "compute_structure_digest",
        ),
    }
    charged = {
        "src/commands/principal.py": ("batch_release_actor",),
        "src/commands/proposal_commands.py": (
            "TaskProposalCommandsMixin._create_ingest_batch",
            "TaskProposalCommandsMixin._update_held_batch",
            "TaskProposalCommandsMixin._cmd_task_batch_release",
        ),
        "src/commands/fidelity_review_commands.py": (
            "FidelityReviewCommandsMixin._cmd_task_batch_review_record",
        ),
    }
    for expected, paths in ((False, generic), (True, charged)):
        for path, names in paths.items():
            source = ownership.python_source(path, (ROOT / path).read_bytes())
            units = {unit.name: unit for unit in source.units}
            for name in names:
                unit = units[name]
                rows = set(range(unit.start, unit.end + 1))
                actual = set(report["spans"].get(path, []))
                if expected:
                    assert rows <= actual, unit.key
                    assert unit.key not in manifest["generic_dependencies"]
                else:
                    assert unit.key in manifest["generic_dependencies"]
                    assert rows.isdisjoint(actual), unit.key


@pytest.mark.parametrize("field,value", [
    (None, None),
    ("batch_id", "another-batch"),
    ("decision_message_id", "another-decision"),
    ("measured_source_revision", "another-source"),
    ("delta", 1_294),
    ("measured_owned_total", 156_793),
    ("original_owned_total_cap", 155_500),
])
def test_batch_exception_cannot_be_reinstated_reused_or_enlarged(manifest, field, value):
    copy = deepcopy(manifest)
    copy["transition_exception"] = dict(ownership.TRANSITION_BATCH_EXCEPTION)
    if field is not None:
        copy["transition_exception"][field] = value
    assert ownership.budget_errors({}, copy) == ["unapproved integration transition exception"]

# Exact historical records survive A5 sharding and retirement independently of
# the current cap. These pins replace mutually contradictory active snapshots.
HISTORICAL_POLICY_DIGESTS = {'actual_landing': 'f253604817f43961ae7f52e32f16d7dc7cb986e07b513e8d9105a6df12a4f40b',
 'assembled_epic_source_review': '784e46a10f6bc4b83657439ee2e39f84ad616123e791603eb4f429554a82afbc',
 'authenticated_push_merge_measurement': '62673e976587c68ca3ad4422ac4662bcfaab4d8bfe54e9d5d0fc3d0c2a29c94e',
 'batch_fixer_launch_recovery_measurement': 'f757868d37beace5c47161e7e787b5b22d08efd1c550c64b023afe35f553bd12',
 'batch_fixer_orphaned_launch_measurement': 'c9960879ef289b4c0a65b03f4825cf61c0033c123bc0fe707e12135725382fb4',
 'ci_wait_repository_mapping_review': '1156007a48c062c3795e3c7e5f238b19c9a4bd4a08758b3bfd643df14021d944',
 'combined_preparation_workspace_safety_review': '23f08a23c2864ba917a3bf9a40ab930762b841b15d3c4c6ce5fcf279d4b8b18c',
 'complete_baseline': '8b21a5fc2d1eba6732c2c3dd2abdf9d3025b38e55f54420415482c67553ee763',
 'daemon_latency_measurement': '71ff4bc73270bfe3f7962b2d3393abc538c0e659ddd29ae706e50b11cd3d8b1d',
 'daemon_latency_merge_measurement': 'ffca31d3ba23d40229cd5edf7fe64af7e2d3ac1646deb9e53772805b3f4ecdb5',
 'dashboard_pr_snapshot_measurement': '08c54aae43b27eb703d84d576a5f743899160ea836fb6f66ab3d385044fc8c6f',
 'dashboard_pr_snapshot_merge_measurement': '2be7f3eb15d16e9fc11c709d6ffb922082307ccac7d99ffc4e5bc0b2014e6546',
 'dedicated_paths': '8dcf8a0391e7e732e3b89dcdf253a3ce7893bf1308351340811e2fce0d0cde64',
 'deferred_worker_recovery_review': '4bb6b0c368109df207427e29b0105895f3eae58e56675453fa13575701778e00',
 'direct_completion_measurement': '1e78ca0fb9e099a80a90dbeed847c65807d3697784d8127050a0ffd7d15757ec',
 'direct_completion_measurement_history': '7d31630d0cc2bd8fbd6a9bb9925f83e1749b109273ae8ae7fe277ce4a67fd46b',
 'escalation_delivery_source_ci_repair': '9765eec1b346ea890d788bfe20a73b06669c58552f2287dc537450338f745a69',
 'escalation_noise_measurement': '9563ec81a01644eacb8fbfa5266940039aa55c335d1856470576b12b2aa972ac',
 'exclusion_policy': '3b66cc9902576cb9eeeb0493fd322435b78475820aa063ca152c4c0c482fc9aa',
 'final_caps': 'ffb825c5ff819510c8e93217f351063649bd5210078dcaa2f833cad2b33b824b',
 'frozen_batch_repair': 'f406f9efe7801f2230ebde5e9a254eaf56b1edc0af494da5ee1b170cca5e8f70',
 'git_first_active_measurement': '0b5d623c54bc049d1951f222e70f4c0248fec802fee8fe7b85e050fae6104d93',
 'github_background_budget_measurement': '2c41686e844ebaab4613c2a980a59e09e7dd4a1a19075fbcdf59b4bba117516c',
 'graph_viewport_readiness_measurement': '1687dfd7e28840cdd99c17053761744904ab9cb72bd6250631e1192405897acb',
 'held_ingestion_p4_merge_measurement': 'c2605ced70807679ab41a4fdd789573f6aab812d8a3c995c8bf04d010742d6b0',
 'held_release_p3_measurement': 'cf229902b9aa5d68f922433e6542b77e87cc6280faf41937df65a38819955087',
 'human_ask_storage_review': '50c30c395da46555a7f1f0ca76f370515d0be0ba1ff93a8c59c2cf58a606879a',
 'inherited_epic_drift_review': 'a23bec74be181d61fd6eb032e6fa3e2a4e9c7f2fa4b8b68f36d97adf1a6fd5de',
 'known_lower_bound': 'ad764a12a56a4de81b1105009de12032f203a78a73d5592bb3c11e3c3b55c864',
 'landing_delta': '18122fed7e0ad87aaf5f3c9a1e9a0681c59afd90078ba681108f7b44e4bbe6b2',
 'landing_revision': '65f7a81288c642518dd2c5a9354a99eb348ad4890cab13e94f3c856f14849bd8',
 'legacy_bookkeeping_guard_measurement': '46c10f06145a768f8859d29f37bfe070ba997d58ba9009e83433398813354083',
 'maintenance': '0bc6a3fcbd5e687aa4e95a76646e8b62d0efd40b15de5a73583c094516aed566',
 'measurement': '162b5a24d1690b970712832503b88ad71f0b8934adfb5a4d201554ba9396fa36',
 'obsolete_child_measurement': 'a96f4687b08b70c2e6b0d51b021573361ca60b1292ce042f05e24a06b17a9021',
 'onboarding_repository_verifier_review': '177914761fb115b36569b83d20de8fba38c5f7191e0a832597e3a4bca99db81c',
 'ordinary_shared_storage': '14a290f868bcc06c5804cbf63f7b7a990e4647abda900e3a39b91b74bfa30bd0',
 'orphaned_train_writer_source_ci_repair': '09bfbad610015c88bf3e977f1448787f6e20e71aee3058a7d2ec5c987be0bfc0',
 'pinned_revision': '10843e718f7aa9996c12f257812a44bc1fa14fafb4e207f1d41c9763a0c86736',
 'playbook_activation_health_measurement': '43276f7b5d25161da6dc4ca74ef7ba515d99c2362e9dedf67da15c540df17d1d',
 'pool_preparation_ci_repair': '0af686b7ad61159628f0e8d19042ed6a1a0a49830988749b300bcdca701ba70e',
 'pool_workspace_preparation_source_ci_repair': '3d54ef4ed31367835e27ae2401b1f3ec1c4fe08898b33511c90e05c54d23e637',
 'post_release_structure_measurement': 'f25fb2a7305aa05d0f54ae5979d14f3a010aa335298b71954fa87e87b3145ce6',
 'remote_pr_base_measurement': 'a614e7fd3688a8bff0a3bfe8b0ceab95df7488b66b02cbc7401126ce85b97cd2',
 'repair_identity_measurement': 'c19f6875a9b0ffc664b655a75f676758dceb4f24095bcff956895ca518bf1875',
 'repository_binding_review': 'efeb6659b1dc94afbe63f542a4ec0bb33f6f1d7898a6c76fd9c368879620bf0e',
 'restart_hold_repair_measurement': '1d2e6db071298832adda3e6f912d94c74183252b6447dc9b191c83e6f1be17bc',
 'restart_hold_repair_merge_measurement': 'f4a51eefecdeeae9ea98e23321850d3bfcf4ddf30bd1f422015c052cffcb7389',
 'resume_recovery_measurement': '0482493e9f34af3024cf33289f986d85d4a3ba675fe47944981cacfc3e52478d',
 'review_revision_completion_ci_repair': 'd51d6ddd68fe5889305748f1c195f5e9b110d7402352aa7da31c931578e0592c',
 'root_pr_gate_cache_measurement': '1a9154baa559419724df63e6cb57526a541e595a3da4211beba9d3f12e443719',
 'scheduler_stack_inputs_measurement': 'a6b89705335f12a66b7cfbb8a3a201a2fac43ea1f6112a9063ba4c706f89948b',
 'shared_check_runs_measurement': '13b8ecbc014ceca3de35af3860e9b1f5cac9cdf50384e5fe076307a44b8bb17e',
 'storage_dispositions': '738351299ab085e9b6ea8237f835d95e238907d847c65f0ee06c4c7552e03016',
 'storage_writer_review': '3cab1c03146f90cd1b853b0ea8af2f05f5d9e8dd1dfb461711446b6cc4db8473',
 'supervisor_wait_recovery_measurement': '31b2160f02dbd97bc0d1c008bd858eeacfe36f8f17b03d4dcc88e85cdaa5fc21',
 'table_predicate': '5211f23811563336ed83ff6ac4f97c4349d045390bc8a3b648d8406360caf34a',
 'takeover_review': 'b3873aba32e9c3875fb20c0d8ac1e66a6d71b88cc4a7fb19e4076d1e0c502eb8',
 'task_create_retained_base_measurement': 'ff110e4cb76e34a07f0e0205b389903559c5317a7ceff5dd2447539b3e4aa163',
 'title': '9ab26f229bc8053fcb328b58f37e8847ab2e7d24c096f92a4b54d7cad717facd',
 'train_pair_conflict_measurement': '8451a607d21abe29323691ebb3e7f968e397706842a6cb9cc03699b24277e7ad',
 'updater_safety_measurement': '8afff9c0e0869cb0db7e977d5be51178e2241a3c70739cb21041a9b3c286071d',
 'verification': '236ebf27719c2ab719207276868db7f2bb7e5efe9634cef3b0e5cde5b9be00d1',
 'wait_graph_lock_measurement': '3f13e80d25092577f6aa74ee1797d38fec6219f670c9ef0a844a4f9cdba4b460',
 'wait_graph_lock_measurement_history': '036782d6754c1462b1e7ffe9de5c9da541a6df956d245938dffa099fb41d0b77',
 'worker_context_recovery_review': '2a10a11fd906313ed8398277e1eddd32f0d8a24f3e6dd1c9f04e8b66402d23f3',
 'workspace_kind_ci_repair_review': '7dba85b6b52615312cbc7772f0fa3ec16d26229ac439af48ae366d432491a23b',
 'workspace_ownership_measurement': '9586f006f144ee7d09514fad1aec5be7b77c520fd37476ae0774d8b5f0d024f3',
 'workspace_ownership_merge_measurement': 'a9547c60f4cf682bbea333dc7dd89de160aca986872599a328abcc27bda04346',
 'transition.pre_reconciliation_batch_exception': '9fbfd84fa6e67cdf6afe9f84692074b35a145f422f9f662a7fd710a8c51e1e92',
 'transition.retired_batch_exception': 'd46f9acbc2213d6a295a05cb8ca48d8268d6872ea95afa9ec8e7041f61163edc',
 'transition.batch_exception_retirement': 'da5d3268e059b492c7d8ab25d9fd7978c49d7d344146367d93c3c2f90c19e585'}


@pytest.mark.parametrize("section,expected", HISTORICAL_POLICY_DIGESTS.items())
def test_historical_grants_and_measurements_are_preserved(section, expected):
    recorded = json.loads((ROOT / "tests/integration_ownership_report.json").read_text())
    value = recorded
    for part in section.split("."):
        value = value[part]
    assert ownership.digest(json.dumps(value, sort_keys=True)) == expected


def test_historical_exception_union_is_preserved():
    assert ownership.digest(json.dumps(ownership.TRANSITION_BATCH_EXCEPTION, sort_keys=True)) == (
        "9fbfd84fa6e67cdf6afe9f84692074b35a145f422f9f662a7fd710a8c51e1e92"
    )


def test_hand_integration_reconciliation_is_separate_exact_and_has_no_headroom(report, manifest):
    grant = manifest["hand_integration_reconciliation"]
    assert grant == ownership.HAND_INTEGRATION_RECONCILIATION
    assert grant["authority"] == "size-cap-delegation-2026-10-23"
    assert grant["operator_decision"] == "6679052c"
    assert grant["original_owned_total_cap"] == 155_499
    assert grant["authorized_owned_delta"] == grant["measured_owned_total"] - 155_499
    assert grant["remaining_headroom"] == 0
    assert grant["expires_at"] == "s4_modules"
    assert report["total"] == ownership.transition_owned_cap(manifest)
    assert report["groups"]["engine"] == ownership.transition_engine_cap(manifest)
    assert len(report["tables"]) == grant["measured_table_count"] == 45
    assert ownership.budget_errors(report, manifest) == []
    assert len(ownership.budget_errors({**report, "total": report["total"] + 1}, manifest)) == 1
    assert len(ownership.budget_errors({**report, "groups": {
        **report["groups"], "engine": report["groups"]["engine"] + 1,
    }}, manifest)) == 1
    recorded = json.loads((ROOT / "tests/integration_ownership_report.json").read_text())
    assert recorded["hand_integration_reconciliation"] == grant
    assert recorded["transition"]["hand_integration_reconciliation"] == grant
    assert recorded["transition"]["engine_cap"] == report["groups"]["engine"]
    # The old cap remains effective without the new, narrowly scoped grant.
    copy = deepcopy(manifest)
    copy.pop("hand_integration_reconciliation")
    copy["baseline"]["total"] += 99_999
    assert ownership.transition_owned_cap(copy) == 155_499


@pytest.mark.parametrize("field", [
    "task_id", "authority", "operator_decision", "supervisor_decision_message_id",
    "source_revision", "original_owned_total_cap", "measured_owned_total",
    "authorized_owned_delta", "measured_engine_lines", "measured_table_count",
    "remaining_headroom", "expires_at", "unexpected_field",
])
def test_hand_integration_grant_cannot_be_reused_or_enlarged(manifest, field):
    copy = deepcopy(manifest)
    copy["hand_integration_reconciliation"][field] = "unapproved replacement"
    assert ownership.budget_errors({}, copy) == ["unapproved hand-integration reconciliation"]


@pytest.mark.parametrize("phase", ["transition", "s4_modules", "final"])
def test_cutover_evidence_uses_the_enforced_caps(manifest, report, monkeypatch, phase):
    copy = deepcopy(manifest)
    copy["phase"] = phase
    monkeypatch.setattr(ownership, "load_manifest", lambda: copy)
    monkeypatch.setattr(ownership, "inventory", lambda *_: report)
    monkeypatch.setitem(sys.modules, "integration_ownership", ownership)
    monkeypatch.setattr(sys, "path", list(sys.path))
    script = runpy.run_path(str(ROOT / "scripts/integration-cutover-evidence.py"))
    result = script["ownership"]()
    assert result["budget_errors"] == ownership.budget_errors(report, copy)
    transition = phase == "transition"
    assert result["owned_cap_in_phase"] == (
        ownership.transition_owned_cap(copy) if transition else 19_999)
    assert result["engine_cap_in_phase"] == (
        ownership.transition_engine_cap(copy) if transition else 11_999)
    assert result["table_cap_in_phase"] == (45 if transition else 10)
    assert result["transition_exception"] is None
    assert result["hand_integration_reconciliation"] == (
        copy["hand_integration_reconciliation"] if transition else None)
    ledger = json.loads((ROOT / "docs/gates/git-first-cutover/ledger.json").read_text())
    ledger["ownership"] = result
    rendered = script["render"](ledger)
    assert f"{result['owned_cap_in_phase']:,} {phase} cap" in rendered
    assert "one-time batch exception: none" in rendered
    if transition:
        assert copy["hand_integration_reconciliation"]["supervisor_decision_message_id"] in rendered
