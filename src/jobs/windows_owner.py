"""Standalone Windows tree owner, invoked only with a server-written request.

No AQ imports: native Python can execute this file from a WSL checkout. The
named Job Object owns descendants; a named device mutex fences other authors.
Processes are created suspended and assigned before their first instruction.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes as w
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid


def atomic(path: Path, value: dict):
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class Windows:
    def __init__(self):
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateMutexW": (w.HANDLE, [w.LPVOID, w.BOOL, w.LPCWSTR]),
            "ReleaseMutex": (w.BOOL, [w.HANDLE]),
            "CreateJobObjectW": (w.HANDLE, [w.LPVOID, w.LPCWSTR]),
            "OpenJobObjectW": (w.HANDLE, [w.DWORD, w.BOOL, w.LPCWSTR]),
            "SetInformationJobObject": (w.BOOL, [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD]),
            "QueryInformationJobObject": (
                w.BOOL,
                [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD, w.LPVOID],
            ),
            "AssignProcessToJobObject": (w.BOOL, [w.HANDLE, w.HANDLE]),
            "TerminateJobObject": (w.BOOL, [w.HANDLE, w.UINT]),
            "TerminateProcess": (w.BOOL, [w.HANDLE, w.UINT]),
            "OpenProcess": (w.HANDLE, [w.DWORD, w.BOOL, w.DWORD]),
            "GetProcessTimes": (w.BOOL, [w.HANDLE, w.LPVOID, w.LPVOID, w.LPVOID, w.LPVOID]),
            "GetCurrentProcess": (w.HANDLE, []),
            "GetCurrentProcessId": (w.DWORD, []),
            "GetStdHandle": (w.HANDLE, [w.DWORD]),
            "WaitForSingleObject": (w.DWORD, [w.HANDLE, w.DWORD]),
            "ResumeThread": (w.DWORD, [w.HANDLE]),
            "GetExitCodeProcess": (w.BOOL, [w.HANDLE, w.LPVOID]),
            "CloseHandle": (w.BOOL, [w.HANDLE]),
        }
        for name, (result, arguments) in signatures.items():
            fn = getattr(self.api, name)
            fn.restype, fn.argtypes = result, arguments

    def checked(self, value):
        if not value:
            raise ctypes.WinError(ctypes.get_last_error())
        return value

    def close(self, handle):
        if handle:
            self.checked(self.api.CloseHandle(handle))

    def creation(self, handle):
        times = [w.FILETIME() for _ in range(4)]
        self.checked(self.api.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)))
        return times[0].dwHighDateTime << 32 | times[0].dwLowDateTime

    def active(self, job):
        class Accounting(ctypes.Structure):
            _fields_ = [
                ("times", ctypes.c_longlong * 4),
                ("faults", w.DWORD),
                ("total", w.DWORD),
                ("active", w.DWORD),
                ("terminated", w.DWORD),
            ]

        info = Accounting()
        self.checked(
            self.api.QueryInformationJobObject(
                job, 1, ctypes.byref(info), ctypes.sizeof(info), None
            )
        )
        return info.active

    def job(self, name):
        class Limits(ctypes.Structure):
            _fields_ = [
                ("times", ctypes.c_longlong * 2),
                ("flags", w.DWORD),
                ("min_ws", ctypes.c_size_t),
                ("max_ws", ctypes.c_size_t),
                ("active_limit", w.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority", w.DWORD),
                ("scheduling", w.DWORD),
            ]

        class Extended(ctypes.Structure):
            _fields_ = [
                ("basic", Limits),
                ("io", ctypes.c_ulonglong * 6),
                ("memory", ctypes.c_size_t * 4),
            ]

        handle = self.checked(self.api.CreateJobObjectW(None, name))
        try:
            info = Extended()
            info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no breakaway
            self.checked(
                self.api.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info))
            )
            return handle
        except BaseException:
            self.close(handle)
            raise

    def spawn(self, job, argv, cwd, env):
        class Startup(ctypes.Structure):
            _fields_ = [
                ("cb", w.DWORD),
                ("reserved", w.LPWSTR),
                ("desktop", w.LPWSTR),
                ("title", w.LPWSTR),
                ("position_size", w.DWORD * 7),
                ("flags", w.DWORD),
                ("show", w.WORD),
                ("reserved_size", w.WORD),
                ("reserved_bytes", w.LPVOID),
                ("stdin", w.HANDLE),
                ("stdout", w.HANDLE),
                ("stderr", w.HANDLE),
            ]

        class Process(ctypes.Structure):
            _fields_ = [
                ("process", w.HANDLE),
                ("thread", w.HANDLE),
                ("pid", w.DWORD),
                ("tid", w.DWORD),
            ]

        create = self.api.CreateProcessW
        create.restype = w.BOOL
        create.argtypes = [
            w.LPCWSTR,
            w.LPWSTR,
            w.LPVOID,
            w.LPVOID,
            w.BOOL,
            w.DWORD,
            w.LPVOID,
            w.LPCWSTR,
            w.LPVOID,
            w.LPVOID,
        ]
        startup, process = Startup(), Process()
        startup.cb, startup.flags = ctypes.sizeof(startup), 0x100
        startup.stdin, startup.stdout, startup.stderr = (
            self.api.GetStdHandle(code & 0xFFFFFFFF) for code in (-10, -11, -12)
        )
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
        environment = ctypes.create_unicode_buffer(
            "\0".join(f"{k}={v}" for k, v in sorted(env.items())) + "\0\0"
        )
        self.checked(
            create(
                argv[0],
                command,
                None,
                None,
                True,
                0x4 | 0x400,
                environment,
                cwd,
                ctypes.byref(startup),
                ctypes.byref(process),
            )
        )
        try:
            self.checked(self.api.AssignProcessToJobObject(job, process.process))
            if self.api.ResumeThread(process.thread) == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
            return process.process, process.pid
        except BaseException:
            self.api.TerminateProcess(process.process, 1)
            self.close(process.process)
            raise
        finally:
            self.close(process.thread)


def probe(directory: Path, nonce: str):
    api = Windows()
    path = directory / "native-intent.json"
    if not path.exists():
        return {"known": False}
    receipt = json.loads(path.read_text(encoding="utf-8"))
    if receipt.get("nonce") != nonce:
        raise ValueError("foreign native owner")
    handle = api.api.OpenProcess(0x1000 | 0x100000, False, receipt["pid"])
    alive = False
    if handle:
        try:
            alive = (
                api.creation(handle) == receipt["creation_time"]
                and api.api.WaitForSingleObject(handle, 0) == 0x102
            )
        finally:
            api.close(handle)
    elif ctypes.get_last_error() != 87:  # invalid PID is gone; access denied is unknown
        raise ctypes.WinError(ctypes.get_last_error())
    job = api.api.OpenJobObjectW(4, False, "Local\\aq-job-" + nonce)
    active = 0
    if job:
        try:
            active = api.active(job)
        finally:
            api.close(job)
    elif ctypes.get_last_error() != 2:
        raise ctypes.WinError(ctypes.get_last_error())
    return {"known": True, "owner_alive": alive, "active_processes": active}


def run(request_path: Path):
    request = json.loads(request_path.read_text(encoding="utf-8"))
    directory, nonce = request_path.parent, request["nonce"]
    api = Windows()
    job, mutex, child, held = None, None, None, False
    receipt = {
        "job_id": request["job_id"],
        "nonce": nonce,
        "pid": api.api.GetCurrentProcessId(),
        "creation_time": api.creation(api.api.GetCurrentProcess()),
    }
    # Ambiguous starts are never replayed, including when an owner dies before spawn.
    if (directory / "native-intent.json").exists():
        raise RuntimeError("native launch already attempted")
    atomic(directory / "native-intent.json", receipt)
    code, reason, cancelled = None, None, False
    try:
        mutex = api.checked(
            api.api.CreateMutexW(
                None,
                False,
                "Global\\aq-gpu-" + hashlib.sha256(request["gpu_id"].encode()).hexdigest(),
            )
        )
        while not held:
            if (directory / "cancel.json").exists():
                cancelled = True
                return
            if time.time() >= request["queue_deadline"]:
                reason = "queue_timeout"
                return
            status = api.api.WaitForSingleObject(mutex, 100)
            held = status in (0, 0x80)  # abandoned owner releases its mutex
            if status not in (0, 0x80, 0x102):
                raise ctypes.WinError(ctypes.get_last_error())
        job = api.job("Local\\aq-job-" + nonce)
        env = {k: os.environ[k] for k in ("SystemRoot", "WINDIR", "TEMP", "TMP") if k in os.environ}
        env.update(request["env"])
        child, pid = api.spawn(job, request["argv"], request["cwd"], env)
        atomic(directory / "native-started.json", {**receipt, "child_pid": pid})
        while api.api.WaitForSingleObject(child, 100) == 0x102:
            if (directory / "cancel.json").exists():
                cancelled = True
                api.checked(api.api.TerminateJobObject(job, 1))
            elif time.time() >= request["run_deadline"]:
                reason = "run_timeout"
                api.checked(api.api.TerminateJobObject(job, 1))
        result = w.DWORD()
        api.checked(api.api.GetExitCodeProcess(child, ctypes.byref(result)))
        code = result.value
        if time.time() >= request["run_deadline"]:
            reason = "run_timeout"
    except BaseException:
        reason = reason or "native_owner_failed"
        raise
    finally:
        if job:
            api.checked(api.api.TerminateJobObject(job, 1))
            while api.active(job):
                time.sleep(0.02)
        api.close(child)
        api.close(job)
        atomic(
            directory / "native-completion.json",
            {
                **receipt,
                "exit_code": code,
                "infra_reason": reason,
                "cancelled": cancelled,
            },
        )
        if held:
            api.checked(api.api.ReleaseMutex(mutex))
        api.close(mutex)


if __name__ == "__main__":
    if os.name != "nt":
        raise SystemExit("native Windows Python required")
    if sys.argv[1] == "run":
        run(Path(sys.argv[2]))
    elif sys.argv[1] == "probe":
        print(json.dumps(probe(Path(sys.argv[2]), sys.argv[3])))
    else:
        raise SystemExit("unsupported native owner operation")
