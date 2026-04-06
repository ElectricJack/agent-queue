#!/usr/bin/env python3
"""Check for outdated pip dependencies, handling packages with invalid PEP 440 versions.

Some system-installed packages (e.g. distro-info on Ubuntu) have version strings
like '1.1build1' that are not PEP 440 compliant. This causes `pip list --outdated`
to crash with an InvalidVersion error.

This script:
1. Discovers packages with invalid version strings
2. Excludes them from the outdated check
3. Reports both outdated packages and skipped packages clearly

Usage:
    python scripts/check-outdated-deps.py [--json]

Exit codes:
    0 - Success (even if outdated packages found)
    1 - Error running pip commands
"""

import json
import subprocess
import sys
from typing import Any


def get_installed_packages() -> list[dict[str, str]]:
    """Get all installed packages as JSON (does not trigger version parsing)."""
    result = subprocess.run(
        [sys.executable, "-m", "pip", "list", "--format=json"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"ERROR: pip list failed: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    return json.loads(result.stdout)


def find_invalid_version_packages(packages: list[dict[str, str]]) -> list[dict[str, str]]:
    """Find packages with version strings that are not PEP 440 compliant."""
    try:
        from packaging.version import InvalidVersion, Version
    except ImportError:
        # Fall back to pip's vendored packaging
        from pip._vendor.packaging.version import InvalidVersion, Version  # type: ignore[no-redef]

    invalid = []
    for pkg in packages:
        try:
            Version(pkg["version"])
        except InvalidVersion:
            invalid.append(pkg)
    return invalid


def get_outdated_packages(
    exclude: list[str] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Run pip list --outdated, excluding packages with invalid versions.

    Returns (outdated_packages, stderr_output).
    """
    cmd = [sys.executable, "-m", "pip", "list", "--outdated", "--format=json"]
    for pkg_name in exclude or []:
        cmd.extend(["--exclude", pkg_name])

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return [], result.stderr
    return json.loads(result.stdout), result.stderr


def main() -> None:
    output_json = "--json" in sys.argv

    # Step 1: Get all installed packages
    packages = get_installed_packages()

    # Step 2: Find packages with invalid PEP 440 versions
    invalid_pkgs = find_invalid_version_packages(packages)
    exclude_names = [p["name"] for p in invalid_pkgs]

    # Step 3: Get outdated packages, excluding invalid ones
    outdated, stderr = get_outdated_packages(exclude=exclude_names)

    if stderr and not outdated:
        print(f"ERROR: pip list --outdated failed even after exclusions: {stderr}", file=sys.stderr)
        sys.exit(1)

    if output_json:
        result = {
            "outdated": outdated,
            "skipped_invalid_version": [
                {"name": p["name"], "version": p["version"]} for p in invalid_pkgs
            ],
        }
        print(json.dumps(result, indent=2))
    else:
        if invalid_pkgs:
            print("⚠ Skipped packages with non-PEP 440 versions (system packages):")
            for p in invalid_pkgs:
                print(f"  - {p['name']}=={p['version']}")
            print()

        if outdated:
            print(f"📦 {len(outdated)} outdated package(s) found:")
            # Compute column widths
            name_w = max(len(p["name"]) for p in outdated)
            ver_w = max(len(p["version"]) for p in outdated)
            lat_w = max(len(p["latest_version"]) for p in outdated)
            header = f"  {'Package':<{name_w}}  {'Current':<{ver_w}}  {'Latest':<{lat_w}}  Type"
            print(header)
            print(f"  {'-' * name_w}  {'-' * ver_w}  {'-' * lat_w}  ----")
            for p in outdated:
                print(
                    f"  {p['name']:<{name_w}}  {p['version']:<{ver_w}}  "
                    f"{p['latest_version']:<{lat_w}}  {p.get('latest_filetype', 'wheel')}"
                )
        else:
            print("✅ All packages are up to date.")


if __name__ == "__main__":
    main()
