"""Fenced, non-destructive project storage copy and atomic binding publication.

A plan is evidence, never authority to overwrite a directory. Old storage and
verified full copies survive failures. Rollback is another forward copy of the
current generation into a fresh directory, never a switch to stale old files.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import stat
import time
from pathlib import Path

from sqlalchemy import select, text, update

from src.build_cache import cache_settings
from src.database.tables import (
    integration_branch_owners, jobs, job_workspace_pins, project_integration_leases, projects, repos,
    sessions, tasks, workspaces,
)
from src.jobs.artifacts import atomic_json, read_json
from src.projects.storage_binding import bound_path
from src.sessions.proctable import workspace_quiescent


class RelocationRefused(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     default=str).encode()).hexdigest()


def native_root(value):
    path = Path(value).expanduser()
    if not path.is_absolute() or path == Path("/") or path != path.resolve():
        raise RelocationRefused("destination must be absolute, canonical and without symlink parents")
    if re.match(r"^/mnt/[a-z](?:/|$)", str(path)):
        raise RelocationRefused("destination must use native Linux storage")
    # WSL Windows mounts can be mounted somewhere other than /mnt/d.
    mounts = Path("/proc/mounts")
    if mounts.exists():
        for line in mounts.read_text().splitlines():
            fields = line.split()
            if len(fields) > 2 and path.is_relative_to(fields[1].replace("\\040", " ")):
                if fields[2] in {"9p", "drvfs", "cifs", "fuseblk", "ntfs", "ntfs3"}:
                    raise RelocationRefused("destination filesystem is not native Linux storage")
    return path


def tree_manifest(root):
    """Hash bytes, links, modes and extended attributes (including Linux ACLs)."""
    root = Path(root)
    if not root.exists():
        return {"exists": False, "entries": {}, "bytes": 0}
    entries, size = {}, 0
    for path in [root, *sorted(root.rglob("*"))]:
        info = path.lstat()
        relative = str(path.relative_to(root))
        mode = stat.S_IMODE(info.st_mode)
        attrs = {name: os.getxattr(path, name, follow_symlinks=False).hex()
                 for name in sorted(os.listxattr(path, follow_symlinks=False))}
        if path.is_symlink():
            entry = {"kind": "link", "target": os.readlink(path)}
        elif path.is_dir():
            entry = {"kind": "directory"}
        elif path.is_file():
            with path.open("rb") as stream:
                sha = hashlib.file_digest(stream, "sha256").hexdigest()
            size += info.st_size
            entry = {"kind": "file", "size": info.st_size, "sha256": sha}
        else:
            raise RelocationRefused(f"special file requires owner handling: {path}")
        entries[relative] = {**entry, "mode": mode, "xattrs": attrs}
    return {"exists": True, "entries": entries, "bytes": size}


def sync_tree(root):
    """Persist copied data and directory entries before publishing database paths."""
    root = Path(root)
    for path in [*root.rglob("*"), root]:
        if path.is_symlink():
            continue
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    # mkdir(parents=True) may have created several ancestors. Persist their
    # entries as well, so a committed binding survives a machine power loss.
    for ancestor in root.parents:
        fd = os.open(ancestor, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def lfs_blockers(repo, common, names):
    blockers = []
    for name in names.split("\0"):
        file = repo / name
        if not name or file.is_symlink() or not file.is_file():
            continue
        with file.open("rb") as stream:
            pointer = stream.read(256)
        if not pointer.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
            continue
        match = re.search(rb"\noid sha256:([a-f0-9]{64})\nsize ([0-9]+)", pointer)
        if not match:
            blockers.append(f"invalid LFS pointer: {file}")
            continue
        oid = match[1].decode()
        obj = Path(common) / "lfs/objects" / oid[:2] / oid[2:4] / oid
        if not obj.is_file():
            blockers.append(f"missing LFS object: {oid}")
            continue
        with obj.open("rb") as stream:
            sha = hashlib.file_digest(stream, "sha256").hexdigest()
        if sha != oid or obj.stat().st_size != int(match[2]):
            blockers.append(f"corrupt LFS object: {oid}")
    return blockers


def verified_copy(source, destination, expected):
    """Never adopt partial copies or overwrite prior work after a crash."""
    destination = Path(destination)
    if destination.exists():
        if tree_manifest(destination) != expected:
            raise RelocationRefused(f"retained partial or changed copy: {destination}; use a fresh root")
        sync_tree(destination)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    if expected["exists"]:
        shutil.copytree(source, destination, symlinks=True, copy_function=shutil.copy2)
        if tree_manifest(destination) != expected:
            raise RelocationRefused(f"copy verification failed: {destination}")
        sync_tree(destination)
    # Absent stores remain absent; their usual owner creates them on first use.


class ProjectRelocation:
    def __init__(self, db, config, git, *, policy_reader, checks_reader,
                 quiescent=workspace_quiescent, crash_hook=None):
        self.db, self.config, self.git = db, config, git
        self.policy_reader, self.checks_reader = policy_reader, checks_reader
        self.quiescent, self.crash_hook = quiescent, crash_hook

    async def _snapshot(self, project_id, conn):
        project = (await conn.execute(select(projects).where(
            projects.c.id == project_id,
        ))).mappings().one_or_none()
        if project is None:
            raise RelocationRefused("unknown project")
        result = {"project": dict(project)}
        for name, table in (("repos", repos), ("workspaces", workspaces)):
            result[name] = [dict(row) for row in (await conn.execute(select(table).where(
                table.c.project_id == project_id,
            ).order_by(table.c.id))).mappings()]
        return result

    async def snapshot(self, project_id):
        """One repeatable-read snapshot for callers needing all four path families."""
        async with self.db._engine.connect() as conn:
            await conn.execution_options(isolation_level="REPEATABLE READ")
            async with conn.begin():
                return await self._snapshot(project_id, conn)

    def _config(self, project_id):
        cache = cache_settings(self.config, project_id)
        return {"data_dir": str(Path(self.config.data_dir).expanduser().resolve()),
                "cache": {"kind": cache.kind, "dir": cache.dir} if cache else None}

    def _paths(self, snapshot, root):
        project = snapshot["project"]
        binding = project["storage_binding"]
        candidates = []
        for repo in snapshot["repos"]:
            for field in ("source_path", "checkout_base_path"):
                if repo[field]:
                    candidates.append(repo[field])
            sha = hashlib.sha256(repo["id"].encode()).hexdigest()
            data = Path(self.config.data_dir)
            for path in (data / "integration-repositories" / f"{sha}.git",
                         data / "development-integration" / sha[:20],
                         data / "truth-reconciliation" / "development-integration" / sha[:20]):
                candidates.append(str(bound_path(binding, path)))
        for relative in ("job-snapshots", "batch-fixers", "branch-backups", "backups/branch-deletions"):
            candidates.append(str(bound_path(binding, Path(self.config.data_dir) / relative)))
        candidates.extend(row["workspace_path"] for row in snapshot["workspaces"])
        if project.get("workspace_path"):
            candidates.append(project["workspace_path"])
        cache = (binding or {}).get("cache", self._config(project["id"])["cache"])
        if cache:
            candidates.append(cache["dir"])
        paths = sorted({str(Path(p).expanduser().absolute()) for p in candidates}, key=len)
        roots = []
        for path in paths:
            if Path(path) != Path(path).resolve():
                raise RelocationRefused(f"source has a symlink parent: {path}")
            if not any(Path(path).is_relative_to(old) for old in roots):
                roots.append(path)
        if any(root.is_relative_to(p) or Path(p).is_relative_to(root) for p in roots):
            raise RelocationRefused("destination overlaps source storage")
        return {p: str(root / "data" / hashlib.sha256(p.encode()).hexdigest()[:20]) for p in roots}

    async def _fences(self, snapshot, paths, conn):
        project = snapshot["project"]
        pid = project["id"]
        if project["status"] != "PAUSED":
            raise RelocationRefused("pause project admission before relocation")
        if (project["hierarchical_integration_mode"] != "disabled"
                or project["hierarchical_integration_desired_mode"] != "disabled"
                or project["hierarchical_integration_draining"]):
            raise RelocationRefused("disable and drain this project's train before relocation")
        if any(row["locked_by_agent_id"] or row["locked_by_task_id"]
               for row in snapshot["workspaces"]):
            raise RelocationRefused("workspace has a live reservation or claim")
        repo_ids = [r["id"] for r in snapshot["repos"]]
        checks = (
            select(sessions.c.id).where(sessions.c.project_id == pid,
                (sessions.c.ended_at.is_(None) & (sessions.c.state != "stopped"))
                | sessions.c.claim_phase.is_not(None)),
            select(tasks.c.id).where(tasks.c.project_id == pid,
                tasks.c.status.in_(["ASSIGNED", "IN_PROGRESS"])),
            select(jobs.c.id).where(jobs.c.project_id == pid,
                jobs.c.ended_at.is_(None) | jobs.c.cleanup_blocked.is_(True)),
            select(job_workspace_pins.c.job_id).join(
                workspaces, workspaces.c.id == job_workspace_pins.c.workspace_id,
            ).where(workspaces.c.project_id == pid),
            select(integration_branch_owners.c.id).where(
                integration_branch_owners.c.repository_id.in_(repo_ids),
                (integration_branch_owners.c.handoff_state != "released")
                | (integration_branch_owners.c.expires_at > time.time())),
            select(project_integration_leases.c.project_id).where(
                project_integration_leases.c.project_id == pid,
                project_integration_leases.c.expires_at > time.time()),
        )
        for check in checks:
            if (await conn.execute(check.limit(1))).first():
                raise RelocationRefused("active writer, preparation, job or publication lease")
        for table, columns in ((repos, ("source_path", "checkout_base_path")),
                               (workspaces, ("workspace_path",))):
            others = (await conn.execute(select(table).where(table.c.project_id != pid))).mappings()
            for row in others:
                for field in columns:
                    if row[field] and self._overlaps(row[field], paths):
                        raise RelocationRefused("ambiguous shared repository/workspace ownership")
        others = (await conn.execute(select(projects).where(projects.c.id != pid))).mappings()
        for row in others:
            cache = (row["storage_binding"] or {}).get("cache", self._config(row["id"])["cache"])
            if cache and self._overlaps(cache["dir"], paths):
                raise RelocationRefused("ambiguous shared compiler-cache ownership")
            if any(self._overlaps(p, paths) for p in (row["storage_binding"] or {}).get("paths", {}).values()):
                raise RelocationRefused("ambiguous shared retained-store ownership")
        # These old backup stores are not partitioned by project. Do not silently
        # move other projects' recovery material or claim it has been relocated.
        for relative in ("job-snapshots", "batch-fixers", "branch-backups", "backups/branch-deletions"):
            shared = Path(self.config.data_dir) / relative
            if (bound_path(project["storage_binding"], shared) == shared
                    and shared.exists() and any(shared.iterdir())):
                raise RelocationRefused(f"owner must partition shared recovery store: {shared}")

    @staticmethod
    def _overlaps(path, roots):
        path = Path(path).expanduser().resolve()
        return any(path.is_relative_to(root) or Path(root).is_relative_to(path) for root in roots)

    async def _inventory(self, paths):
        result = {}
        for path in paths:
            root = Path(path)
            git_info, blockers = [], []
            if root.exists():
                if any(root.rglob(".aq/claim.json")):
                    blockers.append("claim file retained; retire through AQ before relocation")
                markers = list(root.rglob(".git"))
                if (root / "HEAD").is_file() and (root / "objects").is_dir():
                    markers.append(root)
                for marker in markers:
                    if marker.is_file():
                        blockers.append(f"linked worktree/submodule must be retired or materialized: {marker}")
                        continue
                    repo = marker.parent if marker.name == ".git" else marker
                    async def git(*args):
                        value = await self.git.arun_git_result(
                            ["--no-optional-locks", *args], cwd=str(repo),
                        )
                        if value.returncode:
                            raise RelocationRefused(value.stderr or "Git inventory failed")
                        return value.stdout.strip()
                    bare = await git("rev-parse", "--is-bare-repository") == "true"
                    common = await git("rev-parse", "--path-format=absolute", "--git-common-dir")
                    if not Path(common).resolve().is_relative_to(root):
                        blockers.append("external Git common directory")
                    if (Path(common) / "worktrees").exists() and any(
                        (Path(common) / "worktrees").iterdir()
                    ):
                        blockers.append("retire registered linked worktrees through AQ first")
                    if (Path(common) / "objects/info/alternates").exists():
                        blockers.append("external Git object alternates require materialization")
                    config = await git("config", "--local", "--list")
                    for setting in config.splitlines():
                        value = setting.partition("=")[2].removeprefix("file://")
                        if value.startswith("/") and (
                            re.match(r"^/mnt/[a-z](?:/|$)", value)
                            or self._overlaps(value, paths)
                        ):
                            blockers.append("Git config retains a path into relocated or Windows storage")
                    if any(line.startswith(("core.worktree=", "lfs.storage=", "include.path="))
                           for line in config.splitlines()):
                        blockers.append("external Git/LFS/config path requires owner handling")
                    status = "" if bare else await git("status", "--porcelain", "--untracked-files=all")
                    if status:
                        blockers.append("dirty or untracked worktree")
                    await git("fsck", "--full", "--no-reflogs")
                    remote_heads = {}
                    origin = await self.git.aget_remote_url(str(repo))
                    if origin:
                        remote_heads = await self.git.als_remote_head_refs(str(repo))
                        for ref, oid in remote_heads.items():
                            probe = await self.git.arun_git_result(
                                ["cat-file", "-e", oid + "^{commit}"], cwd=str(repo),
                            )
                            if probe.returncode or not await git(
                                "for-each-ref", "--format=%(refname)", "--contains=" + oid,
                            ):
                                blockers.append(f"fetch and retain current remote head before preview: {ref}")
                    if not bare:
                        blockers.extend(await asyncio.to_thread(
                            lfs_blockers, repo, common, await git("ls-files", "-z"),
                        ))
                    git_info.append({"path": str(repo), "origin": origin,
                                     "remote_heads": remote_heads, "bare": bare,
                                     "head": await git("rev-parse", "HEAD"),
                                     "refs": await git("show-ref"), "status": status,
                                     "common_dir": common, "config": config,
                                     "submodules": "" if bare else await git("submodule", "status")})
            manifest = await asyncio.to_thread(tree_manifest, root)
            for relative, entry in manifest["entries"].items():
                if entry["kind"] == "link":
                    target = root / relative
                    resolved = target.resolve()
                    # Relative links within the copied root preserve their meaning.
                    if Path(entry["target"]).is_absolute() or not resolved.is_relative_to(root):
                        blockers.append(f"external/absolute symlink requires owner handling: {target}")
            result[path] = {"manifest": manifest, "git": git_info, "blockers": blockers}
        return result

    async def preview(self, project_id, destination, *, owner_inventory):
        root = native_root(destination)
        if root.exists() and not (root / "relocation.json").is_file():
            raise RelocationRefused("destination is occupied; choose a fresh root")
        snapshot = await self.snapshot(project_id)
        paths = self._paths(snapshot, root)
        async with self.db._engine.connect() as conn:
            await self._fences(snapshot, paths, conn)
        if not snapshot["repos"]:
            raise RelocationRefused("project has no registered repository")
        for repo in snapshot["repos"]:
            url = (repo["url"] or "").removeprefix("file://")
            if url.startswith("/") and (
                re.match(r"^/mnt/[a-z](?:/|$)", url) or self._overlaps(url, paths)
            ):
                raise RelocationRefused("repository origin still depends on relocated or Windows storage")
        if not isinstance(owner_inventory, dict) or "services" not in owner_inventory:
            raise RelocationRefused("owner inventory must explicitly name services (empty if none)")
        services = owner_inventory["services"]
        if not isinstance(services, list) or any(
            not isinstance(item, dict) or item.get("stopped") is not True for item in services
        ):
            raise RelocationRefused("owner inventory has a running or unknown service")
        inventory = await self._inventory(paths)
        policy = await self.policy_reader(snapshot["project"])
        parent = root
        while not parent.exists():
            parent = parent.parent
        required = sum(v["manifest"]["bytes"] for v in inventory.values()) * 2
        if shutil.disk_usage(parent).free < required:
            raise RelocationRefused("insufficient space for destination plus independent recovery copy")
        plan = {"project_id": project_id, "destination": str(root), "snapshot": snapshot,
                "config": self._config(project_id), "paths": paths, "inventory": inventory,
                "policy": policy, "owner_inventory": owner_inventory, "required_bytes": required}
        return {"success": True, "preview": True, "digest": digest(plan), "plan": plan,
                "blockers": [b for v in inventory.values() for b in v["blockers"]]}

    async def _stopped(self, paths, project_id):
        existing = tuple(p for p in paths if Path(p).exists())
        if existing and not await self.quiescent(existing, f"relocation:{project_id}"):
            raise RelocationRefused("process inventory found a live storage user")

    async def apply(self, project_id, destination, *, expected_digest, owner_inventory):
        snapshot = await self.snapshot(project_id)
        binding = snapshot["project"]["storage_binding"] or {}
        if binding.get("plan_digest") == expected_digest:
            return {"success": True, "already_applied": True, "binding": binding}
        preview = await self.preview(project_id, destination, owner_inventory=owner_inventory)
        if preview["digest"] != expected_digest:
            raise RelocationRefused("stale preview: binding, configuration, policy or content changed")
        plan = preview["plan"]
        await self._stopped(plan["paths"], project_id)
        # Claims and linked checkouts must be retired before even making copies.
        unsafe = [b for b in preview["blockers"] if b != "dirty or untracked worktree"]
        if unsafe:
            raise RelocationRefused("; ".join(unsafe))
        root = Path(destination)
        receipt = root / "relocation.json"
        if root.exists():
            if (not receipt.is_file() or receipt.is_symlink()
                    or (await asyncio.to_thread(read_json, receipt)).get("digest") != expected_digest):
                raise RelocationRefused("destination is occupied by another plan or unowned content")
        else:
            await asyncio.to_thread(root.mkdir, parents=True)
            await asyncio.to_thread(atomic_json, receipt, {"digest": expected_digest, "plan": plan})
        for old, new in plan["paths"].items():
            manifest = plan["inventory"][old]["manifest"]
            await asyncio.to_thread(verified_copy, old, root / "backup" / Path(new).name, manifest)
        if preview["blockers"]:
            raise RelocationRefused(f"dirty worktree; full verified backup retained at {root / 'backup'}")
        for old, new in plan["paths"].items():
            await asyncio.to_thread(verified_copy, old, new, plan["inventory"][old]["manifest"])
        await self.checks_reader(plan, plan["paths"])
        if self.crash_hook:
            await self.crash_hook("prepared")
        # Publication is a short transaction. Table-level write fences cover old
        # claim/job/preparation producers as well as newer project row lockers.
        # Readers remain available and see either committed generation, never
        # separately committed repository/workspace/cache/store path changes.
        async with self.db._engine.begin() as conn:
            await conn.execute(text("SET LOCAL lock_timeout = '5s'"))
            await conn.execute(text(
                "LOCK TABLE projects, repos, workspaces, sessions, tasks, jobs, job_workspace_pins, "
                "integration_branch_owners, project_integration_leases, integration_check_evidence "
                "IN SHARE ROW EXCLUSIVE MODE"
            ))
            current = await self._snapshot(project_id, conn)
            if current != plan["snapshot"] or self._config(project_id) != plan["config"]:
                raise RelocationRefused("binding or configuration changed during preparation")
            await self._fences(current, plan["paths"], conn)
            if await self.policy_reader(current["project"]) != plan["policy"]:
                raise RelocationRefused("current playbook changed during preparation")
            await self._stopped([*plan["paths"], *plan["paths"].values()], project_id)
            for old, new in plan["paths"].items():
                expected = plan["inventory"][old]["manifest"]
                for path in (old, new, root / "backup" / Path(new).name):
                    if await asyncio.to_thread(tree_manifest, path) != expected:
                        raise RelocationRefused("source, destination or backup changed before cutover")
            for inventory in plan["inventory"].values():
                for repo in inventory["git"]:
                    if repo["origin"] and await self.git.als_remote_head_refs(
                        repo["path"],
                    ) != repo["remote_heads"]:
                        raise RelocationRefused("remote heads changed; fetch and preview again")
            await self.checks_reader(plan, plan["paths"])
            previous = current["project"]["storage_binding"] or {}
            paths = {old: str(bound_path({"paths": plan["paths"]}, new))
                     for old, new in previous.get("paths", {}).items()}
            paths.update(plan["paths"])
            binding = {"generation": previous.get("generation", 0) + 1, "paths": paths,
                       "plan_digest": expected_digest, "receipt": str(receipt),
                       "previous": previous, "cache": previous.get("cache", plan["config"]["cache"])}
            if binding["cache"]:
                binding["cache"] = {**binding["cache"], "dir": str(bound_path(
                    {"paths": plan["paths"]}, binding["cache"]["dir"]))}
            for row in current["repos"]:
                values = {field: str(bound_path({"paths": plan["paths"]}, row[field]))
                          for field in ("source_path", "checkout_base_path") if row[field]}
                await conn.execute(update(repos).where(repos.c.id == row["id"]).values(**values))
            for row in current["workspaces"]:
                await conn.execute(update(workspaces).where(workspaces.c.id == row["id"]).values(
                    workspace_path=str(bound_path({"paths": plan["paths"]}, row["workspace_path"]))))
            values = {"storage_binding": binding}
            if current["project"].get("workspace_path"):
                values["workspace_path"] = str(bound_path({"paths": plan["paths"]},
                                                          current["project"]["workspace_path"]))
            await conn.execute(update(projects).where(projects.c.id == project_id).values(**values))
            await self.db.log_event("project.storage_relocated", project_id=project_id,
                                    payload=json.dumps(binding), conn=conn)
            if self.crash_hook:
                await self.crash_hook("before_commit")
        return {"success": True, "preview": False, "binding": binding,
                "recovery_manifest": str(receipt),
                "rollback": "Preview current storage into a fresh rollback destination; never reuse stale storage."}
