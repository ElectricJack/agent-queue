"""Allowlist file loading, auto-creation, and hot-reload.

The allowlist lives at
``vault/projects/{project_id}/inbox/allowlist.yaml`` — outside the memory
tree so agents can't rewrite it via the memory extractor.  It declares
which verified ``From:`` addresses produce ``email.received.allowlisted``
events; everything else produces ``email.received.unknown``.

The security boundary is the ``Decision`` returned by
:meth:`Allowlist.classify`.  The poller consults this before emitting,
and the decision is based solely on parsed email headers + the file on
disk — no LLM is involved.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


DEFAULT_ALLOWLIST_TEMPLATE = """\
# aq-inbox allowlist for {project_id}
#
# Senders listed here produce email.received.allowlisted events.
# Everything else produces email.received.unknown events.
# Route the two to different playbooks — that's your security boundary.
#
# SPF/DKIM verification always runs; the settings below control whether
# failures downgrade the event to 'unknown' or block it entirely.

allowlist:
  # - email: someone@example.com
  #   note: why this sender is trusted

settings:
  # SPF/DKIM must pass for an email to be considered 'allowlisted'.
  # When false, allowlist membership alone is sufficient (not recommended).
  require_spf: true
  require_dkim: true

  # DKIM signing domain must match the From: domain.  This is what DMARC
  # enforces — without it, an attacker with a valid DKIM signature from
  # any other domain could impersonate allowlisted senders.
  require_dkim_domain_match: true

  # Optional: require this prefix in the Subject: for allowlisted mail.
  # Mail from allowlisted senders WITHOUT the prefix downgrades to 'unknown'.
  # Leave empty to disable.
  subject_prefix: ""

  # How often to poll Gmail (seconds).  30 is the recommended default.
  poll_interval_seconds: 30
"""


@dataclass(frozen=True)
class AllowlistEntry:
    email: str
    note: str = ""


@dataclass(frozen=True)
class AllowlistSettings:
    require_spf: bool = True
    require_dkim: bool = True
    require_dkim_domain_match: bool = True
    subject_prefix: str = ""
    poll_interval_seconds: int = 30


@dataclass(frozen=True)
class Decision:
    """Result of classifying an incoming email."""

    allowlisted: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)
    matched_entry: AllowlistEntry | None = None


class Allowlist:
    """Per-project allowlist loaded from YAML with mtime-based hot-reload.

    The file is re-read lazily when its mtime changes — no watchdog
    needed.  Every poll cycle calls :meth:`classify`, which first calls
    :meth:`_maybe_reload`.  File reads are cheap relative to an HTTP
    round-trip to Gmail.
    """

    def __init__(self, path: Path | str):
        self._path = Path(path)
        self._mtime: float = 0.0
        self._entries: dict[str, AllowlistEntry] = {}
        self._settings: AllowlistSettings = AllowlistSettings()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def settings(self) -> AllowlistSettings:
        self._maybe_reload()
        return self._settings

    @property
    def entries(self) -> dict[str, AllowlistEntry]:
        self._maybe_reload()
        return dict(self._entries)

    def ensure_exists(self, project_id: str) -> bool:
        """Create the file with a default template if absent.

        Returns True if the file was created, False if it already existed.
        """
        if self._path.exists():
            return False
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(DEFAULT_ALLOWLIST_TEMPLATE.format(project_id=project_id))
        logger.info("Created allowlist template: %s", self._path)
        return True

    def _maybe_reload(self) -> None:
        try:
            mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            self._mtime = 0.0
            self._entries = {}
            self._settings = AllowlistSettings()
            return
        if mtime == self._mtime:
            return
        self._load()
        self._mtime = mtime

    def _load(self) -> None:
        try:
            raw = yaml.safe_load(self._path.read_text()) or {}
        except yaml.YAMLError as e:
            logger.error("Allowlist YAML parse failed %s: %s", self._path, e)
            return
        except OSError as e:
            logger.error("Allowlist read failed %s: %s", self._path, e)
            return

        entries: dict[str, AllowlistEntry] = {}
        for item in raw.get("allowlist") or []:
            if not isinstance(item, dict):
                logger.warning("Allowlist entry must be a mapping, skipping: %r", item)
                continue
            email = str(item.get("email", "")).strip().lower()
            if not email or "@" not in email:
                logger.warning("Allowlist entry missing valid email, skipping: %r", item)
                continue
            entries[email] = AllowlistEntry(email=email, note=str(item.get("note", "")))

        settings_raw = raw.get("settings") or {}
        self._settings = AllowlistSettings(
            require_spf=bool(settings_raw.get("require_spf", True)),
            require_dkim=bool(settings_raw.get("require_dkim", True)),
            require_dkim_domain_match=bool(
                settings_raw.get("require_dkim_domain_match", True)
            ),
            subject_prefix=str(settings_raw.get("subject_prefix", "")).strip(),
            poll_interval_seconds=int(settings_raw.get("poll_interval_seconds", 30)),
        )
        self._entries = entries
        logger.info(
            "Allowlist loaded: %d sender(s), settings=%s (%s)",
            len(entries),
            self._settings,
            self._path,
        )

    def classify(
        self,
        *,
        from_addr: str,
        subject: str,
        spf_pass: bool,
        dkim_pass: bool,
        dkim_domain_matches: bool,
    ) -> Decision:
        """Classify a parsed email against the current allowlist rules.

        All inputs come from the email poller after parsing headers.
        The function itself is pure — no I/O beyond the mtime check.
        """
        self._maybe_reload()

        reasons: list[str] = []
        normalized = (from_addr or "").strip().lower()
        entry = self._entries.get(normalized)

        if entry is None:
            reasons.append("sender_not_in_allowlist")

        if self._settings.require_spf and not spf_pass:
            reasons.append("spf_failed")
        if self._settings.require_dkim and not dkim_pass:
            reasons.append("dkim_failed")
        if self._settings.require_dkim_domain_match and not dkim_domain_matches:
            reasons.append("dkim_domain_mismatch")
        if self._settings.subject_prefix:
            if not (subject or "").startswith(self._settings.subject_prefix):
                reasons.append("subject_prefix_missing")

        return Decision(
            allowlisted=(entry is not None and not reasons),
            reasons=tuple(reasons),
            matched_entry=entry,
        )
