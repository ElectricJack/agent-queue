"""Drift detection between shipped system profiles and their vault copies.

``vault.ensure_default_profiles()`` is deliberately write-if-absent: once
``vault/agent-types/<id>/profile.md`` exists it is never overwritten, so
operator edits survive upgrades.  The cost is that a *system* profile keeps
its original schema and semantics forever — a vault ``reviewer`` seeded
before ``read_only`` became load-bearing still says ``read_only: false``,
and ``GitOpsMixin._task_produces_no_code()``
(``src/orchestrator/git_ops.py``) then re-arms the require-a-PR gate for a
session that is told never to push.

This module is the read-only half of the answer: it compares each vault copy
of a shipped profile against the in-tree default and reports what diverged.
Nothing here writes; :func:`reseed_profile` is the explicit, opt-in write
path and it always leaves a backup behind.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any

from src.profiles.capabilities import NAMESPACES
from src.profiles.retired_defaults import is_retired

#: ``## Config`` keys whose value changes behaviour rather than presentation.
#: A divergence in any of these is what makes a stale vault profile dangerous:
#:
#: * ``read_only`` — gates the require-a-PR close check (``git_ops.py``).
#: * ``harness`` — selects which CLI actually runs the agent.
#: * ``lifecycle`` — push (``task``) vs pull (``pool``) vs ``named``.
#: * ``needs_workspace`` — whether the orchestrator acquires a worktree.
#:
#: Everything else (``description``, ``model``, ``default_class``, prompt
#: text) is presentation or tuning an operator is expected to own, and is
#: deliberately not compared.
SEMANTIC_CONFIG_FIELDS: tuple[str, ...] = (
    "read_only",
    "harness",
    "lifecycle",
    "needs_workspace",
)

#: Status values a :class:`ProfileDrift` can carry, worst last.
STATUS_OK = "ok"
STATUS_NOT_SEEDED = "not_seeded"
#: Absent from the vault *on purpose* — the operator deleted this shipped
#: default and :mod:`src.profiles.retired_defaults` holds the tombstone that
#: stops startup seeding re-creating it.  Distinguished from
#: ``not_seeded`` because the two need opposite advice.
STATUS_RETIRED = "retired"
STATUS_DRIFTED = "drifted"
STATUS_UNREADABLE = "unreadable"


def _atomic_write_bytes(
    path: str,
    data: bytes,
    *,
    fallback_mode_source: str | None = None,
) -> None:
    """Write ``data`` to ``path`` atomically via a same-directory temp file.

    A plain ``open(path, "w")`` truncates before writing, so a crash or a
    killed process mid-write can leave an empty or partial file where a vault
    profile used to be.  This writes the full content to a sibling temp file
    first and ``os.replace``s it into place — on POSIX, ``rename(2)`` onto an
    existing path is atomic, so a reader always sees either the old file or
    the fully-written new one, never a partial one.

    Preserves ``path``'s own permission bits when it already exists (an
    operator's chmod survives the write). For a brand-new file, falls back to
    the mode of ``fallback_mode_source`` if given (matching ``shutil.copy2``'s
    old behaviour of taking the source file's mode for a fresh reseed);
    otherwise the temp file's default mode is used.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)

    mode: int | None = None
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        if fallback_mode_source is not None:
            try:
                mode = stat.S_IMODE(os.stat(fallback_mode_source).st_mode)
            except OSError:
                mode = None

    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".profile-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        if mode is not None:
            os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def defaults_root() -> str:
    """Absolute path of the in-tree ``src/profiles/defaults`` directory."""
    return os.path.join(os.path.dirname(__file__), "defaults")


def shipped_profile_path(profile_id: str, root: str | None = None) -> str:
    """Path of the shipped ``profile.md`` for ``profile_id``."""
    return os.path.join(root or defaults_root(), profile_id, "profile.md")


def vault_profile_path(data_dir: str, profile_id: str) -> str:
    """Path of the vault copy of ``profile_id`` under ``data_dir``."""
    return os.path.join(data_dir, "vault", "agent-types", profile_id, "profile.md")


def system_profile_ids(root: str | None = None) -> list[str]:
    """Ids of every shipped system profile, sorted.

    A directory only counts when it actually holds a ``profile.md``, which
    mirrors what :func:`src.vault.ensure_default_profiles` seeds.
    """
    base = root or defaults_root()
    if not os.path.isdir(base):
        return []
    return sorted(
        entry
        for entry in os.listdir(base)
        if os.path.isfile(shipped_profile_path(entry, base))
    )


@dataclass
class ConfigDivergence:
    """One ``## Config`` field whose vault value differs from the shipped one.

    ``vault`` / ``shipped`` are ``None`` when the field is absent from that
    side.  None of :data:`SEMANTIC_CONFIG_FIELDS` legitimately takes a JSON
    ``null``, so ``None`` unambiguously means "not declared".
    """

    field: str
    shipped: Any
    vault: Any

    def to_dict(self) -> dict[str, Any]:
        return {"field": self.field, "shipped": self.shipped, "vault": self.vault}


@dataclass
class ProfileDrift:
    """The comparison of one system profile's vault copy against the default."""

    profile_id: str
    status: str = STATUS_OK
    #: Semantic ``## Config`` fields that differ.
    config: list[ConfigDivergence] = field(default_factory=list)
    #: Section headings the shipped default has that the vault copy lacks
    #: (lowercased).  A rename shows up here plus in :attr:`extra_sections`.
    missing_sections: list[str] = field(default_factory=list)
    #: Section headings only the vault copy has.  Reported for context —
    #: an operator adding a section is legitimate and is not drift on its own.
    extra_sections: list[str] = field(default_factory=list)
    #: Per capability namespace (``harness_tools``, ``aq_commands``,
    #: ``plugin_tools``), the grant names the shipped default has that the
    #: vault copy lacks.  Extra vault grants are never reported here — an
    #: operator adding a grant is legitimate.  Empty when either side has no
    #: ``## Capabilities`` block (nothing to diff against; see
    #: :attr:`missing_sections` instead).
    missing_grants: dict[str, list[str]] = field(default_factory=dict)
    #: Parse errors from either file; a non-empty list means ``unreadable``.
    errors: list[str] = field(default_factory=list)

    @property
    def is_drifted(self) -> bool:
        return self.status in (STATUS_DRIFTED, STATUS_UNREADABLE)

    def summary(self) -> str:
        """One human line describing this profile's drift."""
        if self.status == STATUS_OK:
            return f"{self.profile_id}: matches shipped default"
        if self.status == STATUS_NOT_SEEDED:
            return f"{self.profile_id}: no vault copy (seeded on next daemon start)"
        if self.status == STATUS_RETIRED:
            return (
                f"{self.profile_id}: retired by the operator, not re-seeded "
                f"(`aq agent profile-reseed {self.profile_id}` restores it)"
            )
        if self.status == STATUS_UNREADABLE:
            return f"{self.profile_id}: {'; '.join(self.errors)}"
        parts = [
            f"{d.field}={d.vault!r} (shipped {d.shipped!r})" for d in self.config
        ]
        if self.missing_sections:
            renamed = ", ".join(sorted(self.missing_sections))
            parts.append(f"missing section(s): {renamed}")
        for ns in NAMESPACES:
            names = self.missing_grants.get(ns)
            if names:
                parts.append(f"missing {len(names)} {ns} grant(s): {', '.join(names)}")
        return f"{self.profile_id}: " + ", ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "status": self.status,
            "config": [d.to_dict() for d in self.config],
            "missing_sections": list(self.missing_sections),
            "extra_sections": list(self.extra_sections),
            "missing_grants": {ns: list(names) for ns, names in self.missing_grants.items()},
            "errors": list(self.errors),
            "summary": self.summary(),
        }


def _parse(path: str) -> tuple[dict, set[str], list[str], dict[str, list[str]] | None]:
    """Parse ``path`` into (config, section-name set, errors, capabilities)."""
    from src.profiles.parser import parse_profile

    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        return {}, set(), [f"cannot read {path}: {exc}"], None
    parsed = parse_profile(text)
    return (
        dict(parsed.config or {}),
        set(parsed.sections),
        list(parsed.errors),
        parsed.capabilities,
    )


def _missing_grants(
    shipped_capabilities: dict[str, list[str]] | None,
    vault_capabilities: dict[str, list[str]] | None,
) -> dict[str, list[str]]:
    """Per-namespace grant names ``shipped`` has that ``vault`` lacks.

    ``None`` on either side means there is no ``## Capabilities`` block to
    diff against (a legacy vault copy still on ``## Tools``, or — in
    principle — a malformed shipped default), and yields an empty result;
    that case is reported as ``missing_sections`` instead.
    """
    if shipped_capabilities is None or vault_capabilities is None:
        return {}
    missing: dict[str, list[str]] = {}
    for ns in NAMESPACES:
        shipped_names = shipped_capabilities.get(ns, [])
        vault_names = set(vault_capabilities.get(ns, []))
        names = [name for name in shipped_names if name not in vault_names]
        if names:
            missing[ns] = names
    return missing


def diff_profile(
    profile_id: str,
    data_dir: str,
    root: str | None = None,
) -> ProfileDrift:
    """Compare one system profile's vault copy against the shipped default."""
    drift = ProfileDrift(profile_id=profile_id)

    shipped_path = shipped_profile_path(profile_id, root)
    if not os.path.isfile(shipped_path):
        drift.status = STATUS_UNREADABLE
        drift.errors.append(f"no shipped default at {shipped_path}")
        return drift

    vault_path = vault_profile_path(data_dir, profile_id)
    if not os.path.isfile(vault_path):
        drift.status = (
            STATUS_RETIRED if is_retired(data_dir, profile_id) else STATUS_NOT_SEEDED
        )
        return drift

    shipped_config, shipped_sections, shipped_errors, shipped_capabilities = _parse(
        shipped_path
    )
    vault_config, vault_sections, vault_errors, vault_capabilities = _parse(vault_path)

    # A shipped default that does not parse is a packaging bug, not operator
    # drift, but the operator still needs to see it.
    drift.errors = [f"shipped: {e}" for e in shipped_errors]
    drift.errors += [f"vault: {e}" for e in vault_errors]
    if drift.errors:
        drift.status = STATUS_UNREADABLE
        return drift

    for name in SEMANTIC_CONFIG_FIELDS:
        shipped_value = shipped_config.get(name)
        vault_value = vault_config.get(name)
        if shipped_value != vault_value:
            drift.config.append(ConfigDivergence(name, shipped_value, vault_value))

    drift.missing_sections = sorted(shipped_sections - vault_sections)
    drift.extra_sections = sorted(vault_sections - shipped_sections)
    drift.missing_grants = _missing_grants(shipped_capabilities, vault_capabilities)

    if drift.config or drift.missing_sections or drift.missing_grants:
        drift.status = STATUS_DRIFTED
    return drift


def scan_profile_drift(
    data_dir: str,
    root: str | None = None,
) -> list[ProfileDrift]:
    """Compare every shipped system profile against its vault copy."""
    return [diff_profile(pid, data_dir, root) for pid in system_profile_ids(root)]


def reseed_profile(
    data_dir: str,
    profile_id: str,
    root: str | None = None,
    backup: bool = True,
) -> dict[str, Any]:
    """Overwrite one vault profile with the shipped default.

    The explicit counterpart to :func:`src.vault.ensure_default_profiles`'s
    write-if-absent rule: startup never clobbers an operator's file, but an
    operator who has read the drift report can ask for the shipped version
    back one profile at a time.  The previous file is copied to
    ``profile.md.bak-<epoch>`` first unless ``backup=False``.

    Returns a dict with ``profile_id``, ``path``, ``backup_path`` (``None``
    when nothing was there to back up) and ``created`` (True when no vault
    copy existed).
    """
    shipped_path = shipped_profile_path(profile_id, root)
    if not os.path.isfile(shipped_path):
        raise FileNotFoundError(f"'{profile_id}' is not a shipped system profile")

    dst = vault_profile_path(data_dir, profile_id)
    existed = os.path.isfile(dst)
    backup_path: str | None = None
    if existed and backup:
        backup_path = f"{dst}.bak-{int(time.time())}"
        shutil.copy2(dst, backup_path)

    with open(shipped_path, "rb") as handle:
        shipped_bytes = handle.read()
    _atomic_write_bytes(dst, shipped_bytes, fallback_mode_source=shipped_path)
    return {
        "profile_id": profile_id,
        "path": dst,
        "backup_path": backup_path,
        "created": not existed,
    }


# --- additive grant repair ---------------------------------------------------
#
# ``reseed_profile`` overwrites the whole file, which is exactly right for a
# profile predating ``## Capabilities`` and exactly wrong for an operator who
# only wants the grants a shipped release added — a full reseed silently
# discards edits like ``"harness": "codex"``.  ``merge_profile_grants``
# operates on the vault file's ``## Capabilities`` fenced JSON block
# textually, so every other byte of the file (``## Config`` including
# ``harness``, other sections, existing/extra grants, their order) survives
# untouched.  The only formatting choice it makes: an array that is still
# written compact (all on one line, e.g. a freshly-seeded ``[]``) is
# rewritten one-per-line once something is appended to it, matching the
# style the shipped defaults use; an array already one-per-line keeps that
# style and only gains lines at the end.

_CAPABILITIES_HEADING_RE = re.compile(r"^## Capabilities[ \t]*\r?\n", re.MULTILINE | re.IGNORECASE)
_NEXT_HEADING_RE = re.compile(r"^## ", re.MULTILINE)
_JSON_FENCE_RE = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)
_ARRAY_ITEM_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')


def _detect_newline(text: str) -> str:
    """The file's own line-ending convention: ``"\\r\\n"`` or ``"\\n"``.

    Read with ``open(..., newline="")`` so no universal-newline translation
    has already collapsed this — otherwise every ``\\r`` is silently gone by
    the time this function sees the text, and there is nothing left to
    detect.  Looks at the first line break found; profile files are not
    expected to mix conventions within one file.
    """
    idx = text.find("\n")
    if idx > 0 and text[idx - 1] == "\r":
        return "\r\n"
    return "\n"


def _capabilities_json_span(text: str) -> tuple[int, int]:
    """Offsets of the raw JSON inside the vault file's ``## Capabilities`` fence."""
    heading = _CAPABILITIES_HEADING_RE.search(text)
    if heading is None:
        raise ValueError("could not locate a '## Capabilities' heading")
    body_start = heading.end()
    next_heading = _NEXT_HEADING_RE.search(text, body_start)
    body_end = next_heading.start() if next_heading else len(text)
    fence = _JSON_FENCE_RE.search(text, body_start, body_end)
    if fence is None:
        raise ValueError("'## Capabilities' has no fenced ```json block")
    return fence.start(1), fence.end(1)


def _key_line_indent(block_text: str, key_start: int) -> str:
    """Leading whitespace of the line ``key_start`` sits on, or ``""``.

    Empty when the key shares its line with other content (a fully compact
    block such as ``{"harness_tools": [], ...}`` all on one line) — there is
    no indentation to imitate, so callers fall back to a plain two spaces.
    """
    line_start = block_text.rfind("\n", 0, key_start) + 1
    candidate = block_text[line_start:key_start]
    return candidate if candidate.strip() == "" else ""


def _rewrite_capabilities_array(
    block_text: str,
    ns: str,
    new_names: list[str],
    newline: str = "\n",
) -> str:
    """Append ``new_names`` to the ``ns`` array inside a Capabilities JSON block.

    Only this one array is touched; everything else in ``block_text`` —
    including the rest of this array's existing entries, their order and
    any other namespace — is preserved verbatim.

    ``newline`` is the file's own line-ending convention (``"\\n"`` or
    ``"\\r\\n"``, from :func:`_detect_newline`). Every line this function
    manufactures — the fixed-up last existing line, and every appended one —
    uses it, so a CRLF vault file comes back fully CRLF rather than a mix of
    the original convention and this function's own LF.
    """
    pattern = re.compile(r'("' + re.escape(ns) + r'"\s*:\s*)\[(.*?)\]', re.DOTALL)
    match = pattern.search(block_text)
    if match is None:
        raise ValueError(f"could not locate '{ns}' inside the ## Capabilities JSON block")

    prefix, body = match.group(1), match.group(2)
    key_indent = _key_line_indent(block_text, match.start())

    if newline in body:
        # Already one-per-line — append after the existing entries, matching
        # their indentation and the shipped-file convention that only the
        # last entry has no trailing comma. Splitting on the file's own
        # ``newline`` (rather than a bare "\n") means a CRLF body yields
        # clean lines with no embedded "\r" to trip up on later.
        lines = body.split(newline)
        if lines and lines[-1].strip() == "":
            closing_indent = lines[-1]
            content_lines = lines[:-1]
        else:
            closing_indent = ""
            content_lines = lines

        item_indent = key_indent + "  "
        for line in content_lines:
            if line.strip():
                item_indent = line[: len(line) - len(line.lstrip())]
                break

        last = content_lines[-1] if content_lines else ""
        if last.strip() and not last.rstrip().endswith(","):
            content_lines[-1] = last.rstrip() + ","

        new_item_lines = [f'{item_indent}"{name}",' for name in new_names[:-1]]
        new_item_lines.append(f'{item_indent}"{new_names[-1]}"')

        merged_before = newline.join(content_lines)
        appended = newline.join(new_item_lines)
        new_array = "[" + merged_before + newline + appended + newline + closing_indent + "]"
    else:
        # Compact single-line array (including "[]") — rewritten one-per-line
        # so the appended names are readable; existing entries and their
        # order are preserved verbatim, just reformatted.
        existing = _ARRAY_ITEM_RE.findall(body)
        all_names = [*existing, *new_names]
        item_indent = key_indent + "  "
        item_lines = [f'{item_indent}"{name}",' for name in all_names[:-1]]
        item_lines.append(f'{item_indent}"{all_names[-1]}"')
        new_array = "[" + newline + newline.join(item_lines) + newline + key_indent + "]"

    return block_text[: match.start()] + prefix + new_array + block_text[match.end() :]


def _add_capability_grants(
    text: str,
    ns: str,
    names: list[str],
    newline: str = "\n",
) -> str:
    start, end = _capabilities_json_span(text)
    new_block = _rewrite_capabilities_array(text[start:end], ns, names, newline)
    return text[:start] + new_block + text[end:]


def merge_profile_grants(
    data_dir: str,
    profile_id: str,
    *,
    root: str | None = None,
) -> dict[str, Any]:
    """Additively repair missing ``## Capabilities`` grants in a vault profile.

    Unlike :func:`reseed_profile`, this never replaces the file: it appends
    only the grant names the shipped default has that the vault copy lacks
    to each namespace's list inside the vault's own ``## Capabilities`` JSON
    block.  Everything else — ``## Config`` (including operator edits like
    ``"harness": "codex"``), every other section, existing and
    operator-added grants and their order — is preserved.

    A ``.bak-<epoch>`` copy is written first, and the merged text is
    validated with :func:`src.profiles.parser.parse_profile` *before* the
    file is replaced; if it does not parse cleanly, nothing is written and
    no backup is left behind.

    Returns ``{"profile_id", "added": {namespace: [names]}, "backup_path",
    "changed"}``.  When nothing is missing this is a no-op: ``added`` is
    empty, ``changed`` is False, ``backup_path`` is None and nothing is
    written.

    Raises
    ------
    FileNotFoundError
        ``profile_id`` is not a shipped system profile, or has no vault copy
        to merge into.
    ValueError
        The vault copy has no ``## Capabilities`` section (it needs a full
        reseed or a hand edit instead); has one but it fails to parse (the
        parser's own errors are included); or the merged text failed to
        parse.
    """
    from src.profiles.parser import parse_profile

    shipped_path = shipped_profile_path(profile_id, root)
    if not os.path.isfile(shipped_path):
        raise FileNotFoundError(f"'{profile_id}' is not a shipped system profile")

    vault_path = vault_profile_path(data_dir, profile_id)
    if not os.path.isfile(vault_path):
        raise FileNotFoundError(f"no vault copy of '{profile_id}' at {vault_path}")

    # ``newline=""`` disables universal-newline translation: a CRLF file's
    # "\r" bytes stay in the string on read (instead of being silently
    # dropped) and are written back untranslated, so untouched lines survive
    # byte-for-byte and _detect_newline() has something real to look at.
    with open(shipped_path, encoding="utf-8", newline="") as handle:
        shipped_text = handle.read()
    with open(vault_path, encoding="utf-8", newline="") as handle:
        vault_text = handle.read()

    shipped_parsed = parse_profile(shipped_text)
    vault_parsed = parse_profile(vault_text)

    if vault_parsed.capabilities is None:
        if "capabilities" in vault_parsed.sections:
            # The heading and fence are there, but the JSON inside it either
            # didn't parse or failed capability validation (e.g. a namespace
            # that is `null` instead of a list) — that is a different, more
            # specific problem than "no section", and the operator needs the
            # parser's own diagnosis to fix it, not a pointer to reseed.
            cap_errors = [e for e in vault_parsed.errors if "capabilities" in e.lower()]
            detail = "; ".join(cap_errors) if cap_errors else "; ".join(vault_parsed.errors)
            raise ValueError(
                f"'{profile_id}' vault copy's '## Capabilities' section does not "
                f"parse: {detail}"
            )
        raise ValueError(
            f"'{profile_id}' vault copy has no '## Capabilities' section to merge "
            "grants into — use a full reseed (`aq agent profile-reseed "
            f"--profile-id {profile_id}`) or a hand edit"
        )

    added = _missing_grants(shipped_parsed.capabilities, vault_parsed.capabilities)
    if not added:
        return {"profile_id": profile_id, "added": {}, "backup_path": None, "changed": False}

    newline = _detect_newline(vault_text)
    merged_text = vault_text
    for ns, names in added.items():
        merged_text = _add_capability_grants(merged_text, ns, names, newline)

    revalidated = parse_profile(merged_text)
    if revalidated.errors:
        raise ValueError(
            f"merged '## Capabilities' for '{profile_id}' would not parse cleanly: "
            f"{'; '.join(revalidated.errors)}"
        )

    backup_path = f"{vault_path}.bak-{int(time.time())}"
    shutil.copy2(vault_path, backup_path)
    _atomic_write_bytes(vault_path, merged_text.encode("utf-8"))

    return {
        "profile_id": profile_id,
        "added": added,
        "backup_path": backup_path,
        "changed": True,
    }
