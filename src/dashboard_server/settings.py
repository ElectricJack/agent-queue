"""Settings the dashboard server reads once, at process start (spec §3.1).

The YAML is read directly rather than through :func:`src.config.load_config`:
that loader also imports the daemon's ``.env`` into the process environment,
and this process needs no secret (§1).  The sections it does read go through
the daemon's own parsers -- :func:`src.config.dashboard_server_config_from_raw`
and :class:`src.config.ApiAuthConfig` -- so a key means the same thing in both
processes.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from src.config import (
    ApiAuthConfig,
    DashboardServerConfig,
    dashboard_server_config_from_raw,
)

DEFAULT_CONFIG_PATH = Path(os.path.expanduser("~/.agent-queue/config.yaml"))
DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8081


class SettingsError(ValueError):
    """A setting the dashboard server cannot start with; the message names the key."""


@dataclass(frozen=True)
class DashboardServerSettings:
    """Everything the dashboard server process needs, resolved and validated."""

    host: str = "127.0.0.1"
    port: int = 8082
    api_url: str = f"http://{DEFAULT_API_HOST}:{DEFAULT_API_PORT}"
    #: Normalised ``api_auth.trusted_dashboard_origins`` entries.
    trusted_origins: tuple[str, ...] = ()
    #: ``None`` means the bundle packaged with this installation.
    bundle_directory: Path | None = None

    @property
    def url(self) -> str:
        """Where a browser on this machine reaches the dashboard server."""
        host = self.host
        if host in {"0.0.0.0", "::"}:
            host = "127.0.0.1"
        if ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{self.port}/"


def normalise_origin(value: str) -> str | None:
    """``scheme://host[:port]`` lower-cased with the default port dropped, else ``None``."""
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    if parts.path or parts.query or parts.fragment:
        return None
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    default = 443 if parts.scheme == "https" else 80
    if port is None or port == default:
        return f"{parts.scheme}://{host}"
    return f"{parts.scheme}://{host}:{port}"


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise SettingsError(f"cannot read {path}: {error}") from error
    try:
        payload = yaml.safe_load(text) or {}
    except yaml.YAMLError as error:
        raise SettingsError(f"{path} is not valid YAML: {error}") from error
    if not isinstance(payload, dict):
        raise SettingsError(f"{path} must contain a mapping")
    return payload


def _api_port(raw: Mapping[str, Any]) -> int:
    section = raw.get("mcp_server")
    port: Any = section.get("port") if isinstance(section, Mapping) else None
    try:
        return int(port) if port not in (None, "") else DEFAULT_API_PORT
    except (TypeError, ValueError):
        return DEFAULT_API_PORT


def resolve_api_url(raw: Mapping[str, Any], environ: Mapping[str, str]) -> str:
    """The daemon's API base, resolved as every ``aq`` command resolves it."""
    override = (environ.get("AQ_API_URL") or environ.get("AGENT_QUEUE_API_URL") or "").strip()
    if override:
        return override.rstrip("/")
    section = raw.get("mcp_server")
    host = DEFAULT_API_HOST
    if isinstance(section, Mapping) and section.get("host"):
        host = str(section["host"])
    return f"http://{host}:{_api_port(raw)}"


def _checked_api_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018 - validates the port's syntax and range
    except ValueError as error:
        raise SettingsError(f"api_url: {value!r} is not a valid URL ({error})") from error
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise SettingsError(f"api_url: must be an http(s) URL with a host, got {value!r}")
    if parts.query or parts.fragment or parts.username or parts.password:
        raise SettingsError(f"api_url: must not carry credentials, a query or a fragment: {value!r}")
    return value.rstrip("/")


def settings_from_config(
    raw: Mapping[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
    host: str | None = None,
    port: int | None = None,
    api_url: str | None = None,
    bundle_directory: Path | None = None,
) -> DashboardServerSettings:
    """Resolve settings from a parsed config plus command-line overrides.

    Overrides are validated by the same rules as the YAML they replace, and
    every error names the key an operator would edit.
    """
    environ = os.environ if environ is None else environ
    try:
        section = dashboard_server_config_from_raw(raw)
    except (TypeError, ValueError) as error:
        raise SettingsError(f"dashboard.server: {error}") from error
    candidate = DashboardServerConfig(
        enabled=section.enabled,
        host=section.host if host is None else host,
        port=section.port if port is None else port,
    )
    errors = [error for error in candidate.validate() if error.field != "enabled"]
    if errors:
        raise SettingsError("; ".join(f"{e.section}.{e.field}: {e.message}" for e in errors))
    api_port = _api_port(raw)
    if candidate.port == api_port:
        raise SettingsError(
            f"dashboard.server.port: must differ from mcp_server.port ({api_port}), the daemon's own"
        )

    auth_section = raw.get("api_auth")
    origins = auth_section.get("trusted_dashboard_origins", []) if isinstance(
        auth_section, Mapping
    ) else []
    auth = ApiAuthConfig(trusted_dashboard_origins=origins if origins is not None else [])
    for error in auth.validate():
        if error.field == "trusted_dashboard_origins":
            raise SettingsError(f"api_auth.trusted_dashboard_origins: {error.message}")
    trusted = tuple(
        dict.fromkeys(
            normalised
            for normalised in (normalise_origin(origin) for origin in auth.trusted_dashboard_origins)
            if normalised is not None
        )
    )

    resolved_api = _checked_api_url(api_url if api_url is not None else resolve_api_url(raw, environ))
    return DashboardServerSettings(
        host=candidate.host,
        port=candidate.port,
        api_url=resolved_api,
        trusted_origins=trusted,
        bundle_directory=bundle_directory,
    )


def load_settings(
    config_path: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    host: str | None = None,
    port: int | None = None,
    api_url: str | None = None,
    bundle_directory: Path | None = None,
) -> DashboardServerSettings:
    """Read ``config.yaml`` (absent means defaults) and resolve the settings."""
    raw = _read_yaml(config_path or DEFAULT_CONFIG_PATH)
    return settings_from_config(
        raw,
        environ=environ,
        host=host,
        port=port,
        api_url=api_url,
        bundle_directory=bundle_directory,
    )
