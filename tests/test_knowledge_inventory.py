"""Offline inventory: exact evidence, reconciliation, confinement and sealed hashes."""

from __future__ import annotations

import base64
import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from src.knowledge.imports.inventory import RootSpec, scan_inventory
from src.knowledge.imports.legacy_memory import (
    MalformedSource,
    ScanLimits,
    parse_export,
    parse_file,
)
from src.knowledge.imports.manifest import (
    Artifact,
    Inventory,
    InventoryItem,
    Source,
    canonical_json,
    seal_manifest,
    sha256,
    verify_manifest,
)

FIXTURE = Path(__file__).parent / "fixtures/knowledge/offline-vector-export.json"


@pytest.fixture(autouse=True)
def _pg_backend():
    """This slice never allocates or reads a database."""


def _root(tmp_path, name="notes", scope="project:fixture", kind="notes"):
    path = tmp_path / name
    path.mkdir()
    return RootSpec(name, path, scope, kind)


def _export(tmp_path, rows, *, alias="fixture", extra_collections=()):
    path = tmp_path / "export.json"
    path.write_bytes(
        canonical_json(
            {
                "export_version": 1,
                "collections": [
                    {
                        "name": "aq_fixture",
                        "scope_alias": alias,
                        "schema": {"original": "VARCHAR"},
                        "entries": rows,
                    },
                    *extra_collections,
                ],
            }
        )
    )
    return path


def _document(**changes):
    return {
        "entry_type": "document",
        "chunk_hash": "doc",
        "original": "original\n",
        "content": "summary",
        **changes,
    }


def _seal(inventory):
    return seal_manifest(
        inventory,
        source_installation_id="synthetic-installation",
        snapshot_id="synthetic-snapshot",
        snapshot_timestamp="2026-10-01T12:00:00Z",
    )


async def test_file_vector_reconciliation_preserves_every_original_and_field(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("offline scanner attempted network access")

    monkeypatch.setattr(socket, "socket", forbidden)
    root = _root(tmp_path)
    (root.path / "paired.md").write_bytes(b"Exact retained original.\n")
    file_only = b"---\ncreated: 2020-01-01\nsource_task: synthetic-task\n---\nUnindexed\r\n"
    (root.path / "file-only.md").write_bytes(file_only)
    export = tmp_path / "export.json"
    export.write_bytes(FIXTURE.read_bytes())
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (root.path / "paired.md", root.path / "file-only.md", export)
    }
    inventory = await scan_inventory(
        [root], vector_export=export, scope_aliases={"fixture": ["project:fixture"]}
    )
    assert inventory.vector_observation == "observed"
    assert sorted(item.classification for item in inventory.items) == [
        "file_only",
        "kv",
        "matched",
        "summary_only",
        "temporal_history",
        "vector_only",
    ]
    matched = next(item for item in inventory.items if item.classification == "matched")
    assert matched.original == "Exact retained original.\n"
    assert matched.summary == "Indexed summary"
    assert len(matched.sources) == 2
    vector = next(source for source in matched.sources if source.source_kind == "vector")
    assert vector.metadata["rows"][0]["retrieval_count"] == 42
    assert vector.metadata["rows"][0]["embedding"] == [0.25, 0.75]
    assert vector.metadata["collections"][0]["schema"]["original"] == "VARCHAR"
    temporal = next(item for item in inventory.items if item.classification == "temporal_history")
    assert temporal.disposition == "candidate"
    assert {row["valid_from"] for row in temporal.sources[0].metadata["rows"]} == {
        1700000000,
        1700000200,
    }
    assert all(item.original is None for item in (temporal,))
    lost = next(item for item in inventory.items if item.classification == "vector_only")
    assert lost.original == "Lost-file original\r\n" and lost.issues == ("missing_file",)
    missing = next(item for item in inventory.items if item.classification == "summary_only")
    assert missing.original is None and missing.disposition == "quarantined"
    manifest = _seal(inventory)
    data = verify_manifest(manifest.content, manifest.sha256)
    assert data["counts"]["sources"] == 7
    retained = {base64.b64decode(artifact["bytes_base64"]) for artifact in data["artifacts"]}
    assert file_only in retained and FIXTURE.read_bytes() in retained
    assert before == {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in before}


@pytest.mark.parametrize("supply_path", [False, True])
async def test_missing_export_is_not_observed_not_an_empty_store(tmp_path, supply_path):
    root = _root(tmp_path)
    (root.path / "one.md").write_bytes(b"file\n")
    inventory = await scan_inventory(
        [root], vector_export=tmp_path / "missing.json" if supply_path else None
    )
    assert inventory.vector_observation == "not_observed"
    assert inventory.items[0].classification in ("file_only", "unavailable_export")
    assert not any(item.classification == "vector_only" for item in inventory.items)
    if supply_path:
        missing = next(
            item for item in inventory.items if item.classification == "unavailable_export"
        )
        assert missing.disposition == "unavailable" and missing.issues == ("missing_source",)
    assert _seal(inventory)


async def test_observed_empty_export_is_distinct(tmp_path):
    inventory = await scan_inventory([], vector_export=_export(tmp_path, []))
    assert inventory.vector_observation == "observed" and inventory.items == ()
    assert len(inventory.artifacts) == 1


async def test_conflicting_originals_retain_both_without_mtime_winner(tmp_path):
    root = _root(tmp_path)
    original = b"file original\r\n"
    (root.path / "one.md").write_bytes(original)
    export = _export(
        tmp_path,
        [
            _document(
                source_root_id="notes",
                relative_path="one.md",
                original="other original",
                updated_at=9999999999,
            )
        ],
    )
    inventory = await scan_inventory(
        [root], vector_export=export, scope_aliases={"fixture": [root.source_scope]}
    )
    assert len(inventory.items) == 1
    item = inventory.items[0]
    assert item.classification == "conflicting_originals" and item.disposition == "quarantined"
    assert item.original is None and len(item.sources) == 2
    assert {artifact.content for artifact in inventory.artifacts} == {original, export.read_bytes()}
    assert item.sources[0].metadata["original"] == "file original\r\n"


async def test_same_file_multiple_vector_aliases_are_accounted_once(tmp_path):
    root = _root(tmp_path)
    (root.path / "one.md").write_bytes(b"original\n")
    rows = [_document(chunk_hash=key, source=str(root.path / "one.md")) for key in ("a", "b")]
    inventory = await scan_inventory(
        [root],
        vector_export=_export(tmp_path, rows),
        scope_aliases={"fixture": [root.source_scope]},
    )
    assert len(inventory.items) == 1 and len(inventory.items[0].sources) == 3
    assert inventory.items[0].classification == "matched"
    assert (
        verify_manifest(_seal(inventory).content, _seal(inventory).sha256)["counts"]["sources"] == 3
    )


@pytest.mark.parametrize(
    "aliases,issue",
    [
        ({"fixture": ["project:a", "project:b"]}, "scope_alias_collision"),
        ({}, "unknown_scope_alias"),
    ],
)
async def test_ambiguous_or_unknown_alias_is_quarantined(tmp_path, aliases, issue):
    inventory = await scan_inventory(
        [], vector_export=_export(tmp_path, [_document()]), scope_aliases=aliases
    )
    item = inventory.items[0]
    assert issue in item.issues and item.disposition == "quarantined"
    assert item.sources[0].source_scope.startswith("unresolved:")
    assert item.original == "original\n"


async def test_equal_content_does_not_cross_scope_or_imply_authority(tmp_path):
    a = _root(tmp_path, "a", "project:a", "agent-type")
    b = _root(tmp_path, "b", "project:b")
    for root in (a, b):
        (root.path / "same.md").write_bytes(b"Same title and exact content\n")
    export = _export(
        tmp_path,
        [
            _document(
                original="Same title and exact content\n",
                source_root_id="a",
                relative_path="same.md",
            )
        ],
    )
    inventory = await scan_inventory(
        [a, b], vector_export=export, scope_aliases={"fixture": ["project:b"]}
    )
    assert len(inventory.items) == 3
    vector = next(item for item in inventory.items if item.sources[0].source_kind == "vector")
    assert "source_scope_mismatch" in vector.issues and vector.disposition == "quarantined"
    assert all(item.classification != "matched" for item in inventory.items)


async def test_duplicate_vector_identity_retains_all_rows_and_quarantines(tmp_path):
    rows = [_document(original="first"), _document(original="second")]
    inventory = await scan_inventory(
        [], vector_export=_export(tmp_path, rows), scope_aliases={"fixture": ["project:fixture"]}
    )
    item = inventory.items[0]
    assert {"duplicate_vector_identity", "conflicting_originals"} <= set(item.issues)
    assert item.disposition == "quarantined" and len(item.sources[0].metadata["rows"]) == 2
    assert item.classification == "conflicting_originals" and item.original is None
    assert _seal(inventory)


@pytest.mark.parametrize(
    "row,issue",
    [
        (_document(original={"not": "text"}), "malformed_original"),
        (_document(content=5), "malformed_content"),
        ({"entry_type": "document", "original": "retained"}, "missing_vector_identity"),
        ("not an object", "malformed_vector_row"),
    ],
)
async def test_malformed_rows_are_retained_and_classified(tmp_path, row, issue):
    export = _export(tmp_path, [row])
    inventory = await scan_inventory(
        [], vector_export=export, scope_aliases={"fixture": ["project:fixture"]}
    )
    item = inventory.items[0]
    assert issue in item.issues and item.disposition == "quarantined"
    assert inventory.artifacts[0].content == export.read_bytes()
    assert _seal(inventory)


@pytest.mark.parametrize(
    "content",
    [
        b"not json",
        b"\xff",
        b'{"export_version":1,"export_version":1}',
        b'{"export_version":1,"collections":NaN}',
        b'{"export_version":1,"collections":[],"x":1e999}',
    ],
)
async def test_malformed_export_has_a_receipt_and_exact_artifact(tmp_path, content):
    export = tmp_path / "bad.json"
    export.write_bytes(content)
    inventory = await scan_inventory([], vector_export=export)
    assert inventory.vector_observation == "unavailable"
    assert inventory.items[0].classification == "malformed_export"
    assert inventory.items[0].disposition == "quarantined"
    assert inventory.artifacts[0].content == content
    assert _seal(inventory)


@pytest.mark.parametrize(
    "content,issue",
    [
        (b"\xfflost", "invalid_utf8"),
        (b"---\nunclosed", "unclosed_frontmatter"),
        (b"---\n[]\n---\ntext", "frontmatter_not_object"),
        (b"---\ntags: [bad\n---\ntext", "malformed_frontmatter"),
        (b"---\nx: 1\nx: 2\n---\ntext", "frontmatter_duplicate_or_invalid_key"),
        (b"---\nx: &x [a]\ny: *x\n---\ntext", "frontmatter_alias"),
        (b"## Original\none\n## Original\ntwo", "ambiguous_original_sections"),
    ],
)
async def test_bad_files_are_never_silently_skipped(tmp_path, content, issue):
    root = _root(tmp_path)
    (root.path / "bad.md").write_bytes(content)
    inventory = await scan_inventory([root])
    assert inventory.items[0].disposition == "quarantined"
    assert inventory.items[0].issues == (issue,)
    assert inventory.artifacts[0].content == content
    assert _seal(inventory)


def test_sections_frontmatter_and_facts_keep_temporal_and_text_evidence():
    content = "---\ncreated: 2020-01-01\nreview_id: rev-synthetic\n---\nSummary\r\n## Original\r\ne\u0301\r\n"
    document = parse_file(content.encode(), ScanLimits())
    assert document.original == "e\u0301\r\n" and document.summary == "Summary\r\n"
    assert document.frontmatter["created"] == "2020-01-01"
    facts = parse_file(b"## project\n- key: old\n- key: new\n", ScanLimits()).facts
    assert [fact["value"] for fact in facts] == ["old", "new"]


async def test_actual_legacy_writer_framing_matches_exact_vector_original(tmp_path):
    root = _root(tmp_path)
    summary = "Indexed summary\n"
    original = "Original with retained trailing CRLF\r\n"
    # Synthetic reproduction of MemoryService._write_vault_file's exact joining.
    frontmatter = [
        "---",
        "tags: [synthetic]",
        "created: 2020-01-01",
        "updated: 2020-01-02",
        "last_retrieved: null",
        "retrieval_count: 3",
        "---",
        "",
    ]
    content = "\n".join(frontmatter) + "\n".join([summary, "\n\n## Original\n", original]) + "\n"
    (root.path / "one.md").write_bytes(content.encode())
    export = _export(
        tmp_path,
        [
            _document(
                content=summary, original=original, source_root_id="notes", relative_path="one.md"
            )
        ],
    )
    inventory = await scan_inventory(
        [root], vector_export=export, scope_aliases={"fixture": [root.source_scope]}
    )
    item = inventory.items[0]
    assert (
        item.classification == "matched" and item.original == original and item.summary == summary
    )
    file = next(source for source in item.sources if source.source_kind == "notes")
    assert file.metadata["representation"] == "legacy_writer_v1"
    assert content.encode() in {artifact.content for artifact in inventory.artifacts}


def test_fenced_original_heading_is_preserved_as_ordinary_content():
    content = b"Example:\n```markdown\n## Original\nnot a separate original\n```\n"
    document = parse_file(content, ScanLimits())
    assert document.original.encode() == content and document.summary is None


async def test_explicit_missing_selection_and_root_are_accounted(tmp_path):
    root = _root(tmp_path)
    selected = RootSpec(root.root_id, root.path, root.source_scope, relative_paths=("deleted.md",))
    missing_root = RootSpec("gone", tmp_path / "gone", "system", "reviews")
    inventory = await scan_inventory([selected, missing_root])
    assert len(inventory.items) == 2
    assert all(item.disposition == "unavailable" for item in inventory.items)
    assert all("missing_source" in item.issues for item in inventory.items)
    assert _seal(inventory)


async def test_symlinks_traversal_archives_and_special_files_are_confined(tmp_path):
    root = _root(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"private")
    (root.path / "escape.md").symlink_to(outside)
    (root.path / "directory").symlink_to(tmp_path, target_is_directory=True)
    (root.path / "archive.zip").write_bytes(b"synthetic archive, not expanded")
    inventory = await scan_inventory([root])
    assert len(inventory.items) == 3
    assert all(item.disposition == "excluded" for item in inventory.items)
    assert b"private" not in {artifact.content for artifact in inventory.artifacts}
    selected = RootSpec(
        "selection", root.path, root.source_scope, relative_paths=("../outside.md",)
    )
    traversal = await scan_inventory([selected])
    assert traversal.items[0].issues == ("path_traversal",)
    linked_root = RootSpec("link", root.path / "directory", "project:fixture")
    assert (await scan_inventory([linked_root])).items[0].disposition == "unavailable"


@pytest.mark.parametrize("relative", ["../secret.md", "/secret.md", "a/../b.md", "a\\b.md"])
async def test_vector_source_paths_are_evidence_not_read_requests(tmp_path, relative):
    root = _root(tmp_path)
    export = _export(tmp_path, [_document(source_root_id="notes", relative_path=relative)])
    inventory = await scan_inventory(
        [root], vector_export=export, scope_aliases={"fixture": [root.source_scope]}
    )
    assert inventory.items[0].issues == ("unsafe_source_path",)
    assert inventory.items[0].disposition == "quarantined"


async def test_limits_never_claim_a_complete_inventory_after_truncation(tmp_path):
    root = _root(tmp_path)
    (root.path / "a.md").write_bytes(b"12345")
    bounded = await scan_inventory([root], limits=ScanLimits(max_file_bytes=4))
    assert bounded.items[0].issues == ("source_size_limit",) and bounded.artifacts == ()
    (root.path / "b.md").write_bytes(b"67890")
    with pytest.raises(MalformedSource, match="entry_count_limit"):
        await scan_inventory([root], limits=ScanLimits(max_entries=1))
    with pytest.raises(MalformedSource, match="total_size_limit"):
        await scan_inventory([root], limits=ScanLimits(max_total_bytes=6))
    with pytest.raises(MalformedSource, match="metadata_size_limit"):
        parse_file(b"---\nlarge: abcdefghijklmnop\n---\ntext", ScanLimits(max_metadata_bytes=5))
    with pytest.raises(MalformedSource, match="metadata_depth_limit"):
        parse_export(
            canonical_json({"export_version": 1, "collections": [], "deep": [[[]]]}),
            ScanLimits(max_depth=2),
        )


async def test_empty_directories_consume_the_global_entry_budget(tmp_path):
    root = _root(tmp_path)
    for directory in ("a", "b"):
        (root.path / directory).mkdir()
        (root.path / directory / "empty").mkdir()
    with pytest.raises(MalformedSource, match="entry_count_limit"):
        await scan_inventory([root], limits=ScanLimits(max_entries=3))


async def test_metadata_limits_preserve_stable_source_identity(tmp_path):
    export = _export(tmp_path, [_document(extra="x" * 1000)])
    aliases = {"fixture": ["project:fixture"]}
    regular = await scan_inventory([], vector_export=export, scope_aliases=aliases)
    bounded = await scan_inventory(
        [], vector_export=export, scope_aliases=aliases, limits=ScanLimits(max_metadata_bytes=200)
    )
    assert regular.items[0].item_key == bounded.items[0].item_key
    assert bounded.items[0].issues == ("metadata_size_limit",)
    assert bounded.items[0].sources[0].metadata["row_hashes"]
    assert bounded.artifacts[0].content == export.read_bytes()


async def test_seal_deterministic_across_root_and_selection_order(tmp_path):
    a = _root(tmp_path, "a")
    b = _root(tmp_path, "b")
    (a.path / "one.md").write_bytes(b"a\r\n")
    (b.path / "two.md").write_bytes(b"b\n")
    first = _seal(await scan_inventory([a, b]))
    second = _seal(await scan_inventory([b, a]))
    assert first == second and first.sha256 == sha256(first.content)
    data = verify_manifest(first.content, first.sha256)
    assert data["snapshot_timestamp"] == "2026-10-01T12:00:00.000000+00:00"
    (a.path / "one.md").write_bytes(b"changed\r\n")
    assert _seal(await scan_inventory([a, b])).sha256 != first.sha256


def _minimal_inventory():
    artifact = Artifact(b"original\r\n")
    source = Source("notes", "project:fixture", "notes/one.md", artifact.sha256, {})
    item = InventoryItem((source,), "file_only", "candidate", original="original\r\n")
    return Inventory((), "not_observed", (item,), (artifact,))


def test_seal_requires_snapshot_identity_and_checks_artifacts_and_accounting():
    sealed = _seal(_minimal_inventory())
    assert sealed.sha256 == "cf098dd2d77bc6d1b86ba64ebc303d5dbde2658ce8081d13642d90a53aebc75f"
    with pytest.raises(ValueError, match="manifest hash mismatch"):
        verify_manifest(sealed.content + b" ", sealed.sha256)
    data = json.loads(sealed.content)
    data["artifacts"][0]["bytes_base64"] = base64.b64encode(b"changed").decode()
    changed = canonical_json(data)
    with pytest.raises(ValueError, match="artifact hash"):
        verify_manifest(changed, sha256(changed))
    data = json.loads(sealed.content)
    data["counts"]["sources"] = 9
    changed = canonical_json(data)
    with pytest.raises(ValueError, match="accounting"):
        verify_manifest(changed, sha256(changed))
    with pytest.raises(ValueError, match="timezone"):
        seal_manifest(
            _minimal_inventory(),
            source_installation_id="x",
            snapshot_id="y",
            snapshot_timestamp="2026-10-01T12:00:00",
        )
    with pytest.raises(ValueError, match="exactly once"):
        item = _minimal_inventory().items[0]
        _seal(Inventory((), "not_observed", (item, item), _minimal_inventory().artifacts))


def test_imports_do_not_depend_on_core_schema_plugins_or_providers():
    code = """
import builtins
real_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.startswith(("src.database", "src.config", "src.providers", "src.plugins",
                        "aq_memory", "memsearch", "pymilvus", "httpx")):
        raise AssertionError("forbidden import: " + name)
    return real_import(name, *args, **kwargs)
builtins.__import__ = guarded
from src.knowledge.imports.inventory import scan_inventory
from src.knowledge.imports.manifest import seal_manifest
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
