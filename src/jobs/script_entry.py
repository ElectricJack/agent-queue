"""Private pre-exec shim, installed root-owned beside the launcher.

Only the launcher's anonymous descriptor supplies this request. This is not an
operator entry point and must never appear in a sudo rule. No imports from a
checkout, no user program, and no descendants exist before cgroup enrollment.
"""

from __future__ import annotations

import ctypes
import json
import os
import resource
import sys


def main() -> None:
    fd = int(sys.argv[1])
    with os.fdopen(fd, "rb") as stream:
        request = json.loads(stream.read(1024 * 1024))
    # The parent has installed every cgroup limit before creating this child.
    # Enrollment before exec prevents the fork/move race of migrating bwrap's
    # PID after it has already created its namespace helper and target.
    with open(request["cgroup"] + "/cgroup.procs", "w") as stream:
        stream.write(str(os.getpid()))
    resource.setrlimit(resource.RLIMIT_NOFILE, (request["fds"], request["fds"]))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (request["scratch"], request["scratch"]))
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        raise OSError("no_new_privileges_unavailable")
    # Never carry a launcher lock, key, credential pipe or control FD into bwrap.
    for raw in os.listdir("/proc/self/fd"):
        descriptor = int(raw)
        if descriptor > 2 and descriptor != request["status_fd"]:
            try:
                os.close(descriptor)
            except OSError:
                pass
    os.execve(request["argv"][0], request["argv"], request["env"])


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Never print the request or exception (it may contain dynamic inputs).
        os.write(2, b"sandbox_entry_failed\n")
        sys.exit(125)
