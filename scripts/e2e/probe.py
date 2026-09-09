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

``GET /ready`` is the honest answer: it runs the daemon's own health
provider, so its ``checks.database`` entry is a real query through the
engine the daemon is using.  It replies 503 rather than 200 when a check
fails, which is why nothing here uses ``curl -f`` semantics — the body
carries the diagnosis either way.

Usage::

    python3 scripts/e2e/probe.py [--url URL] [--timeout SECONDS] [--quiet]

Exit codes:

* ``0`` — ready: the daemon answered and its database check passed;
* ``1`` — answering, but the database is unusable (the interesting case);
* ``2`` — not answering at all.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

READY = 0
UNUSABLE = 1
UNREACHABLE = 2

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
    database = checks.get("database")
    if not isinstance(database, dict):
        return UNUSABLE, "/ready reported no database check at all"
    if database.get("ok"):
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
        "--quiet",
        action="store_true",
        help="report only through the exit code — for shell conditionals",
    )
    args = parser.parse_args(argv)

    code, message = fetch(args.url, args.timeout)
    if args.quiet or code == READY:
        return code
    if code == UNUSABLE:
        print(f"e2e daemon at {args.url} is answering but cannot use its database:", file=sys.stderr)
        print(f"  {message}", file=sys.stderr)
        print(HINT, file=sys.stderr)
    else:
        print(f"e2e daemon at {args.url} is not reachable:", file=sys.stderr)
        print(f"  {message}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
