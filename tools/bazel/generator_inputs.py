"""Derive generator inputs and the npm generator closure without resolving packages.

Run with --write after an intentional source/lock change. Bazel reads the checked
inventory; --check refuses missing/new inputs instead of silently caching them.
The Python closure follows imports even inside functions and optional branches.
Command/API/CLI discovery roots account for their registry-driven dynamic loads.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINTS = (
    "src/api/spec.py",
    "src/integration/generated_merge_driver.py",
    "scripts/generate-recorded-artifacts.py",
    "scripts/bookkeeping_sources.py",
    "scripts/generate-selection-catalogue.py",
    "scripts/generate-command-catalogue.py",
    "scripts/generate-cli-command-inventory.py",
    "scripts/generate-config-schema-inventory.py",
    "scripts/generate-promotion-flow-schema.py",
    "scripts/gen-command-docs.py",
    "scripts/generate-playbook-schema.py",
    "scripts/generate-worker-capabilities.py",
)
TS_ROOTS = ("@hey-api/openapi-ts", "@hey-api/client-fetch", "typescript")


def selection_module_inputs(root: Path) -> set[str]:
    """The selector resolves test `from src... import child` using file existence.

    These modules need not be imported by any generator, but omitting their
    paths changes the catalogue's import list and therefore its digest.
    """
    paths = set()
    for test in (root / "tests").rglob("*.py"):
        for node in ast.walk(ast.parse(test.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ImportFrom) or node.level or not node.module:
                continue
            if node.module != "src" and not node.module.startswith("src."):
                continue
            for alias in node.names:
                base = root.joinpath(*node.module.split("."), alias.name)
                for path in (base.with_suffix(".py"), base / "__init__.py"):
                    if path.is_file():
                        paths.add(path.relative_to(root).as_posix())
    return paths


def python_closure(root: Path, entries: list[str]) -> list[str]:
    modules = {}
    for directory in ("src", "scripts"):
        for path in (root / directory).rglob("*.py"):
            relative = path.relative_to(root).as_posix()
            name = relative[:-3].replace("/", ".").removesuffix(".__init__")
            modules[name] = relative
    pending = list(entries)
    seen = set()
    while pending:
        relative = pending.pop()
        if relative in seen:
            continue
        seen.add(relative)
        path = root / relative
        package = relative[:-3].replace("/", ".").rsplit(".", 1)[0]
        if path.name == "__init__.py":
            package = relative[:-12].replace("/", ".")
        imports = []
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                name = node.module or ""
                if node.level:
                    name = importlib.util.resolve_name("." * node.level + name, package)
                imports.append(name)
                imports.extend(name + "." + alias.name for alias in node.names)
        # Importing a child executes each package initializer too.
        for name in imports + [relative[:-3].replace("/", ".")]:
            parts = name.split(".")
            for length in range(1, len(parts) + 1):
                candidate = modules.get(".".join(parts[:length]))
                if candidate and candidate not in seen:
                    pending.append(candidate)
    return sorted(seen)


def npm_closure(root: Path) -> dict:
    packages = json.loads((root / "package-lock.json").read_text())["packages"]
    manifest = json.loads((root / "packages/aq-ts-client/package.json").read_text())
    pending = ["node_modules/" + name for name in TS_ROOTS]
    result = {}
    while pending:
        location = pending.pop()
        if location in result:
            continue
        package = packages[location]
        if not package.get("resolved", "").startswith("https://registry.npmjs.org/"):
            raise ValueError(f"generator dependency is not an immutable npm archive: {location}")
        if not package.get("integrity", "").startswith("sha512-"):
            raise ValueError(f"generator dependency has no SHA-512 integrity: {location}")
        result[location] = {key: package[key] for key in ("version", "resolved", "integrity")}
        for name in sorted(set(package.get("dependencies", {})) | set(
            package.get("optionalDependencies", {})
        ) | set(package.get("peerDependencies", {}))):
            parent = location
            while True:
                candidate = parent + "/node_modules/" + name
                if candidate in packages:
                    pending.append(candidate)
                    break
                if not parent:
                    # Optional peers absent from the authoritative npm graph stay absent.
                    optional = name in package.get("optionalDependencies", {}) or package.get(
                        "peerDependenciesMeta", {}
                    ).get(name, {}).get("optional", False)
                    if not optional:
                        raise ValueError(f"unresolved locked dependency {location}: {name}")
                    break
                parent = parent.rsplit("/node_modules/", 1)[0] if "/node_modules/" in parent else ""
                candidate = (parent + "/" if parent else "") + "node_modules/" + name
                if candidate in packages:
                    pending.append(candidate)
                    break
    for name in TS_ROOTS:
        expected = manifest.get("devDependencies", {}).get(name) or manifest["dependencies"][name]
        if result["node_modules/" + name]["version"] != expected:
            raise ValueError(f"generator pin differs from npm intent: {name}")
    return {"schema_version": 1, "packages": dict(sorted(result.items()))}


def inventory(root: Path) -> list[str]:
    discovery = [
        path.relative_to(root).as_posix()
        for directory in ("src/api", "src/commands", "src/cli", "src/plugins/internal")
        for path in (root / directory).rglob("*.py")
    ]
    paths = set(python_closure(root, list(ENTRYPOINTS) + discovery))
    paths.update(selection_module_inputs(root))
    # These are scanned as data (catalogue ASTs, profile blocks and approved
    # recordings), rather than imported as application libraries.
    patterns = (
        "tests/**/*.py", "tests/selection_*.yaml",
        "tests/fixtures/generated_sources/**/*", "tests/fixtures/contracts/command_audit_modules/**/*",
        "src/profiles/defaults/**/*", "src/prompts/**/*", "src/sessions/default_harnesses/**/*",
        "scripts/regenerate-*.sh", "scripts/openapi-python-client.yaml",
        "packages/aq-client/**/*", "packages/aq-ts-client/package.json",
        "docs/reference/playbook-commands/*.md",
    )
    for pattern in patterns:
        paths.update(p.relative_to(root).as_posix() for p in root.glob(pattern) if p.is_file()
                     and not {"__pycache__", ".ruff_cache"}.intersection(p.parts)
                     and p.name != "BUILD.bazel"
                     and p.suffix != ".pyc")
    paths.update((
        "pyproject.toml", "package.json", "package-lock.json", ".gitattributes", ".gitignore",
        "openapi.json",
        "scripts/aq-client-boilerplate.sha256", "src/tools/command_catalogue.json",
        "src/playbook_v2_schema.json", "tests/selection_catalogue.json",
        "docs/reference/cli-command-inventory.json", "docs/reference/configuration-schema.json",
        "docs/reference/promotion-flow-schema.json", "docs/reference/playbook-commands/README.md",
        "tests/integration_ownership.json", "tests/integration_ownership_report.json",
        "tests/fixtures/contracts/generated-command-audit.json",
    ))
    # Recorded outputs also include the historical docs bundle.
    index = json.loads((root / "tests/fixtures/generated_sources/playbooks/index.json").read_text())
    for destinations in index.values():
        for destination in destinations:
            paths.update(p.relative_to(root).as_posix() for p in (root / destination).glob("*")
                         if p.is_file())
    return sorted(paths)


def render_inputs(root: Path) -> str:
    return (
        '# Generated by tools/bazel/generator_inputs.py --write; do not edit.\n'
        'GENERATOR_INPUTS = ' + json.dumps(inventory(root), indent=4) + '\n'
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    outputs = {
        "tools/bazel/generator_inputs.bzl": render_inputs(args.root),
        "tools/bazel/generator_npm.lock.json": json.dumps(
            npm_closure(args.root), indent=2, sort_keys=True,
        ) + "\n",
    }
    for relative, content in outputs.items():
        path = args.root / relative
        if args.write:
            path.write_text(content)
        elif not path.exists() or path.read_text() != content:
            parser.exit(1, f"Generator input/lock drift: {relative}; run {Path(__file__).name} --write\n")


if __name__ == "__main__":
    main()
