"""Validate locked inputs and compile the Linux Python lock without local installs."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import subprocess
from string import Template
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOCK_PATH = Path("tools/bazel/toolchains.lock.json")
REQUIREMENTS_PATH = Path("tools/bazel/requirements_linux_x86_64.txt")


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_archive(path: Path, expected: str) -> None:
    if not re.fullmatch(r"[a-f0-9]{64}", expected) or sha256(path) != expected:
        raise ValueError(f"checksum mismatch: {path.name}")


def check_platform(lock: dict, system: str, machine: str, libc: tuple[str, str]) -> None:
    abi = lock["platform"]
    if system.lower() != abi["os"] or machine != abi["arch"] or libc[0] != abi["libc"]:
        raise ValueError("unsupported platform/ABI; use the legacy pytest/npm backend")
    version = tuple(int(p) for p in libc[1].split("."))
    lower = tuple(int(p) for p in abi["glibc_min"].split("."))
    upper = tuple(int(p) for p in abi["glibc_max_exclusive"].split("."))
    if not lower <= version < upper:
        raise ValueError("unsupported glibc ABI; use the legacy pytest/npm backend")


def requirements_input(root: Path, extras: list[str]) -> str:
    project = tomllib.loads((root / "pyproject.toml").read_text())
    reqs = list(project["project"]["dependencies"])
    for extra in extras:
        reqs.extend(project["project"]["optional-dependencies"][extra])
    client = "agent-queue-api-client @ file:packages/aq-client"
    if client not in reqs:
        raise ValueError("generated-client source contract changed; review the dependency inputs")
    reqs.remove(client)
    # Expand the source client's dependency intent, without building/installing it.
    metadata = tomllib.loads((root / "packages/aq-client/pyproject.toml").read_text())
    for name, constraint in metadata["tool"]["poetry"]["dependencies"].items():
        if name == "python":
            continue
        if not isinstance(constraint, str) or not constraint.startswith((">", "<", "=", "!", "~")):
            raise ValueError(f"unsupported client dependency constraint: {name}")
        reqs.append(name + constraint)
    reqs.extend(project["build-system"]["requires"])
    reqs.extend(metadata["build-system"]["requires"])
    if any("file:" in r or " @ " in r for r in reqs):
        raise ValueError("unreviewed direct dependency in Python lock inputs")
    return "\n".join(sorted(set(reqs))) + "\n"


def module_source(root: Path, lock: dict) -> str:
    values = dict(lock["modules"])
    for name in ("python", "node", "typescript"):
        for key, value in lock[name].items():
            values[f"{name}_{key}"] = value
    return Template((root / "tools/bazel/MODULE.bazel.template").read_text()).substitute(values)


def validate(root: Path) -> dict:
    lock = json.loads((root / LOCK_PATH).read_text())
    if lock["schema_version"] != 1:
        raise ValueError("unsupported toolchain lock schema")
    if (root / "MODULE.bazel").read_text() != module_source(root, lock):
        raise ValueError("MODULE.bazel drift; render from reviewed toolchain pins")
    if (root / ".bazelversion").read_text().strip() != lock["bazel"]["version"]:
        raise ValueError("Bazel version drift")
    for name in ("bazel", "python", "node", "uv", "micromamba"):
        archive = lock[name]
        if not re.fullmatch(r"[a-f0-9]{64}", archive["sha256"]):
            raise ValueError(f"{name} archive requires SHA-256")
        if not archive["url"].startswith("https://") or "latest" in archive["url"]:
            raise ValueError(f"{name} archive requires an immutable HTTPS URL")
    npm = json.loads((root / "package-lock.json").read_text())["packages"][
        "node_modules/typescript"
    ]
    for key, npm_key in (("version", "version"), ("integrity", "integrity"), ("url", "resolved")):
        if lock["typescript"][key] != npm[npm_key]:
            raise ValueError("TypeScript smoke pin drift from authoritative npm lock")
    for source, expected in lock["pip"]["sources"].items():
        if sha256(root / source) != expected:
            raise ValueError(f"dependency intent drift: {source}; regenerate the Python lock")
    verify_archive(root / REQUIREMENTS_PATH, lock["pip"]["requirements_sha256"])
    inputs = requirements_input(root, lock["pip"]["extras"])
    if hashlib.sha256(inputs.encode()).hexdigest() != lock["pip"]["input_sha256"]:
        raise ValueError("Python resolver input drift")
    entries = re.split(r"\n(?=[a-zA-Z0-9])", (root / REQUIREMENTS_PATH).read_text())
    for entry in entries:
        if not re.match(r"^[a-zA-Z0-9_.-]+==[^\s]+", entry):
            raise ValueError("Python lock requires exact package versions")
        if not re.search(r"--hash=sha256:[a-f0-9]{64}", entry):
            raise ValueError("Python lock requires package hashes")
    names = set()
    for package in lock["native"]["packages"]:
        if package["name"] in names:
            raise ValueError("duplicate native package")
        names.add(package["name"])
        if not re.fullmatch(r"[a-f0-9]{64}", package["sha256"]):
            raise ValueError("native package requires SHA-256")
        if not package["url"].startswith("https://conda.anaconda.org/conda-forge/"):
            raise ValueError("unreviewed native package origin")
        expected = lock["native"]["versions"].get(package["name"])
        if expected and expected != package["version"]:
            raise ValueError("native version drift")
    if not lock["native"]["versions"].keys() <= names:
        raise ValueError("native runtime is incomplete")
    for package in lock["native"]["packages"]:
        for dependency in package["depends"] or []:
            name = dependency.split()[0]
            if not name.startswith("__") and name not in names:
                raise ValueError(f"native runtime is incomplete: {name}")
    return lock


def compile_lock(root: Path, uv: Path) -> None:
    lock = json.loads((root / LOCK_PATH).read_text())
    verify_archive(uv, lock["uv"]["binary_sha256"])
    output = subprocess.check_output([str(uv), "--version"], text=True)
    if output.split()[1] != lock["uv"]["version"]:
        raise ValueError("wrong uv version; use the locked @aq_uv binary")
    with tempfile.TemporaryDirectory(prefix="aq-pip-lock-") as temporary:
        source = Path(temporary) / "requirements.in"
        inputs = requirements_input(root, lock["pip"]["extras"])
        source.write_text(inputs)
        subprocess.run(
            [
                str(uv),
                "pip",
                "compile",
                str(source),
                "--python-version",
                lock["python"]["version"],
                "--python-platform",
                lock["pip"]["python_platform"],
                "--only-binary",
                ":all:",
                "--generate-hashes",
                "--no-header",
                "--no-annotate",
                "--index-url",
                "https://pypi.org/simple",
                "-o",
                str(root / REQUIREMENTS_PATH),
            ],
            check=True,
        )
    lock["pip"]["sources"] = {path: sha256(root / path) for path in lock["pip"]["sources"]}
    lock["pip"]["input_sha256"] = hashlib.sha256(inputs.encode()).hexdigest()
    lock["pip"]["requirements_sha256"] = sha256(root / REQUIREMENTS_PATH)
    (root / LOCK_PATH).write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--compile", action="store_true")
    parser.add_argument("--render-module", action="store_true")
    parser.add_argument("--uv", type=Path)
    args = parser.parse_args()
    if args.compile:
        if args.uv is None:
            parser.error("--compile requires --uv pointing to the locked executable")
        compile_lock(args.root, args.uv.resolve())
    if args.render_module:
        lock = json.loads((args.root / LOCK_PATH).read_text())
        (args.root / "MODULE.bazel").write_text(module_source(args.root, lock))
    lock = validate(args.root)
    check_platform(lock, platform.system(), platform.machine(), platform.libc_ver())
    print("Toolchain lock, ABI and dependency intent: OK")


if __name__ == "__main__":
    main()
