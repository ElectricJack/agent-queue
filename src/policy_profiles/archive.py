"""Bounded directory/zip IO. Only manifest-declared item JSON is ever read."""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

from src.policy_profiles.models import Bundle, encoded

MAX_BYTES = 32 * 1024 * 1024


def files(bundle: Bundle) -> dict[str, bytes]:
    manifest = bundle.model_dump(mode="json")
    result = {}
    for number, item in enumerate(manifest["items"]):
        name = f"items/{number:04d}.json"
        result[name] = encoded(item.pop("payload"))
        item["file"] = name
    result["manifest.json"] = encoded(manifest)
    if sum(len(data) for data in result.values()) > MAX_BYTES:
        raise ValueError("policy exceeds size limit")
    return result


def pack(bundle: Bundle) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in files(bundle).items():
            archive.writestr(name, data)
    return stream.getvalue()


def _read_bundle(path: str | bytes) -> Bundle:
    root = Path(path) if isinstance(path, str) else None
    if (
        isinstance(path, bytes)
        and len(path) > MAX_BYTES
        or root is not None
        and root.is_file()
        and root.stat().st_size > MAX_BYTES
    ):
        raise ValueError("policy archive exceeds size limit")
    archive = (
        zipfile.ZipFile(io.BytesIO(path))
        if isinstance(path, bytes)
        else zipfile.ZipFile(root)
        if root.is_file()
        else None
    )
    try:
        if archive:
            entries = archive.infolist()
            if len(entries) > 1001 or sum(entry.file_size for entry in entries) > MAX_BYTES:
                raise ValueError("policy archive exceeds size limit")
            if len({entry.filename for entry in entries}) != len(entries):
                raise ValueError("duplicate archive entries")

        def read(name: str) -> bytes:
            if name != "manifest.json" and not (
                len(name) == len("items/0000.json")
                and name.startswith("items/")
                and name[6:10].isdigit()
                and name.endswith(".json")
            ):
                raise ValueError("invalid policy item file")
            if archive:
                return archive.read(name)
            target = root / name
            if (
                target.is_symlink()
                or root.is_symlink()
                or target.resolve() != root.resolve() / name
            ):
                raise ValueError("policy files must not be symlinks")
            if target.stat().st_size > MAX_BYTES:
                raise ValueError("policy file exceeds size limit")
            return target.read_bytes()

        document = json.loads(read("manifest.json"))
        if (
            not isinstance(document, dict)
            or not isinstance(document.get("items"), list)
            or len(document["items"]) > 1000
        ):
            raise ValueError("invalid policy manifest")
        total = 0
        names = set()
        for item in document["items"]:
            name = item.pop("file")
            if name in names:
                raise ValueError("duplicate item file")
            names.add(name)
            data = read(name)
            total += len(data)
            if total > MAX_BYTES:
                raise ValueError("policy exceeds size limit")
            item["payload"] = json.loads(data)
        return Bundle.model_validate(document)
    finally:
        if archive:
            archive.close()


def read_bundle(path: str | bytes) -> Bundle:
    try:
        return _read_bundle(path)
    except (
        zipfile.BadZipFile,
        KeyError,
        TypeError,
        AttributeError,
        UnicodeError,
        RuntimeError,
    ) as exc:
        raise ValueError(f"invalid policy archive: {exc}") from exc


def write_bundle(path: str, bundle: Bundle) -> list[str]:
    """Create only, publish a completed directory or zip without replacing one."""
    destination = Path(path).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("export destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    content = files(bundle)
    staging = Path(tempfile.mkdtemp(prefix=".aqpolicy-", dir=destination.parent))
    try:
        if destination.suffix in {".zip", ".aqpolicy"}:
            temp = staging / "profile.zip"
            with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name, data in content.items():
                    archive.writestr(name, data)
            os.link(temp, destination)  # create-only even if the target races
        else:
            for name, data in content.items():
                target = staging / name
                target.parent.mkdir(exist_ok=True)
                target.write_bytes(data)
            # mkdir reserves the target so rename cannot replace an existing
            # empty directory. Individual files remain create-only.
            destination.mkdir()
            try:
                for name, data in content.items():
                    target = destination / name
                    target.parent.mkdir(exist_ok=True)
                    with target.open("xb") as stream:
                        stream.write(data)
            except BaseException:
                shutil.rmtree(destination)
                raise
    finally:
        shutil.rmtree(staging)
    return sorted(content)
