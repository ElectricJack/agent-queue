#!/usr/bin/env python3
"""Check offline knowledge adapter observations, without implementing context policy.

Two adapters ship here. The default snapshot adapter replays sealed synthetic
observations and needs nothing but the fixtures. ``--adapter context-bundle``
replays the same oracle against the real K08 service in a **disposable**
database named with ``--db-url``; it refuses the worker sentinels and the
operator's production database outright.

Both are contract checks, not retrieval benchmarks or model-quality measurements.
See the fixture README.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
import socket
import subprocess
import sys
import urllib.request
from collections import Counter
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Protocol
from unittest.mock import patch
from uuid import uuid4

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURES = ROOT / "tests/fixtures/knowledge"
INTEGRATED = FIXTURES / "integrated"
SCHEMA_VERSION = 1


def canonical_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def seal_manifest(manifest: dict) -> dict:
    """Fixture-author helper: seal every input and oracle field, including budgets."""
    manifest["input_hashes"] = {
        key: sha256_text(canonical_dumps(value))
        for key, value in manifest.items()
        if key != "input_hashes"
    }
    return manifest


def identity(item: dict) -> str:
    return f"{item['record_id']}@{item['revision_id']}:{item['kind']}"


def render_snapshot(items: list[dict]) -> str:
    """Fixture-author rendering only; runtime adapters supply their own rendering."""
    if not items:
        return ""
    return "Contextual evidence (synthetic fixture)\n" + "".join(
        f"[{identity(item)}] {item['evidence']}; {item['authority']}; {item['freshness']}\n"
        f"{item['excerpt']}\n"
        for item in items
    )


class FixtureAdapter(Protocol):
    """K09 boundary: normalize *observed* K08 output, never select or authorize here.

    Inputs exclude the expected oracle and hashes. Output must match observation schema.
    Implementations receive one fixture request and harness label at a time. They own
    their isolated test setup; the runner does not persist bundles, citations or delivery.
    """

    name: str

    def observe(self, inputs: dict, *, harness: str, role: str) -> dict: ...


class SnapshotAdapter:
    name = "synthetic_snapshot"

    def observe(self, inputs: dict, *, harness: str, role: str) -> dict:
        return copy.deepcopy(inputs["observation"])


class OfflineViolation(RuntimeError):
    pass


@contextmanager
def offline_guard(calls: list[str]):
    """Failing transport/process spies; record names only, never credentials/arguments.

    This guards trusted test adapters, not arbitrary untrusted code in a sandbox.
    A swallowed refusal still fails the evaluation. Patches are always restored.
    """

    def refuse(name):
        def blocked(*args, **kwargs):
            calls.append(name)
            raise OfflineViolation("offline adapter attempted external I/O")

        return blocked

    targets = [
        (socket, "create_connection"),
        (socket, "getaddrinfo"),
        (socket, "gethostbyname"),
        (socket, "gethostbyname_ex"),
        (socket, "gethostbyaddr"),
        (socket.socket, "connect"),
        (socket.socket, "connect_ex"),
        (socket.socket, "send"),
        (socket.socket, "sendall"),
        (socket.socket, "sendto"),
        (socket.socket, "sendmsg"),
        (urllib.request, "urlopen"),
        (subprocess, "Popen"),
    ]
    with ExitStack() as stack:
        for obj, attr in targets:
            if hasattr(obj, attr):
                stack.enter_context(patch.object(obj, attr, refuse(attr)))
        yield


def load_schema() -> dict:
    return json.loads((FIXTURES / "manifest.schema.json").read_text(encoding="utf-8"))


def validate_manifest(manifest: dict, schema: dict) -> list[str]:
    errors = ["manifest.schema"] if not Draft202012Validator(schema).is_valid(manifest) else []
    if errors:
        return errors
    hashes = manifest["input_hashes"]
    sections = set(manifest) - {"input_hashes"}
    if set(hashes) != sections or any(
        hashes.get(key) != sha256_text(canonical_dumps(manifest[key])) for key in sections
    ):
        errors.append("manifest.hash")
    refs = [identity(item) for item in manifest["records"]]
    expected = manifest["expected"]
    allowed = expected["allowed_identities"]
    omissions = expected["omitted_reasons"]
    forbidden = expected["forbidden_records"]
    if (
        len(refs) != len(set(refs))
        or set(allowed) & set(omissions)
        or set(refs) != set(allowed) | set(omissions)
        or not set(forbidden) <= set(omissions)
    ):
        errors.append("manifest.identities")
    rules = manifest["delivery_rules"]
    if (rules["role"] == "worker") != (rules["owner_kind"] == "task_attempt"):
        errors.append("manifest.owner")
    stale = manifest.get("stale_claim")
    if stale and (
        stale["claim_epoch"] != rules["claim_epoch"]
        or stale["stale_epoch"] >= stale["claim_epoch"]
        or stale["expected_forbidden_identity"] not in omissions
    ):
        errors.append("manifest.stale_claim")
    return sorted(set(errors))


def check_observation(manifest: dict, observed: dict) -> tuple[list[str], dict]:
    """Independent oracle over exact refs/bytes, citations, leakage, caps and receipts."""
    errors: list[str] = []
    expected = manifest["expected"]
    items = observed["selected"]
    refs = [identity(item) for item in items]
    records = {identity(item): item for item in manifest["records"]}
    if refs != expected["allowed_identities"]:
        errors.append("selection.identities")
    omissions = {item["identity"]: item["reason"] for item in observed["omissions"]}
    if omissions != expected["omitted_reasons"] or len(omissions) != len(observed["omissions"]):
        errors.append("selection.omissions")
    for item in items:
        record = records.get(identity(item))
        if record is None or item["excerpt"] not in record["excerpts"].values():
            errors.append("selection.exact_revision_bytes")
        # A snapshot fixture hashes the excerpt it rendered; an integrated
        # fixture seals the exact revision hash the record store computed, which
        # is the stronger claim -- the adapter cannot substitute a revision.
        sealed = record.get("revision_sha256") if record else None
        if item["content_sha256"] != (sealed or sha256_text(item["excerpt"])):
            errors.append("selection.content_hash")
        if record and any(
            item[key] != record[key] for key in ("evidence", "authority", "freshness")
        ):
            errors.append("selection.trust_labels")
        if record and any(
            key in record and item.get(key) != record[key]
            for key in ("verification", "lifecycle")
        ):
            errors.append("selection.trust_labels")
    rendered = observed["rendered"]
    if sha256_text(rendered) != observed["rendered_sha256"]:
        errors.append("render.content_hash")
    # Exact bytes, and a delivery that names what it carries: a renderer either
    # prints the composite identity (snapshot) or the sealed revision hash
    # (integrated), and it must contain the selected excerpt verbatim.
    for item in items:
        if item["excerpt"] not in rendered or not any(
            token in rendered for token in (identity(item), item["content_sha256"])
        ):
            errors.append("render.selected_payload")
    if any(
        item[key] not in rendered
        for item in items
        for key in ("evidence", "authority", "freshness")
    ):
        errors.append("render.trust_labels")

    # Check *all* output fields, including errors, counts, metadata and citations. Omission
    # identity/reason pairs are internal oracle data; they are never included in reports.
    public = {key: value for key, value in observed.items() if key != "omissions"}
    serialized = json.dumps(public, sort_keys=True, ensure_ascii=False)

    def contains(text: str) -> bool:
        # JSON escapes quotes, backslashes and control characters; test both spellings.
        return text in serialized or json.dumps(text, ensure_ascii=False)[1:-1] in serialized

    forbidden = expected["forbidden_records"]
    forbidden_refs = set(forbidden)
    stale = manifest.get("stale_claim")
    if stale:
        forbidden_refs.add(stale["expected_forbidden_identity"])
    for ref in forbidden_refs:
        record = records[ref]
        terms = [ref, record["record_id"]]
        terms += list(forbidden.get(ref, {}).values())
        # A revision label may be reused on an authorized record, so test full refs and
        # record IDs, plus explicitly forbidden title/body/snippet/error/count markers.
        if any(contains(term) for term in terms):
            errors.append("security.forbidden_identity_or_content")
    selected_excerpts = {item["excerpt"] for item in items}
    for ref in omissions:
        record = records.get(ref)
        if record and (
            contains(ref)
            or any(
                contains(excerpt)
                for excerpt in record["excerpts"].values()
                if excerpt not in selected_excerpts
            )
        ):
            errors.append("selection.omitted_payload")

    rules = manifest["delivery_rules"]
    owner = {key: rules[key] for key in ("owner_kind", "owner_id", "claim_epoch")}
    if observed["owner"] != owner:
        errors.append("execution.stale_owner")
    expected_deliveries = [
        {
            **{key: value for key, value in attempt.items() if key != "expected_new"},
            "new": attempt["expected_new"],
        }
        for attempt in expected["duplicate_delivery"]["attempts"]
    ]
    deliveries = observed["deliveries"]
    if deliveries != expected_deliveries:
        errors.append("delivery.receipts")
    new_counts = Counter(receipt["transport_key"] for receipt in deliveries if receipt["new"])
    if any(count > 1 for count in new_counts.values()):
        errors.append("delivery.duplicate")
    if any(receipt["bundle_id"] != rules["bundle_id"] for receipt in deliveries):
        errors.append("delivery.bundle")
    delivered = any(receipt["final_state"] == "delivered" for receipt in deliveries)
    citations = observed["citations"]
    citation_refs = [
        (item["record_id"], item["revision_id"], item["content_sha256"]) for item in citations
    ]
    selected_refs = (
        [(item["record_id"], item["revision_id"], item["content_sha256"]) for item in items]
        if delivered
        else []
    )
    if citation_refs != selected_refs:
        errors.append("citation.exact_refs_or_duplicates")
    if any(item["owner"] != owner or item["kind"] != "injected" for item in citations):
        errors.append("citation.owner_or_kind")

    budget = manifest["budget"]
    byte_count = len(rendered.encode("utf-8"))
    usage = {
        "method": "upper_bound_bytes:utf8",
        "tokens": byte_count + budget["reserved_tokens"],
        "bytes": byte_count + budget["reserved_bytes"],
    }
    if observed["usage"] != usage:
        errors.append("budget.accounting")
    required_over = (
        budget["reserved_tokens"] > budget["max_tokens"]
        or budget["reserved_bytes"] > budget["max_bytes"]
    )
    diagnostic = "context.required_over_budget" in observed["diagnostics"]
    if diagnostic != required_over or diagnostic != expected["required_budget_diagnostic"]:
        errors.append("budget.required_diagnostic")
    if required_over:
        if items or rendered or citations:
            errors.append("budget.required_overflow_payload")
    elif usage["tokens"] > budget["max_tokens"] or usage["bytes"] > budget["max_bytes"]:
        errors.append("budget.hard_cap")
    metrics = {
        "selected_count": len(items),
        "citation_count": len(citations),
        "added_input_tokens_upper_bound": byte_count,
        "aggregate_tokens_upper_bound": usage["tokens"],
        "aggregate_bytes": usage["bytes"],
        "cap_status": "required_over_budget" if required_over else "within_cap",
    }
    if errors:
        metrics = {}
    return sorted(set(errors)), metrics


def evaluate_manifest(manifest: dict, adapter: FixtureAdapter | None = None) -> dict:
    schema = load_schema()
    errors = validate_manifest(manifest, schema)
    report: dict = {
        "schema_version": SCHEMA_VERSION,
        "status": "fail" if errors else "pass",
        "measurement": "synthetic_contract",
        "context_bundle_integration": "unsupported",
        "model_quality": "unmeasured",
        "source_class": "synthetic",
        "checks": [],
        "errors": errors,
        "external_io_attempts": 0,
    }
    if errors:
        return report
    report["fixture_id"] = manifest["fixture_id"]
    adapter = adapter or SnapshotAdapter()
    observation_schema = {"$ref": "#/$defs/observation", "$defs": schema["$defs"]}
    validator = Draft202012Validator(observation_schema)
    inputs = {
        key: copy.deepcopy(value)
        for key, value in manifest.items()
        if key not in ("expected", "input_hashes")
    }
    previous = None
    for harness in manifest["labels"]["harnesses"]:
        calls: list[str] = []
        failures: list[str] = []
        metrics: dict = {}
        try:
            with offline_guard(calls):
                observed = adapter.observe(
                    copy.deepcopy(inputs), harness=harness, role=manifest["delivery_rules"]["role"]
                )
            if not validator.is_valid(observed):
                failures.append("adapter.observation_schema")
            else:
                failures, metrics = check_observation(manifest, observed)
                projection = canonical_dumps(observed)
                if previous is not None and projection != previous:
                    failures.append("adapter.harness_parity")
                previous = projection
        except Exception:  # noqa: BLE001 - any adapter failure is one code, never its message
            # Exceptions can contain forbidden data or credentials; retain only a code.
            failures.append("adapter.exception")
        if calls:
            failures.append("offline.external_io")
        report["external_io_attempts"] += len(calls)
        report["checks"].append(
            {
                "harness": harness,
                "role": inputs["delivery_rules"]["role"],
                "status": "fail" if failures else "pass",
                "errors": sorted(set(failures)),
                "metrics": metrics,
            }
        )
    if any(check["status"] == "fail" for check in report["checks"]):
        report["status"] = "fail"
    return report


def evaluate_paths(paths: list[Path], adapter: FixtureAdapter | None = None) -> dict:
    reports = []
    seen = set()
    for path in sorted(paths):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            report = evaluate_manifest(manifest, adapter)
            fixture_id = report.get("fixture_id")
            if fixture_id and fixture_id in seen:
                report["status"] = "fail"
                report["errors"].append("manifest.duplicate_fixture")
            seen.add(fixture_id)
        except (OSError, ValueError):
            report = {"status": "fail", "errors": ["manifest.unreadable_or_invalid_json"]}
        reports.append(report)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "pass" if reports and all(r["status"] == "pass" for r in reports) else "fail",
        "adapter": "synthetic_snapshot" if adapter is None else adapter.name,
        "context_bundle_integration": "unsupported",
        "model_quality": "unmeasured",
        "reports": reports,
    }


class AdapterUnavailable(RuntimeError):
    """The integrated adapter cannot run against the database it was given."""


def _refuse_unsafe_database(db_url: str) -> None:
    """Refuse anything that is not a disposable scratch database.

    The integrated adapter creates its schema, so it must never be pointed at a
    worker sentinel or at the database in the operator's config. Both refusals
    are the guard the daemon itself uses, not a second opinion about it.
    """
    if not db_url:
        raise AdapterUnavailable("adapter.context_bundle_requires_disposable_db")
    sentinels = {
        os.environ.get("AQ_DATABASE_URL"),
        os.environ.get("AGENT_QUEUE_DB"),
        os.environ.get("AGENT_QUEUE_DB_URL"),
    }
    if db_url in {value for value in sentinels if value}:
        raise AdapterUnavailable("adapter.context_bundle_refuses_worker_sentinel")
    if db_url.rpartition("/")[2] in PROTECTED_DATABASES:
        raise AdapterUnavailable("adapter.context_bundle_refuses_maintenance_database")
    from src.database.migration_guard import is_production_database

    if is_production_database(db_url):
        raise AdapterUnavailable("adapter.context_bundle_refuses_production_database")


#: Never recreated, whatever ``--db-url`` says.
PROTECTED_DATABASES = frozenset({"", "postgres", "template0", "template1"})


def _admin_dsn(db_url: str) -> str:
    """asyncpg wants the plain scheme, not SQLAlchemy's ``+asyncpg`` suffix."""
    prefix = db_url.rpartition("/")[0]
    return f"{prefix.replace('postgresql+asyncpg://', 'postgresql://')}/postgres"


async def _recreate_disposable_database(db_url: str) -> tuple[str, callable]:
    """Recreate the scratch database named by *db_url* and return it.

    The integrated run seeds, migrates and acknowledges inside this database, so
    it owns it outright: a run that inherited somebody else's rows would report
    another execution's receipts. The maintenance database is never the target.
    """
    import asyncpg

    prefix, _, name = db_url.rpartition("/")
    admin = await asyncpg.connect(_admin_dsn(db_url))
    try:
        if await admin.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name):
            await admin.execute(
                f'DROP DATABASE "{name}" WITH (FORCE)'
            )
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()
    return db_url, lambda: _drop_database(prefix, name)


async def _drop_database(prefix: str, name: str) -> None:
    import asyncpg

    admin = await asyncpg.connect(_admin_dsn(f"{prefix}/{name}"))
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await admin.close()


async def _run_integrated(paths: list[Path], adapter_name: str, db_url: str, *, keep: bool) -> dict:
    """Replay integrated fixtures against the real service, one per fixture.

    The database is created by the caller; this function only seeds, prepares,
    delivers, reads citations back and hands the runner a normalized adapter.
    Nothing here reads the hidden oracle.
    """
    from src.database import Database
    from tests.knowledge_fixture_adapter import (
        ContextBundleFixtureAdapter,
        LocalModelFixtureAdapter,
    )

    adapter_class = {
        "context-bundle": ContextBundleFixtureAdapter,
        "local-model": LocalModelFixtureAdapter,
    }[adapter_name]
    db_url, drop = await _recreate_disposable_database(db_url)
    database = Database(db_url)
    await database.initialize()
    try:
        reports = []
        seen: set[str] = set()
        for path in paths:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            errors = validate_manifest(manifest, load_schema())
            if errors:
                reports.append({
                    "schema_version": SCHEMA_VERSION, "status": "fail", "errors": errors,
                })
                continue
            fixture_id = str(manifest["fixture_id"])
            if fixture_id in seen:
                reports.append({
                    "schema_version": SCHEMA_VERSION, "status": "fail",
                    "errors": ["manifest.duplicate_fixture"],
                })
                continue
            seen.add(fixture_id)
            # One disposable database can host several runs, so the execution
            # identity is namespaced per run. It never reaches the report: the
            # observed owner is the sealed fixture label.
            adapter = await adapter_class.run(
                database, manifest, identity_suffix=f"-{uuid4().hex[:8]}",
            )
            report = evaluate_manifest(manifest, adapter)
            report["context_bundle_integration"] = "integrated"
            report["adapter"] = adapter_class.name
            reports.append(report)
    finally:
        await database.close()
        if not keep:
            await drop()
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "pass" if reports and all(r["status"] == "pass" for r in reports) else "fail",
        "adapter": adapter_class.name,
        "context_bundle_integration": "integrated",
        "model_quality": "unmeasured",
        "reports": reports,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixtures", nargs="*", type=Path, help="Manifest files or directories.")
    parser.add_argument(
        "--adapter", choices=("snapshot", "context-bundle", "local-model"), default="snapshot",
        help="snapshot replays sealed observations; context-bundle and local-model "
             "drive the real service and need --db-url.",
    )
    parser.add_argument(
        "--db-url", default=None,
        help="Disposable PostgreSQL URL for --adapter context-bundle|local-model. "
             "Worker sentinels and the operator database are refused.",
    )
    parser.add_argument(
        "--keep-db", action="store_true",
        help="Leave the recreated scratch database in place for inspection.",
    )
    parser.add_argument("--output", type=Path, help="Write the deterministic JSON report.")
    args = parser.parse_args(argv)
    if args.adapter in ("context-bundle", "local-model"):
        paths = []
        for path in args.fixtures or [INTEGRATED]:
            paths.extend(sorted(path.glob("*.json")) if path.is_dir() else [path])
        try:
            _refuse_unsafe_database(args.db_url)
        except AdapterUnavailable as exc:
            report = {
                "schema_version": SCHEMA_VERSION,
                "status": "unsupported",
                "errors": [str(exc)],
                "model_quality": "unmeasured",
            }
            code = 2
        else:
            report = asyncio.run(
                _run_integrated(paths, args.adapter, args.db_url, keep=args.keep_db)
            )
            code = 0 if report["status"] == "pass" else 1
    else:
        paths = []
        for path in args.fixtures or [FIXTURES / "golden"]:
            paths.extend(sorted(path.glob("*.json")) if path.is_dir() else [path])
        report = evaluate_paths(paths)
        code = 0 if report["status"] == "pass" else 1
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
