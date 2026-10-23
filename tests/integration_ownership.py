"""Source-only integration inventory. No daemon, database, git history or Node needed.

The manifest is an ownership decision, never an automatically raised ceiling.
Python definitions use AST spans; shared schema/container entries are separate
units. TypeScript units are complete top-level declarations (including multiline
imports and component bodies). All measurements count newline bytes, like wc -l.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re


PINNED_REVISION = "2483f634aef2ece9fd3a6e5d92174cc145aa564a"
LANDING_REVISION = "8e73e088fa5050621a165b9d13f90ef88334572d"
BASELINE_ENGINE_LINES = 82_627
BASELINE_OWNED_LINES = 140_499
REPLACEMENT_ALLOWANCE = 15_000
# Exact standing batch exception, not a new replacement allowance. Remove its
# manifest opt-in when azure-ridge-72 restores the original transition cap.
TRANSITION_BATCH_EXCEPTION = {'batch_id': 'train-745592300af5cef0338dec80ebabb465',
 'decision_message_id': 'msg-7ed85e78d1af4f3e85b485a57967e794',
 'decision_message_ids': ['msg-7ed85e78d1af4f3e85b485a57967e794',
                          'msg-381054d695e64055979a687c4d811e7e',
                          'msg-ab86b9766c2f42b9ba4920aba585b577',
                          'msg-21784b3e56aa4f54bf1042b7c9af4aaf',
                          'msg-1984d09ec70e479c9d0cc14c57e828c3',
                          'msg-00b0e08117cc479c9a4a0b36fc5a30d0',
                          'msg-3bed5d5db4ed4c7fa1ef9150b1c3dd8c',
                          'msg-a337e2c905ce4f5c8a236344a4e25211',
                          'msg-330ea2478c724381acbb5512fc99e4b5',
                          'msg-e2dd5a423516458eb564e745a12ab30e',
                          'msg-3f6311071a6647429059f3a990750597',
                          'msg-99539879',
                          'msg-68ccb1d50ee04851a7268dd58e9d4459',
                          'msg-9f16388d39524c8b9a33d6796b88d1fb'],
 'decision_record': 'record:2720bdbe-7521-427e-8e72-1f40a3ff9818',
 'measured_source_revision': '73e99672d2447c44ef6a71d43a0187f2d16630af',
 'original_owned_total_cap': 155499,
 'measured_owned_total': 161162,
 'delta': 5663,
 'predecessors': [{'batch_id': 'train-77f0d8e451f6aed08d608e443031047a',
                   'decision_message_id': 'msg-edb5f857de344716a38857406fa69958',
                   'decision_record': 'record:2720bdbe-7521-427e-8e72-1f40a3ff9818',
                   'measured_source_revision': 'd00e25221856145f24777f8b016a008b4e954bca',
                   'original_owned_total_cap': 155499,
                   'measured_owned_total': 156792,
                   'delta': 1293,
                   'restore_task': 'azure-ridge-72',
                   'expires_at': 's4_modules'},
                  {'batch_id': 'train-48d0a1c1f1f8292b2977546011609db6',
                   'measured_source_revision': 'cbfd455b0e9f053854a2c1becfa1efaeef231582',
                   'authorized_delta': 2166,
                   'measured_owned_total': 158958}],
 'batch_measured_delta': 2204,
 'restore_task': 'azure-ridge-72',
 'expires_at': 's4_modules',
 'category_approval_message_id': 'msg-90f9f1b1e0cd4e8e8d28071cffc927a6',
 'storage_categories': ['integration_close_attempts'],
 'category_restore_task': 'stark-willow-95.17',
 'verification_task': 'stark-willow-95.18',
 'measured_engine_lines': 85543,
 'measured_table_count': 45,
 'delivery_method': 'operator direct integration; user request 2026-10-22',
 'amendments': [{'task_id': 'solid-impact-56',
                 'decision_message_id': 'msg-ab86b9766c2f42b9ba4920aba585b577',
                 'referenced_decision': 'msg-9f895e0d',
                 'previous_owned_total': 160820,
                 'previous_engine_lines': 85301,
                 'authorized_owned_delta': 17,
                 'authorized_engine_delta': 15,
                 'reason': 'Memoize stack authorization inputs within one scheduler sync pass; '
                           'supervisor approved exact net footprint after removing an unused '
                           'parent query.',
                 'global_request': 'msg-9f895e0d',
                 'global_grant': 'msg-7f2f68c1',
                 'grant_delivery_message_id': 'msg-21784b3e56aa4f54bf1042b7c9af4aaf'},
                {'task_id': 'agile-beacon-80',
                 'decision_message_id': 'msg-1984d09ec70e479c9d0cc14c57e828c3',
                 'referenced_decision': 'msg-00b0e081',
                 'delegation_thread': 'size-cap-delegation-2026-10-23',
                 'previous_owned_total': 160837,
                 'previous_engine_lines': 85316,
                 'authorized_owned_delta': 65,
                 'authorized_engine_delta': 65,
                 'reason': 'Bounded retry of provably unlaunched batch fixers and recovery of '
                           'expired legacy family owners; global supervisor approved the exact '
                           '+65-line exception under the project supervisor delegation.',
                 'grant_delivery_message_id': 'msg-1984d09ec70e479c9d0cc14c57e828c3',
                 'project_grant_message_id': 'msg-00b0e08117cc479c9a4a0b36fc5a30d0',
                 'operator_decision': '6679052c',
                 'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/1820'},
                {'task_id': 'nimble-torrent-12',
                 'decision_message_id': 'msg-3bed5d5db4ed4c7fa1ef9150b1c3dd8c',
                 'referenced_decision': 'size-cap-delegation-2026-10-23',
                 'previous_owned_total': 160879,
                 'previous_shared_lines': 62006,
                 'authorized_owned_delta': 1,
                 'authorized_shared_delta': 1,
                 'authorized_engine_delta': 0,
                 'reason': 'Accept repository mappings in the shared GitHub binding resolver for '
                           'verified CI waits; supervisor approved the exact one-line shared '
                           'footprint.'},
                {'task_id': 'prime-impact-40',
                 'decision_message_id': 'msg-a337e2c905ce4f5c8a236344a4e25211',
                 'referenced_decision': 'size-cap-delegation-2026-10-23',
                 'previous_owned_total': 160879,
                 'previous_engine_lines': 85316,
                 'authorized_owned_delta': 103,
                 'authorized_engine_delta': 82,
                 'reason': 'GitHub caller diagnostics, shared background quota reserve and '
                           'inventory reuse; exact supervisor grant under '
                           'size-cap-delegation-2026-10-23.',
                 'supersedes_message_id': 'msg-ac47a08407a6462bb411326ecefeab44',
                 'grant_source_message_id': 'msg-330ea2478c724381acbb5512fc99e4b5'},
                {'task_id': 'quick-pinnacle-97',
                 'decision_message_id': 'msg-e2dd5a423516458eb564e745a12ab30e',
                 'referenced_decision': 'size-cap-delegation-2026-10-23',
                 'operator_decision': '6679052c',
                 'previous_source_revision': 'dd95038cca3fef9aae70eb0092079fe24d52a062',
                 'previous_owned_total': 161053,
                 'previous_engine_lines': 85468,
                 'authorized_owned_delta': 28,
                 'authorized_engine_delta': 28,
                 'measured_owned_total': 161081,
                 'measured_engine_lines': 85496,
                 'measured_table_count': 45,
                 'measured_source_revision': '344a85b9d8388521975af9b830e4d8b17817562e',
                 'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/1884',
                 'reason': 'Recover orphaned launching fixer reservations after a per-attempt '
                           'grace period; fence late registration and completion by session, '
                           'instance and launch timestamp. Exact self-approved measurement under '
                           'the project supervisor delegation.'},
                {'task_id': 'stark-beacon-94',
                 'decision_id': 'size-cap-delegation-2026-10-23',
                 'operator_decision': '6679052c',
                 'operator_task': 'sharp-cascade-53',
                 'pr_url': 'https://github.com/ElectricJack/agent-queue/pull/1817',
                 'measured_source_revision': '24d081eecf0c00a912a064e37ba616334382e263',
                 'previous_owned_total': 161053,
                 'previous_engine_lines': 85468,
                 'previous_table_count': 45,
                 'authorized_owned_delta': 21,
                 'authorized_engine_delta': 21,
                 'authorized_table_delta': 0,
                 'reason': 'Exclude published base history from repair identity checks; supervisor '
                           'granted the exact measured footprint for PR #1817 under the delegated '
                           'operator decision. Plain merge retains the existing implementation and '
                           'all dev amendments.',
                 'base_revision': 'dd95038cca3fef9aae70eb0092079fe24d52a062',
                 'previous_source_revision': '438aa36cc93945305ea291622009f6b34d4ee733'},
                {'task_id': 'sharp-rapids-10',
                 'decision_message_id': 'msg-3f6311071a6647429059f3a990750597',
                 'referenced_decision': 'size-cap-delegation-2026-10-23',
                 'operator_decision': '6679052c',
                 'grant_id': 'sharp-rapids-10-cap-epoch2-2026-10-23',
                 'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/1885',
                 'previous_source_revision': '61869b7cdcf507b8e8acb596893bb63ac0794997',
                 'previous_owned_total': 161102,
                 'previous_engine_lines': 85517,
                 'authorized_owned_delta': 9,
                 'authorized_engine_delta': 0,
                 'measured_source_revision': '43b3e220a2a1ac6e8d5b7543d3f83063c7c747f6',
                 'measured_owned_total': 161111,
                 'measured_engine_lines': 85517,
                 'measured_table_count': 45,
                 'reason': 'Dashboard PR snapshot quota fix; exact project supervisor amendment '
                           'under size-cap-delegation-2026-10-23. No automatic ceiling increase or '
                           'source relocation.'},
                {'task_id': 'fair-dune-41',
                 'decision_message_id': 'msg-99539879',
                 'referenced_decision': 'size-cap-delegation-2026-10-23',
                 'previous_source_revision': '4a0c4e38ab979a1664586d47d16404b48def4089',
                 'previous_owned_total': 161111,
                 'previous_engine_lines': 85517,
                 'previous_table_count': 45,
                 'authorized_owned_delta': 2,
                 'authorized_engine_delta': 0,
                 'authorized_table_delta': 0,
                 'original_approved_owned_total': 161053,
                 'original_measured_owned_total': 161055,
                 'measured_source_revision': 'ca7a4377c532d723b0ab9c3bb5f833707a317cf5',
                 'measured_owned_total': 161113,
                 'measured_engine_lines': 85517,
                 'measured_table_count': 45,
                 'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/1886',
                 'charged_definition': 'src/doctor/stall_checks.py::_check_sweep',
                 'reason': 'Stall-sweep alert for non-ready playbook activations. Global '
                           'supervisor approved the exact +2 owned lines under '
                           'size-cap-delegation-2026-10-23; project supervisor directed carrying '
                           'that unchanged delta onto current dev after PR1885. Engine lines and '
                           'table count remain unchanged.'},
                {'task_id': 'noble-flare-12',
                 'decision_message_id': 'msg-68ccb1d50ee04851a7268dd58e9d4459',
                 'operator_decision': '6679052c',
                 'operator_task': 'sharp-cascade-53',
                 'referenced_decision': 'size-cap-delegation-2026-10-23',
                 'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/1892',
                 'previous_source_revision': '4a0c4e38ab979a1664586d47d16404b48def4089',
                 'previous_owned_total': 161111,
                 'previous_engine_lines': 85517,
                 'authorized_owned_delta': 23,
                 'authorized_engine_delta': 22,
                 'authorized_shared_delta': 1,
                 'measured_source_revision': 'b194122701a0130039230dea4c8caa93c70f60e1',
                 'measured_owned_total': 161134,
                 'measured_engine_lines': 85539,
                 'measured_table_count': 45,
                 'reason': 'Bound cached tile readiness to visible candidates and stack proof '
                           'sources; exact project supervisor approval under '
                           'size-cap-delegation-2026-10-23. Preserve scheduler shared inputs and '
                           'fail-closed authorization. No new storage or automatic cap increase.'},
                {'task_id': 'sound-forge-62',
                 'decision_message_id': 'msg-9f16388d39524c8b9a33d6796b88d1fb',
                 'referenced_decision': 'size-cap-delegation-2026-10-23',
                 'operator_decision': '6679052c',
                 'operator_task': 'sharp-cascade-53',
                 'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/1891',
                 'previous_source_revision': '4a0c4e38ab979a1664586d47d16404b48def4089',
                 'previous_owned_total': 161111,
                 'previous_engine_lines': 85517,
                 'previous_table_count': 45,
                 'authorized_owned_delta': 26,
                 'authorized_engine_delta': 4,
                 'authorized_shared_delta': 22,
                 'authorized_table_delta': 0,
                 'measured_source_revision': '1f0633050802438db6ef954c43d6d0eb2ce0edf3',
                 'measured_owned_total': 161137,
                 'measured_engine_lines': 85521,
                 'measured_table_count': 45,
                 'original_task_source_revision': '8e4e71b34b5c03b191b392a9d8f28176d177dd82',
                 'original_task_owned_total': 161079,
                 'original_task_engine_lines': 85472,
                 'original_task_base_owned_total': 161053,
                 'original_task_base_engine_lines': 85468,
                 'reason': 'Explicit supervisor approval of the exact retained Git filing resolver '
                           'footprint; compose the unchanged +26 owned/+4 engine grant on dev, '
                           'preserving historical grants.'}],
 'supplemental_grants': [{'task_id': 'fresh-apex-30',
                          'approval_message_id': 'msg-4e25c0d3',
                          'relay_message_id': 'msg-bd98955a2fa443cc920ce725e42a5c3f',
                          'measured_source_revision': 'd9c50555a1d87beaefbfc8de3b13229a6ab9f061',
                          'prior_owned_total_cap': 160820,
                          'authorized_delta': 5,
                          'measured_owned_total': 160825,
                          'reason': 'Verify registered linked fixer worktrees before permission '
                                    'bypass.'}],
 'merge_measurement': {'task_id': 'fresh-apex-30',
                       'dev_source_revision': '4e50c276fca68cbe92ba4fc704363ec6026db7dc',
                       'dev_owned_total': 160879,
                       'dev_engine_lines': 85316,
                       'authorized_owned_delta': 5,
                       'authorized_engine_delta': 5,
                       'approval_message_id': 'msg-a10c7ab8ff2946f8b1f27528a407524c',
                       'reason': 'Carry forward the unchanged approved fixer delta onto the '
                                 'measured dev tree.',
                       'decision_id': 'size-cap-delegation-2026-10-23'},
 'merge_remeasurement': {'task_id': 'agile-beacon-80',
                         'authority': 'msg-00b0e08117cc479c9a4a0b36fc5a30d0',
                         'operator_decision': '6679052c',
                         'delegation_thread': 'size-cap-delegation-2026-10-23',
                         'merged_dev_revision': '4e50c276fca68cbe92ba4fc704363ec6026db7dc',
                         'merged_dev_owned_total': 160879,
                         'merged_dev_engine_lines': 85316,
                         'merged_dev_table_count': 45,
                         'previous_source_revision': '974e10dcf94ad95f98c14fbc3d7171072e644da2',
                         'previous_measured_owned_total': 160944,
                         'measured_source_revision': 'efd690243cecfafedfeaf81c829d86255cabd620',
                         'measured_owned_total': 160944,
                         'measured_engine_lines': 85381,
                         'measured_table_count': 45,
                         'authorized_owned_delta': 65,
                         'authorized_engine_delta': 65,
                         'reason': 'Plain merge of PR1877 preparation diagnostics from dev; '
                                   'combined totals remain unchanged and this task retains its '
                                   'approved 65-line delta. Project grant requires re-recording '
                                   'against the combined total when dev advances.'},
 'batch_composition': {'batch_id': 'train-854bd30527fc85b1edc0e2c7047bd673',
                       'starting_revision': 'bd8fcfe183d6a59ea90f4498cda2bee80a7222f4',
                       'base_revision': '4e50c276fca68cbe92ba4fc704363ec6026db7dc',
                       'base_owned_total': 160879,
                       'base_engine_lines': 85316,
                       'approved_deltas': {'fresh-apex-30': {'owned': 5, 'engine': 5},
                                           'agile-beacon-80': {'owned': 65, 'engine': 65},
                                           'nimble-torrent-12': {'owned': 1, 'engine': 0},
                                           'prime-impact-40': {'owned': 103, 'engine': 82},
                                           'quick-pinnacle-97': {'owned': 28, 'engine': 28},
                                           'stark-beacon-94': {'owned': 21, 'engine': 21}},
                       'reason': 'Compose the existing exact grants on their common measured dev '
                                 'tree; preserve each historical measurement below.',
                       'source_revision': '4e98fa0b96d561cfa7de934541c3e66df430b2a6',
                       'member_sources': {'agile-beacon-80': '6ba25e2472d4a8c6bb145d6f9bc2e067c8ebad61',
                                          'grand-quest-20': 'd60c599767a340d3e3fd6dba2eff8451d349d7e7',
                                          'nimble-torrent-12': '9f2dd1988046baa0e40681c4457f4729b55e80c6',
                                          'prime-impact-40': '0b090fb969c46143f8cce7a55dd21f7d8bf1b1d8',
                                          'quick-pinnacle-97': '344a85b9d8388521975af9b830e4d8b17817562e',
                                          'stark-beacon-94': '7cf8687ede868f8f714a655a63a00ba5dd428266'}},
 'post_batch_composition': {'base_revision': '4a0c4e38ab979a1664586d47d16404b48def4089',
                            'base_owned_total': 161111,
                            'base_engine_lines': 85517,
                            'source_revision': '1f0633050802438db6ef954c43d6d0eb2ce0edf3',
                            'member_sources': {'sound-forge-62': '8e4e71b34b5c03b191b392a9d8f28176d177dd82'},
                            'approved_deltas': {'sound-forge-62': {'owned': 26,
                                                                   'engine': 4,
                                                                   'shared': 22,
                                                                   'tables': 0}},
                            'decision_message_id': 'msg-9f16388d39524c8b9a33d6796b88d1fb'},
 'frozen_batch_composition': {'batch_id': 'train-5429c3c71859f327ff8b46dbf1edc557',
                              'starting_revision': '48adaddad079a7adc2930e470590621c1575e674',
                              'source_revision': '73e99672d2447c44ef6a71d43a0187f2d16630af',
                              'base_owned_total': 161113,
                              'base_engine_lines': 85517,
                              'base_table_count': 45,
                              'member_sources': {'noble-flare-12': 'f09829d145915bc7426d24f28623283f7319a7a2',
                                                 'sound-forge-62': '15239e75971ae5ac39e6a7d0a5e467331ab58428'},
                              'approved_deltas': {'noble-flare-12': {'owned': 23,
                                                                     'engine': 22,
                                                                     'shared': 1,
                                                                     'tables': 0},
                                                  'sound-forge-62': {'owned': 26,
                                                                     'engine': 4,
                                                                     'shared': 22,
                                                                     'tables': 0}},
                              'reason': 'Compose both unchanged approved member grants on the '
                                        'retained starting head, which already includes '
                                        'fair-dune-41. Preserve every historical grant and '
                                        'measurement.'}}
FINAL_ENGINE_CAP = 11_999
FINAL_TOTAL_CAP = 19_999
FINAL_TABLE_CAP = 10
TARGET_TABLES = 5
EXTRA_TABLES = frozenset({
    "task_delivery_receipts", "task_branch_origins", "task_integration_checkpoints",
    "epic_dependencies", "project_integration_schedules", "project_integration_leases",
})
RETAINED_TABLES = frozenset({
    "integration_branch_owners", "integration_check_evidence", "integration_batches",
    "integration_batch_members", "integration_review_evidence",
})
MANIFEST_PATH = Path(__file__).with_name("integration_ownership.json")

# Direct semantic signals, independent of the file's location. Imported aliases
# and owned-symbol references extend this set below; comments alone are not proof.
SIGNAL = re.compile(
    r"integration[_.]|(?:^|\.)integration(?:\.|$)|Integration[A-Z_]|INTEGRATION_|hierarchical_integration|"
    r"EpicDelivery|epic_delivery|epicDelivery|delivery_status|"
    + "|".join(sorted(EXTRA_TABLES))
)
STORAGE_METHOD = re.compile(
    r"(?:set|upsert|append|write|record|log|save).*"
    r"(?:task_meta|metadata|op_event|operation_event|journal)|^log_event$"
)
STORAGE_TABLES = {"task_metadata", "op_events", "operation_events", "events",
                  "integration_subject_journal", "system_config"}
SQL_STORAGE_WRITE = re.compile(
    r"\b(?:insert\s+into|update|delete\s+from)\s+[\"\w.]*"
    r"(?:task_metadata|op_events|operation_events|events|integration_subject_journal|system_config)\b",
    re.IGNORECASE,
)
WRITE_CALLS = {"insert", "update", "delete"}
TS_START = re.compile(
    r"^(?:export\s+(?:default\s+)?)?(?:async\s+)?"
    r"(?:function|class|interface|type|const|let|var|import)\b"
)


def integration_table(name: str) -> bool:
    return name.startswith("integration_") or name in EXTRA_TABLES


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def tokens(node: ast.AST) -> set[str]:
    result = set()
    for part in ast.walk(node):
        if isinstance(part, ast.Name):
            result.add(part.id)
        elif isinstance(part, ast.Attribute):
            result.add(part.attr)
        elif isinstance(part, ast.Constant) and isinstance(part.value, str):
            result.add(part.value)
        elif isinstance(part, ast.alias):
            result.add(part.name)
            if part.asname:
                result.add(part.asname)
    return result


def fingerprint(node: ast.AST) -> str:
    return digest(ast.dump(node, include_attributes=False))


@dataclass(frozen=True)
class Unit:
    path: str
    name: str
    start: int
    end: int
    fingerprint: str
    references: frozenset[str]
    signal: bool
    kind: str = "text"

    @property
    def key(self) -> str:
        return f"{self.path}::{self.name}"


@dataclass
class Source:
    path: str
    data: bytes
    units: list[Unit]
    sinks: dict[str, dict]
    tables: dict[str, tuple[int, int]]
    imports: dict[str, str]


def attached_start(lines: list[str], start: int) -> int:
    """Attach preceding comments/blanks, stopping at the previous code line."""
    while start > 1:
        previous = lines[start - 2].strip()
        if previous and not previous.startswith(("#", "//", "/*", "*", "*/")):
            break
        start -= 1
    return start


def table_declarations(tree: ast.AST, *, owned_only: bool = True) -> dict[str, tuple[int, int]]:
    """Count Table calls by their literal SQL name, including aliased imports."""
    aliases = {"Table"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "sqlalchemy":
            aliases.update(alias.asname or alias.name for alias in node.names
                           if alias.name == "Table")
    result = {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and call_name(node.func) in aliases
                and node.args and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            name = node.args[0].value
            if not owned_only or integration_table(name):
                if name in result:
                    raise AssertionError(f"duplicate integration table declaration: {name}")
                result[name] = (node.lineno, node.end_lineno)
    return result


def python_source(path: str, data: bytes) -> Source:
    text = data.decode()
    lines = text.splitlines()
    tree = ast.parse(text, filename=path)
    aliases = set()
    imports = {}
    storage_aliases = set(STORAGE_TABLES)
    write_aliases = set(WRITE_CALLS)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and SIGNAL.search(node.module or ""):
            aliases.update(alias.asname or alias.name for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.startswith("sqlalchemy"):
                write_aliases.update(alias.asname or alias.name for alias in node.names
                                     if alias.name in WRITE_CALLS)
            if node.level:
                package = path.removesuffix(".py").split("/")[:-1]
                module = ".".join(package[:len(package) - node.level + 1] + [module]).rstrip(".")
            if module.startswith("src."):
                for alias in node.names:
                    imports[alias.asname or alias.name] = module.replace(".", "/") + ".py::" + alias.name
                    if alias.name in STORAGE_TABLES:
                        storage_aliases.add(alias.asname or alias.name)
    units = []
    sinks = {}

    def add(node: ast.AST, name: str, *, start=None, end=None, refs=None):
        references = tokens(node) if refs is None else refs
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            references = references | {node.name}
        first = node.lineno if start is None else start
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            first = min([first] + [d.lineno for d in node.decorator_list])
        units.append(Unit(
            path, name, attached_start(lines, first), end or node.end_lineno,
            fingerprint(node), frozenset(references),
            bool(any(SIGNAL.search(token) for token in references) or aliases & references),
            type(node).__name__,
        ))

    def visit(body, prefix=""):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                add(node, prefix + node.name)
            elif isinstance(node, ast.ClassDef):
                if SIGNAL.search(node.name):
                    add(node, prefix + node.name)
                    # Methods retain fingerprints if later extracted from a class.
                    visit(node.body, prefix + node.name + ".")
                else:
                    refs = {node.name} | set().union(*(tokens(b) for b in node.bases))
                    add(node, prefix + node.name + ".<header>",
                        end=node.body[0].lineno - 1, refs=refs)
                    visit(node.body, prefix + node.name + ".")
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                target = node.targets[0] if isinstance(node, ast.Assign) else node.target
                name = prefix + ast.unparse(target)
                value = node.value
                if isinstance(value, ast.Call) and call_name(value.func) == "Table":
                    sql_name = value.args[0].value
                    if integration_table(sql_name):
                        add(node, name)
                    else:
                        # A new table imported by owned code must receive an
                        # ownership decision even if its SQL name is innocuous.
                        add(node, name, refs={sql_name})
                        # Charge owned columns/constraints in ordinary tables;
                        # the generic task/project storage is not owned wholesale.
                        for column in value.args[2:]:
                            if isinstance(column, ast.Call) and column.args:
                                label = ast.unparse(column.args[0])
                                add(column, name + "[" + label + "]")
                elif isinstance(value, ast.Dict) and not SIGNAL.search(name):
                    for key, item in zip(value.keys, value.values, strict=True):
                        label = ast.unparse(key) if key is not None else "**" + ast.unparse(item)
                        refs = tokens(item) | (tokens(key) if key is not None else set())
                        add(item, name + "[" + label + "]",
                            start=key.lineno if key is not None else item.lineno, refs=refs)
                elif isinstance(value, (ast.List, ast.Tuple, ast.Set)) and not SIGNAL.search(name):
                    for item in value.elts:
                        if isinstance(item, ast.Dict):
                            label = next((str(v.value) for k, v in zip(item.keys, item.values)
                                          if isinstance(k, ast.Constant) and k.value == "name"
                                          and isinstance(v, ast.Constant)), fingerprint(item)[:16])
                        elif isinstance(item, ast.Constant):
                            label = repr(item.value)
                        else:
                            label = ast.unparse(item)
                        add(item, name + "[" + label + "]")
                else:
                    add(node, name)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                add(node, "import " + ast.unparse(node))
            elif isinstance(node, (ast.If, ast.Try, ast.With)):
                # Definitions behind TYPE_CHECKING/platform guards still count.
                visit(node.body, prefix)
                visit(getattr(node, "orelse", []), prefix)
                for handler in getattr(node, "handlers", []):
                    visit(handler.body, prefix)
            elif not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                      and isinstance(node.value.value, str)):
                add(node, prefix + "statement:" + fingerprint(node)[:16])

    visit(tree.body)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = call_name(node.func)
        refs = tokens(node)
        if (STORAGE_METHOD.search(name) or
                name in write_aliases and storage_aliases & refs or
                name == "text" and any(SQL_STORAGE_WRITE.search(token) for token in refs)):
            enclosing = [u for u in units if u.start <= node.lineno <= u.end]
            owner = min(enclosing, key=lambda u: u.end - u.start, default=None)
            key = f"{path}::{owner.name if owner else '<module>'}::{fingerprint(node)}"
            sinks[key] = {
                "line": node.lineno, "owner": owner.name if owner else "<module>",
                "call": name,
                "owner_fingerprint": owner.fingerprint if owner else fingerprint(tree),
                "integration_signal": bool(owner and owner.signal or any(
                    SIGNAL.search(token) for token in refs
                )),
            }
    return Source(path, data, units, sinks, table_declarations(tree, owned_only=False), imports)


def text_source(path: str, data: bytes) -> Source:
    lines = data.decode().splitlines()
    if path.endswith(".md"):
        starts = [i + 1 for i, line in enumerate(lines) if line.startswith("#")]
    else:
        starts = [i + 1 for i, line in enumerate(lines) if TS_START.match(line)]
    if not starts or starts[0] != 1:
        starts.insert(0, 1)
    starts = [attached_start(lines, start) for start in starts]
    units = []
    names = defaultdict(int)
    for index, start in enumerate(starts):
        end = starts[index + 1] - 1 if index + 1 < len(starts) else len(lines)
        if end < start:
            continue
        text = "\n".join(lines[start - 1:end])
        declaration = next((line for line in lines[start - 1:end]
                            if line.startswith("#") or TS_START.match(line)), "<header>")
        name = declaration.strip()
        names[name] += 1
        if names[name] > 1:
            name += f" #{names[name]}"
        units.append(Unit(path, name, start, end, digest(text),
                          frozenset(re.findall(r"[\w.]+", text)), bool(SIGNAL.search(text))))
    return Source(path, data, units, {}, {}, {})


@lru_cache(maxsize=4)
def sources(root: Path) -> dict[str, Source]:
    result = {}
    for directory, extensions in (("src", {".py", ".md"}),
                                  ("dashboard/src", {".ts", ".tsx"}),
                                  ("migrations", {".py"})):
        for path in sorted((root / directory).rglob("*")):
            rel = path.relative_to(root).as_posix()
            if (path.suffix not in extensions or
                    any(part in {"__pycache__", "__tests__", "testUtils", "versions"}
                        for part in path.parts) or ".test." in path.name):
                continue
            if directory == "migrations" and path.name != "integration_guards.py":
                continue
            if path.suffix == ".md" and not (
                "playbooks/" in rel or rel == "src/integration/development_policy_template.md"
            ):
                continue
            if path.name == "manifest.md":
                continue  # review attestations, not executable policy
            data = path.read_bytes()
            result[rel] = (python_source if path.suffix == ".py" else text_source)(rel, data)
    return result


@lru_cache(maxsize=4096)
def _dedicated_group(path: str, groups: tuple) -> str | None:
    for group, patterns in groups:
        for pattern in patterns:
            if Path(path).match(pattern):
                return group
    return None


def dedicated_group(path: str, manifest: dict) -> str | None:
    groups = tuple((group, tuple(patterns)) for group, patterns in manifest["dedicated"].items())
    return _dedicated_group(path, groups)


def inventory(root: Path, manifest: dict) -> dict:
    tree = sources(root)
    file_identities = set(manifest.get("owned_file_fingerprints", []))
    relocated_files = {path for path, source in tree.items()
                       if hashlib.sha256(source.data).hexdigest() in file_identities}
    owned = set(manifest["shared"])
    exclusions = manifest["exclusions"]
    identities = set(manifest["owned_fingerprints"])
    units = [unit for source in tree.values() for unit in source.units]
    candidates = {unit.key for unit in units if unit.signal or unit.key in owned
                  or unit.fingerprint in identities or unit.path in relocated_files
                  or dedicated_group(unit.path, manifest)}
    by_key = {unit.key: unit for unit in units}
    # An alias imported from a shared owned module is ownership evidence even
    # when neither the alias nor its caller contains "integration".
    owned_targets = {unit.key for unit in units if unit.key in owned
                     or unit.path in relocated_files
                     or dedicated_group(unit.path, manifest)}
    for unit in units:
        if any(alias in unit.references and target in owned_targets
               for alias, target in tree[unit.path].imports.items()):
            candidates.add(unit.key)
    generic = set(manifest.get("generic_dependencies", {}))
    # Follow imported pure helpers too: relocating/renaming a helper and importing
    # it back cannot hide ownership. Generic dependencies are explicitly reviewed.
    while True:
        added = set()
        for key in candidates:
            unit = by_key[key]
            if key in exclusions:
                continue
            for alias, target in tree[unit.path].imports.items():
                if alias in unit.references and target not in generic and target in by_key:
                    added.add(target)
        added -= candidates
        if not added:
            break
        candidates.update(added)
    unknown = []
    selected = defaultdict(set)
    for path, source in tree.items():
        group = dedicated_group(path, manifest)
        if group or path in relocated_files:
            selected[path].update(range(1, source.data.count(b"\n") + 1))
        for unit in source.units:
            relocated = unit.fingerprint in identities
            is_owned = unit.key in owned or relocated
            candidate = unit.key in candidates
            if candidate and unit.key not in exclusions:
                if not group and path not in relocated_files and not is_owned:
                    unknown.append(f"{unit.key} (lines {unit.start}-{unit.end})")
                selected[path].update(range(unit.start, unit.end + 1))
    groups = defaultdict(int)
    spans = {}
    for path, rows in selected.items():
        source = tree[path]
        # splitlines() would charge an unterminated final line: wc -l does not.
        newline_count = source.data.count(b"\n")
        charged = sorted(row for row in rows if row <= newline_count)
        spans[path] = charged
        groups[dedicated_group(path, manifest) or "shared"] += len(charged)
    tables = {}
    for path, source in tree.items():
        for name, span in source.tables.items():
            if not integration_table(name) and not any(
                unit.key in candidates and unit.key not in exclusions
                and unit.start <= span[0] and unit.end >= span[1]
                for unit in source.units
            ):
                continue
            if name in tables:
                raise AssertionError(f"duplicate table {name}: {tables[name][0]} and {path}")
            tables[name] = (path, *span)
    current_sinks = {key: sink for source in tree.values() for key, sink in source.sinks.items()}
    unknown_sinks = sorted(key for key, sink in current_sinks.items()
                           if key not in manifest["storage_sinks"] or
                           sink["owner_fingerprint"] != manifest["storage_sinks"][key].get(
                               "owner_fingerprint"))
    legacy_sinks = sorted(key for key in current_sinks
                          if manifest["storage_sinks"].get(key, {}).get("disposition") == "retire")
    return {"groups": dict(groups), "total": sum(groups.values()),
            "spans": spans, "tables": tables, "unknown": sorted(unknown),
            "unknown_sinks": unknown_sinks, "legacy_sinks": legacy_sinks}


def transition_owned_cap(manifest: dict) -> int:
    """Accept only the recorded batch decision; never derive a cap from counts."""
    original = BASELINE_OWNED_LINES + REPLACEMENT_ALLOWANCE
    exception = manifest.get("transition_exception")
    if exception is None:
        return original
    if exception != TRANSITION_BATCH_EXCEPTION:
        raise ValueError("unapproved integration transition exception")
    return original + TRANSITION_BATCH_EXCEPTION["delta"]


def budget_errors(report: dict, manifest: dict) -> list[str]:
    phase = manifest["phase"]
    if phase not in {"transition", "s4_modules", "final"}:
        return [f"unknown budget phase: {phase}"]
    if phase == "transition":
        engine_cap = BASELINE_ENGINE_LINES + REPLACEMENT_ALLOWANCE
        try:
            total_cap = transition_owned_cap(manifest)
        except ValueError as exc:
            return [str(exc)]
        table_cap = 45  # replacement reshapes the existing five, adds no table
    else:
        engine_cap, total_cap, table_cap = FINAL_ENGINE_CAP, FINAL_TOTAL_CAP, FINAL_TABLE_CAP
    errors = []
    for name, count, cap in (
        ("engine", report["groups"].get("engine", 0), engine_cap),
        ("owned total", report["total"], total_cap),
        ("tables", len(report["tables"]), table_cap),
    ):
        if count > cap:
            errors.append(f"{name}: {count:,} exceeds {cap:,} in {phase}")
    if phase != "transition" and report["legacy_sinks"]:
        errors.append("legacy journal/duplicate metadata writers remain: "
                      + ", ".join(report["legacy_sinks"]))
    return errors


def load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text())


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    manifest = load_manifest()
    report = inventory(root, manifest)
    print(json.dumps({key: value for key, value in report.items() if key != "spans"}, indent=2))
