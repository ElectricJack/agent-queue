"""Finite capture admission, native ownership and retained capture evidence."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path

from src.jobs.artifacts import atomic_json, open_artifact, read_json
from src.jobs.policy import JobError, Preset


def file_hash(path: str | Path) -> str:
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def capture_inputs(args: list[str], cwd: Path) -> tuple[str, dict]:
    if not isinstance(args, list) or len(args) != 1 or not isinstance(args[0], str):
        raise JobError("jobs.preset_denied")
    value = args[0]
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or "\0" in value or "\\" in value:
        raise JobError("jobs.cwd_invalid")
    try:
        resolved = (cwd / path).resolve(strict=True)
        resolved.relative_to(cwd)
        candidate_path = resolved / "candidate.json"
        candidate_path.resolve(strict=True).relative_to(cwd)
        if candidate_path.stat().st_size > 1024**2:
            raise ValueError("candidate manifest too large")
        candidate = read_json(candidate_path)
        if candidate.get("version") != 1:
            raise ValueError("unsupported candidate")
        views = candidate["manifest"]["rig"]["views"]
        names = [v["name"] for v in views]
        if not names or len(names) != len(set(names)):
            raise ValueError("invalid view set")
        return str(resolved), {
            "candidate_sha256": candidate["candidate_sha256"],
            "rig_sha256": candidate["rig_sha256"],
            "views": names,
        }
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise JobError("jobs.cwd_invalid") from exc


def preset(settings) -> Preset:
    paths = [settings.matter_python, settings.matter_capture_script, settings.matter_editor]
    if any(
        not value or not Path(value).is_absolute() or not Path(value).is_file() for value in paths
    ):
        raise JobError("jobs.matter_unconfigured")
    if settings.matter_editor.lower().endswith(
        ".exe"
    ) and not settings.matter_python.lower().endswith(".exe"):
        raise JobError("jobs.native_python_required")
    return Preset(
        "matter_render",
        (
            settings.matter_python,
            str(Path(__file__).with_name("matter_capture.py")),
            settings.matter_capture_script,
        ),
        job_class="exclusive",
    )


async def windows_path(path: str | Path) -> str:
    proc = await asyncio.create_subprocess_exec(
        "/usr/bin/wslpath",
        "-w",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), 10)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    if proc.returncode:
        raise JobError("jobs.native_path_unavailable")
    return stdout.decode().strip()


async def native_request(job: dict, directory: Path, argv: list[str], started: float) -> list[str]:
    native = job["contract"]["native_windows"]
    converted = [
        await windows_path(value) if Path(value).is_absolute() else value for value in argv
    ]
    request = {
        "job_id": job["id"],
        "nonce": job["runner_nonce"],
        "gpu_id": job["contract"]["gpu_id"],
        "argv": converted,
        "cwd": await windows_path(job["contract"]["cwd"]),
        "env": {k: v for k, v in job["contract"]["env"].items() if k not in {"PATH", "HOME"}},
        "queue_deadline": job["queue_deadline"],
        "run_deadline": started + job["run_timeout"],
    }
    await asyncio.to_thread(atomic_json, directory / "native-request.json", request)
    return [
        native["python"],
        native["owner_script"],
        "run",
        await windows_path(directory / "native-request.json"),
    ]


async def native_status(job: dict, directory: Path) -> dict:
    native = job["contract"]["native_windows"]
    proc = await asyncio.create_subprocess_exec(
        native["python"],
        native["owner_script"],
        "probe",
        await windows_path(directory),
        job["runner_nonce"],
        # A read-only probe must not appear to the runner as a surviving job
        # descendant while it is certifying cleanup.
        env={
            k: v
            for k, v in job["contract"].get("env", {}).items()
            if k not in {"AQ_JOB_NONCE", "AQ_JOB_ID"}
        },
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), 15)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    if proc.returncode:
        raise RuntimeError("jobs.cleanup_blocked")
    result = json.loads(stdout)
    if not isinstance(result, dict) or not result.get("known"):
        raise RuntimeError("jobs.cleanup_blocked")
    return result


async def cancel_native(directory: Path):
    await asyncio.to_thread(atomic_json, directory / "cancel.json", {"native_cancel": True})


def retain_capture(directory: Path, max_bytes: int, expected: dict) -> dict:
    """Validate fresh evidence and hash bounded files without following symlinks."""
    capture = directory / "capture"
    if capture.is_symlink() or not capture.is_dir():
        raise ValueError("missing capture artifacts")
    if (capture / "capture.json").stat().st_size > min(max_bytes, 1024**2):
        raise ValueError("capture receipt too large")
    receipt = read_json(capture / "capture.json")
    if (
        not isinstance(receipt, dict)
        or receipt.get("status") != "complete"
        or receipt.get("version") != 1
    ):
        raise ValueError("capture not valid")
    if any(
        receipt.get(k) != expected[k] for k in ("candidate_sha256", "rig_sha256", "editor_sha256")
    ):
        raise ValueError("foreign capture identity")
    if set(receipt.get("views", {})) != set(expected["views"]):
        raise ValueError("incomplete capture coverage")
    files, total = [], 0
    for path in sorted(capture.rglob("*")):
        if path.is_symlink():
            raise ValueError("symlink capture artifact")
        if path.is_dir():
            continue
        if len(files) >= 4096:
            raise ValueError("too many capture artifacts")
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(open_artifact(path, os.O_RDONLY), "rb") as stream:
            while chunk := stream.read(65536):
                total += len(chunk)
                size += len(chunk)
                if total > max_bytes:
                    raise ValueError("capture artifact budget exceeded")
                digest.update(chunk)
        files.append(
            {
                "path": str(path.relative_to(directory)),
                "bytes": size,
                "sha256": digest.hexdigest(),
            }
        )
    hashes = {f["path"]: f["sha256"] for f in files}
    for name, view in receipt["views"].items():
        for key, suffix in (("image", ".png"), ("channels", ".png.channels.bin")):
            if view[key]["sha256"] != hashes.get(f"capture/{name}{suffix}"):
                raise ValueError("missing or changed capture evidence")
    return {"receipt": receipt, "artifacts": files, "bytes": total}
