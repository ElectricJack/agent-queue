"""Gmail API wrapper — reuses google-docs MCP's cached OAuth token.

The ``@a-bonus/google-docs-mcp`` npm package stores a refresh token at
``~/.config/google-docs-mcp/token.json`` in Google's standard
``authorized_user`` format (``type``, ``client_id``, ``refresh_token``).
We construct ``google.oauth2.credentials.Credentials`` from that file,
providing ``client_secret`` separately because the MCP omits it.

The refresh flow is handled transparently by google-auth; we just call
``creds.refresh(Request())`` when the access token expires.  The Gmail
service is built lazily so an import failure doesn't break plugin init.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


class GmailUnavailable(RuntimeError):
    """Raised when google client libs or credentials are missing."""


@dataclass
class GmailClientConfig:
    token_path: Path
    client_secret: str
    user_id: str = "me"  # Gmail convention: 'me' = authenticated user


class GmailClient:
    """Thin async-friendly wrapper over the Gmail REST API.

    The google-api-python-client is synchronous; we run calls via
    ``asyncio.to_thread`` to keep the event loop responsive.  The poller
    makes one batch of calls every 30s, so thread overhead is negligible.
    """

    def __init__(self, config: GmailClientConfig):
        self._config = config
        self._service = None  # lazy
        self._creds = None

    def _load_creds(self):
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
        except ImportError as e:
            raise GmailUnavailable(
                "google-auth not installed; run: pip install -e '.[inbox]'"
            ) from e

        if not self._config.token_path.exists():
            raise GmailUnavailable(
                f"OAuth token not found: {self._config.token_path}. "
                "Run the google-docs MCP once to authenticate."
            )

        data = json.loads(self._config.token_path.read_text())
        client_id = data.get("client_id")
        refresh_token = data.get("refresh_token")
        if not client_id or not refresh_token:
            raise GmailUnavailable(
                f"Token file missing client_id or refresh_token: {self._config.token_path}"
            )
        if not self._config.client_secret:
            raise GmailUnavailable(
                "client_secret not configured; set inbox.client_secret in config.yaml "
                "(or GOOGLE_CLIENT_SECRET env var)"
            )

        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            client_id=client_id,
            client_secret=self._config.client_secret,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=GMAIL_SCOPES,
        )
        # Force an initial refresh to validate credentials.
        creds.refresh(Request())
        self._creds = creds

    def _ensure_service(self):
        if self._service is not None and self._creds and self._creds.valid:
            return self._service
        try:
            from googleapiclient.discovery import build
        except ImportError as e:
            raise GmailUnavailable(
                "google-api-python-client not installed; run: pip install -e '.[inbox]'"
            ) from e

        if self._creds is None or not self._creds.valid:
            self._load_creds()

        self._service = build("gmail", "v1", credentials=self._creds, cache_discovery=False)
        return self._service

    # ------------------------------------------------------------------
    # API methods
    # ------------------------------------------------------------------

    def list_new_message_ids(self, *, history_id: str | None, query: str) -> tuple[list[str], str]:
        """Return (message_ids, new_history_id).

        On first run ``history_id`` is None — we fall back to a
        ``messages.list`` query (e.g. ``is:unread newer_than:1d``).
        After that, each poll uses ``users.history.list`` against the
        stored history_id, which is much cheaper (returns only what
        changed since the last poll).
        """
        service = self._ensure_service()
        if history_id:
            try:
                resp = (
                    service.users()
                    .history()
                    .list(
                        userId=self._config.user_id,
                        startHistoryId=history_id,
                        historyTypes=["messageAdded"],
                    )
                    .execute()
                )
                msg_ids = []
                for h in resp.get("history", []):
                    for added in h.get("messagesAdded", []):
                        m = added.get("message", {})
                        if m.get("id"):
                            msg_ids.append(m["id"])
                new_hid = resp.get("historyId", history_id)
                return msg_ids, new_hid
            except Exception as e:
                # history_id may have expired (7-day retention).  Fall back.
                logger.info(
                    "Gmail history.list failed (%s); falling back to messages.list", e
                )

        resp = (
            service.users()
            .messages()
            .list(userId=self._config.user_id, q=query, maxResults=25)
            .execute()
        )
        msg_ids = [m["id"] for m in resp.get("messages", [])]
        profile = service.users().getProfile(userId=self._config.user_id).execute()
        return msg_ids, str(profile.get("historyId", ""))

    def get_message_metadata(self, message_id: str) -> dict:
        """Fetch headers-only metadata for a message."""
        service = self._ensure_service()
        resp = (
            service.users()
            .messages()
            .get(
                userId=self._config.user_id,
                id=message_id,
                format="metadata",
                metadataHeaders=[
                    "From",
                    "To",
                    "Subject",
                    "Date",
                    "Message-ID",
                    "Authentication-Results",
                ],
            )
            .execute()
        )
        return resp

    def mark_read(self, message_id: str) -> None:
        service = self._ensure_service()
        service.users().messages().modify(
            userId=self._config.user_id,
            id=message_id,
            body={"removeLabelIds": ["UNREAD"]},
        ).execute()


def resolve_client_secret(config_value: str | None) -> str:
    """Resolve the client_secret from config, falling back to env vars.

    Accepts literal strings, ``$VAR`` env references, or None.  If None
    or unresolved, falls back to ``GOOGLE_CLIENT_SECRET``.
    """
    if config_value is None:
        return os.environ.get("GOOGLE_CLIENT_SECRET", "")
    v = str(config_value).strip()
    if v.startswith("$"):
        return os.environ.get(v[1:], "")
    return v
