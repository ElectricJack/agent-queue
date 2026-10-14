"""Pinned inputs and ordinary-process acceptance, with no host provisioning."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import os
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from src.config import ScriptsConfig
from src.doctor import default_registry
from src.git.manager import GitError, GitManager
from src.jobs.script_sandbox import (
    BINDING_BYTES, SandboxRequest, SandboxResult, _process_argv, launch,
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
    for values in ({"wall_seconds": True}, {"stdout_bytes": "32"}, {"wall_seconds": 901}, {"stderr_bytes": 0}):
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
        script_environment((name,), (name,), {name: "must-not-leak"}, home="/scratch")
    assert "must-not-leak" not in str(script_environment((), (), {}, home="/scratch"))


def test_environment_is_explicit_intersection_and_sandbox_home():
    assert script_environment(("LABEL",), ("LABEL",), {"LABEL": "a;b$(id)"}, home="/scratch") == {
        "PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "HOME": "/scratch",
        "LABEL": "a;b$(id)",
    }
    with pytest.raises(ScriptError):
        script_environment(("LABEL",), (), {"LABEL": "value"}, home="/scratch")


def test_shell_data_is_a_separate_argv_element(tmp_path):
    text = '$(touch /scratch/injected); `id`; "quoted"'
    req = request(arguments=(text,))
    argv = _process_argv(ScriptsConfig(runtime_root="/runtime"), req, tmp_path)
    assert argv == ["/runtime/usr/bin/sh", "-c", req.pinned.program.shell, "aq-script", text]


def test_reviewed_shell_parameter_syntax_is_not_a_playbook_template():
    p = ScriptProgram(runtime="test", shell='printf "%s" "${1}"', interpreter="usr/bin/sh")
    assert p.shell == 'printf "%s" "${1}"'


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


@pytest.mark.parametrize("values", [{"env_allowlist": ["AQ_API_TOKEN"]},
                                    {"wall_seconds": 901}, {"queue_seconds": False},
                                    {"runtime_root": "relative"}, {"enabled": 1}])
def test_script_config_validates_policy(values):
    assert ScriptsConfig(**values).validate()
    assert ScriptsConfig().enabled
    assert not ScriptsConfig().validate()


def test_no_isolation_fields_or_doctor_gate():
    assert default_registry().get("resources.script_sandbox") is None
    assert not {"launcher", "bwrap", "cgroup_root", "execution_uid", "network"} & {
        item.name for item in dataclasses.fields(ScriptsConfig)
    }
    for field in ("cpu_seconds", "memory_bytes", "pids", "fds", "scratch_bytes"):
        with pytest.raises(ValidationError):
            ScriptLimits(**{field: 1})


def test_payloads_never_appear_in_result_repr():
    result = SandboxResult("test", "succeeded", stdout=b"secret", stderr=b"another-secret",
                           stdout_json={"value": "third-secret"})
    assert "secret" not in repr(result)


@pytest.fixture
def process_host(tmp_path):
    # These are ordinary host executables, no copied image, UID or launcher.
    python = Path(sys.executable).resolve()
    shell = Path("/bin/sh").resolve()
    files = tuple(RuntimeFile(path=str(path).lstrip("/"), sha256=digest(path.read_bytes()),
                              byte_count=path.stat().st_size, executable=True)
                  for path in (python, shell))
    runtime = RuntimeManifest(runtime="local", files=files)
    manifest_path = tmp_path / "runtime.json"
    manifest_path.write_text(runtime.model_dump_json())
    settings = ScriptsConfig(runtime_root="/", runtime_manifest=str(manifest_path),
                             input_root=str(tmp_path / "store"), state_root=str(tmp_path / "state"))
    return settings, runtime, tmp_path / "box-lock", python, shell


async def local_request(host, code, *, arguments=(), stdin=b"", limits=None, environment=None,
                        shell=False, form="script", output="text", json_required=False,
                        cwd="scratch"):
    settings, runtime, _, python, sh = host
    interpreter = str(sh if shell else python).lstrip("/")
    forms = {"shell": code, "interpreter": interpreter} if shell else {
        "script": "run.py", "interpreter": interpreter}
    if form == "argv":
        forms = {"argv": (interpreter, *({"type": "literal", "value": arg} for arg in arguments))}
    p = ScriptProgram(runtime=runtime.runtime, **forms, source_kind="vault", cwd=cwd,
                      arguments=() if form == "argv" else tuple(
                          {"type": "literal", "value": arg} for arg in arguments),
                      env_allowlist=tuple((environment or {}).keys()), limits=limits or ScriptLimits(),
                      output=output, json_required=json_required)
    source = Path(settings.input_root).parent / ("source-" + uuid.uuid4().hex)
    source.mkdir()
    (source / "run.py").write_text(code)
    pinned = await pin_script_inputs(p, source_root=source, source_revision="local-test",
                                     runtime=runtime, store=InputStore(Path(settings.input_root)))
    # Changes after proposal do not affect execution.
    (source / "run.py").write_text("raise AssertionError('live source read')")
    return request(pinned=pinned, arguments=arguments, environment=environment or {},
                   stdin_base64=base64.b64encode(stdin).decode())


async def run_request(host, req, *, cancelled=None, jobs_enabled=True):
    settings, _, lock_dir, *_ = host
    return await launch(settings, req, jobs_enabled=jobs_enabled,
                        cancelled=cancelled or asyncio.Event(), lock_dir=lock_dir, capacity=1)


async def local_run(host, code, *, cancelled=None, **kwargs):
    return await run_request(host, await local_request(host, code, **kwargs), cancelled=cancelled)


async def test_process_stdin_environment_cwd_and_closed_descriptors(process_host, monkeypatch):
    monkeypatch.setenv("AQ_API_TOKEN", "never-inherit")
    process_host[0].env_allowlist = ["LABEL"]
    fd = os.open(__file__, os.O_RDONLY)
    os.set_inheritable(fd, True)
    try:
        result = await local_run(process_host, '''import json, os, sys
print(json.dumps({"uid": os.getuid(), "stdin": sys.stdin.read(), "env": dict(os.environ),
                 "cwd": os.getcwd(), "fds": [os.readlink('/proc/self/fd/'+fd)
                 for fd in os.listdir('/proc/self/fd') if os.path.exists('/proc/self/fd/'+fd)]}))
''', stdin=b"bounded input", environment={"LABEL": "a;b$(id)"}, output="json", json_required=True)
    finally:
        os.close(fd)
    assert result.outcome == "succeeded" and result.cleanup_complete and result.capture_complete
    body = result.stdout_json
    assert body["uid"] == os.getuid()
    assert body["stdin"] == "bounded input"
    assert body["env"]["LABEL"] == "a;b$(id)"
    assert body["cwd"] == body["env"]["HOME"]
    assert "AQ_API_TOKEN" not in body["env"]
    assert not any(path.endswith("test_playbook_script_sandbox.py") for path in body["fds"])
    assert not Path(body["cwd"]).exists()
    binding = result.binding_value()
    assert binding["stdout_json"] == body and not binding["inline_omitted"]
    assert len(canonical(binding)) <= BINDING_BYTES


async def test_all_program_forms_and_shell_data(process_host):
    data = '$(touch injected); `id`; "x"'
    result = await local_run(process_host, 'printf "%s" "$1"; test ! -e injected',
                             shell=True, arguments=(data,))
    assert result.outcome == "succeeded" and result.stdout == data.encode()
    result = await local_run(process_host, "", form="argv", arguments=("-c", "print('argv')"))
    assert result.outcome == "succeeded" and result.stdout == b"argv\n"
    result = await local_run(process_host, "import os; print(os.path.basename(os.getcwd()))",
                             cwd="inputs")
    assert result.outcome == "succeeded" and result.stdout == b"inputs\n"


@pytest.mark.parametrize("switch", ["scripts", "jobs"])
async def test_disabled_switch_prevents_spawn(process_host, monkeypatch, switch):
    req = await local_request(process_host, "raise AssertionError('must not run')")
    if switch == "scripts":
        process_host[0].enabled = False
    def forbidden(*args, **kwargs):
        pytest.fail("disabled scripts must not spawn")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    result = await run_request(process_host, req, jobs_enabled=switch != "jobs")
    assert result.outcome == "admission_denied" and result.reason == "script_disabled"
    assert result.started_at is None and result.cleanup_complete


@pytest.mark.parametrize("target", ["input", "runtime"])
async def test_changed_pins_refuse_before_spawn(process_host, monkeypatch, target):
    req = await local_request(process_host, "print('original')")
    if target == "input":
        path = Path(process_host[0].input_root) / req.pinned.inputs[0].sha256
        path.chmod(0o644)
        path.write_bytes(b"changed")
        path.chmod(0o444)
    else:
        runtime = process_host[1].model_dump(mode="json")
        runtime["files"][0]["sha256"] = "0" * 64
        Path(process_host[0].runtime_manifest).write_text(json.dumps(runtime))
    def forbidden(*args, **kwargs):
        pytest.fail("changed pins must not spawn")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    result = await run_request(process_host, req)
    assert result.outcome == "admission_denied" and result.started_at is None
    assert "digest_mismatch" in result.reason


async def test_simultaneous_stream_flood_stops_process_group(process_host):
    result = await local_run(process_host, '''import os
if os.fork() == 0:
 while True: os.write(2, b'e'*8192)
while True: os.write(1, b'o'*8192)
''', limits=ScriptLimits(stdout_bytes=32768, stderr_bytes=32768), output="json", json_required=True)
    assert result.outcome == "output_limit_exceeded" and result.cleanup_complete
    assert not result.capture_complete and result.stdout_json is None
    assert len(result.stdout) <= 32768 and len(result.stderr) <= 32768
    assert max(result.stdout_bytes, result.stderr_bytes) > 32768
    assert result.binding_value()["stdout"] is None


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
async def test_exact_stream_cap_is_complete_overflow_is_not_json_success(process_host, stream):
    for count in (32, 33):
        result = await local_run(process_host, f"import os; os.write({1 if stream == 'stdout' else 2}, "
                                 f"b'1'+b' '*{count-1})", limits=ScriptLimits(
                                     stdout_bytes=32, stderr_bytes=32),
                                 output="json" if stream == "stdout" else "text", json_required=False)
        assert result.outcome == ("succeeded" if count == 32 else "output_limit_exceeded")
        assert result.capture_complete == (count == 32)
        assert getattr(result, stream + "_bytes") == count


@pytest.mark.parametrize("payload,status", [
    ('{"a":[1,true,null]}', "parsed"), ('null', "parsed"), ('42', "parsed"),
    ('{"a":1} trailing', "invalid"), ('{} {}', "invalid"), ('```json\n{}\n```', "invalid"),
    ('NaN', "invalid"), ('Infinity', "invalid"), ('1e999', "invalid"), ('', "invalid"),
])
async def test_json_parses_entire_stdout_strictly(process_host, payload, status):
    result = await local_run(process_host, f"import sys; sys.stdout.write({payload!r})",
                             output="json", json_required=True)
    assert result.json_status == status
    assert result.outcome == ("succeeded" if status == "parsed" else "invalid_json")


async def test_nonzero_exit_precedes_invalid_json_and_optional_json_can_fail(process_host):
    result = await local_run(process_host, "print('invalid'); raise SystemExit(7)",
                             output="json", json_required=True)
    assert result.outcome == "nonzero_exit" and result.exit_code == 7 and result.json_status == "invalid"
    result = await local_run(process_host, "print('invalid')", output="json")
    assert result.outcome == "succeeded" and result.json_status == "invalid"


async def test_large_binding_omits_whole_streams_and_required_json_fails(process_host):
    result = await local_run(process_host, "print('x'*300000)")
    binding = result.binding_value()
    assert result.outcome == "succeeded" and len(result.stdout) == 300001
    assert binding["inline_omitted"] and binding["stdout"] is None and binding["stderr"] is None
    assert len(canonical(binding)) <= BINDING_BYTES and result.capture_complete
    result = await local_run(process_host, "import json; print(json.dumps('x'*130000))",
                             output="json", json_required=True)
    assert result.outcome == "succeeded" and result.json_status == "parsed"
    assert result.stdout_json == "x" * 130000
    assert result.binding_value()["inline_omitted"]
    result = await local_run(process_host, "import json; print(json.dumps('x'*300000))",
                             output="json", json_required=True)
    assert result.outcome == "invalid_json" and result.json_status == "too_large"
    assert result.stdout_json is None and result.capture_complete
    assert len(canonical(result.binding_value())) <= BINDING_BYTES


async def wait_file(path):
    async with asyncio.timeout(10):
        while not path.exists():
            await asyncio.sleep(0.01)


def assert_not_running(pid):
    path = Path(f"/proc/{pid}/stat")
    try:
        stat = path.read_text()
    except FileNotFoundError:
        # Reaping can remove /proc entries between any existence check and read.
        return
    assert stat.rsplit(")", 1)[1].split()[0] == "Z"


def test_child_cleanup_check_accepts_reaping_during_read(monkeypatch):
    monkeypatch.setattr(Path, "exists", lambda self: True)

    def reaped(self):
        raise FileNotFoundError(self)

    monkeypatch.setattr(Path, "read_text", reaped)
    assert_not_running(123)


@pytest.mark.parametrize("state", ["R", "S", "Z"])
def test_child_cleanup_check_rejects_live_processes(monkeypatch, state):
    monkeypatch.setattr(Path, "read_text", lambda self: f"123 (script child) {state} 1")
    if state == "Z":
        assert_not_running(123)
    else:
        with pytest.raises(AssertionError):
            assert_not_running(123)


@pytest.mark.parametrize("state", [None, "R", "S", "Z", "X"])
def test_group_cleanup_observes_only_live_owned_members(tmp_path, monkeypatch, state):
    from src.jobs import script_sandbox

    unrelated = tmp_path / "234"
    unrelated.mkdir()
    (unrelated / "stat").write_bytes(b"234 (other \xff process) R 1 234")
    owned = tmp_path / "123"
    owned.mkdir()
    if state is not None:
        (owned / "stat").write_text(f"123 (script (child)) {state} 1 123")
    monkeypatch.setattr(script_sandbox, "Path", lambda value: tmp_path)
    assert script_sandbox._process_group_running(123) is (state in {"R", "S"})


@pytest.mark.parametrize("stuck", [False, True])
async def test_cleanup_observes_child_exit_after_kill(monkeypatch, stuck):
    from src.jobs import script_sandbox

    signals = []
    observations = []
    closed = []
    now = 0.0

    async def pause(seconds):
        nonlocal now
        now += seconds

    def running(pgid):
        assert pgid == 123
        assert signals[-1] == script_sandbox.signal.SIGKILL
        observations.append(pgid)
        return stuck or len(observations) == 1

    monkeypatch.setattr(script_sandbox, "GRACE_SECONDS", 0.025)
    monkeypatch.setattr(script_sandbox, "time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(script_sandbox, "asyncio", SimpleNamespace(
        to_thread=asyncio.to_thread, wait_for=asyncio.wait_for, sleep=pause,
    ))
    monkeypatch.setattr(script_sandbox.os, "killpg", lambda pid, sig: signals.append(sig))
    monkeypatch.setattr(script_sandbox, "_process_group_running", running)

    async def reaped():
        return 0

    process = SimpleNamespace(pid=123, wait=reaped,
                              _transport=SimpleNamespace(close=lambda: closed.append(True)))
    assert await script_sandbox._stop_process(process) is not stuck
    assert len(observations) >= 2
    assert bool(closed) is stuck


@pytest.mark.parametrize("mode", ["timeout", "event", "task", "leader_exit"])
async def test_timeout_cancel_and_child_cleanup(process_host, tmp_path, mode):
    marker = tmp_path / "pids"
    code = f'''import os, signal, time
child = os.fork()
if child == 0:
 signal.signal(signal.SIGTERM, signal.SIG_IGN)
 while True: time.sleep(.01)
open({str(marker)!r}, 'w').write(str(os.getpid())+' '+str(child))
{'raise SystemExit(0)' if mode == 'leader_exit' else 'while True: time.sleep(.01)'}
'''
    event = asyncio.Event()
    pending = asyncio.create_task(local_run(process_host, code, cancelled=event,
                                            limits=ScriptLimits(wall_seconds=1 if mode == "timeout"
                                                                else 20)))
    await wait_file(marker)
    if mode == "event":
        event.set()
    if mode == "task":
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    else:
        result = await pending
        assert result.outcome == {"timeout": "timed_out", "event": "cancelled",
                                   "leader_exit": "succeeded"}[mode]
        assert result.cleanup_complete
    for pid in marker.read_text().split():
        assert_not_running(int(pid))


async def test_launch_identity_is_never_replayed(process_host):
    req = await local_request(process_host, "print('once')")
    first = await run_request(process_host, req)
    second = await run_request(process_host, req)
    assert first.outcome == "succeeded"
    assert second.outcome == "admission_denied" and second.started_at is None
    assert first.quota_key == second.quota_key


async def test_shared_capacity_queue_timeout_and_cancellation_release_locks(process_host):
    from src.resources.box_lock import BoxLock

    settings, _, lock_dir, *_ = process_host
    settings.queue_seconds = 1
    req = await local_request(process_host, "print('admitted')")
    with BoxLock(lock_dir, 1).acquire():
        result = await run_request(process_host, req)
        assert result.outcome == "admission_timed_out" and result.started_at is None
        settings.queue_seconds = 30
        pending = asyncio.create_task(run_request(process_host, req))
        await asyncio.sleep(.2)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    result = await run_request(process_host, req)
    assert result.outcome == "succeeded"


async def test_run_lock_serializes_same_run(process_host, tmp_path):
    marker = tmp_path / "started"
    event = asyncio.Event()
    first = asyncio.create_task(local_run(process_host,
        f"import time; open({str(marker)!r},'w').close(); time.sleep(30)", cancelled=event))
    await wait_file(marker)
    process_host[0].queue_seconds = 1
    second = await local_run(process_host, "print('must wait')")
    assert second.outcome == "admission_timed_out" and second.started_at is None
    event.set()
    assert (await first).outcome == "cancelled"


async def test_cleanup_uncertainty_is_not_success(process_host, monkeypatch):
    from src.jobs import script_sandbox

    original = script_sandbox._stop_process
    async def uncertain(process):
        await original(process)
        return False
    monkeypatch.setattr(script_sandbox, "_stop_process", uncertain)
    result = await local_run(process_host, "print('{}')", output="json", json_required=True)
    assert result.outcome == "runtime_error" and result.reason == "script_cleanup_uncertain"
    assert not result.cleanup_complete and not result.capture_complete
    assert result.stdout_json is None and result.binding_value()["stdout"] is None


async def test_invalid_utf8_and_deep_json_are_named_parse_failures(process_host):
    # CPython 3.12 JSON uses a separate C recursion budget.
    depth = 10000
    for code in ("import os; os.write(1, b'\\xff')", f"print('['*{depth}+'0'+']'*{depth})"):
        result = await local_run(process_host, code, output="json", json_required=True)
        assert result.outcome == "invalid_json" and result.json_status == "invalid", (
            code, result, result.json_status, result.stdout[:100])


async def test_quota_and_input_limit_refuse_launch(process_host):
    req = await local_request(process_host, "print('must not run')")
    settings = process_host[0]
    settings.input_bytes = 1
    result = await run_request(process_host, req)
    assert result.reason == "script_input_limit_exceeded" and result.started_at is None
    settings.input_bytes = 64 * MIB
    settings.output_storage_bytes = 1
    result = await run_request(process_host, req)
    assert result.reason == "script_storage_quota" and result.started_at is None
