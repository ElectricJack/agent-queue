"""Read-only token-efficiency report: corrected accounting, attribution, halts, export."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import pytest

from src.database import Database
from src.metrics.token_efficiency import (
    DEFAULT_THRESHOLDS,
    Call,
    TranscriptUsage,
    attempt_spans,
    build_report,
    classify_tool,
    claude_transcript_paths,
    compare,
    export_rows,
    load_transcript,
    read_claude,
    read_codex,
)
from src.models import Project, Task, TaskStatus
from tests.db_fixtures import lease_dsn

T0 = 1_790_000_000.0


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat().replace("+00:00", "Z")


def _claude_row(ts, api_id, uuid, *, read=0, write=0, inp=1, out=1, tools=()):
    content = [{"type": "text", "text": "x"}]
    content += [{"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}
                for tool_id, name, tool_input in tools]
    return json.dumps({
        "type": "assistant", "uuid": uuid, "timestamp": _iso(ts),
        "message": {"id": api_id, "model": "claude-opus-5-5", "content": content,
                    "usage": {"input_tokens": inp, "output_tokens": out,
                              "cache_read_input_tokens": read,
                              "cache_creation_input_tokens": write}},
    }) + "\n"


def _codex_lines(*records):
    return [json.dumps(record) + "\n" for record in records]


def _usage(response_id, ts, *, inp, cached, out):
    return {"type": "token_usage_record", "timestamp": _iso(ts),
            "payload": {"response_id": response_id,
                        "usage": {"input_tokens": inp, "cached_input_tokens": cached,
                                  "cache_write_input_tokens": 0, "output_tokens": out}}}


def _token_count(ts, total, last, used_percent=40.0):
    return {"type": "event_msg", "timestamp": _iso(ts),
            "payload": {"type": "token_count",
                        "info": {"total_token_usage": total, "last_token_usage": last},
                        "rate_limits": {"primary": {"used_percent": used_percent,
                                                    "window_minutes": 10080}}}}


class TestClaudeAccounting:
    def test_repeated_content_blocks_of_one_call_count_once_at_their_maximum(self):
        lines = [
            _claude_row(T0, "msg_1", "u1", read=1000, write=50, out=5),
            _claude_row(T0 + 1, "msg_1", "u2", read=1000, write=50, out=90),
            _claude_row(T0 + 2, "msg_2", "u3", read=1100, write=0, out=7),
            _claude_row(T0 + 3, "msg_3", "u4", inp=0, out=0).replace(
                "claude-opus-5-5", "<synthetic>"),
        ]
        usage = read_claude(lines)
        assert [call.usage for call in usage.calls] == [
            {"uncached_input": 1, "cache_read": 1000, "cache_write": 50, "output": 90},
            {"uncached_input": 1, "cache_read": 1100, "cache_write": 0, "output": 7},
        ]
        assert usage.calls[0].context == 1051

    def test_tool_uses_dedupe_by_id_and_partial_lines_are_skipped(self):
        bash = ("t1", "Bash", {"command": "sleep 30 && aq --json task show x"})
        lines = [
            _claude_row(T0, "msg_1", "u1", tools=[bash]).encode(),
            _claude_row(T0 + 1, "msg_1", "u2", tools=[bash]).encode(),
            json.dumps({"type": "system", "subtype": "compact_boundary",
                        "timestamp": _iso(T0 + 2)}).encode() + b"\n",
            b'{"type": "assistant", "message": {"id": "msg_9"',
        ]
        usage = read_claude(lines)
        assert len(usage.tools) == 1
        assert classify_tool(*usage.tools[0][1:]) == {"sleep", "status_poll"}
        assert usage.compactions == [T0 + 2]
        assert usage.skipped_lines == 1
        assert len(usage.calls) == 1


class TestCodexAccounting:
    def test_one_call_per_response_and_cached_input_is_split_out(self):
        lines = _codex_lines(
            {"type": "turn_context", "timestamp": _iso(T0), "payload": {"model": "gpt-6.1-sol"}},
            _usage("r1", T0 + 1, inp=1000, cached=800, out=10),
            _usage("r1", T0 + 2, inp=1000, cached=800, out=4),
            _usage("r2", T0 + 3, inp=1500, cached=1400, out=20),
            _token_count(T0 + 3, {"total_tokens": 1}, {"input_tokens": 1}, used_percent=41.0),
            {"type": "response_item", "timestamp": _iso(T0 + 4),
             "payload": {"type": "custom_tool_call", "call_id": "c1", "name": "exec",
                         "input": 'tools.exec_command({cmd:"tail -5 /tmp/run.log"})'}},
        )
        usage = read_codex(lines)
        assert [call.usage for call in usage.calls] == [
            {"uncached_input": 200, "cache_read": 800, "cache_write": 0, "output": 10},
            {"uncached_input": 100, "cache_read": 1400, "cache_write": 0, "output": 20},
        ]
        assert {call.model for call in usage.calls} == {"gpt-6.1-sol"}
        assert usage.quota == [(T0 + 3, 41.0, 10080)]
        assert classify_tool(*usage.tools[0][1:]) == {"output_poll"}

    def test_older_rollouts_fall_back_to_changes_in_cumulative_totals(self):
        first = {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 5}
        second = {"input_tokens": 300, "cached_input_tokens": 90, "output_tokens": 9}
        lines = _codex_lines(
            _token_count(T0, first, first),
            _token_count(T0 + 1, first, first),  # repeated emission, same totals
            _token_count(T0 + 2, second, {"input_tokens": 200, "cached_input_tokens": 90,
                                          "output_tokens": 4}),
        )
        assert [call.usage for call in read_codex(lines).calls] == [
            {"uncached_input": 100, "cache_read": 0, "cache_write": 0, "output": 5},
            {"uncached_input": 110, "cache_read": 90, "cache_write": 0, "output": 4},
        ]


class TestClassification:
    @pytest.mark.parametrize(("name", "text", "expected"), [
        ("Bash", "aq task close --outcome pass --claim-next --wait 60", {"close", "claim"}),
        ("Bash", "aq wait register --idempotency-key k", {"durable_wait"}),
        ("Bash", "aq test --aq-detach --aq-wait tests/test_x.py", {"durable_wait", "test"}),
        ("Bash", "gh pr checks 12", {"status_poll"}),
        ("Bash", "aq message inbox", {"inbox"}),
        ("Bash", "tail -5 src/main.py; sed -n 1,9p notes.log", set()),
        ("Read", "/tmp/claude/tasks/b1.output", {"output_poll"}),
        ("TaskOutput", "", {"output_poll"}),
        ("Bash", "aq task comment x --body 'see task show'", set()),
    ])
    def test_categories_stay_within_one_shell_segment(self, name, text, expected):
        assert classify_tool(name, text) == expected


def test_transcript_paths_follow_the_work_dir_slug_and_include_subagents(tmp_path):
    work_dir = "/home/u/repo/.aq/worktrees/slot-3"
    folder = tmp_path / "-home-u-repo--aq-worktrees-slot-3"
    (folder / "s1" / "subagents").mkdir(parents=True)
    (folder / "s1.jsonl").write_text(_claude_row(T0, "m1", "u1", read=10))
    (folder / "s1" / "subagents" / "agent-a.jsonl").write_text(
        _claude_row(T0 + 5, "m2", "u2", read=20))
    assert claude_transcript_paths(tmp_path, work_dir, "s1")[0] == folder / "s1.jsonl"
    usage = load_transcript({"harness": "claude", "work_dir": work_dir, "session_key": "s1"},
                            claude_root=tmp_path, codex_root=tmp_path)
    assert [call.subagent for call in usage.calls] == [False, True]
    assert load_transcript({"harness": "claude", "work_dir": work_dir, "session_key": "nope"},
                           claude_root=tmp_path, codex_root=tmp_path) is None


def test_first_attempt_owns_start_up_and_later_attempts_split_the_conversation():
    spans = attempt_spans([
        {"id": "a2", "session_id": "s", "started_at": T0 + 100, "session_started_at": T0},
        {"id": "a1", "session_id": "s", "started_at": T0 + 10, "session_started_at": T0},
        {"id": "b1", "session_id": "t", "started_at": T0 + 50, "session_started_at": T0 + 40},
    ])
    assert spans == {"a1": (T0, T0 + 100), "a2": (T0 + 100, float("inf")),
                     "b1": (T0 + 40, float("inf"))}


def _attempt(attempt_id, task_id, *, start, end, outcome="pass", session=None,
             harness="claude", task_type="bugfix", end_reason="session_close"):
    return {"id": attempt_id, "session_id": session or attempt_id, "task_id": task_id,
            "harness": harness, "model": "m", "intelligence_class": "standard-high",
            "profile_id": "p", "task_type": task_type, "outcome": outcome,
            "end_reason": end_reason, "started_at": start, "session_started_at": start,
            "ended_at": end, "session_key": attempt_id, "work_dir": "/w"}


def _calls(*specs):
    usage = TranscriptUsage()
    for ts, read in specs:
        usage.calls.append(Call(ts, "m", {"uncached_input": 1, "cache_read": read,
                                          "cache_write": 2, "output": 3}))
    usage.tools.append((specs[0][0], "Bash", "sleep 5"))
    return usage


def _export(attempts, *, completions=(), routes=(), open_attempts=(), now=None):
    return {"since": T0, "until": T0 + 86_400, "exported_at": now or T0 + 86_400,
            "attempts": list(attempts), "session_attempts": list(attempts),
            "completions": list(completions), "routes": list(routes),
            "open_attempts": list(open_attempts)}


def _report(export, transcripts, **thresholds):
    return build_report(export, claude_root=Path("/none"), codex_root=Path("/none"),
                        thresholds=thresholds,
                        loader=lambda attempt, **_: transcripts.get(attempt["id"]))


class TestReport:
    def test_cohort_metrics_exclude_churn_from_latency_and_count_repairs_by_task(self):
        attempts = [
            _attempt("a1", "t1", start=T0, end=T0 + 600),
            _attempt("a2", "t2", start=T0 + 10, end=T0 + 1800, outcome="fail"),
            _attempt("a3", "repair-x-0", start=T0 + 20, end=T0 + 22, outcome=None,
                     task_type=None, end_reason="slot_reset_failed"),
            _attempt("a4", "repair-x-0", start=T0 + 30, end=T0 + 500, task_type=None),
        ]
        report = _report(_export(attempts, completions=[
            {"task_id": "t1", "outcome": "pass", "completed_at": T0 + 600},
        ], routes=[{"task_id": "t1", "task_type": "bugfix", "provider": "codex",
                    "profile_id": "standard-high-codex"}]), {
            "a1": _calls((T0 + 1, 40_000), (T0 + 2, 90_000)),
            "a2": _calls((T0 + 11, 50_000)),
            "a3": _calls((T0 + 21, 9_999_999)),
            "a4": _calls((T0 + 31, 70_000)),
        })
        bugfix = report["cohorts"]["claude|m|standard-high|bugfix"]
        assert bugfix["pass_rate"] == 0.5
        assert bugfix["calls_per_attempt"]["median"] == 1.5
        assert bugfix["startup_context"]["median"] == 45_003
        assert bugfix["tokens_total"]["cache_read"] == 180_000
        assert bugfix["polls_per_attempt"] == 1.0
        assert bugfix["duration_s"]["median"] == 1195
        repair = report["cohorts"]["claude|m|standard-high|integration-repair"]
        assert repair["churn"] == 1 and repair["duration_s"]["median"] == 470
        assert repair["with_transcript"] == 1  # the churned attempt is not a measured task
        assert report["integration_repair_share"] == round(1 / 3, 4)
        assert report["churn_share"] == 0.25
        assert report["routes"]["by_kind_provider"] == {"bugfix|codex": 1}
        assert repair["tokens_total"]["cache_read"] == 70_000
        assert report["missing_transcripts"] == {}
        assert report["completions_in_window"] == {"pass": 1}

    def test_ledger_is_checked_against_corrected_transcript_totals(self):
        attempts = [_attempt("a1", "t1", start=T0, end=T0 + 600),
                    _attempt("a2", "t2", start=T0 + 5, end=T0 + 700)]
        export = _export(attempts)
        export["ledger"] = [{"attempt_id": "a1", "rows": 4, "uncached_input": 2,
                             "cache_read": 260_000, "cache_write": 4, "output": 6}]
        report = _report(export, {"a1": _calls((T0 + 1, 40_000), (T0 + 2, 90_000)),
                                  "a2": _calls((T0 + 6, 10))})
        assert report["ledger_vs_transcript"]["claude"] == {
            "attempts": 1,
            "ledger": {"uncached_input": 2, "cache_read": 260_000, "cache_write": 4, "output": 6},
            "transcript": {"uncached_input": 2, "cache_read": 130_000, "cache_write": 4,
                           "output": 6},
            "ratio": {"uncached_input": 1.0, "cache_read": 2.0, "cache_write": 1.0,
                      "output": 1.0},
        }

    def test_calls_after_the_export_are_not_counted(self):
        attempts = [_attempt("a1", "t1", start=T0, end=None, outcome=None)]
        report = _report(_export(attempts, now=T0 + 100),
                         {"a1": _calls((T0 + 1, 10), (T0 + 99, 20), (T0 + 101, 30))})
        assert report["attempts"][0]["calls"] == 2
        assert report["attempts"][0]["duration_s"] == 100

    def test_only_live_attempts_count_as_stuck(self):
        now = T0 + 86_400
        report = _report(_export([], now=now, open_attempts=[
            {"id": "o1", "state": "running", "session_state": "running", "started_at": T0},
            {"id": "o2", "state": "stopped", "session_state": "stopped", "started_at": T0},
            {"id": "o3", "state": "running", "session_state": "stopped", "started_at": T0},
            {"id": "o4", "state": "running", "session_state": "running",
             "started_at": now - 60},
        ]), {})
        assert [row["id"] for row in report["stuck_attempts_now"]] == ["o1"]
        assert report["orphaned_open_attempts"] == 2


class TestCompare:
    def _window(self, *, passes, fails, duration, repairs=0, churn=0):
        attempts = [
            _attempt(f"p{i}", f"t{i}", start=T0 + i, end=T0 + i + duration)
            for i in range(passes)
        ] + [
            _attempt(f"f{i}", f"u{i}", start=T0 + i, end=T0 + i + duration, outcome="fail")
            for i in range(fails)
        ] + [
            _attempt(f"r{i}", f"repair-{i}", start=T0 + i, end=T0 + i + 300, task_type=None)
            for i in range(repairs)
        ] + [
            _attempt(f"c{i}", f"v{i}", start=T0 + i, end=T0 + i + 2, outcome=None,
                     task_type="feature")
            for i in range(churn)
        ]
        return _report(_export(attempts), {})

    def test_matched_cohorts_within_limits_do_not_halt(self):
        result = compare(self._window(passes=9, fails=1, duration=600),
                         self._window(passes=9, fails=1, duration=700))
        assert not result["halt"]
        assert result["matched"]["claude|m|standard-high|bugfix"]["duration_s"] == [600, 700]

    def test_quality_latency_repair_and_churn_regressions_halt_expansion(self):
        result = compare(self._window(passes=9, fails=1, duration=600),
                         self._window(passes=7, fails=3, duration=1200, repairs=4, churn=3))
        reasons = " ".join(result["halt_reasons"])
        assert result["halt"]
        assert "pass rate 0.90 -> 0.70" in reasons
        assert "median duration 600s -> 1200s" in reasons
        assert "integration repair share" in reasons
        assert "no-outcome churn share" in reasons

    def test_small_cohorts_are_reported_insufficient_not_compared(self):
        result = compare(self._window(passes=2, fails=0, duration=600),
                         self._window(passes=1, fails=1, duration=6000))
        assert result["matched"] == {}
        assert result["insufficient"] == ["claude|m|standard-high|bugfix"]
        assert DEFAULT_THRESHOLDS["min_attempts"] == 5


async def test_export_rejects_an_empty_window():
    async def fetch(*_args):
        raise AssertionError("no query for an invalid window")

    with pytest.raises(ValueError):
        await export_rows(fetch, since=T0, until=T0, now=T0)


async def test_export_reads_attempts_routes_and_completions_in_a_read_only_transaction():
    dsn = lease_dsn("token-efficiency")
    database = Database(dsn)
    await database.initialize()
    try:
        await database.create_project(Project(id="p-1", name="p-1"))
        await database.create_task(Task(id="t-live", project_id="p-1", title="live",
                                        description="d", status=TaskStatus.READY))
    finally:
        await database.close()
    connection = await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        await connection.execute(
            "UPDATE tasks SET task_type = 'bugfix', route = $1::jsonb WHERE id = 't-live'",
            json.dumps({"provider": "codex", "profile_id": "standard-high-codex",
                        "routed_at": T0 + 5, "adjusted_at_apply": True}),
        )
        await connection.execute(
            "INSERT INTO archived_tasks (id, project_id, title, description, status, task_type,"
            " created_at, updated_at, archived_at) VALUES ('t-old', 'p-1', 'old', 'd',"
            " 'COMPLETED', 'feature', $1, $1, $1)", T0,
        )
        for attempt_id, task_id, session, start, ended in (
            ("a1", "t-old", "s1", T0 + 10, T0 + 70),
            ("a2", "t-live", "s1", T0 + 90_000, None),  # same session, after the window
            ("a3", "t-live", "s2", T0 - 10, None),  # before the window
        ):
            await connection.execute(
                "INSERT INTO task_session_attempts (id, session_id, task_id, profile_id, name,"
                " lifecycle, harness, provider, state, work_dir, started_at,"
                " session_started_at, ended_at) VALUES ($1, $2, $3, 'p', 'n', 'pool',"
                " 'claude', 'tmux', 'running', '/w', $4, $4, $5)",
                attempt_id, session, task_id, start, ended,
            )
        await connection.execute(
            "INSERT INTO token_ledger (id, agent_id, task_id, tokens_used, input_tokens,"
            " output_tokens, cache_read_tokens, cache_write_tokens, attempt_id, timestamp)"
            " VALUES ('l1', 'g', 't-old', 30, 1, 2, 20, 7, 'a1', $1),"
            " ('l2', 'g', 't-old', 3, 1, 1, 1, NULL, 'a1', $1),"
            " ('l3', 'g', 't-live', 9, 9, 0, 0, 0, 'a3', $1)", T0 + 20,
        )
        await connection.execute(
            "INSERT INTO task_completion_records (id, task_id, outcome, completed_at)"
            " VALUES ('c1', 't-old', 'pass', $1), ('c2', 'other', 'fail', $2)",
            T0 + 70, T0 - 500,
        )
        async with connection.transaction(readonly=True):
            async def fetch(sql, *params):
                return [dict(row) for row in await connection.fetch(sql, *params)]

            export = await export_rows(fetch, since=T0, until=T0 + 86_400, now=T0 + 86_400)
    finally:
        await connection.close()
    assert [(row["id"], row["task_type"]) for row in export["attempts"]] == [("a1", "feature")]
    assert sorted(row["id"] for row in export["session_attempts"]) == ["a1", "a2"]
    assert [row["id"] for row in export["completions"]] == ["c1"]
    assert [(r["task_id"], r["provider"], r["adjusted_at_apply"]) for r in export["routes"]] \
        == [("t-live", "codex", True)]
    assert sorted(row["id"] for row in export["open_attempts"]) == ["a2", "a3"]
    assert export["ledger"] == [{"attempt_id": "a1", "rows": 2, "uncached_input": 2,
                                 "cache_read": 21, "cache_write": 7, "output": 3}]
