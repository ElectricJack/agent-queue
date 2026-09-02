"""Shipped-harness manifest: fixes reach pristine vault copies, edits survive.

Background: ``ensure_default_harnesses`` never overwrote an existing
``vault/harnesses/<name>.md``, so the ``is_regex`` fix to the shipped dialog
rules (PR #212) could not reach any install seeded before it.  The manifest
in ``src/sessions/harness_manifest.py`` records every hash a shipped file has
ever had; a vault copy matching an *older* shipped version is refreshed, and
anything else is an operator edit that is left alone and reported.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from src.sessions.harness_manifest import (
    SHIPPED_HARNESS_HASHES,
    audit_vault_harnesses,
    classify_vault_harness,
    list_shipped_harnesses,
    restore_shipped_harness,
    sha256_path,
    shipped_harness_dir,
    sync_vault_harnesses,
)
from src.sessions.harness_parser import parse_harness_markdown

FIXTURES = Path(__file__).parent / "fixtures" / "harnesses"
PRE_PR212_CLAUDE = FIXTURES / "claude-pre-pr212.md"


def _vault_copy(tmp_path: Path, filename: str) -> Path:
    return tmp_path / "vault" / "harnesses" / filename


# ---------------------------------------------------------------------------
# The ratchet: shipped files must be recorded in the manifest
# ---------------------------------------------------------------------------


class TestManifestRatchet:
    def test_every_shipped_file_has_a_manifest_entry(self):
        shipped = list_shipped_harnesses()
        assert shipped, "no shipped harnesses found"
        assert set(shipped) <= set(SHIPPED_HARNESS_HASHES), (
            f"shipped harness without a manifest entry: "
            f"{set(shipped) - set(SHIPPED_HARNESS_HASHES)}"
        )

    def test_current_shipped_hash_is_in_the_manifest(self):
        """Changing a shipped file without recording its hash fails here.

        Without the entry, an install seeded from *this* version could not be
        refreshed by the *next* one.  Fix: append the new hash to
        ``SHIPPED_HARNESS_HASHES[<file>]`` (``sha256sum`` the file).
        """
        for filename in list_shipped_harnesses():
            digest = sha256_path(os.path.join(shipped_harness_dir(), filename))
            assert digest in SHIPPED_HARNESS_HASHES[filename], (
                f"{filename} changed (sha256 {digest}) but the manifest in "
                "src/sessions/harness_manifest.py does not list that hash"
            )

    def test_manifest_hashes_are_well_formed(self):
        for filename, hashes in SHIPPED_HARNESS_HASHES.items():
            assert filename.endswith(".md")
            assert hashes, f"{filename} has an empty hash set"
            for h in hashes:
                assert len(h) == 64 and int(h, 16) >= 0

    def test_pre_pr212_claude_is_a_known_shipped_version(self):
        """The fixture is the file every pre-#212 install has in its vault."""
        assert sha256_path(str(PRE_PR212_CLAUDE)) in SHIPPED_HARNESS_HASHES["claude.md"]


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


class TestClassify:
    def test_missing(self, tmp_path):
        shipped = tmp_path / "shipped.md"
        shipped.write_text("new\n")
        assert classify_vault_harness(str(tmp_path / "nope.md"), str(shipped)) == "missing"

    def test_current(self, tmp_path):
        shipped = tmp_path / "shipped.md"
        vault = tmp_path / "vault.md"
        shipped.write_text("new\n")
        vault.write_text("new\n")
        assert classify_vault_harness(str(vault), str(shipped)) == "current"

    def test_stale_when_hash_is_a_previous_shipped_version(self, tmp_path):
        shipped = tmp_path / "shipped.md"
        vault = tmp_path / "vault.md"
        shipped.write_text("new\n")
        vault.write_text("old\n")
        old_hash = sha256_path(str(vault))
        assert (
            classify_vault_harness(str(vault), str(shipped), known_hashes={old_hash}) == "stale"
        )

    def test_edited_when_hash_is_unknown(self, tmp_path):
        shipped = tmp_path / "shipped.md"
        vault = tmp_path / "vault.md"
        shipped.write_text("new\n")
        vault.write_text("operator wrote this\n")
        assert classify_vault_harness(str(vault), str(shipped), known_hashes={"0" * 64}) == "edited"


# ---------------------------------------------------------------------------
# Sync (what ensure_default_harnesses runs at startup)
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_shipped(tmp_path):
    """A fake defaults dir with one harness at version 2, plus its manifest."""
    defaults = tmp_path / "defaults"
    defaults.mkdir()
    v1 = '---\nid: fake\n---\n\n## Config\n\n```json\n{"command": "fake", "v": 1}\n```\n'
    v2 = '---\nid: fake\n---\n\n## Config\n\n```json\n{"command": "fake", "v": 2}\n```\n'
    (defaults / "fake.md").write_text(v2, encoding="utf-8")
    import hashlib

    manifest = {
        "fake.md": frozenset(
            {
                hashlib.sha256(v1.encode()).hexdigest(),
                hashlib.sha256(v2.encode()).hexdigest(),
            }
        )
    }
    return {"dir": str(defaults), "manifest": manifest, "v1": v1, "v2": v2}


class TestSync:
    def test_creates_missing_copy(self, tmp_path, fake_shipped):
        data_dir = tmp_path / "data"
        result = sync_vault_harnesses(
            str(data_dir), defaults_dir=fake_shipped["dir"], known_hashes=fake_shipped["manifest"]
        )
        assert result["created"] == ["fake.md"]
        assert _vault_copy(data_dir, "fake.md").read_text() == fake_shipped["v2"]

    def test_refreshes_copy_matching_an_older_shipped_version(self, tmp_path, fake_shipped):
        data_dir = tmp_path / "data"
        copy = _vault_copy(data_dir, "fake.md")
        copy.parent.mkdir(parents=True)
        copy.write_text(fake_shipped["v1"], encoding="utf-8")

        result = sync_vault_harnesses(
            str(data_dir), defaults_dir=fake_shipped["dir"], known_hashes=fake_shipped["manifest"]
        )
        assert result["refreshed"] == ["fake.md"]
        assert result["created"] == [] and result["edited"] == []
        assert copy.read_text() == fake_shipped["v2"]

    def test_leaves_operator_edit_alone_and_warns(self, tmp_path, fake_shipped, caplog):
        data_dir = tmp_path / "data"
        copy = _vault_copy(data_dir, "fake.md")
        copy.parent.mkdir(parents=True)
        custom = '---\nid: fake\n---\n\n## Config\n\n```json\n{"command": "my-fake"}\n```\n'
        copy.write_text(custom, encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="src.sessions.harness_manifest"):
            result = sync_vault_harnesses(
                str(data_dir),
                defaults_dir=fake_shipped["dir"],
                known_hashes=fake_shipped["manifest"],
            )
        assert result["edited"] == ["fake.md"]
        assert result["skipped"] == ["fake.md"]
        assert result["refreshed"] == []
        assert copy.read_text() == custom
        assert any("fake.md" in r.message and "left alone" in r.message for r in caplog.records)
        assert any("aq vault reset-harness fake" in r.message for r in caplog.records)

    def test_current_copy_is_skipped_silently(self, tmp_path, fake_shipped):
        data_dir = tmp_path / "data"
        sync_vault_harnesses(
            str(data_dir), defaults_dir=fake_shipped["dir"], known_hashes=fake_shipped["manifest"]
        )
        result = sync_vault_harnesses(
            str(data_dir), defaults_dir=fake_shipped["dir"], known_hashes=fake_shipped["manifest"]
        )
        assert result == {"created": [], "refreshed": [], "skipped": ["fake.md"], "edited": []}

    def test_audit_is_read_only(self, tmp_path, fake_shipped):
        data_dir = tmp_path / "data"
        copy = _vault_copy(data_dir, "fake.md")
        copy.parent.mkdir(parents=True)
        copy.write_text(fake_shipped["v1"], encoding="utf-8")
        report = audit_vault_harnesses(
            str(data_dir), defaults_dir=fake_shipped["dir"], known_hashes=fake_shipped["manifest"]
        )
        assert report["fake.md"]["status"] == "stale"
        assert copy.read_text() == fake_shipped["v1"]


# ---------------------------------------------------------------------------
# Acceptance: a pre-#212 install ends up with the shipped trust rule
# ---------------------------------------------------------------------------


class TestPrePR212Install:
    def test_pre_pr212_vault_copy_is_refreshed_to_the_shipped_file(self, tmp_path):
        """An install whose vault claude.md is the pre-#212 shipped file gets
        the current shipped file — and therefore its trust-folder rule —
        without anyone deleting the copy by hand."""
        copy = _vault_copy(tmp_path, "claude.md")
        copy.parent.mkdir(parents=True)
        copy.write_bytes(PRE_PR212_CLAUDE.read_bytes())

        from src.vault import ensure_default_harnesses

        result = ensure_default_harnesses(str(tmp_path))
        shipped = Path(shipped_harness_dir()) / "claude.md"
        assert copy.read_bytes() == shipped.read_bytes()
        assert "claude.md" not in result["edited"]
        # Either the fixture *is* the current shipped file (pre-#212 tree) or
        # it was refreshed; both leave the vault copy current.
        assert audit_vault_harnesses(str(tmp_path))["claude.md"]["status"] == "current"

        vault_rule = _trust_rule(copy.read_text(encoding="utf-8"))
        shipped_rule = _trust_rule(shipped.read_text(encoding="utf-8"))
        assert vault_rule == shipped_rule

    def test_operator_edited_claude_copy_survives(self, tmp_path):
        copy = _vault_copy(tmp_path, "claude.md")
        copy.parent.mkdir(parents=True)
        # A one-character edit to a real shipped version — the kind of tweak
        # an operator makes — is not a version we ever shipped.
        text = PRE_PR212_CLAUDE.read_text(encoding="utf-8").replace(
            '"command": "claude"', '"command": "my-claude"'
        )
        assert text != PRE_PR212_CLAUDE.read_text(encoding="utf-8")
        copy.write_text(text, encoding="utf-8")

        from src.vault import ensure_default_harnesses

        result = ensure_default_harnesses(str(tmp_path))
        assert result["edited"] == ["claude.md"]
        assert copy.read_text(encoding="utf-8") == text


def _trust_rule(text: str):
    parsed = parse_harness_markdown(text, fallback_id="claude")
    assert parsed.harness is not None, parsed.errors
    for rule in parsed.harness.dialogs:
        if rule.name == "trust-folder":
            return rule
    raise AssertionError("no trust-folder rule")


# ---------------------------------------------------------------------------
# Restore (aq vault reset-harness)
# ---------------------------------------------------------------------------


class TestRestore:
    def test_restores_edited_copy(self, tmp_path, fake_shipped):
        data_dir = tmp_path / "data"
        copy = _vault_copy(data_dir, "fake.md")
        copy.parent.mkdir(parents=True)
        copy.write_text("operator\n", encoding="utf-8")
        out = restore_shipped_harness(str(data_dir), "fake", defaults_dir=fake_shipped["dir"])
        assert out["previous_status"] == "edited"
        assert out["name"] == "fake"
        assert copy.read_text() == fake_shipped["v2"]

    def test_accepts_md_suffix_and_creates_missing(self, tmp_path, fake_shipped):
        data_dir = tmp_path / "data"
        out = restore_shipped_harness(str(data_dir), "fake.md", defaults_dir=fake_shipped["dir"])
        assert out["previous_status"] == "missing"
        assert _vault_copy(data_dir, "fake.md").read_text() == fake_shipped["v2"]

    def test_unknown_name_raises(self, tmp_path, fake_shipped):
        with pytest.raises(FileNotFoundError):
            restore_shipped_harness(str(tmp_path), "nope", defaults_dir=fake_shipped["dir"])
