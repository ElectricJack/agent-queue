"""Shared vectors for the hosted `main` push attestation verifier (App-mode spec §7.2).

Payloads are built with the daemon's own ``AttestationPayload``, so its canonical
bytes and ``external_id`` are what the stdlib verifier must accept. Every
negative changes one thing about a valid publication and must fail safe:
``configured=true`` and ``attested=false``, which runs the full audit CI.
"""

from __future__ import annotations

import ast
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from src.integration import hosted_attestation as hosted
from src.integration.ci import (
    ATTESTATION_CHECK_NAME,
    AttestationPayload,
    AttestedCheck,
    AttestedWorkflowRun,
)

ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "src/integration/hosted_attestation.py"
SHA = "a" * 40
OTHER_SHA = "b" * 40
APP_ID = 5075923
CI_APP_ID = 15368
REPOSITORY = "ElectricJack/agent-queue"
REPOSITORY_ID = 1160639300
VERSION = "tests-yml-v3"
RECORD_ID = 7000


def _checks(head_sha: str = SHA, producer: int = CI_APP_ID) -> tuple[AttestedCheck, ...]:
    return (
        AttestedCheck(
            name="Tests (default-1/8)",
            check_run_id=101,
            check_suite_id=900,
            producer_app_id=producer,
            head_sha=head_sha,
            conclusion="success",
        ),
        AttestedCheck(
            name="E2E CLI (claims)",
            check_run_id=102,
            check_suite_id=900,
            producer_app_id=producer,
            head_sha=head_sha,
            conclusion="success",
        ),
    )


def _payload(**overrides) -> AttestationPayload:
    head_sha = overrides.get("head_sha", SHA)
    fields = {
        "schema": "aq.integration-attestation.v1",
        "canonical_repository_id": "agent-queue2",
        "repository_id": REPOSITORY_ID,
        "ci_producer_app_id": CI_APP_ID,
        "attestation_app_id": APP_ID,
        "head_sha": head_sha,
        "required_check_set_version": VERSION,
        "checks": _checks(head_sha, overrides.get("ci_producer_app_id", CI_APP_ID)),
        "workflow_runs": (
            AttestedWorkflowRun(
                workflow_run_id=55,
                run_attempt=1,
                check_suite_id=900,
                head_sha=head_sha,
                conclusion="success",
            ),
        ),
    }
    fields.update(overrides)
    return AttestationPayload(**fields)


def _external_id(text: str) -> str:
    return "aq-attestation-v1:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _record(
    text: str,
    *,
    record_id: int = RECORD_ID,
    app_id: int = APP_ID,
    external_id: str | None = None,
    **overrides,
) -> dict:
    record = {
        "id": record_id,
        "name": ATTESTATION_CHECK_NAME,
        "app": {"id": app_id, "slug": "agent-queue-train"},
        "head_sha": SHA,
        "status": "completed",
        "conclusion": "success",
        "external_id": _external_id(text) if external_id is None else external_id,
        "output": {"title": "Attested", "summary": "Attested", "text": text},
    }
    record.update(overrides)
    return record


def _check_run(check: AttestedCheck, **overrides) -> dict:
    run = {
        "id": check.check_run_id,
        "name": check.name,
        "app": {"id": check.producer_app_id, "slug": "github-actions"},
        "head_sha": check.head_sha,
        "status": "completed",
        "conclusion": check.conclusion,
        "check_suite": {"id": check.check_suite_id},
    }
    run.update(overrides)
    return run


class FakeGitHub:
    """The two GitHub endpoints the verifier reads, with Link-header pagination."""

    def __init__(self, *, page_size: int = 100) -> None:
        self.attestations: list[dict] = []
        self.check_runs: dict[int, dict] = {}
        self.requests: list[str] = []
        self.page_size = page_size
        self.next_link_override: str | None = None
        self.error: Exception | None = None

    def __call__(self, url: str) -> tuple[bytes, dict[str, str]]:
        self.requests.append(url)
        if self.error is not None:
            raise self.error
        parts = urlsplit(url)
        assert (parts.scheme, parts.netloc) == ("https", "api.github.com"), url
        query = parse_qs(parts.query)
        listing = f"/repos/{REPOSITORY}/commits/{SHA}/check-runs"
        if parts.path == listing:
            assert query["check_name"] == [ATTESTATION_CHECK_NAME]
            assert query["filter"] == ["all"]
            page = int(query.get("page", ["1"])[0])
            start = (page - 1) * self.page_size
            rows = self.attestations[start : start + self.page_size]
            headers: dict[str, str] = {}
            if start + self.page_size < len(self.attestations):
                following = {key: values[0] for key, values in query.items()}
                following["page"] = str(page + 1)
                target = self.next_link_override or (
                    f"https://api.github.com{listing}?{urlencode(following)}"
                )
                last = f"https://api.github.com{listing}?page=999"
                headers["link"] = f'<{target}>; rel="next", <{last}>; rel="last"'
            body = {"total_count": len(self.attestations), "check_runs": rows}
            return json.dumps(body).encode(), headers
        prefix = f"/repos/{REPOSITORY}/check-runs/"
        if parts.path.startswith(prefix):
            run = self.check_runs.get(int(parts.path[len(prefix) :]))
            if run is None:
                raise hosted.VerificationError(f"GitHub API returned HTTP 404 for {parts.path}")
            return json.dumps(run).encode(), {}
        raise AssertionError(f"unexpected request {url}")


def _publish(github: FakeGitHub, payload: AttestationPayload, **record) -> str:
    """Publish exactly what the daemon's ``AuthenticatedGitHubObserver.publish`` posts."""
    text = payload.canonical_bytes().decode("ascii")
    record.setdefault("external_id", payload.external_id)
    github.attestations.append(_record(text, **record))
    for check in payload.checks:
        github.check_runs.setdefault(check.check_run_id, _check_run(check))
    return text


def _env(**overrides) -> dict[str, str]:
    env = {
        "GH_TOKEN": "ghs_token",
        "REPOSITORY": REPOSITORY,
        "REPOSITORY_ID": str(REPOSITORY_ID),
        "SHA": SHA,
        "APP_ID": str(APP_ID),
        "CHECK_VERSION": VERSION,
    }
    env.update(overrides)
    return env


def _valid() -> FakeGitHub:
    github = FakeGitHub()
    _publish(github, _payload())
    return github


def _refused(github: FakeGitHub, env: dict[str, str] | None = None) -> str:
    verdict = hosted.verify(_env() if env is None else env, github)
    assert (verdict.configured, verdict.attested) == (True, False), verdict
    assert verdict.reason
    return verdict.reason


# -- accepted -----------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(_payload(), id="two-checks-one-suite"),
        pytest.param(
            _payload(
                checks=(
                    AttestedCheck(
                        name="Tests (café ✓)",
                        check_run_id=301,
                        check_suite_id=910,
                        producer_app_id=CI_APP_ID,
                        head_sha=SHA,
                        conclusion="success",
                    ),
                    AttestedCheck(
                        name='E2E "quoted" \\ name',
                        check_run_id=302,
                        check_suite_id=911,
                        producer_app_id=CI_APP_ID,
                        head_sha=SHA,
                        conclusion="success",
                    ),
                ),
                workflow_runs=(
                    AttestedWorkflowRun(
                        workflow_run_id=61,
                        run_attempt=2,
                        check_suite_id=910,
                        head_sha=SHA,
                        conclusion="success",
                    ),
                    AttestedWorkflowRun(
                        workflow_run_id=62,
                        run_attempt=1,
                        check_suite_id=911,
                        head_sha=SHA,
                        conclusion="success",
                    ),
                ),
                required_check_set_version="vérsion/2",
            ),
            id="non-ascii-escaped-two-suites",
        ),
    ],
)
def test_accepts_exactly_what_the_daemon_publishes(payload: AttestationPayload) -> None:
    github = FakeGitHub()
    _publish(github, payload)

    verdict = hosted.verify(_env(CHECK_VERSION=payload.required_check_set_version), github)

    assert (verdict.configured, verdict.attested) == (True, True), verdict.reason
    rechecked = {url.rsplit("/", 1)[1] for url in github.requests if "/check-runs/" in url}
    assert rechecked == {str(check.check_run_id) for check in payload.checks}


def test_ignores_other_apps_and_names_and_takes_the_newest_trusted_record() -> None:
    github = FakeGitHub()
    text = _publish(github, _payload(), record_id=10)
    github.attestations.append(_record(text, record_id=50, app_id=999, conclusion="failure"))
    github.attestations.append(
        _record(text, record_id=60, name="Agent Queue Integration Attestation (copy)")
    )

    verdict = hosted.verify(_env(), github)

    assert verdict.attested, verdict.reason
    assert "10" in verdict.reason


# -- not configured -------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [{"APP_ID": ""}, {"CHECK_VERSION": ""}, {"APP_ID": "", "CHECK_VERSION": ""}],
    ids=["app-id", "check-version", "both"],
)
def test_unset_variables_mean_not_configured_and_no_api_calls(overrides) -> None:
    github = _valid()

    verdict = hosted.verify(_env(**overrides), github)

    assert (verdict.configured, verdict.attested) == (False, False)
    assert github.requests == []
    for name, variable in (
        ("APP_ID", "AQ_INTEGRATION_ATTESTATION_APP_ID"),
        ("CHECK_VERSION", "AQ_INTEGRATION_REQUIRED_CHECK_VERSION"),
    ):
        assert (variable in verdict.reason) is (name in overrides)


# -- rejected -------------------------------------------------------------------


def test_rejects_an_attestation_from_another_app() -> None:
    github = FakeGitHub()
    _publish(github, _payload(), app_id=999)
    assert "no " + ATTESTATION_CHECK_NAME in _refused(github)


def test_rejects_a_payload_naming_another_attestation_app() -> None:
    github = FakeGitHub()
    _publish(github, _payload(attestation_app_id=999))
    assert "attestation_app_id" in _refused(github)


def test_rejects_the_wrong_check_set_version() -> None:
    assert "required_check_set_version" in _refused(_valid(), _env(CHECK_VERSION="tests-yml-v4"))


def test_rejects_a_check_run_on_another_head() -> None:
    github = FakeGitHub()
    _publish(github, _payload(), head_sha=OTHER_SHA)
    assert "head" in _refused(github)


def test_rejects_a_payload_for_another_head() -> None:
    github = FakeGitHub()
    _publish(github, _payload(head_sha=OTHER_SHA))
    assert "head_sha" in _refused(github)


def test_rejects_another_repository() -> None:
    assert "repository_id" in _refused(_valid(), _env(REPOSITORY_ID="42"))


def test_rejects_a_ci_producer_equal_to_the_attestation_app() -> None:
    github = FakeGitHub()
    _publish(github, _payload(ci_producer_app_id=APP_ID))
    assert "ci_producer_app_id" in _refused(github)


@pytest.mark.parametrize(
    ("status", "conclusion"),
    [("in_progress", None), ("completed", "failure"), ("completed", "neutral")],
)
def test_rejects_an_unsuccessful_attestation(status, conclusion) -> None:
    github = FakeGitHub()
    _publish(github, _payload(), status=status, conclusion=conclusion)
    _refused(github)


def test_newest_trusted_record_wins_even_when_older_ones_succeeded() -> None:
    github = FakeGitHub()
    text = _publish(github, _payload(), record_id=10)
    github.attestations.append(_record(text, record_id=11, conclusion="failure"))
    assert "11" in _refused(github)


def _noncanonical_texts() -> list:
    canonical = json.loads(_payload().canonical_bytes())
    duplicate = _payload().canonical_bytes().decode()
    duplicate = duplicate[:-1] + ',"schema":"aq.integration-attestation.v1"}'
    return [
        pytest.param(json.dumps(canonical, indent=2, sort_keys=True), id="indented"),
        pytest.param(
            json.dumps(dict(reversed(list(canonical.items()))), separators=(",", ":")),
            id="unsorted",
        ),
        pytest.param(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")) + "\n", id="newline"
        ),
        pytest.param(duplicate, id="duplicate-key"),
        pytest.param("not json", id="not-json"),
    ]


@pytest.mark.parametrize("text", _noncanonical_texts())
def test_rejects_noncanonical_text_even_with_a_matching_digest(text: str) -> None:
    github = FakeGitHub()
    payload = _payload()
    _publish(github, payload)
    github.attestations[0]["output"]["text"] = text
    github.attestations[0]["external_id"] = _external_id(text)
    _refused(github)


def test_rejects_a_missing_payload() -> None:
    github = _valid()
    github.attestations[0]["output"] = {"title": "Attested", "summary": "Attested"}
    assert "payload" in _refused(github)


@pytest.mark.parametrize(
    "external_id",
    [
        "aq-attestation-v1:" + "0" * 64,
        hashlib.sha256(_payload().canonical_bytes()).hexdigest(),
        None,
    ],
    ids=["other-digest", "missing-prefix", "absent"],
)
def test_rejects_the_wrong_external_id(external_id) -> None:
    github = _valid()
    github.attestations[0]["external_id"] = external_id
    assert "external_id" in _refused(github)


def _recanonicalized(mutate) -> str:
    document = json.loads(_payload().canonical_bytes())
    mutate(document)
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda d: d.update(schema="aq.integration-attestation.v2"), id="schema"),
        pytest.param(lambda d: d.update(extra=1), id="extra-field"),
        pytest.param(lambda d: d.pop("workflow_runs"), id="missing-field"),
        pytest.param(lambda d: d.update(repository_id=str(REPOSITORY_ID)), id="string-id"),
        pytest.param(lambda d: d.update(repository_id=True), id="bool-id"),
        pytest.param(lambda d: d.update(checks=[]), id="no-checks"),
        pytest.param(lambda d: d["checks"][0].update(conclusion="failure"), id="check-conclusion"),
        pytest.param(lambda d: d["checks"][1].update(name=d["checks"][0]["name"]), id="dup-name"),
        pytest.param(lambda d: d["checks"][0].update(check_suite_id=901), id="suite-coverage"),
        pytest.param(lambda d: d["checks"][0].update(extra=1), id="check-extra-field"),
        pytest.param(lambda d: d["workflow_runs"][0].update(head_sha=OTHER_SHA), id="run-head"),
    ],
)
def test_rejects_payloads_the_daemon_model_refuses(mutate) -> None:
    text = _recanonicalized(mutate)
    with pytest.raises(ValueError):
        AttestationPayload.from_canonical_bytes(text.encode())
    github = _valid()
    github.attestations[0]["output"]["text"] = text
    github.attestations[0]["external_id"] = _external_id(text)
    _refused(github)


def test_rejects_a_check_attributed_to_another_producer() -> None:
    text = _recanonicalized(lambda d: d["checks"][0].update(producer_app_id=999))
    github = _valid()
    github.attestations[0]["output"]["text"] = text
    github.attestations[0]["external_id"] = _external_id(text)
    assert "producer_app_id" in _refused(github)


@pytest.mark.parametrize(
    ("overrides", "problem"),
    [
        ({"app": {"id": 999}}, "app"),
        ({"conclusion": "failure"}, "conclusion"),
        ({"status": "in_progress", "conclusion": None}, "conclusion"),
        ({"name": "Tests (renamed)"}, "name"),
        ({"head_sha": OTHER_SHA}, "head_sha"),
        ({"check_suite": {"id": 901}}, "check_suite"),
    ],
    ids=["wrong-app", "wrong-conclusion", "not-completed", "name", "head", "suite"],
)
def test_rejects_an_attested_check_that_does_not_reverify(overrides, problem) -> None:
    github = _valid()
    github.check_runs[102].update(overrides)
    reason = _refused(github)
    assert "E2E CLI (claims)" in reason and problem in reason


def test_rejects_an_attested_check_that_no_longer_exists() -> None:
    github = _valid()
    del github.check_runs[101]
    assert "404" in _refused(github)


# -- pagination -------------------------------------------------------------------


def test_finds_the_attestation_on_a_later_page() -> None:
    github = FakeGitHub(page_size=1)
    text = _publish(github, _payload(), record_id=30)
    github.attestations.insert(0, _record(text, record_id=40, app_id=999))
    github.attestations.insert(0, _record(text, record_id=50, name="Other"))

    verdict = hosted.verify(_env(), github)

    assert verdict.attested, verdict.reason
    assert sum("/commits/" in url for url in github.requests) == 3


def test_a_newer_failure_on_a_later_page_wins() -> None:
    github = FakeGitHub(page_size=1)
    text = _publish(github, _payload(), record_id=30)
    github.attestations.append(_record(text, record_id=31, conclusion="failure"))
    _refused(github)


def test_refuses_a_pagination_link_off_the_api_host() -> None:
    github = FakeGitHub(page_size=1)
    text = _publish(github, _payload(), record_id=30)
    github.attestations.append(_record(text, record_id=31))
    github.next_link_override = "https://evil.example/repos/x/check-runs?page=2"
    assert "pagination" in _refused(github)
    assert len(github.requests) == 1


def test_refuses_an_unbounded_listing() -> None:
    github = FakeGitHub(page_size=1)
    text = _publish(github, _payload(), record_id=1)
    for record_id in range(2, hosted.MAX_PAGES + 2):
        github.attestations.append(_record(text, record_id=record_id, app_id=999))
    assert "pages" in _refused(github)


# -- inputs and errors fail safe --------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"APP_ID": "abc"},
        {"APP_ID": " 5075923"},
        {"APP_ID": "0"},
        {"REPOSITORY_ID": ""},
        {"SHA": "A" * 40},
        {"SHA": ""},
        {"REPOSITORY": "not a repository"},
    ],
    ids=["app-text", "app-space", "app-zero", "repo-id", "sha-case", "sha-empty", "repository"],
)
def test_malformed_inputs_are_configured_but_unattested(overrides) -> None:
    github = _valid()
    _refused(github, _env(**overrides))
    assert github.requests == []


def test_api_errors_are_unattested() -> None:
    github = _valid()
    github.error = hosted.VerificationError("GitHub API returned HTTP 502")
    assert "502" in _refused(github)


def test_unexpected_errors_are_unattested() -> None:
    github = _valid()
    github.error = RuntimeError("boom")
    assert "boom" in _refused(github)


def test_malformed_api_json_is_unattested() -> None:
    def github(url: str) -> tuple[bytes, dict[str, str]]:
        return b'{"check_runs": "nope"}', {}

    _refused(github)


# -- step outputs -------------------------------------------------------------------


def _outputs(path: Path) -> list[str]:
    return path.read_text().splitlines()


def test_main_writes_outputs_and_a_one_line_summary(tmp_path: Path) -> None:
    output, summary = tmp_path / "output", tmp_path / "summary"
    env = _env(GITHUB_OUTPUT=str(output), GITHUB_STEP_SUMMARY=str(summary))

    assert hosted.main(env, _valid()) == 0

    lines = _outputs(output)
    assert lines[:2] == ["attested=true", "configured=true"]
    assert lines[2].startswith("reason=") and len(lines) == 3
    assert len(summary.read_text().splitlines()) == 1


def test_main_keeps_an_unattested_reason_on_one_output_line(tmp_path: Path) -> None:
    output, summary = tmp_path / "output", tmp_path / "summary"
    env = _env(GITHUB_OUTPUT=str(output), GITHUB_STEP_SUMMARY=str(summary))
    github = _valid()
    github.error = hosted.VerificationError("bad\nattested=true\r\nconfigured=false")

    assert hosted.main(env, github) == 0

    lines = _outputs(output)
    assert lines[:2] == ["attested=false", "configured=true"]
    assert len(lines) == 3 and lines[2].startswith("reason=")
    assert len(summary.read_text().splitlines()) == 1


def test_script_runs_alone_on_a_bare_runner(tmp_path: Path) -> None:
    """The workflow's sparse checkout holds this one file; nothing from src is importable."""
    script = tmp_path / "src/integration/hosted_attestation.py"
    script.parent.mkdir(parents=True)
    shutil.copy(VERIFIER, script)
    output, summary = tmp_path / "output", tmp_path / "summary"
    env = {
        "PATH": "/usr/bin:/bin",
        "GITHUB_OUTPUT": str(output),
        "GITHUB_STEP_SUMMARY": str(summary),
        **_env(APP_ID="", GH_TOKEN=""),
    }

    completed = subprocess.run(
        [sys.executable, "-I", "src/integration/hosted_attestation.py"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert _outputs(output)[:2] == ["attested=false", "configured=false"]
    assert "not configured" in summary.read_text()


def test_verifier_imports_only_the_standard_library() -> None:
    tree = ast.parse(VERIFIER.read_text())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            modules.add((node.module or "").split(".")[0])
    assert modules
    assert modules <= set(sys.stdlib_module_names) | {"__future__"}, modules


def test_shared_constants_match_the_daemon() -> None:
    assert hosted.ATTESTATION_CHECK_NAME == ATTESTATION_CHECK_NAME
    payload = _payload()
    assert payload.external_id == hosted.EXTERNAL_ID_PREFIX + hashlib.sha256(
        payload.canonical_bytes()
    ).hexdigest()
    assert json.loads(payload.canonical_bytes())["schema"] == hosted.ATTESTATION_SCHEMA
