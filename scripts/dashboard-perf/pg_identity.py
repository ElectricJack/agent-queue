#!/usr/bin/env python3
"""Is the test PostgreSQL the daemon's PostgreSQL?  Endpoints only, never credentials (spec §4.2).

Test databases on the daemon's server compete with it for buffers, IO and
connections (``tests/pg_dsn.py``), so the experiment records whether the two
share a server and whether they are the very same database.  An identity is
scheme, host, port and database name — never the user, the password or the
query string — and nothing here echoes a DSN it failed to parse.

``main()`` prints ``compare($POSTGRES_TEST_DSN, <daemon's database.url>)``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

DEFAULT_CONFIG = Path.home() / ".agent-queue" / "config.yaml"
_SCHEMES = {"postgresql": "postgresql", "postgres": "postgresql"}
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
_DEFAULT_PORT = 5432
_PLACEHOLDER_RE = re.compile(r"\$\{(\w+)\}")


def identity(dsn: str) -> dict:
    """``{"scheme", "host", "port", "database"}`` of a PostgreSQL DSN — never user or password.

    Raises ``ValueError`` (with no part of the DSN in its message) for
    anything that is not a PostgreSQL URL.
    """
    try:
        parts = urlsplit(str(dsn or "").strip())
        scheme = _SCHEMES.get(parts.scheme.split("+", 1)[0].lower())
        port = parts.port
    except ValueError:
        scheme = None
    if scheme is None:
        raise ValueError("not a PostgreSQL DSN")
    host = parts.hostname or (parse_qs(parts.query).get("host") or ["localhost"])[0]
    return {
        "scheme": scheme,
        "host": host,
        "port": port or _DEFAULT_PORT,
        "database": parts.path.lstrip("/") or None,
    }


def _server_key(ident: dict) -> tuple[str, int]:
    host = str(ident["host"]).lower()
    return ("localhost" if host in _LOOPBACK else host, int(ident["port"]))


def compare(test_dsn: str | None, daemon_dsn: str | None) -> dict:
    """Both identities and whether they share a server / a database.

    A missing or unparseable side leaves both verdicts ``None`` and names why
    in ``reason`` (``no_test_dsn``, ``no_daemon_dsn``, ``invalid_test_dsn``,
    ``invalid_daemon_dsn``); the test side's reason wins when both apply.
    """
    out: dict = {"test": None, "daemon": None, "same_server": None, "same_database": None,
                 "reason": None}
    reasons: list[str] = []
    for side, dsn in (("test", test_dsn), ("daemon", daemon_dsn)):
        if not dsn:
            reasons.append(f"no_{side}_dsn")
            continue
        try:
            out[side] = identity(dsn)
        except ValueError:
            reasons.append(f"invalid_{side}_dsn")
    if reasons:
        out["reason"] = reasons[0]
        return out
    out["same_server"] = _server_key(out["test"]) == _server_key(out["daemon"])
    out["same_database"] = out["same_server"] and out["test"]["database"] == out["daemon"]["database"]
    return out


def _read_dotenv(path: Path) -> dict[str, str]:
    """``KEY=value`` lines, parsed the way ``src.config._load_env_file`` does."""
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip():
            values[key.strip()] = value.strip()
    return values


def daemon_dsn_from_config(path: Path = DEFAULT_CONFIG) -> tuple[str | None, str | None]:
    """The daemon's ``database.url`` and ``None``, or ``None`` and why not.

    Resolves it the way ``src.config.load_config`` does, without touching this
    process's environment: ``.env`` beside the config wins over the shell,
    the ``config.<env>.yaml`` overlay and then an ``AGENT_QUEUE_PROFILE``
    overlay win over the base file, and ``${VAR}`` is substituted.  (A
    profile given only as ``--profile`` on the daemon's command line is not
    visible from here.)  Reasons: ``no_config``, ``unreadable_config``,
    ``no_database_url``, ``unresolved_placeholder``.
    """
    import yaml

    path = Path(path).expanduser()
    if not path.exists():
        return None, "no_config"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None, "unreadable_config"
    if not isinstance(raw, dict):
        return None, "unreadable_config"
    env = {**os.environ, **_read_dotenv(path.parent / ".env")}
    url = _database_url(raw)
    env_name = env.get("AGENT_QUEUE_ENV", raw.get("env", "production"))
    overlays = [path.with_name(f"{path.stem}.{env_name}{path.suffix}")]
    if env.get("AGENT_QUEUE_PROFILE"):
        overlays.append(path.parent / "profiles" / f"{env['AGENT_QUEUE_PROFILE']}.yaml")
    for overlay in overlays:
        if not overlay.exists():
            continue
        try:
            url = _database_url(yaml.safe_load(overlay.read_text(encoding="utf-8")) or {}) or url
        except (OSError, yaml.YAMLError):
            return None, "unreadable_config"
    if not url:
        return None, "no_database_url"
    unresolved = [name for name in _PLACEHOLDER_RE.findall(url) if env.get(name) is None]
    if unresolved:
        return None, "unresolved_placeholder"
    return _PLACEHOLDER_RE.sub(lambda m: env[m.group(1)], url), None


def _database_url(raw) -> str:
    section = raw.get("database") if isinstance(raw, dict) else None
    url = section.get("url") if isinstance(section, dict) else None
    return str(url or "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help="the daemon's config file (default %(default)s)")
    args = parser.parse_args(argv)
    daemon, reason = daemon_dsn_from_config(args.config)
    out = compare(os.environ.get("POSTGRES_TEST_DSN"), daemon)
    if reason:
        out["daemon_reason"] = reason
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
