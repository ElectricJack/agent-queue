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
        checkout_path: str | None = None
        # Prefer a workspace for the given project; fall back to any.
        if project_id:
            workspaces = await self.db.list_workspaces(project_id=project_id)
            if workspaces:
                checkout_path = workspaces[0].workspace_path
        if not checkout_path:
            workspaces = await self.db.list_workspaces()
            if workspaces:
                checkout_path = workspaces[0].workspace_path
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
