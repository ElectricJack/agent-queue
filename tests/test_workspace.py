"""Real linked-checkout reuse preserves warm output and uncertain owners."""

from unittest.mock import AsyncMock

import pytest

from src.build_cache import build_measurements, cache_counters, cache_environment, parse_counters
from src.config import AppConfig, BuildCacheConfig, load_config
from src.git.manager import GitManager
from src.warm_workspaces import WarmWorkspacePool


@pytest.fixture
async def pool(tmp_path):
    store = tmp_path / "primary"
    store.mkdir()
    git = GitManager()
    await git._arun(["init", "-b", "main"], cwd=str(store))
    (store / ".gitignore").write_text("build/\n")
    (store / "source").write_text("first")
    await git._arun(["add", "."], cwd=str(store))
    await git._arun(["-c", "user.name=Test", "-c", "user.email=test@example.test",
                     "commit", "-m", "first"], cwd=str(store))
    first = await git.arev_parse(str(store), "HEAD")
    (store / "source").write_text("second")
    await git._arun(["-c", "user.name=Test", "-c", "user.email=test@example.test",
                     "commit", "-am", "second"], cwd=str(store))
    second = await git.arev_parse(str(store), "HEAD")
    return WarmWorkspacePool(git, store, tmp_path / "warm"), first, second


async def test_reuse_shares_primary_objects_and_preserves_ignored_builds(pool):
    pool, first, second = pool
    released = AsyncMock(return_value=True)
    path = await pool.acquire({"batch": "one"}, first, released=released)
    (path / "build").mkdir()
    artifact = path / "build" / "output.o"
    artifact.write_text("cached")
    again = await pool.acquire({"batch": "two"}, second, released=released)
    assert again == path and artifact.read_text() == "cached"
    assert (again / "source").read_text() == "second"
    assert await pool.git.arev_parse(str(again), "HEAD") == second
    common = await pool.git._arun(["rev-parse", "--path-format=absolute", "--git-common-dir"],
                                  cwd=str(again))
    assert common.strip() == str(pool.store / ".git")
    assert (pool.store / "source").read_text() == "second"


@pytest.mark.parametrize("retained", ["dirty", "untracked", "unpublished", "live", "unknown"])
async def test_unreleased_work_is_never_reset(pool, retained):
    pool, first, second = pool
    released = AsyncMock(return_value=retained not in {"live", "unknown"})
    path = await pool.acquire({"batch": "one"}, first, released=released)
    if retained == "dirty":
        (path / "source").write_text("uncommitted")
    elif retained == "untracked":
        (path / "notes").write_text("keep")
    elif retained == "unpublished":
        # The owner port rejects a clean but unpushed commit.
        released.return_value = False
    again = await pool.acquire({"batch": "two"}, second, released=released)
    assert again != path
    assert await pool.git.arev_parse(str(path), "HEAD") == first
    if retained == "dirty":
        assert (path / "source").read_text() == "uncommitted"
    if retained == "untracked":
        assert (path / "notes").read_text() == "keep"


async def test_same_owner_replay_preserves_inflight_changes(pool):
    pool, first, _ = pool
    released = AsyncMock(return_value=False)
    path = await pool.acquire({"attempt": "one"}, first, released=released)
    (path / "source").write_text("in flight")
    assert await pool.acquire({"attempt": "one"}, first, released=released) == path
    assert (path / "source").read_text() == "in flight"
    released.assert_not_awaited()


async def test_ignored_file_cannot_be_overwritten_by_next_tree(pool):
    pool, first, _ = pool
    path = await pool.acquire({"batch": "one"}, first, released=AsyncMock(return_value=True))
    (path / "build").mkdir()
    (path / "build" / "output.o").write_text("warm")
    (pool.store / "build").mkdir()
    (pool.store / "build" / "output.o").write_text("now tracked")
    await pool.git._arun(["add", "-f", "build/output.o"], cwd=str(pool.store))
    await pool.git._arun(["-c", "user.name=Test", "-c", "user.email=test@example.test",
                          "commit", "-m", "track output"], cwd=str(pool.store))
    oid = await pool.git.arev_parse(str(pool.store), "HEAD")
    other = await pool.acquire({"batch": "two"}, oid, released=AsyncMock(return_value=True))
    assert other != path and (path / "build" / "output.o").read_text() == "warm"


@pytest.mark.parametrize("kind,key", [("ccache", "CCACHE_DIR"), ("sccache", "SCCACHE_DIR")])
def test_cache_config_roundtrip_and_project_override(tmp_path, kind, key):
    path = tmp_path / "config.yaml"
    path.write_text("database:\n  url: postgresql+asyncpg://test:test@localhost/test\n"
                    "discord:\n  bot_token: test\n  guild_id: '1'\n"
                    f"workspaces:\n  build_cache:\n    kind: {kind}\n    dir: /native/cache\n"
                    "  projects:\n    disabled: null\n    dedicated:\n      kind: ccache\n"
                    "      dir: /native/dedicated\n")
    config = load_config(str(path))
    assert cache_environment(config, "default") == {key: "/native/cache"}
    assert cache_environment(config, "disabled") == {}
    assert cache_environment(config, "dedicated") == {"CCACHE_DIR": "/native/dedicated"}
    assert cache_environment(AppConfig(), "any") == {}


@pytest.mark.parametrize("directory", ["", "relative", "/", "/mnt/c/cache", "C:\\cache"])
def test_cache_requires_native_absolute_directory(directory):
    assert BuildCacheConfig(dir=directory).validate()


def test_cache_counters_and_missing_measurements_remain_honest():
    assert parse_counters("ccache", "direct_cache_hit 8\npreprocessed_cache_hit 2\ncache_miss 5") == (10, 5)
    assert parse_counters("sccache", '{"stats":{"cache_hits":{"counts":{"C":8}},'
                          '"cache_misses":{"counts":{"C":2}}}}') == (8, 2)
    job = {"preset": "build", "contract": {"cwd": "/warm/candidate"}}
    assert build_measurements(job, (10, 5), (18, 7), 1.25) == {
        "workspace_path": "/warm/candidate", "rebuild_seconds": 1.25,
        "cache_hits": 8, "cache_misses": 2, "cache_hit_rate": .8, "cache_scope": "shared_cache",
    }
    for before, after in [(None, None), ((1, 1), (1, 1)), ((5, 5), (1, 2))]:
        assert "cache_hit_rate" not in build_measurements(job, before, after, 1)
    assert "rebuild_seconds" not in build_measurements(
        {"preset": "ruff", "contract": {"cwd": "/warm/candidate"}}, None, None, 1,
    )


async def test_missing_compiler_cache_tool_is_optional(tmp_path):
    assert await cache_counters({"kind": "ccache", "dir": str(tmp_path)},
                                {"PATH": str(tmp_path), "CCACHE_DIR": str(tmp_path)}) is None
    assert await cache_counters(None, {}) is None


@pytest.mark.parametrize("kind,output", [
    ("ccache", "unexpected 5"), ("ccache", "direct_cache_hit -1\n"
     "preprocessed_cache_hit 0\ncache_miss 2"),
    ("sccache", '{"stats":{"cache_hits":{},"cache_misses":{}}}'),
])
def test_incomplete_cache_counters_are_unknown(kind, output):
    with pytest.raises((KeyError, ValueError)):
        parse_counters(kind, output)


async def test_broken_owner_symlink_is_preserved(pool):
    pool, first, _ = pool
    manifests = pool.root / ".owners"
    manifests.mkdir(parents=True)
    record = manifests / (pool.key + "-0.json")
    record.symlink_to(manifests / "missing")
    path = await pool.acquire({"batch": "new"}, first, released=AsyncMock(return_value=True))
    assert path.name == pool.key + "-1"
    assert record.is_symlink()
