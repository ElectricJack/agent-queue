#!/usr/bin/env python3
"""Generate the synthetic golden knowledge fixtures with consistent section hashes.

The golden fixtures under ``tests/fixtures/knowledge/golden/`` pin ``input_hashes``
for the ``records`` / ``expected`` / ``labels`` / ``delivery_rules`` sections;
editing a section invalidates its hash and the runner must fail it.  This
regenerates the hashes from the current section contents so a deliberate
hash-mutation test can compare against a tampered copy in-place in a test,
never against the shipped golden set.

Usage::

    python scripts/generate-knowledge-fixtures.py           # write the golden set
    python scripts/generate-knowledge-fixtures.py --check   # fail when drift
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.knowledge.evaluation_helpers import canonical_dumps, sha256_text  # noqa: E402

GOLDEN_DIR = ROOT / "tests" / "fixtures" / "knowledge" / "golden"
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
    harnesses: list,
    adapters: list,
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
        "labels": {"harnesses": harnesses, "adapters": adapters},
    }
    if budget is not None:
        m["budget"] = budget
    if stale_claim is not None:
        m["stale_claim"] = stale_claim
    m["input_hashes"] = {
        "records": sha256_text(canonical_dumps(m["records"])),
        "expected": sha256_text(canonical_dumps(m["expected"])),
        "labels": sha256_text(canonical_dumps(m["labels"])),
        "delivery_rules": sha256_text(canonical_dumps(m["delivery_rules"])),
    }
    return m


ALL_ADAPTERS = ["claude", "codex", "opencode", "local"]
ALL_HARNESSES = ["claude", "codex", "opencode", "local"]


def rec(record_id: str, revision_id: str, kind: str, **excerpts: str) -> dict:
    return {"record_id": record_id, "revision_id": revision_id, "kind": kind, "excerpts": excerpts}


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
        "provider_labels_allowed": [],
    },
    ALL_HARNESSES,
    ALL_ADAPTERS,
)

# 2. Conflicting claims and disputed fact
f_conflicting = manifest(
    "conflicting-claims",
    "Return evidence/trust distinctions, no silent merge or verified overwrite",
    [
        rec("rec-claim-alpha", "rev-0002", "evidence", body="Alpha observed the rail reject at step three."),
        rec("rec-claim-beta", "rev-0003", "evidence", body="Beta observed the rail reject at step four."),
    ],
    {
        "allowed_identities": [IDENTITY_CONFLICT_A, IDENTITY_CONFLICT_B],
        "omitted_reasons": {},
        "forbidden_records": {},
        "provider_labels_allowed": [],
    },
    ALL_HARNESSES,
    ALL_ADAPTERS,
)

# 3. Cross-project secret, private source behind global link and revoked share
f_secret = manifest(
    "cross-project-secret",
    "Zero title/body/snippet/count/error leakage on search/history/link/export/bundle",
    [
        rec("rec-secret-crossproject", "rev-0003", "note", body="The cross-project vault key is vault-key-77."),
        rec("rec-private-global-link", "rev-0001", "note", body="Private project alpha notes are linked here."),
        rec("rec-share-revoked", "rev-0002", "note", body="A revoked share still points at this note."),
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
        "provider_labels_allowed": [],
    },
    ALL_HARNESSES,
    ALL_ADAPTERS,
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
        "provider_labels_allowed": [],
    },
    ALL_HARNESSES,
    ALL_ADAPTERS,
)

# 5. Edited/retired/restored/redacted history
f_history = manifest(
    "edited-retired-redacted-history",
    "Exact old bytes/links until redaction; redacted is typed unavailable, never current fallback",
    [
        rec("rec-evidence-log-retained", "rev-0002", "evidence", body=EVIDENCE_TEXT),
        rec("rec-history-redacted", "rev-0004", "evidence", body="[redacted by operator at rev-0004]"),
        rec("rec-summary-prior", "rev-0001", "context", body=SUMMARY_TEXT),
    ],
    {
        "allowed_identities": [IDENTITY_EVIDENCE, IDENTITY_SUMMARY],
        "omitted_reasons": {IDENTITY_REDACED: "redacted-typed-unavailable"},
        "forbidden_records": {
            IDENTITY_REDACED: {"body": "redacted by operator at rev-0004"},
        },
        "provider_labels_allowed": [],
    },
    ALL_HARNESSES,
    ALL_ADAPTERS,
)

# 6. Duplicate extraction event (duplicate delivery ledger)
f_duplicate = manifest(
    "duplicate-delivery-event",
    "One proposal per source/output key, durable retry or explicit ambiguity quarantine",
    [
        rec("rec-evidence-log-retained", "rev-0002", "evidence", body=EVIDENCE_TEXT),
    ],
    {
        "allowed_identities": [IDENTITY_EVIDENCE],
        "omitted_reasons": {},
        "forbidden_records": {},
        "provider_labels_allowed": [],
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
    ALL_HARNESSES,
    ALL_ADAPTERS,
)

# 7. Compaction / provider switch / recycled pool slot + stale claim
f_stale = manifest(
    "stale-claim-recycled-slot",
    "Reauthorize exact references, lower budget reduces selection, stale claim cannot receive prior context",
    [
        rec("rec-evidence-log-retained", "rev-0002", "evidence", body=EVIDENCE_TEXT),
        rec("rec-guidance-small", "rev-0001", "guidance", body="Small guidance: check the rail lease before restart."),
    ],
    {
        "allowed_identities": [IDENTITY_EVIDENCE, IDENTITY_SMALL],
        "omitted_reasons": {IDENTITY_PROC: "stale-claim"},
        "forbidden_records": {},
        "provider_labels_allowed": [],
    },
    ALL_HARNESSES,
    ALL_ADAPTERS,
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
        "provider_labels_allowed": [],
        "required_budget_diagnostic": False,
    },
    ALL_HARNESSES,
    ALL_ADAPTERS,
    budget={
        "max_tokens": 200,
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
        "provider_labels_allowed": [],
        "required_budget_diagnostic": True,
    },
    ALL_HARNESSES,
    ALL_ADAPTERS,
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
        "provider_labels_allowed": ["claude"],
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
    ["claude"],
    ["claude"],
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
        "provider_labels_allowed": [],
    },
    ["codex", "opencode"],
    ["codex", "opencode"],
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
        "provider_labels_allowed": [],
    },
    ALL_HARNESSES,
    ALL_ADAPTERS,
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


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    problems: list[str] = []
    for name, fixture in sorted(FIXTURES.items()):
        path = GOLDEN_DIR / f"{name}.json"
        rendered = json.dumps(fixture, indent=2, sort_keys=True) + "\n"
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != rendered:
                problems.append(name)
        # Re-verify each hash is self-consistent in the generator.
        for section in ("records", "expected", "labels", "delivery_rules"):
            want = sha256_text(canonical_dumps(fixture[section]))
            if fixture["input_hashes"].get(section) != want:
                problems.append(f"{name}:hash:{section}")
    if problems:
        for p in problems:
            print(p, file=sys.stderr)
        print("drift or bad hashes:", file=sys.stderr)
        return 1
    if not args.check:
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        for name, fixture in sorted(FIXTURES.items()):
            path = GOLDEN_DIR / f"{name}.json"
            path.write_text(json.dumps(fixture, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"Wrote {len(FIXTURES)} golden fixtures to {GOLDEN_DIR}")
    else:
        print(f"Golden fixtures current: {len(FIXTURES)} fixtures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
