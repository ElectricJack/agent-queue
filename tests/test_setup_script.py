"""``setup.sh`` installs things that exist.

The contributor bootstrap is a shell script, so nothing type-checks it and
nothing fails when it drifts from ``pyproject.toml`` or from the runtime model.
It had drifted twice at once: it installed a ``gemini`` extra that no longer
exists (pip *warns* and silently skips an undefined extra, so the install
looked clean while ``google-genai`` was missing), and it installed ``acpx``
while explaining it as needed "by profiles whose ``runtime`` is ``acpx``" --
both the runtime and the ``runtime`` profile key were removed, and
``src/profiles/parser.py`` now rejects the key outright.

These assertions are what stop either coming back.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETUP_SH = ROOT / "setup.sh"
PYPROJECT = ROOT / "pyproject.toml"

#: ``pip install -e ".[a,b,c]"`` -- captures the bracketed extras list.
_EDITABLE_EXTRAS = re.compile(r"""pip\s+install\s+-e\s+["']?\.\[([^\]]+)\]""")


def _declared_extras() -> set[str]:
    data = tomllib.loads(PYPROJECT.read_text())
    return set(data["project"].get("optional-dependencies", {}))


def test_every_extra_setup_sh_installs_is_defined_in_pyproject() -> None:
    """An undefined extra is a warning, not an error -- so pip won't catch this.

    ``pip install -e ".[dev,cli,gemini]"`` printed
    ``WARNING: agent-queue does not provide the extra 'gemini'`` and carried on,
    leaving a "successful" setup without the Google SDK.
    """
    declared = _declared_extras()
    requested = _EDITABLE_EXTRAS.findall(SETUP_SH.read_text())
    assert requested, "setup.sh no longer installs the project with extras -- update this test"

    for group in requested:
        for extra in (part.strip() for part in group.split(",")):
            assert extra in declared, (
                f"setup.sh installs the extra '{extra}', which pyproject.toml does not "
                f"define. pip only warns about that, so the missing dependency is silent. "
                f"Defined extras: {sorted(declared)}"
            )


def test_setup_sh_does_not_resurrect_the_acpx_runtime() -> None:
    """``acpx`` and the ``runtime`` profile key were retired together."""
    text = SETUP_SH.read_text()
    assert "acpx" not in text, (
        "setup.sh mentions acpx; the ACP runtime was removed and every agent now runs "
        "as a tmux session selected by the profile's `## Config.harness` field."
    )
    assert "runtime:" not in text, (
        "setup.sh references the `runtime:` profile config key, which "
        "src/profiles/parser.py rejects. Point at `harness` instead."
    )


def test_setup_sh_hands_machine_setup_to_the_one_installer() -> None:
    """The contributor bootstrap prepares a checkout; ``aq install`` sets up the machine.

    The legacy first-run wizard was a second installation flow with its own
    prompts, its own configuration writer and a required Discord bot token.  It
    was deleted in favour of ``aq install``, which is the entry point the
    documentation describes; this asserts the split does not quietly return.
    """
    text = SETUP_SH.read_text()
    assert "aq install" in text, "setup.sh must hand machine setup to `aq install`"
    assert "setup_wizard" not in text, (
        "setup.sh references the retired first-run wizard; machine setup belongs to "
        "`aq install` (docs/reference/cli/install.md)."
    )
    assert not (ROOT / "src" / "setup_wizard.py").exists(), (
        "src/setup_wizard.py is back. Onboarding lives in src/install/ — the engine, the "
        "onboarding steps and the wizard front-end — behind the single `aq install` command."
    )


def test_setup_sh_carries_no_operator_specific_paths() -> None:
    """A shipped installer must not default to whatever box wrote it.

    ``AQ_MEMORY_PATH`` used to default to one developer's checkout under
    ``/mnt/d``, which is neither portable nor discoverable.
    """
    text = SETUP_SH.read_text()
    assert "/mnt/d" not in text
    assert "AQ_MEMORY_PATH:-/" not in text
