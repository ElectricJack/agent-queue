"""The exact source identity every integration merge records in its message.

One ``AQ-Source: <task-id>@<full-source-sha>`` trailer is written per applied
member, and only once that member's complete source has been applied: candidate
construction, child-to-parent promotion, the ``merge_members`` primitive and the
development publisher all stamp it through :func:`with_source_trailers`, which
never rewrites or duplicates a trailer and never removes a trailer whose reader
has not retired.

The trailer names a whole generation, not one commit of it, so matching a single
commit of a multi-commit task proves nothing. A reader accepts the identity only
from a commit **reachable from the fetched target**, spelled exactly: a
substring, an abbreviated SHA, or the previous generation's SHA of the same task
is a different identity, so an old trailer never satisfies a reopened task's new
completion.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from src.git.manager import GitError, is_valid_git_oid

#: Trailer key naming the applied source. Case-insensitively matched on read,
#: written in this exact case so a written message is itself readable.
SOURCE_TRAILER = "AQ-Source"

#: One record per commit, as ``candidates._authors`` already does.
_RECORD = "\x1e"
_FIELD = "\x00"

_TRAILER_RE = re.compile(
    rf"^{re.escape(SOURCE_TRAILER)}:[ \t]+(\S+)@([0-9a-f]+)$", re.IGNORECASE | re.MULTILINE
)
#: Git's own trailer token shape: ``Token: value``. A last line of this shape
#: belongs to the message's trailer paragraph, so an added trailer joins it.
_TRAILER_LINE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*: .+$")


@dataclass(frozen=True, order=True)
class SourceIdentity:
    """One task's exact applied source: the pair a reader matches in full."""

    task_id: str
    source_oid: str

    def __post_init__(self):
        if not isinstance(self.task_id, str) or not self.task_id.strip() or (
            len(self.task_id) > 500 or any(ord(c) < 32 for c in self.task_id)
        ):
            raise ValueError("source trailer requires exact task identity")
        if not is_valid_git_oid(self.source_oid):
            raise ValueError("source trailer requires a full lowercase Git OID")

    @property
    def trailer(self) -> str:
        return f"{SOURCE_TRAILER}: {self.task_id}@{self.source_oid}"


def source_identity(task_id: str, source_oid: str) -> SourceIdentity:
    """The identity a merge of *source_oid* for *task_id* must record."""
    return SourceIdentity(task_id, source_oid)


def parse_source_trailers(message: str) -> frozenset[SourceIdentity]:
    """Every exact identity in *message*; a malformed line is not an identity.

    A trailer that carries anything besides one task id and one full OID is
    skipped rather than repaired: a substring or abbreviated SHA is a different
    generation, and guessing would deliver the wrong work.
    """
    found: set[SourceIdentity] = set()
    for task_id, source_oid in _TRAILER_RE.findall(message or ""):
        try:
            found.add(SourceIdentity(task_id, source_oid))
        except ValueError:
            continue
    return frozenset(found)


def with_source_trailers(message: str, identities: Iterable[SourceIdentity]) -> str:
    """*message* plus one trailer per identity, in order, exactly once.

    The existing message is otherwise untouched: its own trailers stay until
    their readers retire, and an identity already present is not written twice.
    An identity joins the message's trailer paragraph when it already ends in
    one (so ``Co-authored-by`` attribution survives) and starts its own
    paragraph after prose.
    """
    ordered = sorted(set(identities))
    present = parse_source_trailers(message)
    missing = [identity for identity in ordered if identity not in present]
    if not missing:
        return message
    body = message.rstrip()
    last = body.splitlines()[-1:] or [""]
    gap = "\n" if _TRAILER_LINE_RE.match(last[0]) else "\n\n"
    return f"{body}{gap}" + "".join(f"{identity.trailer}\n" for identity in missing).rstrip("\n")


async def reachable_source_identities(
    git, store, target_oid: str, *, identities: Iterable[SourceIdentity] | None = None
) -> frozenset[SourceIdentity]:
    """Every exact source identity on a commit reachable from *target_oid*.

    Only reachability counts: a trailer on the source branch, a fetched but
    unreachable candidate, or a commit that merely mentions the identity is not
    delivery. A failed observation raises rather than reporting nothing.

    Narrowing by *identities* asks Git for the matching commits first; the
    returned messages are still parsed exactly, so a message that merely
    mentions the identity does not answer for it.
    """
    if not is_valid_git_oid(target_oid):
        raise ValueError("source trailer reachability requires a full lowercase Git OID")
    args = ["--no-replace-objects", "log", "--format=%B%x00%x1e"]
    for identity in identities or ():
        args += ["--fixed-strings", "--grep", identity.trailer]
    result = await git.arun_git_result([*args, target_oid], cwd=str(store))
    if result.returncode:
        raise GitError(result.stderr or result.stdout or "source trailer read failed")
    found: set[SourceIdentity] = set()
    for record in result.stdout.split(_RECORD):
        found |= parse_source_trailers(record.replace(_FIELD, ""))
    return frozenset(found)


async def delivered_by_source_trailer(git, store, task_id: str, source_oid: str, target_oid: str):
    """Whether *target_oid* carries *task_id*'s exact *source_oid* trailer.

    The whole identity must match: another task, a different generation's SHA,
    an abbreviated SHA or a substring is not this source.
    """
    identity = SourceIdentity(task_id, source_oid)
    return identity in await reachable_source_identities(
        git, store, target_oid, identities=[identity]
    )
