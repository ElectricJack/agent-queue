"""Disposable PG lifecycle inside H2's isolated target (one cluster per shard).

B1 calls run_postgres_target instead of run_isolated for PG consumers. The
wrapper enters the same network/PID/filesystem boundary *before* initdb. Every
binary/library is an H1 runfile; there is no PATH/pg_config discovery. Standalone
postgres_cluster is also usable by disposable E2E fixtures inside that boundary.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import signal
import socket
import sys
import tempfile
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlencode

from tools.bazel.test_environment import IsolationError, TestEnvironment, run_isolated


@dataclass(frozen=True)
class PostgresBinaries:
    initdb: Path
    postgres: Path
    pg_ctl: Path
    psql: Path

    def validate(self, environment: TestEnvironment) -> None:
        for binary in (self.initdb, self.postgres, self.pg_ctl, self.psql):
            if (not binary.is_absolute() or not binary.is_file()
                    or not os.access(binary, os.X_OK)
                    or not any(binary.is_relative_to(root)
                               for root in environment.runtime.read_only)):
                raise IsolationError("PostgreSQL binary missing from declared runtime inputs")


@dataclass(frozen=True)
class Cluster:
    root: Path
    socket_dir: Path
    port: int
    dsn: str
    process: asyncio.subprocess.Process
    environ: dict[str, str]


async def _stop_group(process: asyncio.subprocess.Process, grace: float) -> None:
    # postgres and initdb are session leaders; include children on failed startup
    # and backends still attached at shutdown. PID 1 in H2 is the final kill fence.
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), grace)
    except TimeoutError:
        pass
    finally:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await asyncio.wait_for(process.wait(), grace)


async def _command(binary: Path, *args: str, env: dict[str, str], log, timeout: float) -> None:
    process = await asyncio.create_subprocess_exec(
        str(binary), *args, env=env, stdin=asyncio.subprocess.DEVNULL,
        stdout=log, stderr=log, start_new_session=True,
    )
    try:
        result = await asyncio.wait_for(process.wait(), timeout)
        if result:
            raise IsolationError(f"{binary.name} failed ({result}); see PostgreSQL target log")
    finally:
        await _stop_group(process, 1)


@asynccontextmanager
async def postgres_cluster(
    environment: TestEnvironment,
    binaries: PostgresBinaries,
    *,
    tcp: bool = False,
    startup_timeout: float = 30,
    grace: float = 2,
    socket_alias_root: Path | None = None,
    python_paths: tuple[Path, ...] = (),
):
    """Create a new cluster, yield its child-only DSN, and bound all teardown.

    Caller must be inside H2 for hermetic execution. tcp reserves a loopback
    port through initdb, releasing it immediately before postgres binds it; any
    bind collision fails closed. No TCP listener exists by default. A short
    socket alias may live in the namespace's private /tmp: the socket itself
    remains under TEST_TMPDIR, even when Bazel's path exceeds sockaddr_un.
    """
    binaries.validate(environment)
    if any(not path.is_absolute() or not any(path.is_relative_to(root)
           for root in environment.runtime.read_only) for path in python_paths):
        raise IsolationError("Python dependency path is outside declared runtime inputs")
    if any(not math.isfinite(n) or n <= 0 for n in (startup_timeout, grace)):
        raise IsolationError("PostgreSQL startup and teardown budgets must be positive and finite")
    with tempfile.TemporaryDirectory(prefix="pg-", dir=environment.root) as temporary:
        root = Path(temporary)
        data = root / "data"
        sockets = root / "s"
        sockets.mkdir(mode=0o700)
        alias = None
        if len(os.fsencode(sockets)) > 80:
            if socket_alias_root is None:
                raise IsolationError("PostgreSQL socket path needs a short private socket alias root")
            alias = socket_alias_root / ("pg-" + uuid.uuid4().hex[:12])
            alias.symlink_to(sockets, target_is_directory=True)
            sockets = alias
        env = environment.child_env()
        process = None
        reservation = socket.socket()
        try:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
            # Unique directory + port even for Unix-only targets; no shared socket.
            with (root / "postgres.log").open("wb") as log:
                async with asyncio.timeout(startup_timeout):
                    await _command(
                        binaries.initdb, "-D", str(data), "-U", "aq_test", "--auth=trust",
                        "--encoding=UTF8", "--no-locale", "--no-sync",
                        env=env, log=log, timeout=startup_timeout,
                    )
                    if tcp:
                        reservation.close()
                    process = await asyncio.create_subprocess_exec(
                        str(binaries.postgres), "-D", str(data), "-k", str(sockets),
                        "-p", str(port), "-h", "127.0.0.1" if tcp else "",
                        "-c", "unix_socket_permissions=0700", "-c", "fsync=off",
                        env=env, stdin=asyncio.subprocess.DEVNULL,
                        stdout=log, stderr=log, start_new_session=True,
                    )
                    # postmaster.pid publishes 'ready' after startup recovery. Do
                    # not confuse a socket appearing with a database accepting SQL.
                    while True:
                        if process.returncode is not None:
                            raise IsolationError("PostgreSQL exited during startup; see target log")
                        try:
                            ready = (data / "postmaster.pid").read_text().splitlines()[7].strip()
                        except (OSError, IndexError):
                            ready = ""
                        if ready == "ready":
                            break
                        await asyncio.sleep(.02)
                    database = "aq_target_" + uuid.uuid4().hex[:16]
                    await _command(
                        binaries.psql, "-X", "-v", "ON_ERROR_STOP=1", "-h", str(sockets),
                        "-p", str(port), "-U", "aq_test", "-d", "postgres",
                        "-c", f'CREATE DATABASE "{database}"',
                        env=env, log=log, timeout=startup_timeout,
                    )
                query = urlencode({"host": str(sockets), "port": port})
                dsn = f"postgresql+asyncpg://aq_test@/{database}?{query}"
                manifest = root / "environment.json"
                manifest.write_text(json.dumps({
                    "root": str(environment.root), "runtime": asdict(environment.runtime),
                    "env": environment.child_env(), "python_paths": python_paths,
                    "socket_alias_root": socket_alias_root,
                    "postgres_dsn": dsn,
                    "tcp_dsn": f"postgresql+asyncpg://aq_test@127.0.0.1:{port}/{database}"
                    if tcp else None,
                }, default=str))
                env.update({"POSTGRES_TEST_DSN": dsn, "AQ_TEST_PG_MODE": "postgres",
                            "AQ_REQUIRE_POSTGRES_TESTS": "1", "AQ_DB_SCOPE": "test",
                            "AQ_TEST_ENVIRONMENT": str(manifest)})
                yield Cluster(root, sockets, port, dsn, process, env)
        except BaseException:
            # The private tree is removed even on startup failure. Preserve a
            # bounded diagnostic in the target output before deleting its log.
            if (root / "postgres.log").exists():
                with (root / "postgres.log").open("rb") as failed_log:
                    failed_log.seek(0, 2)
                    failed_log.seek(max(0, failed_log.tell() - 8192))
                    print(failed_log.read().decode(errors="replace"), file=sys.stderr)
            raise
        finally:
            reservation.close()
            if process is not None:
                await _stop_group(process, grace)
            if alias is not None:
                alias.unlink(missing_ok=True)


async def run_postgres_target(
    environment: TestEnvironment,
    binaries: PostgresBinaries,
    command: list[str],
    *,
    cwd: Path,
    tcp: bool = False,
    timeout: float = 290,
    grace: float = 2,
    python_paths: tuple[Path, ...] = (),
) -> int:
    """H2 entry point: cluster and test process share one private namespace.

    All migrations/alembic.ini must be supplied by the PG target's declared
    data closure (//:postgres_test_migrations); templates build inside this
    cluster through the unchanged ownership/LeasePool machinery.
    """
    binaries.validate(environment)
    if not command or not Path(command[0]).is_absolute():
        raise IsolationError("PG target executable must be a declared absolute path")
    if not any(Path(command[0]).is_relative_to(root)
               for root in (*environment.runtime.read_only, *environment.writable)):
        raise IsolationError("PG target executable must be a declared absolute path")
    plan = environment.root / ("pg-plan-" + uuid.uuid4().hex + ".json")
    plan.write_text(json.dumps({
        "root": str(environment.root), "env": environment.child_env(),
        "binaries": {key: str(value) for key, value in vars(binaries).items()},
        "read_only": list(map(str, environment.runtime.read_only)),
        "python": str(environment.runtime.python),
        "bin_dirs": list(map(str, environment.runtime.bin_dirs)),
        "library_dirs": list(map(str, environment.runtime.library_dirs)),
        "git_exec_path": str(environment.runtime.git_exec_path) if environment.runtime.git_exec_path
        else None,
        "shell": str(environment.runtime.shell) if environment.runtime.shell else None,
        "env_executable": str(environment.runtime.env_executable)
        if environment.runtime.env_executable else None,
        "python_paths": list(map(str, python_paths)),
        "command": command, "tcp": tcp, "grace": grace,
    }))
    try:
        # -c supplies a declared import root without ambient PYTHONPATH. The H2
        # child is already inside the namespace before importing this module.
        bootstrap = (
            f"import sys; sys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r}); "
            "from tools.bazel.postgres_cluster import main; sys.exit(main())"
        )
        return await run_isolated(environment, [str(environment.runtime.python), "-I", "-c",
                                               bootstrap, str(plan)],
                                  cwd=cwd, timeout=timeout, grace=grace)
    finally:
        plan.unlink(missing_ok=True)


async def _target(plan: dict) -> int:
    from tools.bazel.test_environment import Runtime

    environment = TestEnvironment(
        Path(plan["root"]), Runtime(Path(plan["python"]), tuple(map(Path, plan["read_only"])),
                                    tuple(map(Path, plan["bin_dirs"])),
                                    tuple(map(Path, plan["library_dirs"])),
                                    Path(plan["git_exec_path"]) if plan["git_exec_path"] else None,
                                    Path(plan["shell"]) if plan["shell"] else None,
                                    Path(plan["env_executable"]) if plan["env_executable"] else None),
        plan["env"], (Path(plan["root"]),),
    )
    binaries = PostgresBinaries(**{key: Path(value) for key, value in plan["binaries"].items()})
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, task.cancel)
    async with postgres_cluster(environment, binaries, tcp=plan["tcp"], grace=plan["grace"],
                                socket_alias_root=Path("/tmp"),
                                python_paths=tuple(map(Path, plan["python_paths"]))) as cluster:
        process = await asyncio.create_subprocess_exec(
            *plan["command"], env=cluster.environ, start_new_session=True,
        )
        try:
            return await process.wait()
        finally:
            await _stop_group(process, plan["grace"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    args = parser.parse_args()
    try:
        return asyncio.run(_target(json.loads(args.plan.read_text())))
    except asyncio.CancelledError:
        return 143
    except (IsolationError, OSError, TimeoutError) as exc:
        print(f"PostgreSQL target failed ({type(exc).__name__})", file=sys.stderr)
        return 125


if __name__ == "__main__":
    sys.exit(main())
