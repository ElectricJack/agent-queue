"""A private, pinned Node.js for building the dashboard.

The dashboard toolchain needs Node.js 20 or newer (``@tailwindcss/oxide``
declares ``node >= 20``, and npm silently skips an optional native package
whose engine does not match, so an older Node fails deep inside the build with
"Cannot find native binding").  Building with whatever Node a machine happens
to have made the result depend on nvm, Homebrew, Ubuntu's 18.x ``nodejs``
package and global npm settings -- a first real macOS install failed exactly
there.

So the installer does not use the machine's Node at all.  It downloads one
official Node.js LTS release from nodejs.org, checks it against the SHA-256
recorded *in this file* (the exact bytes, not a checksum fetched beside the
archive), and unpacks it under AQ's own data directory.  Nothing is installed
system-wide, nothing needs ``sudo``, and every machine builds with the same
Node.
"""

from __future__ import annotations

import hashlib
import shutil
import tarfile
import tempfile
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

NODE_VERSION = "v24.21.0"

#: ``(system, arch)`` -> (archive name, SHA-256), from nodejs.org's
#: ``SHASUMS256.txt`` for NODE_VERSION.  Update all of them together.
NODE_ARCHIVES: dict[tuple[str, str], tuple[str, str]] = {
    ("darwin", "arm64"): (
        f"node-{NODE_VERSION}-darwin-arm64.tar.gz",
        "bed7eea5325e1108f32ce5228ddd6a5f0f08a499ee42aa7442aea583702f6057",
    ),
    ("darwin", "x86_64"): (
        f"node-{NODE_VERSION}-darwin-x64.tar.gz",
        "1462cb3b3046b815cf8ea436d3da450ec1a9f11dac7e5a46b0ada5305d7e8097",
    ),
    ("linux", "arm64"): (
        f"node-{NODE_VERSION}-linux-arm64.tar.gz",
        "724282c3b43aec998aa9527380465b45d229e021b58035f5f4f63095eabfe5d5",
    ),
    ("linux", "x86_64"): (
        f"node-{NODE_VERSION}-linux-x64.tar.gz",
        "6e1db87ef58b8819e5d5402eff1536491b18edd8eb7bee5ef7897876e88dc5ff",
    ),
}

DIST_URL = "https://nodejs.org/dist/{version}/{archive}"

#: Seconds to wait on a stalled download before giving up.
DOWNLOAD_TIMEOUT = 120.0

#: Written into a toolchain once it has been verified and unpacked, holding
#: the archive digest it came from.
VERIFIED_MARKER = ".aq-verified"

#: ``fetch(url, destination)`` writes the body of *url* to *destination*.
Fetcher = Callable[[str, Path], None]


class ToolchainError(Exception):
    """The pinned Node.js could not be provided; the message says why."""


@dataclass(frozen=True, slots=True)
class NodeToolchain:
    root: Path

    @property
    def bin(self) -> Path:
        return self.root / "bin"

    @property
    def node(self) -> Path:
        return self.bin / "node"

    @property
    def npm(self) -> Path:
        return self.bin / "npm"


def download(url: str, destination: Path) -> None:
    with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as response:
        with destination.open("wb") as handle:
            shutil.copyfileobj(response, handle, length=1 << 20)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def toolchain_root(state_dir: Path, system: str, arch: str) -> Path:
    return state_dir / "toolchain" / f"node-{NODE_VERSION}-{system}-{arch}"


def installed_toolchain(state_dir: Path, system: str, arch: str) -> NodeToolchain | None:
    """The pinned toolchain if it is already unpacked and verified here."""
    archive = NODE_ARCHIVES.get((system, arch))
    if archive is None:
        return None
    toolchain = NodeToolchain(toolchain_root(state_dir, system, arch))
    try:
        marker = (toolchain.root / VERIFIED_MARKER).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if marker != archive[1] or not toolchain.node.is_file() or not toolchain.npm.exists():
        return None
    return toolchain


def ensure_toolchain(
    state_dir: Path, system: str, arch: str, *, fetch: Fetcher = download
) -> NodeToolchain:
    """Return the pinned Node.js for this host, downloading it the first time."""
    existing = installed_toolchain(state_dir, system, arch)
    if existing is not None:
        return existing
    archive = NODE_ARCHIVES.get((system, arch))
    if archive is None:
        raise ToolchainError(
            f"there is no pinned Node.js build for {system}/{arch}; AQ builds its dashboard "
            "on macOS (Apple Silicon or Intel) and on x86_64 or arm64 Linux"
        )
    name, expected = archive
    root = toolchain_root(state_dir, system, arch)
    root.parent.mkdir(parents=True, exist_ok=True)
    url = DIST_URL.format(version=NODE_VERSION, archive=name)

    with tempfile.TemporaryDirectory(dir=root.parent, prefix=".download-") as scratch:
        scratch_dir = Path(scratch)
        tarball = scratch_dir / name
        try:
            fetch(url, tarball)
        except OSError as error:  # urllib's URLError and HTTPError are OSErrors
            raise ToolchainError(f"could not download Node.js from {url}: {error}") from error
        actual = _sha256(tarball)
        if actual != expected:
            raise ToolchainError(
                f"the Node.js download from {url} does not match its pinned checksum "
                f"(expected {expected[:12]}…, got {actual[:12]}…); it was discarded"
            )
        unpacked = scratch_dir / "unpacked"
        try:
            with tarfile.open(tarball, mode="r:gz") as bundle:
                # `data` refuses absolute paths, parent escapes and device
                # files, while keeping the executable bits and the relative
                # symlinks npm's launcher relies on.
                bundle.extractall(unpacked, filter="data")
        except (OSError, tarfile.TarError) as error:
            raise ToolchainError(f"could not unpack {name}: {error}") from error
        top = unpacked / name.removesuffix(".tar.gz")
        if not (top / "bin" / "node").is_file():
            raise ToolchainError(f"{name} did not contain bin/node")
        (top / VERIFIED_MARKER).write_text(expected + "\n", encoding="utf-8")
        if root.exists():
            shutil.rmtree(root)
        top.rename(root)
    return NodeToolchain(root)


__all__ = [
    "DIST_URL",
    "NODE_ARCHIVES",
    "NODE_VERSION",
    "VERIFIED_MARKER",
    "NodeToolchain",
    "ToolchainError",
    "download",
    "ensure_toolchain",
    "installed_toolchain",
    "toolchain_root",
]
