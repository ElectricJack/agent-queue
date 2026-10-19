#!/usr/bin/env python3
"""Ask the e2e daemon whether it can actually use its database.

``GET /api/health`` is a *liveness* stub: it answers ``{"status": "ok"}``
with a 200 as soon as uvicorn is listening and never touches the database.
Gating the kit on it means a daemon whose schema setup failed — or whose
database was dropped out from under it by a concurrent
``scripts/e2e-env.sh --reset`` — is reported "healthy", and the fifteen
smoke scenarios then run against an empty database and produce a capability
report full of ``relation "projects" does not exist`` that reads like a
product regression (task vivid-rapids).

``GET /ready`` is the honest answer: it reads the daemon's background health
snapshot, whose ``checks.database`` entry comes from a query through the
engine the daemon is using. It replies 503 rather than 200 when a check
fails, which is why nothing here uses ``curl -f`` semantics — the body
carries the diagnosis either way.

Usage::

    python3 scripts/e2e/probe.py [--url URL] [--timeout SECONDS] [--wait SECONDS] [--quiet]

Exit codes:

* ``0`` — ready: the daemon answered and its database check passed;
* ``1`` — answering, but the database is unusable (the interesting case);
* ``2`` — not answering at all.
* ``3`` — health snapshot temporarily unavailable (initializing/stale/timeout).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

READY = 0
UNUSABLE = 1
UNREACHABLE = 2
BUSY = 3

#: What to tell whoever is reading the failure.  The overwhelmingly likely
#: cause is two checkouts sharing the kit's defaults, because
#: ``AQ_E2E_HOME``, ``AQ_E2E_PORT`` and ``E2E_DB_NAME`` are per-box rather
#: than per-worktree unless they are overridden.
HINT = (
    "The database was dropped or recreated under the running daemon, or its "
    "schema setup never completed.\n"
    "A second checkout running `scripts/e2e-env.sh --reset` against the same "
    "AQ_E2E_HOME / E2E_DB_NAME does exactly this: the reset terminates the "
    "live daemon's backends and drops its database while it keeps answering.\n"
    "Export AQ_E2E_HOME, AQ_E2E_PORT and E2E_DB_NAME to run two kits side by "
    "side.\n"
    "To rebuild this one:\n"
    "  scripts/e2e-daemon.sh stop && scripts/e2e-env.sh --reset && "
    "scripts/e2e-daemon.sh start"
)


def _first_line(text: str) -> str:
    """The head of a SQLAlchemy error, which is otherwise a wall of SQL."""
    for line in str(text).splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return str(text).strip()


def classify(payload: object) -> tuple[int, str]:
    """Turn a ``/ready`` body into an exit code and a message.

    Pure, so the interesting branches are unit-testable without a daemon.
    A body that is not the readiness envelope at all counts as unreachable:
    something answered, but it was not this daemon.
    """
    if not isinstance(payload, dict) or "checks" not in payload:
        return UNREACHABLE, f"/ready did not return a readiness envelope: {payload!r:.200}"
    checks = payload.get("checks")
    if not isinstance(checks, dict):
        return UNREACHABLE, f"/ready returned no checks: {payload!r:.200}"
    snapshot = checks.get("health_snapshot")
    if isinstance(snapshot, dict) and not snapshot.get("ok"):
        reason = snapshot.get("reason") or "unknown"
        code = BUSY if reason in {"initializing", "stale", "timeout"} else UNUSABLE
        return code, f"health snapshot unavailable: {reason}"
    database = checks.get("database")
    if not isinstance(database, dict):
        return UNUSABLE, "/ready reported no database check at all"
    if database.get("ok"):
        failed = [name for name, check in checks.items()
                  if isinstance(check, dict) and not check.get("ok")]
        if payload.get("ready") is not True or failed:
            return UNUSABLE, "/ready reports not ready: " + (", ".join(failed) or "no reason reported")
        return READY, "database ok"
    error = _first_line(database.get("error") or "no error reported")
    return UNUSABLE, f"database check failed: {error}"


def fetch(url: str, timeout: float) -> tuple[int, str]:
    """Probe ``<url>/ready`` once."""
    endpoint = url.rstrip("/") + "/ready"
    try:
        with urllib.request.urlopen(endpoint, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        # 503 is the documented "not ready" reply and still carries the body.
        body = exc.read() or b""
    except Exception as exc:  # noqa: BLE001 — any transport failure is "not up"
        return UNREACHABLE, f"{endpoint} is not answering: {exc.__class__.__name__}: {exc}"
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        return UNREACHABLE, f"{endpoint} returned non-JSON: {body[:200]!r}"
    return classify(payload)


def wait_until_ready(url: str, timeout: float, wait: float) -> tuple[int, str]:
    """Wait only for transient snapshot recovery; never accept stale evidence."""
    deadline = time.monotonic() + wait
    code, message = fetch(url, min(timeout, wait) if wait > 0 else timeout)
    while code == BUSY and wait > 0:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.25, remaining))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        code, message = fetch(url, min(timeout, remaining))
    return code, message


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=os.environ.get("AQ_E2E_API_URL", "http://127.0.0.1:8099"),
        help="daemon base URL (default: $AQ_E2E_API_URL)",
    )
    # Generous on purpose: a daemon whose database vanished answers slowly
    # (every health check re-opens a connection and fails), and timing it
    # out would report "not reachable" for the one case this exists to
    # diagnose.
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--wait", type=float, default=0.0,
        help="seconds to wait for an initializing, stale or timed-out health snapshot",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="report only through the exit code — for shell conditionals",
    )
    args = parser.parse_args(argv)
    if args.timeout <= 0 or args.wait < 0:
        parser.error("--timeout must be positive and --wait must be nonnegative")

    code, message = wait_until_ready(args.url, args.timeout, args.wait)
    if args.quiet or code == READY:
        return code
    if code == UNUSABLE and message.startswith(("database check failed:", "/ready reported no database")):
        print(f"e2e daemon at {args.url} is answering but cannot use its database:", file=sys.stderr)
        print(f"  {message}", file=sys.stderr)
        print(HINT, file=sys.stderr)
    elif code in {UNUSABLE, BUSY}:
        print(f"e2e daemon at {args.url} is not ready: {message}", file=sys.stderr)
    else:
        print(f"e2e daemon at {args.url} is not reachable:", file=sys.stderr)
        print(f"  {message}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
