"""The documentation-overhaul coverage check, and the contract that splits it.

``docs/plans/documentation-overhaul/refresh_inventory.py`` carries two
guarantees that belong to different owners:

* **coverage** — every tracked path matches an ownership rule.  Every ticket
  runs this before it pushes (``--check``), and it must stay green on ``main``.
* **freshness** — the committed ``module-inventory.json`` /
  ``module-ownership.json`` still match the tree.  That pair is owned by the
  overhaul's foundation/acceptance shard and is regenerated on a cadence, so a
  stale artefact is an acceptance-gate failure (``--check-artefacts``), never a
  reason a documentation ticket cannot push.

These tests pin that split, because the failure they replace was ``--check``
going red on ``main`` for every ticket the moment any other ticket added a page.
"""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "docs" / "plans" / "documentation-overhaul" / "refresh_inventory.py"


@pytest.fixture(scope="module")
def refresh():
    spec = importlib.util.spec_from_file_location("refresh_inventory", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def built(refresh):
    """``build()`` once — it shells out to ``git ls-files`` over the whole tree."""
    return refresh.build()


def test_every_tracked_path_has_a_documentation_owner(built):
    _inventory, _manifest, unassigned = built
    assert unassigned == [], (
        "tracked path(s) match no rule in RULES; add one naming the owning shard"
    )


def test_coverage_check_is_green_on_the_current_tree(refresh, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["refresh_inventory.py", "--check"])
    assert refresh.main() == 0
    assert "tracked paths assigned" in capsys.readouterr().out


def test_coverage_check_fails_on_a_path_no_rule_owns(refresh, monkeypatch, capsys):
    monkeypatch.setattr(refresh, "tracked_files", lambda: ["nobody/owns/this.xyz"])
    monkeypatch.setattr("sys.argv", ["refresh_inventory.py", "--check"])
    assert refresh.main() == 1
    out = capsys.readouterr().out
    assert "have no documentation owner" in out
    assert "nobody/owns/this.xyz" in out


def _write_stale_artefacts(refresh, built, tmp_path, monkeypatch) -> str:
    """Commit artefacts that are one tracked path behind the tree."""
    inventory, manifest, _ = built
    behind_manifest = copy.deepcopy(manifest)
    behind_inventory = copy.deepcopy(inventory)
    victim = max(behind_manifest["modules"])
    behind_manifest["modules"].pop(victim)
    for value in behind_inventory.values():
        if isinstance(value, list) and victim in value:
            value.remove(victim)
    inv_path = tmp_path / "module-inventory.json"
    own_path = tmp_path / "module-ownership.json"
    inv_path.write_text(json.dumps(behind_inventory, indent=2) + "\n", encoding="utf-8")
    own_path.write_text(json.dumps(behind_manifest, indent=2) + "\n", encoding="utf-8")
    monkeypatch.setattr(refresh, "INVENTORY", inv_path)
    monkeypatch.setattr(refresh, "OWNERSHIP", own_path)
    return victim


def test_stale_artefacts_are_a_note_not_a_failure_for_the_coverage_check(
    refresh, built, tmp_path, monkeypatch, capsys,
):
    victim = _write_stale_artefacts(refresh, built, tmp_path, monkeypatch)
    monkeypatch.setattr("sys.argv", ["refresh_inventory.py", "--check"])

    assert refresh.main() == 0
    out = capsys.readouterr().out
    assert "tracked paths assigned" in out
    assert "have changed since the coverage manifest was regenerated" in out
    assert victim in out
    assert "do not\nregenerate it on a ticket branch" in out.replace("Do not", "do not")


def test_stale_artefacts_fail_the_acceptance_gate(
    refresh, built, tmp_path, monkeypatch, capsys,
):
    victim = _write_stale_artefacts(refresh, built, tmp_path, monkeypatch)
    monkeypatch.setattr("sys.argv", ["refresh_inventory.py", "--check-artefacts"])

    assert refresh.main() == 1
    out = capsys.readouterr().out
    assert out.startswith("stale artefact(s): ")
    assert victim in out
    assert "run: python3 docs/plans/documentation-overhaul/refresh_inventory.py" in out


def test_acceptance_gate_is_green_when_the_artefacts_match_the_tree(
    refresh, built, tmp_path, monkeypatch, capsys,
):
    inventory, manifest, _ = built
    inv_path = tmp_path / "module-inventory.json"
    own_path = tmp_path / "module-ownership.json"
    refresh.write(inventory, inv_path)
    refresh.write(manifest, own_path)
    monkeypatch.setattr(refresh, "INVENTORY", inv_path)
    monkeypatch.setattr(refresh, "OWNERSHIP", own_path)
    monkeypatch.setattr("sys.argv", ["refresh_inventory.py", "--check-artefacts"])

    assert refresh.main() == 0
    assert "stale artefact(s)" not in capsys.readouterr().out


def test_drift_names_the_shard_a_new_path_was_assigned_to(refresh, built, tmp_path,
                                                          monkeypatch):
    victim = _write_stale_artefacts(refresh, built, tmp_path, monkeypatch)
    _inventory, manifest, _ = built

    entries = refresh.drift(manifest)

    assert (("+", victim, str(manifest["modules"][victim]["shard"])),) == tuple(entries)
