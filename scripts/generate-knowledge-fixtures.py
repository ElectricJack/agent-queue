#!/usr/bin/env python3
"""Generate the synthetic knowledge fixtures with consistent section hashes.

The golden fixtures under ``tests/fixtures/knowledge/golden/`` pin ``input_hashes``
for every top-level section, including observations and budgets;
editing a section invalidates its hash and the runner must fail it. This
regenerates the hashes from the current section contents so a deliberate
hash-mutation test can compare against a tampered copy in-place in a test,
never against the shipped golden set.

``tests/fixtures/knowledge/integrated/`` holds the fixtures the real K08
service is replayed against. Sealing their exact revision hashes needs the
service's own canonical snapshot form, so this generator imports
``tests.knowledge_fixture_adapter`` for the one snapshot builder the adapter
seeds from: two implementations would be two sets of bytes. It never opens a
database; only the integrated run does.

Usage::

    python scripts/generate-knowledge-fixtures.py           # write the golden set
    python scripts/generate-knowledge-fixtures.py --check   # fail when drift
"""

from __future__ import annotations

import copy
import json
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
EVALUATOR = runpy.run_path(str(ROOT / "scripts/evaluate-knowledge.py"))
canonical_dumps = EVALUATOR["canonical_dumps"]
sha256_text = EVALUATOR["sha256_text"]
seal_manifest = EVALUATOR["seal_manifest"]
render_snapshot = EVALUATOR["render_snapshot"]

GOLDEN_DIR = ROOT / "tests" / "fixtures" / "knowledge" / "golden"
INTEGRATED_DIR = ROOT / "tests" / "fixtures" / "knowledge" / "integrated"
PREPARED_AT = "2026-10-01T00:00:00Z"

IDENTITY_PROC = "rec-procedure-deploy@rev-0007:procedure"
IDENTITY_PROC_OLD = "rec-procedure-deploy@rev-0006:procedure"
IDENTITY_EVIDENCE = "rec-evidence-log-retained@rev-0002:evidence"
IDENTITY_SUMMARY = "rec-summary-prior@rev-0001:context"
IDENTITY_SECRET = "rec-secret-crossproject@rev-0003:note"
IDENTITY_PRIVATE = "rec-private-global-link@rev-0001:note"
IDENTITY_REVOKED = "rec-share-revoked@rev-0002:note"
IDENTITY_CONFLICT_A = "rec-claim-alpha@rev-0002:evidence"
IDENTITY_CONFLICT_B = "rec-claim-beta@rev-0003:evidence"
IDENTITY_REDACED = "rec-history-redacted@rev-0004:evidence"
IDENTITY_NEAR = "rec-procedure-nearmatch@rev-0001:procedure"
IDENTITY_BIG = "rec-guidance-long@rev-0005:guidance"
IDENTITY_SMALL = "rec-guidance-small@rev-0001:guidance"

PROC_TEXT = "Deploy to the staging rail: run the health gate, then freeze the schema and release."
PROC_OLD_TEXT = "Deploy to the staging rail: skip the health gate when in a hurry."
EVIDENCE_TEXT = "Observed log line: the rail rejected the schema change at step three."
SUMMARY_TEXT = "A prior agent summary said the deploy failed at step three."
NEAR_PROC_TEXT = "Deploy to the production rail: run the health gate, then release."
BIG_TEXT = "Long guidance body: " + ("padding token. " * 40)


def manifest(
    fixture_id: str,
    scenario: str,
    records: list,
    expected: dict,
    *,
    role: str = "worker",
    budget: dict | None = None,
    stale_claim: dict | None = None,
    owner_kind: str = "task_attempt",
    claim_epoch: int = 1,
    bundle_id: str | None = None,
) -> dict:
    m: dict = {
        "schema_version": 1,
        "fixture_id": fixture_id,
        "scenario": scenario,
        "synthetic": True,
        "source_class": "synthetic",
        "records": records,
        "expected": expected,
        "delivery_rules": {
            "claim_epoch": claim_epoch,
            "owner_kind": owner_kind,
            "owner_id": f"attempt-{fixture_id}",
            "prepared_at": PREPARED_AT,
            "bundle_id": bundle_id or f"bundle-{fixture_id}",
            "role": role,
        },
        "labels": {"harnesses": ALL_HARNESSES},
    }
    if budget is not None:
        m["budget"] = budget
    if stale_claim is not None:
        m["stale_claim"] = stale_claim
    return m


ALL_HARNESSES = ["claude", "codex", "opencode", "local"]


def rec(record_id: str, revision_id: str, kind: str, **excerpts: str) -> dict:
    return {
        "record_id": record_id,
        "revision_id": revision_id,
        "kind": kind,
        "excerpts": excerpts,
        "evidence": "synthetic-log-evidence" if kind == "evidence" else "synthetic-agent-assertion",
        "authority": "unverified",
        "freshness": "current",
    }


# 1. Current procedure plus superseded near-match
f_current_superseded = manifest(
    "current-plus-superseded",
    "Select current eligible procedure, preserve exact revision, warn for explicit retired read",
    [
        rec("rec-procedure-deploy", "rev-0007", "procedure", body=PROC_TEXT),
        rec("rec-procedure-deploy", "rev-0006", "procedure", body=PROC_OLD_TEXT),
        rec("rec-procedure-nearmatch", "rev-0001", "procedure", body=NEAR_PROC_TEXT),
    ],
    {
        "allowed_identities": [IDENTITY_PROC],
        "omitted_reasons": {
            IDENTITY_PROC_OLD: "retired",
            IDENTITY_NEAR: "not-current-eligible",
        },
        "forbidden_records": {},
    },
)

# 2. Conflicting claims and disputed fact
f_conflicting = manifest(
    "conflicting-claims",
    "Return evidence/trust distinctions, no silent merge or verified overwrite",
    [
        rec(
            "rec-claim-alpha",
            "rev-0002",
            "evidence",
            body="Alpha observed the rail reject at step three.",
        ),
        rec(
            "rec-claim-beta",
            "rev-0003",
            "evidence",
            body="Beta observed the rail reject at step four.",
        ),
    ],
    {
        "allowed_identities": [IDENTITY_CONFLICT_A, IDENTITY_CONFLICT_B],
        "omitted_reasons": {},
        "forbidden_records": {},
    },
)

# 3. Cross-project secret, private source behind global link and revoked share
f_secret = manifest(
    "cross-project-secret",
    "Zero title/body/snippet/count/error leakage on search/history/link/export/bundle",
    [
        rec(
            "rec-secret-crossproject",
            "rev-0003",
            "note",
            body="The cross-project vault key is vault-key-77.",
        ),
        rec(
            "rec-private-global-link",
            "rev-0001",
            "note",
            body="Private project alpha notes are linked here.",
        ),
        rec(
            "rec-share-revoked",
            "rev-0002",
            "note",
            body="A revoked share still points at this note.",
        ),
        rec("rec-evidence-log-retained", "rev-0002", "evidence", body=EVIDENCE_TEXT),
    ],
    {
        "allowed_identities": [IDENTITY_EVIDENCE],
        "omitted_reasons": {
            IDENTITY_SECRET: "forbidden-identity",
            IDENTITY_PRIVATE: "forbidden-identity",
            IDENTITY_REVOKED: "forbidden-identity",
        },
        "forbidden_records": {
            IDENTITY_SECRET: {"title": "Cross-project vault key", "body": "vault-key-77"},
            IDENTITY_PRIVATE: {"title": "Private project alpha", "body": "alpha notes"},
            IDENTITY_REVOKED: {"snippet": "revoked share"},
        },
    },
)

# 4. Irrelevant near-match and task requiring no memory
f_irrelevant = manifest(
    "irrelevant-near-match",
    "Empty or relevant small bundle, no minimum filler, no new task or paid call",
    [
        rec("rec-procedure-nearmatch", "rev-0001", "procedure", body=NEAR_PROC_TEXT),
    ],
    {
        "allowed_identities": [],
        "omitted_reasons": {IDENTITY_NEAR: "not-current-eligible"},
        "forbidden_records": {},
    },
)

# 5. Edited/retired/restored/redacted history
f_history = manifest(
    "edited-retired-redacted-history",
    "Exact old bytes/links until redaction; redacted is typed unavailable, never current fallback",
    [
        rec("rec-evidence-log-retained", "rev-0002", "evidence", body=EVIDENCE_TEXT),
        rec(
            "rec-history-redacted",
            "rev-0004",
            "evidence",
            body="[redacted by operator at rev-0004]",
        ),
        rec("rec-summary-prior", "rev-0001", "context", body=SUMMARY_TEXT),
    ],
    {
        "allowed_identities": [IDENTITY_EVIDENCE, IDENTITY_SUMMARY],
        "omitted_reasons": {IDENTITY_REDACED: "redacted-typed-unavailable"},
        "forbidden_records": {
            IDENTITY_REDACED: {"body": "redacted by operator at rev-0004"},
        },
    },
)

# 6. Observed duplicate delivery receipts (no extraction or delivery ledger here)
f_duplicate = manifest(
    "duplicate-delivery-event",
    "One observed delivery per transport key; duplicate observation adds no citation",
    [
        rec("rec-evidence-log-retained", "rev-0002", "evidence", body=EVIDENCE_TEXT),
    ],
    {
        "allowed_identities": [IDENTITY_EVIDENCE],
        "omitted_reasons": {},
        "forbidden_records": {},
        "duplicate_delivery": {
            "attempts": [
                {
                    "bundle_id": "bundle-duplicate-delivery-event",
                    "transport": "hook",
                    "transport_key": "bundle-duplicate-delivery-event:hook",
                    "expected_new": True,
                    "final_state": "delivered",
                },
                {
                    "bundle_id": "bundle-duplicate-delivery-event",
                    "transport": "hook",
                    "transport_key": "bundle-duplicate-delivery-event:hook",
                    "expected_new": False,
                    "final_state": "delivered",
                },
                {
                    "bundle_id": "bundle-duplicate-delivery-event",
                    "transport": "cli",
                    "transport_key": "bundle-duplicate-delivery-event:cli",
                    "expected_new": True,
                    "final_state": "delivered",
                },
            ]
        },
    },
)

# 7. Compaction / provider switch / recycled pool slot + stale claim
f_stale = manifest(
    "stale-claim-recycled-slot",
    "Reauthorize exact references, lower budget reduces selection, stale claim cannot receive prior context",
    [
        rec("rec-evidence-log-retained", "rev-0002", "evidence", body=EVIDENCE_TEXT),
        rec(
            "rec-guidance-small",
            "rev-0001",
            "guidance",
            body="Small guidance: check the rail lease before restart.",
        ),
    ],
    {
        "allowed_identities": [IDENTITY_EVIDENCE, IDENTITY_SMALL],
        "omitted_reasons": {IDENTITY_PROC: "stale-claim"},
        "forbidden_records": {},
    },
    role="worker",
    claim_epoch=2,
    stale_claim={
        "expected_forbidden_identity": IDENTITY_PROC,
        "claim_epoch": 2,
        "stale_epoch": 1,
    },
)

# 8. Lower budget reduces selection (token budget, whole-item drop)
f_budget = manifest(
    "lower-budget-reduces-selection",
    "Lower budget reduces selection; drop whole knowledge items, record omissions",
    [
        rec("rec-guidance-long", "rev-0005", "guidance", body=BIG_TEXT),
        rec("rec-evidence-log-retained", "rev-0002", "evidence", body=EVIDENCE_TEXT),
    ],
    {
        "allowed_identities": [IDENTITY_EVIDENCE],
        "omitted_reasons": {IDENTITY_BIG: "budget-exceeded"},
        "forbidden_records": {},
        "required_budget_diagnostic": False,
    },
    budget={
        "max_tokens": 300,
        "max_bytes": 400,
        "token_unit": "upper_bound_bytes:utf8",
        "reserved_tokens": 0,
        "reserved_bytes": 0,
    },
)

# 9. Required content alone exceeds budget -> typed diagnostic
f_required_over = manifest(
    "required-content-over-budget",
    "If required non-knowledge content alone exceeds the input budget, typed context.required_over_budget diagnostic",
    [
        rec("rec-guidance-long", "rev-0005", "guidance", body=BIG_TEXT),
    ],
    {
        "allowed_identities": [],
        "omitted_reasons": {IDENTITY_BIG: "context.required_over_budget"},
        "forbidden_records": {},
        "required_budget_diagnostic": True,
    },
    budget={
        "max_tokens": 32,
        "max_bytes": 64,
        "token_unit": "upper_bound_bytes:utf8",
        "reserved_tokens": 160,
        "reserved_bytes": 320,
    },
)

# 10. No-hook harness: identical selected Markdown through the explicit CLI adapter
f_no_hook = manifest(
    "no-hook-cli-adapter",
    "A harness with no hook remains supported through explicit startup/claim guidance",
    [
        rec("rec-evidence-log-retained", "rev-0002", "evidence", body=EVIDENCE_TEXT),
    ],
    {
        "allowed_identities": [IDENTITY_EVIDENCE],
        "omitted_reasons": {},
        "forbidden_records": {},
        "duplicate_delivery": {
            "attempts": [
                {
                    "bundle_id": "bundle-no-hook-cli-adapter",
                    "transport": "cli-plain",
                    "transport_key": "bundle-no-hook-cli-adapter:cli-plain:claude",
                    "expected_new": True,
                    "final_state": "delivered",
                }
            ]
        },
    },
    role="worker",
)

# 11. Supervisor role + cross-project global summaries (role parity)
f_supervisor = manifest(
    "supervisor-global-project-set",
    "Global supervisors must explicitly choose bounded project sets or global summaries",
    [
        rec("rec-evidence-log-retained", "rev-0002", "evidence", body=EVIDENCE_TEXT),
        rec("rec-summary-prior", "rev-0001", "context", body=SUMMARY_TEXT),
    ],
    {
        "allowed_identities": [IDENTITY_EVIDENCE, IDENTITY_SUMMARY],
        "omitted_reasons": {},
        "forbidden_records": {},
    },
    role="supervisor",
    owner_kind="supervisor_session",
)

# 12. Azure-incident synthetic reproduction (labelled synthetic)
f_azure = manifest(
    "azure-incident-synthetic-reproduction",
    "Cite the retained log artifact for observed facts, label summary as assertion, preserve repair/task state outside knowledge",
    [
        rec("rec-evidence-log-retained", "rev-0002", "evidence", body=EVIDENCE_TEXT),
        rec("rec-summary-prior", "rev-0001", "context", body=SUMMARY_TEXT),
    ],
    {
        "allowed_identities": [IDENTITY_EVIDENCE, IDENTITY_SUMMARY],
        "omitted_reasons": {},
        "forbidden_records": {},
    },
)


FIXTURES: dict[str, dict] = {
    f["fixture_id"]: f
    for f in (
        f_current_superseded,
        f_conflicting,
        f_secret,
        f_irrelevant,
        f_history,
        f_duplicate,
        f_stale,
        f_budget,
        f_required_over,
        f_no_hook,
        f_supervisor,
        f_azure,
    )
}

# Recorded selections are explicit fixture data, independent of the expected oracle.
# This authoring code does not run a selector, budget gate, authorization or ledger.
SNAPSHOTS = {
    "current-plus-superseded": (
        [IDENTITY_PROC],
        {IDENTITY_PROC_OLD: "retired", IDENTITY_NEAR: "not-current-eligible"},
    ),
    "conflicting-claims": ([IDENTITY_CONFLICT_A, IDENTITY_CONFLICT_B], {}),
    "cross-project-secret": (
        [IDENTITY_EVIDENCE],
        {
            IDENTITY_SECRET: "forbidden-identity",
            IDENTITY_PRIVATE: "forbidden-identity",
            IDENTITY_REVOKED: "forbidden-identity",
        },
    ),
    "irrelevant-near-match": ([], {IDENTITY_NEAR: "not-current-eligible"}),
    "edited-retired-redacted-history": (
        [IDENTITY_EVIDENCE, IDENTITY_SUMMARY],
        {IDENTITY_REDACED: "redacted-typed-unavailable"},
    ),
    "duplicate-delivery-event": ([IDENTITY_EVIDENCE], {}),
    "stale-claim-recycled-slot": (
        [IDENTITY_EVIDENCE, IDENTITY_SMALL],
        {IDENTITY_PROC: "stale-claim"},
    ),
    "lower-budget-reduces-selection": ([IDENTITY_EVIDENCE], {IDENTITY_BIG: "budget-exceeded"}),
    "required-content-over-budget": ([], {IDENTITY_BIG: "context.required_over_budget"}),
    "no-hook-cli-adapter": ([IDENTITY_EVIDENCE], {}),
    "supervisor-global-project-set": ([IDENTITY_EVIDENCE, IDENTITY_SUMMARY], {}),
    "azure-incident-synthetic-reproduction": ([IDENTITY_EVIDENCE, IDENTITY_SUMMARY], {}),
}


def snapshot(fixture: dict, selected: list[str], omitted: dict[str, str]) -> dict:
    items = []
    for ref in selected:
        record = next(
            r
            for r in fixture["records"]
            if f"{r['record_id']}@{r['revision_id']}:{r['kind']}" == ref
        )
        excerpt = record["excerpts"]["body"]
        items.append(
            {
                **{k: v for k, v in record.items() if k != "excerpts"},
                "excerpt": excerpt,
                "content_sha256": sha256_text(excerpt),
            }
        )
    rules = fixture["delivery_rules"]
    owner = {key: rules[key] for key in ("owner_kind", "owner_id", "claim_epoch")}
    rendered = render_snapshot(items)
    return {
        "selected": items,
        "omissions": [
            {"identity": ref, "reason": reason} for ref, reason in sorted(omitted.items())
        ],
        "rendered": rendered,
        "rendered_sha256": sha256_text(rendered),
        "owner": owner,
        "citations": [
            {
                **{key: item[key] for key in ("record_id", "revision_id", "content_sha256")},
                "kind": "injected",
                "owner": owner,
            }
            for item in items
        ],
        "deliveries": [
            {
                "bundle_id": rules["bundle_id"],
                "transport": "cli-plain",
                "transport_key": f"{rules['bundle_id']}:cli-plain",
                "new": True,
                "final_state": "delivered",
            }
        ],
        "usage": {
            "method": "upper_bound_bytes:utf8",
            "tokens": len(rendered.encode("utf-8")) + fixture["budget"]["reserved_tokens"],
            "bytes": len(rendered.encode("utf-8")) + fixture["budget"]["reserved_bytes"],
        },
        "diagnostics": (
            ["context.required_over_budget"]
            if fixture["fixture_id"] == "required-content-over-budget"
            else []
        ),
        "metadata": {"prepared_at": rules["prepared_at"], "synthetic": True},
    }


for name, fixture in FIXTURES.items():
    fixture["labels"] = {"harnesses": ALL_HARNESSES}
    fixture["delivery_rules"]["owner_id"] = (
        f"session-{name}"
        if fixture["delivery_rules"]["role"] == "supervisor"
        else f"attempt-{name}"
    )
    fixture.setdefault(
        "budget",
        {
            "max_tokens": 4096,
            "max_bytes": 16384,
            "token_unit": "upper_bound_bytes:utf8",
            "reserved_tokens": 64,
            "reserved_bytes": 64,
        },
    )
    fixture["expected"].setdefault("required_budget_diagnostic", False)
    bundle = fixture["delivery_rules"]["bundle_id"]
    fixture["expected"].setdefault(
        "duplicate_delivery",
        {
            "attempts": [
                {
                    "bundle_id": bundle,
                    "transport": "cli-plain",
                    "transport_key": f"{bundle}:cli-plain",
                    "expected_new": True,
                    "final_state": "delivered",
                }
            ]
        },
    )
    if name == "stale-claim-recycled-slot":
        fixture["records"].append(
            rec("rec-procedure-deploy", "rev-0007", "procedure", body=PROC_TEXT)
        )
        fixture["expected"]["forbidden_records"][IDENTITY_PROC] = {"body": PROC_TEXT}
    if name == "current-plus-superseded":
        fixture["records"][1]["freshness"] = "retired"
    if name == "conflicting-claims":
        for record in fixture["records"]:
            record["authority"] = "disputed"
    if name == "edited-retired-redacted-history":
        fixture["records"][1]["freshness"] = "redacted"
    fixture["observation"] = snapshot(fixture, *SNAPSHOTS[name])
    if name == "duplicate-delivery-event":
        fixture["observation"]["deliveries"] = [
            {
                "bundle_id": bundle,
                "transport": transport,
                "transport_key": f"{bundle}:{transport}",
                "new": new,
                "final_state": "delivered",
            }
            for transport, new in (("hook", True), ("hook", False), ("cli", True))
        ]
    if name == "no-hook-cli-adapter":
        fixture["expected"]["duplicate_delivery"]["attempts"][0]["transport_key"] = (
            f"{bundle}:cli-plain"
        )
    fixture["delivery_rules"]["events"] = [
        {key: value for key, value in receipt.items() if key != "new"}
        for receipt in fixture["observation"]["deliveries"]
    ]
    seal_manifest(fixture)

# An identical authorized payload under a worker owner makes cross-role parity reviewable.
worker = copy.deepcopy(FIXTURES["supervisor-global-project-set"])
worker["fixture_id"] = "worker-bounded-project-set"
worker["delivery_rules"].update(
    role="worker",
    owner_kind="task_attempt",
    owner_id="attempt-worker-bounded-project-set",
    bundle_id="bundle-worker-bounded-project-set",
)
worker["observation"] = snapshot(worker, [IDENTITY_EVIDENCE, IDENTITY_SUMMARY], {})
worker["expected"]["duplicate_delivery"]["attempts"] = [
    {
        **worker["observation"]["deliveries"][0],
        "expected_new": True,
    }
]
worker["expected"]["duplicate_delivery"]["attempts"][0].pop("new")
worker["delivery_rules"]["events"] = [
    {key: value for key, value in receipt.items() if key != "new"}
    for receipt in worker["observation"]["deliveries"]
]
seal_manifest(worker)
FIXTURES[worker["fixture_id"]] = worker

# ---------------------------------------------------------------------------
# Integrated fixtures: replayed against the real K08 service, not a snapshot.
#
# Each record seals the exact revision content hash the record store computes,
# so the oracle compares the adapter's reported hash with sealed bytes instead
# of hashing the excerpt it rendered. ``authority`` is the delivery authority
# label ("none" without an authority grant), and ``evidence`` is a retained
# artifact source id, which the rendered payload carries verbatim.
# ---------------------------------------------------------------------------


def snapshot_fixture_snapshot(fixture: dict) -> dict:
    """Placeholder observation for an integrated fixture.

    The integrated adapter ignores it — that is the whole point of an
    integration fixture — but the manifest schema requires the section, so an
    empty observation is sealed rather than a recorded snapshot of a run.
    """
    rules = fixture["delivery_rules"]
    return {
        "selected": [],
        "omissions": [],
        "rendered": "",
        "rendered_sha256": sha256_text(""),
        "owner": {key: rules[key] for key in ("owner_kind", "owner_id", "claim_epoch")},
        "citations": [],
        "deliveries": [],
        "usage": {"method": "upper_bound_bytes:utf8", "tokens": 0, "bytes": 0},
        "diagnostics": [],
        "metadata": {"synthetic": True},
    }


def integration_hash(doc: dict) -> str:
    """The revision content hash the record store will compute for *doc*.

    Deliberately produced by the same canonicalization the service uses: the
    fixture pins the bytes it sealed, and the adapter has to arrive at that
    hash from the database rather than by re-rendering anything.
    """
    from src.knowledge.models import content_hash, normalize_snapshot

    return content_hash(normalize_snapshot(doc))


def integration_record(record_id: str, revision_id: str, kind: str, body: str) -> dict:
    evidence = "synthetic-log-evidence" if kind == "evidence" else "synthetic-agent-assertion"
    record = {
        "record_id": record_id,
        "revision_id": revision_id,
        "kind": kind,
        "excerpts": {"body": body},
        "evidence": evidence,
        "authority": "none",
        "freshness": "current",
        "verification": "unverified",
        "lifecycle": "active",
    }
    from tests.knowledge_fixture_adapter import _seed_snapshot

    record["revision_sha256"] = integration_hash(_seed_snapshot(record))
    return record


def integration_manifest(
    fixture_id: str,
    scenario: str,
    records: list,
    *,
    allowed: list,
    omitted: dict | None = None,
    attempts: list,
    budget: dict | None = None,
    required_diagnostic: bool = False,
) -> dict:
    fixture = manifest(
        fixture_id,
        scenario,
        records,
        {
            "allowed_identities": allowed,
            "omitted_reasons": omitted or {},
            "forbidden_records": {},
            "duplicate_delivery": {"attempts": attempts},
            "required_budget_diagnostic": required_diagnostic,
        },
        budget=budget or {
            "max_tokens": 8192,
            "max_bytes": 16384,
            "token_unit": "upper_bound_bytes:utf8",
            "reserved_tokens": 64,
            "reserved_bytes": 64,
        },
    )
    fixture["observation"] = snapshot_fixture_snapshot(fixture)
    fixture["delivery_rules"]["events"] = [
        {key: value for key, value in attempt.items() if key != "expected_new"}
        for attempt in attempts
    ]
    seal_manifest(fixture)
    return fixture


def _identity(record: dict) -> str:
    return f"{record['record_id']}@{record['revision_id']}:{record['kind']}"


_PARITY_RECORDS = [
    integration_record("rec-integrated-evidence", "rev-0001", "evidence", EVIDENCE_TEXT),
    integration_record(
        "rec-integrated-procedure", "rev-0002", "procedure",
        "Run the staging health gate, then freeze the schema before release.",
    ),
]

# Every installed harness label delivers the identical selection: Claude and
# Codex through the SessionStart hook envelope, OpenCode through the startup
# prompt, and a local model through explicit guidance.
INTEGRATED: dict[str, dict] = {
    "hook-and-startup-parity": integration_manifest(
        "hook-and-startup-parity",
        "Identical selected Markdown and one citation per record across every harness",
        _PARITY_RECORDS,
        allowed=[_identity(record) for record in _PARITY_RECORDS],
        attempts=[{
            "bundle_id": "bundle-hook-and-startup-parity",
            "transport": "hook_envelope",
            "transport_key": "bundle-hook-and-startup-parity:hook_envelope",
            "final_state": "delivered",
            "expected_new": True,
        }],
    ),
    # A lost acknowledgment is `unknown`, and the retry deduplicates: one
    # delivery receipt and one citation, not two of either.
    "duplicate-acknowledgment": integration_manifest(
        "duplicate-acknowledgment",
        "A retried acknowledgment on one transport key creates no second receipt or citation",
        [_PARITY_RECORDS[0]],
        allowed=[_identity(_PARITY_RECORDS[0])],
        attempts=[
            {
                "bundle_id": "bundle-duplicate-acknowledgment",
                "transport": "startup_prompt",
                "transport_key": "bundle-duplicate-acknowledgment:startup_prompt",
                "final_state": "unknown",
                "expected_new": True,
            },
            {
                "bundle_id": "bundle-duplicate-acknowledgment",
                "transport": "startup_prompt",
                "transport_key": "bundle-duplicate-acknowledgment:startup_prompt",
                "final_state": "delivered",
                "expected_new": False,
            },
        ],
    ),
    # Required content alone over budget: a typed diagnostic, no knowledge, no
    # rendering, and no delivery receipt to claim a cap that was never met.
    "required-content-over-budget": integration_manifest(
        "required-content-over-budget",
        "Required reserves alone exceed the cap: typed diagnostic, zero payload",
        _PARITY_RECORDS,
        allowed=[],
        omitted={_identity(record): "context.required_over_budget" for record in _PARITY_RECORDS},
        attempts=[],
        budget={
            "max_tokens": 32,
            "max_bytes": 16384,
            "token_unit": "upper_bound_bytes:utf8",
            "reserved_tokens": 64,
            "reserved_bytes": 64,
        },
        required_diagnostic=True,
    ),
}

# Intentionally failing, sealed observations: these demonstrate independent oracle failures.
NEGATIVE: dict[str, dict] = {}
for name, base in (
    ("leaked-private-body", "cross-project-secret"),
    ("duplicate-citation", "duplicate-delivery-event"),
    ("stale-revision-substitution", "current-plus-superseded"),
):
    failure = copy.deepcopy(FIXTURES[base])
    failure["fixture_id"] = name
    observed = failure["observation"]
    if name == "leaked-private-body":
        observed["metadata"]["adapter_error"] = "vault-key-77"
    elif name == "duplicate-citation":
        observed["citations"].append(copy.deepcopy(observed["citations"][0]))
    else:
        observed["selected"][0]["revision_id"] = "rev-0006"
    NEGATIVE[name] = seal_manifest(failure)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    problems: list[str] = []
    all_fixtures = [
        (GOLDEN_DIR, FIXTURES),
        (INTEGRATED_DIR, INTEGRATED),
        (GOLDEN_DIR.parent / "negative", NEGATIVE),
    ]
    for directory, group in all_fixtures:
        for name, fixture in sorted(group.items()):
            path = directory / f"{name}.json"
            rendered = json.dumps(fixture, indent=2, sort_keys=True) + "\n"
            if args.check:
                if not path.exists() or path.read_text(encoding="utf-8") != rendered:
                    problems.append(name)
            # Re-verify each hash is self-consistent in the generator.
            for section in fixture["input_hashes"]:
                want = sha256_text(canonical_dumps(fixture[section]))
                if fixture["input_hashes"].get(section) != want:
                    problems.append(f"{name}:hash:{section}")
    if problems:
        for p in problems:
            print(p, file=sys.stderr)
        print("drift or bad hashes:", file=sys.stderr)
        return 1
    if not args.check:
        for directory, group in all_fixtures:
            directory.mkdir(parents=True, exist_ok=True)
            for name, fixture in sorted(group.items()):
                path = directory / f"{name}.json"
                path.write_text(
                    json.dumps(fixture, indent=2, sort_keys=True) + "\n", encoding="utf-8"
                )
        print(
            f"Wrote {len(FIXTURES)} golden, {len(INTEGRATED)} integrated "
            f"and {len(NEGATIVE)} negative fixtures"
        )
    else:
        print(
            f"Fixtures current: {len(FIXTURES)} golden, {len(INTEGRATED)} integrated, "
            f"{len(NEGATIVE)} negative"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
