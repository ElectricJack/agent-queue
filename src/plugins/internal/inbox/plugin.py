"""Internal plugin: aq-inbox — Gmail poller with deterministic event routing.

Polls one Gmail inbox per configured project, parses Authentication-Results
headers, matches against a per-project allowlist file, and emits one of
two distinct events:

- ``email.received.allowlisted`` — SPF/DKIM pass, DKIM domain aligned,
  verified ``From:`` in the project's allowlist.
- ``email.received.unknown`` — anything else.

Project playbooks subscribe to one or both events; the routing choice is
made entirely by the plugin using parsed headers + the YAML file on disk,
with no LLM involvement.

Configuration (``~/.agent-queue/config.yaml``)::

    inbox:
      enabled: true
      projects:
        - moss-and-spade-business-logic
      oauth_token_path: ~/.config/google-docs-mcp/token.json
      client_secret: $GOOGLE_CLIENT_SECRET
      mark_read_on_emit: true
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from src.plugins.base import InternalPlugin, PluginPermission
from src.plugins.internal.inbox.allowlist import Allowlist
from src.plugins.internal.inbox.gmail_client import (
    GmailClient,
    GmailClientConfig,
    GmailUnavailable,
    resolve_client_secret,
)
from src.plugins.internal.inbox.poller import (
    EVENT_ALLOWLISTED,
    EVENT_UNKNOWN,
    InboxPoller,
    allowlist_path_for,
)

if TYPE_CHECKING:
    from src.plugins.base import PluginContext

logger = logging.getLogger(__name__)


DEFAULT_TOKEN_PATH = "~/.config/google-docs-mcp/token.json"


class InboxPlugin(InternalPlugin):
    """Gmail inbox poller — one poller per configured project."""

    plugin_permissions = [PluginPermission.NETWORK]

    config_schema = {
        "enabled": {"type": "boolean", "default": False},
        "projects": {
            "type": "array",
            "description": "List of project IDs to poll.",
            "default": [],
        },
        "oauth_token_path": {
            "type": "string",
            "description": "Path to the google-docs MCP token.json file.",
            "default": DEFAULT_TOKEN_PATH,
        },
        "client_secret": {
            "type": "string",
            "description": (
                "OAuth client secret.  Accepts a literal value or $ENV_VAR. "
                "Falls back to GOOGLE_CLIENT_SECRET env var."
            ),
            "default": "$GOOGLE_CLIENT_SECRET",
        },
        "mark_read_on_emit": {"type": "boolean", "default": True},
    }

    default_config = {
        "enabled": False,
        "projects": [],
        "oauth_token_path": DEFAULT_TOKEN_PATH,
        "client_secret": "$GOOGLE_CLIENT_SECRET",
        "mark_read_on_emit": True,
    }

    async def initialize(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        self._pollers: list[InboxPoller] = []

        # Read config from global config.yaml (not plugin DB config).  This
        # lets users keep inbox settings next to other daemon config.
        config_svc = ctx.get_service("config")
        inbox_cfg = getattr(config_svc, "inbox", None)
        if not inbox_cfg:
            # Fall back to plugin DB config for backwards compatibility.
            inbox_cfg = {**self.default_config, **(ctx.get_config() or {})}

        if not inbox_cfg.get("enabled"):
            logger.info("InboxPlugin: disabled (set inbox.enabled=true to activate)")
            return

        project_ids = inbox_cfg.get("projects") or []
        if not project_ids:
            logger.info("InboxPlugin: enabled but no projects configured; nothing to do")
            return

        token_path = Path(os.path.expanduser(inbox_cfg.get("oauth_token_path") or DEFAULT_TOKEN_PATH))
        client_secret = resolve_client_secret(inbox_cfg.get("client_secret"))
        mark_read = bool(inbox_cfg.get("mark_read_on_emit", True))

        # Register event types (informational).
        ctx.register_event_type(EVENT_ALLOWLISTED)
        ctx.register_event_type(EVENT_UNKNOWN)

        # Register admin/diagnostic commands.
        ctx.register_command("inbox_status", self.cmd_status)
        ctx.register_command("inbox_reload_allowlist", self.cmd_reload_allowlist)
        ctx.register_command("inbox_list_allowlist", self.cmd_list_allowlist)

        data_dir = config_svc.data_dir or os.path.expanduser("~/.agent-queue")

        # Start one poller per configured project.
        for project_id in project_ids:
            try:
                poller = await self._build_poller(
                    ctx=ctx,
                    project_id=project_id,
                    data_dir=data_dir,
                    token_path=token_path,
                    client_secret=client_secret,
                    mark_read=mark_read,
                )
            except GmailUnavailable as e:
                logger.error(
                    "InboxPlugin: skipping project %s — %s", project_id, e
                )
                continue
            except Exception:
                logger.exception(
                    "InboxPlugin: failed to build poller for %s", project_id
                )
                continue

            await poller.start()
            self._pollers.append(poller)

        logger.info(
            "InboxPlugin initialized (%d poller(s) running: %s)",
            len(self._pollers),
            [p._project_id for p in self._pollers],  # noqa: SLF001
        )

    async def shutdown(self, ctx: PluginContext) -> None:
        for poller in self._pollers:
            try:
                await poller.stop()
            except Exception:
                logger.exception("InboxPlugin: poller %s stop failed", poller._project_id)  # noqa: SLF001
        self._pollers = []

    # ------------------------------------------------------------------
    # Poller construction
    # ------------------------------------------------------------------

    async def _build_poller(
        self,
        *,
        ctx: PluginContext,
        project_id: str,
        data_dir: str,
        token_path: Path,
        client_secret: str,
        mark_read: bool,
    ) -> InboxPoller:
        # Allowlist file — auto-create on first run.
        allowlist_file = allowlist_path_for(data_dir, project_id)
        allowlist = Allowlist(allowlist_file)
        allowlist.ensure_exists(project_id)

        gmail = GmailClient(
            GmailClientConfig(token_path=token_path, client_secret=client_secret)
        )
        # Trigger a credential load up front so init fails loud if the
        # token file is missing or the client_secret is wrong.
        gmail._load_creds()  # noqa: SLF001

        async def _emit(event_type: str, payload: dict) -> None:
            await ctx.emit_event(event_type, payload)

        return InboxPoller(
            project_id=project_id,
            gmail=gmail,
            allowlist=allowlist,
            emit=_emit,
            mark_read_on_emit=mark_read,
        )

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def cmd_status(self, args: dict) -> dict:
        """Return status + stats for all active pollers."""
        pollers = []
        for p in self._pollers:
            pollers.append({
                "project_id": p._project_id,  # noqa: SLF001
                "stats": p.stats,
                "allowlist_path": str(p._allowlist.path),  # noqa: SLF001
                "poll_interval_seconds": p._allowlist.settings.poll_interval_seconds,  # noqa: SLF001
                "allowlist_size": len(p._allowlist.entries),  # noqa: SLF001
            })
        return {"success": True, "pollers": pollers}

    async def cmd_reload_allowlist(self, args: dict) -> dict:
        """Force-reload a project's allowlist from disk."""
        project_id = args.get("project_id")
        for p in self._pollers:
            if p._project_id == project_id:  # noqa: SLF001
                # Force reload by zeroing the cached mtime.
                p._allowlist._mtime = 0.0  # noqa: SLF001
                _ = p._allowlist.entries  # triggers reload  # noqa: SLF001
                return {
                    "success": True,
                    "project_id": project_id,
                    "allowlist_size": len(p._allowlist.entries),  # noqa: SLF001
                }
        return {"success": False, "error": f"No inbox poller for project '{project_id}'"}

    async def cmd_list_allowlist(self, args: dict) -> dict:
        """Return the current allowlist entries for a project."""
        project_id = args.get("project_id")
        for p in self._pollers:
            if p._project_id == project_id:  # noqa: SLF001
                return {
                    "success": True,
                    "project_id": project_id,
                    "path": str(p._allowlist.path),  # noqa: SLF001
                    "entries": [
                        {"email": e.email, "note": e.note}
                        for e in p._allowlist.entries.values()  # noqa: SLF001
                    ],
                    "settings": {
                        "require_spf": p._allowlist.settings.require_spf,  # noqa: SLF001
                        "require_dkim": p._allowlist.settings.require_dkim,  # noqa: SLF001
                        "require_dkim_domain_match": (
                            p._allowlist.settings.require_dkim_domain_match  # noqa: SLF001
                        ),
                        "subject_prefix": p._allowlist.settings.subject_prefix,  # noqa: SLF001
                        "poll_interval_seconds": (
                            p._allowlist.settings.poll_interval_seconds  # noqa: SLF001
                        ),
                    },
                }
        return {"success": False, "error": f"No inbox poller for project '{project_id}'"}
