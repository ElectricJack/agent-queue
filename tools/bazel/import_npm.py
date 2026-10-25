"""Strict npm v3 -> pnpm v9 importer, version 1 (no registry or solver).

The npm workspace lock is authoritative. Unlike pnpm import's preferred-version
resolution, this converter follows npm's installed locations, including aliases,
peers and nested versions. Unsupported ambiguous contexts fail closed. The output
is JSON (a YAML subset) consumed directly by rules_js npm_translate_lock.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import posixpath
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERSION = 1
LOCK = "tools/bazel/pnpm-lock.yaml"
GROUPS = ("dependencies", "devDependencies", "optionalDependencies")


def identity(location: str, package: dict) -> str:
    return package.get("name", location.rsplit("node_modules/", 1)[-1]) + "@" + package["version"]


def resolve(packages: dict, location: str, name: str) -> str | None:
    """Node's upward node_modules lookup, without consulting the filesystem."""
    parent = location
    while True:
        candidate = (parent + "/" if parent else "") + "node_modules/" + name
        if candidate in packages:
            return candidate
        if not parent:
            return None
        parent = posixpath.dirname(parent)
        if posixpath.basename(parent) == "node_modules":
            parent = posixpath.dirname(parent)


def derive(root: Path) -> dict:
    npm_bytes = (root / "package-lock.json").read_bytes()
    npm = json.loads(npm_bytes)
    if npm["lockfileVersion"] != 3:
        raise ValueError("importer requires npm lockfile v3")
    packages = npm["packages"]
    manifest = json.loads((root / "package.json").read_text())
    workspaces = manifest["workspaces"]
    if not isinstance(workspaces, list) or any("*" in p or ".." in p for p in workspaces):
        raise ValueError("importer requires explicit workspace paths")
    if packages[""].get("workspaces") != workspaces:
        raise ValueError("npm workspace intent drift: workspaces")
    importers = {".": {}}
    result_packages = {}
    snapshots = {}

    def reference(location: str, name: str, optional: bool, importer: str | None = None):
        resolved = resolve(packages, location, name)
        if resolved is None:
            if optional:
                return None
            raise ValueError(f"unresolved npm dependency {location}: {name}")
        package = packages[resolved]
        if package.get("link"):
            workspace = package["resolved"]
            if workspace not in workspaces:
                raise ValueError(f"unknown workspace link: {workspace}")
            # Importer links are relative to the importing workspace. Snapshot
            # links are relative to the lock's directory (tools/bazel).
            return "link:" + posixpath.relpath(workspace, importer or "tools/bazel")
        return identity(resolved, package)

    for workspace in ["", *workspaces]:
        path = root / workspace / "package.json"
        source = json.loads(path.read_text())
        locked = packages[workspace]
        importer = {}
        for group in GROUPS:
            if source.get(group, {}) != locked.get(group, {}):
                raise ValueError(f"npm workspace intent drift: {workspace}/{group}")
            entries = {}
            for name, specifier in source.get(group, {}).items():
                version = reference(workspace, name, group == "optionalDependencies", workspace)
                if version is not None:
                    entries[name] = {"specifier": specifier, "version": version}
            if entries:
                importer[group] = entries
        importers[posixpath.relpath(workspace or ".", "tools/bazel")] = importer

    for location, package in packages.items():
        if "node_modules/" not in location or package.get("link"):
            continue
        key = identity(location, package)
        if not package.get("resolved", "").startswith("https://registry.npmjs.org/"):
            raise ValueError(f"unreviewed npm archive: {location}")
        if not package.get("integrity", "").startswith("sha512-"):
            raise ValueError(f"npm archive has no SHA-512 integrity: {location}")
        metadata = {"resolution": {"integrity": package["integrity"],
                                   "tarball": package["resolved"]}}
        for field in ("engines", "os", "cpu", "libc"):
            if field in package:
                metadata[field] = package[field]
        if package.get("bin"):
            metadata["hasBin"] = True
        if key in result_packages and result_packages[key] != metadata:
            raise ValueError(f"ambiguous npm package metadata: {key}")
        result_packages[key] = metadata
        snapshot = {}
        optional = package.get("optionalDependencies", {})
        peers = package.get("peerDependencies", {})
        dependencies = {**peers, **package.get("dependencies", {})}
        for group, entries in (("dependencies", dependencies), ("optionalDependencies", optional)):
            resolved = {}
            for name in entries:
                if name in package.get("bundleDependencies", []):
                    continue
                if group == "dependencies" and name in optional:
                    continue
                is_optional = group == "optionalDependencies" or package.get(
                    "peerDependenciesMeta", {}).get(name, {}).get("optional", False)
                version = reference(location, name, is_optional)
                if version is not None:
                    resolved[name] = version
            if resolved:
                snapshot[group] = resolved
        if key in snapshots and snapshots[key] != snapshot:
            raise ValueError(f"ambiguous npm dependency context: {key}; extend reviewed importer")
        snapshots[key] = snapshot

    return {
        "lockfileVersion": "9.0",
        "settings": {"autoInstallPeers": False, "excludeLinksFromLockfile": False},
        "aqImport": {
            "version": VERSION, "npmSha256": hashlib.sha256(npm_bytes).hexdigest(),
            "locations": {
                location: ("link:" + package["resolved"] if package.get("link")
                           else identity(location, package))
                for location, package in packages.items() if "node_modules/" in location
            },
        },
        "importers": importers,
        "packages": result_packages,
        "snapshots": snapshots,
    }


def render(root: Path) -> str:
    return json.dumps(derive(root), indent=2, sort_keys=True) + "\n"


def check(root: Path) -> None:
    if (root / LOCK).read_text() != render(root):
        raise ValueError("npm/pnpm graph drift; run python tools/bazel/import_npm.py --write")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    if args.write:
        (args.root / LOCK).write_text(render(args.root))
    check(args.root)
    print(f"npm/pnpm graph equivalence: exact (importer v{VERSION})")


if __name__ == "__main__":
    main()
