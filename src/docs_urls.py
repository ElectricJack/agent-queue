"""Stable documentation URL helpers shared by configuration and contracts."""

from __future__ import annotations


DEFAULT_DOCS_BASE_URL = "https://github.com/ElectricJack/agent-queue/blob/main/docs/"


def command_docs_url(base_url: str, command_name: str) -> str:
    """Return the reference page for one contracted playbook command.

    ``docs.base_url`` is configured as a directory URL.  Normalising its
    trailing slash here keeps both the default and a deployment override from
    producing a malformed double- or missing-slash link.
    """
    return f"{base_url.rstrip('/')}/reference/playbook-commands/{command_name}.md"
