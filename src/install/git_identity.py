"""Suggesting the installation's Git commit identity, and asking a person to confirm it.

Every commit AQ creates is attributed to the installation default
(``git_identity:`` in ``config.yaml``, see :mod:`src.git.identity`) unless a
project overrides it.  This module is the one place that *suggests* that
default, so ``aq install``'s wizard and ``aq system config git-identity``
offer the same choices with the same labels:

1. the authenticated ``gh`` account for the GitHub host (``github.com`` by
   default) — its public profile email, a verified address when the token can
   already read ``user/emails``, and the account's GitHub noreply address;
2. the global ``git config`` identity, labelled as such and offered after the
   GitHub suggestions (it also lends its name to a GitHub account that has no
   profile name).

Four rules are enforced here rather than left to each caller:

* **Never invent an address.**  An email is suggested only when GitHub or Git
  reported it, or it is the noreply address GitHub documents for an account
  whose numeric id and login ``gh api user`` returned.  Every suggestion
  passes :func:`~src.git.identity.validate_git_email`.
* **Never widen access.**  ``user/emails`` needs a scope a default ``gh``
  login may lack; a refusal (403/404/scope error) is skipped silently.  This
  module never runs ``gh auth refresh`` or asks for a scope.
* **Never block.**  Every probe has a short timeout and an empty stdin (the
  :mod:`src.install.command` runner), so a missing, offline, signed-out or
  hung ``gh`` degrades to manual entry instead of a stalled install.
* **Never read a credential.**  ``gh`` reads its own token; nothing here
  touches it, and only the account's public facts are kept.

Spec: ``docs/specs/git-identity.md``.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from src.git.identity import (
    FALLBACK_IDENTITY,
    GitIdentity,
    GitIdentityError,
    installation_identity,
    validate_git_email,
    validate_git_name,
)

from .command import CommandOutput, CommandRunner, run_command

#: The GitHub host whose ``gh`` account is suggested when none is named.
DEFAULT_HOST = "github.com"

#: Seconds one ``gh api`` call may take.  It is a network round trip, so it
#: gets longer than a local ``git config`` read, but never long enough to make
#: a wizard feel hung.
GH_TIMEOUT = 6.0
#: Seconds one ``git config --global`` read may take.
GIT_TIMEOUT = 3.0

#: ``git_identity.source`` values (informational; recorded with the choice).
SOURCE_MANUAL = "manual"
SOURCE_GIT_CONFIG = "git-config"
SOURCE_GH_PREFIX = "gh:"

#: Provenance labels shown next to each suggestion.
LABEL_PUBLIC = "public profile email"
LABEL_VERIFIED_PRIMARY = "verified primary email"
LABEL_VERIFIED = "verified email"
LABEL_NOREPLY = "GitHub noreply address (keeps your email private)"
LABEL_GIT_CONFIG = "global git config (user.name / user.email)"
NAME_FROM_PROFILE = "name from your GitHub profile"
NAME_FROM_GIT_CONFIG = "name from global git config"
NAME_FROM_LOGIN = "your GitHub login (no profile name set)"

#: What ``discover`` concluded about ``gh``.
GH_AUTHENTICATED = "authenticated"
GH_MISSING = "missing"
GH_UNAUTHENTICATED = "unauthenticated"
GH_UNREACHABLE = "unreachable"
GH_ERROR = "error"

#: The domain GitHub.com's noreply commit addresses use.  Only github.com's
#: format is documented, so no other host gets a noreply suggestion.
NOREPLY_DOMAIN = "users.noreply.github.com"

#: How many suggestions a prompt lists at most.
MAX_SUGGESTIONS = 5

#: The interactive command that sets the default on an existing install.
SETUP_COMMAND = "aq system config git-identity"

#: The one sentence every surface prints while the default is unset.
NOT_CONFIGURED_NOTE = (
    f"Git identity: not configured — AQ commits as {FALLBACK_IDENTITY.formatted()} "
    f"until you run `{SETUP_COMMAND}`"
)

#: Keys accepted under ``settings.git_identity`` in the install input file.
SETTING_KEYS = frozenset({"name", "email", "source"})

Which = Callable[[str], str | None]


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Suggestion:
    """One identity a person may confirm, and where it came from."""

    name: str
    email: str
    #: Provenance of the email, e.g. ``GitHub @octocat: public profile email``.
    label: str
    #: Recorded as ``git_identity.source`` when this suggestion is confirmed.
    source: str
    #: Provenance of the name.
    name_label: str = ""

    @property
    def identity(self) -> GitIdentity:
        return GitIdentity(self.name, self.email)

    def formatted(self) -> str:
        return f"{self.name} <{self.email}>"

    def describe(self) -> str:
        extra = f"; {self.name_label}" if self.name_label else ""
        return f"{self.label}{extra}"

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "email": self.email,
            "label": self.label,
            "source": self.source,
            "name_label": self.name_label,
        }


@dataclass(frozen=True, slots=True)
class GitHubAccount:
    """The public facts ``gh api user`` reported for the active account."""

    host: str
    login: str
    id: int | None = None
    name: str | None = None
    #: The *public* profile email; ``None`` when the account keeps it private.
    email: str | None = None


@dataclass(frozen=True, slots=True)
class Discovery:
    """Everything the probes found, ready to put in front of a person."""

    suggestions: tuple[Suggestion, ...] = ()
    account: GitHubAccount | None = None
    gh_status: str = GH_MISSING
    #: One human-readable line about ``gh`` (empty when it answered).
    gh_note: str = ""
    git_name: str | None = None
    git_email: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "suggestions": [suggestion.to_dict() for suggestion in self.suggestions],
            "github_login": self.account.login if self.account else None,
            "github_host": self.account.host if self.account else None,
            "gh_status": self.gh_status,
            "gh_note": self.gh_note,
        }


@dataclass(frozen=True, slots=True)
class Choice:
    """A confirmed identity and the source to record with it."""

    name: str
    email: str
    source: str = SOURCE_MANUAL

    @property
    def identity(self) -> GitIdentity:
        return GitIdentity(self.name, self.email)

    def formatted(self) -> str:
        return f"{self.name} <{self.email}>"

    def as_setting(self) -> dict[str, str]:
        """The ``git_identity`` body this choice writes."""
        return {"name": self.name, "email": self.email, "source": self.source}


class GitIdentitySettingError(ValueError):
    """An explicit ``git_identity`` setting that cannot be used, and why."""


def gh_source(login: str) -> str:
    return f"{SOURCE_GH_PREFIX}{login}"


def configured_identity(raw: Mapping[str, Any] | None) -> GitIdentity | None:
    """The installation default a raw ``config.yaml`` mapping holds, or ``None``."""
    section = (raw or {}).get("git_identity")
    return installation_identity(SimpleNamespace(git_identity=section))


def configured_source(raw: Mapping[str, Any] | None) -> str:
    section = (raw or {}).get("git_identity")
    value = section.get("source") if isinstance(section, Mapping) else None
    return str(value) if isinstance(value, str) else ""


def parse_setting(value: Any) -> Choice | None:
    """An explicit ``git_identity`` setting as a :class:`Choice`, or ``None`` when absent.

    Raises :class:`GitIdentitySettingError` naming the problem when the setting
    is present but unusable: not a mapping, an unknown key, only one of
    name/email, or a value :mod:`src.git.identity` refuses.  An unattended run that quietly
    ignored a misspelled ``emial:`` would commit as the fallback identity.
    """
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise GitIdentitySettingError("settings.git_identity must be a mapping with name and email")
    unknown = sorted(str(key) for key in set(value) - SETTING_KEYS)
    if unknown:
        raise GitIdentitySettingError(
            f"unknown git_identity setting(s): {', '.join(unknown)} "
            f"(recognised: {', '.join(sorted(SETTING_KEYS))})"
        )
    name, email = value.get("name"), value.get("email")
    if not name and not email:
        return None
    if not name or not email:
        raise GitIdentitySettingError("git_identity needs both name and email")
    try:
        identity = GitIdentity.parse(name, email)
    except GitIdentityError as error:
        raise GitIdentitySettingError(f"git_identity {error}") from error
    source = value.get("source")
    source = str(source).strip()[:120] if source else SOURCE_MANUAL
    return Choice(identity.name, identity.email, source or SOURCE_MANUAL)


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


def _gh_env(environ: Mapping[str, str] | None) -> dict[str, str]:
    """``gh``'s environment with every interactive affordance switched off."""
    env = dict(os.environ if environ is None else environ)
    env.update(
        {
            "GH_PROMPT_DISABLED": "1",
            "GH_NO_UPDATE_NOTIFIER": "1",
            "GH_SPINNER_DISABLED": "1",
            "GH_PAGER": "cat",
            "NO_COLOR": "1",
        }
    )
    return env


#: Fragments of ``gh`` output meaning "no usable login for this host".
_SIGNED_OUT_MARKERS = (
    "gh auth login",
    "not logged",
    "authentication",
    "http 401",
    "bad credentials",
)
#: Fragments meaning "the host could not be reached".
_OFFLINE_MARKERS = (
    "error connecting",
    "could not resolve",
    "dial tcp",
    "no such host",
    "timeout",
)


def _classify_gh_failure(output: CommandOutput, host: str) -> tuple[str, str]:
    """``(status, note)`` for a ``gh api user`` that did not answer."""
    if output.error:
        if "is not installed" in output.error:
            return GH_MISSING, "gh is not installed"
        if "did not finish" in output.error:
            return GH_UNREACHABLE, f"gh did not answer within {GH_TIMEOUT:g}s (offline?)"
        return GH_ERROR, f"gh could not be run: {output.error}"
    text = f"{output.stderr}\n{output.stdout}".lower()
    if any(marker in text for marker in _SIGNED_OUT_MARKERS):
        return GH_UNAUTHENTICATED, f"gh is not signed in to {host}"
    if any(marker in text for marker in _OFFLINE_MARKERS):
        return GH_UNREACHABLE, f"gh could not reach {host} (offline?)"
    return GH_ERROR, f"gh api user failed: {output.message(limit=160)}"


def _json(output: CommandOutput) -> Any:
    try:
        return json.loads(output.stdout or "null")
    except ValueError:
        return None


def probe_github_account(
    gh: str,
    *,
    runner: CommandRunner,
    host: str = DEFAULT_HOST,
    environ: Mapping[str, str] | None = None,
) -> tuple[GitHubAccount | None, str, str]:
    """The active ``gh`` account on *host*: ``(account, status, note)``."""
    output = runner(
        [gh, "api", "--hostname", host, "user"], timeout=GH_TIMEOUT, env=_gh_env(environ)
    )
    if not output.ok:
        status, note = _classify_gh_failure(output, host)
        return None, status, note
    payload = _json(output)
    if not isinstance(payload, Mapping) or not isinstance(payload.get("login"), str):
        return None, GH_ERROR, "gh api user returned no account"
    raw_id = payload.get("id")
    account = GitHubAccount(
        host=host,
        login=payload["login"],
        id=raw_id if isinstance(raw_id, int) and not isinstance(raw_id, bool) else None,
        name=payload.get("name") if isinstance(payload.get("name"), str) else None,
        email=payload.get("email") if isinstance(payload.get("email"), str) else None,
    )
    return account, GH_AUTHENTICATED, ""


def probe_verified_emails(
    gh: str,
    *,
    runner: CommandRunner,
    host: str = DEFAULT_HOST,
    environ: Mapping[str, str] | None = None,
) -> list[tuple[str, bool]]:
    """``[(email, primary)]`` for every verified address the token may read.

    ``user/emails`` needs the ``user:email`` (or ``user``) scope.  A token
    without it answers 404/403; that — and any other failure — is an empty
    list, never a request for a broader scope.
    """
    output = runner(
        [gh, "api", "--hostname", host, "user/emails"],
        timeout=GH_TIMEOUT,
        env=_gh_env(environ),
    )
    if not output.ok:
        return []
    payload = _json(output)
    if not isinstance(payload, list):
        return []
    found: list[tuple[str, bool]] = []
    for entry in payload:
        if not isinstance(entry, Mapping) or entry.get("verified") is not True:
            continue
        email = entry.get("email")
        if isinstance(email, str) and email:
            found.append((email, entry.get("primary") is True))
    # Primary first; the order GitHub gave is kept otherwise.
    return sorted(found, key=lambda item: not item[1])


def probe_git_config(
    git: str,
    *,
    runner: CommandRunner,
) -> tuple[str | None, str | None]:
    """``git config --global user.name`` / ``user.email``, each ``None`` when unset."""
    values: list[str | None] = []
    for key in ("user.name", "user.email"):
        output = runner([git, "config", "--global", "--get", key], timeout=GIT_TIMEOUT)
        values.append((output.out or None) if output.ok else None)
    return values[0], values[1]


def noreply_address(account: GitHubAccount) -> str | None:
    """GitHub.com's ``<id>+<login>@users.noreply.github.com``, when it is knowable."""
    if account.host != DEFAULT_HOST or account.id is None or not account.login:
        return None
    return f"{account.id}+{account.login}@{NOREPLY_DOMAIN}"


def _valid_name(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return validate_git_name(value)
    except GitIdentityError:
        return None


def _valid_email(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return validate_git_email(value)
    except GitIdentityError:
        return None


def build_suggestions(
    account: GitHubAccount | None,
    *,
    verified: Sequence[tuple[str, bool]] = (),
    git_name: str | None = None,
    git_email: str | None = None,
) -> tuple[Suggestion, ...]:
    """Order and label what the probes found.  Pure: no I/O.

    GitHub suggestions come first — public profile email, then verified
    addresses (primary first), then the noreply address — and the global git
    config pair last.  Anything that fails validation is dropped, never
    "repaired", and a duplicate address is listed once.
    """
    suggestions: list[Suggestion] = []
    seen: set[tuple[str, str]] = set()

    def add(suggestion: Suggestion) -> None:
        key = (suggestion.name, suggestion.email.lower())
        if key not in seen:
            seen.add(key)
            suggestions.append(suggestion)

    config_name = _valid_name(git_name)
    if account is not None:
        name = _valid_name(account.name)
        name_label = NAME_FROM_PROFILE
        if name is None and config_name is not None:
            name, name_label = config_name, NAME_FROM_GIT_CONFIG
        if name is None:
            name, name_label = _valid_name(account.login), NAME_FROM_LOGIN
        if name is not None:
            prefix = f"GitHub @{account.login}"
            if account.host != DEFAULT_HOST:
                prefix += f" on {account.host}"
            source = gh_source(account.login)
            public = _valid_email(account.email)
            if public:
                add(Suggestion(name, public, f"{prefix}: {LABEL_PUBLIC}", source, name_label))
            for email, primary in verified:
                address = _valid_email(email)
                if address is None or address.lower().endswith(f"@{NOREPLY_DOMAIN}"):
                    continue
                label = LABEL_VERIFIED_PRIMARY if primary else LABEL_VERIFIED
                add(Suggestion(name, address, f"{prefix}: {label}", source, name_label))
            noreply = _valid_email(noreply_address(account))
            if noreply:
                add(Suggestion(name, noreply, f"{prefix}: {LABEL_NOREPLY}", source, name_label))
    config_email = _valid_email(git_email)
    if config_name and config_email:
        add(Suggestion(config_name, config_email, LABEL_GIT_CONFIG, SOURCE_GIT_CONFIG))
    return tuple(suggestions[:MAX_SUGGESTIONS])


def discover(
    *,
    runner: CommandRunner | None = None,
    which: Which | None = None,
    host: str = DEFAULT_HOST,
    environ: Mapping[str, str] | None = None,
) -> Discovery:
    """Probe ``gh`` and ``git config`` and return labelled suggestions.

    Never raises for anything a host can do: a missing, signed-out, offline or
    hung ``gh`` is a :class:`Discovery` with a note and whatever ``git config``
    offered, and the caller falls back to manual entry.
    """
    execute = runner or run_command
    lookup = which or shutil.which
    account: GitHubAccount | None = None
    verified: list[tuple[str, bool]] = []
    gh = lookup("gh")
    if gh is None:
        status, note = GH_MISSING, "gh is not installed"
    else:
        account, status, note = probe_github_account(
            gh, runner=execute, host=host, environ=environ
        )
        if account is not None and _valid_email(account.email) is None:
            verified = probe_verified_emails(gh, runner=execute, host=host, environ=environ)
    git = lookup("git")
    git_name, git_email = probe_git_config(git, runner=execute) if git else (None, None)
    return Discovery(
        suggestions=build_suggestions(
            account, verified=verified, git_name=git_name, git_email=git_email
        ),
        account=account,
        gh_status=status,
        gh_note=note,
        git_name=git_name,
        git_email=git_email,
    )


def discover_suggestions(
    runner: CommandRunner | None = None,
    which: Which | None = None,
    host: str = DEFAULT_HOST,
) -> list[Suggestion]:
    """Just the suggestions :func:`discover` would offer."""
    return list(discover(runner=runner, which=which, host=host).suggestions)


def source_for(name: str, email: str, suggestions: Sequence[Suggestion]) -> str:
    """The source of the suggestion *name*/*email* is, or ``manual`` when edited."""
    for suggestion in suggestions:
        if suggestion.name == name and suggestion.email.lower() == email.lower():
            return suggestion.source
    return SOURCE_MANUAL


# ---------------------------------------------------------------------------
# The interview
# ---------------------------------------------------------------------------


def _optional(check: Callable[[Any], str]) -> Callable[[str], str]:
    """A click ``value_proc`` that accepts an empty answer (skip) or a valid value."""
    import click

    def proc(value: str) -> str:
        text = (value or "").strip()
        if not text:
            return ""
        try:
            return check(text)
        except GitIdentityError as error:
            raise click.BadParameter(error.message) from error

    return proc


def _required(check: Callable[[Any], str]) -> Callable[[str], str]:
    import click

    def proc(value: str) -> str:
        try:
            return check(value)
        except GitIdentityError as error:
            raise click.BadParameter(error.message) from error

    return proc


def interview(
    discovery: Discovery,
    *,
    current: GitIdentity | None = None,
    current_source: str = "",
    indent: str = "  ",
) -> Choice | None:
    """Show the suggestions, let a person confirm or edit one, and return it.

    ``None`` means nothing should be written: the person chose to set it up
    later, or kept the *current* identity.  Every line goes to stderr, so a
    caller parsing stdout is never handed a prompt.  An invalid name or email
    is refused by :mod:`src.git.identity`'s validators and asked again.
    """
    import click

    def say(line: str = "") -> None:
        click.echo(f"{indent}{line}" if line else "", err=True)

    suggestions = discovery.suggestions
    say("Git commit identity — AQ's commits in your projects use this name and email")
    say("(a project can override it in its settings).")
    if current is not None:
        origin = f" ({current_source})" if current_source else ""
        say(f"Current: {current.formatted()}{origin}")
    if discovery.gh_note:
        say(f"GitHub: {discovery.gh_note}; enter an identity by hand if you like.")
    if suggestions:
        say("Suggestions:")
        for index, suggestion in enumerate(suggestions, start=1):
            say(f"  {index}. {suggestion.formatted()}")
            say(f"     {suggestion.describe()}")
        numbers = "1" if len(suggestions) == 1 else f"1-{len(suggestions)}"
        keep = ", k to keep the current one" if current is not None else ""
        options = {str(index) for index in range(1, len(suggestions) + 1)} | {"e", "s"}
        if current is not None:
            options.add("k")

        def pick(value: str) -> str:
            text = (value or "").strip().lower()
            if text not in options:
                raise click.BadParameter(f"answer {numbers}, e{', k' if current else ''} or s")
            return text

        answer = click.prompt(
            f"{indent}Use {numbers}, e to enter your own{keep}, or s to set it up later",
            default="k" if current is not None else "1",
            value_proc=pick,
            err=True,
        )
        if answer in ("s", "k"):
            return None
        if answer != "e":
            chosen = suggestions[int(answer) - 1]
            return Choice(chosen.name, chosen.email, chosen.source)
        first = suggestions[0]
        name = click.prompt(
            f"{indent}Commit name", default=first.name, value_proc=_required(validate_git_name),
            err=True,
        )
        email = click.prompt(
            f"{indent}Commit email",
            default=first.email,
            value_proc=_required(validate_git_email),
            err=True,
        )
        return Choice(name, email, source_for(name, email, suggestions))

    if discovery.gh_status == GH_AUTHENTICATED or discovery.git_name or discovery.git_email:
        say("No usable suggestion was found; enter the identity to commit as.")
    later = "Enter to keep the current one" if current is not None else "Enter to set it up later"
    name = click.prompt(
        f"{indent}Commit name ({later})",
        default="",
        show_default=False,
        value_proc=_optional(validate_git_name),
        err=True,
    )
    if not name:
        return None
    email = click.prompt(
        f"{indent}Commit email ({later})",
        default="",
        show_default=False,
        value_proc=_optional(validate_git_email),
        err=True,
    )
    if not email:
        return None
    return Choice(name, email, SOURCE_MANUAL)


__all__ = [
    "DEFAULT_HOST",
    "GH_AUTHENTICATED",
    "GH_ERROR",
    "GH_MISSING",
    "GH_TIMEOUT",
    "GH_UNAUTHENTICATED",
    "GH_UNREACHABLE",
    "GIT_TIMEOUT",
    "LABEL_GIT_CONFIG",
    "LABEL_NOREPLY",
    "LABEL_PUBLIC",
    "LABEL_VERIFIED",
    "LABEL_VERIFIED_PRIMARY",
    "NOT_CONFIGURED_NOTE",
    "SETTING_KEYS",
    "SETUP_COMMAND",
    "SOURCE_GIT_CONFIG",
    "SOURCE_MANUAL",
    "Choice",
    "Discovery",
    "GitHubAccount",
    "GitIdentitySettingError",
    "Suggestion",
    "build_suggestions",
    "configured_identity",
    "configured_source",
    "discover",
    "discover_suggestions",
    "gh_source",
    "interview",
    "noreply_address",
    "parse_setting",
    "probe_git_config",
    "probe_github_account",
    "probe_verified_emails",
    "source_for",
]
