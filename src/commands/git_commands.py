"""Git commands mixin for CommandHandler.

Provides git-related commands that go through the CommandHandler dispatch
surface.  Currently contains:

- ``pr_merge`` — merge a PR via ``gh pr merge``.  Only callable by profiles
  that whitelist ``pr_merge`` in ``allowed_tools`` (final-reviewer only in
  the shipped dv2-phase2 configuration).
"""

from __future__ import annotations

import os


class GitCommandsMixin:
    """Mixin that adds git PR commands to CommandHandler."""

    async def _cmd_pr_merge(self, args: dict) -> dict:
        """Merge a PR.  Backs ``aq pr merge`` and the final-reviewer's tool.

        Only allowed for profiles that whitelist ``pr_merge`` in
        ``allowed_tools`` — the profile system enforces the toolset per
        agent, so worker profiles cannot invoke this even if they discover
        the command name.

        Args:
            project_id: ID of the project whose workspace will be used.
            pr_url: Full GitHub PR URL, e.g. ``https://github.com/o/r/pull/42``.
            method: Merge strategy — ``"squash"`` (default), ``"merge"``,
                or ``"rebase"``.

        Returns:
            ``{"success": bool, "pr_url": str, "sha": str | None, "error": str | None}``
        """
        project_id = args.get("project_id")
        pr_url = args.get("pr_url")
        method = str(args.get("method") or "squash")

        if not project_id:
            return {"success": False, "pr_url": "", "sha": None, "error": "project_id is required"}
        if not pr_url:
            return {"success": False, "pr_url": "", "sha": None, "error": "pr_url is required"}

        project = await self.db.get_project(project_id)
        if project is None:
            return {
                "success": False,
                "pr_url": pr_url,
                "sha": None,
                "error": f"unknown project: {project_id}",
            }

        # ``gh pr merge`` used to run in ``get_project_workspace_path()`` —
        # the project's first workspace row, clones before links, which under
        # worktree mode is the *base* checkout and is routinely the operator's
        # own working tree.  Merging does not need it: given a full PR URL gh
        # resolves owner/repo/number from the URL and talks to the API, so any
        # directory works (verified against gh 2.45).  Run in the daemon's
        # data dir instead, which is not a checkout at all.  The one behaviour
        # this drops is ``--delete-branch``'s *local* half; the remote branch
        # is still deleted, and the local branch it used to remove lived in a
        # tree no agent should have been touching.  See
        # :mod:`src.orchestrator.base_workspace`.
        cwd = self.config.data_dir or os.getcwd()
        os.makedirs(cwd, exist_ok=True)

        result = await self.orchestrator.git.amerge_pr(cwd, pr_url, method=method)
        return {
            "success": result["success"],
            "pr_url": pr_url,
            "sha": result.get("sha"),
            "error": result.get("error"),
        }
