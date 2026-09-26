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
