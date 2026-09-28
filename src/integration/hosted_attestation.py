"""Hosted verifier for the `main` push audit (App-mode integration train spec §7.2).

`.github/workflows/main-attestation.yml` runs this file from a sparse checkout on a
bare runner, so it uses the standard library only and imports nothing from `src`.
It decides whether the pushed `main` SHA carries a valid integration attestation,
mirroring the daemon's `select_trusted_attestation` (`src/integration/ci.py`):

1. `APP_ID` or `CHECK_VERSION` empty: the repository is not in App mode yet, so
   `configured=false` and no fallback CI runs.
2. List the SHA's `Agent Queue Integration Attestation` check runs (paginated),
   keep those from `APP_ID`, and take the newest by id.
3. It must be completed, successful and on the SHA. `output.text` must be the
   canonical JSON of its own parse, and `external_id` its digest.
4. The payload must name this repository, App, head and check-set version, and a
   CI producer distinct from the attestation App.
5. Every attested check run is read back from GitHub and must still match.
6. Otherwise `attested=true`. Any error means `attested=false` with the reason,
   so the audit fails safe: `unattested-ci` runs the full suite.

Outputs go to `$GITHUB_OUTPUT` (`attested`, `configured`, `reason`) and a one-line
summary to `$GITHUB_STEP_SUMMARY`. The script exits 0 whatever the verdict, so the
workflow's `unattested-ci` job can read the outputs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

ATTESTATION_CHECK_NAME = "Agent Queue Integration Attestation"
ATTESTATION_SCHEMA = "aq.integration-attestation.v1"
EXTERNAL_ID_PREFIX = "aq-attestation-v1:"
APP_ID_VARIABLE = "AQ_INTEGRATION_ATTESTATION_APP_ID"
CHECK_VERSION_VARIABLE = "AQ_INTEGRATION_REQUIRED_CHECK_VERSION"
DEFAULT_API_URL = "https://api.github.com"
MAX_PAGES = 20
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 30

_SHA = re.compile(r"[0-9a-f]{40}")
_POSITIVE_DECIMAL = re.compile(r"[1-9][0-9]*")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_PAYLOAD_FIELDS = frozenset(
    {
        "schema",
        "canonical_repository_id",
        "repository_id",
        "ci_producer_app_id",
        "attestation_app_id",
        "head_sha",
        "required_check_set_version",
        "checks",
        "workflow_runs",
    }
)
_CHECK_FIELDS = frozenset(
    {"name", "check_run_id", "check_suite_id", "producer_app_id", "head_sha", "conclusion"}
)
_WORKFLOW_RUN_FIELDS = frozenset(
    {"workflow_run_id", "run_attempt", "check_suite_id", "head_sha", "conclusion"}
)

HttpGet = Callable[[str], tuple[bytes, Mapping[str, str]]]
"""GET an absolute URL; return the body and lower-cased response headers."""


class VerificationError(Exception):
    """The SHA is not attested; the message is the step's `reason`."""


@dataclass(frozen=True)
class Verdict:
    configured: bool
    attested: bool
    reason: str


def verify(environ: Mapping[str, str], http_get: HttpGet) -> Verdict:
    unset = [
        variable
        for key, variable in (("APP_ID", APP_ID_VARIABLE), ("CHECK_VERSION", CHECK_VERSION_VARIABLE))
        if environ.get(key, "") == ""
    ]
    if unset:
        return Verdict(False, False, f"not configured: {' and '.join(unset)} unset")
    try:
        return Verdict(True, True, _verify_configured(environ, http_get))
    except VerificationError as exc:
        return Verdict(True, False, str(exc))
    except Exception as exc:  # noqa: BLE001 - any failure must run the audit CI
        return Verdict(True, False, f"verifier error: {type(exc).__name__}: {exc}")


def _verify_configured(environ: Mapping[str, str], http_get: HttpGet) -> str:
    app_id = _positive_decimal(environ["APP_ID"], "APP_ID")
    version = environ["CHECK_VERSION"]
    repository_id = _positive_decimal(environ.get("REPOSITORY_ID", ""), "REPOSITORY_ID")
    sha = environ.get("SHA", "")
    if _SHA.fullmatch(sha) is None:
        raise VerificationError("SHA is not a 40-character lowercase hex commit")
    repository = environ.get("REPOSITORY", "")
    if _REPOSITORY.fullmatch(repository) is None:
        raise VerificationError("REPOSITORY is not owner/name")
    api = environ.get("GITHUB_API_URL") or DEFAULT_API_URL
    if not api.startswith("https://"):
        raise VerificationError("GITHUB_API_URL is not https")
    api = api.rstrip("/")

    listing = (
        f"{api}/repos/{repository}/commits/{sha}/check-runs"
        f"?check_name={urllib.parse.quote(ATTESTATION_CHECK_NAME, safe='')}"
        "&filter=all&per_page=100"
    )
    record_id, record = _newest_trusted(_paged_check_runs(http_get, api, listing), app_id, sha)
    payload = _canonical_payload(record, record_id, sha)
    _require_identity(
        payload, repository_id=repository_id, app_id=app_id, sha=sha, version=version
    )
    for check in payload["checks"]:
        _reverify(http_get, api, repository, sha, payload["ci_producer_app_id"], check)
    return (
        f"attested by check run {record_id} from App {app_id}; "
        f"{len(payload['checks'])} checks re-verified"
    )


def _paged_check_runs(http_get: HttpGet, api: str, url: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for _ in range(MAX_PAGES):
        body, headers = http_get(url)
        page = _json(body)
        runs = page.get("check_runs") if isinstance(page, dict) else None
        if not isinstance(runs, list) or not all(isinstance(run, dict) for run in runs):
            raise VerificationError("GitHub check-run page is malformed")
        records.extend(runs)
        following = _next_link(headers.get("link"))
        if following is None:
            return records
        if not following.startswith(api + "/"):
            raise VerificationError("GitHub pagination link leaves the API")
        url = following
    raise VerificationError(f"attestation listing exceeds {MAX_PAGES} pages")


def _newest_trusted(
    records: list[dict[str, Any]], app_id: int, sha: str
) -> tuple[int, dict[str, Any]]:
    trusted: list[tuple[int, dict[str, Any]]] = []
    for record in records:
        if record.get("name") != ATTESTATION_CHECK_NAME:
            continue
        app = record.get("app")
        record_app = _strict_int(app.get("id")) if isinstance(app, dict) else None
        if record_app is None or record_app <= 0:
            raise VerificationError("an attestation check run's App identity is malformed")
        if record_app != app_id:
            continue
        record_id = _strict_int(record.get("id"))
        if record_id is None or record_id <= 0:
            raise VerificationError("a trusted attestation check run's id is malformed")
        trusted.append((record_id, record))
    if not trusted:
        raise VerificationError(f"no {ATTESTATION_CHECK_NAME} check run from App {app_id} on {sha}")
    return max(trusted, key=lambda item: item[0])


def _canonical_payload(record: dict[str, Any], record_id: int, sha: str) -> dict[str, Any]:
    status, conclusion = record.get("status"), record.get("conclusion")
    if status != "completed" or conclusion != "success":
        raise VerificationError(
            f"newest attestation check run {record_id} is {status}/{conclusion}, "
            "not completed/success"
        )
    if record.get("head_sha") != sha:
        raise VerificationError(f"newest attestation check run {record_id} is on another head")
    output = record.get("output")
    text = output.get("text") if isinstance(output, dict) else None
    if not isinstance(text, str):
        raise VerificationError(f"attestation check run {record_id} has no payload text")
    data = text.encode("utf-8")
    try:
        payload = json.loads(
            data, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise VerificationError(f"attestation payload is not valid JSON: {exc}") from None
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    if canonical != data:
        raise VerificationError("attestation payload text is noncanonical")
    if record.get("external_id") != EXTERNAL_ID_PREFIX + hashlib.sha256(data).hexdigest():
        raise VerificationError("attestation external_id is not the payload digest")
    _require_shape(payload)
    return payload


def _require_shape(payload: Any) -> None:
    """The `AttestationPayload` model's constraints, so nothing the daemon refuses passes."""
    _require_fields(payload, _PAYLOAD_FIELDS, "payload")
    if payload["schema"] != ATTESTATION_SCHEMA:
        raise VerificationError(f"attestation schema is not {ATTESTATION_SCHEMA}")
    for field in ("canonical_repository_id", "required_check_set_version"):
        _require_text(payload[field], field)
    for field in ("repository_id", "ci_producer_app_id", "attestation_app_id"):
        _require_id(payload[field], field)
    _require_sha(payload["head_sha"], "head_sha")
    checks, runs = payload["checks"], payload["workflow_runs"]
    for field, items, fields in (
        ("checks", checks, _CHECK_FIELDS),
        ("workflow_runs", runs, _WORKFLOW_RUN_FIELDS),
    ):
        if not isinstance(items, list) or not items:
            raise VerificationError(f"attestation {field} is empty or malformed")
        for item in items:
            _require_fields(item, fields, field)
            _require_sha(item["head_sha"], f"{field}.head_sha")
            _require_id(item["check_suite_id"], f"{field}.check_suite_id")
            if item["conclusion"] != "success":
                raise VerificationError(f"attestation {field} entry is not successful")
            if item["head_sha"] != payload["head_sha"]:
                raise VerificationError(f"attestation {field} entry is on another head")
    for check in checks:
        _require_text(check["name"], "checks.name")
        _require_id(check["check_run_id"], "checks.check_run_id")
        _require_id(check["producer_app_id"], "checks.producer_app_id")
    for run in runs:
        _require_id(run["workflow_run_id"], "workflow_runs.workflow_run_id")
        _require_id(run["run_attempt"], "workflow_runs.run_attempt")
    names = [check["name"] for check in checks]
    check_ids = [check["check_run_id"] for check in checks]
    suites = [run["check_suite_id"] for run in runs]
    if len(set(names)) != len(names) or len(set(check_ids)) != len(check_ids):
        raise VerificationError("attested checks contain duplicates")
    if len(set(suites)) != len(suites):
        raise VerificationError("attested workflow runs contain duplicate suites")
    if set(suites) != {check["check_suite_id"] for check in checks}:
        raise VerificationError("attested workflow runs do not cover the checks' suites")


def _require_identity(
    payload: dict[str, Any], *, repository_id: int, app_id: int, sha: str, version: str
) -> None:
    mismatched = [
        field
        for field, matches in (
            ("repository_id", payload["repository_id"] == repository_id),
            ("attestation_app_id", payload["attestation_app_id"] == app_id),
            ("head_sha", payload["head_sha"] == sha),
            ("required_check_set_version", payload["required_check_set_version"] == version),
            ("ci_producer_app_id", payload["ci_producer_app_id"] != app_id),
            (
                "checks.producer_app_id",
                all(
                    check["producer_app_id"] == payload["ci_producer_app_id"]
                    for check in payload["checks"]
                ),
            ),
        )
        if not matches
    ]
    if mismatched:
        raise VerificationError(f"attestation payload does not match: {', '.join(mismatched)}")


def _reverify(
    http_get: HttpGet,
    api: str,
    repository: str,
    sha: str,
    producer: int,
    check: dict[str, Any],
) -> None:
    check_run_id = check["check_run_id"]
    body, _headers = http_get(f"{api}/repos/{repository}/check-runs/{check_run_id}")
    run = _json(body)
    if not isinstance(run, dict):
        raise VerificationError(f"check run {check_run_id} is malformed")
    app, suite = run.get("app"), run.get("check_suite")
    problems = [
        problem
        for problem, matches in (
            ("id", _strict_int(run.get("id")) == check_run_id),
            ("name", run.get("name") == check["name"]),
            ("app", isinstance(app, dict) and _strict_int(app.get("id")) == producer),
            ("head_sha", run.get("head_sha") == sha),
            (
                "conclusion",
                run.get("status") == "completed" and run.get("conclusion") == "success",
            ),
            (
                "check_suite",
                isinstance(suite, dict)
                and _strict_int(suite.get("id")) == check["check_suite_id"],
            ),
        )
        if not matches
    ]
    if problems:
        raise VerificationError(
            f"attested check {check['name']!r} (check run {check_run_id}) does not "
            f"re-verify: {', '.join(problems)}"
        )


def _require_fields(value: Any, fields: frozenset[str], where: str) -> None:
    if not isinstance(value, dict) or set(value) != fields:
        raise VerificationError(f"attestation {where} fields are not the v1 schema")


def _require_text(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value:
        raise VerificationError(f"attestation {field} is not a non-empty string")


def _require_id(value: Any, field: str) -> None:
    number = _strict_int(value)
    if number is None or number <= 0:
        raise VerificationError(f"attestation {field} is not a positive integer")


def _require_sha(value: Any, field: str) -> None:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise VerificationError(f"attestation {field} is not a commit SHA")


def _strict_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _positive_decimal(value: str, name: str) -> int:
    if _POSITIVE_DECIMAL.fullmatch(value) is None:
        raise VerificationError(f"{name} is not a positive decimal id")
    return int(value)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate field {key!r}")
        result[key] = value
    return result


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-finite number {name}")


def _json(body: bytes) -> Any:
    try:
        return json.loads(body)
    except (UnicodeDecodeError, ValueError) as exc:
        raise VerificationError(f"GitHub response is not JSON: {exc}") from None


def _next_link(value: str | None) -> str | None:
    """The `rel="next"` target of a GitHub `Link` header, if any."""
    if not value:
        return None
    links = []
    for item in value.split(","):
        target, _, parameters = item.partition(";")
        target = target.strip()
        if not (target.startswith("<") and target.endswith(">")):
            raise VerificationError("GitHub pagination link is malformed")
        if any(part.strip().lower() == 'rel="next"' for part in parameters.split(";")):
            links.append(target[1:-1])
    if len(links) > 1:
        raise VerificationError("GitHub pagination link is ambiguous")
    return links[0] if links else None


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Never forward the token elsewhere; a redirect is an error, so CI runs."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def urllib_get(token: str) -> HttpGet:
    opener = urllib.request.build_opener(_RefuseRedirects)

    def get(url: str) -> tuple[bytes, Mapping[str, str]]:
        path = urllib.parse.urlsplit(url).path
        if not token:
            raise VerificationError("GH_TOKEN is empty")
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "agent-queue-main-attestation",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                headers = {key.lower(): value for key, value in response.headers.items()}
        except urllib.error.HTTPError as exc:
            raise VerificationError(f"GitHub API returned HTTP {exc.code} for {path}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise VerificationError(f"GitHub API request for {path} failed: {exc}") from None
        if len(body) > MAX_RESPONSE_BYTES:
            raise VerificationError(f"GitHub response for {path} is too large")
        return body, headers

    return get


def _one_line(text: str, limit: int = 500) -> str:
    """Printable ASCII on one line, so a reason cannot add `$GITHUB_OUTPUT` keys."""
    flat = " ".join("".join(c if " " <= c <= "~" else " " for c in text).split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def _append(path: str | None, text: str) -> None:
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)
    else:
        sys.stdout.write(text)


def main(environ: Mapping[str, str] | None = None, http_get: HttpGet | None = None) -> int:
    env = os.environ if environ is None else environ
    verdict = verify(env, urllib_get(env.get("GH_TOKEN", "")) if http_get is None else http_get)
    reason = _one_line(verdict.reason)
    if not verdict.configured:
        summary = f"Main attestation: {reason}; no audit CI runs."
    elif verdict.attested:
        summary = f"Main attestation: attested ({reason})."
    else:
        summary = f"Main attestation: NOT attested ({reason}); unattested-ci runs the full suite."
    flag = {True: "true", False: "false"}
    _append(
        env.get("GITHUB_OUTPUT"),
        f"attested={flag[verdict.attested]}\n"
        f"configured={flag[verdict.configured]}\n"
        f"reason={reason}\n",
    )
    _append(env.get("GITHUB_STEP_SUMMARY"), summary + "\n")
    print(summary)
    if verdict.configured and not verdict.attested:
        print(f"::warning title=Main attestation::{reason.replace('%', '%25')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
