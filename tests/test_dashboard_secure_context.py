"""Ratchet: the dashboard never calls ``crypto.randomUUID`` directly.

Browsers expose ``crypto.randomUUID`` only in secure contexts (HTTPS or
localhost). Served over plain HTTP from a LAN address, a bare call throws
``crypto.randomUUID is not a function`` and takes the pane down with it.
``dashboard/src/randomUUID.ts`` falls back to ``getRandomValues`` and is the
only module allowed to reference it. ESLint carries the same rule, but neither
lint nor the vitest suite runs in CI, so this is where it is enforced.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
SRC = ROOT / "dashboard" / "src"
HELPER = SRC / "randomUUID.ts"

RANDOM_UUID_MEMBER = re.compile(r"(?:\.|\?\.|\[\s*[\"'`])\s*randomUUID\b")
SOURCE_SUFFIXES = {".ts", ".tsx", ".js", ".jsx", ".mts", ".cts"}


def _sources() -> list[Path]:
    return sorted(p for p in SRC.rglob("*") if p.suffix in SOURCE_SUFFIXES and p.is_file())


def test_only_the_helper_references_crypto_random_uuid() -> None:
    sources = _sources()
    assert len(sources) > 100, "the scan no longer sees the dashboard sources"
    assert RANDOM_UUID_MEMBER.search(HELPER.read_text(encoding="utf-8")), (
        "the pattern no longer matches the helper that owns crypto.randomUUID"
    )

    violations = []
    for path in sources:
        if path == HELPER:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            match = RANDOM_UUID_MEMBER.search(line)
            if match:
                violations.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")

    assert violations == [], (
        "crypto.randomUUID is undefined outside secure contexts (plain HTTP, non-localhost). "
        "Import { randomUUID } from dashboard/src/randomUUID.ts instead:\n  "
        + "\n  ".join(violations)
    )


def test_helper_falls_back_to_get_random_values() -> None:
    text = HELPER.read_text(encoding="utf-8")
    assert 'typeof generator?.randomUUID === "function"' in text
    assert "getRandomValues" in text
