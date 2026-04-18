"""Parse Authentication-Results headers to extract SPF/DKIM verdicts.

Gmail (and other compliant MTAs) prepend an ``Authentication-Results:``
header to delivered mail, summarising the results of its own verification
checks.  This module parses that header — we do not re-verify the
signatures ourselves, because Gmail's verdict is authoritative for mail
it has already accepted.

Format (RFC 8601)::

    Authentication-Results: mx.google.com;
           dkim=pass header.i=@example.com header.s=selector header.b=signature;
           spf=pass (google.com: domain of sender@example.com designates ...)
             smtp.mailfrom=sender@example.com;
           dmarc=pass (p=REJECT sp=REJECT dis=NONE) header.from=example.com
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from email.utils import parseaddr

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuthResults:
    """Parsed SPF/DKIM/DMARC verdicts from an Authentication-Results header."""

    spf_pass: bool
    dkim_pass: bool
    dmarc_pass: bool
    dkim_signing_domain: str  # header.i / header.d from the dkim= entry
    raw: str  # the header verbatim, for debugging

    def dkim_matches_from_domain(self, from_header: str) -> bool:
        """Whether the DKIM signing domain aligns with the From: domain.

        This is what DMARC alignment enforces.  Without it, an attacker
        with a valid DKIM signature from an unrelated domain could still
        spoof the From: header.

        Accepts three forms of alignment:

        1. Direct / subdomain match (DMARC relaxed mode).
        2. Google Workspace delegated DKIM — a customer domain without
           a custom DKIM key is signed by Google as
           ``<domain-with-dots-as-dashes>.<selector>.gappssmtp.com``.
           e.g. ``mossandspade.com`` → ``mossandspade-com.20251104.gappssmtp.com``.
        """
        if not self.dkim_signing_domain:
            return False
        from_domain = _extract_domain(from_header)
        if not from_domain:
            return False
        dkim = self.dkim_signing_domain.lstrip("@").lower()
        f = from_domain.lower()

        # Direct or subdomain alignment.
        if f == dkim or f.endswith("." + dkim) or dkim.endswith("." + f):
            return True

        # Google Workspace delegated signing.  The prefix check is
        # directional — we derive the expected label from the From: domain
        # and require DKIM to match, so an attacker can't substitute an
        # arbitrary gappssmtp prefix.
        if dkim.endswith(".gappssmtp.com"):
            expected_prefix = f.replace(".", "-")
            first_label = dkim.split(".", 1)[0]
            if first_label == expected_prefix:
                return True

        return False


# RFC 8601 method=result patterns.  Each method appears as
# "method=result" possibly followed by "(text)" and "property=value"
# pairs, all separated by whitespace.  Separator between methods is ";".
_METHOD_RE = re.compile(
    r"\b(?P<method>spf|dkim|dmarc)=(?P<result>\w+)",
    re.IGNORECASE,
)
_DKIM_DOMAIN_RE = re.compile(
    r"\bdkim=pass\b[^;]*?\bheader\.(?:d|i)=(?P<domain>[^\s;]+)",
    re.IGNORECASE,
)


def parse_authentication_results(header_value: str) -> AuthResults:
    """Parse an Authentication-Results header value.

    Returns :class:`AuthResults` with all three verdicts defaulting to
    False when their method isn't present (fail-closed).
    """
    if not header_value:
        return AuthResults(False, False, False, "", "")

    verdicts: dict[str, str] = {}
    for m in _METHOD_RE.finditer(header_value):
        method = m.group("method").lower()
        # First occurrence wins (some MTAs report multiple DKIM sig results).
        verdicts.setdefault(method, m.group("result").lower())

    dkim_domain = ""
    m = _DKIM_DOMAIN_RE.search(header_value)
    if m:
        dkim_domain = m.group("domain").strip()

    return AuthResults(
        spf_pass=verdicts.get("spf") == "pass",
        dkim_pass=verdicts.get("dkim") == "pass",
        dmarc_pass=verdicts.get("dmarc") == "pass",
        dkim_signing_domain=dkim_domain,
        raw=header_value,
    )


def _extract_domain(from_header: str) -> str:
    _, addr = parseaddr(from_header or "")
    if "@" not in addr:
        return ""
    return addr.rsplit("@", 1)[-1].strip().lower()


def extract_from_address(from_header: str) -> str:
    """Return the bare email from a From: header, lowercased."""
    _, addr = parseaddr(from_header or "")
    return addr.strip().lower()
