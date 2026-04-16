"""Per-project inbox poller.

One :class:`InboxPoller` instance per configured project.  Each poller
runs its own async loop, calls Gmail every N seconds, parses each new
message's headers, consults the project's allowlist, and emits either
``email.received.allowlisted`` or ``email.received.unknown`` on the
event bus.

The emit decision is deterministic — see
:mod:`src.plugins.internal.inbox.allowlist` for the classifier.  No LLM
is involved in routing.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from src.plugins.internal.inbox.allowlist import Allowlist
from src.plugins.internal.inbox.auth import extract_from_address, parse_authentication_results
from src.plugins.internal.inbox.gmail_client import GmailClient, GmailUnavailable

logger = logging.getLogger(__name__)


EVENT_ALLOWLISTED = "email.received.allowlisted"
EVENT_UNKNOWN = "email.received.unknown"
# Default bootstrap query used on first run (before we have a history_id)
# and after history expiry.  Scoped to prevent re-processing old mail.
DEFAULT_BOOTSTRAP_QUERY = "is:unread newer_than:1d"


class InboxPoller:
    def __init__(
        self,
        *,
        project_id: str,
        gmail: GmailClient,
        allowlist: Allowlist,
        emit: Any,  # async callable: (event_type, payload) -> None
        bootstrap_query: str = DEFAULT_BOOTSTRAP_QUERY,
        mark_read_on_emit: bool = True,
    ):
        self._project_id = project_id
        self._gmail = gmail
        self._allowlist = allowlist
        self._emit = emit
        self._bootstrap_query = bootstrap_query
        self._mark_read = mark_read_on_emit

        self._task: asyncio.Task | None = None
        self._running = False
        self._history_id: str | None = None
        self._seen_ids: set[str] = set()
        self._stats = {
            "polls": 0,
            "emitted_allowlisted": 0,
            "emitted_unknown": 0,
            "errors": 0,
        }

    @property
    def stats(self) -> dict:
        return dict(self._stats)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name=f"inbox:{self._project_id}")
        logger.info("InboxPoller started for %s", self._project_id)

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info(
            "InboxPoller stopped for %s (stats=%s)", self._project_id, self._stats
        )

    async def _loop(self) -> None:
        # Consume initial history without emitting so the first poll sees
        # a baseline (prevents re-processing an entire day's backlog).
        await self._bootstrap_history()

        while self._running:
            interval = max(5, int(self._allowlist.settings.poll_interval_seconds))
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._stats["errors"] += 1
                logger.exception("InboxPoller %s: poll failed", self._project_id)
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise

    async def _bootstrap_history(self) -> None:
        """Record current history_id without emitting events for existing mail.

        Without this, the first poll after startup would emit events for
        every message returned by the bootstrap query (up to a day of
        mail).  We anchor on the current profile.historyId instead.
        """
        def _fetch():
            svc = self._gmail._ensure_service()  # noqa: SLF001
            profile = svc.users().getProfile(userId=self._gmail._config.user_id).execute()  # noqa: SLF001
            return str(profile.get("historyId", ""))

        try:
            self._history_id = await asyncio.to_thread(_fetch)
            logger.info(
                "InboxPoller %s: bootstrap history_id=%s",
                self._project_id,
                self._history_id,
            )
        except GmailUnavailable as e:
            logger.error("InboxPoller %s: gmail unavailable at bootstrap: %s", self._project_id, e)
        except Exception:
            logger.exception("InboxPoller %s: bootstrap failed", self._project_id)

    async def _poll_once(self) -> None:
        self._stats["polls"] += 1

        ids, new_hid = await asyncio.to_thread(
            self._gmail.list_new_message_ids,
            history_id=self._history_id,
            query=self._bootstrap_query,
        )
        self._history_id = new_hid

        for msg_id in ids:
            if msg_id in self._seen_ids:
                continue
            self._seen_ids.add(msg_id)
            # Cap the set so it doesn't grow unboundedly.
            if len(self._seen_ids) > 10000:
                self._seen_ids = set(list(self._seen_ids)[-5000:])

            try:
                await self._process_message(msg_id)
            except Exception:
                self._stats["errors"] += 1
                logger.exception(
                    "InboxPoller %s: process_message %s failed",
                    self._project_id,
                    msg_id,
                )

    async def _process_message(self, msg_id: str) -> None:
        meta = await asyncio.to_thread(self._gmail.get_message_metadata, msg_id)
        headers = {h["name"].lower(): h["value"] for h in meta.get("payload", {}).get("headers", [])}
        from_hdr = headers.get("from", "")
        subject = headers.get("subject", "")
        auth_raw = headers.get("authentication-results", "")
        thread_id = meta.get("threadId", "")

        auth = parse_authentication_results(auth_raw)
        from_addr = extract_from_address(from_hdr)

        decision = self._allowlist.classify(
            from_addr=from_addr,
            subject=subject,
            spf_pass=auth.spf_pass,
            dkim_pass=auth.dkim_pass,
            dkim_domain_matches=auth.dkim_matches_from_domain(from_hdr),
        )

        event_type = EVENT_ALLOWLISTED if decision.allowlisted else EVENT_UNKNOWN
        payload = {
            "project_id": self._project_id,
            "message_id": msg_id,
            "thread_id": thread_id,
            "from": from_addr,
            "from_header": from_hdr,
            "subject": subject,
            "snippet": meta.get("snippet", ""),
            "received_at": int(meta.get("internalDate", 0)) // 1000,
            "auth": {
                "spf_pass": auth.spf_pass,
                "dkim_pass": auth.dkim_pass,
                "dmarc_pass": auth.dmarc_pass,
                "dkim_signing_domain": auth.dkim_signing_domain,
                "dkim_domain_matches_from": auth.dkim_matches_from_domain(from_hdr),
            },
            "classification_reasons": list(decision.reasons),
        }

        await self._emit(event_type, payload)
        if decision.allowlisted:
            self._stats["emitted_allowlisted"] += 1
        else:
            self._stats["emitted_unknown"] += 1

        logger.info(
            "InboxPoller %s: emitted %s msg=%s from=%s reasons=%s",
            self._project_id,
            event_type,
            msg_id,
            from_addr,
            decision.reasons,
        )

        if self._mark_read:
            try:
                await asyncio.to_thread(self._gmail.mark_read, msg_id)
            except Exception:
                logger.exception(
                    "InboxPoller %s: mark_read %s failed",
                    self._project_id,
                    msg_id,
                )


def allowlist_path_for(data_dir: str | Path, project_id: str) -> Path:
    """Canonical on-disk path for a project's allowlist file."""
    return Path(data_dir) / "vault" / "projects" / project_id / "inbox" / "allowlist.yaml"
