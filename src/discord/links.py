"""HTTP links in Discord text, independent of dashboard reachability policy."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable
from urllib.parse import quote, urlsplit, urlunsplit

LINK_UNAVAILABLE = "This link could not be built as an absolute HTTP(S) URL."
LINKS_TOO_LONG = "Some links could not fit in this post; see the full text in the dashboard."


def text_units(text: str) -> int:
    """Discord counts astral characters as two UTF-16 units."""
    return len(text) + sum(ord(char) > 0xFFFF for char in text)


def text_prefix(text: str, room: int) -> str:
    """A prefix within a UTF-16 budget, without splitting a character."""
    used = 0
    for index, char in enumerate(text):
        used += 2 if ord(char) > 0xFFFF else 1
        if used > room:
            return text[:index]
    return text


# Recognise existing Markdown and angle links before bare URLs. A Markdown
# destination can contain balanced parentheses, and adjacent Markdown links
# remain separate tokens.
LINK_PATTERN = re.compile(
    r"\[(?P<label>[^\]\n]+)\]\((?P<target><https?://[^>\n]+>|"
    r"https?://(?:[^\s()]|\([^()\s]*\))*)\)"
    r"|<(?P<angle>https?://[^>\n]+)>|(?P<bare>https?://[^\s<>`]+)",
    re.IGNORECASE,
)


def http_url(value: str) -> str:
    """Validate an absolute URL and escape characters that break link markup.

    IPs, IPv6 and single-label tailnet hosts are valid destinations. This does
    not infer a scheme or claim that a host is reachable from the reader.
    """
    value = value.strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("URL contains control characters")
    parts = urlsplit(value)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ValueError("URL requires an HTTP(S) scheme and host")
    host = parts.hostname
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        host = host.encode("idna").decode("ascii")
        if not re.fullmatch(r"[a-zA-Z0-9_-]+(?:\.[a-zA-Z0-9_-]+)*\.?", host):
            raise ValueError("URL host is invalid") from None
    else:
        host = f"[{address}]" if address.version == 6 else str(address)
    if parts.username is not None or parts.password is not None:
        raise ValueError("URL contains credentials")
    port = parts.port  # Also validates the port range.
    authority = host + (f":{port}" if port is not None else "")
    return urlunsplit(
        (
            parts.scheme.lower(),
            authority,
            quote(re.sub(r"%(?![0-9a-fA-F]{2})", "%25", parts.path), safe="/%:@!$&'*+,;=~-"),
            quote(re.sub(r"%(?![0-9a-fA-F]{2})", "%25", parts.query), safe="/%?:@!$&'*+,;=~-"),
            quote(re.sub(r"%(?![0-9a-fA-F]{2})", "%25", parts.fragment), safe="/%?:@!$&'*+,;=~-"),
        )
    )


def hyperlink(
    value: str, *, label: str = "Dashboard", unavailable_notice: str = LINK_UNAVAILABLE
) -> str:
    """Explicit clickable markup, including for hosts without a public suffix."""
    try:
        result = f"[{label}]({http_url(value)})"
        # Leave room for the delivery marker and visible failure text below
        # Discord's 2000-character ceiling, including UTF-16 accounting.
        if text_units(result) > 1800:
            return LINKS_TOO_LONG
        return result
    except (ValueError, UnicodeError):
        return unavailable_notice


def format_links(text: str, clean: Callable[[str], str]) -> str:
    """Clean prose and labels without mention escaping inside URL destinations."""

    def prose(value: str) -> str:
        if not value.strip():
            return value
        leading = value[: len(value) - len(value.lstrip())]
        trailing = value[len(value.rstrip()) :]
        return leading + clean(value.strip()) + trailing

    result: list[str] = []
    end = 0
    for match in LINK_PATTERN.finditer(text):
        result.append(prose(text[end : match.start()]))
        url = match["target"] or match["angle"] or match["bare"] or ""
        suffix = ""
        if match["bare"]:
            # Sentence punctuation and unmatched closing parentheses are prose.
            while url and (
                url[-1] in ".,!?;:" or (url[-1] == ")" and url.count(")") > url.count("("))
            ):
                suffix = url[-1] + suffix
                url = url[:-1]
        if url.startswith("<") and url.endswith(">"):
            url = url[1:-1]
        result.append(hyperlink(url, label=clean(match["label"] or "Link").strip() or "Link"))
        result.append(suffix)
        end = match.end()
    result.append(prose(text[end:]))
    return "".join(result)


def links_in(text: str) -> list[str]:
    """Complete links and their failure notices, in order without duplicates."""
    tokens = [(match.start(), match.group()) for match in LINK_PATTERN.finditer(text)]
    for notice in (LINK_UNAVAILABLE, LINKS_TOO_LONG):
        if (position := text.find(notice)) >= 0:
            tokens.append((position, notice))
    return list(dict.fromkeys(value for _, value in sorted(tokens)))


def reserve_links(links: list[str], *, room: int) -> list[str]:
    """Keep whole links within a budget; explain any that cannot fit."""
    if text_units("\n".join(links)) <= room:
        return links
    notice = LINKS_TOO_LONG if room >= len(LINKS_TOO_LONG) else "Link too long."
    if room < len(notice):
        return ["…"] if room > 0 else []
    kept: list[str] = []
    for link in links:
        if text_units("\n".join([*kept, link, notice])) <= room:
            kept.append(link)
    return [*kept, notice]
