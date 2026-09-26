"""The experiment kit's pure parts: bounded load, attribution, identity, seeding."""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from src.config import AppConfig
from tests.db_fixtures import lease_dsn

KIT = Path(__file__).resolve().parent.parent / "scripts" / "dashboard-perf"


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"aqperf_{name}", KIT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def load():
    return load_script("load")


# --- load.py ---------------------------------------------------------------


def test_load_completes_fixed_work_within_the_caps_and_cleans_up(load, tmp_path):
    out = load.run(seconds=60, iterations=200_000, workers=1, tmp_max_mib=1,
                   io_bytes=2 << 20, tmp_root=str(tmp_path), file_bytes=512 << 10)
    assert out["completed_iterations"] == 200_000 and out["timed_out"] is False
    assert out["requested_iterations"] == 200_000 and out["interrupted"] is None
    assert out["io"]["bytes_written"] == 2 << 20
    assert out["io"]["peak_bytes_on_disk"] <= 1 << 20
    assert out["throughput_iter_per_s"] > 0
    assert out["nice"] == os.nice(0)
    assert out["tmp_dir_removed"] is True and list(tmp_path.iterdir()) == []


def test_load_splits_the_work_without_losing_the_remainder(load, tmp_path):
    out = load.run(seconds=60, iterations=100_001, workers=3, tmp_max_mib=1,
                   io_bytes=0, tmp_root=str(tmp_path), file_bytes=512 << 10)
    assert out["requested_iterations"] == 100_001
    assert out["completed_iterations"] == 100_001 and out["timed_out"] is False
    assert out["workers"] == 3 and out["io"]["bytes_written"] == 0


def test_load_stops_at_the_deadline_and_still_cleans_up(load, tmp_path):
    started = time.monotonic()
    out = load.run(seconds=1, iterations=10**12, workers=1, tmp_max_mib=1,
                   io_bytes=10**12, tmp_root=str(tmp_path), file_bytes=512 << 10)
    assert out["timed_out"] is True and out["interrupted"] is None
    assert 0 < out["completed_iterations"] < 10**12
    assert time.monotonic() - started < 15
    assert out["tmp_dir_removed"] is True and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("kwargs", [
    {"tmp_max_mib": 257},                     # above the spec's 256 MiB ceiling
    {"tmp_max_mib": 1, "file_bytes": 2 << 20},  # one file would not fit
    {"seconds": 0},
    {"workers": 0},
])
def test_load_refuses_arguments_outside_the_caps_before_touching_disk(load, tmp_path, kwargs):
    args = {"seconds": 5, "iterations": 10, "workers": 1, "tmp_max_mib": 1, "io_bytes": 0,
            "tmp_root": str(tmp_path), "file_bytes": 512 << 10, **kwargs}
    with pytest.raises(ValueError):
        load.run(**args)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_load_cleans_up_and_reports_when_signalled(tmp_path, signum):
    proc = subprocess.Popen(
        [sys.executable, str(KIT / "load.py"), "--seconds", "60", "--iterations", str(10**12),
         "--workers", "1", "--io-bytes", str(10**12), "--tmp-max-mib", "1",
         "--file-bytes", str(512 << 10), "--tmp-root", str(tmp_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        waited = time.monotonic() + 20
        while not list(tmp_path.iterdir()) and time.monotonic() < waited:
            time.sleep(0.05)
        assert list(tmp_path.iterdir()), "the helper never created its temporary directory"
        time.sleep(0.3)
        proc.send_signal(signum)
        stdout, stderr = proc.communicate(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    assert proc.returncode == 128 + signum, stderr
    out = json.loads(stdout)
    assert out["interrupted"] == signal.Signals(signum).name
    assert out["timed_out"] is True and out["completed_iterations"] > 0
    assert out["tmp_dir_removed"] is True and list(tmp_path.iterdir()) == []


def test_wrapped_argv_applies_session_nice_and_caps(load, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/nice" if name == "nice" else None)
    cfg = AppConfig()
    cfg.resources.session_nice = 10
    cfg.resources.cgroups.enabled = False
    argv, env = load.wrapped_argv(cfg, [sys.executable, "load.py"])
    assert argv[:3] == ["nice", "-n", "10"]
    assert argv[3:] == [sys.executable, "load.py"]
    assert "PYTEST_XDIST_AUTO_NUM_WORKERS" in env and "AQ_CPU_SHARE" in env


# --- inventory.py ----------------------------------------------------------


def test_inventory_classifies_by_marker_and_redacts_the_instance_token():
    inv = load_script("inventory")
    assert inv.classify({"AQ_JOB_ID": "j"}, "pytest") == "job"
    assert inv.classify({"AQ_SESSION_ID": "s"}, "pytest") == "session"
    assert inv.classify({"AQ_TEST_RUN_ID": "r"}, "pytest") == "test_run"
    assert inv.classify({}, "python scripts/dashboard-perf/load.py --seconds 120") == "load_helper"
    assert inv.classify({}, "pytest -n 8") == "ungated"
    rows = [{"pid": 1, "ppid": 0, "pcpu": 50.0, "rss_kb": 10, "comm": "pytest", "args": "pytest -n 8"},
            {"pid": 2, "ppid": 1, "pcpu": 25.0, "rss_kb": 10, "comm": "claude", "args": "claude"}]
    markers = {1: {}, 2: {"AQ_SESSION_ID": "s1", "AQ_INSTANCE_TOKEN": "hunter2"}}
    out = inv.inventory(rows=rows, read_markers=lambda pid: markers[pid])
    assert out["totals"]["ungated"] == {"count": 1, "pcpu": 50.0}
    assert out["processes"][1]["markers"]["AQ_INSTANCE_TOKEN"] == "set"
    assert "hunter2" not in json.dumps(out)


def test_inventory_names_the_load_helper_even_inside_a_session():
    """A helper launched from a worker session inherits its markers; it is still the load."""
    inv = load_script("inventory")
    helper = "/usr/bin/python3 /repo/scripts/dashboard-perf/load.py --cpu-worker --iterations 5"
    assert inv.classify({"AQ_SESSION_ID": "s", "AQ_INSTANCE_TOKEN": "t"}, helper) == "load_helper"
    assert inv.classify({"AQ_INSTANCE_TOKEN": "t"}, "bash") == "session"
    assert inv.classify({"AQ_SESSION_ID": "s", "AQ_TEST_RUN_ID": "r"}, "pytest") == "session"
    assert inv.classify({"AQ_JOB_ID": "j", "AQ_SESSION_ID": "s"}, "pytest") == "job"


def test_inventory_resolves_the_slot_caps_rows_and_redacts_dsn_passwords():
    inv = load_script("inventory")
    rows = [{"pid": p, "ppid": 1, "pcpu": float(10 - p), "rss_kb": 1, "comm": "x",
             "args": "psql postgresql://aq:s3cret@db:5533/agent_queue" if p == 3 else "x"}
            for p in range(3, 8)]
    cwds = {3: "/home/u/repo/.aq/worktrees/slot-4/src", 4: "/tmp"}
    out = inv.inventory(top=3, rows=rows, read_markers=lambda pid: {},
                        read_cwd=lambda pid: cwds.get(pid))
    assert [p["pid"] for p in out["processes"]] == [3, 4, 5]
    assert out["processes"][0]["slot"] == "slot-4" and out["processes"][1]["slot"] is None
    assert "s3cret" not in json.dumps(out)
    assert out["totals"] == {"ungated": {"count": 3, "pcpu": 18.0}}


def test_inventory_reads_the_live_process_table():
    inv = load_script("inventory")
    out = inv.inventory(top=5)
    assert 0 < len(out["processes"]) <= 5
    assert all(p["class"] in inv.CLASSES for p in out["processes"])
    assert all("AQ_INSTANCE_TOKEN" not in p["markers"] or p["markers"]["AQ_INSTANCE_TOKEN"] == "set"
               for p in out["processes"])


# --- pg_identity.py --------------------------------------------------------


def test_pg_identity_never_prints_credentials():
    pg = load_script("pg_identity")
    test = "postgresql+asyncpg://tester:secret-a@db.local:5534/postgres"
    daemon = "postgresql://aq:secret-b@db.local:5533/agent_queue"
    out = pg.compare(test, daemon)
    assert out["test"] == {"scheme": "postgresql", "host": "db.local", "port": 5534, "database": "postgres"}
    assert out["same_server"] is False and out["same_database"] is False
    assert pg.compare(test, "postgresql://x:y@db.local:5534/other")["same_server"] is True
    assert pg.compare(None, daemon)["reason"] == "no_test_dsn"
    assert "secret" not in json.dumps(out)


def test_pg_identity_folds_loopback_and_the_default_port():
    pg = load_script("pg_identity")
    out = pg.compare("postgresql://u:p@127.0.0.1/agent_queue", "postgres://v:q@localhost:5432/agent_queue")
    assert out["same_server"] is True and out["same_database"] is True
    assert pg.compare("postgresql://u:p@localhost:5433/x", None) == {
        "test": {"scheme": "postgresql", "host": "localhost", "port": 5433, "database": "x"},
        "daemon": None, "same_server": None, "same_database": None, "reason": "no_daemon_dsn",
    }
    bad = pg.compare("not a dsn with password hunter2", "postgresql://u:p@h/db")
    assert bad["reason"] == "invalid_test_dsn" and bad["same_server"] is None
    assert "hunter2" not in json.dumps(bad)


def test_daemon_dsn_from_config_resolves_placeholders_like_the_daemon(tmp_path, monkeypatch):
    pg = load_script("pg_identity")
    assert pg.daemon_dsn_from_config(tmp_path / "missing.yaml") == (None, "no_config")

    config = tmp_path / "config.yaml"
    config.write_text("database:\n  url: postgresql+asyncpg://aq:${AQPERF_PW}@db:5533/agent_queue\n")
    monkeypatch.delenv("AQPERF_PW", raising=False)
    assert pg.daemon_dsn_from_config(config) == (None, "unresolved_placeholder")

    monkeypatch.setenv("AQPERF_PW", "from-shell")
    assert pg.daemon_dsn_from_config(config) == (
        "postgresql+asyncpg://aq:from-shell@db:5533/agent_queue", None)

    # The daemon lets .env beside the config win over the shell; so does this.
    (tmp_path / ".env").write_text("# creds\nAQPERF_PW=from-dotenv\n")
    assert pg.daemon_dsn_from_config(config)[0] == (
        "postgresql+asyncpg://aq:from-dotenv@db:5533/agent_queue")

    config.write_text("database: {}\n")
    assert pg.daemon_dsn_from_config(config) == (None, "no_database_url")
    config.write_text("database: [unclosed\n")
    assert pg.daemon_dsn_from_config(config) == (None, "unreadable_config")


def test_daemon_dsn_from_config_applies_the_environment_overlay(tmp_path, monkeypatch):
    pg = load_script("pg_identity")
    monkeypatch.delenv("AGENT_QUEUE_ENV", raising=False)
    (tmp_path / "config.yaml").write_text("env: dev\ndatabase:\n  url: postgresql://a:b@h/base\n")
    (tmp_path / "config.dev.yaml").write_text("database:\n  url: postgresql://a:b@h/overlay\n")
    assert pg.daemon_dsn_from_config(tmp_path / "config.yaml") == ("postgresql://a:b@h/overlay", None)


def test_pg_identity_main_prints_only_endpoints(tmp_path, monkeypatch, capsys):
    pg = load_script("pg_identity")
    config = tmp_path / "config.yaml"
    config.write_text("database:\n  url: postgresql://aq:daemon-pw@db:5533/agent_queue\n")
    monkeypatch.setenv("POSTGRES_TEST_DSN", "postgresql+asyncpg://t:test-pw@db:5533/postgres")
    assert pg.main(["--config", str(config)]) == 0
    printed = capsys.readouterr().out
    out = json.loads(printed)
    assert out["same_server"] is True and out["same_database"] is False
    assert "pw" not in printed


# --- seed.py ---------------------------------------------------------------


async def test_seed_refuses_the_operator_database_and_seeds_an_isolated_one():
    seed = load_script("seed")
    dsn = lease_dsn("perf-seed.db")
    with pytest.raises(seed.RefusedOperatorDatabase):
        seed.refuse_if_operator(dsn, dsn)
    seed.refuse_if_operator(dsn, "postgresql://u:p@elsewhere:5533/agent_queue")
    out = await seed.seed(dsn, "perf-fixture", 50)
    assert out == {"project_id": "perf-fixture", "created": 50}
    from src.database import Database

    db = Database(dsn)
    await db.initialize()
    try:
        counts = await db.metrics_live_counts()
        assert sum(counts["tasks"].values()) == 50
        assert await db.get_project("perf-fixture") is not None
    finally:
        await db.close()


def test_seed_refusal_matches_endpoints_not_spelling_and_hides_credentials():
    seed = load_script("seed")
    with pytest.raises(seed.RefusedOperatorDatabase) as refused:
        seed.refuse_if_operator("postgresql+asyncpg://x:pw-one@127.0.0.1/agent_queue",
                                "postgresql://aq:pw-two@localhost:5432/agent_queue")
    assert "pw-" not in str(refused.value)
    seed.refuse_if_operator("postgresql://x:y@localhost:5432/agent_queue_e2e",
                            "postgresql://x:y@localhost:5432/agent_queue")
    seed.refuse_if_operator("postgresql://x:y@localhost/db", None)


def test_seed_main_refuses_before_connecting(tmp_path, monkeypatch, capsys):
    seed = load_script("seed")

    async def must_not_connect(*args, **kwargs):  # pragma: no cover - the assertion is that it is not
        raise AssertionError("seed() ran against a refused database")

    monkeypatch.setattr(seed, "seed", must_not_connect)
    dsn = "postgresql://aq:op-pw@db:5533/agent_queue"
    assert seed.main(["--dsn", dsn, "--refuse-dsn", dsn]) == 2
    err = capsys.readouterr().err
    assert "daemon's database" in err and "op-pw" not in err

    # An operator config whose URL cannot be resolved is not permission to proceed.
    config = tmp_path / "config.yaml"
    config.write_text("database:\n  url: postgresql://aq:${AQPERF_UNSET}@db:5533/agent_queue\n")
    monkeypatch.delenv("AQPERF_UNSET", raising=False)
    assert seed.main(["--dsn", "postgresql://t:p@db:5534/x", "--operator-config", str(config)]) == 2
    assert "unresolved_placeholder" in capsys.readouterr().err


# --- experiment.py ---------------------------------------------------------


def complete_manifest():
    return {
        "mode": "loaded", "repetitions": 3, "clients": [1, 3],
        "warmup_ms": 30000, "observe_ms": 120000,
        "seed": {"tasks_total": 10000, "agents_total": 2}, "chrome": "Chrome/130",
        "viewport": [1600, 1000],
        "host": {"kernel": "6.18", "cpu_count": 24, "mem_total_mb": 64000},
        "load": {"argv": ["load.py"]}, "caps": {"PYTEST_XDIST_AUTO_NUM_WORKERS": "3"},
        "session_nice": 10, "git_sha": "abc", "pg_identity": {"same_server": False},
        "surfaces": ["tasks"],
    }


def test_manifest_validation_names_every_missing_factor():
    exp = load_script("experiment")
    complete = complete_manifest()
    assert exp.validate_manifest(complete) == []
    broken = {**complete, "repetitions": 1}
    del broken["chrome"]
    problems = exp.validate_manifest(broken)
    assert any("chrome" in p for p in problems) and any("repetitions" in p for p in problems)
    for key in complete:
        assert any(key in p for p in exp.validate_manifest(
            {k: v for k, v in complete.items() if k != key})), key


def test_spread_and_summary_keep_raw_values_and_medians():
    from src.metrics.histogram import new_hist, observe

    exp = load_script("experiment")
    assert exp.spread([100.0, 110.0, 130.0]) == pytest.approx(30 / 110)
    assert exp.spread([100.0]) is None
    runs = [
        {"cold": {"tasks": {"ready_ms": v, "raw": [v]}}, "idle": {},
         "api": {"POST /api/task/get": {"median_ms": v, "raw_ms": [v]}}}
        for v in (100, 120, 110)
    ]
    loads = [{"throughput_iter_per_s": 1000.0, "timed_out": False}] * 3
    drift = new_hist()
    observe(drift, 30)
    series = [[{"ts": 1.0, "perf": {"enabled": True, "loop": {"drift": drift},
                                   "db": {}, "api": {}, "host": {}}}]] * 3
    summary = exp.summarize(runs, loads, series)
    assert summary["cold"]["tasks"]["ready_ms"] == {
        "median": 110, "raw": [100, 120, 110], "spread": pytest.approx(20 / 110)}
    assert summary["api"]["POST /api/task/get"]["median_ms"]["median"] == 110
    assert summary["load"]["throughput_iter_per_s"]["median"] == 1000.0
    assert summary["daemon"]["loop_drift_p95_ms"] < 50
    assert summary["daemon"]["stalls_over_500ms"] == 0


def test_queued_mode_is_refused_without_a_workload_command(capsys):
    exp = load_script("experiment")
    assert exp.main(["--mode", "queued", "--dashboard-url", "http://127.0.0.1:1",
                     "--api-url", "http://127.0.0.1:2", "--out", "/nonexistent"]) == 2
    assert "job submit" in capsys.readouterr().err


def test_summary_merges_buckets_and_preserves_unknown_probes():
    from src.metrics.histogram import new_hist, observe, percentile, merge_hists

    exp = load_script("experiment")
    fast, slow = new_hist(), new_hist()
    for _ in range(99):
        observe(fast, 1)
    observe(slow, 800)
    groups = [[{"perf": {"enabled": True, "loop": {"drift": h},
                          "sampler": {"perf_ms": cost},
                          "host": {"psi": {"cpu": {"some_avg10": cost}},
                                   "ungated": {"pytest_processes": cost}}}}]
              for h, cost in ((fast, 2), (slow, 9))]
    out = exp.summarize([], [], groups)
    assert out["daemon"]["loop_drift_p95_ms"] == percentile(merge_hists([fast, slow]), .95)
    assert out["daemon"]["loop_drift_max_ms"] == 800
    assert out["daemon"]["stalls_over_500ms"] == 1
    assert out["daemon"]["pool_wait_p95_ms"] is None
    assert out["daemon"]["psi_max"]["cpu"]["some_avg10"] == 9
    assert out["daemon"]["ungated_processes_max"] == 9
    assert out["daemon"]["sampler_perf_ms_p95"] == 9
    assert exp.summarize([], [], [[]])["daemon"]["stalls_over_500ms"] is None


def test_workload_coverage_flags_a_helper_that_finished_before_observation():
    exp = load_script("experiment")
    runs = [{"manifest": {"clients": 3}, "start_ts": 10, "end_ts": 40,
             "api_start_ts": 30, "api_end_ts": 40,
             "idle": {"tasks": {"clients": [{"start_ts": 10, "end_ts": 30}]}}}]
    assert all(row["fraction"] == 0 for row in exp.coverage(runs, 0, 5))
    rows = exp.coverage(runs, 0, 20)
    assert next(row for row in rows if row["surface"] == "tasks")["fraction"] == .5
    assert all(row["fraction"] == 1 for row in exp.coverage(runs, 0, 40))


def test_workload_wrapper_deadline_stops_a_process_and_preserves_logs(tmp_path):
    exp = load_script("experiment")
    worker = exp.Workload([sys.executable, "-c", "import time; time.sleep(60)"],
                          dict(os.environ), tmp_path / "load.log", .2)
    worker.finish()
    assert worker.timed_out is True
    assert worker.proc.returncode != 0 and worker.ended >= worker.started
    assert worker.stdout.closed and worker.stderr.closed


def test_experiment_target_refusal_happens_before_any_http(tmp_path, monkeypatch):
    from types import SimpleNamespace

    exp = load_script("experiment")
    pg = exp.kit_module("pg_identity")
    operator = tmp_path / "operator.yaml"
    target = tmp_path / "target.yaml"
    operator.write_text("database:\n  url: postgresql://aq:private@localhost:5533/agent_queue\n")
    target.write_text(operator.read_text())
    monkeypatch.setattr(pg, "DEFAULT_CONFIG", operator)
    monkeypatch.setattr(exp, "kit_module", lambda name: pg)
    monkeypatch.setattr(exp, "fetch_json", lambda url: pytest.fail("HTTP before target refusal"))
    args = SimpleNamespace(config=target, api_url="http://127.0.0.1:8099",
                           dashboard_url="http://127.0.0.1:8092")
    with pytest.raises(ValueError, match="operator's database") as refused:
        exp.verify_target(args, {"port": 8099})
    assert "private" not in str(refused.value)
    target.write_text("database:\n  url: postgresql://aq:private@localhost:5533/isolated\n")
    with pytest.raises(ValueError, match="mcp_server.port"):
        exp.verify_target(args, {"port": 8081})


def test_experiment_config_never_mutates_worker_environment(tmp_path, monkeypatch):
    exp = load_script("experiment")
    config = tmp_path / "config.yaml"
    config.write_text("resources:\n  session_nice: 7\nmcp_server:\n  port: 8099\n")
    (tmp_path / ".env").write_text("AQ_DATABASE_URL=do-not-import\nAQ_DB_SCOPE=operator\n")
    monkeypatch.setenv("AQ_DATABASE_URL", "refusal-sentinel")
    monkeypatch.setenv("AQ_DB_SCOPE", "worker")
    parsed, mcp = exp.read_config(config)
    assert parsed.resources.session_nice == 7 and mcp["port"] == 8099
    assert os.environ["AQ_DATABASE_URL"] == "refusal-sentinel"
    assert os.environ["AQ_DB_SCOPE"] == "worker"


def test_api_probe_uses_real_contracts_and_reports_http_errors_and_missing_tasks():
    import shutil
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    if not shutil.which("node"):
        pytest.skip("node is unavailable")
    requests = []
    has_task = True

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.respond({})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            requests.append((self.path, body))
            self.respond({"tasks": [{"id": "fixture-1"}] if has_task else []})

        def respond(self, body):
            payload = json.dumps(body).encode()
            self.send_response(503 if self.path == "/ready" else 200)
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    script = (
        f"import {{probeApi}} from {json.dumps((KIT / 'api.mjs').as_uri())};"
        f"console.log(JSON.stringify(await probeApi('http://127.0.0.1:{server.server_port}',"
        "{project:'fixture',samples:2})));"
    )
    try:
        result = subprocess.run(["node", "--input-type=module", "-e", script],
                                capture_output=True, text=True, check=True, timeout=30)
        out = json.loads(result.stdout)
        assert out["GET /ready"]["errors"] == 2
        assert len(out["POST /api/task/get"]["raw_ms"]) == 2
        assert ("/api/task/get", {"task_id": "fixture-1"}) in requests
        assert ("/api/task/gate-list", {"project_id": "fixture"}) in requests
        assert all("limit" not in body for _, body in requests)
        has_task = False
        result = subprocess.run(["node", "--input-type=module", "-e", script],
                                capture_output=True, text=True, check=True, timeout=30)
        assert json.loads(result.stdout)["POST /api/task/get"]["reason"] == "no_task_id"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_experiment_writes_all_artifacts_and_keeps_client_counts_separate(tmp_path, monkeypatch):
    exp = load_script("experiment")
    target = tmp_path / "target.yaml"
    target.write_text("database:\n  url: postgresql://aq:never-record-this@localhost:5533/isolated\n"
                      "mcp_server:\n  port: 8099\nresources:\n  session_nice: 7\n")
    seed = {"ts": 1, "tasks": {"total": 10000}, "agents": {"total": 2}}
    monkeypatch.setattr(exp, "fetch_series", lambda *args: [seed])
    monkeypatch.setattr(exp, "verify_target", lambda *args: None)
    def check_output(cmd, **kwargs):
        return "Chrome/130" if cmd[-1] == "--version" else "abc123"

    monkeypatch.setattr(exp.subprocess, "check_output", check_output)
    inv = exp.kit_module("inventory")
    monkeypatch.setattr(inv, "inventory", lambda: {"processes": [], "totals": {}})
    wrapped = []
    load_module = exp.kit_module("load")

    def wrapped_argv(config, argv):
        wrapped.append((config.resources.session_nice, argv))
        return [sys.executable, "-c",
                'print(\'{"throughput_iter_per_s": 200, "timed_out": false}\')'], dict(os.environ)

    monkeypatch.setattr(load_module, "wrapped_argv", wrapped_argv)
    real_kit_module = exp.kit_module
    monkeypatch.setattr(exp, "kit_module", lambda name: {
        "load": load_module, "inventory": inv}.get(name) or real_kit_module(name))

    def harness(cmd, **kwargs):
        assert "--task-detail-only" in cmd and "--api" in cmd
        clients = int(cmd[cmd.index("--clients") + 1])
        now = time.time()
        Path(cmd[3]).write_text(json.dumps({
            "manifest": {"clients": clients}, "start_ts": now, "end_ts": now + 1,
            "cold": {"tasks": {"ready_ms": clients * 10}}, "idle": {}, "api": {},
        }))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(exp.subprocess, "run", harness)
    out = tmp_path / "out"
    assert exp.main(["--mode", "loaded", "--dashboard-url", "http://127.0.0.1:8092",
                     "--api-url", "http://127.0.0.1:8099", "--config", str(target),
                     "--out", str(out), "--warmup-ms", "0", "--observe-ms", "1"]) == 0
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["seed"] == {"tasks_total": 10000, "agents_total": 2}
    assert manifest["session_nice"] == 7 and manifest["load"]["wrapped"] is True
    assert manifest["chrome"] == "Chrome/130" and manifest["git_sha"]
    assert "never-record-this" not in (out / "manifest.json").read_text()
    assert len(wrapped) == 4  # manifest plus each repetition, through the session wrapper
    for index in (1, 2, 3):
        for prefix in ("inventory-before", "inventory-after", "load", "series"):
            assert (out / f"{prefix}-{index}.json").is_file()
        for clients in (1, 3):
            assert (out / f"harness-{index}-clients-{clients}.json").is_file()
    summary = json.loads((out / "summary.json").read_text())
    assert summary["by_clients"]["1"]["cold"]["tasks"]["ready_ms"]["raw"] == [10] * 3
    assert summary["by_clients"]["3"]["cold"]["tasks"]["ready_ms"]["raw"] == [30] * 3
    assert summary["load"]["throughput_iter_per_s"]["median"] == 200
    assert summary["loaded_observations_covered"] is False
    # A second run cannot overwrite the evidence.
    assert exp.main(["--mode", "idle", "--dashboard-url", "http://127.0.0.1:8092",
                     "--api-url", "http://127.0.0.1:8099", "--config", str(target),
                     "--out", str(out)]) == 2
