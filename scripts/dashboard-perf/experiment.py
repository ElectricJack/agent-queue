#!/usr/bin/env python3
"""Run the fixed dashboard performance protocol against an isolated daemon."""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import platform
import shlex
import signal
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import fields
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
KIT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import AppConfig, ResourceCgroupConfig, ResourcesConfig  # noqa: E402
from src.metrics.histogram import count_over, merge_hists, percentile  # noqa: E402
from src.resources.limits import session_env_caps  # noqa: E402

MODES = ("idle", "loaded")
class ProtocolError(ValueError):
    """A public validation error that carries no configuration secrets."""


SURFACES = ("tasks", "agents", "metrics", "graph", "reviews", "sessions", "overview")


def kit_module(name):
    spec = importlib.util.spec_from_file_location(f"dashboard_perf_{name}", KIT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def spread(values: list[float]) -> float | None:
    values = [v for v in values if numeric(v)]
    if len(values) < 2:
        return None
    mid = statistics.median(values)
    return (max(values) - min(values)) / mid if mid != 0 else None


def stats(values):
    raw = list(values)
    valid = [v for v in raw if numeric(v)]
    return {"median": statistics.median(valid) if valid else None,
            "raw": raw, "spread": spread(valid)}


def get_path(obj, *path):
    for part in path:
        obj = obj.get(part) if isinstance(obj, dict) else None
    return obj


def validate_manifest(manifest: dict) -> list[str]:
    problems = []
    required = ("mode", "repetitions", "clients", "warmup_ms", "observe_ms", "seed", "chrome",
                "viewport", "host", "load", "caps", "session_nice", "git_sha", "pg_identity",
                "surfaces")
    for key in required:
        if key not in manifest:
            problems.append(f"missing {key}")
    if manifest.get("mode") not in (*MODES, "queued"):
        problems.append("mode must be idle, loaded or queued")
    if not isinstance(manifest.get("repetitions"), int) or manifest["repetitions"] < 3:
        problems.append("repetitions must be at least 3")
    clients = manifest.get("clients")
    if (not isinstance(clients, list) or not clients or
            any(not isinstance(n, int) or isinstance(n, bool) or n < 1 for n in clients)):
        problems.append("clients must contain positive integers")
    for key in ("warmup_ms", "observe_ms"):
        val = manifest.get(key)
        if not numeric(val) or val < (1 if key == "observe_ms" else 0):
            problems.append(f"{key} must be {'positive' if key == 'observe_ms' else 'nonnegative'}")
    for key in ("chrome", "git_sha"):
        if not isinstance(manifest.get(key), str) or not manifest[key].strip():
            problems.append(f"{key} must be populated")
    for key, names in (("seed", ("tasks_total", "agents_total")),
                       ("host", ("kernel", "cpu_count", "mem_total_mb")),
                       ("load", ("argv",)), ("pg_identity", ("same_server",))):
        if not isinstance(manifest.get(key), dict):
            problems.append(f"{key} must be an object")
        else:
            for name in names:
                if name not in manifest[key] or (manifest[key][name] is None and key != "pg_identity"):
                    problems.append(f"{key}.{name} must be populated")
    if not isinstance(manifest.get("caps"), dict):
        problems.append("caps must be an object")
    if not numeric(manifest.get("session_nice")):
        problems.append("session_nice must be numeric")
    vp = manifest.get("viewport")
    if not isinstance(vp, list) or len(vp) != 2 or any(not numeric(v) or v <= 0 for v in vp):
        problems.append("viewport must have two positive dimensions")
    surfaces = manifest.get("surfaces")
    if (not isinstance(surfaces, list) or not surfaces or
            any(s not in SURFACES for s in surfaces)):
        problems.append("surfaces must name measured surfaces")
    for key in ("tasks_total", "agents_total"):
        value = get_path(manifest, "seed", key)
        if not numeric(value) or value < 0:
            problems.append(f"seed.{key} must be nonnegative")
    for key in ("cpu_count", "mem_total_mb"):
        value = get_path(manifest, "host", key)
        if not numeric(value) or value <= 0:
            problems.append(f"host.{key} must be positive")
    if not isinstance(get_path(manifest, "host", "kernel"), str):
        problems.append("host.kernel must be a string")
    argv = get_path(manifest, "load", "argv")
    if not isinstance(argv, list) or not argv or any(not isinstance(v, str) for v in argv):
        problems.append("load.argv must be a nonempty string list")
    return problems


def numeric_summary(objects):
    """Median and spread of each numeric leaf; raw client/timing arrays stay in run files."""
    keys = dict.fromkeys(k for obj in objects if isinstance(obj, dict) for k in obj)
    out = {}
    for key in keys:
        values = [obj.get(key) if isinstance(obj, dict) else None for obj in objects]
        if any(numeric(v) for v in values):
            out[key] = stats(values)
        elif any(isinstance(v, dict) for v in values):
            nested = numeric_summary(values)
            if nested:
                out[key] = nested
    return out


def daemon_summary(samples):
    perfs = [s["perf"] for s in samples if get_path(s, "perf", "enabled") is True]
    def hist(*path):
        return merge_hists(get_path(p, *path) for p in perfs)

    drift = hist("loop", "drift")
    sampler = [get_path(p, "sampler", "perf_ms") for p in perfs]
    sampler = sorted(v for v in sampler if numeric(v))
    psi = {}
    for resource in ("cpu", "io", "memory"):
        psi[resource] = {}
        for kind in ("some_avg10", "full_avg10"):
            vals = [get_path(p, "host", "psi", resource, kind) for p in perfs]
            psi[resource][kind] = max((v for v in vals if numeric(v)), default=None)
    ungated = [get_path(p, "host", "ungated", "pytest_processes") for p in perfs]
    return {
        "samples": len(samples), "perf_samples": len(perfs),
        "loop_drift_p95_ms": percentile(drift, .95),
        "loop_drift_max_ms": drift["max"] if drift["count"] else None,
        "stalls_over_500ms": count_over(drift, 500) if drift["count"] else None,
        "api_all_p95_ms": percentile(hist("api", "all"), .95),
        "pool_wait_p95_ms": percentile(hist("db", "pool_wait"), .95),
        "query_p95_ms": percentile(hist("db", "query"), .95),
        "relay_p95_ms": percentile(hist("relay", "http"), .95),
        "sampler_perf_ms_p95": sampler[math.ceil(len(sampler) * .95) - 1] if sampler else None,
        "psi_max": psi,
        "ungated_processes_max": max((v for v in ungated if numeric(v)), default=None),
    }


def browser_windows(runs):
    """Each harness run's complete interval: cold/warm loads through its direct API reads."""
    return [{"clients": get_path(r, "manifest", "clients"), "start_ts": r["start_ts"],
             "end_ts": r["end_ts"]}
            for r in runs if numeric(r.get("start_ts")) and numeric(r.get("end_ts"))]


def browser_active(samples, windows):
    """1 s samples stamped inside a browser window; warm-up and helper-only tail fall outside."""
    return [s for s in samples if numeric(s.get("ts")) and
            any(w["start_ts"] <= s["ts"] <= w["end_ts"] for w in windows)]


def helper_only_s(runs, ended):
    """Seconds a workload ran after its repetition's last browser window."""
    last = max((r["end_ts"] for r in runs if numeric(r.get("end_ts"))), default=None)
    return round(max(0, ended - last), 3) if numeric(ended) and last is not None else None


def daemon_scope(series):
    # Buckets add across seconds/repetitions. Never average percentiles.
    repetitions = [daemon_summary(group) for group in series]
    return {"daemon": daemon_summary([s for group in series for s in group]),
            "daemon_repetitions": repetitions, "daemon_spread": numeric_summary(repetitions)}


def summarize(runs: list[dict], loads: list[dict], series: list[list[dict]]) -> dict:
    out = {}
    for phase in ("cold", "warm", "idle", "api"):
        out[phase] = numeric_summary([r.get(phase, {}) for r in runs])
    labels = dict.fromkeys(i["label"] for r in runs for group in r.get("interactions", [])
                           for i in group)
    out["interactions"] = {
        label: numeric_summary([
            i for r in runs for group in r.get("interactions", []) for i in group
            if i["label"] == label]) for label in labels
    }
    out["api_raw_ms"] = {
        label: [get_path(r, "api", label, "raw_ms") for r in runs]
        for label in dict.fromkeys(k for r in runs for k in r.get("api", {}))
    }
    out["load"] = {"throughput_iter_per_s": stats([load.get("throughput_iter_per_s") for load in loads]),
                   "timed_out": [load.get("timed_out") for load in loads],
                   "timed_out_count": sum(load.get("timed_out") is True for load in loads),
                   "repetitions": loads}
    # The envelope is judged while browsers run. A loaded repetition also waits out its
    # helper, so the whole-repetition series can end in a quiet tail idle never has.
    active, out["activity"] = [], []
    for index, samples in enumerate(series, 1):
        load = loads[index - 1] if index <= len(loads) else {}
        windows = browser_windows(r for r in runs if r.get("repetition") == index)
        active.append(browser_active(samples, windows))
        out["activity"].append({
            "repetition": index, "browser_windows": windows, "samples": len(samples),
            "browser_active_samples": len(active[-1]),
            "excluded_samples": len(samples) - len(active[-1]),
            "helper_only_s": load.get("helper_only_s")})
    out["daemon_scope"] = "browser_active"
    out.update(daemon_scope(active))
    out["whole_repetition"] = daemon_scope(series)
    return out


def fetch_json(url):
    with urlopen(url, timeout=30) as response:
        return json.load(response)


def fetch_series(base, start=None, end=None):
    query = {"step": "1s"}
    if start is not None:
        query["from"] = start
    if end is not None:
        query["to"] = end
    response = fetch_json(base.rstrip("/") + "/api/metrics/series?" + urlencode(query))
    if response.get("step") != "1s" or response.get("truncated"):
        raise ProtocolError("metrics series must be untruncated 1s samples")
    if not isinstance(response.get("samples"), list):
        raise ProtocolError("metrics series must contain samples")
    return response["samples"]


def load_args(args):
    return shlex.split(args.load_args) if args.load_args is not None else [
        "--seconds", "120", "--iterations", "200000000", "--io-bytes", "1073741824",
        "--tmp-max-mib", "256", "--workers", str(min(4, os.cpu_count() or 1)),
    ]


def workload_argv(args, config):
    if args.workload_cmd:
        return shlex.split(args.workload_cmd), dict(os.environ)
    return kit_module("load").wrapped_argv(
        config, [sys.executable, str(KIT / "load.py"), *load_args(args)])


def build_manifest(args, *, config, chrome_version: str) -> dict:
    pg = kit_module("pg_identity")
    daemon_dsn, reason = pg.daemon_dsn_from_config(args.config)
    newest = max(fetch_series(args.api_url), key=lambda s: s["ts"], default={})
    mem = next((line.split()[1] for line in Path("/proc/meminfo").read_text().splitlines()
                if line.startswith("MemTotal:")), None)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    argv, _ = workload_argv(args, config)
    identity = pg.compare(os.environ.get("POSTGRES_TEST_DSN"), daemon_dsn)
    if reason:
        identity["daemon_reason"] = reason
    return {
        "mode": args.mode, "repetitions": args.repetitions, "clients": args.clients,
        "warmup_ms": args.warmup_ms, "observe_ms": args.observe_ms,
        "surfaces": args.surfaces, "project": args.project,
        "dashboard_url": args.dashboard_url, "api_url": args.api_url,
        "seed": {"tasks_total": get_path(newest, "tasks", "total"),
                 "agents_total": get_path(newest, "agents", "total")},
        "chrome": chrome_version, "viewport": [1600, 1000],
        "host": {"kernel": platform.release(), "cpu_count": os.cpu_count(),
                 "mem_total_mb": int(mem) / 1024 if mem else None},
        "load": {"argv": argv, "args": load_args(args), "active": args.mode != "idle",
                 "wrapped": not bool(args.workload_cmd),
                 "timeout_s": workload_timeout(args)},
        "caps": session_env_caps(config), "session_nice": config.resources.session_nice,
        "parent_nice": os.nice(0), "git_sha": sha, "pg_identity": identity,
        "network": "loopback; LAN/tailnet delay not measured",
    }


def read_config(path):
    """Only experiment factors; do not import a daemon .env into a worker's environment."""
    import yaml
    from src.config import _deep_merge

    pg = kit_module("pg_identity")
    raw = yaml.safe_load(path.read_text()) or {}
    env = {**os.environ, **pg._read_dotenv(path.parent / ".env")}
    name = env.get("AGENT_QUEUE_ENV", raw.get("env", "production"))
    overlays = [path.with_name(f"{path.stem}.{name}{path.suffix}")]
    if env.get("AGENT_QUEUE_PROFILE"):
        overlays.append(path.parent / "profiles" / f"{env['AGENT_QUEUE_PROFILE']}.yaml")
    for overlay in overlays:
        if overlay.exists():
            raw = _deep_merge(raw, yaml.safe_load(overlay.read_text()) or {})
    def resolve(value):
        if isinstance(value, str):
            return pg._PLACEHOLDER_RE.sub(lambda m: env[m[1]], value)
        if isinstance(value, dict):
            return {k: resolve(v) for k, v in value.items()}
        return value
    # Resolve only public experiment factors, never unrelated secrets.
    res = resolve(raw.get("resources", {}))
    cg = res.pop("cgroups", {})
    config = AppConfig()
    config.resources = ResourcesConfig(
        **{f.name: res[f.name] for f in fields(ResourcesConfig) if f.name in res},
        cgroups=ResourceCgroupConfig(**cg),
    )
    errors = config.resources.validate()
    if errors:
        raise ProtocolError("invalid resources config")
    return config, resolve(raw.get("mcp_server", {}))


def endpoint(url):
    """Loopback HTTP only for the controlled local experiment; no credentials in artifacts."""
    try:
        parsed = urlsplit(url)
        port = parsed.port or 80
    except ValueError:
        raise ProtocolError("invalid experiment URL") from None
    if (parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ("", "/")):
        raise ProtocolError("experiment URLs must be loopback HTTP origins without credentials")
    return ("loopback", port)


def verify_target(args, mcp):
    pg = kit_module("pg_identity")
    target, reason = pg.daemon_dsn_from_config(args.config)
    operator, op_reason = pg.daemon_dsn_from_config(pg.DEFAULT_CONFIG)
    if reason or op_reason:
        raise ProtocolError(f"cannot verify isolated database: {reason or op_reason}")
    comparison = pg.compare(target, operator)
    if comparison["same_database"] is not False:
        raise ProtocolError("refusing experiment on the operator's database")
    if endpoint(args.api_url)[1] != int(mcp.get("port", 8081)):
        raise ProtocolError("api-url does not match the isolated config's mcp_server.port")
    if endpoint(args.api_url) == endpoint(args.dashboard_url):
        raise ProtocolError("dashboard-url must be the separate dashboard server")
    edge = fetch_json(args.dashboard_url.rstrip("/") + "/__aq/health")
    if endpoint(edge.get("api_url", "")) != endpoint(args.api_url) or not edge.get("upstream_ok"):
        raise ProtocolError("dashboard server must relay to the ready isolated API")


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def workload_timeout(args):
    if args.workload_timeout_s is not None:
        return args.workload_timeout_s
    if args.workload_cmd:
        return 3600.0
    argv = load_args(args)
    return float(argv[argv.index("--seconds") + 1]) + 30 if "--seconds" in argv else 150.0


class Workload:
    """A bounded process group, with exit time observed while the browser is running."""

    def __init__(self, argv, env, out, timeout):
        self.stdout = out.open("w")
        self.stderr = out.with_suffix(".stderr.log").open("w")
        self.started = time.time()
        self.ended = None
        self.timed_out = False
        try:
            self.proc = subprocess.Popen(argv, env=env, stdout=self.stdout, stderr=self.stderr,
                                         text=True, start_new_session=True)
        except BaseException:
            self.stdout.close()
            self.stderr.close()
            raise
        self.thread = threading.Thread(target=self.watch, args=(timeout,), daemon=True)
        self.thread.start()

    def stop(self):
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        # The parent may exit before one of its children; stop the entire group.
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.proc.wait()

    def watch(self, timeout):
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.timed_out = True
            self.stop()
        finally:
            self.ended = time.time()

    def finish(self, *, stop=False):
        if stop:
            self.stop()
        self.thread.join()
        self.stdout.close()
        self.stderr.close()


def coverage(runs, start, end):
    out = []
    for run in runs:
        if run.get("start_ts") is not None:
            a, b = run["start_ts"], run["end_ts"]
            out.append({"clients": run["manifest"]["clients"], "surface": "browser_run",
                        "fraction": max(0, min(b, end) - max(a, start)) / (b - a) if b > a else 0})
        for surface, observation in run.get("idle", {}).items():
            for index, client in enumerate(observation.get("clients", [])):
                a, b = client["start_ts"], client["end_ts"]
                overlap = max(0, min(b, end) - max(a, start))
                out.append({"clients": run["manifest"]["clients"], "surface": surface,
                            "client": index, "fraction": overlap / (b - a) if b > a else 0})
        if run.get("api_start_ts") is not None:
            a, b = run["api_start_ts"], run["api_end_ts"]
            out.append({"clients": run["manifest"]["clients"], "surface": "direct_api",
                        "fraction": max(0, min(b, end) - max(a, start)) / (b - a) if b > a else 0})
    return out


def run_repetition(args, config, index):
    inv = kit_module("inventory")
    write_json(args.out / f"inventory-before-{index}.json", inv.inventory())
    start = time.time()
    workload = None
    runs = []
    try:
        if args.mode != "idle":
            argv, env = workload_argv(args, config)
            workload = Workload(argv, env, args.out / f"workload-{index}.stdout.log",
                                workload_timeout(args))
        time.sleep(args.warmup_ms / 1000)
        for clients in args.clients:
            out = args.out / f"harness-{index}-clients-{clients}.json"
            cmd = ["node", str(KIT / "harness.mjs"), args.dashboard_url, str(out),
                   "--api", args.api_url, "--clients", str(clients),
                   "--warmup-ms", str(args.warmup_ms), "--observe-ms", str(args.observe_ms),
                   "--runs", "1", "--only", ",".join(args.surfaces), "--project", args.project]
            cmd.append("--task-detail-only" if "tasks" in args.surfaces else "--no-interactions")
            with (args.out / f"harness-{index}-clients-{clients}.log").open("w") as log:
                subprocess.run(cmd, check=True, stdout=log, stderr=log,
                               timeout=workload_timeout(args) + 3600)
            run = json.loads(out.read_text())
            run["repetition"] = index
            runs.append(run)
    finally:
        if workload:
            workload.finish(stop=sys.exc_info()[0] is not None)
        end = time.time()
        write_json(args.out / f"inventory-after-{index}.json", inv.inventory())
        load = {"active": workload is not None, "timed_out": None, "throughput_iter_per_s": None}
        if workload:
            log = args.out / f"workload-{index}.stdout.log"
            try:
                load.update(json.loads(log.read_text().strip().splitlines()[-1]))
            except (ValueError, IndexError, TypeError):
                load["reason"] = "no_load_summary"
            load.update(start_ts=workload.started, end_ts=workload.ended,
                        exit_code=workload.proc.returncode,
                        wrapper_timed_out=workload.timed_out,
                        coverage=coverage(runs, workload.started, workload.ended),
                        helper_only_s=helper_only_s(runs, workload.ended))
            if workload.timed_out:
                load["timed_out"] = True
            load["covers_observations"] = bool(load["coverage"]) and all(
                c["fraction"] >= .99 for c in load["coverage"])
        write_json(args.out / f"load-{index}.json", load)
    samples = fetch_series(args.api_url, start, end)
    write_json(args.out / f"series-{index}.json", samples)
    return runs, load, samples


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=(*MODES, "queued"), required=True)
    parser.add_argument("--dashboard-url", required=True)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--clients", default="1,3")
    parser.add_argument("--warmup-ms", type=int, default=30000)
    parser.add_argument("--observe-ms", type=int, default=120000)
    parser.add_argument("--surfaces", default="tasks,agents,metrics")
    parser.add_argument("--project", default="agent-queue")
    parser.add_argument("--workload-cmd")
    parser.add_argument("--workload-timeout-s", type=float)
    parser.add_argument("--load-args")
    parser.add_argument("--config", type=Path, default=Path(
        os.environ.get("AQ_E2E_HOME", str(Path.home() / ".agent-queue-e2e"))) / "config.yaml")
    args = parser.parse_args(argv)
    if args.mode == "queued" and not args.workload_cmd:
        print("the exclusive job queue is not implemented on this checkout; pass --workload-cmd "
              "once aq job submit exists", file=sys.stderr)
        return 2
    try:
        args.config = args.config.expanduser()
        args.clients = [int(n) for n in args.clients.split(",")]
        args.surfaces = args.surfaces.split(",")
        if (args.repetitions < 3 or any(n < 1 for n in args.clients) or
                len(set(args.clients)) != len(args.clients) or args.warmup_ms < 0 or
                args.observe_ms <= 0 or any(s not in SURFACES for s in args.surfaces) or
                not math.isfinite(workload_timeout(args)) or workload_timeout(args) <= 0):
            raise ProtocolError("invalid repetitions, clients, surfaces or duration")
        endpoint(args.api_url)
        endpoint(args.dashboard_url)
        config, mcp = read_config(args.config)
        verify_target(args, mcp)
        chrome = subprocess.check_output(
            [os.environ.get("CHROME", "/usr/bin/google-chrome"), "--version"],
            text=True, timeout=30).strip()
        manifest = build_manifest(args, config=config, chrome_version=chrome)
        problems = validate_manifest(manifest)
        if problems:
            raise ProtocolError("; ".join(problems))
        args.out = args.out.expanduser()
        args.out.mkdir(parents=True, exist_ok=True)
        if any(args.out.iterdir()):
            raise ProtocolError("output directory must be empty; preserve prior experiment artifacts")
        write_json(args.out / "manifest.json", manifest)
        runs, loads, series = [], [], []
        for index in range(1, args.repetitions + 1):
            batch, load, samples = run_repetition(args, config, index)
            runs.extend(batch)
            loads.append(load)
            series.append(samples)
        summary = summarize(runs, loads, series)
        summary["by_clients"] = {
            str(n): summarize([r for r in runs if r["manifest"]["clients"] == n], loads, series)
            for n in args.clients}
        summary["loaded_observations_covered"] = (
            all(load.get("covers_observations") for load in loads) if args.mode != "idle" else None)
        write_json(args.out / "summary.json", summary)
        print(args.out)
        whole = summary["whole_repetition"]["daemon"]
        print(f"loop p95, browser-active: {summary['daemon']['loop_drift_p95_ms']} ms "
              f"(whole repetition: {whole['loop_drift_p95_ms']} ms)")
        print(f"API p95, browser-active: {summary['daemon']['api_all_p95_ms']} ms "
              f"(whole repetition: {whole['api_all_p95_ms']} ms)")
        print(f"helper-only tail: {[a['helper_only_s'] for a in summary['activity']]} s")
        detail = get_path(summary, "interactions", "tasks: click task row → pane",
                          "visible_ms", "median")
        print(f"task-detail visible median: {detail} ms")
        print(f"throughput: {summary['load']['throughput_iter_per_s']['median']} iter/s")
        print(f"timed_out count: {summary['load']['timed_out_count']}")
        if summary["loaded_observations_covered"] is False:
            print("Workload did not cover every observation; calibrate fixed load arguments "
                  "before claiming loaded results.", file=sys.stderr)
        return 0
    except ProtocolError as exc:
        print(f"Experiment refused: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError):
        # Avoid echoing config/DSN content or arbitrary subprocess arguments.
        print("Experiment failed; check isolated config, arguments and artifact logs.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
