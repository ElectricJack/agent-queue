"""Resolve dashboard links that are safe to send to remote collaborators.

The daemon itself may still listen on loopback.  Discord is different: a
``localhost`` link in a message points at the recipient's machine, not the
daemon.  This module replaces only loopback bases with the machine's
Tailscale identity and deliberately refuses to guess from ordinary network
interfaces.
"""

from __future__ import annotations

import ipaddress
import json
import subprocess
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit, urlunsplit


# Wildcard bind addresses are just as unusable to a Discord recipient as
# loopback: they describe where this daemon listens, not how to reach it.
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "::"}
_TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")
_TAILSCALE_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
_UNAVAILABLE = "Remote dashboard link unavailable ({reason}; open it on the daemon host)."


@dataclass(frozen=True, slots=True)
class RemoteLinkBase:
    """A usable remote dashboard base, or a safe explanation for its absence."""

    url: str = ""
    unavailable_notice: str = ""


def _run_tailscale(args: list[str], runner: Callable[..., subprocess.CompletedProcess[str]]) -> str:
    try:
        result = runner(
            ["tailscale", *args],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _tailscale_ip(runner: Callable[..., subprocess.CompletedProcess[str]]) -> str:
    for family in ("-4", "-6"):
        for candidate in _run_tailscale(["ip", family], runner).splitlines():
            try:
                address = ipaddress.ip_address(candidate.strip())
            except ValueError:
                continue
            if address in (_TAILSCALE_V4 if address.version == 4 else _TAILSCALE_V6):
                return str(address)
    return ""


def _tailscale_hostname(runner: Callable[..., subprocess.CompletedProcess[str]]) -> str:
    raw = _run_tailscale(["status", "--json"], runner)
    try:
        hostname = str(json.loads(raw)["Self"]["DNSName"]).strip().rstrip(".")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return ""
    # A hostname from status is a tailnet identity, but still reject values
    # that could turn into an authority/userinfo injection when inserted in a
    # URL.  DNS names may contain labels, digits and hyphens only.
    labels = hostname.split(".")
    if not hostname or any(
        not label
        or len(label) > 63
        or any(
            char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"
            for char in label
        )
        or label.startswith("-")
        or label.endswith("-")
        for label in labels
    ):
        return ""
    return hostname


def _replace_host(parts, host: str) -> str:
    authority = f"[{host}]" if ":" in host else host
    if parts.port is not None:
        authority += f":{parts.port}"
    return urlunsplit((parts.scheme, authority, parts.path, parts.query, parts.fragment))


def resolve_remote_link_base(
    base_url: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> RemoteLinkBase:
    """Return a remote-safe equivalent of ``base_url`` for Discord links.

    Public configured URLs are retained.  A loopback URL is rewritten only
    with a verified Tailscale address (preferred) or DNS identity; no LAN or
    public interface discovery is attempted.  The returned notice is designed
    to be rendered verbatim when neither identity is available.
    """
    try:
        parts = urlsplit(base_url)
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        return RemoteLinkBase(
            unavailable_notice=_UNAVAILABLE.format(reason="dashboard URL is invalid")
        )
    if parts.scheme not in {"http", "https"} or not hostname:
        return RemoteLinkBase(
            unavailable_notice=_UNAVAILABLE.format(reason="dashboard URL is invalid")
        )
    if parts.username is not None or parts.password is not None:
        return RemoteLinkBase(
            unavailable_notice=_UNAVAILABLE.format(reason="dashboard URL contains credentials")
        )
    # Force parsing of an invalid port before deciding the URL is public.
    _ = port
    if hostname.lower() not in _LOOPBACK_HOSTS:
        return RemoteLinkBase(url=base_url)

    identity = _tailscale_ip(runner) or _tailscale_hostname(runner)
    if not identity:
        return RemoteLinkBase(
            unavailable_notice=_UNAVAILABLE.format(reason="Tailscale has no usable identity")
        )
    return RemoteLinkBase(url=_replace_host(parts, identity))
