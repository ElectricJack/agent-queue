"""Physical-line ownership and table integration ratchets.

During replacement the *fixed* recorded baseline permits 15,000 overlapping
lines, plus the explicitly recorded one-time batch exception. Set the manifest
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
    assert recorded["transition"]["batch_exception"] == manifest["transition_exception"]
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


def test_transition_budget_cannot_reset_to_current_counts(manifest):
    copy = deepcopy(manifest)
    copy["phase"] = "transition"
    copy.pop("transition_exception", None)
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


def test_batch_exception_is_exact_and_has_no_remaining_headroom(report, manifest):
    exception = manifest["transition_exception"]
    assert exception == ownership.TRANSITION_BATCH_EXCEPTION
    assert exception["original_owned_total_cap"] == 155_499
    measured = exception["measured_owned_total"]
    assert measured == report["total"]
    assert exception["measured_engine_lines"] == report["groups"]["engine"]
    assert exception["measured_table_count"] == len(report["tables"])
    assert exception["storage_categories"] == ["integration_close_attempts"]
    assert exception["delta"] == measured - 155_499
    assert [p.get("delta", p.get("authorized_delta")) for p in exception["predecessors"]] == [
        1_293, 2_166,
    ]
    assert exception["batch_measured_delta"] == measured - 158_958
    grant, = exception["supplemental_grants"]
    assert grant["task_id"] == "fresh-apex-30"
    assert grant["approval_message_id"] == "msg-4e25c0d3"
    assert grant["authorized_delta"] == 5
    assert grant["prior_owned_total_cap"] + grant["authorized_delta"] == 160_825
    assert grant["measured_owned_total"] == 160_825
    amendments = {item["task_id"]: item for item in exception["amendments"]}
    assert set(amendments) == {
        "solid-impact-56", "agile-beacon-80", "nimble-torrent-12", "prime-impact-40",
        "quick-pinnacle-97",
        "stark-beacon-94",
        "sharp-rapids-10",
        "fair-dune-41",
        "noble-flare-12",
        "sound-forge-62",
        "calm-rapids-50",
        "fair-harbor-71",
    }
    amendment = amendments["solid-impact-56"]
    assert amendment["task_id"] == "solid-impact-56"
    assert amendment["authorized_owned_delta"] == 17
    assert grant["measured_owned_total"] + amendment["authorized_owned_delta"] == 160_842
    merged = exception["merge_measurement"]
    assert merged["task_id"] == "fresh-apex-30"
    assert merged["approval_message_id"] == "msg-a10c7ab8ff2946f8b1f27528a407524c"
    assert merged["decision_id"] == "size-cap-delegation-2026-10-23"
    assert merged["authorized_owned_delta"] == grant["authorized_delta"] == 5
    assert merged["dev_owned_total"] + merged["authorized_owned_delta"] == 160_884
    assert (merged["dev_engine_lines"] + merged["authorized_engine_delta"]
            == 85_321)
    composition = exception["batch_composition"]
    assert composition["source_revision"] == "4e98fa0b96d561cfa7de934541c3e66df430b2a6"
    # sharp-rapids-10's exact +9/+0 is composed on top of dev's frozen batch record.
    snapshot = amendments["sharp-rapids-10"]
    assert snapshot["decision_message_id"] == "msg-3f6311071a6647429059f3a990750597"
    assert snapshot["decision_message_id"] in exception["decision_message_ids"]
    assert snapshot["referenced_decision"] == "size-cap-delegation-2026-10-23"
    assert snapshot["operator_decision"] == "6679052c"
    assert snapshot["grant_id"] == "sharp-rapids-10-cap-epoch2-2026-10-23"
    assert snapshot["pull_request"] == "https://github.com/ElectricJack/agent-queue/pull/1885"
    assert snapshot["previous_source_revision"] == "61869b7cdcf507b8e8acb596893bb63ac0794997"
    assert snapshot["previous_owned_total"] == 161_102
    assert snapshot["authorized_owned_delta"] == 9
    assert snapshot["authorized_engine_delta"] == 0
    assert snapshot["previous_owned_total"] + snapshot["authorized_owned_delta"] == 161_111
    assert snapshot["measured_owned_total"] == 161_111
    assert snapshot["previous_engine_lines"] == snapshot["measured_engine_lines"] == 85_517
    assert snapshot["measured_table_count"] == 45
    assert snapshot["measured_source_revision"] == "43b3e220a2a1ac6e8d5b7543d3f83063c7c747f6"
    recorded = json.loads((ROOT / "tests/integration_ownership_report.json").read_text())
    assert recorded["dashboard_pr_snapshot_measurement"] == snapshot
    resume = amendments["calm-rapids-50"]
    assert resume["decision_message_id"] == "msg-5c55b72faec1497391205876fc5dfeb9"
    assert resume["decision_message_id"] in exception["decision_message_ids"]
    assert resume["referenced_decision"] == "size-cap-delegation-2026-10-23"
    assert resume["operator_decision"] == "6679052c"
    assert resume["pull_request"] == 1814
    assert resume["merged_dev_revision"] == "4a0c4e38ab979a1664586d47d16404b48def4089"
    assert resume["previous_owned_total"] == snapshot["measured_owned_total"]
    assert resume["authorized_owned_delta"] == 50
    assert resume["previous_owned_total"] + resume["authorized_owned_delta"] == 161_161
    assert resume["authorized_engine_delta"] == 0
    assert resume["previous_engine_lines"] == snapshot["measured_engine_lines"]
    check_runs = amendments["fair-harbor-71"]
    assert check_runs["decision_message_id"] == "msg-96fed6b8e86148f0bbf55ded244c9a0f"
    assert check_runs["decision_message_id"] in exception["decision_message_ids"]
    assert check_runs["referenced_decision"] == "size-cap-delegation-2026-10-23"
    assert check_runs["operator_decision"] == "6679052c"
    assert check_runs["operator_task"] == "sharp-cascade-53"
    assert check_runs["grant_delivery_message_id"] == "msg-3a0b0c6448ef44699f7a7d7c43777642"
    assert check_runs["previous_source_revision"] == "4a0c4e38ab979a1664586d47d16404b48def4089"
    assert check_runs["previous_owned_total"] == snapshot["measured_owned_total"]
    assert check_runs["previous_engine_lines"] == snapshot["measured_engine_lines"]
    assert check_runs["authorized_owned_delta"] == check_runs["authorized_engine_delta"] == 1
    assert check_runs["authorized_table_delta"] == 0
    assert check_runs["previous_owned_total"] + check_runs["authorized_owned_delta"] == 161_112
    assert check_runs["measured_owned_total"] == 161_112 == 161_112
    assert (check_runs["previous_engine_lines"] + check_runs["authorized_engine_delta"]
            == check_runs["measured_engine_lines"] == 85_518)
    assert check_runs["measured_table_count"] == 45
    assert check_runs["measured_source_revision"] == "14828bb29104d825e9ba5a75aa44a3799dd48a48"
    assert check_runs["pull_request"] == "https://github.com/ElectricJack/agent-queue/pull/1890"
    assert recorded["shared_check_runs_measurement"] == check_runs
    viewport = amendments["noble-flare-12"]
    assert viewport["decision_message_id"] == "msg-68ccb1d50ee04851a7268dd58e9d4459"
    assert viewport["decision_message_id"] in exception["decision_message_ids"]
    assert viewport["referenced_decision"] == "size-cap-delegation-2026-10-23"
    assert viewport["operator_decision"] == "6679052c"
    assert viewport["operator_task"] == "sharp-cascade-53"
    assert viewport["pull_request"] == "https://github.com/ElectricJack/agent-queue/pull/1892"
    assert viewport["previous_source_revision"] == "4a0c4e38ab979a1664586d47d16404b48def4089"
    assert viewport["previous_owned_total"] == snapshot["measured_owned_total"]
    assert viewport["previous_engine_lines"] == snapshot["measured_engine_lines"]
    assert viewport["authorized_owned_delta"] == 23
    assert viewport["authorized_engine_delta"] == 22
    assert viewport["authorized_shared_delta"] == 1
    assert (viewport["previous_owned_total"] + viewport["authorized_owned_delta"]
            == viewport["measured_owned_total"])
    assert viewport["measured_owned_total"] == 161_134
    assert (viewport["previous_engine_lines"] + viewport["authorized_engine_delta"]
            == viewport["measured_engine_lines"] == 85_539)
    assert viewport["measured_table_count"] == exception["measured_table_count"] == 45
    assert viewport["measured_source_revision"] == "b194122701a0130039230dea4c8caa93c70f60e1"
    assert recorded["graph_viewport_readiness_measurement"] == viewport
    filing = amendments["sound-forge-62"]
    assert filing["decision_message_id"] == "msg-9f16388d39524c8b9a33d6796b88d1fb"
    assert filing["decision_message_id"] in exception["decision_message_ids"]
    assert filing["referenced_decision"] == "size-cap-delegation-2026-10-23"
    assert filing["operator_decision"] == "6679052c"
    assert filing["operator_task"] == "sharp-cascade-53"
    assert filing["pull_request"] == "https://github.com/ElectricJack/agent-queue/pull/1891"
    assert filing["previous_owned_total"] == snapshot["measured_owned_total"]
    assert filing["previous_engine_lines"] == snapshot["measured_engine_lines"]
    assert filing["authorized_owned_delta"] == 26
    assert filing["authorized_engine_delta"] == 4
    assert filing["authorized_shared_delta"] == 22
    assert filing["authorized_table_delta"] == 0
    assert filing["previous_owned_total"] + 26 == filing["measured_owned_total"] == 161_137
    assert filing["previous_engine_lines"] + 4 == filing["measured_engine_lines"] == 85_521
    assert filing["measured_source_revision"] == "1f0633050802438db6ef954c43d6d0eb2ce0edf3"
    assert filing["measured_table_count"] == filing["previous_table_count"] == 45
    assert recorded["task_create_retained_base_measurement"] == filing
    post = exception["post_batch_composition"]
    assert post["base_revision"] == filing["previous_source_revision"]
    assert post["source_revision"] == filing["measured_source_revision"]
    assert post["approved_deltas"] == {
        "sound-forge-62": {"owned": 26, "engine": 4, "shared": 22, "tables": 0},
    }
    assert post["base_owned_total"] + 26 == filing["measured_owned_total"]
    assert post["base_engine_lines"] + 4 == filing["measured_engine_lines"]
    assert set(composition["member_sources"]) == {
        "agile-beacon-80", "grand-quest-20", "nimble-torrent-12", "prime-impact-40",
        "quick-pinnacle-97",
        "stark-beacon-94",
    }
    assert composition["base_revision"] == merged["dev_source_revision"]
    assert composition["base_owned_total"] == merged["dev_owned_total"]
    assert composition["base_engine_lines"] == merged["dev_engine_lines"]
    assert composition["approved_deltas"] == {
        "fresh-apex-30": {"owned": 5, "engine": 5},
        "agile-beacon-80": {"owned": 65, "engine": 65},
        "nimble-torrent-12": {"owned": 1, "engine": 0},
        "prime-impact-40": {"owned": 103, "engine": 82},
        "quick-pinnacle-97": {"owned": 28, "engine": 28},
        "stark-beacon-94": {"owned": 21, "engine": 21},
    }
    # Keep historical member measurements separate from the combined exact grant.
    orphan_launch = amendments["quick-pinnacle-97"]
    assert orphan_launch["previous_owned_total"] == 161_053
    assert orphan_launch["measured_owned_total"] == 161_081
    assert orphan_launch["measured_engine_lines"] == 85_496
    assert orphan_launch["measured_table_count"] == 45
    assert orphan_launch["referenced_decision"] == "size-cap-delegation-2026-10-23"
    assert orphan_launch["decision_message_id"] == "msg-e2dd5a423516458eb564e745a12ab30e"
    assert orphan_launch["pull_request"] == "https://github.com/ElectricJack/agent-queue/pull/1884"
    for task_id in ("agile-beacon-80", "nimble-torrent-12", "prime-impact-40", "quick-pinnacle-97"):
        grant_delta = composition["approved_deltas"][task_id]
        assert grant_delta["owned"] == amendments[task_id]["authorized_owned_delta"]
        assert grant_delta["engine"] == amendments[task_id]["authorized_engine_delta"]
        assert amendments[task_id]["decision_message_id"] in exception["decision_message_ids"]
    assert composition["base_owned_total"] + sum(
        delta["owned"] for delta in composition["approved_deltas"].values()
    ) == snapshot["previous_owned_total"]
    assert composition["base_engine_lines"] + sum(
        delta["engine"] for delta in composition["approved_deltas"].values()
    ) == snapshot["measured_engine_lines"]
    repair = amendments["stark-beacon-94"]
    assert repair["decision_id"] == "size-cap-delegation-2026-10-23"
    assert repair["operator_decision"] == "6679052c"
    assert repair["operator_task"] == "sharp-cascade-53"
    assert repair["pr_url"] == "https://github.com/ElectricJack/agent-queue/pull/1817"
    assert repair["previous_owned_total"] + repair["authorized_owned_delta"] == 161_074
    assert (repair["previous_engine_lines"] + repair["authorized_engine_delta"]
            == 85_489)
    assert repair["previous_table_count"] + repair["authorized_table_delta"] == 45
    assert repair["measured_source_revision"] == "24d081eecf0c00a912a064e37ba616334382e263"
    snapshot_merge = exception["dashboard_pr_snapshot_merge_measurement"]
    assert snapshot_merge["referenced_decision"] == "size-cap-delegation-2026-10-23"
    assert snapshot_merge["grant_id"] == snapshot["grant_id"]
    assert snapshot_merge["merged_dev_revision"] == "61869b7cdcf507b8e8acb596893bb63ac0794997"
    assert snapshot_merge["dev_owned_total"] == 161_102
    assert snapshot_merge["prerequisite_owned_delta"] == snapshot["authorized_owned_delta"] == 9
    assert snapshot_merge["authorized_owned_delta"] == 0
    assert (snapshot_merge["dev_owned_total"] + snapshot_merge["prerequisite_owned_delta"]
            + snapshot_merge["authorized_owned_delta"]) == snapshot_merge["measured_owned_total"] == 161_111
    assert snapshot_merge["dev_engine_lines"] == snapshot_merge["measured_engine_lines"] == 85_517
    assert snapshot_merge["prerequisite_engine_delta"] == snapshot_merge["authorized_engine_delta"] == 0
    assert snapshot_merge["dev_table_count"] == snapshot_merge["measured_table_count"] == 45
    assert snapshot_merge["measured_owned_total"] == 161_111
    assert recorded["dashboard_pr_snapshot_merge_measurement"] == snapshot_merge
    assert ownership.transition_owned_cap(manifest) == measured
    too_large = {**report, "total": measured + 1}
    assert ownership.budget_errors(too_large, manifest) == [
        f"owned total: {measured + 1:,} exceeds {measured:,} in transition",
    ]
    # Changing the historical snapshot still cannot enlarge the exception.
    copy = deepcopy(manifest)
    copy["baseline"]["total"] += 99_999
    assert ownership.budget_errors(too_large, copy) == ownership.budget_errors(too_large, manifest)
    copy.pop("transition_exception")
    assert ownership.transition_owned_cap(copy) == 155_499
    assert ownership.budget_errors({**report, "total": measured}, copy) == [
        f"owned total: {measured:,} exceeds 155,499 in transition",
    ]
    # The exception is an upper bound: later genuine reductions still pass.
    assert ownership.budget_errors({**report, "total": measured - 1}, manifest) == []


def test_activation_health_grant_carries_only_two_owned_lines(report, manifest):
    exception = manifest["transition_exception"]
    grant = next(item for item in exception["amendments"] if item["task_id"] == "fair-dune-41")
    assert grant["decision_message_id"] == "msg-99539879"
    assert grant["decision_message_id"] in exception["decision_message_ids"]
    assert grant["referenced_decision"] == "size-cap-delegation-2026-10-23"
    assert grant["charged_definition"] == "src/doctor/stall_checks.py::_check_sweep"
    assert grant["previous_source_revision"] == "4a0c4e38ab979a1664586d47d16404b48def4089"
    assert grant["original_approved_owned_total"] == 161_053
    assert grant["original_measured_owned_total"] == 161_055
    assert grant["authorized_owned_delta"] == 2
    assert grant["authorized_engine_delta"] == grant["authorized_table_delta"] == 0
    assert grant["previous_owned_total"] == 161_111
    assert (grant["previous_owned_total"] + grant["authorized_owned_delta"]
            == grant["measured_owned_total"])
    assert grant["measured_owned_total"] == 161_113
    assert grant["previous_engine_lines"] == grant["measured_engine_lines"] == 85_517
    assert grant["previous_table_count"] == grant["measured_table_count"] == len(report["tables"])
    assert grant["measured_source_revision"] == "ca7a4377c532d723b0ab9c3bb5f833707a317cf5"
    recorded = json.loads((ROOT / "tests/integration_ownership_report.json").read_text())
    assert recorded["playbook_activation_health_measurement"] == grant
    assert ownership.budget_errors(report, manifest) == []
    assert ownership.budget_errors({**report, "total": report["total"] + 1}, manifest) == [
        f"owned total: {report['total'] + 1:,} exceeds {report['total']:,} in transition",
    ]


def test_frozen_batch_composes_only_the_approved_member_deltas(report, manifest):
    exception = manifest["transition_exception"]
    batch = exception["frozen_batch_composition"]
    assert batch["batch_id"] == "train-5429c3c71859f327ff8b46dbf1edc557"
    assert batch["starting_revision"] == "48adaddad079a7adc2930e470590621c1575e674"
    assert batch["source_revision"] == exception["measured_source_revision"]
    assert batch["member_sources"] == {
        "noble-flare-12": "f09829d145915bc7426d24f28623283f7319a7a2",
        "sound-forge-62": "15239e75971ae5ac39e6a7d0a5e467331ab58428",
    }
    assert batch["approved_deltas"] == {
        "noble-flare-12": {"owned": 23, "engine": 22, "shared": 1, "tables": 0},
        "sound-forge-62": {"owned": 26, "engine": 4, "shared": 22, "tables": 0},
    }
    amendments = {item["task_id"]: item for item in exception["amendments"]}
    for task_id, delta in batch["approved_deltas"].items():
        grant = amendments[task_id]
        for category in ("owned", "engine", "shared"):
            assert delta[category] == grant[f"authorized_{category}_delta"]
        assert delta["tables"] == grant.get("authorized_table_delta", 0)
    activation = amendments["fair-dune-41"]
    assert batch["base_owned_total"] == activation["measured_owned_total"] == 161_113
    assert batch["base_engine_lines"] == activation["measured_engine_lines"] == 85_517
    assert batch["base_table_count"] == activation["measured_table_count"] == 45
    assert batch["base_owned_total"] + sum(
        delta["owned"] for delta in batch["approved_deltas"].values()
    ) == exception["measured_owned_total"] == report["total"] == 161_162
    assert batch["base_engine_lines"] + sum(
        delta["engine"] for delta in batch["approved_deltas"].values()
    ) == exception["measured_engine_lines"] == report["groups"]["engine"] == 85_543
    assert batch["base_table_count"] + sum(
        delta["tables"] for delta in batch["approved_deltas"].values()
    ) == exception["measured_table_count"] == len(report["tables"]) == 45


@pytest.mark.parametrize("field,value", [
    ("batch_id", "another-batch"),
    ("decision_message_id", "another-decision"),
    ("measured_source_revision", "another-source"),
    ("delta", 1_294),
    ("measured_owned_total", 156_793),
    ("original_owned_total_cap", 155_500),
])
def test_batch_exception_cannot_be_reused_or_enlarged(manifest, field, value):
    copy = deepcopy(manifest)
    copy["transition_exception"][field] = value
    assert ownership.budget_errors({}, copy) == ["unapproved integration transition exception"]


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
    assert result["engine_cap_in_phase"] == (97_627 if transition else 11_999)
    assert result["table_cap_in_phase"] == (45 if transition else 10)
    assert result["transition_exception"] == (copy["transition_exception"] if transition else None)
    ledger = json.loads((ROOT / "docs/gates/git-first-cutover/ledger.json").read_text())
    ledger["ownership"] = result
    rendered = script["render"](ledger)
    if transition:
        assert f"{ownership.transition_owned_cap(copy):,} transition cap" in rendered
        assert copy["transition_exception"]["decision_message_id"] in rendered
    else:
        assert f"19,999 {phase} cap" in rendered
        assert "one-time batch exception: none" in rendered


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
