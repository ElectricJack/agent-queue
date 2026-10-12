"""Immutable promotion notes inputs from full Git history, assembled by the daemon."""

from __future__ import annotations

import hashlib
import json
import re

import yaml
from sqlalchemy import select

from src.database.tables import (
    archived_tasks,
    events,
    task_branch_origins,
    task_completion_records,
    task_labels,
    task_results,
    tasks,
)

PREPARE_CONTEXT = "promotion_prepare"
MAX_NOTES_COMMITS = 2000
MAX_PR_BODY_BYTES = 65_536
NOTES_TRUNCATION_MARKER = "\n\n[Release notes truncated to fit the promotion PR body limit.]\n"
_SOURCE = re.compile(r"^AQ-Source: ([^\s@]+)@([0-9a-f]{40})$", re.MULTILINE)
_REVERT = re.compile(r"This reverts commit ([0-9a-f]{40})\.")
_SEMVER = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"


class NotesRefusal(ValueError):
    def __init__(self, outcome, message):
        super().__init__(message)
        self.outcome = outcome


def semver(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or re.fullmatch(_SEMVER, value) is None:
        raise NotesRefusal("version_mismatch", "Version must be a MAJOR.MINOR.PATCH release.")
    return tuple(int(part) for part in value.split("."))


async def source_version(ops, repo, versioning, sha):
    selector = versioning.get("source")
    if selector == "pyproject":
        import tomllib

        value = tomllib.loads(await ops.run(repo, "show", sha + ":pyproject.toml")) \
            .get("project", {}).get("version")
    elif selector == "package.json":
        value = json.loads(await ops.run(repo, "show", sha + ":package.json")).get("version")
    elif selector and selector.startswith("file:"):
        path, pattern = selector[5:].split(":", 1)
        match = re.search(pattern, await ops.run(repo, "show", sha + ":" + path))
        value = match.group(1) if match and match.groups() else None
    else:
        value = None
    if not isinstance(value, str) or not value:
        raise NotesRefusal("version_mismatch", "Version source did not contain a version.")
    if versioning["kind"] == "semver_tag":
        semver(value)
    return value


async def previous_tag(ops, repo, step):
    """Use the remote inventory, never stale local tags, for this step's boundary."""
    if not step_has_version(step):
        return None
    pattern = re.escape(step["versioning"]["tag_format"])
    for field, capture in {
        "version": "(?P<version>" + (_SEMVER if step["versioning"]["kind"] == "semver_tag"
                                      else "[^/]+") + ")", "step": re.escape(step["id"]),
        "sha12": "[0-9a-f]{12}", "utc_date": r"\d{4}-\d{2}-\d{2}",
    }.items():
        pattern = pattern.replace(re.escape("{" + field + "}"), capture)
    inventory = await ops.git.als_remote_tag_refs(
        str(repo.store), repository_url=f"https://github.com/{repo.binding.full_name}.git",
    )
    entries = []
    for ref, oid in inventory.items():
        if ref.endswith("^{}"):
            continue
        match = re.fullmatch(pattern, ref.removeprefix("refs/tags/"))
        if not match:
            continue
        version = match.groupdict().get("version")
        if step["versioning"]["kind"] == "semver_tag":
            order = semver(version)
        else:
            # Custom versions have no universal ordering; order their immutable commits.
            order = None
        sha = inventory.get(ref + "^{}", oid)
        await ensure_commit(ops, repo, sha)
        if order is None:
            order = (int(await ops.run(repo, "show", "-s", "--format=%ct", sha)),)
        entries.append((order, ref, {"tag": ref[10:], "version": version,
                                     "sha": sha, "tag_oid": oid}))
    return max(entries, key=lambda item: item[:2])[2] if entries else None


async def ensure_commit(ops, repo, sha):
    present = await ops.git.arun_git_result(
        ["--no-replace-objects", "cat-file", "-e", sha + "^{commit}"], cwd=str(repo.store),
    )
    if present.returncode:
        await ops.git.afetch_repository_oid(
            str(repo.store), repository=repo.binding, oid=sha,
            destination_ref="refs/aq/promotion-notes/" + sha,
        )
    await ops.exact(repo, sha)


def notes_metadata(body, kind, version=None):
    """Read authored source identities in either supported committed format."""
    if kind == "file_template":
        match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", body, re.DOTALL)
        try:
            value = yaml.safe_load(match.group(1)) if match else {}
        except yaml.YAMLError as exc:
            raise NotesRefusal("notes_stale", "Notes frontmatter is malformed.") from exc
        return value if isinstance(value, dict) else {}
    sections = re.split(r"(?m)^## \[([^\]\n]+)\][^\n]*\n", body)
    values = []
    for i in range(1, len(sections), 2):
        if version is not None and sections[i] != version:
            continue
        match = re.search(r"<!-- aq-promotion: (.*?) -->", sections[i + 1])
        if match:
            try:
                value = json.loads(match.group(1))
            except ValueError as exc:
                raise NotesRefusal("notes_stale", "Notes digest comment is malformed.") from exc
            if not isinstance(value, dict) or not isinstance(value.get("sources", []), list):
                raise NotesRefusal("notes_stale", "Notes source metadata is malformed.")
            values.append(value)
    if version is not None:
        return values[0] if len(values) == 1 else {}
    return {"sources": [source for value in values for source in value.get("sources", [])]}


async def _prior_sources(ops, repo, step, previous):
    if not previous or step["notes"]["kind"] == "none":
        return set()
    kind, path = step["notes"]["kind"], step["notes"]["path"]
    paths = (await ops.run(repo, "ls-tree", "-r", "--name-only", previous["sha"])).splitlines()
    pattern = re.escape(path).replace(re.escape("{version}"), r"[^/]+")
    sources = set()
    for candidate in paths:
        if re.fullmatch(pattern, candidate):
            body = await ops.run(repo, "show", previous["sha"] + ":" + candidate)
            for source in notes_metadata(body, kind).get("sources", []):
                identity = source.get("task") if isinstance(source, dict) else str(source)
                if isinstance(identity, str):
                    sources.add(identity.split("@", 1)[0])
    return sources


async def _source_labels(conn, row):
    if "archived_at" not in row:
        return list((await conn.execute(select(task_labels.c.label).where(
            task_labels.c.task_id == row["id"],
        ).order_by(task_labels.c.label))).scalars())
    # Archival removes task_labels, but retains the command layer's label
    # audit. Replay only that task's events up to this archive snapshot.
    labels = set()
    history = await conn.execute(select(events.c.event_type, events.c.payload).where(
        events.c.task_id == row["id"], events.c.project_id == row["project_id"],
        events.c.event_type.in_(("label.added", "label.removed")),
        events.c.timestamp <= row["archived_at"],
    ).order_by(events.c.timestamp, events.c.id))
    for kind, label in history:
        if label:
            if kind == "label.added":
                labels.add(label)
            else:
                labels.discard(label)
    return sorted(labels)


async def assemble_notes_input(conn, ops, repo, *, project_id, repository_id, step, head,
                               previous=None, target_tip=None):
    """Walk all parents, preserving a pinned range and first topological task identity."""
    await ensure_commit(ops, repo, head)
    versioned = step_has_version(step)
    previous = previous if versioned else None
    bound = previous["sha"] if previous else (
        step["notes"].get("bootstrap_sha") if versioned else None
    )
    bound = bound or target_tip or await ops.remote(repo, "refs/heads/" + step["target"])
    if not bound:
        raise NotesRefusal("notes_range_invalid", "Notes target branch is unavailable.")
    await ensure_commit(ops, repo, bound)
    if not await ops.is_ancestor(repo, bound, head):
        raise NotesRefusal("notes_range_invalid", "Notes boundary is outside source history.")
    range_args = [head, "^" + bound]
    commits = (await ops.run(repo, "rev-list", "--topo-order",
                            f"--max-count={MAX_NOTES_COMMITS + 1}", *range_args)).splitlines()
    if len(commits) > MAX_NOTES_COMMITS:
        raise NotesRefusal("notes_range_too_large",
                           f"Notes range exceeds {MAX_NOTES_COMMITS} commits.")
    # Preserve every parent within the bounded range, including epic child merges.
    raw = await ops.run(repo, "log", "--topo-order", f"--max-count={MAX_NOTES_COMMITS}",
                        "--format=%H%x00%P%x00%B%x00", *range_args)
    fields = raw.split("\0")
    messages = {fields[i].strip(): fields[i + 2]
                for i in range(0, len(fields) - 2, 3)}
    parents = {fields[i].strip(): fields[i + 1].split()
               for i in range(0, len(fields) - 2, 3)}
    excluded = await _prior_sources(ops, repo, step, previous)
    identities = {}
    reverts = {}
    for index, sha in enumerate(commits):
        for task, source in _SOURCE.findall(messages[sha]):
            identities.setdefault(task, (source, index))
        for source in _REVERT.findall(messages[sha]):
            reverts.setdefault(source, index)
    sources, covered = [], set()
    for task, (sha, index) in identities.items():
        # Do not reintroduce worker commits as bare subjects. This ancestry
        # comes from the already-read range, including sources shipped before
        # this range whose later merge is being excluded by notes metadata.
        pending = [sha]
        while pending:
            ancestor = pending.pop()
            if ancestor not in covered and ancestor in parents:
                covered.add(ancestor)
                pending.extend(parents[ancestor])
        if task in excluded:
            continue
        row = None
        for table in (tasks, archived_tasks):
            row = (await conn.execute(select(table).where(
                table.c.id == task, table.c.project_id == project_id, table.c.repo_id == repository_id,
            ))).mappings().first()
            if row:
                break
        if row is None:
            raise NotesRefusal("notes_source_missing", f"Notes source task {task} is unavailable.")
        if (row["task_type"] in {"promotion", "backmerge"}
                or row["created_by_kind"] == PREPARE_CONTEXT):
            continue
        await ensure_commit(ops, repo, sha)
        summary = await conn.scalar(select(task_completion_records.c.summary).where(
            task_completion_records.c.task_id == task,
        ).order_by(task_completion_records.c.completed_at.desc(), task_completion_records.c.id).limit(1))
        if not summary:
            summary = await conn.scalar(select(task_results.c.summary).where(
                task_results.c.task_id == task,
            ).order_by(task_results.c.created_at.desc(), task_results.c.id).limit(1))
        labels = await _source_labels(conn, row)
        base = await conn.scalar(select(task_branch_origins.c.base_sha).where(
            task_branch_origins.c.task_id == task,
            task_branch_origins.c.repository_id == repository_id,
        ).order_by(task_branch_origins.c.creation_generation.desc()).limit(1))
        if base:
            await ensure_commit(ops, repo, base)
            delta = await ops.run(repo, "diff", "--name-only", base, sha, "--", "migrations/versions/")
        else:
            delta = await ops.run(repo, "diff-tree", "--root", "--no-commit-id", "-r",
                                  "--name-only", sha, "--", "migrations/versions/")
        sources.append({"task": task, "sha": sha, "type": row["task_type"],
                        "title": row["title"], "summary": summary or None, "labels": list(labels),
                        "migrations": delta.splitlines(),
                        "reverted": sha in reverts and reverts[sha] < index})
    migrations = await ops.run(repo, "diff", "--name-only", bound, head,
                               "--", "migrations/versions/")
    bare = [{"sha": sha, "subject": messages[sha].splitlines()[0] if messages[sha] else "(no subject)"}
            for sha in commits if (sha not in covered or len(parents[sha]) > 1)
            and not _SOURCE.search(messages[sha])]
    digest = hashlib.sha256("\n".join(sorted(
        f"{source['task']}@{source['sha']}" for source in sources
    )).encode()).hexdigest()
    return {"step": step["id"], "range": {"base": bound, "head": head,
            "previous_tag": previous["tag"] if previous else None},
            "sources": sources, "bare_commits": bare,
            "migration_files": migrations.splitlines(), "source_digest": digest}


def render_release_notes(notes_input):
    sections = {name: [] for name in ("Added", "Changed", "Fixed", "Upgrade", "Breaking")}
    for source in notes_input["sources"]:
        text = source["summary"] or source["title"] + " [no summary]"
        text = " ".join(text.split()) + (" (reverted)" if source["reverted"] else "")
        text += f" (`{source['task']}`)"
        name = {"feature": "Added", "bugfix": "Fixed", "fix": "Fixed"}.get(source["type"], "Changed")
        sections[name].append(text)
        if source["migrations"]:
            sections["Upgrade"].append(text)
        if "breaking" in source["labels"]:
            sections["Breaking"].append(text)
    sections["Changed"].extend(commit["subject"] for commit in notes_input["bare_commits"])
    return "\n\n".join(f"### {name}\n\n" + "\n".join(f"- {text}" for text in entries)
                        for name, entries in sections.items()) + "\n"


def render_operator_notes(notes_input):
    bound = notes_input["range"]["base"] or "root"
    head = notes_input["range"]["head"]
    migrations = notes_input["migration_files"]
    return (f"Range: `{bound}..{head}`\n\n"
            f"Source digest: `{notes_input['source_digest']}`\n\n"
            "Migrations:\n" + ("\n".join(f"- `{path}`" for path in migrations)
                               if migrations else "- None") + "\n")


def draft_notes(notes_input, *, kind, version):
    metadata = {"version": version, "sources": [f"{s['task']}@{s['sha']}"
                for s in notes_input["sources"]], "source_digest": notes_input["source_digest"]}
    content = render_release_notes(notes_input) + "\n### Operator notes\n\n" \
        + render_operator_notes(notes_input)
    if kind == "file_template":
        return "---\n" + yaml.safe_dump(metadata, sort_keys=False) + "---\n\n" + content
    if kind == "changelog_heading":
        return f"## [{version}]\n<!-- aq-promotion: {json.dumps(metadata, sort_keys=True)} -->\n\n" + content
    raise ValueError("Only configured notes formats can be drafted")


def step_pr_body(step, source, request_id, notes_input, authored_notes=None, *, origin_ref=None):
    prefix = f"Promote `{origin_ref or step['source']}` to `{step['target']}` at `{source}`.\n\n"
    suffix = f"\nAQ-Promotion-Request: {request_id}\n"
    if notes_input is None:
        body = prefix + suffix
        if len(body.encode()) > MAX_PR_BODY_BYTES:
            raise NotesRefusal("promotion_body_too_large", "Required promotion body exceeds the limit.")
        return body
    release = authored_notes if authored_notes is not None else render_release_notes(notes_input)
    prefix += "## Release notes\n\n"
    suffix = "\n\n## Operator notes\n\n" + render_operator_notes(notes_input) + suffix
    available = MAX_PR_BODY_BYTES - len((prefix + suffix).encode())
    encoded = release.encode()
    if len(encoded) > available:
        available -= len(NOTES_TRUNCATION_MARKER.encode())
        if available < 0:
            raise NotesRefusal("promotion_body_too_large", "Required promotion body exceeds the limit.")
        release = encoded[:available].decode("utf-8", errors="ignore") + NOTES_TRUNCATION_MARKER
    return prefix + release + suffix


def step_has_version(step):
    versioning = step["versioning"]
    return versioning["kind"] == "semver_tag" or (
        versioning["kind"] == "custom" and "{version}" in versioning["tag_format"]
    )


def authored_notes_section(body, kind, version):
    """Keep human edits while limiting a changelog PR to its requested release."""
    if kind == "file_template":
        return body
    match = re.search(r"(?m)^## \[" + re.escape(version) + r"\][^\n]*\n", body)
    if match is None:
        raise NotesRefusal("notes_stale", "Pinned changelog has no requested version section.")
    rest = body[match.start():]
    following = re.search(r"(?m)^## \[", rest[match.end() - match.start():])
    return rest[:match.end() - match.start() + following.start()] if following else rest
