"""Every tracked path must stay distinct on a case-insensitive filesystem.

macOS formats its disks case-insensitive by default.  A checkout there holds
`dashboard/src/pages/metrics/ProviderUsage.tsx` and `providerUsage.ts` side by
side, but TypeScript resolves `import "./ProviderUsage"` extension-first, finds
`providerUsage.ts`, and `tsc -b` fails with TS1149/TS1261 -- which stopped the
first macOS install at `dashboard.build` while Linux CI stayed green.  This test
runs from the checkout alone, so the collision is caught on Linux too.
"""

from __future__ import annotations

import subprocess
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Extensions a module resolver may add, so `a/Foo.tsx` and `a/foo.ts` collide.
MODULE_EXTENSIONS = (".tsx", ".ts", ".mts", ".cts", ".jsx", ".js", ".mjs", ".cjs")


def _tracked_paths() -> list[str]:
    output = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True
    ).stdout
    return [path for path in output.decode("utf-8").split("\0") if path]


def _module_key(path: str) -> str:
    lowered = path.lower()
    for extension in MODULE_EXTENSIONS:
        if lowered.endswith(extension):
            return lowered[: -len(extension)]
    return lowered


def _collisions(paths: list[str], key) -> list[list[str]]:
    groups: dict[str, set[str]] = defaultdict(set)
    for path in paths:
        groups[key(path)].add(path)
    return sorted(sorted(group) for group in groups.values() if len(group) > 1)


def test_no_two_tracked_paths_differ_only_in_case():
    assert _collisions(_tracked_paths(), str.lower) == []


def test_no_two_modules_resolve_to_the_same_import_on_macos():
    modules = [path for path in _tracked_paths() if path.lower().endswith(MODULE_EXTENSIONS)]
    # Two files of one module with different extensions (`x.ts` + `x.test.ts`
    # differ in stem, so they are fine) only collide when the stems differ in case.
    clashes = [
        group
        for group in _collisions(modules, _module_key)
        if len({Path(path).stem.split(".")[0] for path in group}) > 1
    ]
    assert clashes == []


def test_the_guard_recognises_the_collision_that_broke_macos():
    paths = [
        "dashboard/src/pages/metrics/ProviderUsage.tsx",
        "dashboard/src/pages/metrics/providerUsage.ts",
    ]
    assert _collisions(paths, _module_key) == [sorted(paths)]
