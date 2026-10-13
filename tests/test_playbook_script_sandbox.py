"""Script policy tests and a mandatory (never skipped) real Linux acceptance arm.

The integration fixture consumes an operator-provisioned isolated setup. Missing
prerequisites FAIL that arm; default marker deselection is not acceptance evidence.
No test installs packages, creates users, delegates cgroups or enables the daemon.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import os
import socket
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from src.config import ScriptsConfig
from src.doctor import default_registry
from src.doctor.models import DoctorContext
from src.doctor.script_checks import check_script_sandbox
from src.git.manager import GitError, GitManager
from src.jobs.script_sandbox import (
    LauncherPolicy, SandboxRequest, SandboxResult, _cgroup_files, _read_bounded, _sandbox_argv,
    launch, prerequisites,
    probe_launcher,
)
from src.playbooks.proposal import pin_source_scripts
from src.playbooks.script_inputs import (
    InputStore, PinnedScript, QuotaLedger, RuntimeFile, RuntimeManifest, canonical, digest,
    pin_script_inputs, read_file,
)
from src.playbooks.script_policy import (
    MIB, ScriptError, ScriptLimits, ScriptProgram, script_environment,
)


def program(**kwargs):
    return ScriptProgram(runtime="test", shell='printf "%s" "$1"', interpreter="usr/bin/sh",
                         **kwargs)


def manifest():
    return RuntimeManifest(runtime="test", files=(
        RuntimeFile(path="usr/bin/sh", sha256=digest(b"shell"), byte_count=5, executable=True),
    ))


def pin(p=None):
    p = p or program()
    return PinnedScript(program=p, program_sha256=digest(canonical(p.model_dump(mode="json"))),
                        source_revision="0" * 40, runtime_sha256=manifest().sha256,
                        executable_sha256=digest(b"shell"))


def request(pinned=None, **kwargs):
    return SandboxRequest(execution_id=uuid.uuid4().hex, run_id="test-run",
                          pinned=pinned or pin(), **kwargs)


@pytest.mark.parametrize("body", [
    {"runtime": "test"},
    {"runtime": "test", "script": "one.py", "shell": "echo bad", "interpreter": "usr/bin/sh"},
    {"runtime": "test", "argv": []},
    {"runtime": "test", "argv": [{"type": "literal", "value": "usr/bin/sh"}]},
    {"runtime": "test", "argv": ["usr/bin/sh", "not-a-value"]},
    {"runtime": "test", "argv": ["usr/bin/sh"], "interpreter": "usr/bin/sh"},
    {"runtime": "test", "script": "one.py"},
    {"runtime": "test", "shell": "true", "interpreter": "usr/bin/sh", "network": "host"},
    {"runtime": "test", "shell": "true", "interpreter": "usr/bin/sh", "secrets": ["token"]},
    {"runtime": "test", "shell": "true", "interpreter": "usr/bin/sh", "json_required": True},
])
def test_program_forms_and_authority_are_strict(body):
    with pytest.raises(ValidationError):
        ScriptProgram.model_validate(body)


@pytest.mark.parametrize("path", ["/etc/passwd", "../outside", "x/../y", "./x", "x//y",
                                   "~/code", "x\\y", "x\ny", "{{input}}", "${HOME}", "x/"])
def test_paths_reject_escapes_and_templates(path):
    with pytest.raises(ValidationError):
        ScriptProgram(runtime="test", script=path, interpreter="usr/bin/sh")


def test_frozen_program_and_noncoercing_limits():
    p = program()
    with pytest.raises(ValidationError):
        p.shell = "replacement"
    for values in ({"wall_seconds": True}, {"pids": "32"}, {"wall_seconds": 901}, {"fds": 0}):
        with pytest.raises(ValidationError):
            ScriptLimits(**values)
    with pytest.raises(ScriptError, match="exceed_host"):
        ScriptLimits(wall_seconds=61).narrow(ScriptLimits())
    assert ScriptLimits(wall_seconds=30).narrow(ScriptLimits()).wall_seconds == 30


@pytest.mark.parametrize("name", ["PATH", "HOME", "PYTHONPATH", "BASH_ENV", "ENV", "LD_PRELOAD",
                                  "AQ_API_TOKEN", "PGPASSWORD", "SSH_AUTH_SOCK", "OPENAI_API_KEY",
                                  "MY_SECRET", "NODE_OPTIONS", "XDG_RUNTIME_DIR"])
def test_environment_never_inherits_or_allows_credentials(name, monkeypatch):
    monkeypatch.setenv(name, "must-not-leak")
    with pytest.raises(ScriptError, match="environment_denied"):
        script_environment((name,), (name,), {name: "must-not-leak"})
    assert "must-not-leak" not in str(script_environment((), (), {}))


def test_environment_is_explicit_intersection_and_sandbox_home():
    assert script_environment(("LABEL",), ("LABEL",), {"LABEL": "a;b$(id)"}) == {
        "PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "HOME": "/scratch",
        "LABEL": "a;b$(id)",
    }
    with pytest.raises(ScriptError):
        script_environment(("LABEL",), (), {"LABEL": "value"})


def test_shell_data_is_a_separate_argv_element(tmp_path):
    text = '$(touch /scratch/injected); `id`; "quoted"'
    req = request(arguments=(text,))
    argv = _sandbox_argv(ScriptsConfig(runtime_root="/immutable/runtime"), req, tmp_path)
    assert argv[-5:] == ["/usr/bin/sh", "-c", req.pinned.program.shell, "aq-script", text]
    assert "--unshare-net" in argv and "--cap-drop" in argv
    assert "--disable-userns" in argv and "--assert-userns-disabled" in argv
    assert argv[argv.index("--size") + 1] == str(16 * MIB)
    assert "/dev/shm" in argv and argv[argv.index("--remount-ro") + 1] == "/dev"


def test_reviewed_shell_parameter_syntax_is_not_a_playbook_template():
    p = ScriptProgram(runtime="test", shell='printf "%s" "${1}"', interpreter="usr/bin/sh")
    assert p.shell == 'printf "%s" "${1}"'


async def test_private_transport_reads_fragmented_messages_and_enforces_whole_stream_limit():
    stream = asyncio.StreamReader()
    pending = asyncio.create_task(_read_bounded(stream, 8))
    stream.feed_data(b"1234")
    await asyncio.sleep(0)
    stream.feed_data(b"5678")
    stream.feed_eof()
    assert await pending == b"12345678"
    stream = asyncio.StreamReader()
    stream.feed_data(b"123456789")
    stream.feed_eof()
    with pytest.raises(ScriptError, match="protocol_invalid"):
        await _read_bounded(stream, 8)


def test_input_store_deduplicates_checks_digest_and_rejects_tampering(tmp_path):
    store = InputStore(tmp_path / "store", quota_bytes=6)
    key = store.put(b"12345")
    assert store.put(b"12345") == key
    assert store.read(key, 5) == b"12345"
    with pytest.raises(ScriptError, match="storage_quota"):
        store.put(b"67")
    path = store.root / key
    path.chmod(0o644)
    path.write_bytes(b"67890")
    path.chmod(0o444)
    with pytest.raises(ScriptError, match="digest_mismatch"):
        store.read(key, 5)


@pytest.mark.parametrize("kind", ["file_symlink", "directory_symlink", "fifo", "hardlink"])
def test_source_paths_do_not_follow_links_or_block_on_special_files(tmp_path, kind):
    source = tmp_path / "source"
    source.mkdir()
    (tmp_path / "secret").write_bytes(b"outside")
    if kind == "file_symlink":
        (source / "file").symlink_to(tmp_path / "secret")
        name = "file"
    elif kind == "directory_symlink":
        (source / "dir").symlink_to(tmp_path, target_is_directory=True)
        name = "dir/secret"
    elif kind == "fifo":
        os.mkfifo(source / "file")
        name = "file"
    else:
        os.link(tmp_path / "secret", source / "file")
        name = "file"
    with pytest.raises(ScriptError):
        read_file(source, name, 100)


async def test_git_proposal_uses_approved_bytes_not_mutable_checkout(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = GitManager()
    await manager._arun(["init"], cwd=str(repo))
    (repo / "script.sh").write_bytes(b"printf pinned\n")
    (repo / "support.bin").write_bytes(bytes(range(256)))
    (repo / "oversized.bin").write_bytes(b"x" * MIB)
    await manager._arun(["add", "."], cwd=str(repo))
    await manager._arun(["-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                         "commit", "-m", "fixture"], cwd=str(repo))
    revision = (await manager._arun(["rev-parse", "HEAD"], cwd=str(repo))).strip()
    (repo / "script.sh").write_bytes(b"printf mutable\n")
    await manager._arun(["add", "script.sh"], cwd=str(repo))
    await manager._arun(["-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                         "commit", "-m", "replacement"], cwd=str(repo))
    replacement = (await manager._arun(["rev-parse", "HEAD"], cwd=str(repo))).strip()
    await manager._arun(["replace", revision, replacement], cwd=str(repo))
    (repo / "script.sh").write_bytes(b"printf dirty\n")
    p = ScriptProgram(runtime="test", script="script.sh", interpreter="usr/bin/sh",
                      supporting_files=("support.bin",))
    store = InputStore(tmp_path / "store")
    pinned = await pin_script_inputs(p, store=store, source_root=repo, source_revision=revision,
                                     runtime=manifest())
    assert store.read(pinned.inputs[0].sha256, pinned.inputs[0].byte_count) == b"printf pinned\n"
    assert store.read(pinned.inputs[1].sha256, 256) == bytes(range(256))
    oid = (await manager._arun(["rev-parse", revision + ":oversized.bin"], cwd=str(repo))).strip()
    with pytest.raises(GitError, match="exceeds input limit"):
        await asyncio.wait_for(manager.aread_blob(str(repo), oid, max_bytes=1), 5)
    with pytest.raises(ScriptError, match="revision_not_pinned"):
        await pin_script_inputs(p, store=store, source_root=repo, source_revision="HEAD",
                                runtime=manifest())


async def test_vault_proposal_requires_declared_scoped_sources(tmp_path):
    source = tmp_path / "scoped-vault"
    source.mkdir()
    (source / "run.sh").write_text("true")
    p = ScriptProgram(runtime="test", script="run.sh", interpreter="usr/bin/sh", source_kind="vault")
    result = await pin_source_scripts(
        SimpleNamespace(frontmatter={"scripts": {"run": p.model_dump(mode="json")}}),
        source_root=source, source_revision="vault-review-1", store=InputStore(tmp_path / "store"),
        runtimes={"test": manifest()}, max_bytes=10,
    )
    assert result["run"].inputs[0].sha256 == digest(b"true")
    (source / "run.sh").unlink()
    (source / "run.sh").symlink_to("/etc/passwd")
    with pytest.raises(ScriptError):
        await pin_script_inputs(p, store=InputStore(tmp_path / "store"), source_root=source,
                                source_revision="next-review", runtime=manifest())


def test_pin_rejects_compiler_authored_digests_or_missing_declared_file():
    body = pin().model_dump(mode="json")
    body["program_sha256"] = "0" * 64
    with pytest.raises(ValidationError):
        PinnedScript.model_validate(body)
    body = pin().model_dump(mode="json")
    body["inputs"] = [{"path": "extra", "byte_count": 0, "sha256": digest(b"")}]
    with pytest.raises(ValidationError):
        PinnedScript.model_validate(body)


def test_quota_reservations_are_atomic_idempotent_and_not_evicted(tmp_path):
    ledger = QuotaLedger(tmp_path / "quota", input_bytes=10, output_bytes=10, per_run_bytes=10)
    ledger.reserve("1" * 64, "a", input_bytes=4, output_bytes=5)
    ledger.reserve("1" * 64, "a", input_bytes=4, output_bytes=5)
    with pytest.raises(ScriptError, match="identity_conflict"):
        ledger.reserve("1" * 64, "a", input_bytes=3, output_bytes=5)
    with pytest.raises(ScriptError, match="storage_quota"):
        ledger.reserve("2" * 64, "a", input_bytes=2, output_bytes=0)
    ledger.reserve("2" * 64, "b", input_bytes=5, output_bytes=5)
    with pytest.raises(ScriptError, match="storage_quota"):
        ledger.reserve("3" * 64, "c", input_bytes=0, output_bytes=1)
    ledger.release("1" * 64)
    ledger.reserve("3" * 64, "c", input_bytes=0, output_bytes=1)


async def test_disabled_and_missing_launcher_fail_closed_without_process():
    req = request()
    result = await launch(ScriptsConfig(), req, jobs_enabled=True, cancelled=asyncio.Event())
    assert result.outcome == "admission_denied" and result.started_at is None
    assert not (await probe_launcher(ScriptsConfig(launcher="/missing/aq-launcher")))["ready"]


async def test_dedicated_identity_and_delegation_are_mandatory():
    policy = LauncherPolicy(scripts=dataclasses.asdict(ScriptsConfig()), daemon_uid=os.getuid(),
                            lock_dir="/nonexistent", capacity=1)
    result = await prerequisites(policy)
    assert not result["ready"]
    assert result["reason"] == "script_execution_identity_unavailable"


def test_missing_real_delegation_fails_without_creating_a_cgroup(tmp_path):
    # A real directory outside the cgroup hierarchy is never a delegation.
    with pytest.raises(ScriptError, match="cgroup_invalid"):
        _cgroup_files(tmp_path)
    assert list(tmp_path.iterdir()) == []
    with pytest.raises((ScriptError, OSError)):
        _cgroup_files(Path("/sys/fs/cgroup/aq-e1-missing-" + uuid.uuid4().hex))


async def test_doctor_is_registered_read_only_and_disabled_by_default():
    ctx = DoctorContext(config=SimpleNamespace(resources=SimpleNamespace(scripts=ScriptsConfig())))
    result = await check_script_sandbox(ctx)
    assert result.data == {"ready": False} and not result.fixable
    registry = default_registry()
    check = registry.get("resources.script_sandbox")
    assert check is not None and check.fix is None


@pytest.mark.parametrize("values", [{"network": "host"}, {"env_allowlist": ["AQ_API_TOKEN"]},
                                    {"wall_seconds": 901}, {"queue_seconds": False},
                                    {"enabled": True}])
def test_script_config_validates_host_policy(values):
    assert ScriptsConfig(**values).validate()
    assert not ScriptsConfig().validate()


def test_payloads_never_appear_in_result_repr():
    result = SandboxResult("test", "succeeded", stdout=b"secret", stderr=b"another-secret")
    assert "secret" not in repr(result)


# ------------------------------- real host -------------------------------

@pytest.fixture
def real_host():
    path = os.environ.get("AQ_SCRIPT_ACCEPTANCE_CONFIG")
    assert path, (
        "UNMET E1 ACCEPTANCE: operator must supply AQ_SCRIPT_ACCEPTANCE_CONFIG pointing to an "
        "isolated, pre-provisioned launcher policy; missing prerequisites are not a skip/pass"
    )
    policy = LauncherPolicy.model_validate_json(Path(path).read_bytes())
    settings = policy.settings()
    assert settings.enabled and policy.jobs_enabled
    runtime = RuntimeManifest.model_validate_json(Path(settings.runtime_manifest).read_bytes())
    return policy, settings, runtime


async def real_run(host, code, *, arguments=(), stdin=b"", limits=None, environment=None,
                   shell=False, cancelled=None):
    _, settings, runtime = host
    interpreter = "usr/bin/sh" if shell else "usr/bin/python3"
    # Fixture runtime must include these two reviewed entrypoints. No host
    # interpreter paths can be supplied by a command request.
    p = ScriptProgram(
        runtime=runtime.runtime, shell=code if shell else None,
        script=None if shell else "acceptance.py", interpreter=interpreter, source_kind="vault",
        arguments=tuple({"type": "literal", "value": arg} for arg in arguments),
        env_allowlist=tuple((environment or {}).keys()), limits=limits or ScriptLimits(),
    )
    source = Path(settings.input_root).parent / ("acceptance-" + uuid.uuid4().hex)
    source.mkdir(mode=0o700)
    try:
        (source / "acceptance.py").write_text(code)
        pinned = await pin_script_inputs(p, source_root=source, source_revision="acceptance",
                                         runtime=runtime, store=InputStore(Path(settings.input_root)))
        req = request(pinned=pinned, arguments=arguments, environment=environment or {},
                      stdin_base64=base64.b64encode(stdin).decode())
        return await launch(settings, req, jobs_enabled=True, cancelled=cancelled or asyncio.Event())
    finally:
        (source / "acceptance.py").unlink()
        source.rmdir()


@pytest.mark.integration
async def test_real_host_identity_caps_fds_stdin_and_environment(real_host):
    _, settings, _ = real_host
    ready = await probe_launcher(settings)
    assert ready["ready"], ready
    code = '''import json, os, resource, sys
status = dict(line.split(":", 1) for line in open("/proc/self/status") if ":" in line)
print(json.dumps({"uid": os.getuid(), "caps": int(status["CapEff"], 16),
 "nnp": int(status["NoNewPrivs"]), "fds": os.listdir("/proc/self/fd"),
 "nofile": resource.getrlimit(resource.RLIMIT_NOFILE), "env": dict(os.environ),
 "stdin": sys.stdin.read()}))
'''
    result = await real_run(real_host, code, stdin=b"bounded-input")
    assert result.outcome == "succeeded" and result.cleanup_complete, result
    body = json.loads(result.stdout)
    assert body["uid"] != os.getuid() and body["uid"] != 0
    assert body["caps"] == 0 and body["nnp"] == 1
    assert set(body["fds"]) <= {"0", "1", "2", "3"}  # listdir's own directory FD
    assert body["nofile"] == [128, 128]
    assert body["stdin"] == "bounded-input" and body["env"]["HOME"] == "/scratch"
    assert not any(name.startswith(("AQ_", "PYTHON", "SSH_")) for name in body["env"])


@pytest.mark.integration
async def test_real_host_files_loopback_and_unix_sockets_are_inaccessible(real_host, tmp_path):
    host_secret = tmp_path / "secret"
    host_secret.write_text("host-private")
    tcp = socket.socket()
    tcp.bind(("127.0.0.1", 0))
    tcp.listen()
    unix = socket.socket(socket.AF_UNIX)
    unix.bind(str(tmp_path / "control.sock"))
    unix.listen()
    abstract = socket.socket(socket.AF_UNIX)
    abstract_name = "\0aq-e1-" + uuid.uuid4().hex
    abstract.bind(abstract_name)
    abstract.listen()
    try:
        code = f'''import os, socket
assert not os.path.exists({str(host_secret)!r})
for family, address in [(socket.AF_INET, ("127.0.0.1", {tcp.getsockname()[1]})),
 (socket.AF_UNIX, {str(tmp_path / "control.sock")!r}), (socket.AF_UNIX, {abstract_name!r})]:
 s = socket.socket(family); s.settimeout(0.25)
 try: s.connect(address)
 except OSError: pass
 else: raise AssertionError("host socket accessible")
print("isolated")
'''
        result = await real_run(real_host, code)
        assert result.outcome == "succeeded" and result.stdout == b"isolated\n", result
    finally:
        tcp.close()
        unix.close()
        abstract.close()


@pytest.mark.integration
async def test_real_shell_metacharacters_remain_data(real_host):
    data = '$(touch /scratch/injected); `id`; "x"'
    result = await real_run(real_host, 'printf "%s" "$1"; test ! -e /scratch/injected',
                            shell=True, arguments=(data,))
    assert result.outcome == "succeeded" and result.stdout == data.encode(), result


@pytest.mark.integration
async def test_real_simultaneous_stdout_stderr_floods_stop_the_cgroup(real_host):
    code = '''import os
if os.fork() == 0:
 while True: os.write(2, b"e"*8192)
while True: os.write(1, b"o"*8192)
'''
    result = await real_run(real_host, code, limits=ScriptLimits(stdout_bytes=32768, stderr_bytes=32768))
    assert result.outcome == "output_limit_exceeded" and result.cleanup_complete, result
    assert not result.capture_complete
    assert len(result.stdout) <= 32768 and len(result.stderr) <= 32768


@pytest.mark.integration
@pytest.mark.parametrize("limit,code,reason", [
    ({"cpu_seconds": 1}, "while True: pass", "cpu"),
    ({"memory_bytes": 32 * MIB}, "x = bytearray(128*1024**2)", "memory"),
    ({"pids": 8}, "import os,time\nwhile True:\n if os.fork()==0: time.sleep(30)", "pids"),
    ({"scratch_bytes": 4096}, "import signal\nsignal.signal(signal.SIGXFSZ, signal.SIG_DFL)\n"
     "open('/scratch/full','wb').write(b'x'*8192)", "scratch"),
])
async def test_real_resource_limits(real_host, limit, code, reason):
    result = await real_run(real_host, code, limits=ScriptLimits(**limit))
    assert result.cleanup_complete and result.outcome == "resource_limit_exceeded", result
    assert result.reason == reason, result


@pytest.mark.integration
async def test_real_fd_scratch_and_no_second_writable_mount(real_host):
    code = '''import errno, os
fds=[]
try:
 while True: fds.append(os.open('/dev/null', os.O_RDONLY))
except OSError as exc: assert exc.errno == errno.EMFILE
for fd in fds: os.close(fd)
assert len(fds) <= 29
for path in ['/dev/shm/bypass', '/tmp/bypass', '/inputs/bypass', '/usr/bypass']:
 try: open(path, 'w').write('bad')
 except OSError: pass
 else: raise AssertionError(path)
print('limits')
'''
    result = await real_run(real_host, code, limits=ScriptLimits(fds=32))
    assert result.outcome == "succeeded" and result.stdout == b"limits\n", result


@pytest.mark.integration
async def test_real_tmpfs_total_limit_cannot_be_bypassed_with_many_files(real_host):
    code = '''import errno, os
total = 0
try:
 for index in range(128):
  with open('/scratch/' + str(index), 'wb', buffering=0) as file:
   total += file.write(b'x' * 4096)
except OSError as exc: assert exc.errno == errno.ENOSPC
else: raise AssertionError('scratch tmpfs exceeded its total cap')
assert total <= 65536
print('bounded')
'''
    result = await real_run(real_host, code, limits=ScriptLimits(scratch_bytes=65536))
    assert result.outcome == "succeeded" and result.stdout == b"bounded\n", result


@pytest.mark.integration
@pytest.mark.parametrize("cancel", [False, True])
async def test_real_cancel_wall_timeout_and_double_fork_cleanup(real_host, cancel):
    event = asyncio.Event()
    code = '''import os,signal,time
if os.fork() == 0:
 os.setsid()
 if os.fork() == 0:
  signal.signal(signal.SIGTERM, signal.SIG_IGN)
  while True: time.sleep(.01)
 else: os._exit(0)
while True: time.sleep(.01)
'''
    async def cancel_later():
        await asyncio.sleep(1)
        event.set()
    timer = asyncio.create_task(cancel_later()) if cancel else None
    result = await real_run(real_host, code, limits=ScriptLimits(wall_seconds=2), cancelled=event)
    if timer:
        await timer
    assert result.outcome == ("cancelled" if cancel else "timed_out"), result
    assert result.cleanup_complete and result.cgroup and not Path(result.cgroup).exists(), result
