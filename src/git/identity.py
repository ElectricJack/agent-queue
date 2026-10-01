"""The one Git commit identity AQ writes with — resolution and validation.

Every commit AQ creates or causes (worker sessions, ``aq git commit``,
checkpoints, owner recovery, integration candidates and promotions, project
onboarding) is attributed to the identity :func:`resolve_git_identity`
returns for its project.  Resolution is a pair, never a mix of fields:

1. the project's override (``projects.git_identity_name`` /
   ``git_identity_email``, set in Project Settings), else
2. the installation default (``git_identity:`` in ``config.yaml``, chosen at
   ``aq install`` or with ``aq system config git-identity``), else
3. :data:`FALLBACK_IDENTITY` — the documented, deterministic identity of an
   install that has not chosen one yet.  It is never a person.

Repository and global Git configuration may *suggest* a default (the
installer reads them) but never take part in resolution: the identity is
injected through ``GIT_AUTHOR_*`` / ``GIT_COMMITTER_*``, which outrank every
``git config`` level.  Model and agent provenance stay in AQ's own records
(the task, session and profile), never in a fabricated address.

Spec: ``docs/specs/git-identity.md``.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "FALLBACK_IDENTITY",
    "LEDGER_IDENTITY",
    "LEGACY_AQ_EMAILS",
    "LEGACY_AQ_EMAIL_SUFFIXES",
    "GitIdentity",
    "GitIdentityError",
    "IdentitySource",
    "PublishPolicy",
    "ResolvedGitIdentity",
    "identity_digest",
    "installation_identity",
    "is_legacy_aq_identity",
    "legacy_pool_identity",
    "project_identity_override",
    "publish_policy",
    "resolve_git_identity",
    "same_identity",
    "validate_git_email",
    "validate_git_name",
]

IdentitySource = Literal["project", "installation", "fallback"]

#: Longest accepted name / email.  Git itself has no limit; these keep a
#: pasted paragraph out of every commit header.
MAX_NAME_LENGTH = 200
MAX_EMAIL_LENGTH = 254

#: Characters Git strips from both ends of a name or email
#: (``ident.c: crud()``), so a value carrying them at an end would never
#: compare equal to what lands in a commit.
_GIT_CRUD = set(".,:;<>\"\\'")
_EMAIL_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+$")


class GitIdentityError(ValueError):
    """A name or email that is not a safe, single-line Git identity field."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.field = field
        self.message = message


def _clean(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise GitIdentityError(field, "must be a string")
    text = value.strip()
    if not text:
        raise GitIdentityError(field, "must not be empty")
    if len(text) > limit:
        raise GitIdentityError(field, f"must be at most {limit} characters")
    for char in text:
        if char in "\r\n" or unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"}:
            raise GitIdentityError(field, "must be a single line without control characters")
    if "<" in text or ">" in text:
        raise GitIdentityError(field, "must not contain '<' or '>'")
    if text[0] in _GIT_CRUD or text[-1] in _GIT_CRUD:
        raise GitIdentityError(
            field, "must not start or end with punctuation Git strips (. , : ; \" ' \\)"
        )
    return text


def validate_git_name(value: Any) -> str:
    """The normalized name, or :class:`GitIdentityError`."""
    return _clean(value, "name", MAX_NAME_LENGTH)


def validate_git_email(value: Any) -> str:
    """The normalized email, or :class:`GitIdentityError`.

    One ``local@domain`` address with no spaces.  A dotless domain is allowed
    (``agent-queue@localhost`` is the documented fallback); deliverability is
    the operator's choice, not something AQ can verify.
    """
    text = _clean(value, "email", MAX_EMAIL_LENGTH)
    if any(char.isspace() for char in text) or not _EMAIL_RE.fullmatch(text):
        raise GitIdentityError("email", "must be one address of the form name@domain")
    return text


def identity_digest(name: str, email: str) -> str:
    """Stable fingerprint of an identity pair, as recorded on a session row.

    Compared the way :func:`same_identity` compares (email case-insensitive),
    so a digest taken from a commit header matches the configured pair.
    """
    payload = f"{name.strip()}\n{email.strip().lower()}".encode()
    return hashlib.sha256(payload).hexdigest()


def same_identity(name: str, email: str, other: GitIdentity) -> bool:
    """Whether a commit's ``name``/``email`` is *other* (email case-insensitive)."""
    return name.strip() == other.name and email.strip().lower() == other.email.lower()


@dataclass(frozen=True, slots=True)
class GitIdentity:
    """A validated name/email pair."""

    name: str
    email: str

    @classmethod
    def parse(cls, name: Any, email: Any) -> GitIdentity:
        return cls(validate_git_name(name), validate_git_email(email))

    @property
    def digest(self) -> str:
        return identity_digest(self.name, self.email)

    def formatted(self) -> str:
        return f"{self.name} <{self.email}>"

    def env(self) -> dict[str, str]:
        """``GIT_AUTHOR_*`` and ``GIT_COMMITTER_*`` for a process that commits.

        Author env only applies to new authorship: rebase, cherry-pick,
        ``commit --amend`` and ``am`` keep the original author and take this
        identity as committer only.
        """
        return {
            "GIT_AUTHOR_NAME": self.name,
            "GIT_AUTHOR_EMAIL": self.email,
            "GIT_COMMITTER_NAME": self.name,
            "GIT_COMMITTER_EMAIL": self.email,
        }

    def committer_env(self) -> dict[str, str]:
        return {"GIT_COMMITTER_NAME": self.name, "GIT_COMMITTER_EMAIL": self.email}

    def config_args(self) -> list[str]:
        """``-c user.name=… -c user.email=…`` for a command that takes no env."""
        return ["-c", f"user.name={self.name}", "-c", f"user.email={self.email}"]

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "email": self.email}


#: The identity of an install that has not chosen one.  Deterministic and
#: documented so an upgraded install keeps committing (and validating)
#: without guessing at a person; ``aq doctor`` and the settings surfaces
#: report it as unset until the operator chooses.
FALLBACK_IDENTITY = GitIdentity("Agent Queue", "agent-queue@localhost")

#: The identity of AQ's provenance *ledger* objects
#: (``src/integration/provenance.py``): marker commits on AQ's own refs whose
#: tree and parent are the source's, which never enter delivered history.
#: Each is content-addressed and must stay byte-identical across retries and
#: releases -- a published record is compared by object id -- so it never
#: follows the configurable identity, and keeps the value records were
#: always written with.
LEDGER_IDENTITY = GitIdentity("Agent Queue", "aq@localhost")

#: Identities earlier releases hard-coded.  Recognized (never written) so
#: stall detection and push reports can tell old AQ commits from upstream ones.
LEGACY_AQ_EMAILS = frozenset(
    {"aq@localhost", "agent-queue@localhost", "integration@agent-queue.local"}
)
LEGACY_AQ_EMAIL_SUFFIXES = ("@agent-queue.local",)


def legacy_pool_identity(profile_id: str) -> tuple[str, str]:
    """The per-profile ``(name, email)`` pool sessions launched before this release carried.

    Recognized at the publishing check for sessions still running from an
    earlier release; never written.
    """
    return f"aq {profile_id}", f"{profile_id}@agent-queue.local"


def is_legacy_aq_identity(email: str) -> bool:
    lowered = email.strip().lower()
    return lowered in LEGACY_AQ_EMAILS or lowered.endswith(LEGACY_AQ_EMAIL_SUFFIXES)


@dataclass(frozen=True, slots=True)
class ResolvedGitIdentity:
    """The effective identity for one project and where it came from."""

    identity: GitIdentity
    source: IdentitySource
    #: The installation default, or ``None`` while it is unset.
    installation: GitIdentity | None
    #: The project's own override, or ``None`` when it inherits.
    project_override: GitIdentity | None

    @property
    def configured(self) -> bool:
        """False only while the fallback is in use."""
        return self.source != "fallback"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.identity.name,
            "email": self.identity.email,
            "source": self.source,
            "configured": self.configured,
            "installation": self.installation.as_dict() if self.installation else None,
            "project_override": (
                self.project_override.as_dict() if self.project_override else None
            ),
            "fallback": FALLBACK_IDENTITY.as_dict(),
        }


def _section(config: Any) -> Any:
    return getattr(config, "git_identity", None) if config is not None else None


def installation_identity(config: Any) -> GitIdentity | None:
    """The confirmed installation default, or ``None`` while unset.

    An invalid stored value counts as unset here; config validation reports
    it, and resolution must never hand Git a malformed header.
    """
    section = _section(config)
    if section is None:
        return None
    if isinstance(section, Mapping):
        name, email = section.get("name"), section.get("email")
    else:
        name, email = getattr(section, "name", ""), getattr(section, "email", "")
    if not name or not email:
        return None
    try:
        return GitIdentity.parse(name, email)
    except GitIdentityError:
        return None


def project_identity_override(project: Any) -> GitIdentity | None:
    """The project's override pair, or ``None`` when it inherits."""
    if project is None:
        return None
    name = getattr(project, "git_identity_name", None)
    email = getattr(project, "git_identity_email", None)
    if not name or not email:
        return None
    try:
        return GitIdentity.parse(name, email)
    except GitIdentityError:
        return None


@dataclass(frozen=True, slots=True)
class PublishPolicy:
    """Who may have committed the new commits of one AQ publication.

    ``allowed`` holds committer digests: the identity the project resolves to
    now, plus every launch identity of a session that worked the task (an
    operator edit mid-task must not strand finished work).  ``enforce`` is
    false only when a session from an earlier release launched without an
    identity AQ can name; then mismatches are reported, not refused.  The
    publication records its findings in ``notes``.
    """

    identity: GitIdentity
    source: IdentitySource
    allowed: frozenset[str]
    enforce: bool = True
    notes: list = field(default_factory=list, compare=False)


def publish_policy(
    resolved: ResolvedGitIdentity, launches: Iterable[tuple[str | None, str, str]] = ()
) -> PublishPolicy:
    """The policy for *resolved* and the ``(digest, lifecycle, profile_id)`` launches.

    A ``legacy`` pool launch carried :func:`legacy_pool_identity`; a ``legacy``
    task launch carried whatever its daemon's Git configuration said, which
    AQ cannot name, so that task's commits are reported rather than refused.
    """
    allowed = {resolved.identity.digest}
    enforce = True
    for digest, lifecycle, profile_id in launches:
        if digest == "legacy":
            if lifecycle == "pool":
                allowed.add(identity_digest(*legacy_pool_identity(profile_id)))
            else:
                enforce = False
        elif digest:
            allowed.add(digest)
    return PublishPolicy(resolved.identity, resolved.source, frozenset(allowed), enforce)


def resolve_git_identity(config: Any, project: Any = None) -> ResolvedGitIdentity:
    """Project override > installation default > :data:`FALLBACK_IDENTITY`."""
    installation = installation_identity(config)
    override = project_identity_override(project)
    if override is not None:
        return ResolvedGitIdentity(override, "project", installation, override)
    if installation is not None:
        return ResolvedGitIdentity(installation, "installation", installation, None)
    return ResolvedGitIdentity(FALLBACK_IDENTITY, "fallback", None, None)
