"""PR polling mixin — shared ``gh``-backed PR merge-state polling.

The sole consumer is the gate sweep (``_sweep_resolve_pr_ci_gates`` in
``core.py``), which resolves ``pr-merged``/``ci-run`` gates.  The legacy
AWAITING_APPROVAL task poller that used to live beside this was retired
with the ``requires_approval`` flag: PR completion is a ``pr-merged``
gate on downstream work, never a task status.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class PRPollingMixin:
    """PR merge-state polling mixed into Orchestrator."""

    async def _poll_pr_merged(
        self, pr_url: str, *, project_id: str | None = None
    ) -> bool | None:
        """Poll ``gh`` for a PR's merge state.

        Returns:
            * ``True``  — PR is merged.
            * ``False`` — PR is still open (``gh`` said so), or the poll
              could not run yet (no checkout available, transient ``gh``
              failure) — retry next cycle.
            * ``None``  — PR is closed without merge.

        The gate-sweep caller (``_sweep_resolve_pr_ci_gates``) only acts on
        ``True`` and treats both ``False`` and ``None`` as "leave the gate
        open", so a closed-unmerged PR keeps blocking its waiters until an
        operator resolves the gate by hand.
        """
        checkout_path = await self._pr_checkout_path(project_id)
        if not checkout_path:
            # Nothing to poll from — behave as "still open" and retry next
            # cycle when a workspace shows up.
            return False

        try:
            # ``acheck_pr_merged`` returns ``None`` for closed-unmerged —
            # let that propagate so callers can distinguish it from
            # still-open.
            return await self.git.acheck_pr_merged(checkout_path, pr_url)
        except Exception as e:
            logger.warning("Error polling PR %s: %s", pr_url, e)
            # Transient gh failure — retry next cycle rather than resolve.
            return False

    async def _pr_checkout_path(self, project_id: str | None) -> str | None:
        """Any checkout ``gh``/``git`` can run in — the project's, else any."""
        if project_id:
            workspaces = await self.db.list_workspaces(project_id=project_id)
            if workspaces:
                return workspaces[0].workspace_path
        workspaces = await self.db.list_workspaces()
        return workspaces[0].workspace_path if workspaces else None

    async def _pr_reached_default_branch(
        self, pr_url: str, *, project_id: str | None = None
    ) -> bool:
        """Has this merged PR's work actually reached the default branch?

        A merged PR whose base is a *feature* branch has put nothing on the
        default branch.  Pkg 4 is the worked example: #284/#288/#289 all
        merged into ``feature/playbook-v2-pkg4-core``, every task closed
        COMPLETED, and ``main`` never gained the code — while every
        downstream ``pr-merged`` gate resolved and released dependents that
        then could not find their prerequisite.

        Returns:
            * ``True``  — the PR targeted the default branch, or its base has
              itself been merged into the default branch, **or** the question
              could not be answered (no ``gh``, no checkout, no network).
              Unknowable must not wedge a gate shut forever.
            * ``False`` — the base is a branch that has not reached the
              default branch yet.  The gate stays open.
        """
        checkout = await self._pr_checkout_path(project_id)
        if not checkout:
            return True
        base = await self.git.apr_base_ref(checkout, pr_url)
        if not base:
            return True
        project = await self.db.get_project(project_id) if project_id else None
        default_branch = await self._get_default_branch(project, checkout)
        if base == default_branch:
            return True
        try:
            await self.git._arun(["fetch", "origin"], cwd=checkout)
        except Exception:
            logger.debug("fetch before base-branch check failed", exc_info=True)
        reached = await self.git.ais_ancestor(
            checkout, f"origin/{base}", f"origin/{default_branch}"
        )
        if not reached:
            logger.info(
                "PR %s merged into '%s', which has not reached '%s' — its "
                "pr-merged gate stays open",
                pr_url,
                base,
                default_branch,
            )
        return reached
