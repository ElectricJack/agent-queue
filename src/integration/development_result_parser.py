"""Bounded, deterministic command-result parsing, independent of execution.

Feed every output chunk to ``PytestOutputParser`` before any log retention or
trimming. Reports retain the first 100 distinct failure ids, sorted for stable
rendering, while classification observes *all* failures (including omitted ones).
JUnit parsing accepts byte chunks rather than paths; artifact selection and safe
file opening belong to the runner. No subprocess, database or filesystem I/O.
"""

from __future__ import annotations

import codecs
import re
import signal
from collections.abc import Iterable
from dataclasses import dataclass
from xml.parsers import expat

PASSED = "passed"
FAILED = "failed"
INFRASTRUCTURE = "infrastructure"
LOST = "lost"
CANCELLED = "cancelled"
PARSER_VERSION = 1
MAX_FAILING_TESTS = 100
MAX_LINE_BYTES = 64 * 1024
MAX_FAILURE_BYTES = 1024
MAX_JUNIT_BYTES = 16 * 1024 * 1024
_CHUNK_BYTES = 64 * 1024

#: Exit codes that say nothing was verified.  ``3``/``4``/``5`` are pytest's
#: internal error, usage error and "no tests collected" (``aq test`` also
#: uses ``4`` for a missing path or test DSN); ``75`` is ``aq test``'s
#: EX_TEMPFAIL when no slot came free; ``124`` is ``timeout(1)``'s and the
#: publisher's own timeout code; ``126``/``127`` mean the command could not
#: be executed at all.
_EXIT_REASONS = {
    3: "test_runner_error",
    4: "test_runner_error",
    5: "no_tests_collected",
    75: "slot_unavailable",
    124: "timeout",
    126: "command_unavailable",
    127: "command_unavailable",
}
#: Signals that mean something outside the run stopped it.
_KILL_SIGNALS = {signal.SIGHUP, signal.SIGINT, signal.SIGKILL, signal.SIGTERM}

#: Failure text that names the validation environment rather than the code
#: under test: the test database, the box, the wrapper's container.
_INFRASTRUCTURE_PATTERNS = (
    r"connection refused",
    r"connect call failed",
    r"could not connect to server",
    r"too many (?:clients|connections)",
    r"remaining connection slots are reserved",
    r"the database system is (?:starting up|shutting down|in recovery mode)",
    r"server closed the connection unexpectedly",
    r"terminating connection due to administrator command",
    r"CannotConnectNowError",
    r"TooManyConnectionsError",
    r"ConnectionDoesNotExistError",
    r"connection was closed in the middle of operation",
    r"no space left on device",
    r"too many open files",
    r"POSTGRES_TEST_DSN is not set",
    r"no test slot free",
    r"could not clean owned PostgreSQL test databases",
    r"could not drop leased PostgreSQL test databases",
    r"PostgreSQL test database cleanup deadline exceeded",
    r"Cannot connect to the Docker daemon",
    r"Error response from daemon",
)
INFRASTRUCTURE_SIGNATURES = re.compile("|".join(_INFRASTRUCTURE_PATTERNS), re.IGNORECASE)

#: pytest's short test summary: ``FAILED <node id> - <reason>``.  The node id
#: must be a ``.py`` path, so captured output and log lines that merely start
#: with FAILED/ERROR are never taken for tests.
_FAILURE_LINE = re.compile(
    r"^(?:FAILED|ERROR) (?P<id>[^\s:]+\.py(?:::.*?)?)(?: - (?P<reason>.*))?$"
)
_SUMMARY_LINE = re.compile(
    r"^=*\s*(?P<counts>\d+ [a-z]+(?:, \d+ [a-z]+)*) in [\d.]+s\b.*$", re.MULTILINE
)
_SUMMARY_COUNT = re.compile(r"(\d+) ([a-z]+)")
_NO_TESTS_RAN = re.compile(r"^=*\s*no tests ran\b", re.MULTILINE)
_COUNT_KEYS = {"error": "errors", "warning": "warnings"}


@dataclass(frozen=True)
class PytestReport:
    failing: list[dict]
    summary: dict | None
    no_tests_ran: bool
    infrastructure_error: bool = False
    all_failures_infrastructure: bool = True
    # Occurrences omitted after the distinct-id cap. Repeated retained ids are
    # deduplicated; deduplicating unlimited omitted ids would be unbounded.
    omitted_failure_count: int = 0
    truncated: bool = False
    parser_error: str | None = None
    source: str = "text"
    artifact_status: str | None = None


def _bounded(text: str, limit: int = MAX_FAILURE_BYTES) -> str:
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= limit:
        return text
    return raw[: limit - 3].decode("utf-8", errors="ignore") + "..."


class _Failures:
    """Bounded ids, with classification facts retained beyond the id cap."""

    def __init__(self):
        self.tests: dict[str, str] = {}
        self.omitted = 0
        self.all_infra = True
        self.truncated = False

    def add(self, test_id: str, reason: str, *, infrastructure: bool | None = None):
        if infrastructure is None:
            infrastructure = bool(INFRASTRUCTURE_SIGNATURES.search(reason))
        self.all_infra &= infrastructure
        bounded_id, bounded_reason = _bounded(test_id), _bounded(reason)
        self.truncated |= bounded_id != test_id or bounded_reason != reason
        test_id, reason = bounded_id, bounded_reason
        if test_id in self.tests:
            return
        if len(self.tests) < MAX_FAILING_TESTS:
            self.tests[test_id] = reason
        else:
            self.omitted += 1

    def report(self, summary, no_tests_ran, **kwargs) -> PytestReport:
        return PytestReport(
            failing=[{"id": key, "reason": self.tests[key]} for key in sorted(self.tests)],
            summary=summary,
            no_tests_ran=no_tests_ran,
            all_failures_infrastructure=self.all_infra,
            omitted_failure_count=self.omitted,
            truncated=self.truncated or bool(self.omitted),
            **kwargs,
        )


class PytestOutputParser:
    """Incremental UTF-8 parser; memory does not grow with output or line size.

    At most 64 KiB of a line is retained. The rest is drained and scanned for
    infrastructure signatures in bounded windows; it cannot create new lines.
    Invalid UTF-8 is replaced, and a split valid code point is decoded intact.
    ``finish`` is idempotent and feeding after it is an API error.
    """

    def __init__(self):
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._line = ""
        self._line_bytes = 0
        self._overlap = ""
        self._line_infra = False
        self._line_truncated = False
        self._infra = False
        self._failures = _Failures()
        self._summary = None
        self._no_tests = False
        self._error = None
        self._finished = None

    def feed(self, chunk: bytes) -> None:
        if self._finished is not None:
            raise ValueError("cannot feed a finished parser")
        if self._error:
            return
        try:
            for offset in range(0, len(chunk), _CHUNK_BYTES):
                self._text(self._decoder.decode(chunk[offset : offset + _CHUNK_BYTES]))
        except Exception:  # noqa: BLE001 - a parser bug must not stall the runner
            # Parsing never prevents a runner from draining its pipe, and a
            # partial report never upgrades a nonzero exit to a success.
            self._error = "text_parser_error"
            self._summary = None

    def _text(self, text: str) -> None:
        offset = 0
        while offset < len(text):
            end = text.find("\n", offset)
            segment = text[offset:] if end < 0 else text[offset:end]
            scan = self._overlap + segment
            self._line_infra |= bool(INFRASTRUCTURE_SIGNATURES.search(scan))
            self._infra |= self._line_infra
            self._overlap = scan[-256:]
            remaining = MAX_LINE_BYTES - self._line_bytes
            raw = segment.encode("utf-8")
            kept = raw[:remaining].decode("utf-8", errors="ignore")
            self._line += kept
            self._line_bytes += len(kept.encode("utf-8"))
            if len(raw) > remaining:
                self._failures.truncated = True
                self._line_truncated = True
                # Do not append later chunks after a partially retained code point.
                self._line_bytes = MAX_LINE_BYTES
            if end < 0:
                break
            self._consume_line()
            offset = end + 1

    def _consume_line(self) -> None:
        line = self._line.strip()
        match = _FAILURE_LINE.match(line)
        if match:
            self._failures.add(
                match.group("id").strip(),
                (match.group("reason") or "").strip(),
                infrastructure=(
                    bool(INFRASTRUCTURE_SIGNATURES.search(match.group("reason") or ""))
                    or (self._line_truncated and self._line_infra)
                ),
            )
        match = _SUMMARY_LINE.fullmatch(line)
        if match:
            self._summary = {
                _COUNT_KEYS.get(word, word): int(count)
                for count, word in _SUMMARY_COUNT.findall(match.group("counts"))
            }
        self._no_tests |= bool(_NO_TESTS_RAN.match(line))
        self._line = ""
        self._line_bytes = 0
        self._line_infra = False
        self._line_truncated = False
        self._overlap = ""

    def finish(self) -> PytestReport:
        if self._finished is None:
            try:
                if not self._error:
                    self._text(self._decoder.decode(b"", final=True))
                    self._consume_line()
            except Exception:  # noqa: BLE001 - a parser bug is a report error
                self._error = "text_parser_error"
                self._summary = None
            self._finished = self._failures.report(
                self._summary,
                self._no_tests,
                infrastructure_error=self._infra,
                parser_error=self._error,
            )
        return self._finished


def parse_pytest_output(text: str) -> PytestReport:
    """Compatibility entry point using the same bounded streaming parser."""
    parser = PytestOutputParser()
    for offset in range(0, len(text), _CHUNK_BYTES):
        parser.feed(text[offset : offset + _CHUNK_BYTES].encode("utf-8", errors="replace"))
    return parser.finish()


def parse_junit(chunks: Iterable[bytes] | None) -> PytestReport:
    """Parse a selected JUnit artifact, without reading files or expanding entities.

    The artifact has a hard byte cap, no DTDs, and no external entities. Only
    testcase attributes and a bounded failure message are retained. A missing,
    malformed or oversize artifact is explicit and never evidence of a pass.
    Pytest's ``file`` + ``classname`` + ``name`` attributes produce node ids;
    other producers fall back to ``classname::name``.
    """
    failures = _Failures()
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    parser = expat.ParserCreate()
    stack = []
    case = None
    problem = None
    message = ""
    message_infra = False
    message_overlap = ""
    total = 0
    roots = 0

    def reject(*args):
        raise ValueError("unsafe_junit")

    def start(name, attrs):
        nonlocal case, problem, message, message_infra, message_overlap, roots
        if not stack:
            if name not in {"testsuite", "testsuites"}:
                raise ValueError("malformed_junit")
            roots += 1
        if len(stack) >= 64:
            raise ValueError("junit_limit")
        stack.append(name)
        if name == "testcase":
            if case is not None or not attrs.get("name"):
                raise ValueError("malformed_junit")
            file, cls, test = (attrs.get(key, "") for key in ("file", "classname", "name"))
            if file:
                # pytest classnames include the module; keep only any test class.
                module = file.removesuffix(".py").replace("/", ".")
                cls = cls.removeprefix(module).lstrip(".") if cls.startswith(module) else cls
            case = {
                "id": _bounded("::".join(part for part in (file, cls, test) if part)),
                "kind": "passed",
            }
        elif name in {"failure", "error", "skipped"} and case is not None:
            kind = {"failure": "failed", "error": "errors", "skipped": "skipped"}[name]
            if kind in {"failed", "errors"} or case["kind"] == "passed":
                case["kind"] = kind
            problem = name
            raw = attrs.get("message", "")
            message = _bounded(raw)
            failures.truncated |= message != raw
            message_infra = bool(INFRASTRUCTURE_SIGNATURES.search(raw))
            message_overlap = raw[-256:]

    def data(text):
        nonlocal message, message_infra, message_overlap
        if problem is None:
            return
        scan = message_overlap + text
        message_infra |= bool(INFRASTRUCTURE_SIGNATURES.search(scan))
        message_overlap = scan[-256:]
        combined = message + text
        message = _bounded(combined)
        failures.truncated |= message != combined

    def end(name):
        nonlocal case, problem
        if name in {"failure", "error"} and case is not None:
            failures.add(case["id"], message.strip(), infrastructure=message_infra)
        if name in {"failure", "error", "skipped"}:
            problem = None
        if name == "testcase":
            counts[case["kind"]] += 1
            case = None
        stack.pop()

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = data
    parser.StartDoctypeDeclHandler = reject
    parser.EntityDeclHandler = reject
    parser.ExternalEntityRefHandler = reject
    error = None
    if chunks is None:
        error = "missing_junit"
    else:
        try:
            for chunk in chunks:
                total += len(chunk)
                if total > MAX_JUNIT_BYTES:
                    raise ValueError("junit_limit")
                for offset in range(0, len(chunk), _CHUNK_BYTES):
                    parser.Parse(chunk[offset : offset + _CHUNK_BYTES], False)
            parser.Parse(b"", True)
            if roots != 1:
                raise ValueError("malformed_junit")
        except expat.ExpatError:
            error = "malformed_junit"
        except ValueError as exc:
            error = str(exc)
        except Exception:  # noqa: BLE001 - a parser bug is a report error
            error = "junit_parser_error"
    summary = None if error else {key: count for key, count in counts.items() if count}
    return failures.report(
        summary,
        not error and not any(counts.values()),
        parser_error=error,
        source="junit",
        artifact_status=error or "valid",
    )


def _killed_by(code: int) -> bool:
    if code < 0:
        return -code in _KILL_SIGNALS
    return code > 128 and code - 128 in _KILL_SIGNALS


def classify_report(
    exit_code: int | None,
    report: PytestReport,
    *,
    output_integrity: bool = True,
    cancelled: bool = False,
    legacy_no_tests: bool = False,
) -> tuple[str, str | None, list[dict]]:
    """Classify observed exit and integrity; parser text never implies success.

    Managed pytest exit 5 fails. The development publisher opts into its existing
    infrastructure deferral for empty collections. Unknown exit is lost, and
    malformed selected artifacts may leave counts null but cannot green a run.
    """
    failing = report.failing
    if cancelled:
        return CANCELLED, None, failing
    if exit_code is None:
        return LOST, "missing_exit", failing
    if not output_integrity:
        return INFRASTRUCTURE, "output_store_failed", failing
    if exit_code == 5 and not legacy_no_tests:
        return FAILED, "no_tests_collected", failing
    if exit_code in _EXIT_REASONS:
        return INFRASTRUCTURE, _EXIT_REASONS[exit_code], failing
    if _killed_by(exit_code):
        return INFRASTRUCTURE, "killed", failing
    if report.no_tests_ran:
        return INFRASTRUCTURE, "no_tests_collected", failing
    if exit_code == 0:
        if report.parser_error:
            return INFRASTRUCTURE, report.parser_error, failing
        return PASSED, None, []
    if failing or report.omitted_failure_count:
        if report.all_failures_infrastructure:
            return INFRASTRUCTURE, "infrastructure_error", failing
        return FAILED, None, failing
    if report.infrastructure_error:
        return INFRASTRUCTURE, "infrastructure_error", []
    return FAILED, None, []
