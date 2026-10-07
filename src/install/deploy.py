"""Release selection and the operator-owned deployment record.

This uses the installer's injectable synchronous runner; daemon callers run
planning off the event loop. Remote tag OIDs are pinned before fetching them.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from pathlib import Path

from .command import CommandRunner, run_command

DEPLOY_RECORD = "deploy.json"
_SEMVER = re.compile(r"(?:^|[^0-9])(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


class DeployRefused(Exception):
    """The selector or record cannot safely identify a deployment."""


@dataclass(frozen=True, slots=True)
class DeploySelection:
    selector: str
    commit: str
    tag_oid: str | None = None
    kind: str = "unreleased"


def newest_tag(tags: dict[str, tuple[str, str | None]], glob: str) -> str | None:
    """Choose a stable semantic version, independent of tag creation time."""
    versions = []
    for name in tags:
        match = _SEMVER.search(name)
        if fnmatchcase(name, glob) and match:
            versions.append((tuple(int(part) for part in match.groups()), name))
    return max(versions)[1] if versions else None


class DeploySelector:
    def __init__(
        self,
        checkout: Path,
        *,
        execute: CommandRunner = run_command,
        remote: str = "origin",
        target: str = "main",
    ) -> None:
        self.checkout = checkout
        self.execute = execute
        self.remote = remote
        self.target = target

    def git(self, *args: str) -> str:
        result = self.execute(["git", "-C", str(self.checkout), *args], timeout=300.0)
        if not result.ok:
            raise DeployRefused(result.message())
        return result.out

    def resolve(self, *, ref: str | None = None, tag_glob: str | None = None) -> DeploySelection:
        listing = self.git("ls-remote", "--tags", self.remote)
        tags: dict[str, tuple[str, str | None]] = {}
        peeled = {}
        for line in listing.splitlines():
            oid, name = line.split()
            name = name.removeprefix("refs/tags/")
            if name.endswith("^{}"):
                peeled[name[:-3]] = oid
            else:
                tags[name] = (oid, None)
        tags = {name: (oid, peeled.get(name)) for name, (oid, _) in tags.items()}
        selected = ref.removeprefix("refs/tags/") if ref else newest_tag(tags, tag_glob or "v*")
        if selected is None:
            raise DeployRefused(f"deploy_tag_invalid: no release tag matches {tag_glob!r}")
        if selected not in tags:
            # A qualified tag or a version-shaped selector must never silently
            # become an unreleased branch deployment.
            if (ref and ref.startswith("refs/tags/")) or _SEMVER.search(selected):
                raise DeployRefused(f"deploy_tag_invalid: tag {selected!r} is missing")
            self.git("check-ref-format", f"refs/heads/{selected}")
            pin = self.git("ls-remote", "--heads", self.remote, f"refs/heads/{selected}")
            if not pin:
                raise DeployRefused(f"deploy_tag_invalid: selector {selected!r} is missing")
            commit = pin.split()[0]
            self.git("fetch", "--quiet", self.remote, f"refs/heads/{selected}")
            self.git("cat-file", "-e", f"{commit}^{{commit}}")
            return DeploySelection(selected, commit)
        tag_oid, commit = tags[selected]
        if commit is None:
            raise DeployRefused(f"deploy_tag_invalid: {selected!r} is a lightweight tag")
        self.git("check-ref-format", f"refs/heads/{self.target}")
        # Full target history is needed for ancestry even in a depth-one install.
        shallow = self.git("rev-parse", "--is-shallow-repository") == "true"
        self.git(
            "fetch",
            "--quiet",
            *(["--unshallow"] if shallow else []),
            self.remote,
            f"+refs/heads/{self.target}:refs/remotes/{self.remote}/{self.target}",
            f"refs/tags/{selected}:refs/tags/{selected}",
        )
        fetched = self.git("rev-parse", f"refs/tags/{selected}")
        if fetched != tag_oid or self.git("cat-file", "-t", tag_oid) != "tag":
            raise DeployRefused(f"deploy_tag_invalid: {selected!r} changed while fetching")
        if self.git("rev-parse", f"{tag_oid}^{{commit}}") != commit:
            raise DeployRefused(f"deploy_tag_invalid: {selected!r} does not peel to a commit")
        ancestry = self.execute(
            [
                "git",
                "-C",
                str(self.checkout),
                "merge-base",
                "--is-ancestor",
                commit,
                f"refs/remotes/{self.remote}/{self.target}",
            ],
            timeout=300.0,
        )
        if not ancestry.ok:
            raise DeployRefused(
                f"deploy_not_related: {selected!r} is not reachable from {self.remote}/{self.target}"
            )
        return DeploySelection(selected, commit, tag_oid, "release")


def read_record(state_dir: Path) -> dict | None:
    path = state_dir / DEPLOY_RECORD
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or not isinstance(record.get("selector"), str):
            raise ValueError("missing selector")
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", str(record.get("commit", ""))):
            raise ValueError("missing or invalid commit OID")
        return record
    except (OSError, ValueError) as error:
        raise DeployRefused(f"could not read {path}: {error}") from error


def write_record(
    state_dir: Path, selection: DeploySelection, previous: str, *, rollback: bool = False
) -> None:
    record = {
        **asdict(selection),
        "previous_commit": previous,
        "tag": selection.selector if selection.tag_oid else None,
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "kind": "rollback" if rollback else selection.kind,
    }
    save_record(state_dir, record)


def save_record(state_dir: Path, record: dict | None) -> None:
    path = state_dir / DEPLOY_RECORD
    if record is None:
        path.unlink(missing_ok=True)
        return
    state_dir.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
