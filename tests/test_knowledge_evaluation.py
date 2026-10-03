"""Offline knowledge fixtures: exact output, privacy, caps and adapter compatibility."""

from __future__ import annotations

import copy
import json
import runpy
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/evaluate-knowledge.py"
API = runpy.run_path(str(SCRIPT))
FIXTURES = ROOT / "tests/fixtures/knowledge"
GOLDEN = sorted((FIXTURES / "golden").glob("*.json"))
NEGATIVE = sorted((FIXTURES / "negative").glob("*.json"))


@pytest.fixture(autouse=True)
def _pg_backend():
    """File-only contract checks; no test database lease."""


def load(name="current-plus-superseded"):
    return json.loads((FIXTURES / "golden" / f"{name}.json").read_text())


def errors(report):
    return set(report["errors"]) | {
        error for check in report["checks"] for error in check["errors"]
    }


def evaluate(manifest, adapter=None):
    return API["evaluate_manifest"](manifest, adapter)


def reseal(manifest):
    return API["seal_manifest"](manifest)


def rerender(observation, fixture):
    rendered = API["render_snapshot"](observation["selected"])
    observation["rendered"] = rendered
    observation["rendered_sha256"] = API["sha256_text"](rendered)
    count = len(rendered.encode("utf-8"))
    observation["usage"].update(
        tokens=count + fixture["budget"]["reserved_tokens"],
        bytes=count + fixture["budget"]["reserved_bytes"],
    )


@pytest.mark.parametrize("path", GOLDEN, ids=lambda p: p.stem)
def test_golden_contract(path):
    fixture = json.loads(path.read_text())
    report = evaluate(fixture)
    assert report["status"] == "pass", report
    assert report["context_bundle_integration"] == "unsupported"
    assert report["model_quality"] == "unmeasured"
    assert report["measurement"] == "synthetic_contract"
    assert report["external_io_attempts"] == 0
    assert {check["harness"] for check in report["checks"]} == {
        "claude",
        "codex",
        "opencode",
        "local",
    }
    assert fixture["synthetic"] is True and fixture["source_class"] == "synthetic"


@pytest.mark.parametrize("path", NEGATIVE, ids=lambda p: p.stem)
def test_intentionally_failing_golden_observations(path):
    report = evaluate(json.loads(path.read_text()))
    assert report["status"] == "fail"
    assert report["errors"] == []  # Valid sealed manifests, independently wrong observations.
    assert all(check["metrics"] == {} for check in report["checks"])


@pytest.mark.parametrize(
    "section",
    [
        "schema_version",
        "fixture_id",
        "scenario",
        "source_class",
        "records",
        "expected",
        "budget",
        "delivery_rules",
        "labels",
        "observation",
    ],
)
def test_every_input_and_oracle_section_is_sealed(section):
    fixture = load()
    fixture["input_hashes"][section] = "0" * 64
    assert errors(evaluate(fixture)) == {"manifest.hash"}


@pytest.mark.parametrize("mutation", ["missing_hash", "extra_hash", "changed_budget"])
def test_incomplete_or_tampered_manifest_hashes(mutation):
    fixture = load()
    if mutation == "missing_hash":
        fixture["input_hashes"].pop("budget")
    elif mutation == "extra_hash":
        fixture["input_hashes"]["external"] = "a" * 64
    else:
        fixture["budget"]["reserved_tokens"] += 1
    assert "manifest.hash" in errors(evaluate(fixture))


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_schema",
        "extra_field",
        "unsynthetic",
        "unknown_harness",
        "no_harnesses",
        "negative_budget",
        "fake_tokenizer",
        "bad_observation",
    ],
)
def test_bad_manifest_schema(mutation):
    fixture = load()
    if mutation == "unknown_schema":
        fixture["schema_version"] = 2
    elif mutation == "extra_field":
        fixture["live_provider"] = "forbidden"
    elif mutation == "unsynthetic":
        fixture["synthetic"] = False
    elif mutation == "unknown_harness":
        fixture["labels"]["harnesses"] = ["invented"]
    elif mutation == "no_harnesses":
        fixture["labels"]["harnesses"] = []
    elif mutation == "negative_budget":
        fixture["budget"]["max_tokens"] = -1
    elif mutation == "fake_tokenizer":
        fixture["budget"]["token_unit"] = "exact:whitespace"
    else:
        fixture["observation"]["selected"] = "not an array"
    assert errors(evaluate(reseal(fixture))) == {"manifest.schema"}


def test_invalid_identity_and_execution_requests():
    fixture = load()
    fixture["records"].append(copy.deepcopy(fixture["records"][0]))
    assert "manifest.identities" in errors(evaluate(reseal(fixture)))
    fixture = load()
    fixture["delivery_rules"]["role"] = "supervisor"
    assert "manifest.owner" in errors(evaluate(reseal(fixture)))
    fixture = load("stale-claim-recycled-slot")
    fixture["stale_claim"]["stale_epoch"] = 2
    assert "manifest.stale_claim" in errors(evaluate(reseal(fixture)))


@pytest.mark.parametrize(
    "field", ["body", "title", "snippet", "error", "count", "unicode", "escaped"]
)
def test_leakage_in_rendering_and_metadata_is_refused_and_not_echoed(field):
    fixture = load("cross-project-secret")
    secret = next(iter(fixture["expected"]["forbidden_records"]))
    marker = "秘密キー" if field == "unicode" else f"synthetic-private-{field}-marker"
    if field == "escaped":
        marker += ' "quoted"\n'
    fixture["expected"]["forbidden_records"][secret][
        "body" if field in ("unicode", "escaped") else field
    ] = marker
    observed = fixture["observation"]
    observed["metadata"][field] = marker
    report = evaluate(reseal(fixture))
    assert "security.forbidden_identity_or_content" in errors(report)
    assert marker not in json.dumps(report, ensure_ascii=False)
    assert all(check["metrics"] == {} for check in report["checks"])


def test_unauthorized_selected_identity_fails_even_without_forbidden_words():
    fixture = load("cross-project-secret")
    observed = fixture["observation"]
    observed["selected"][0]["record_id"] = "rec-private-global-link"
    report = evaluate(reseal(fixture))
    assert "security.forbidden_identity_or_content" in errors(report)
    assert "selection.identities" in errors(report)
    assert "rec-private-global-link" not in json.dumps(report)


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("revision", "selection.identities"),
        ("bytes", "selection.exact_revision_bytes"),
        ("hash", "selection.content_hash"),
        ("trust", "selection.trust_labels"),
        ("render_hash", "render.content_hash"),
        ("render_payload", "render.selected_payload"),
        ("omission", "selection.omissions"),
        ("stale_in_render", "selection.omitted_payload"),
    ],
)
def test_exact_selection_and_history(mutation, code):
    fixture = load()
    observed = fixture["observation"]
    item = observed["selected"][0]
    if mutation == "revision":
        item["revision_id"] = "rev-0006"
    elif mutation == "bytes":
        item["excerpt"] = "Invented replacement bytes"
        item["content_sha256"] = API["sha256_text"](item["excerpt"])
    elif mutation == "hash":
        item["content_sha256"] = "0" * 64
    elif mutation == "trust":
        item["authority"] = "verified"
    elif mutation == "render_hash":
        observed["rendered_sha256"] = "0" * 64
    elif mutation == "render_payload":
        observed["rendered"] = ""
    elif mutation == "omission":
        observed["omissions"][0]["reason"] = "silently-current"
    else:
        observed["rendered"] += fixture["records"][1]["excerpts"]["body"]
    assert code in errors(evaluate(reseal(fixture)))


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("duplicate", "citation.exact_refs_or_duplicates"),
        ("revision", "citation.exact_refs_or_duplicates"),
        ("hash", "citation.exact_refs_or_duplicates"),
        ("owner", "citation.owner_or_kind"),
        ("claim", "execution.stale_owner"),
        ("delivery_duplicate", "delivery.duplicate"),
        ("bundle", "delivery.bundle"),
    ],
)
def test_citation_and_observed_delivery_invariants(mutation, code):
    fixture = load("duplicate-delivery-event")
    observed = fixture["observation"]
    if mutation == "duplicate":
        observed["citations"].append(copy.deepcopy(observed["citations"][0]))
    elif mutation in ("revision", "hash"):
        key = "revision_id" if mutation == "revision" else "content_sha256"
        observed["citations"][0][key] = "rev-0006" if mutation == "revision" else "a" * 64
    elif mutation == "owner":
        observed["citations"][0]["owner"]["claim_epoch"] -= 1
    elif mutation == "claim":
        observed["owner"]["claim_epoch"] -= 1
    elif mutation == "delivery_duplicate":
        observed["deliveries"][1]["new"] = True
    else:
        observed["deliveries"][0]["bundle_id"] = "bundle-another-claim"
    assert code in errors(evaluate(reseal(fixture)))


@pytest.mark.parametrize("state", ["prepared", "failed", "unknown"])
def test_unobserved_delivery_has_no_injected_citations(state):
    fixture = load()
    fixture["expected"]["duplicate_delivery"]["attempts"][0]["final_state"] = state
    fixture["observation"]["deliveries"][0]["final_state"] = state
    fixture["observation"]["citations"] = []
    assert evaluate(reseal(fixture))["status"] == "pass"
    item = fixture["observation"]["selected"][0]
    fixture["observation"]["citations"].append(
        {
            **{key: item[key] for key in ("record_id", "revision_id", "content_sha256")},
            "kind": "injected",
            "owner": fixture["observation"]["owner"],
        }
    )
    assert "citation.exact_refs_or_duplicates" in errors(evaluate(reseal(fixture)))


def test_aggregate_budget_includes_wrappers_reserves_and_multibyte_utf8():
    fixture = load()
    observed = fixture["observation"]
    excerpt = "確認🧪"
    fixture["records"][0]["excerpts"]["body"] = excerpt
    observed["selected"][0]["excerpt"] = excerpt
    observed["selected"][0]["content_sha256"] = API["sha256_text"](excerpt)
    observed["citations"][0]["content_sha256"] = observed["selected"][0]["content_sha256"]
    rerender(observed, fixture)
    report = evaluate(reseal(fixture))
    assert report["status"] == "pass", report
    metric = report["checks"][0]["metrics"]
    assert metric["aggregate_bytes"] == len(observed["rendered"].encode()) + 64
    fixture["budget"]["max_tokens"] = len(observed["rendered"]) + 64
    assert "budget.hard_cap" in errors(evaluate(reseal(fixture)))
    fixture["budget"]["max_tokens"] = 4096
    fixture["budget"]["max_bytes"] = len(observed["rendered"].encode()) + 63
    assert "budget.hard_cap" in errors(evaluate(reseal(fixture)))
    fixture["budget"]["max_bytes"] = 16384
    observed["usage"]["tokens"] -= 1
    assert "budget.accounting" in errors(evaluate(reseal(fixture)))


def test_required_content_overflow_is_diagnostic_not_a_cap_claim():
    fixture = load("required-content-over-budget")
    report = evaluate(fixture)
    assert report["status"] == "pass"
    assert report["checks"][0]["metrics"]["cap_status"] == "required_over_budget"
    observed = fixture["observation"]
    observed["rendered"] = "knowledge despite overflow"
    assert "budget.required_overflow_payload" in errors(evaluate(reseal(fixture)))


def test_worker_and_supervisor_have_identical_authorized_payload():
    worker = load("worker-bounded-project-set")
    supervisor = load("supervisor-global-project-set")
    assert worker["observation"]["selected"] == supervisor["observation"]["selected"]
    assert worker["observation"]["rendered"] == supervisor["observation"]["rendered"]
    assert evaluate(worker)["checks"][0]["role"] == "worker"
    assert evaluate(supervisor)["checks"][0]["role"] == "supervisor"


def test_adapter_receives_inputs_without_expected_oracle_and_parity_is_checked():
    class Adapter:
        name = "test"

        def observe(self, inputs, *, harness, role):
            assert "expected" not in inputs and "input_hashes" not in inputs
            assert role == "worker"
            output = inputs["observation"]
            if harness == "local":
                output["metadata"]["transport_note"] = "different"
            return output

    report = evaluate(load(), Adapter())
    assert "adapter.harness_parity" in errors(report)
    assert report["checks"][0]["status"] == "pass"


@pytest.mark.parametrize("transport", ["socket", "connect_ex", "dns", "urlopen", "process"])
def test_transport_spies_fail_even_if_adapter_swallows_refusal(transport):
    original = socket.socket.connect

    class Adapter:
        name = "external-io-probe"

        def observe(self, inputs, *, harness, role):
            try:
                if transport == "socket":
                    with socket.socket() as connection:
                        connection.connect(("127.0.0.1", 1))
                elif transport == "connect_ex":
                    with socket.socket() as connection:
                        connection.connect_ex(("127.0.0.1", 1))
                elif transport == "dns":
                    socket.getaddrinfo("example.invalid", 443)
                elif transport == "urlopen":
                    import urllib.request

                    urllib.request.urlopen("https://example.invalid")
                else:
                    subprocess.Popen(["false"])
            except API["OfflineViolation"]:
                pass
            return inputs["observation"]

    report = evaluate(load(), Adapter())
    assert "offline.external_io" in errors(report)
    assert report["external_io_attempts"] == 4
    assert socket.socket.connect is original


def test_adapter_exceptions_do_not_disclose_private_values():
    class Adapter:
        name = "broken"

        def observe(self, inputs, *, harness, role):
            raise RuntimeError("vault-key-77 credentials")

    report = evaluate(load("cross-project-secret"), Adapter())
    assert errors(report) == {"adapter.exception"}
    assert "vault-key-77" not in json.dumps(report)


def test_malformed_adapter_observation_fails():
    class Adapter:
        name = "malformed"

        def observe(self, inputs, *, harness, role):
            return {"arbitrary": "not an observation"}

    assert errors(evaluate(load(), Adapter())) == {"adapter.observation_schema"}


def test_cli_is_deterministic_and_missing_context_bundle_is_unsupported(tmp_path):
    output = tmp_path / "report.json"
    command = [sys.executable, str(SCRIPT), "--output", str(output)]
    assert subprocess.run(command, capture_output=True).returncode == 0
    first = output.read_bytes()
    assert subprocess.run(command, capture_output=True).returncode == 0
    assert output.read_bytes() == first
    assert json.loads(first)["model_quality"] == "unmeasured"
    unavailable = subprocess.run(
        [sys.executable, str(SCRIPT), "--adapter", "context-bundle"],
        capture_output=True,
        text=True,
    )
    assert unavailable.returncode == 2
    assert json.loads(unavailable.stdout)["status"] == "unsupported"


def test_bad_json_missing_manifest_and_duplicate_fixture_fail(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{bad")
    assert API["evaluate_paths"]([bad])["status"] == "fail"
    assert API["evaluate_paths"]([tmp_path / "missing.json"])["status"] == "fail"
    assert API["evaluate_paths"]([])["status"] == "fail"
    good = GOLDEN[0]
    report = API["evaluate_paths"]([good, good])
    assert report["status"] == "fail"
    assert report["reports"][1]["errors"] == ["manifest.duplicate_fixture"]
