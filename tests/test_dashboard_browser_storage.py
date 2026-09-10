"""Ratchet: persistent dashboard feature state lives on the server, not in the browser.

The dashboard's feature state moved to the server-backed ``dashboard_state``
namespaces (docs/superpowers/specs/2026-09-10-dashboard-state-contract-design.md)
with no migration and no browser fallback. What may still persist in the
browser is device-local transport state, and only through
``dashboard/src/deviceLocal.ts``, whose keys must each be documented in
``dashboard/CLAUDE.md``. The vitest suite does not run in CI, so this is where
the rule is enforced.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
DASHBOARD = ROOT / "dashboard"
SRC = DASHBOARD / "src"
DEVICE_LOCAL_MODULE = SRC / "deviceLocal.ts"
DASHBOARD_DOCS = DASHBOARD / "CLAUDE.md"

# Device-local transport keys: meaningless on another device, recoverable by
# reconnecting. Adding one needs the same kind of justification in
# dashboard/CLAUDE.md — a remembered UI choice never qualifies.
ALLOWED_DEVICE_LOCAL_KEYS = {
    "aq:ws:last_seq",
    "aq:ws:epoch",
    "aq:session:id",
}

# Feature-state keys (and the in-page fan-out events that accompanied them)
# retired by the move to server state. Nothing under src/ may mention them —
# not as a reader, and not as a test seeding a legacy value to prove it is
# ignored.
RETIRED_FEATURE_KEYS = {
    "aq.shell.project-organization",
    "aq:shellpane:width:",
    "aq:rightsurface:width",
    "aq:flock:collapsed",
    "aq.dashboard.lastProjectId",
    "aq.command-center.graph-density",
    "aq:command-center:expanded-task-ids:v1",
    "aq:command-center:expanded-finished-task-ids:v1",
    "aq.command-center.graph-positions",
    "aq:project-organization-changed",
    "aq:command-center-graph-positions-changed",
}

BROWSER_STORAGE = re.compile(
    r"\b(?:localStorage|sessionStorage|indexedDB|IDB[A-Z]\w*|CacheStorage|cookieStore)\b"
    r"|\bdocument\s*\.\s*cookie\b"
    r"|\bcaches\s*\.\s*(?:open|match|has|keys|delete)\b"
    r"|\bnavigator\s*\.\s*storage\b"
    r"|\bStorage\s*\.\s*prototype\b"
)

SOURCE_SUFFIXES = {".ts", ".tsx", ".js", ".jsx", ".mts", ".cts"}


def _sources() -> list[Path]:
    return sorted(p for p in SRC.rglob("*") if p.suffix in SOURCE_SUFFIXES and p.is_file())


def _is_test_code(path: Path) -> bool:
    rel = path.relative_to(SRC)
    return (
        ".test." in path.name
        or "__tests__" in rel.parts
        or "testUtils" in rel.parts
        or path.name == "setupTests.ts"
    )


def _registry_keys() -> set[str]:
    text = DEVICE_LOCAL_MODULE.read_text(encoding="utf-8")
    block = re.search(r"DEVICE_LOCAL_KEYS\s*=\s*\{(.*?)\}\s*as\s+const", text, re.DOTALL)
    assert block, f"{DEVICE_LOCAL_MODULE.relative_to(ROOT)} lost its DEVICE_LOCAL_KEYS registry"
    return set(re.findall(r'^\s*"([^"]+)"\s*:', block.group(1), re.MULTILINE))


def test_only_the_device_local_module_touches_browser_storage() -> None:
    production = [p for p in _sources() if not _is_test_code(p)]
    assert len(production) > 100, "the scan no longer sees the dashboard sources"
    assert BROWSER_STORAGE.search(DEVICE_LOCAL_MODULE.read_text(encoding="utf-8")), (
        "the storage pattern no longer matches the module that owns browser storage"
    )

    violations = []
    for path in production:
        if path == DEVICE_LOCAL_MODULE:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            match = BROWSER_STORAGE.search(line)
            if match:
                violations.append(f"{path.relative_to(ROOT)}:{lineno}: {match.group(0)}")

    assert violations == [], (
        "Persistent dashboard feature state belongs on the server (useDashboardDocument). "
        "Device-local transport state goes through dashboard/src/deviceLocal.ts with a key "
        "documented in dashboard/CLAUDE.md. Offending references:\n  " + "\n  ".join(violations)
    )


def test_device_local_registry_holds_only_documented_transport_keys() -> None:
    assert _registry_keys() == ALLOWED_DEVICE_LOCAL_KEYS

    docs = DASHBOARD_DOCS.read_text(encoding="utf-8")
    undocumented = sorted(key for key in ALLOWED_DEVICE_LOCAL_KEYS if f"`{key}`" not in docs)
    assert undocumented == [], f"device-local keys missing from dashboard/CLAUDE.md: {undocumented}"


def test_retired_feature_keys_are_gone_from_source_and_tests() -> None:
    violations = []
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        for key in sorted(RETIRED_FEATURE_KEYS):
            if key in text:
                violations.append(f"{path.relative_to(ROOT)}: {key}")
    assert violations == []
