"""The pinned Node.js the dashboard build uses instead of the machine's.

No test here touches the network: a fake fetcher writes a tiny tarball shaped
like an official Node.js archive, and the pinned checksum is swapped for that
tarball's so the verification path runs for real.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

import pytest

from src.install import node_toolchain
from src.install.node_toolchain import (
    NODE_ARCHIVES,
    NODE_VERSION,
    VERIFIED_MARKER,
    ToolchainError,
    ensure_toolchain,
    installed_toolchain,
)

SYSTEM, ARCH = "darwin", "arm64"


def _archive_bytes(name: str, *, with_node: bool = True, escape: bool = False) -> bytes:
    top = name.removesuffix(".tar.gz")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        entries = {f"{top}/lib/node_modules/npm/bin/npm-cli.js": b"#!/usr/bin/env node\n"}
        if with_node:
            entries[f"{top}/bin/node"] = b"\x7fELF-not-really\n"
        if escape:
            entries["../outside.txt"] = b"escaped\n"
        for path, data in entries.items():
            info = tarfile.TarInfo(path)
            info.size = len(data)
            info.mode = 0o755
            bundle.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo(f"{top}/bin/npm")
        link.type = tarfile.SYMTYPE
        link.linkname = "../lib/node_modules/npm/bin/npm-cli.js"
        bundle.addfile(link)
    return buffer.getvalue()


@pytest.fixture
def pinned(monkeypatch):
    """Pin this host's archive to a tarball we build, and count downloads."""
    name = NODE_ARCHIVES[(SYSTEM, ARCH)][0]
    state = {"body": _archive_bytes(name), "urls": []}

    def pin(body: bytes) -> None:
        state["body"] = body
        monkeypatch.setitem(
            NODE_ARCHIVES, (SYSTEM, ARCH), (name, hashlib.sha256(body).hexdigest())
        )

    def fetch(url: str, destination: Path) -> None:
        state["urls"].append(url)
        destination.write_bytes(state["body"])

    pin(state["body"])
    state["pin"] = pin
    state["fetch"] = fetch
    state["name"] = name
    return state


def test_a_verified_archive_is_unpacked_and_marked(tmp_path, pinned):
    toolchain = ensure_toolchain(tmp_path, SYSTEM, ARCH, fetch=pinned["fetch"])

    assert pinned["urls"] == [f"https://nodejs.org/dist/{NODE_VERSION}/{pinned['name']}"]
    assert toolchain.node.is_file()
    assert toolchain.npm.is_symlink() and toolchain.npm.resolve().is_file()
    assert (toolchain.root / VERIFIED_MARKER).read_text().strip() == NODE_ARCHIVES[(SYSTEM, ARCH)][1]
    assert not [p for p in toolchain.root.parent.iterdir() if p.name.startswith(".download-")]


def test_an_installed_toolchain_is_reused_without_downloading(tmp_path, pinned):
    ensure_toolchain(tmp_path, SYSTEM, ARCH, fetch=pinned["fetch"])
    ensure_toolchain(tmp_path, SYSTEM, ARCH, fetch=pinned["fetch"])

    assert len(pinned["urls"]) == 1


def test_a_download_that_does_not_match_the_pin_is_discarded(tmp_path, pinned):
    tampered = _archive_bytes(pinned["name"]) + b"extra"

    def fetch(url, destination):
        destination.write_bytes(tampered)

    with pytest.raises(ToolchainError, match="does not match its pinned checksum"):
        ensure_toolchain(tmp_path, SYSTEM, ARCH, fetch=fetch)
    assert installed_toolchain(tmp_path, SYSTEM, ARCH) is None


def test_an_archive_escaping_its_directory_is_refused(tmp_path, pinned):
    pinned["pin"](_archive_bytes(pinned["name"], escape=True))

    with pytest.raises(ToolchainError, match="could not unpack"):
        ensure_toolchain(tmp_path, SYSTEM, ARCH, fetch=pinned["fetch"])
    assert not (tmp_path / "outside.txt").exists()


def test_an_archive_without_node_is_refused(tmp_path, pinned):
    pinned["pin"](_archive_bytes(pinned["name"], with_node=False))

    with pytest.raises(ToolchainError, match="did not contain bin/node"):
        ensure_toolchain(tmp_path, SYSTEM, ARCH, fetch=pinned["fetch"])


def test_a_network_failure_is_reported_not_raised_raw(tmp_path):
    def offline(url, destination):
        raise OSError("connection refused")

    with pytest.raises(ToolchainError, match="could not download Node.js.*connection refused"):
        ensure_toolchain(tmp_path, SYSTEM, ARCH, fetch=offline)


def test_an_unpinned_platform_is_refused_before_any_download(tmp_path):
    def fetch(url, destination):
        raise AssertionError("must not download")

    with pytest.raises(ToolchainError, match="no pinned Node.js build for linux/ppc64le"):
        ensure_toolchain(tmp_path, "linux", "ppc64le", fetch=fetch)


def test_a_marker_from_another_release_is_not_trusted(tmp_path, pinned):
    toolchain = ensure_toolchain(tmp_path, SYSTEM, ARCH, fetch=pinned["fetch"])
    (toolchain.root / VERIFIED_MARKER).write_text("0" * 64)

    assert installed_toolchain(tmp_path, SYSTEM, ARCH) is None


def test_every_supported_host_has_a_pinned_archive():
    """The installer supports macOS on both architectures and WSL2 on x86_64/arm64."""
    assert set(NODE_ARCHIVES) == {
        ("darwin", "arm64"),
        ("darwin", "x86_64"),
        ("linux", "arm64"),
        ("linux", "x86_64"),
    }
    for name, digest in NODE_ARCHIVES.values():
        assert name.startswith(f"node-{NODE_VERSION}-") and name.endswith(".tar.gz")
        assert len(digest) == 64 and int(digest, 16) >= 0


def test_the_default_fetcher_is_the_real_download():
    assert node_toolchain.ensure_toolchain.__kwdefaults__["fetch"] is node_toolchain.download
