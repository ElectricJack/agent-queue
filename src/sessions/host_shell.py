"""Operator host shells: plain login shells in tmux, never agents.

A host shell is a tmux session named ``aq-host-shell-<n>`` on the daemon's
tmux socket, running the operator's login shell in ``$HOME``. It has no
``sessions`` row, no agent and no task, so it never appears in session or
agent lists, pool counts, the stall sweep or the reaper (which only adopt or
reap the ``s-``/``n-``/``p-`` prefixes). The dashboard attaches to it through
the same ``/ws/terminal`` PTY client agent terminals use, so it survives page
reloads and can be reattached or closed.

The shell starts from ``env -i`` with a short allowlist, so no daemon secret,
database DSN or ``AQ_*`` agent/session token reaches it; every other tmux
global variable is marked removed for later windows in the session.
``AQ_INSTANCE_TOKEN`` lives only in the tmux *session* environment (set after
the shell started) and fences attach and close like an agent's instance token.
"""
from __future__ import annotations

import os
import pwd
import re
import secrets
import shlex
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.sessions.provider import SessionError
from src.sessions.terminal_pty import resolve_session_id

if TYPE_CHECKING:
    from src.sessions.tmux import TmuxProvider

HOST_SHELL_PREFIX = "aq-host-shell-"
_NAME = re.compile(r"aq-host-shell-([1-9][0-9]{0,3})")
_MARKER_OPTION = "@aq_host_shell"
#: The only daemon variables a host shell inherits; the login shell sets the rest.
_ENV_ALLOWLIST = ("HOME", "USER", "LOGNAME", "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ")


class HostShellError(Exception):
    """A constant, operator-safe refusal."""


@dataclass(frozen=True)
class HostShell:
    name: str
    instance_token: str
    created_at: float
    attached_clients: int
    provider: str = "tmux"

    def to_dict(self) -> dict:
        return {
            "name": self.name, "created_at": self.created_at,
            "attached_clients": self.attached_clients,
        }


def is_host_shell_name(name: str) -> bool:
    return bool(_NAME.fullmatch(name or ""))


def _login_shell() -> str:
    try:
        shell = pwd.getpwuid(os.getuid()).pw_shell
    except KeyError:
        shell = ""
    return shell if shell and os.path.isabs(shell) else "/bin/bash"


def shell_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    """The whole environment a new host shell starts with."""
    source = os.environ if source is None else source
    env = {k: source[k] for k in _ENV_ALLOWLIST if source.get(k)}
    env.setdefault("HOME", os.path.expanduser("~"))
    env["SHELL"] = _login_shell()
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    return env


def shell_command(env: dict[str, str]) -> str:
    return shlex.join(["env", "-i", *(f"{k}={v}" for k, v in env.items()), env["SHELL"], "-l"])


class HostShellManager:
    """Open, list and close host shells on one tmux provider."""

    def __init__(self, provider: TmuxProvider):
        self.provider = provider

    async def _token(self, name: str) -> str | None:
        try:
            out = await self.provider._tmux(
                "show-environment", "-t", f"={name}", "AQ_INSTANCE_TOKEN", timeout=2,
            )
        except (SessionError, OSError):
            return None
        line = out.strip()
        return line.split("=", 1)[1] if line.startswith("AQ_INSTANCE_TOKEN=") else None

    async def _is_marked(self, name: str) -> bool:
        try:
            out = await self.provider._tmux(
                "show-options", "-qv", "-t", f"={name}", _MARKER_OPTION, timeout=2,
            )
        except (SessionError, OSError):
            return False
        return out.strip() == "1"

    async def list(self) -> list[HostShell]:
        try:
            out = await self.provider._tmux(
                "list-sessions", "-F",
                "#{session_name}\t#{session_created}\t#{session_attached}", timeout=2,
            )
        except (SessionError, OSError):
            return []  # no tmux server on the socket means no host shells
        shells = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) != 3 or not is_host_shell_name(parts[0]):
                continue
            token = await self._token(parts[0])
            if not token or not await self._is_marked(parts[0]):
                continue
            shells.append(HostShell(
                name=parts[0], instance_token=token,
                created_at=float(parts[1] or 0), attached_clients=int(parts[2] or 0),
            ))
        return sorted(shells, key=lambda s: int(_NAME.fullmatch(s.name).group(1)))

    async def get(self, name: str) -> HostShell | None:
        if not is_host_shell_name(name):
            return None
        return next((s for s in await self.list() if s.name == name), None)

    async def _global_names(self) -> list[str]:
        try:
            out = await self.provider._tmux("show-environment", "-g", timeout=2)
        except (SessionError, OSError):
            return []
        names = []
        for line in out.splitlines():
            key = line.lstrip("-").split("=", 1)[0]
            if key and key not in _ENV_ALLOWLIST:
                names.append(key)
        return names

    async def open(self, *, max_shells: int) -> HostShell:
        existing = await self.list()
        if len(existing) >= max_shells:
            raise HostShellError("Too many host shells are open; close one first")
        used = {int(_NAME.fullmatch(s.name).group(1)) for s in existing}
        number = next(n for n in range(1, len(used) + 2) if n not in used)
        name = f"{HOST_SHELL_PREFIX}{number}"
        env = shell_environment()
        token = secrets.token_hex(16)
        try:
            await self.provider._tmux(
                "new-session", "-d", "-s", name, "-c", env["HOME"],
                "-x", "120", "-y", "32", shell_command(env), timeout=5,
            )
            target = f"={name}"
            await self.provider._tmux(
                "set-environment", "-t", target, "AQ_INSTANCE_TOKEN", token, timeout=2,
            )
            await self.provider._tmux(
                "set-option", "-t", target, _MARKER_OPTION, "1", timeout=2,
            )
            await self.provider._tmux(
                "set-option", "-t", target, "detach-on-destroy", "on", timeout=2,
            )
            # Later windows inherit the tmux server's global environment;
            # remove every daemon variable from it for this session.
            for key in await self._global_names():
                await self.provider._tmux("set-environment", "-r", "-t", target, key, timeout=2)
        except (SessionError, OSError):
            await self._kill_name(name)
            raise HostShellError("Host shell could not be started") from None
        return HostShell(name=name, instance_token=token, created_at=time.time(),
                         attached_clients=0)

    async def _kill_name(self, name: str) -> None:
        try:
            await self.provider._tmux("kill-session", "-t", f"={name}", timeout=2)
        except (SessionError, OSError):
            return  # already gone

    async def close(self, name: str) -> bool:
        shell = await self.get(name)
        if shell is None:
            return False
        session_id = await resolve_session_id(self.provider, shell.name, shell.instance_token)
        if session_id is None:
            return False
        try:
            await self.provider._tmux("kill-session", "-t", session_id, timeout=3)
        except (SessionError, OSError):
            raise HostShellError("Host shell could not be closed") from None
        return True
