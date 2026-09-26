"""Detached supervisor. No database, daemon connection or session credentials.

This process owns admission locks, pipes, deadlines and fsync receipts. A
completion receipt is written only after marked descendants have stopped.
Launch intent and an instance lock prohibit replay of ambiguous execution.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import sys
import time
from pathlib import Path
from src.jobs.artifacts import OutputStore, atomic_json, read_json, open_artifact
from src.jobs.identity import identity, processes, stop_tree
from src.jobs.result import build_result, report_json
from src.integration.development_result_parser import PytestOutputParser, parse_junit
from src.resources.box_lock import BoxLock, IncompatibleLockClient
from src.resources.semaphore import SlotTimeout
from src.git.manager import GitManager


async def fingerprint(cwd: Path, *, tracked_only=False) -> tuple[str | None, str | None]:
    import hashlib

    manager = GitManager()
    try:
        head = (await manager._arun(["rev-parse", "HEAD"], cwd=str(cwd))).strip()
        diff = await manager._arun(["diff", "--binary", "HEAD"], cwd=str(cwd))
        names = "" if tracked_only else await manager._arun(
            ["ls-files", "--others", "--exclude-standard", "-z"], cwd=str(cwd)
        )

        def digest():
            h = hashlib.sha256(diff.encode())
            for name in sorted(filter(None, names.split("\0"))):
                path = cwd / name
                h.update(name.encode())
                fd = open_artifact(path, os.O_RDONLY)
                with os.fdopen(fd, "rb") as stream:
                    while chunk := stream.read(65536):
                        h.update(chunk)
            return h.hexdigest()

        return head, await asyncio.to_thread(digest)
    except Exception:  # Git errors leave explicitly unverified inputs
        return None, None


async def supervise(directory: Path) -> None:
    fd = open_artifact(directory / "instance.lock", os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return
    try:
        # A second supervisor never executes the same intent, even if the
        # original was killed before its started receipt appeared.
        if read_json(directory / "intent.json"):
            return
        job = read_json(directory / "request.json")
        if job["contract"].get("nice", 0):
            os.nice(job["contract"]["nice"])
        nonce = job["runner_nonce"]
        os.environ["AQ_JOB_NONCE"] = nonce
        os.environ["AQ_JOB_ID"] = job["id"]
        intent = {"job_id": job["id"], "nonce": nonce, **await identity()}
        await asyncio.to_thread(atomic_json, directory / "intent.json", intent)
        box = BoxLock(job["contract"]["lock_dir"], job["contract"]["capacity"])
        started, exit_code, infra, cancelled, output_failed = None, None, None, False, False
        parser = PytestOutputParser()
        store = None
        initial = bytearray()
        before = after = (None, None)
        cm = None
        child = None

        def waiting(*_):
            if (directory / "cancel.json").exists():
                raise InterruptedError("job cancelled while queued for capacity")

        try:
            cm = box.acquire(
                exclusive=job["job_class"] == "exclusive",
                weight=job["weight"],
                timeout=max(0, job["queue_deadline"] - time.time()),
                poll=0.1,
                meta={"job_id": job["id"], "job_nonce": nonce, "task_id": job["task_id"]},
                on_wait=waiting,
            )
            await asyncio.to_thread(cm.__enter__)
            if (directory / "cancel.json").exists():
                raise InterruptedError()
            started = time.time()
            before = await asyncio.wait_for(
                fingerprint(
                    Path(job["contract"]["cwd"]), tracked_only=job["input_mode"] == "snapshot"
                ),
                min(30, job["run_timeout"]),
            )
            if job["input_mode"] == "snapshot":
                import hashlib

                if before != (job["input_ref"], hashlib.sha256(b"").hexdigest()):
                    infra = "snapshot_modified"
                    raise ValueError("snapshot changed before execution")
            store = await asyncio.to_thread(
                OutputStore,
                directory,
                head_bytes=job["contract"].get("head_bytes", 1024**2),
                tail_bytes=job["contract"].get("tail_bytes", 63 * 1024**2),
            )
            argv = list(job["argv"])
            junit_path = None
            if job["contract"].get("pytest"):
                # Respect caller flags; artifact parsing refuses symlinks
                # along caller paths inside the pinned workspace.
                for index, arg in enumerate(argv):
                    if arg.startswith(("--junitxml=", "--junit-xml=")):
                        junit_path = Path(job["contract"]["cwd"]) / arg.split("=", 1)[1]
                        break
                    if arg in {"--junitxml", "--junit-xml"} and index + 1 < len(argv):
                        junit_path = Path(job["contract"]["cwd"]) / argv[index + 1]
                        break
                if junit_path is None:
                    junit_path = directory / "junit.xml"
                    argv.append(f"--junitxml={junit_path}")
            inherited = []
            for raw in os.listdir("/proc/self/fd"):
                try:
                    candidate = int(raw)
                    if candidate > 2 and os.get_inheritable(candidate):
                        inherited.append(candidate)
                except OSError:
                    continue
            child = await asyncio.create_subprocess_exec(
                *argv,
                cwd=job["contract"]["cwd"],
                env=job["contract"]["env"],
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
                pass_fds=tuple(inherited),
            )
            receipt = {
                **intent,
                "child_pid": child.pid,
                "started_at": started,
                "input_ref": before[0],
                "input_fingerprint": before[1],
            }
            await asyncio.to_thread(atomic_json, directory / "started.json", receipt)

            async def pump():
                nonlocal output_failed
                while chunk := await child.stdout.read(65536):
                    parser.feed(chunk)
                    if len(initial) < 1024:
                        initial.extend(chunk[: 1024 - len(initial)])
                    try:
                        await asyncio.to_thread(store.append, chunk)
                    except OSError:
                        output_failed = True
                        await stop_tree(nonce)
                        raise

            pumping = asyncio.create_task(pump())
            deadline = time.monotonic() + max(0, job["run_timeout"] - (time.time() - started))
            wait_child = asyncio.create_task(child.wait())
            try:
                while not wait_child.done():
                    if (directory / "cancel.json").exists():
                        cancelled = True
                        await stop_tree(nonce)
                    elif time.monotonic() >= deadline:
                        infra = "run_timeout"
                        await stop_tree(nonce)
                    await asyncio.wait({wait_child}, timeout=0.1)
                exit_code = await wait_child
                # Descendants may retain pipes and locks after the leader exits.
                # Finite presets never leave those descendants alive.
                if [p for p in await processes(nonce) if p.pid != os.getpid()]:
                    await stop_tree(nonce)
                await pumping
            finally:
                if not pumping.done():
                    pumping.cancel()
                    await asyncio.gather(pumping, return_exceptions=True)
            try:
                after = await asyncio.wait_for(
                    fingerprint(
                        Path(job["contract"]["cwd"]), tracked_only=job["input_mode"] == "snapshot"
                    ),
                    30,
                )
            except TimeoutError:
                after = (None, None)
            report = parser.finish()
            if junit_path:

                def parse():
                    try:
                        base = (
                            directory
                            if junit_path == directory / "junit.xml"
                            else Path(job["contract"]["cwd"])
                        )
                        relative = junit_path.relative_to(base)
                        candidate = base
                        for part in relative.parts:
                            candidate /= part
                            if candidate.is_symlink():
                                raise OSError("symlink JUnit artifact")
                        artifact = open_artifact(junit_path, os.O_RDONLY)
                        with os.fdopen(artifact, "rb") as stream:
                            return parse_junit(iter(lambda: stream.read(65536), b""))
                    except OSError:
                        return parse_junit(None)

                junit_report = await asyncio.to_thread(parse)
                from dataclasses import replace

                report = replace(
                    junit_report,
                    infrastructure_error=report.infrastructure_error,
                    failing=junit_report.failing or report.failing,
                    omitted_failure_count=max(
                        report.omitted_failure_count, junit_report.omitted_failure_count
                    ),
                )
        except InterruptedError:
            cancelled = True
            report = parser.finish()
        except TimeoutError:
            infra = "run_timeout"
            report = parser.finish()
        except SlotTimeout:
            infra = "queue_timeout"
            report = parser.finish()
        except IncompatibleLockClient:
            infra = "incompatible_lock_client"
            report = parser.finish()
        except Exception:  # receipts must distinguish supervisor failure from assertions
            infra = infra or ("output_store_failed" if output_failed else "runner_failed")
            report = parser.finish()
            if child:
                await stop_tree(nonce)
        finally:
            # Pin/locks are held until this check; never certify cleanup on /proc
            # uncertainty. A killed supervisor writes no receipt at all.
            remaining = [p for p in await processes(nonce) if p.pid != os.getpid()]
            if remaining:
                return
            completion = {
                **intent,
                "exit_code": exit_code,
                "signal": -exit_code if exit_code is not None and exit_code < 0 else None,
                "ended_at": time.time(),
                "queue_seconds": (started or time.time()) - job["submitted_at"],
                "run_seconds": time.time() - started if started else 0,
                "infra_reason": infra,
                "cancelled": cancelled,
                "output_store_failed": output_failed,
                "report": report_json(report),
                "input_ref": before[0],
                "input_fingerprint": before[1],
                "end_fingerprint": after[1],
                "input_stability": "unverified"
                if job["input_mode"] == "live"
                else ("stable" if before[1] and before == after else "unverified"),
                "initial_errors": initial.decode("utf-8", "replace")
                if report.infrastructure_error
                else "",
                **(store.stats() if store else {}),
            }
            tail = b""
            if store:
                read = await asyncio.to_thread(
                    store.read, max(0, store.manifest["seen"] - 8192), 8192
                )
                tail = b"".join(c["data"] for c in read["chunks"])
            await asyncio.to_thread(atomic_json, directory / "completion.json", completion)
            await asyncio.to_thread(
                atomic_json, directory / "result.json", build_result(job, completion, tail)
            )
            if store:
                store.close()
            if cm:
                await asyncio.to_thread(cm.__exit__, None, None, None)
    finally:
        os.close(fd)


if __name__ == "__main__":
    asyncio.run(supervise(Path(sys.argv[1])))
