"""Execute existing tsc/Vite obligations in writable scratch with declared deps."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from tools.bazel.generate import copy_inputs, manifest
from tools.bazel.import_npm import check as check_lock
from tools.bazel.import_npm import resolve


def environment(base: Path, native: Path, node: Path) -> dict[str, str]:
    home, binary, temporary = (base / name for name in ("home", "bin", "tmp"))
    for directory in (home, binary, temporary):
        directory.mkdir()
    (binary / "node").symlink_to(node)
    return {
        "PATH": f"{binary}:{native / 'bin'}",
        "LD_LIBRARY_PATH": str(native / "lib"),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "TMPDIR": str(temporary),
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC",
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
    }


def supports_platform(package: dict) -> bool:
    for field, value in (("os", "linux"), ("cpu", "x64"), ("libc", "glibc")):
        entries = package.get(field, [])
        allowed = [item for item in entries if not item.startswith("!")]
        if "!" + value in entries or (allowed and value not in allowed and "any" not in allowed):
            return False
    return True


def materialize_npm(inputs: list[list[str]], root: Path) -> None:
    """Reconstruct npm locations from declared, integrity-checked package trees.

    Symlinking the pnpm store changes TypeScript's ancestor @types lookup. Copy
    archive contents instead, retaining npm's hoisting, aliases and nested deps.
    No package is resolved or read from an ambient node_modules directory.
    """
    archives = {}
    for source, relative in inputs:
        if "node_modules" not in Path(relative).parts:
            continue
        package = Path(source) / "package.json"
        if package.is_file():
            metadata = json.loads(package.read_text())
            archives[metadata["name"] + "@" + metadata["version"]] = Path(source)
    packages = json.loads((root / "package-lock.json").read_text())["packages"]
    for location, package in sorted(packages.items(), key=lambda pair: len(Path(pair[0]).parts)):
        if "node_modules/" not in location:
            continue
        path = Path(location)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"unsafe npm location: {location}")
        output = root / path
        if package.get("link"):
            workspace = package["resolved"]
            metadata = json.loads((root / workspace / "package.json").read_text())
            key = metadata["name"] + "@" + metadata["version"]
            workspace_output = root / workspace
            if key in archives:
                shutil.copytree(archives[key], workspace_output, dirs_exist_ok=True)
            elif not workspace_output.is_dir():
                raise ValueError(f"workspace absent from declared inputs: {workspace}")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.symlink_to(workspace_output, target_is_directory=True)
        else:
            # npm and rules_js omit optional archives for other platforms.
            if not supports_platform(package):
                if not package.get("optional"):
                    raise ValueError(f"required npm package excludes the pinned platform: {location}")
                continue
            name = package.get("name", location.rsplit("node_modules/", 1)[-1])
            key = name + "@" + package["version"]
            if key not in archives:
                raise ValueError(f"npm archive missing from declared rules_js closure: {key}")
            shutil.copytree(archives[key], output, dirs_exist_ok=True)


def run(command: list[str], root: Path, env: dict[str, str]) -> str:
    result = subprocess.run(command, cwd=root, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300)
    if result.returncode:
        raise RuntimeError(f"dashboard command failed ({result.returncode}): {command}\n{result.stdout}")
    return result.stdout


def execute(kind: str, inputs: dict, output: Path, node: Path, native: Path,
            generated: Path | None = None) -> None:
    with tempfile.TemporaryDirectory(prefix="aq-dashboard-") as temporary:
        base = Path(temporary)
        root = base / "source"
        root.mkdir()
        env = environment(base, native, node)
        copy_inputs(inputs["srcs"], root)
        if kind == "package":
            if generated is None:
                raise ValueError("the TS package requires B2's generated artifact")
            shutil.copytree(generated / "packages/aq-ts-client/src",
                            root / "packages/aq-ts-client/src")
            shutil.copytree(root / "packages/aq-ts-client", output, dirs_exist_ok=True)
            return
        check_lock(root)
        materialize_npm(inputs["npm"], root)
        dashboard = root / "dashboard"
        packages = json.loads((root / "package-lock.json").read_text())["packages"]
        tsc = root / resolve(packages, "dashboard", "typescript") / "bin/tsc"
        vite = root / resolve(packages, "dashboard", "vite") / "bin/vite.js"
        arguments = [str(node), str(tsc), "-b"]
        if kind == "typecheck":
            arguments.append("--noEmit")
        run(arguments, dashboard, env)
        output.mkdir(exist_ok=True)
        if kind == "build":
            run([str(node), str(vite), "build"], dashboard, env)
            shutil.copytree(dashboard / "dist", output / "dist")
        # Invocation timing/logs belong in BEP, outside cached output bytes.
        (output / "result.json").write_text(json.dumps({"command": kind, "success": True}) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("package", "typecheck", "build"), required=True)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--node", type=Path, required=True)
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--generated", type=Path)
    args = parser.parse_args()
    execute(args.kind, json.loads(args.inputs.read_text()), args.output.absolute(),
            args.node.absolute(), args.native.absolute(), args.generated)
    args.manifest.write_text(json.dumps(manifest(args.output), indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
