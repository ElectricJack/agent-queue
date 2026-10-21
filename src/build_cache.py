"""Compiler-cache environments and bounded, optional telemetry.

Counters are observed on the shared cache, never reset. Concurrent builds can
contribute to a counter delta, so its scope is explicitly shared_cache rather
than claiming the hit rate belongs exclusively to one process.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal


def cache_settings(config, project_id):
    settings = getattr(config, "workspaces", None)
    if settings is None:
        return None
    return settings.projects.get(project_id, settings.build_cache)


def cache_environment(config, project_id):
    cache = cache_settings(config, project_id)
    if cache is None:
        return {}
    return {"CCACHE_DIR" if cache.kind == "ccache" else "SCCACHE_DIR": cache.dir}


async def cache_counters(cache, env):
    """A missing tool/counter is unknown and cannot fail a build."""
    if not cache:
        return None
    argv = (["ccache", "--print-stats"] if cache["kind"] == "ccache"
            else ["sccache", "--show-stats", "--stats-format", "json"])
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            *argv, env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        async with asyncio.timeout(3):
            # Read a bounded response; a tool cannot consume unbounded memory.
            output = bytearray()
            while chunk := await process.stdout.read(65537 - len(output)):
                output.extend(chunk)
                if len(output) > 65536:
                    return None
            if await process.wait():
                return None
        return parse_counters(cache["kind"], output.decode("utf-8"))
    except (OSError, TimeoutError, ValueError, KeyError, TypeError):
        return None
    finally:
        if process is not None and process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()


def parse_counters(kind, output):
    if kind == "ccache":
        stats = dict(line.split() for line in output.splitlines() if line.strip())
        hits = int(stats["direct_cache_hit"]) + int(stats["preprocessed_cache_hit"])
        misses = int(stats["cache_miss"])
    else:
        stats = json.loads(output)["stats"]
        counts = [stats[name]["counts"].values() for name in ("cache_hits", "cache_misses")]
        if any(type(value) is not int or value < 0 for group in counts for value in group):
            raise ValueError("invalid compiler cache counters")
        hits, misses = (sum(group) for group in counts)
    if hits < 0 or misses < 0:
        raise ValueError("negative compiler cache counters")
    return hits, misses


def build_measurements(job, before, after, elapsed):
    values = {"workspace_path": job["contract"]["cwd"]}
    if job["preset"] in {"build", "pnpm_build"} and elapsed is not None:
        values["rebuild_seconds"] = max(0, elapsed)
    if before is not None and after is not None:
        hits, misses = (after[i] - before[i] for i in range(2))
        if hits >= 0 and misses >= 0 and hits + misses:
            values.update(cache_hit_rate=hits / (hits + misses), cache_hits=hits,
                          cache_misses=misses, cache_scope="shared_cache")
    return values
