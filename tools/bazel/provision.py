"""Install the pinned Bazel executable for ``aq test --aq-backend bazel``.

The Bazel binary is never discovered from host paths. This script downloads the
release named by ``toolchains.lock.json``, checks the SHA-256 recorded there
(the reviewed pin, not a checksum fetched beside the download), and installs it
read-only under the AQ data directory:

    <data-dir>/toolchain/bazel-<version>-linux-x86_64/bin/bazel

An existing install is reused only when its recorded digest and its bytes both
match the pin. It prints the executable path, which operators wire into worker
sessions as ``AQ_BAZEL_EXECUTABLE`` (see ``ENVIRONMENT.md``).

    python tools/bazel/provision.py [--data-dir ~/.agent-queue]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import stat
import sys
import tempfile
import urllib.request
from collections.abc import Callable
from pathlib import Path

LOCK = Path(__file__).with_name("toolchains.lock.json")

#: Seconds to wait on a stalled download before giving up.
DOWNLOAD_TIMEOUT = 300.0

#: Written beside the binary once verified, holding the pinned digest.
VERIFIED_MARKER = ".aq-verified"

#: ``fetch(url, destination)`` writes the body of *url* to *destination*.
Fetcher = Callable[[str, Path], None]


class ProvisionError(Exception):
    """The pinned Bazel could not be installed; the message says why."""


def download(url: str, destination: Path) -> None:
    with (
        urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as response,
        destination.open("wb") as handle,
    ):
        shutil.copyfileobj(response, handle, length=1 << 20)


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def pinned_bazel(lock: Path = LOCK) -> dict[str, str]:
    entry = json.loads(lock.read_text(encoding="utf-8"))["bazel"]
    if not entry["url"].endswith(f"/bazel-{entry['version']}-linux-x86_64"):
        raise ProvisionError(f"lock URL does not name bazel {entry['version']} linux-x86_64")
    return entry


def install_root(data_dir: Path, version: str) -> Path:
    return data_dir / "toolchain" / f"bazel-{version}-linux-x86_64"


def installed(root: Path, sha256: str) -> Path | None:
    """The verified executable under *root*, or None if absent or altered."""
    executable = root / "bin" / "bazel"
    try:
        marker = (root / VERIFIED_MARKER).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if marker != sha256 or not executable.is_file() or _sha256(executable) != sha256:
        return None
    return executable


def _make_read_only(path: Path, mode: int) -> None:
    path.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def provision(data_dir: Path, *, fetch: Fetcher = download, lock: Path = LOCK) -> Path:
    if (sys.platform, platform.machine()) != ("linux", "x86_64"):
        raise ProvisionError("the pinned Bazel supports only Linux x86_64")
    pin = pinned_bazel(lock)
    root = install_root(data_dir, pin["version"])
    existing = installed(root, pin["sha256"])
    if existing is not None:
        return existing
    if root.exists():
        raise ProvisionError(
            f"{root} exists but does not hold the pinned, verified Bazel; inspect and remove it"
        )
    root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root.parent, prefix=".download-") as scratch:
        staged = Path(scratch) / root.name
        (staged / "bin").mkdir(parents=True)
        binary = staged / "bin" / "bazel"
        fetch(pin["url"], binary)
        actual = _sha256(binary)
        if actual != pin["sha256"]:
            raise ProvisionError(
                f"downloaded Bazel has SHA-256 {actual}, but toolchains.lock.json pins "
                f"{pin['sha256']}; nothing was installed"
            )
        (staged / VERIFIED_MARKER).write_text(pin["sha256"] + "\n", encoding="utf-8")
        _make_read_only(binary, 0o555)
        _make_read_only(staged / VERIFIED_MARKER, 0o444)
        _make_read_only(staged / "bin", 0o555)
        # Publish atomically: a reader sees either nothing or the verified tree.
        # Moving a directory across parents needs write access to it, so the
        # root becomes read-only only once it is in place.
        os.rename(staged, root)
    _make_read_only(root, 0o555)
    return root / "bin" / "bazel"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--data-dir", type=Path, default=Path("~/.agent-queue"),
        help="AQ data directory (default: ~/.agent-queue)",
    )
    args = parser.parse_args(argv)
    try:
        executable = provision(args.data_dir.expanduser().resolve())
    except ProvisionError as error:
        print(f"provision: {error}", file=sys.stderr)
        return 1
    print(executable)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
