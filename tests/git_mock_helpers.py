"""Test-only helpers that teach a mocked ``GitManager`` the contracts that
``_prepare_workspace`` actually depends on.

Suites that only want git to be a no-op still have to answer the handoff
guards, because those *fail closed*: ``_ensure_control_files_excluded`` refuses
to hand an agent a checkout whose managed ``info/exclude`` block it could not
install, and ``resolve_managed_exclude_path`` first proves the configured path
is the repository root by comparing it against ``git rev-parse
--show-toplevel``.  A mock that answers that query with a ``MagicMock`` (a bare
``AsyncMock``) or with ``""`` never compares equal, so the guard raises, git
setup is abandoned, the workspace is released and **no session is launched** --
which surfaces far from here as "session was never launched".
"""

from __future__ import annotations

from unittest.mock import AsyncMock

__all__ = ["stub_repo_root_identity"]


def stub_repo_root_identity(git_mock) -> None:
    """Answer ``rev-parse --show-toplevel`` with the checkout it was asked about.

    Every other ``_arun`` call (fetch, checkout, reset) stays the no-op empty
    string these suites already assumed.
    """

    async def _arun(args, cwd=None, timeout=None):
        if list(args[:2]) == ["rev-parse", "--show-toplevel"]:
            return cwd or ""
        return ""

    git_mock._arun = AsyncMock(side_effect=_arun)
