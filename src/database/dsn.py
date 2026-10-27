"""Credential redaction for database diagnostics without loading an adapter."""

import re


#: ``scheme://user:password@rest`` — the password is group 2.
_DSN_CREDENTIALS_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*://[^/\s:@]*:)([^/\s@]*)(@)")


def redact_dsn(url: str) -> str:
    """*url* with any password in its authority replaced by ``***``.

    Log lines outlive the process and get pasted into issues; a DSN is the
    one config value that routinely carries a live credential.
    """
    return _DSN_CREDENTIALS_RE.sub(r"\1***\3", str(url or ""))
