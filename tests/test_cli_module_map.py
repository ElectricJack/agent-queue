"""``src/cli/CLAUDE.md``'s module map must name modules that actually exist.

The map is the first thing an agent reads before touching the CLI, and it
rotted exactly the way a hand-maintained inventory does: it still listed
``agents.py`` and ``hooks.py`` long after both were deleted (agent commands
are auto-generated from the tool definitions; hooks became playbooks), and
listed none of the fifteen modules added since.  A wrong map costs a turn to
discover and is invisible to every test that exercises the CLI itself.

The check is deliberately two-sided: a named module must exist, and an
existing module must be named.  One-sided would let the map go empty.
"""

from __future__ import annotations

import re
from pathlib import Path

CLI_DIR = Path(__file__).resolve().parents[1] / "src" / "cli"
MAP_DOC = CLI_DIR / "CLAUDE.md"

#: Not worth a line in an orientation map.
_EXEMPT = {"__init__.py"}

_ENTRY = re.compile(r"^(?P<module>[a-z_][a-z0-9_]*\.py)\b")


def _documented_modules() -> set[str]:
    """Module filenames listed in the fenced Architecture block."""
    found: set[str] = set()
    in_block = False
    for line in MAP_DOC.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            in_block = not in_block
            continue
        if not in_block:
            continue
        match = _ENTRY.match(line)
        if match:
            found.add(match.group("module"))
    return found


def _real_modules() -> set[str]:
    return {path.name for path in CLI_DIR.glob("*.py")} - _EXEMPT


def test_the_module_map_block_is_where_we_think_it_is():
    """A moved or reformatted block must not turn the check into a no-op."""
    documented = _documented_modules()
    assert len(documented) > 10, (
        f"{MAP_DOC} parsed to {sorted(documented)} — the Architecture fence moved "
        "or changed shape; fix this parser rather than deleting the check."
    )


def test_every_documented_cli_module_exists():
    missing = sorted(_documented_modules() - _real_modules())
    assert not missing, (
        "src/cli/CLAUDE.md names modules that no longer exist: "
        + ", ".join(missing)
        + " — delete the line or point it at what replaced the module."
    )


def test_every_cli_module_is_documented():
    undocumented = sorted(_real_modules() - _documented_modules())
    assert not undocumented, (
        "src/cli/CLAUDE.md does not mention: "
        + ", ".join(undocumented)
        + " — add a one-line entry to the Architecture block."
    )
