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
TRANSITION_BATCH_EXCEPTION = {'amendments': [{'authorized_engine_delta': 15,
                 'authorized_owned_delta': 17,
                 'decision_message_id': 'msg-ab86b9766c2f42b9ba4920aba585b577',
                 'global_grant': 'msg-7f2f68c1',
                 'global_request': 'msg-9f895e0d',
                 'grant_delivery_message_id': 'msg-21784b3e56aa4f54bf1042b7c9af4aaf',
                 'previous_engine_lines': 85301,
                 'previous_owned_total': 160820,
                 'reason': 'Memoize stack authorization inputs within one scheduler sync pass; '
                           'supervisor approved exact net footprint after removing an unused '
                           'parent query.',
                 'referenced_decision': 'msg-9f895e0d',
                 'task_id': 'solid-impact-56'},
                {'authorized_engine_delta': 65,
                 'authorized_owned_delta': 65,
                 'decision_message_id': 'msg-1984d09ec70e479c9d0cc14c57e828c3',
                 'delegation_thread': 'size-cap-delegation-2026-10-23',
                 'grant_delivery_message_id': 'msg-1984d09ec70e479c9d0cc14c57e828c3',
                 'operator_decision': '6679052c',
                 'previous_engine_lines': 85316,
                 'previous_owned_total': 160837,
                 'project_grant_message_id': 'msg-00b0e08117cc479c9a4a0b36fc5a30d0',
                 'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/1820',
                 'reason': 'Bounded retry of provably unlaunched batch fixers and recovery of '
                           'expired legacy family owners; global supervisor approved the exact '
                           '+65-line exception under the project supervisor delegation.',
                 'referenced_decision': 'msg-00b0e081',
                 'task_id': 'agile-beacon-80'},
                {'authorized_engine_delta': 0,
                 'authorized_owned_delta': 1,
                 'authorized_shared_delta': 1,
                 'decision_message_id': 'msg-3bed5d5db4ed4c7fa1ef9150b1c3dd8c',
                 'previous_owned_total': 160879,
                 'previous_shared_lines': 62006,
                 'reason': 'Accept repository mappings in the shared GitHub binding resolver for '
                           'verified CI waits; supervisor approved the exact one-line shared '
                           'footprint.',
                 'referenced_decision': 'size-cap-delegation-2026-10-23',
                 'task_id': 'nimble-torrent-12'},
                {'authorized_engine_delta': 82,
                 'authorized_owned_delta': 103,
                 'decision_message_id': 'msg-a337e2c905ce4f5c8a236344a4e25211',
                 'grant_source_message_id': 'msg-330ea2478c724381acbb5512fc99e4b5',
                 'previous_engine_lines': 85316,
                 'previous_owned_total': 160879,
                 'reason': 'GitHub caller diagnostics, shared background quota reserve and '
                           'inventory reuse; exact supervisor grant under '
                           'size-cap-delegation-2026-10-23.',
                 'referenced_decision': 'size-cap-delegation-2026-10-23',
                 'supersedes_message_id': 'msg-ac47a08407a6462bb411326ecefeab44',
                 'task_id': 'prime-impact-40'},
                {'authorized_engine_delta': 28,
                 'authorized_owned_delta': 28,
                 'decision_message_id': 'msg-e2dd5a423516458eb564e745a12ab30e',
                 'measured_engine_lines': 85496,
                 'measured_owned_total': 161081,
                 'measured_source_revision': '344a85b9d8388521975af9b830e4d8b17817562e',
                 'measured_table_count': 45,
                 'operator_decision': '6679052c',
                 'previous_engine_lines': 85468,
                 'previous_owned_total': 161053,
                 'previous_source_revision': 'dd95038cca3fef9aae70eb0092079fe24d52a062',
                 'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/1884',
                 'reason': 'Recover orphaned launching fixer reservations after a per-attempt '
                           'grace period; fence late registration and completion by session, '
                           'instance and launch timestamp. Exact self-approved measurement under '
                           'the project supervisor delegation.',
                 'referenced_decision': 'size-cap-delegation-2026-10-23',
                 'task_id': 'quick-pinnacle-97'},
                {'authorized_engine_delta': 21,
                 'authorized_owned_delta': 21,
                 'authorized_table_delta': 0,
                 'base_revision': 'dd95038cca3fef9aae70eb0092079fe24d52a062',
                 'decision_id': 'size-cap-delegation-2026-10-23',
                 'measured_source_revision': '24d081eecf0c00a912a064e37ba616334382e263',
                 'operator_decision': '6679052c',
                 'operator_task': 'sharp-cascade-53',
                 'pr_url': 'https://github.com/ElectricJack/agent-queue/pull/1817',
                 'previous_engine_lines': 85468,
                 'previous_owned_total': 161053,
                 'previous_source_revision': '438aa36cc93945305ea291622009f6b34d4ee733',
                 'previous_table_count': 45,
                 'reason': 'Exclude published base history from repair identity checks; '
                           'supervisor granted the exact measured footprint for PR #1817 under '
                           'the delegated operator decision. Plain merge retains the existing '
                           'implementation and all dev amendments.',
                 'task_id': 'stark-beacon-94'},
                {'authorized_engine_delta': 0,
                 'authorized_owned_delta': 9,
                 'decision_message_id': 'msg-3f6311071a6647429059f3a990750597',
                 'grant_id': 'sharp-rapids-10-cap-epoch2-2026-10-23',
                 'measured_engine_lines': 85517,
                 'measured_owned_total': 161111,
                 'measured_source_revision': '43b3e220a2a1ac6e8d5b7543d3f83063c7c747f6',
                 'measured_table_count': 45,
                 'operator_decision': '6679052c',
                 'previous_engine_lines': 85517,
                 'previous_owned_total': 161102,
                 'previous_source_revision': '61869b7cdcf507b8e8acb596893bb63ac0794997',
                 'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/1885',
                 'reason': 'Dashboard PR snapshot quota fix; exact project supervisor amendment '
                           'under size-cap-delegation-2026-10-23. No automatic ceiling increase '
                           'or source relocation.',
                 'referenced_decision': 'size-cap-delegation-2026-10-23',
                 'task_id': 'sharp-rapids-10'},
                {'authorized_engine_delta': 0,
                 'authorized_owned_delta': 2,
                 'authorized_table_delta': 0,
                 'charged_definition': 'src/doctor/stall_checks.py::_check_sweep',
                 'decision_message_id': 'msg-99539879',
                 'measured_engine_lines': 85517,
                 'measured_owned_total': 161113,
                 'measured_source_revision': 'ca7a4377c532d723b0ab9c3bb5f833707a317cf5',
                 'measured_table_count': 45,
                 'original_approved_owned_total': 161053,
                 'original_measured_owned_total': 161055,
                 'previous_engine_lines': 85517,
                 'previous_owned_total': 161111,
                 'previous_source_revision': '4a0c4e38ab979a1664586d47d16404b48def4089',
                 'previous_table_count': 45,
                 'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/1886',
                 'reason': 'Stall-sweep alert for non-ready playbook activations. Global '
                           'supervisor approved the exact +2 owned lines under '
                           'size-cap-delegation-2026-10-23; project supervisor directed carrying '
                           'that unchanged delta onto current dev after PR1885. Engine lines and '
                           'table count remain unchanged.',
                 'referenced_decision': 'size-cap-delegation-2026-10-23',
                 'task_id': 'fair-dune-41'},
                {'authorized_engine_delta': 22,
                 'authorized_owned_delta': 23,
                 'authorized_shared_delta': 1,
                 'decision_message_id': 'msg-68ccb1d50ee04851a7268dd58e9d4459',
                 'measured_engine_lines': 85539,
                 'measured_owned_total': 161134,
                 'measured_source_revision': 'b194122701a0130039230dea4c8caa93c70f60e1',
                 'measured_table_count': 45,
                 'operator_decision': '6679052c',
                 'operator_task': 'sharp-cascade-53',
                 'previous_engine_lines': 85517,
                 'previous_owned_total': 161111,
                 'previous_source_revision': '4a0c4e38ab979a1664586d47d16404b48def4089',
                 'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/1892',
                 'reason': 'Bound cached tile readiness to visible candidates and stack proof '
                           'sources; exact project supervisor approval under '
                           'size-cap-delegation-2026-10-23. Preserve scheduler shared inputs and '
                           'fail-closed authorization. No new storage or automatic cap increase.',
                 'referenced_decision': 'size-cap-delegation-2026-10-23',
                 'task_id': 'noble-flare-12'},
                {'authorized_engine_delta': 4,
                 'authorized_owned_delta': 26,
                 'authorized_shared_delta': 22,
                 'authorized_table_delta': 0,
                 'decision_message_id': 'msg-9f16388d39524c8b9a33d6796b88d1fb',
                 'measured_engine_lines': 85521,
                 'measured_owned_total': 161137,
                 'measured_source_revision': '1f0633050802438db6ef954c43d6d0eb2ce0edf3',
                 'measured_table_count': 45,
                 'operator_decision': '6679052c',
                 'operator_task': 'sharp-cascade-53',
                 'original_task_base_engine_lines': 85468,
                 'original_task_base_owned_total': 161053,
                 'original_task_engine_lines': 85472,
                 'original_task_owned_total': 161079,
                 'original_task_source_revision': '8e4e71b34b5c03b191b392a9d8f28176d177dd82',
                 'previous_engine_lines': 85517,
                 'previous_owned_total': 161111,
                 'previous_source_revision': '4a0c4e38ab979a1664586d47d16404b48def4089',
                 'previous_table_count': 45,
                 'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/1891',
                 'reason': 'Explicit supervisor approval of the exact retained Git filing '
                           'resolver footprint; compose the unchanged +26 owned/+4 engine grant '
                           'on dev, preserving historical grants.',
                 'referenced_decision': 'size-cap-delegation-2026-10-23',
                 'task_id': 'sound-forge-62'},
                {'authorized_engine_delta': 0,
                 'authorized_owned_delta': 50,
                 'decision_message_id': 'msg-5c55b72faec1497391205876fc5dfeb9',
                 'grant_delivery_message_id': 'msg-6cbd33a9db9846f29816f2fc0df8f8fd',
                 'implementation_revision': '70d79c47c561ee85ecd74080767bbc5cdf0e210c',
                 'merged_dev_revision': '4a0c4e38ab979a1664586d47d16404b48def4089',
                 'operator_decision': '6679052c',
                 'previous_engine_lines': 85517,
                 'previous_owned_total': 161111,
                 'pull_request': 1814,
                 'reason': 'Bounded transient retry of resume workspace preparation and the '
                           'fenced supervisor requeue of held continuations; supervisor approved '
                           'exact footprint after trimming from +109.',
                 'referenced_decision': 'size-cap-delegation-2026-10-23',
                 'request_message_id': 'msg-f3ef547cbf48428abcab2266d7fe8df6',
                 'source_base': '1e3ac0b14608fd4a4b5a2c90a25cacb57f4bf511',
                 'task_id': 'calm-rapids-50'},
                {'authorized_engine_delta': 1,
                 'authorized_owned_delta': 1,
                 'authorized_table_delta': 0,
                 'decision_message_id': 'msg-96fed6b8e86148f0bbf55ded244c9a0f',
                 'grant_delivery_message_id': 'msg-3a0b0c6448ef44699f7a7d7c43777642',
                 'measured_engine_lines': 85518,
                 'measured_owned_total': 161112,
                 'measured_source_revision': '14828bb29104d825e9ba5a75aa44a3799dd48a48',
                 'measured_table_count': 45,
                 'operator_decision': '6679052c',
                 'operator_task': 'sharp-cascade-53',
                 'previous_engine_lines': 85517,
                 'previous_owned_total': 161111,
                 'previous_source_revision': '4a0c4e38ab979a1664586d47d16404b48def4089',
                 'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/1890',
                 'reason': 'Shared exact-commit check-run observations and one bounded recovery '
                           'wait; plain dev merge retains dashboard badges, quota reserve and '
                           'ETag behavior. Supervisor approved the exact merged +1 owned / +1 '
                           'engine measurement under size-cap-delegation-2026-10-23.',
                 'referenced_decision': 'size-cap-delegation-2026-10-23',
                 'task_id': 'fair-harbor-71'},
                {'authorized_engine_delta': 0,
                 'authorized_owned_delta': 19,
                 'authorized_shared_delta': 19,
                 'authorized_table_delta': 0,
                 'decision_message_id': 'msg-76ff7843e72d451b9017a0b4db30c970',
                 'implementation_message_id': 'msg-9a7a03cd',
                 'measured_engine_lines': 85543,
                 'measured_owned_total': 161181,
                 'measured_source_revision': '10af37472a90cd488707c483047c9c97e2f7a5db',
                 'measured_table_count': 45,
                 'previous_engine_lines': 85543,
                 'previous_measurement_revision': '73e99672d2447c44ef6a71d43a0187f2d16630af',
                 'previous_owned_total': 161162,
                 'previous_source_revision': 'c177d24e86d25fde7804131c94bfd637b0eb540a',
                 'previous_table_count': 45,
                 'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/1759',
                 'reason': 'Exact project supervisor grant for the PR1759 merge repair under '
                           'size-cap-delegation-2026-10-23, with explicit delegation to record '
                           "the approved grant. Preserve recovery holds through dev's "
                           'manual-pause Resume path until the authorized restart READY '
                           'transaction. Keep the historical frozen-batch measurements; engine '
                           'and table counts remain unchanged.',
                 'referenced_decision': 'size-cap-delegation-2026-10-23',
                 'root_task_id': 'prime-crest-79',
                 'task_id': 'fair-flare-10'},
                {'authorized_owned_delta': -30,
                 'decision': 'size-cap-delegation-2026-10-23',
                 'previous_owned_total': 160879,
                 'reason': 'Remove the legacy updater path in clear-delta-43 / PR #1855 after '
                           'merging PR #1797; re-record exactly origin/dev measured_owned_total '
                           'minus 30 under the supervisor delegated size-cap decision.',
                 'task_id': 'prime-grove-46'},
                {'authorized_engine_delta': 8,
                 'authorized_owned_delta': 12,
                 'authorized_shared_delta': 4,
                 'authorized_table_delta': 0,
                 'charged_definitions': ['src/integration/pr_repair_publication.py::PRRepairPublication',
                                         'src/cli/client.py::_COMMAND_TIMEOUTS'],
                 'decision_message_id': 'msg-c47a1a9a635a465b8a0cb286f93cb0b0',
                 'measured_engine_lines': 85551,
                 'measured_owned_total': 161174,
                 'measured_shared_lines': 62041,
                 'measured_source_revision': '925ee7bf7fbeffacf5db9fb2b4c41420f67d7be3',
                 'measured_table_count': 45,
                 'operator_decision': '6679052c',
                 'operator_task': 'sharp-cascade-53',
                 'previous_engine_lines': 85543,
                 'previous_owned_total': 161162,
                 'previous_shared_lines': 62037,
                 'previous_source_revision': 'c177d24e86d25fde7804131c94bfd637b0eb540a',
                 'previous_table_count': 45,
                 'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/1902',
                 'reason': 'Authenticated current remote PR base history and repair/root-redrive '
                           'CLI timeout budgets. Project supervisor approved the exact +12 owned '
                           '(+8 engine, +4 shared) footprint for PR1902 under '
                           'size-cap-delegation-2026-10-23; preserve historical frozen-batch '
                           'measurements and classifications.',
                 'referenced_decision': 'size-cap-delegation-2026-10-23',
                 'task_id': 'calm-lantern-21'},
                {'authorized_engine_delta': 55,
                 'authorized_owned_delta': 55,
                 'authorized_shared_delta': 0,
                 'authorized_table_delta': 0,
                 'decision_message_id': 'msg-c45ccf8341f946819758858c7bff488d',
                 'measured_engine_lines': 85599,
                 'measured_owned_total': 161268,
                 'measured_source_revision': 'ddf86c8595adb25c7361740bf680b2273f88c90a',
                 'measured_table_count': 45,
                 'operator_decision': '6679052c',
                 'operator_task': 'sharp-cascade-53',
                 'previous_engine_lines': 85544,
                 'previous_owned_total': 161213,
                 'previous_source_revision': 'dae4a86819d8c4ed6ec70d0d9349f3895f284c4d',
                 'previous_table_count': 45,
                 'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/1907',
                 'reason': 'Cache exact-head root PR admission against the fetched target and '
                           'report selection coverage; exact supervisor approval under '
                           'size-cap-delegation-2026-10-23. Preserve historical grants; no '
                           'headroom or new storage.',
                 'referenced_decision': 'size-cap-delegation-2026-10-23',
                 'task_id': 'smart-forge-49'},
                {'authorized_engine_delta': 204,
                 'authorized_owned_delta': 295,
                 'authorized_shared_delta': 91,
                 'authorized_storage_category_delta': 0,
                 'authorized_table_delta': 0,
                 'decision_message_id': 'msg-625e460c4a7c456983e5eb114ea22446',
                 'implementation_revision': 'b8b754ecb8992b45b6c3dfefa9c51bf2d9519b3f',
                 'measured_engine_lines': 85748,
                 'measured_owned_total': 161508,
                 'measured_shared_lines': 62087,
                 'measured_source_revision': '55a41b433cec061cddfd51ab786af7366663bbc5',
                 'measured_table_count': 45,
                 'operator_decision': '6679052c',
                 'operator_task': 'sharp-cascade-53',
                 'previous_engine_lines': 85544,
                 'previous_owned_total': 161213,
                 'previous_shared_lines': 61996,
                 'previous_source_revision': 'dae4a86819d8c4ed6ec70d0d9349f3895f284c4d',
                 'previous_table_count': 45,
                 'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/1908',
                 'reason': 'Drop conflicting train members and their dependents, rebuild the '
                           'healthy remainder and refresh the original source. Project supervisor '
                           'approved the exact merged footprint under '
                           'size-cap-delegation-2026-10-23; no headroom, new tables or storage '
                           'categories. Preserve all historical grants and final caps.',
                 'referenced_decision': 'size-cap-delegation-2026-10-23',
                 'task_id': 'azure-cascade-22'},
                {'authorized_engine_delta': 0,
                 'authorized_owned_delta': 26,
                 'decision_message_id': 'msg-c80bde55a6d1442791a2ba4f9597d7a0',
                 'measured_engine_lines': 85316,
                 'measured_owned_total': 160863,
                 'measured_table_count': 45,
                 'operator_decision': '6679052c',
                 'pr_url': 'https://github.com/ElectricJack/agent-queue/pull/1861',
                 'previous_engine_lines': 85316,
                 'previous_owned_total': 160837,
                 'reason': 'PR #1861 latency instrumentation and bounded background graph waits; '
                           'project supervisor approved the exact footprint under '
                           'size-cap-delegation-2026-10-23. Retain features and count equality.',
                 'referenced_decision': 'size-cap-delegation-2026-10-23',
                 'task_id': 'bright-impact-14'},
                {'authorized_engine_delta': 1,
                 'authorized_owned_delta': 105,
                 'authorized_table_delta': 0,
                 'confirmation_message_id': 'msg-1d04c8bcd4cd4b3a8274bba7518e47d9',
                 'decision_message_id': 'msg-2a11607018264013b97698493f53906e',
                 'measured_engine_lines': 85518,
                 'measured_owned_total': 161218,
                 'measured_source_revision': '0ec24f68578dbf9834fa96c0aa1133c15b6270ed',
                 'measured_table_count': 45,
                 'operator_decision': '6679052c',
                 'operator_task': 'sharp-cascade-53',
                 'previous_engine_lines': 85517,
                 'previous_owned_total': 161113,
                 'previous_source_revision': '48adaddad079a7adc2930e470590621c1575e674',
                 'previous_table_count': 45,
                 'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/1898',
                 'reason': 'Supervisor-owned wait delivery and independent CI observation '
                           'recovery. Exact project-supervisor amendment under '
                           'size-cap-delegation-2026-10-23; preserve existing ownership '
                           'classifications and historical grants.',
                 'referenced_decision': 'size-cap-delegation-2026-10-23',
                 'task_id': 'fair-meadow-87'},
                {'authorization': 'Supervisor reopen instructions, 2026-10-23: record the exact '
                                  'measured amendment without asking.',
                 'authorized_engine_delta': 352,
                 'authorized_owned_delta': 352,
                 'authorized_shared_delta': 0,
                 'authorized_table_delta': 0,
                 'measured_engine_lines': 85895,
                 'measured_owned_total': 161514,
                 'measured_sources_sha256': {'src/integration/bookkeeping_guard.py': '919f524776c0d9989b3c4283936fc958ab380076c945cae64a39bf2f9b91262b',
                                             'src/integration/development.py': '0523ad2f59f852dc623b2155a64cfbab6d43f0e9c5a23d98647181307c2136e2',
                                             'src/integration/generated_merge_driver.py': '91f79cf8c4d0f7cc91bcc6427693ed3999214154234c388b994c35524cbfc720',
                                             'src/integration/gitops.py': '920cebc4a60a17361489c70a49a5c81e7daa3f6f588a43f6aaa4cd838d316023',
                                             'src/integration/regeneration.py': 'ebb5053f3734bfa05ffc9339511bab273425528f55b873f23b7641598336976c'},
                 'measured_table_count': 45,
                 'previous_engine_lines': 85543,
                 'previous_owned_total': 161162,
                 'previous_source_revision': '2f84216797cfb17b6cf0cda921d659a96ca8447b',
                 'previous_table_count': 45,
                 'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/1906',
                 'reason': 'Fail closed on unported reviewed legacy bookkeeping deltas before '
                           'train regeneration, Git generated merges and regeneration checks. '
                           'Exact scoped guard footprint; preserve all frozen batch grants and '
                           'original baselines.',
                 'referenced_decision': 'size-cap-delegation-2026-10-23',
                 'task_id': 'prime-vault-39'},
                {'authorized_engine_delta': 4,
                 'authorized_owned_delta': 186,
                 'decision_message_id': 'msg-08efc39cceeb430cafb8636f1acb85f1',
                 'measured_source_revision': 'a68665e4afde3be9a0aba0d9dec50e61e632be84',
                 'operator_decision': '6679052c',
                 'previous_engine_lines': 85316,
                 'previous_owned_total': 160837,
                 'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/1858',
                 'reason': 'Escalation source recovery and supervisor routing, including the '
                           'deferred database imports required by CLI discovery; supervisor '
                           'approved the exact PR #1858 footprint under '
                           'size-cap-delegation-2026-10-23.',
                 'referenced_decision': 'size-cap-delegation-2026-10-23',
                 'task_id': 'keen-forge-77'},
                {'authorized_engine_delta': 1,
                 'authorized_owned_delta': 325,
                 'authorized_shared_delta': 324,
                 'decision_message_id': 'msg-1b59c9af50fd43418246593c54d85a16',
                 'decision_record': 'decision:8f9bd96b-900d-4afc-8b98-645dd0ae4eab',
                 'global_request': 'msg-a65b93b943b44cd48cbdee7fb0672950',
                 'grant_delivery_message_id': 'msg-8011136a1ef04a6b8ec1112fab150eb1',
                 'measured_shared_lines': 62288,
                 'previous_engine_lines': 85316,
                 'previous_owned_total': 160837,
                 'previous_shared_lines': 61964,
                 'reason': 'Merge held-ingestion P3 (sharp-cascade-53, PR #1774, ddc3e6e6d) with '
                           'dev 94e69a6a6; global supervisor approved the exact merged footprint '
                           '(batch release outbox enqueue, retirement proof reads, held-release '
                           'contracts and doctor checks).',
                 'task_id': 'fair-ember-93'},
                {'authorized_engine_delta': -486,
                 'authorized_owned_delta': -9,
                 'decision_message_id': 'msg-8f9cda4b9dcc460f8bcf006a22bfa2e0',
                 'global_decision': '907cdbb6-a78a-4d3f-9fd0-063b0cd050d6',
                 'global_grant': 'msg-178ee4e85b0140598410d0cb16005443',
                 'previous_engine_lines': 85316,
                 'previous_owned_total': 160837,
                 'reason': "Held-ingestion P4 merged onto dev: dev's retirements plus three "
                           'orphaned helpers removed; supervisor approved recording the exact '
                           'lower footprint under size-cap-delegation-2026-10-23.',
                 'referenced_decision': 'msg-f06302bed6424579bdb6b51290cbed1e',
                 'task_id': 'stark-grove-42'},
                {'authorized_engine_delta': 0,
                 'authorized_owned_delta': 144,
                 'decision_message_id': 'msg-98d8517c56104e8fbf87e1330d2d5c1a',
                 'grant_baseline_owned_total': 160820,
                 'grant_maximum_delta': 172,
                 'measured_delta_from_grant_baseline': 161,
                 'measured_source_revision': '07988d776f3dd68759d34c8152dd684099ff7298',
                 'operator_decision_id': 'c363ba40-0d03-4303-8667-e192e92091ad',
                 'previous_engine_lines': 85316,
                 'previous_owned_total': 160837,
                 'reason': 'Exact remaining ownership-fence footprint after sharing session '
                           'predicates and reusing transaction row reads; no change to baseline, '
                           'final caps or storage categories.',
                 'task_id': 'nimble-meadow-99'},
                {'authorized_engine_delta': -534,
                 'authorized_owned_delta': 129,
                 'authorized_table_delta': 0,
                 'measured_engine_lines': 85010,
                 'measured_owned_total': 161342,
                 'measured_source_revision': '9b8ad6e58179dde7ac486d12d446e116e8120187',
                 'measured_table_count': 45,
                 'merged_dev_revision': 'd60c0f238c8205f495a384c0bd77c322a8fc89ac',
                 'operator_decision': '6679052c',
                 'operator_task': 'sharp-cascade-53',
                 'previous_engine_lines': 85544,
                 'previous_owned_total': 161213,
                 'previous_source_revision': '4bafa45f1ac0a466eab372c7b6b0b866f67bb832',
                 'previous_table_count': 45,
                 'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/1802',
                 'reason': 'Exact combined-tree remeasurement after merging origin/dev into PR '
                           '#1802. Preapproved by size-cap-delegation-2026-10-23, operator '
                           'decision 6679052c on sharp-cascade-53. Preserve every historical '
                           'grant and classification; no code trimmed or moved for the count.',
                 'referenced_decision': 'size-cap-delegation-2026-10-23',
                 'task_id': 'nimble-meadow-99'},
                {'authority_task': 'sharp-cascade-53',
                 'authorized_engine_delta': 0,
                 'authorized_owned_delta': 131,
                 'branch': 'aq/epic/wait-graph-advisory-lock-held-across-idle-in-transaction',
                 'decision_message_id': 'msg-000c812477994a65a62633a4a051224c',
                 'measured_engine_lines': 85316,
                 'measured_owned_total': 160968,
                 'measured_source_revision': '24b2dde7d4be3f393c698eff68b2fd08bd7b9de0',
                 'measured_table_count': 45,
                 'previous_engine_lines': 85316,
                 'previous_owned_total': 160837,
                 'reason': 'Keep external observations outside the wait graph fence and reconcile '
                           'one scheduler owner per transaction; the extracted reconciliation '
                           'helper remains explicitly charged. Project supervisor approved the '
                           'exact measured footprint under the delegated size-cap authority.',
                 'referenced_decision': 'size-cap-delegation-2026-10-23',
                 'task_id': 'bold-flare-41'},
                {'authority_task': 'sharp-cascade-53',
                 'authorized_engine_delta': 0,
                 'authorized_owned_delta': 131,
                 'branch': 'aq/epic/wait-graph-advisory-lock-held-across-idle-in-transaction',
                 'decision_id': 'size-cap-delegation-2026-10-23',
                 'measured_engine_lines': 85544,
                 'measured_owned_total': 161344,
                 'measured_source_revision': '0300be58d47adf905900b108db979240ff4aea16',
                 'measured_table_count': 45,
                 'operator_decision': '6679052c',
                 'previous_engine_lines': 85544,
                 'previous_owned_total': 161213,
                 'previous_source_revision': 'dae4a86819d8c4ed6ec70d0d9349f3895f284c4d',
                 'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/1816',
                 'reason': 'Preapproved exact PR #1816 conflict-repair measurement on merged dev '
                           'tree. Preserve all historical grants and the charged extracted '
                           'scheduler/resume helpers; no implementation changes beyond conflict '
                           'resolution.',
                 'task_id': 'bold-flare-41'},
                {'authorized_engine_delta': 366,
                 'authorized_owned_delta': 430,
                 'authorized_table_delta': 0,
                 'decision_id': 'size-cap-delegation-2026-10-23',
                 'decision_message_id': 'msg-efc241b8ddbb4c1fb63a41f5fe3f0d90',
                 'grant_delivery_message_id': 'msg-21f0bc97741343c39d69d7c5c7b97400',
                 'measured_engine_lines': 85376,
                 'measured_owned_total': 161647,
                 'measured_source_revision': '9c830f717be72274fb8af1521941c52927af541b',
                 'measured_table_count': 45,
                 'operator_decision': '6679052c',
                 'operator_task': 'sharp-cascade-53',
                 'previous_engine_lines': 85010,
                 'previous_owned_total': 161217,
                 'previous_source_revision': 'de9a68b81cfcabe55ca5ce527a933891d36c8a1d',
                 'previous_table_count': 45,
                 'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/1789',
                 'reason': 'Compose the reviewed obsolete-child disposition with current dev '
                           'using a plain merge. Exact merged measurement pre-approved under '
                           'size-cap-delegation-2026-10-23; preserve all historical grants and '
                           'strict Git ancestry.',
                 'task_id': 'fair-flare-67'}],
 'authenticated_push_measurement': {'approval_message_id': 'msg-49a7a4f4f8a5420eb80abf1d4d8b4a12',
                                    'authorized_engine_delta': 13,
                                    'authorized_owned_delta': 13,
                                    'decision_id': 'size-cap-delegation-2026-10-23',
                                    'headroom': 0,
                                    'measured_engine_lines': 85334,
                                    'measured_owned_total': 160897,
                                    'measured_source_revision': '715a3caf99c083f80a9426a303c9336cbb264921',
                                    'measured_table_count': 45,
                                    'prior_engine_lines': 85321,
                                    'prior_owned_total_cap': 160884,
                                    'reason': 'Persist fenced push-failure diagnostics in fixer '
                                              'blockers and train notices.',
                                    'request_message_id': 'msg-027dff134dbc4cd8b5966c36151384ba',
                                    'restore_task': 'azure-ridge-72',
                                    'task_id': 'fleet-dune-15'},
 'authenticated_push_merge_measurement': {'approval_message_id': 'msg-49a7a4f4f8a5420eb80abf1d4d8b4a12',
                                          'authority': 'Supervisor reopen preapproval, '
                                                       '2026-10-23: exact merged amendment for PR '
                                                       '#1882.',
                                          'authorized_engine_delta': 13,
                                          'authorized_owned_delta': 13,
                                          'authorized_table_delta': 0,
                                          'dev_engine_lines': 85010,
                                          'dev_owned_total': 161198,
                                          'dev_table_count': 45,
                                          'headroom': 0,
                                          'measured_engine_lines': 85023,
                                          'measured_owned_total': 161211,
                                          'measured_source_revision': '34302a22e2bc0541d1e73b667bb9161cf27431e6',
                                          'measured_table_count': 45,
                                          'merged_dev_revision': 'd60c0f238c8205f495a384c0bd77c322a8fc89ac',
                                          'operator_decision': '6679052c',
                                          'operator_task': 'sharp-cascade-53',
                                          'prior_engine_cap': 85544,
                                          'prior_owned_total_cap': 161213,
                                          'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/1882',
                                          'reason': 'Carry unchanged approved push diagnostics '
                                                    'onto independently measured dev; retain all '
                                                    'historical grants and real reductions.',
                                          'referenced_decision': 'size-cap-delegation-2026-10-23',
                                          'restore_task': 'azure-ridge-72',
                                          'task_id': 'fleet-dune-15'},
 'authenticated_push_merge_measurement_history': [{'approval_message_id': 'msg-49a7a4f4f8a5420eb80abf1d4d8b4a12',
                                                   'authority': 'Supervisor reopen preapproval, '
                                                                '2026-10-23: exact merged '
                                                                'amendment for PR #1882.',
                                                   'authorized_engine_delta': 13,
                                                   'authorized_owned_delta': 13,
                                                   'authorized_table_delta': 0,
                                                   'dev_engine_lines': 85544,
                                                   'dev_owned_total': 161213,
                                                   'dev_table_count': 45,
                                                   'headroom': 0,
                                                   'measured_engine_lines': 85557,
                                                   'measured_owned_total': 161226,
                                                   'measured_source_revision': 'a8cb26e99371072b6b4ff69bfdf9852aafdddd29',
                                                   'measured_table_count': 45,
                                                   'merged_dev_revision': 'dae4a86819d8c4ed6ec70d0d9349f3895f284c4d',
                                                   'operator_decision': '6679052c',
                                                   'operator_task': 'sharp-cascade-53',
                                                   'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/1882',
                                                   'reason': 'Carry the unchanged approved '
                                                             'push-diagnostic delta onto merged '
                                                             'dev; preserve every historical '
                                                             'grant.',
                                                   'referenced_decision': 'size-cap-delegation-2026-10-23',
                                                   'restore_task': 'azure-ridge-72',
                                                   'task_id': 'fleet-dune-15'}],
 'batch_composition': {'approved_deltas': {'agile-beacon-80': {'engine': 65, 'owned': 65},
                                           'fresh-apex-30': {'engine': 5, 'owned': 5},
                                           'nimble-torrent-12': {'engine': 0, 'owned': 1},
                                           'prime-impact-40': {'engine': 82, 'owned': 103},
                                           'quick-pinnacle-97': {'engine': 28, 'owned': 28},
                                           'stark-beacon-94': {'engine': 21, 'owned': 21}},
                       'base_engine_lines': 85316,
                       'base_owned_total': 160879,
                       'base_revision': '4e50c276fca68cbe92ba4fc704363ec6026db7dc',
                       'batch_id': 'train-854bd30527fc85b1edc0e2c7047bd673',
                       'member_sources': {'agile-beacon-80': '6ba25e2472d4a8c6bb145d6f9bc2e067c8ebad61',
                                          'grand-quest-20': 'd60c599767a340d3e3fd6dba2eff8451d349d7e7',
                                          'nimble-torrent-12': '9f2dd1988046baa0e40681c4457f4729b55e80c6',
                                          'prime-impact-40': '0b090fb969c46143f8cce7a55dd21f7d8bf1b1d8',
                                          'quick-pinnacle-97': '344a85b9d8388521975af9b830e4d8b17817562e',
                                          'stark-beacon-94': '7cf8687ede868f8f714a655a63a00ba5dd428266'},
                       'reason': 'Compose the existing exact grants on their common measured dev '
                                 'tree; preserve each historical measurement below.',
                       'source_revision': '4e98fa0b96d561cfa7de934541c3e66df430b2a6',
                       'starting_revision': 'bd8fcfe183d6a59ea90f4498cda2bee80a7222f4'},
 'batch_id': 'train-745592300af5cef0338dec80ebabb465',
 'batch_measured_delta': 2310,
 'category_approval_message_id': 'msg-90f9f1b1e0cd4e8e8d28071cffc927a6',
 'category_restore_task': 'stark-willow-95.17',
 'daemon_latency_merge_measurement': {'authorized_engine_delta': 0,
                                      'authorized_owned_delta': 26,
                                      'measured_engine_lines': 85544,
                                      'measured_owned_total': 161239,
                                      'measured_source_revision': 'ebc184bd0922e51438cb5a048324c0631df71a08',
                                      'measured_table_count': 45,
                                      'merged_dev_revision': 'dae4a86819d8c4ed6ec70d0d9349f3895f284c4d',
                                      'operator_decision': '6679052c',
                                      'operator_task': 'sharp-cascade-53',
                                      'pr_url': 'https://github.com/ElectricJack/agent-queue/pull/1861',
                                      'previous_engine_lines': 85544,
                                      'previous_owned_total': 161213,
                                      'reason': 'PR #1861 merge refresh: retain all dev grants '
                                                'and the historical latency grant; exact merged '
                                                'footprint pre-approved in supervisor reopen '
                                                'feedback under size-cap-delegation-2026-10-23. '
                                                'No additional headroom.',
                                      'referenced_decision': 'size-cap-delegation-2026-10-23',
                                      'task_id': 'bright-impact-14'},
 'dashboard_pr_snapshot_merge_measurement': {'authorized_engine_delta': 0,
                                             'authorized_owned_delta': 0,
                                             'dev_engine_lines': 85517,
                                             'dev_owned_total': 161102,
                                             'dev_table_count': 45,
                                             'grant_id': 'sharp-rapids-10-cap-epoch2-2026-10-23',
                                             'measured_engine_lines': 85517,
                                             'measured_owned_total': 161111,
                                             'measured_table_count': 45,
                                             'merged_dev_revision': '61869b7cdcf507b8e8acb596893bb63ac0794997',
                                             'prerequisite_engine_delta': 0,
                                             'prerequisite_owned_delta': 9,
                                             'prerequisite_task_id': 'sharp-rapids-10',
                                             'reason': 'Plain merge of origin/dev into accepted '
                                                       'PR 1889. Compose the existing '
                                                       'sharp-rapids-10 grant with dev; '
                                                       'fair-crest-95 contributes zero owned or '
                                                       'engine lines. Reopen instructions cite '
                                                       'size-cap-delegation-2026-10-23.',
                                             'referenced_decision': 'size-cap-delegation-2026-10-23',
                                             'source_revision': '3de48be2157c1b68414a77514c9250cb2b27d11d',
                                             'task_id': 'fair-crest-95'},
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
                          'msg-9f16388d39524c8b9a33d6796b88d1fb',
                          'msg-5c55b72faec1497391205876fc5dfeb9',
                          'msg-96fed6b8e86148f0bbf55ded244c9a0f',
                          'msg-76ff7843e72d451b9017a0b4db30c970',
                          'msg-c47a1a9a635a465b8a0cb286f93cb0b0',
                          'msg-c45ccf8341f946819758858c7bff488d',
                          'msg-625e460c4a7c456983e5eb114ea22446',
                          'msg-c80bde55a6d1442791a2ba4f9597d7a0',
                          'msg-49a7a4f4f8a5420eb80abf1d4d8b4a12',
                          'msg-2a11607018264013b97698493f53906e',
                          'msg-08efc39cceeb430cafb8636f1acb85f1',
                          'msg-1b59c9af50fd43418246593c54d85a16',
                          'msg-f06302bed6424579bdb6b51290cbed1e',
                          'msg-8f9cda4b9dcc460f8bcf006a22bfa2e0',
                          'msg-98d8517c56104e8fbf87e1330d2d5c1a',
                          'size-cap-delegation-2026-10-23',
                          'msg-000c812477994a65a62633a4a051224c',
                          'msg-d3de4e1b',
                          'msg-efc241b8ddbb4c1fb63a41f5fe3f0d90',
                          'msg-21f0bc97741343c39d69d7c5c7b97400'],
 'decision_record': 'record:2720bdbe-7521-427e-8e72-1f40a3ff9818',
 'delivery_method': 'operator direct integration; user request 2026-10-22',
 'delta': 6148,
 'expires_at': 's4_modules',
 'frozen_batch_composition': {'approved_deltas': {'noble-flare-12': {'engine': 22,
                                                                     'owned': 23,
                                                                     'shared': 1,
                                                                     'tables': 0},
                                                  'sound-forge-62': {'engine': 4,
                                                                     'owned': 26,
                                                                     'shared': 22,
                                                                     'tables': 0}},
                              'base_engine_lines': 85517,
                              'base_owned_total': 161113,
                              'base_table_count': 45,
                              'batch_id': 'train-5429c3c71859f327ff8b46dbf1edc557',
                              'member_sources': {'noble-flare-12': 'f09829d145915bc7426d24f28623283f7319a7a2',
                                                 'sound-forge-62': '15239e75971ae5ac39e6a7d0a5e467331ab58428'},
                              'reason': 'Compose both unchanged approved member grants on the '
                                        'retained starting head, which already includes '
                                        'fair-dune-41. Preserve every historical grant and '
                                        'measurement.',
                              'source_revision': '73e99672d2447c44ef6a71d43a0187f2d16630af',
                              'starting_revision': '48adaddad079a7adc2930e470590621c1575e674'},
 'measured_engine_lines': 85376,
 'measured_owned_total': 161647,
 'measured_source_revision': '9c830f717be72274fb8af1521941c52927af541b',
 'measured_table_count': 45,
 'merge_measurement': {'approval_message_id': 'msg-a10c7ab8ff2946f8b1f27528a407524c',
                       'authorized_engine_delta': 5,
                       'authorized_owned_delta': 5,
                       'decision_id': 'size-cap-delegation-2026-10-23',
                       'dev_engine_lines': 85316,
                       'dev_owned_total': 160879,
                       'dev_source_revision': '4e50c276fca68cbe92ba4fc704363ec6026db7dc',
                       'reason': 'Carry forward the unchanged approved fixer delta onto the '
                                 'measured dev tree.',
                       'task_id': 'fresh-apex-30'},
 'merge_remeasurement': {'authority': 'msg-00b0e08117cc479c9a4a0b36fc5a30d0',
                         'authorized_engine_delta': 65,
                         'authorized_owned_delta': 65,
                         'delegation_thread': 'size-cap-delegation-2026-10-23',
                         'measured_engine_lines': 85381,
                         'measured_owned_total': 160944,
                         'measured_source_revision': 'efd690243cecfafedfeaf81c829d86255cabd620',
                         'measured_table_count': 45,
                         'merged_dev_engine_lines': 85316,
                         'merged_dev_owned_total': 160879,
                         'merged_dev_revision': '4e50c276fca68cbe92ba4fc704363ec6026db7dc',
                         'merged_dev_table_count': 45,
                         'operator_decision': '6679052c',
                         'previous_measured_owned_total': 160944,
                         'previous_source_revision': '974e10dcf94ad95f98c14fbc3d7171072e644da2',
                         'reason': 'Plain merge of PR1877 preparation diagnostics from dev; '
                                   'combined totals remain unchanged and this task retains its '
                                   'approved 65-line delta. Project grant requires re-recording '
                                   'against the combined total when dev advances.',
                         'task_id': 'agile-beacon-80'},
 'original_owned_total_cap': 155499,
 'post_batch_composition': {'approved_deltas': {'sound-forge-62': {'engine': 4,
                                                                   'owned': 26,
                                                                   'shared': 22,
                                                                   'tables': 0}},
                            'base_engine_lines': 85517,
                            'base_owned_total': 161111,
                            'base_revision': '4a0c4e38ab979a1664586d47d16404b48def4089',
                            'decision_message_id': 'msg-9f16388d39524c8b9a33d6796b88d1fb',
                            'member_sources': {'sound-forge-62': '8e4e71b34b5c03b191b392a9d8f28176d177dd82'},
                            'source_revision': '1f0633050802438db6ef954c43d6d0eb2ce0edf3'},
 'post_release_structure_measurement': {'authorized_engine_delta': -554,
                                        'authorized_owned_delta': -38,
                                        'authorized_table_delta': 0,
                                        'dev_source_revision': 'c177d24e86d25fde7804131c94bfd637b0eb540a',
                                        'measured_engine_lines': 84989,
                                        'measured_groups': {'companions': 11168,
                                                            'engine': 84989,
                                                            'guards': 633,
                                                            'policy': 493,
                                                            'policy_sources': 1042,
                                                            'shared': 62537,
                                                            'ui': 262},
                                        'measured_owned_total': 161124,
                                        'measured_source_revision': '063b7620f635b34aeb376d1d9d4280dd22273b7d',
                                        'measured_table_count': 45,
                                        'operator_decision': '6679052c',
                                        'operator_task': 'sharp-cascade-53',
                                        'previous_engine_lines': 85543,
                                        'previous_owned_total': 161162,
                                        'previous_source_revision': '73e99672d2447c44ef6a71d43a0187f2d16630af',
                                        'previous_table_count': 45,
                                        'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/1775',
                                        'reason': 'Exact combined-tree remeasurement after '
                                                  'merging PR #1775 head with dev. Approval '
                                                  'pre-granted by size-cap-delegation-2026-10-23, '
                                                  'operator decision 6679052c on '
                                                  'sharp-cascade-53. Preserve ownership '
                                                  'classifications and all historical grants; no '
                                                  'production code moved or trimmed for the '
                                                  'count.',
                                        'referenced_decision': 'size-cap-delegation-2026-10-23',
                                        'required_pr_head': '14183dc9d82cdc4cb32610827e228b49b6e488d2',
                                        'task_id': 'swift-nexus-38'},
 'predecessors': [{'batch_id': 'train-77f0d8e451f6aed08d608e443031047a',
                   'decision_message_id': 'msg-edb5f857de344716a38857406fa69958',
                   'decision_record': 'record:2720bdbe-7521-427e-8e72-1f40a3ff9818',
                   'delta': 1293,
                   'expires_at': 's4_modules',
                   'measured_owned_total': 156792,
                   'measured_source_revision': 'd00e25221856145f24777f8b016a008b4e954bca',
                   'original_owned_total_cap': 155499,
                   'restore_task': 'azure-ridge-72'},
                  {'authorized_delta': 2166,
                   'batch_id': 'train-48d0a1c1f1f8292b2977546011609db6',
                   'measured_owned_total': 158958,
                   'measured_source_revision': 'cbfd455b0e9f053854a2c1becfa1efaeef231582'}],
 'restart_hold_repair_merge_measurement': {'authorized_engine_delta': 0,
                                           'authorized_owned_delta': 19,
                                           'authorized_shared_delta': 19,
                                           'authorized_table_delta': 0,
                                           'decision_message_id': 'msg-76ff7843e72d451b9017a0b4db30c970',
                                           'measured_engine_lines': 85010,
                                           'measured_owned_total': 161217,
                                           'measured_source_revision': 'a36c389c39a33ff60bffa98cc019591298bdcfa9',
                                           'measured_table_count': 45,
                                           'operator_decision': '6679052c',
                                           'operator_task': 'sharp-cascade-53',
                                           'previous_engine_lines': 85010,
                                           'previous_measurement_revision': '4bafa45f1ac0a466eab372c7b6b0b866f67bb832',
                                           'previous_owned_total': 161198,
                                           'previous_recorded_owned_cap': 161213,
                                           'previous_source_revision': 'd60c0f238c8205f495a384c0bd77c322a8fc89ac',
                                           'previous_table_count': 45,
                                           'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/1900',
                                           'reason': 'Compose the unchanged approved +19 restart '
                                                     'hold repair grant on current dev for PR '
                                                     '#1900. Preserve every historical grant and '
                                                     'frozen batch measurement.',
                                           'referenced_decision': 'size-cap-delegation-2026-10-23',
                                           'root_task_id': 'prime-crest-79',
                                           'task_id': 'fair-flare-10'},
 'restore_task': 'azure-ridge-72',
 'resume_batch_composition': {'approved_deltas': {'calm-rapids-50': {'engine': 0,
                                                                     'owned': 50,
                                                                     'tables': 0},
                                                  'fair-crest-95': {'engine': 0,
                                                                    'owned': 0,
                                                                    'tables': 0},
                                                  'fair-harbor-71': {'engine': 1,
                                                                     'owned': 1,
                                                                     'tables': 0},
                                                  'noble-apex-40': {'engine': 0,
                                                                    'owned': 0,
                                                                    'tables': 0},
                                                  'sharp-vault-27': {'engine': 0,
                                                                     'owned': 0,
                                                                     'tables': 0}},
                              'base_engine_lines': 85543,
                              'base_owned_total': 161162,
                              'base_table_count': 45,
                              'batch_id': 'train-9394fdec634bb068ee8e58c1008dd4b2-resume-1e142f1858d8',
                              'member_sources': {'calm-rapids-50': '45626ba27ff1c7711f5aa17893f7ce997928da5f',
                                                 'fair-crest-95': '63163a7d4fd4aa5611c18ae139580902abf32770',
                                                 'fair-harbor-71': '4bb9221436187f5c9c747531109749b503600e65',
                                                 'noble-apex-40': '7f87074a2a463074595e0b71018fda7f33fecb47',
                                                 'sharp-vault-27': 'c5e891f5ae3b2987977d0e2bb0268631154a8421'},
                              'reason': 'Compose the unchanged resume and shared-check grants on '
                                        'the retained starting head; the other members add no '
                                        'owned lines. Preserve every historical grant and batch '
                                        'measurement.',
                              'source_revision': '4bafa45f1ac0a466eab372c7b6b0b866f67bb832',
                              'starting_revision': 'c177d24e86d25fde7804131c94bfd637b0eb540a'},
 'storage_categories': ['integration_close_attempts'],
 'supplemental_grants': [{'approval_message_id': 'msg-4e25c0d3',
                          'authorized_delta': 5,
                          'measured_owned_total': 160825,
                          'measured_source_revision': 'd9c50555a1d87beaefbfc8de3b13229a6ab9f061',
                          'prior_owned_total_cap': 160820,
                          'reason': 'Verify registered linked fixer worktrees before permission '
                                    'bypass.',
                          'relay_message_id': 'msg-bd98955a2fa443cc920ce725e42a5c3f',
                          'task_id': 'fresh-apex-30'}],
 'verification_task': 'stark-willow-95.18'}
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


# Separate operator-approved reconciliation; historical exceptions above remain retired.
HAND_INTEGRATION_RECONCILIATION = {'deleted_epic_retained_selection_composition': {'authorized_engine_delta': 16,
 'authorized_owned_delta': 16,
 'authorized_table_delta': 0,
 'charged_files': {'src/integration/legacy_backfill.py': 16},
 'decision_message_id': 'msg-9b501b0a8c2f4c258c07a43cb924a8da',
 'dev_source_revision': '7bff0f25e71c4502b9f8cd9266fb047f0f6cc3fc',
 'measured_engine_lines': 83434,
 'measured_owned_total': 158864,
 'measured_table_count': 45,
 'operator_decision': '6679052c',
 'previous_measured_engine_lines': 83418,
 'previous_measured_owned_total': 158848,
 'previous_measured_table_count': 45,
 'reason': 'Exact supervisor-approved +16 owned/engine lines for authoritative deleted-parent '
           'selection and default-target reproof of retained child completions, including retired '
           'origins. Compose on current dev; preserve all historical grants, original baselines '
           'and ownership classifications, zero remaining headroom and no new storage category.',
 'referenced_decision': 'size-cap-delegation-2026-10-23',
 'task_id': 'stark-rapids-81'},
 'deleted_epic_child_recovery_composition': {'task_id': 'stark-rapids-81',
 'decision_message_id': 'msg-18a65dbb43bf4491ae77cc71bfd931a8',
 'referenced_decision': 'size-cap-delegation-2026-10-23',
 'operator_decision': '6679052c',
 'dev_source_revision': '389f345d17ce86fd52f4e4e7aace9fcd35c0a7eb',
 'previous_measured_engine_lines': 83221,
 'previous_measured_owned_total': 158609,
 'previous_measured_table_count': 45,
 'authorized_engine_delta': 103,
 'authorized_owned_delta': 103,
 'authorized_table_delta': 0,
 'measured_engine_lines': 83324,
 'measured_owned_total': 158712,
 'measured_table_count': 45,
 'charged_files': {'src/integration/delivery_branches.py': 76,
                   'src/integration/delivery_truth.py': 1,
                   'src/integration/legacy_backfill.py': 26},
 'reason': 'Exact supervisor pre-approved delta on current dev for strict final-source recovery of '
           'deleted-epic children and a generation-fenced retained-witness deletion hold. Preserve '
           'all historical grants, baselines and ownership classifications; zero remaining '
           'headroom and no new storage category.'},
 'reconciliation_noop_composition': {'authorized_engine_delta': 20,
 'authorized_owned_delta': 20,
 'authorized_table_delta': 0,
 'charged_files': {'src/integration/repository_recovery.py': 0,
                   'src/integration/truth_reconciler.py': 20},
 'decision_message_id': 'msg-8aa9311e7afa474193d720e4a9cab95b',
 'dev_source_revision': 'ea0cb2e29710cd1b243a19e4f1292e22a3d5f980',
 'measured_engine_lines': 83183,
 'measured_owned_total': 158571,
 'measured_table_count': 45,
 'operator_decision': '6679052c',
 'previous_measured_engine_lines': 83163,
 'previous_measured_owned_total': 158551,
 'previous_measured_table_count': 45,
 'reason': 'Exact supervisor-approved receipt/projection no-op probe and restored incremental '
           'acquisition after a recovered incident journal. Remeasured on merged dev under the '
           'same decision. Preserve all mutation freshness, identity, writer and '
           'retained-integrity gates, every historical grant and ownership classification; '
           'zero remaining headroom and no new storage category.',
 'referenced_decision': 'size-cap-delegation-2026-10-23',
 'task_id': 'bright-torrent-86'},
 'authority': 'size-cap-delegation-2026-10-23',
 'authorized_owned_delta': 3339,
 'tick_snapshot_sharing_composition': {'authorized_engine_delta': 101,
 'authorized_owned_delta': 116,
 'authorized_shared_owned_delta': 15,
 'authorized_table_delta': 0,
 'charged_files': {'src/git/manager.py::GitManager.als_remote_snapshot_refs': 15,
                   'src/integration/delivery_truth.py': 1,
                   'src/integration/git_truth.py': 58,
                   'src/integration/repository_recovery.py': 29,
                   'src/integration/train.py': 6,
                   'src/integration/train_sources.py': 2,
                   'src/integration/truth_reconciler.py': 5},
 'decision_message_id': 'msg-f4efa032bcda45afa490a6e9a7f10e17',
 'dev_source_revision': 'ddf8b118329f8cd301981c516bbb79e1e8cfa44f',
 'measured_engine_lines': 82877,
 'measured_owned_total': 158193,
 'measured_table_count': 45,
 'operator_decision': '6679052c',
 'prerequisite_revision': 'fe04588c6b4aac2fbc7afacc7d9746fb95682e83',
 'previous_measured_engine_lines': 82776,
 'previous_measured_owned_total': 158077,
 'previous_measured_table_count': 45,
 'reason': 'Exact supervisor-approved tick-scoped repository snapshot sharing, integrity-gated '
           'no-change remote-ref probe and per-visit diagnostics, including the '
           'fresh-journey-57 prerequisite merge. Preserve historical baselines, grants and '
           'ownership classifications; zero remaining headroom and no new storage category.',
 'referenced_decision': 'size-cap-delegation-2026-10-23',
 'request_message_id': 'msg-d838754c89fc435b99e77211e8914a9d',
 'task_id': 'sound-impact-32'},
 'fresh_continuation_sentinel_composition': {'authorized_engine_delta': 0,
 'authorized_owned_delta': 15,
 'authorized_shared_owned_delta': 15,
 'authorized_table_delta': 0,
 'charged_files': {'src/wait_runtime/resume.py': 15},
 'decision_message_id': 'msg-d4963b0d021949979babe990b4d8fe31',
 'dev_source_revision': '94f4c82ee1898298711498c93ca90cb459816db6',
 'measured_engine_lines': 82532,
 'measured_owned_total': 157808,
 'measured_table_count': 45,
 'operator_decision': '6679052c',
 'previous_measured_engine_lines': 82532,
 'previous_measured_owned_total': 157793,
 'previous_measured_table_count': 45,
 'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/2008',
 'reason': 'Exact supervisor-approved +15 shared owned lines for the fresh-continuation '
           'workspace acquisition receipt before spawn. Sentinel stamping and prime/ack '
           'guidance remain intact. Preserve original baselines, every historical grant and '
           'ownership classification, with zero remaining headroom and no new storage '
           'category.',
 'referenced_decision': 'size-cap-delegation-2026-10-23',
 'source_revision': '490a0b1d82633cf41a071af421726fd9842c9d78',
 'task_id': 'wise-cascade-80',
 'thread_id': 'wise-cascade-80-ci-size'},
 'claim_marker_recovery_composition': {
     'authorized_engine_delta': 0,
     'authorized_owned_delta': 5,
     'authorized_shared_owned_delta': 5,
     'authorized_table_delta': 0,
     'decision_message_id': 'msg-3cec5c343e89461790d7371a8ac0111c',
     'dev_source_revision': 'b4d6931c79aa04b0e50edd50ed0be6dde1ca31e9',
     'operator_decision': '6679052c',
     'previous_measured_engine_lines': 82443,
     'previous_measured_owned_total': 157544,
     'previous_measured_table_count': 45,
     'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/1929',
     'reason': 'Exact approved +5 shared owned lines for missing claim-marker recovery. '
               'Remeasured after merging current dev; preserve original baselines, all '
               'historical grants and ownership classifications, with zero remaining headroom.',
     'referenced_decision': 'size-cap-delegation-2026-10-23',
     'source_revision': 'a10b7d957d2fb0030e396097fee73b5a431fda05',
     'supervisor_decision_comment_id': 'comment-ccb6fb19a24f447c97da075bc800a9a2',
     'task_id': 'wise-crest-14',
     'thread_id': 'wise-crest-14-ci-size'},
 'legacy_abandon_keep_branch_composition': {'task_id': 'eager-grove-95',
     'authorized_owned_delta': 10,
     'authorized_engine_delta': 10,
     'authorized_table_delta': 0,
     'charged_files': {'src/integration/legacy_backfill.py': 10},
     'decision_message_id': 'msg-42d72c76b0464cf79d519048e57985ac',
     'referenced_decision': 'size-cap-delegation-2026-10-23',
     'operator_decision': '6679052c',
     'previous_measured_owned_total': 157793,
     'previous_measured_engine_lines': 82532,
     'previous_measured_table_count': 45,
     'dev_source_revision': '00b7a51cf',
     'source_revision': '92326912dcff1bed5ca8bd5e7ab7c4f587ec9f5d',
     'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/1990',
     'thread_id': 'eager-grove-95-ci-size-epoch4',
     'reason': 'Exact supervisor-approved +10 owned/engine lines for explicit keep-branch legacy '
               'source abandonment after merging current dev. Preserve all historical grants, original '
               'baselines and ownership classifications, with zero remaining headroom and no new '
               'tables or storage category.'},
 'expires_at': 's4_modules',
 'held_ingestion_composition': {'authorized_engine_delta': 0,
                                'authorized_owned_delta': 14,
                                'authorized_table_delta': 0,
                                'dev_source_revision': '1048b9aff3350df8e72361252fbb61be4ea62e60',
                                'operator_decision': '6679052c',
                                'previous_measured_engine_lines': 82420,
                                'previous_measured_owned_total': 157507,
                                'previous_measured_table_count': 45,
                                'published_source_revision': '8e4496f7763808591ec8db0c79b4591b263829d0',
                                'pull_request': 1918,
                                'reason': 'Exact ancestry-preserving composition of the '
                                          'held-ingestion children with repaired dev, explicitly '
                                          'preapproved in bright-quest-30 under operator decision '
                                          '6679052c. Preserve all historical evidence and original '
                                          'caps; no new headroom or storage category.',
                                'referenced_decision': 'size-cap-delegation-2026-10-23',
                                'task_id': 'bright-quest-30'},
 'held_ingestion_dev_merge_composition': {'authorized_engine_delta': 0,
                                          'authorized_owned_delta': 14,
                                          'authorized_table_delta': 0,
                                          'dev_source_revision': '6cb338192d569c4df75539e5848fd32be05ac980',
                                          'operator_decision': '6679052c',
                                          'previous_measured_engine_lines': 82443,
                                          'previous_measured_owned_total': 157530,
                                          'previous_measured_table_count': 45,
                                          'published_source_revision': '67341d693cd448e6eca98392a3a4b9104bf2e449',
                                          'pull_request': 1918,
                                          'reason': 'Exact ancestry-preserving composition of the '
                                                    'held-ingestion children with current dev and '
                                                    'its approved large-session prompt amendment, '
                                                    'preapproved in bright-quest-30. Preserve both '
                                                    'original composition records, all historical '
                                                    'evidence and original caps; zero remaining '
                                                    'headroom and no new storage category.',
                                          'referenced_decision': 'size-cap-delegation-2026-10-23',
                                          'task_id': 'bright-quest-30'},
 'historical_root_pr_diagnostics_composition': {'authorized_engine_delta': 86,
  'authorized_owned_delta': 86,
  'authorized_table_delta': 0,
  'decision_message_id': 'msg-1da1b007351f478493b5681519dcc45c',
  'dev_source_revision': '5d995a8378e53acd797b19e5b8d0fb2263156c2a',
  'operator_decision': '6679052c',
  'previous_measured_engine_lines': 82532,
  'previous_measured_owned_total': 157785,
  'previous_measured_table_count': 45,
  'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/1991',
  'reason': 'Exact supervisor-approved +86 owned/engine lines for historical root PR '
            'diagnostics in the existing root_pull_requests receipt owner. Preserve all '
            'historical grants, original baselines and ownership classifications, with zero '
            'remaining headroom and no new storage category.',
  'referenced_decision': 'size-cap-delegation-2026-10-23',
  'source_revision': '0a81ecabe1d1dcdfd22ac7a0d75187c8bf406702',
  'task_id': 'bold-delta-18',
  'thread_id': 'bold-delta-18-ci-contracts'},
 'initial_inventory': '/tmp/smart-nexus-83-live-inventory.json',
 'inventory_sha256': '87c9b5ab3d968137db1e136be1ee523790bbef710bc6ef8124c90cb2d345e416',
 'large_session_prompt_composition': {'authorized_engine_delta': 23,
                                      'authorized_owned_delta': 23,
                                      'authorized_table_delta': 0,
                                      'charged_file': 'src/integration/fixer_runtime.py',
                                      'decision_message_id': 'msg-06bb3b1643ef4298a715c0e55fa317b9',
                                      'previous_measured_engine_lines': 82420,
                                      'previous_measured_owned_total': 157507,
                                      'previous_source_revision': '1048b9aff3350df8e72361252fbb61be4ea62e60',
                                      'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/1919',
                                      'reason': 'Compose the previously approved exact +23 '
                                                'owned/engine lines on current dev after the '
                                                'supervisor-directed merge. Preserve all '
                                                'historical grants, ownership classifications and '
                                                'zero remaining headroom; no new tables or storage '
                                                'category.',
                                      'source_revision': '5e33516f93715b484eb15c2525d050ac303d5ee4',
                                      'task_id': 'swift-lantern-21'},
 'legacy_bookkeeping_refresh_composition': {
     'authorized_engine_delta': 24,
     'authorized_owned_delta': 24,
     'authorized_table_delta': 0,
     'charged_file': 'src/integration/bookkeeping_guard.py',
     'decision_message_id': 'msg-9de2c6e7e51249babbc2e680b41d185d',
     'dev_source_revision': '2378cda7eda6ba241bae7a9ab43c70111607199b',
     'operator_decision': '6679052c',
     'previous_measured_engine_lines': 82443,
     'previous_measured_owned_total': 157549,
     'previous_measured_table_count': 45,
     'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/1935',
     'reason': 'Exact approved +24 owned and engine lines for inherited legacy bookkeeping '
               'evidence. Includes the already landed wise-crest-14 grant; preserves original '
               'baselines, historical grants and ownership classifications, with zero '
               'remaining headroom and no new storage category.',
     'referenced_decision': 'size-cap-delegation-2026-10-23',
     'source_revision': '8ecfb8e7e384db46a6b203141165c033e58c02e0',
     'task_id': 'solid-dune-53',
     'thread_id': 'solid-dune-53-size-amendment'},
 'legacy_epic_delivery_composition': {'authorized_engine_delta': 37,
     'authorized_owned_delta': 37,
     'authorized_table_delta': 0,
     'charged_files': {'src/integration/stacked_branches.py': 8,
                       'src/integration/train_sources.py': 29},
     'decision_message_id': 'msg-83c55ca50e03451b85cc6040d1e3ce04',
     'operator_decision': '6679052c',
     'previous_measured_engine_lines': 82443,
     'previous_measured_owned_total': 157549,
     'previous_measured_table_count': 45,
     'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/1934',
     'reason': 'Exact supervisor-approved +37 owned/engine lines for current passing legacy epic '
               'Git delivery proof and redundant refresh suppression. Preserve original '
               'baselines, historical grants and classifications; zero remaining headroom and no '
               'new storage category.',
     'referenced_decision': 'size-cap-delegation-2026-10-23',
     'source_revision': '9ee11420309902aafda1d7a81f93866df50896af',
     'task_id': 'keen-harbor-84'},
 'measured_engine_lines': 83434,
 'measured_owned_total': 158838,
 'measured_table_count': 45,
 'operator_decision': '6679052c',
 'original_owned_total_cap': 155499,
 'reason': "Separate exact reconciliation of the operator's 2026-10-23 hand integration of "
           'approximately 25 individually approved PRs, A5 bookkeeping shards and PR 1780 '
           'retirement. Preserves the original baseline, every historical grant and retired '
           "exception; includes this task's check/refusal composition fix. No automatic cap growth "
           'or new storage category.',
 'reconciled_inventory': '/tmp/smart-nexus-83-final-inventory.json',
 'remaining_headroom': 0,
 'project_schedule_composition': {
     'authorized_engine_delta': 0,
     'authorized_owned_delta': 101,
     'authorized_shared_owned_delta': 101,
     'authorized_table_delta': 0,
     'decision_message_id': 'msg-3f67bea5611149e99abb6ae53a48aae7',
     'dev_source_revision': 'b0bb1645a81b14b594192d6a75e80f6c4fdd3b4f',
     'operator_decision': '6679052c',
     'previous_measured_engine_lines': 82504,
     'previous_measured_owned_total': 157610,
     'previous_measured_table_count': 45,
     'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/1945',
     'reason': 'Exact supervisor-approved +101 counted shared owned lines for project '
               'schedules and their legacy-import exclusion. Count register_schedule_contracts '
               'without a new ownership exclusion; preserve original baselines, historical '
               'grants and classifications, with zero remaining headroom.',
     'referenced_decision': 'size-cap-delegation-2026-10-23',
     'source_revision': 'e906bfe0b601a4cd7c78ac9bd079bb7f5b43ef39',
     'supersedes_decision_message_id': 'msg-8a308cadb20142e38c2692c762734e85',
     'task_id': 'swift-forge-67',
     'thread_id': 'swift-forge-67-ci-size'},
 'review_followup_composition': {'authorized_engine_delta': 0,
     'authorized_owned_delta': 4,
     'authorized_shared_owned_delta': 4,
     'authorized_table_delta': 0,
     'charged_file': 'src/commands/session_commands.py',
     'dev_source_revision': 'b4171631c2780f3ca52132324266a31fb1065be4',
     'operator_decision': '6679052c',
     'previous_measured_engine_lines': 82504,
     'previous_measured_owned_total': 157711,
     'previous_measured_table_count': 45,
     'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/1921',
     'reason': 'Exact supervisor-approved +4 shared owned lines for the post-approval follow-up '
               'task close guard, authorized in amber-journey-18 reopen feedback at 2026-10-24 '
               '20:58Z. Preserve original baselines, historical grants and ownership '
               'classifications, with zero remaining headroom and no new storage category.',
     'referenced_decision': 'size-cap-delegation-2026-10-23',
     'source_revision': '9f4d979c54865bdaf06d85c036796f64b803991c',
     'task_id': 'amber-journey-18'},
 'wait_listener_fence_inherited_composition': {'authorized_engine_delta': 0,
      'authorized_owned_delta': 3,
      'authorized_shared_owned_delta': 3,
      'authorized_table_delta': 0,
      'charged_files': {'src/wait_runtime/resume.py': 3},
      'dev_source_revision': 'c08d949a113a04b4160100655f70ff907edd04c6',
      'operator_decision': '6679052c',
      'previous_measured_engine_lines': 82504,
      'previous_measured_owned_total': 157718,
      'previous_measured_table_count': 45,
      'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/1957',
      'reason': 'Exact supervisor-approved +3 shared owned lines from keen-dune-16 / PR 1957 '
                '(wait listener fence release and startup resume recovery), hand-landed on dev '
                'without re-recording; re-recorded while hand-landing PR 1921. Preserve the '
                'separately approved PR 1921 +4 and PR 1955 +3 compositions, original baselines, '
                'historical grants and ownership classifications, with zero remaining headroom '
                'and no new storage category.',
      'referenced_decision': 'size-cap-delegation-2026-10-23',
      'repair_task_id': 'amber-journey-18',
      'source_revision': '42cc80377e3ca9d0bba7658d4afe958332a74e2d',
      'task_id': 'keen-dune-16'},
 'wait_resume_inherited_composition': {'authorized_engine_delta': 0,
      'authorized_owned_delta': 3,
      'authorized_shared_owned_delta': 3,
      'authorized_table_delta': 0,
      'charged_file': 'src/wait_runtime/resume.py',
      'decision_message_id': 'msg-fb9bbab03e48428d866eb4d239bc8ef6',
      'dev_source_revision': 'ab14a935bc4ca9402fdbd519b651b1847943584d',
      'operator_decision': '6679052c',
      'previous_measured_engine_lines': 82504,
      'previous_measured_owned_total': 157715,
      'previous_measured_table_count': 45,
      'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/1955',
      'reason': 'Exact supervisor-approved +3 inherited shared owned lines from fleet-zenith-14 / '
                'PR 1955 after merging current dev into PR 1921. Preserve the separately approved '
                '+4 review-followup composition, original baselines, historical grants and '
                'ownership classifications, with zero remaining headroom and no new storage '
                'category.',
      'referenced_decision': 'size-cap-delegation-2026-10-23',
      'repair_task_id': 'amber-journey-18',
      'task_id': 'fleet-zenith-14',
      'thread_id': 'amber-journey-18-epoch4-size'},
 'wait_admission_reobservation_composition': {'authorized_engine_delta': 0,
     'authorized_owned_delta': 29,
     'authorized_shared_owned_delta': 29,
     'authorized_table_delta': 0,
     'charged_files': {'src/waits/admission.py:AdmissionReader.observe_task': 29},
     'decision_message_id': 'msg-093cb31dec41486ba5891e1b8bb20c7d',
     'dev_source_revision': 'cc66aec8460d14d67473c1d67f876717fd65a5b6',
     'operator_decision': '6679052c',
     'previous_measured_engine_lines': 82504,
     'previous_measured_owned_total': 157727,
     'previous_measured_table_count': 45,
     'reason': 'Exact supervisor-approved +29 shared owned lines for constraint-specific admission '
               'reobservation, reducing redundant work inside graph-fenced transactions. Preserve '
               'original baselines, historical grants and ownership classifications, with zero '
               'remaining headroom and no new storage category.',
     'referenced_decision': 'size-cap-delegation-2026-10-23',
     'supervisor_decision_comment_id': 'comment-26bf155ce932484188a12ac8776616fd',
     'task_id': 'sharp-delta-65'},
 'wait_source_listener_backlog_composition': {'authorized_engine_delta': 20,
      'authorized_owned_delta': 20,
      'authorized_table_delta': 0,
      'charged_files': {'src/integration/checks.py': 20},
      'dev_source_revision': '42e216ff1d6dd192459ab3eb46dfb837c912568c',
      'operator_decision': '6679052c',
      'previous_measured_engine_lines': 82504,
      'previous_measured_owned_total': 157756,
      'previous_measured_table_count': 45,
      'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/1974',
      'reason': 'Exact supervisor-approved +20 owned/engine lines from stark-horizon-78 / PR 1974 '
                '(wait source listener backlog fix), hand-landed on dev by the supervisor. Preserve '
                'every separately approved composition, original baselines, historical grants and '
                'ownership classifications, with zero remaining headroom and no new storage category.',
      'referenced_decision': 'size-cap-delegation-2026-10-23',
      'source_revision': '93c9d12a6f87dbd119b9736279023b73500f68c1',
      'task_id': 'stark-horizon-78'},
 'wait_dispatch_unobservable_keep_composition': {     'authorized_engine_delta': 3,
      'authorized_owned_delta': 3,
      'authorized_table_delta': 0,
      'charged_files': {'src/integration/dispatch_waits.py': 3},
      'dev_source_revision': '677da30953d618601b049f4b4eb78c48d8269098',
      'operator_decision': '6679052c',
      'previous_measured_engine_lines': 82524,
      'previous_measured_owned_total': 157777,
      'previous_measured_table_count': 45,
      'reason': 'Exact supervisor-approved +3 engine/owned lines: a train visit keeps an '
                'unobservable stale CI wait instead of aborting the whole dev-lane visit, '
                'hand-landed on dev by the global supervisor 2026-10-25. Zero remaining headroom.',
      'referenced_decision': 'size-cap-delegation-2026-10-23',
      'source_revision': '677da30953d618601b049f4b4eb78c48d8269098',
      'task_id': 'supervisor-global'},
 'wait_ci_observation_retry_composition': {     'authorized_engine_delta': 5,
      'authorized_owned_delta': 5,
      'authorized_table_delta': 0,
      'charged_files': {'src/waits/listener.py': 5},
      'dev_source_revision': '1b65b85de2ca2a0b9f7ac13a8841e76c558ace56',
      'operator_decision': '6679052c',
      'previous_measured_engine_lines': 82527,
      'previous_measured_owned_total': 157780,
      'previous_measured_table_count': 45,
      'reason': 'Exact supervisor-approved +5 engine/owned lines: fresh-glacier-53 retries skipped '
                'CI source observations in the wait listener instead of discarding them, '
                'hand-landed on dev by the global supervisor 2026-10-25 while the train was '
                'starved. Zero remaining headroom.',
      'referenced_decision': 'size-cap-delegation-2026-10-23',
      'source_revision': '1b65b85de2ca2a0b9f7ac13a8841e76c558ace56',
      'task_id': 'fresh-glacier-53'},
 'reconciler_resume_retain_composition': {     'authorized_engine_delta': 0,
      'authorized_owned_delta': 8,
      'authorized_table_delta': 0,
      'charged_files': {'src/sessions/reconciler.py': 8},
      'dev_source_revision': '4f713c63b9f6dce0e28df7a460814049b23e7b0f',
      'operator_decision': '6679052c',
      'previous_measured_engine_lines': 82532,
      'previous_measured_owned_total': 157785,
      'previous_measured_table_count': 45,
      'reason': 'Exact supervisor-approved +8 owned lines: azure-zenith-14 keeps the session '
                'reconciler from draining a RESUME_PENDING continuation session, hand-landed on '
                'dev by the global supervisor 2026-10-25 while the train was starved. Zero '
                'remaining headroom.',
      'referenced_decision': 'size-cap-delegation-2026-10-23',
      'source_revision': 'f7dcc0f757c11bb4c14f66eadb32bb3cf5c5c315',
      'task_id': 'azure-zenith-14'},
 'wait_ci_timeout_backoff_composition': {'authorized_engine_delta': 0,
      'authorized_owned_delta': 1,
      'authorized_table_delta': 0,
      'charged_files': {'src/waits/watchers/ci.py': 1},
      'dev_source_revision': 'c413b548b78926032681cb99b410451f288a2bfe',
      'operator_decision': '6679052c',
      'previous_measured_engine_lines': 82524,
      'previous_measured_owned_total': 157776,
      'previous_measured_table_count': 45,
      'reason': 'Exact supervisor-approved +1 owned line: CI watcher backs off after an observation '
                'timeout so the wait listener drain does not pay a 5s timeout per CI event, hand-landed '
                'on dev by the global supervisor 2026-10-25. Zero remaining headroom.',
      'referenced_decision': 'size-cap-delegation-2026-10-23',
      'source_revision': 'c413b548b78926032681cb99b410451f288a2bfe',
      'task_id': 'supervisor-global'},
 'wait_scheduler_sync_rollback_composition': {'authorized_engine_delta': 0,
      'authorized_owned_delta': 6,
      'authorized_shared_owned_delta': 6,
      'authorized_table_delta': 0,
      'charged_files': {'src/wait_runtime/scheduler.py': 6},
      'dev_source_revision': '286269b134d961c1a13c921ae82aa4db8e580f47',
      'operator_decision': '6679052c',
      'previous_measured_engine_lines': 82504,
      'previous_measured_owned_total': 157721,
      'previous_measured_table_count': 45,
      'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/1963',
      'reason': 'Exact supervisor-approved +6 shared owned lines from brisk-impact-45 / PR 1963 '
                '(scheduler_wait_sync rollback after timeout so an invalidated transaction cannot '
                'wedge the wait runtime), hand-landed on dev by the supervisor. Preserve the '
                'separately approved PR 1921 +4, PR 1955 +3 and PR 1957 +3 compositions, original '
                'baselines, historical grants and ownership classifications, with zero remaining '
                'headroom and no new storage category.',
      'referenced_decision': 'size-cap-delegation-2026-10-23',
      'source_revision': '8a7897370c06707e4920856754c47fe9ed2846b9',
      'task_id': 'brisk-impact-45'},
 'source_ci_repair_composition': {'authorized_engine_delta': 46,
                                  'authorized_owned_delta': 46,
                                  'merged_pull_request': 1917,
                                  'merged_source_revision': 'ccd4d9a095bbe5f433bdec5af2192786f0640096',
                                  'merged_task_id': 'nimble-willow-51',
                                  'previous_measured_engine_lines': 82374,
                                  'previous_measured_owned_total': 157461,
                                  'reason': 'Exact combined-tree remeasurement under the existing '
                                            'size-cap delegation after the supervisor-authorized '
                                            'ancestry-preserving merge of PR 1917. No new headroom '
                                            'or storage category.',
                                  'source_revision': '66f21e348',
                                  'supervisor_decision_message_id': 'msg-09ca31f5246b46af8c7d4bcd1602faec'},
 'source_revision': 'f564f9e2e76e5a9cf76a9690d26f1d550fb3bbc0',
 'supervisor_decision_message_id': 'msg-2077aa41ec2c4f8685869fa59596627b',
 'task_id': 'smart-nexus-83',
 'admission_diagnostics_composition': {'authorized_engine_delta': 107,
     'authorized_owned_delta': 107,
     'authorized_table_delta': 0,
     'charged_files': {'src/integration/admission.py': 54, 'src/integration/pr_admission.py': 53},
     'combined_source_revision': '2f4e9d248bdff2286d9f8cdd90dc8e7c75ff94d9',
     'decision_message_id': 'msg-290fb7b068ab47dbb5901f118a73e069',
     'dev_source_revision': '94f4c82ee1898298711498c93ca90cb459816db6',
     'operator_decision': '6679052c',
     'previous_measured_engine_lines': 82532,
     'previous_measured_owned_total': 157793,
     'previous_measured_table_count': 45,
     'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/2006',
     'reason': 'Exact supervisor-approved +107 engine/owned lines for per-candidate admission '
               'comparison, PR freshness and lock diagnostics. Compose the unchanged '
               'implementation with current dev; preserve original baselines, every historical '
               'grant and ownership classification, zero remaining headroom and no new storage '
               'category.',
     'referenced_decision': 'size-cap-delegation-2026-10-23',
     'source_revision': '6951e0fae10722f912b78c66664b2d700fbc36f5',
     'supervisor_grant_reference': 'msg-2d36d0eb',
     'task_id': 'eager-stone-76',
     'thread_id': 'eager-stone-76-ci-size'},
 'retained_fixer_authority_composition': {'task_id': 'noble-harbor-10',
 'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/2025',
 'referenced_decision': 'size-cap-delegation-2026-10-23',
 'operator_decision': '6679052c',
 'decision_message_id': 'msg-3472013282e44ae0810b733694d356fe',
 'thread_id': 'noble-harbor-10-ci-size',
 'source_revision': '02e73e0b72d1e281050abd77bd14becc950714c5',
 'dev_source_revision': 'aa92c4b8b2726f08f37af6fcec326e47301e7b34',
 'authorized_owned_delta': 124,
 'authorized_engine_delta': 78,
 'authorized_shared_owned_delta': 46,
 'authorized_table_delta': 0,
 'charged_files': {'src/integration/batches.py': 8,
                   'src/integration/fixer_runtime.py': 54,
                   'src/integration/train_sources.py': 16,
                   'src/commands/integration_commands.py': 27,
                   'src/cli/integration.py': 15,
                   'src/commands/contracts/integration.py': 3,
                   'src/api/scope.py': 1},
 'previous_measured_owned_total': 157925,
 'previous_measured_engine_lines': 82649,
 'previous_measured_table_count': 45,
 'measured_owned_total': 158049,
 'measured_engine_lines': 82727,
 'measured_table_count': 45,
 'reason': 'Exact supervisor-approved footprint for retained-fixer publication recovery after '
           'daemon restart and the owning-supervisor authority control. Preserve all '
           'historical grants, original baselines and ownership classifications; zero '
           'remaining headroom and no new storage category.'},
 'retained_fixer_authority_merge_measurement': {'task_id': 'noble-harbor-10',
 'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/2025',
 'referenced_decision': 'size-cap-delegation-2026-10-23',
 'operator_decision': '6679052c',
 'decision_message_id': 'msg-de48ec376521488e81ab42017fe4f0d6',
 'original_decision_message_id': 'msg-3472013282e44ae0810b733694d356fe',
 'source_revision': 'b5b1db9bbe32703f855b95bb051647b8b66e94f4',
 'dev_source_revision': '5ff637f73bcb2a990e72035d8c5415c657b0a8a3',
 'authorized_owned_delta': 124,
 'authorized_engine_delta': 78,
 'authorized_shared_owned_delta': 46,
 'authorized_table_delta': 0,
 'previous_measured_owned_total': 158011,
 'previous_measured_engine_lines': 82735,
 'previous_measured_table_count': 45,
 'measured_owned_total': 158135,
 'measured_engine_lines': 82813,
 'measured_table_count': 45,
 'reason': 'Carry the unchanged approved retained-fixer authority delta onto current dev; the '
           'project supervisor pre-approved this exact merge measurement. Preserve every '
           'historical grant, original baseline and ownership classification, with zero '
           'remaining headroom and no new storage category.'},
 'pr_retirement_snapshot_composition': {'authorized_engine_delta': 41,
 'authorized_owned_delta': 41,
 'authorized_table_delta': 0,
 'charged_files': {'src/integration/delivery_observer.py': 11,
                   'src/integration/pr_delivery.py': 30},
 'decision_message_id': 'msg-16d85278911b46f391de921c562d1ea4',
 'dev_source_revision': '76e0ba209d55fd34161da53e07ae914c94da11e0',
 'measured_engine_lines': 82776,
 'measured_owned_total': 158052,
 'measured_table_count': 45,
 'operator_decision': '6679052c',
 'previous_measured_engine_lines': 82735,
 'previous_measured_owned_total': 158011,
 'previous_measured_table_count': 45,
 'reason': 'Exact supervisor-approved +41 owned/engine lines for shared delivery snapshots, '
           'fresh remote PR/default OID validation and retention across cancelled readers. '
           'Preserve the accepted implementation, original baselines, every historical grant and '
           'ownership classification; zero remaining headroom and no new storage category.',
 'referenced_decision': 'size-cap-delegation-2026-10-23',
 'source_revision': 'c2404a0320d8659ec5a950025a1f22f176fd1db1',
 'supervisor_decision_comment_id': 'comment-f1dfa485200d4cbd965cbdfd06b8075b',
 'task_id': 'nimble-willow-13'},
 'terminal_hibernation_holds_composition': {'authorized_engine_delta': 0,
 'authorized_owned_delta': 25,
 'authorized_shared_owned_delta': 25,
 'authorized_table_delta': 0,
 'dev_source_revision': '76e0ba209d55fd34161da53e07ae914c94da11e0',
 'measured_engine_lines': 82776,
 'measured_owned_total': 158077,
 'measured_table_count': 45,
 'previous_measured_engine_lines': 82776,
 'previous_measured_owned_total': 158052,
 'previous_measured_table_count': 45,
 'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/2037',
 'reason': 'Exact supervisor-approved ledger-only reconciliation for terminal hibernation hold '
           'retirement and resolved orphan cancellation, authorized in noble-beacon-61 reopen '
           'feedback at 2026-10-25 21:16Z. Measured against current dev after restoring the '
           'accepted published head. Preserve all historical grants, original baselines and '
           'ownership classifications, with zero remaining headroom and no new storage category.',
 'referenced_decision': 'size-cap-delegation-2026-10-23',
 'source_revision': 'e6882fa3e35febcc85f51d38c6d336969244d126',
 'supervisor_grant_reference': 'noble-beacon-61 reopen feedback, 2026-10-25 21:16Z',
 'task_id': 'noble-beacon-61'},
 'train_fetch_changed_refs_composition': {'authorized_engine_delta': 86,
   'authorized_owned_delta': 87,
   'authorized_table_delta': 0,
   'dev_source_revision': 'c0255a1f4',
   'measured_engine_lines': 82963,
   'measured_owned_total': 158280,
   'measured_table_count': 45,
   'operator_decision': '6679052c',
   'previous_measured_engine_lines': 82877,
   'previous_measured_owned_total': 158193,
   'previous_measured_table_count': 45,
   'reason': 'Exact supervisor-approved +87 owned / +86 engine lines for changed-ref-only train '
             'fetches, advisory delivery-observer refreshes and truth reconciliation on the train '
             'repository. Preserve every historical grant; zero remaining headroom and no new '
             'storage category.',
   'referenced_decision': 'size-cap-delegation-2026-10-23',
   'source_revision': '14ce4d02a',
   'task_id': 'smart-bridge-86'},
 'aborted_batch_branch_retirement_composition': {'authorized_engine_delta': 55,
   'authorized_owned_delta': 63,
   'authorized_table_delta': 0,
   'dev_source_revision': '0f0a16299',
   'measured_engine_lines': 83018,
   'measured_owned_total': 158343,
   'measured_table_count': 45,
   'operator_decision': '6679052c',
   'previous_measured_engine_lines': 82963,
   'previous_measured_owned_total': 158280,
   'previous_measured_table_count': 45,
   'reason': 'Exact supervisor-approved +63 owned / +55 engine lines for branch retirement of '
             'aborted-batch holds and the advisory-refresh-aware claim test (dev red fix). Preserve '
             'every historical grant; zero remaining headroom and no new storage category.',
   'referenced_decision': 'size-cap-delegation-2026-10-23',
   'source_revision': '24596d693',
   'task_id': 'crisp-apex-76'},
 'tick_checkout_minimum_interval_composition': {'authorized_engine_delta': 67,
 'authorized_owned_delta': 67,
 'authorized_table_delta': 0,
 'decision_message_id': 'msg-fe0cc19fc0a340bc8164791fc4d056ba',
 'dev_source_revision': '0f0a162997b368a14ed333b6722a16a6b1101e6e',
 'measured_engine_lines': 83030,
 'measured_owned_total': 158347,
 'measured_table_count': 45,
 'operator_decision': '6679052c',
 'previous_measured_engine_lines': 82963,
 'previous_measured_owned_total': 158280,
 'previous_measured_table_count': 45,
 'reason': 'Exact supervisor-approved tick snapshot join before checkout setup, tick-scoped '
           'checkout-open reuse and per-lane minimum visit interval. Preserve every historical '
           'grant and ownership classification; zero remaining headroom and no new storage '
           'category.',
 'referenced_decision': 'size-cap-delegation-2026-10-23',
 'request_message_id': 'msg-118c87f377634a1f8fc8a01cefd0f7f0',
 'source_revision': 'a5a528a31d905e0066c8dccf889677a00e8fc51c',
 'task_id': 'sharp-stone-41'},
 'tick_checkout_branch_retirement_merge_composition': {'authorized_engine_delta': 67,
 'authorized_owned_delta': 67,
 'authorized_table_delta': 0,
 'decision_message_id': 'msg-2c3c609249f14e15bd513ec8dfead06d',
 'dev_source_revision': '3ef631d52bc8270394732ee4181196eea016c6b6',
 'measured_engine_lines': 83085,
 'measured_owned_total': 158410,
 'measured_table_count': 45,
 'operator_decision': '6679052c',
 'previous_measured_engine_lines': 83018,
 'previous_measured_owned_total': 158343,
 'previous_measured_table_count': 45,
 'reason': 'Arithmetic composition of the existing sharp-stone-41 +67 owned / +67 engine grant '
           'with the inherited crisp-apex-76 +63 owned / +55 engine grant on common base '
           '0f0a16299. Preserves both approvals and every historical grant; zero remaining '
           'headroom and no new storage category.',
 'referenced_decision': 'size-cap-delegation-2026-10-23',
 'source_revision': 'c2ce4605f287313ba9dccc62ed8eb3c759012b3c',
 'task_id': 'sharp-stone-41'},
 'retained_fixer_authority_snapshot_refresh_measurement': {'authorized_engine_delta': 78,
 'authorized_owned_delta': 124,
 'authorized_shared_owned_delta': 46,
 'authorized_table_delta': 0,
 'decision_message_id': 'msg-5352ed6c4d7c492fa8da0fc0db3cdef3',
 'dev_source_revision': '392b339e3e9ee31b659c2a8309a91be0aa8fb8dc',
 'measured_engine_lines': 82955,
 'measured_owned_total': 158317,
 'measured_table_count': 45,
 'operator_decision': '6679052c',
 'original_decision_message_id': 'msg-3472013282e44ae0810b733694d356fe',
 'previous_measured_engine_lines': 82877,
 'previous_measured_owned_total': 158193,
 'previous_measured_table_count': 45,
 'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/2025',
 'reason': 'Exact supervisor-approved composition of the unchanged retained-fixer authority '
           'delta with dev snapshot sharing. Preserve every historical grant, prior merge '
           'measurement, original baseline and ownership classification, with zero remaining '
           'headroom and no new storage category.',
 'referenced_decision': 'size-cap-delegation-2026-10-23',
 'source_revision': '2681b3e0caa93c033ff01af7701c456ecf3b7e48',
 'task_id': 'noble-harbor-10'},
 'retained_fixer_authority_dev_refresh_measurement': {'task_id': 'noble-harbor-10',
 'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/2025',
 'referenced_decision': 'size-cap-delegation-2026-10-23',
 'operator_decision': '6679052c',
 'decision_message_id': 'msg-36443ca7b0ef465480834e27e53aa406',
 'original_decision_message_id': 'msg-3472013282e44ae0810b733694d356fe',
 'source_revision': 'fc96c5472f2caf1c768f8d4aa492f80602d91f74',
 'dev_source_revision': 'ddf8b118329f8cd301981c516bbb79e1e8cfa44f',
 'authorized_owned_delta': 124,
 'authorized_engine_delta': 78,
 'authorized_shared_owned_delta': 46,
 'authorized_table_delta': 0,
 'previous_measured_owned_total': 158077,
 'previous_measured_engine_lines': 82776,
 'previous_measured_table_count': 45,
 'measured_owned_total': 158201,
 'measured_engine_lines': 82854,
 'measured_table_count': 45,
 'reason': 'Exact supervisor-approved composition of the unchanged retained-fixer authority '
           'delta with current dev. Preserve every historical grant, prior merge measurement, '
           'original baseline and ownership classification, with zero remaining headroom and no '
           'new storage category.'},
 'wait_listener_ci_check_coalesce_composition': {'authorized_engine_delta': 0,
   'authorized_owned_delta': 17,
   'authorized_table_delta': 0,
   'dev_source_revision': '4e1ce7c75',
   'measured_engine_lines': 83163,
   'measured_owned_total': 158551,
   'measured_table_count': 45,
   'operator_decision': '6679052c',
   'previous_measured_engine_lines': 83163,
   'previous_measured_owned_total': 158534,
   'previous_measured_table_count': 45,
   'reason': 'Exact supervisor-approved +17 owned lines for coalescing in-progress ci_check '
             're-observation and a short graph-lock hold in the wait listener. Preserve every '
             'historical grant; zero remaining headroom and no new storage category.',
   'referenced_decision': 'size-cap-delegation-2026-10-23',
   'source_revision': 'wait-listener-coalesce epic',
   'task_id': 'fresh-ridge-78'},
 'originless_backfill_provenance_composition': {'authorized_engine_delta': 38,
   'authorized_owned_delta': 38,
   'authorized_table_delta': 0,
   'dev_source_revision': '638bbd2f0',
   'measured_engine_lines': 83221,
   'measured_owned_total': 158609,
   'measured_table_count': 45,
   'operator_decision': 'global-supervisor-2026-10-25',
   'previous_measured_engine_lines': 83183,
   'previous_measured_owned_total': 158571,
   'previous_measured_table_count': 45,
   'reason': 'Exact supervisor-approved +38 owned/+38 engine lines for retaining git-proven '
             'provenance on originless legacy completions in the backfill. Preserve every historical '
             'grant; zero remaining headroom and no new storage category.',
   'referenced_decision': 'size-cap-delegation-2026-10-23',
   'source_revision': 'legacy-backfill epic',
   'task_id': 'fair-quest-48'},
 'missing_target_abort_composition': {'authorized_engine_delta': 59,
   'authorized_owned_delta': 59,
   'authorized_table_delta': 0,
   'charged_files': {'src/integration/batches.py': 4,
                     'src/integration/fixers.py': 26,
                     'src/integration/train_controls.py': 15,
                     'src/integration/train_sources.py': 14},
   'dev_source_revision': '21109c4eb',
   'measured_engine_lines': 83383,
   'measured_owned_total': 158771,
   'measured_table_count': 45,
   'operator_decision': '6679052c',
   'previous_measured_engine_lines': 83324,
   'previous_measured_owned_total': 158712,
   'previous_measured_table_count': 45,
   'reason': 'Exact task-preapproved missing-target abort and epic-refresh retirement delta on '
             'merged dev. Preserve historical baselines, grants and ownership classifications; zero '
             'remaining headroom and no new storage category.',
   'referenced_decision': 'size-cap-delegation-2026-10-23',
   'source_revision': '39242c4c5',
   'task_id': 'grand-falcon-16'},
 'git_truth_phase_profile_composition': {'authorized_engine_delta': 35,
   'authorized_owned_delta': 77,
   'authorized_table_delta': 0,
   'dev_source_revision': 'c51bb0ea8',
   'measured_engine_lines': 83418,
   'measured_owned_total': 158848,
   'measured_table_count': 45,
   'operator_decision': '6679052c',
   'previous_measured_engine_lines': 83383,
   'previous_measured_owned_total': 158771,
   'previous_measured_table_count': 45,
   'reason': 'Exact task-preapproved reconcile_git_truth per-phase timing instrumentation '
             '(sound-meadow-89) plus sibling-slot branch release (bright-beacon-87, no size change). '
             'Preserve every historical grant; zero remaining headroom and no new storage category.',
   'referenced_decision': 'size-cap-delegation-2026-10-23',
   'source_revision': 'ebb6a75bb',
   'task_id': 'sound-meadow-89'},
 'responsive_dashboard_reduction': {'current_charged_files': {'dashboard/src/pages/BatchDetail.tsx': 33},
 'measured_engine_lines': 82649,
 'measured_owned_total': 157899,
 'measured_table_count': 45,
 'previous_charged_files': {'dashboard/src/pages/focus/FocusBatch.tsx': 36,
                            'dashboard/src/pages/focus/focusFormat.ts': 23},
 'previous_measured_owned_total': 157925,
 'pull_request': 'https://github.com/ElectricJack/agent-queue/pull/2028',
 'reason': 'Retire Focus UI and simplify its surviving batch helpers in the main dashboard. '
           'Lower the exact cap by 26 removed owned lines; preserve historical grants, fixed '
           'baselines, integration ownership, equality assertions and zero remaining '
           'headroom. No cap growth or new storage category.',
 'reclaimed_owned_lines': 26,
 'source_revision': 'db4c9af789df269ba20ddcaa30143ccc7137cd5c',
 'task_id': 'grand-ember-55'}}


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
    """Fixed original cap or the exact separately approved reconciliation."""
    if manifest.get("transition_exception") is not None:
        raise ValueError("unapproved integration transition exception")
    grant = manifest.get("hand_integration_reconciliation")
    if grant is not None:
        if grant != HAND_INTEGRATION_RECONCILIATION:
            raise ValueError("unapproved hand-integration reconciliation")
        return HAND_INTEGRATION_RECONCILIATION["measured_owned_total"]
    return BASELINE_OWNED_LINES + REPLACEMENT_ALLOWANCE


def transition_engine_cap(manifest: dict) -> int:
    """The reconciliation has no engine headroom and expires with transition."""
    transition_owned_cap(manifest)  # validate the same exact approval
    if manifest.get("hand_integration_reconciliation") is not None:
        return HAND_INTEGRATION_RECONCILIATION["measured_engine_lines"]
    return BASELINE_ENGINE_LINES + REPLACEMENT_ALLOWANCE


def budget_errors(report: dict, manifest: dict) -> list[str]:
    phase = manifest["phase"]
    if phase not in {"transition", "s4_modules", "final"}:
        return [f"unknown budget phase: {phase}"]
    if phase == "transition":
        try:
            total_cap = transition_owned_cap(manifest)
            engine_cap = transition_engine_cap(manifest)
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
