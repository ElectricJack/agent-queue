"""Opt-in POSIX test launcher: one preloaded CLI, a fresh process per invocation.

No handlers are called here. Children run the real Click entry point, REST
transport, environment and exit contract. Plugin startup probes bypass this
launcher. The private Unix socket and server exist only in the owned test home.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
from pathlib import Path


def invoke(argv, env):
    from src.cli.app import main

    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        sys.stdout.flush()
        sys.stderr.flush()
        pid = os.fork()
        if pid == 0:
            os.dup2(stdout.fileno(), 1)
            os.dup2(stderr.fileno(), 2)
            os.environ.clear()
            os.environ.update(env)
            sys.argv = ["aq", *argv]
            code = 0
            try:
                main()
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else int(exc.code is not None)
            except BaseException:
                import traceback
                traceback.print_exc()
                code = 1
            finally:
                sys.stdout.flush()
                sys.stderr.flush()
            os._exit(code)
        _, status = os.waitpid(pid, 0)
        stdout.seek(0)
        stderr.seek(0)
        return {"returncode": os.waitstatus_to_exitcode(status),
                "stdout": stdout.read().decode(errors="replace"),
                "stderr": stderr.read().decode(errors="replace")}


def request(path, argv, env):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(120)
        client.connect(str(path))
        client.sendall(json.dumps({"argv": argv, "env": env}).encode() + b"\n")
        return json.loads(client.makefile("rb").readline())


def main():
    path = Path(sys.argv[1])
    # Register the full CLI before serving, including operator-configured plugin
    # extensions. Command-specific imports still happen in each child.
    from src.cli.app import main as _main  # noqa: F401

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(path))
        path.chmod(0o600)
        server.listen()
        while True:
            client, _ = server.accept()
            with client:
                payload = json.loads(client.makefile("rb").readline())
                client.sendall(json.dumps(invoke(payload["argv"], payload["env"])).encode() + b"\n")


if __name__ == "__main__":
    main()
