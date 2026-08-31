"""Enabled-path coverage for ``src/commands/playbook_commands.py``.

The dedicated playbook-command test files concentrate on validation and
error envelopes; the *success* paths — actually running, resuming, timing
out, cancelling, and authoring playbooks — were exercised only incidentally
(test-coverage-final-report FU-1).  This file drives each of those paths
through the command mixin with the runner/compiler seams mocked, using the
same mock-handler helpers as ``tests/test_playbook_commands.py``.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.playbooks.runner import RunResult

from tests.test_playbook_commands import (
    FakePlaybookRun,
    _make_handler,
    _make_playbook,
)


def _run_result(**overrides) -> RunResult:
    defaults = dict(
        run_id="run-1",
        status="completed",
        node_trace=[{"node_id": "start", "status": "completed"}],
        tokens_used=42,
        error=None,
        final_response=None,
    )
    defaults.update(overrides)
    return RunResult(**defaults)


# ===========================================================================
# run_playbook — manual trigger, LLM graph path
# ===========================================================================


class TestRunPlaybook:
    async def test_success_returns_run_result_fields(self):
        pb = _make_playbook(playbook_id="pb", version=4)
        handler = _make_handler(playbooks={"pb": pb})

        with patch("src.playbooks.runner.PlaybookRunner") as runner_cls:
            inst = runner_cls.return_value
            inst.run_id = "run-1"
            inst.run = AsyncMock(
                return_value=_run_result(final_response="all done", error=None)
            )
            result = await handler._cmd_run_playbook({"playbook_id": "pb"})

        assert result["run_id"] == "run-1"
        assert result["playbook_id"] == "pb"
        assert result["version"] == 4
        assert result["status"] == "completed"
        assert result["tokens_used"] == 42
        assert result["node_count"] == 1
        assert result["final_response"] == "all done"
        assert "error" not in result

    async def test_event_json_string_is_parsed_and_defaults_injected(self):
        pb = _make_playbook(playbook_id="pb")
        handler = _make_handler(
            playbooks={"pb": pb}, scope_identifiers={"pb": "proj-1"}
        )
        bot = MagicMock()
        bot._get_channel.return_value = SimpleNamespace(id=1234)
        handler.orchestrator._discord_bot = bot

        with patch("src.playbooks.runner.PlaybookRunner") as runner_cls:
            inst = runner_cls.return_value
            inst.run_id = "run-2"
            inst.run = AsyncMock(return_value=_run_result())
            await handler._cmd_run_playbook(
                {"playbook_id": "pb", "event": json.dumps({"foo": "bar"})}
            )
            event = runner_cls.call_args.kwargs["event"]

        assert event["foo"] == "bar"
        assert event["type"] == "manual"  # default injected
        assert event["project_id"] == "proj-1"  # from scope identifier
        assert event["notification_channel_id"] == "1234"

    async def test_invalid_event_json_and_non_dict_event_are_rejected(self):
        pb = _make_playbook(playbook_id="pb")
        handler = _make_handler(playbooks={"pb": pb})

        bad_json = await handler._cmd_run_playbook(
            {"playbook_id": "pb", "event": "not{json"}
        )
        assert "Invalid event JSON" in bad_json["error"]

        non_dict = await handler._cmd_run_playbook(
            {"playbook_id": "pb", "event": ["a", "b"]}
        )
        assert "must be a JSON object" in non_dict["error"]

    async def test_disabled_playbook_refused_unless_forced(self):
        pb = _make_playbook(playbook_id="pb")
        pb.enabled = False
        handler = _make_handler(playbooks={"pb": pb})

        refused = await handler._cmd_run_playbook({"playbook_id": "pb"})
        assert "disabled" in refused["error"]
        assert "force" in refused["error"]

        with patch("src.playbooks.runner.PlaybookRunner") as runner_cls:
            inst = runner_cls.return_value
            inst.run_id = "run-3"
            inst.run = AsyncMock(return_value=_run_result())
            forced = await handler._cmd_run_playbook({"playbook_id": "pb", "force": True})
        assert forced["status"] == "completed"

    async def test_llm_not_configured_is_refused(self):
        pb = _make_playbook(playbook_id="pb")
        handler = _make_handler(playbooks={"pb": pb})
        services = handler.orchestrator.playbook_services.return_value
        services.llm.is_configured.return_value = False

        result = await handler._cmd_run_playbook({"playbook_id": "pb"})

        assert result == {"error": "LLM is not configured (config.llm)"}

    async def test_runner_exception_becomes_error_envelope(self):
        pb = _make_playbook(playbook_id="pb")
        handler = _make_handler(playbooks={"pb": pb})

        with patch("src.playbooks.runner.PlaybookRunner") as runner_cls:
            inst = runner_cls.return_value
            inst.run_id = "run-4"
            inst.run = AsyncMock(side_effect=RuntimeError("node exploded"))
            result = await handler._cmd_run_playbook({"playbook_id": "pb"})

        assert result == {"error": "Playbook execution failed: node exploded"}

    async def test_run_error_is_surfaced_alongside_the_trace(self):
        pb = _make_playbook(playbook_id="pb")
        handler = _make_handler(playbooks={"pb": pb})

        with patch("src.playbooks.runner.PlaybookRunner") as runner_cls:
            inst = runner_cls.return_value
            inst.run_id = "run-5"
            inst.run = AsyncMock(
                return_value=_run_result(status="failed", error="boom")
            )
            result = await handler._cmd_run_playbook({"playbook_id": "pb"})

        assert result["status"] == "failed"
        assert result["error"] == "boom"

    async def test_missing_id_no_manager_and_unknown_playbook(self):
        handler = _make_handler(playbooks={})
        assert "playbook_id" in (await handler._cmd_run_playbook({}))["error"]
        assert "not found" in (
            await handler._cmd_run_playbook({"playbook_id": "ghost"})
        )["error"]

        no_pm = _make_handler(has_playbook_manager=False)
        assert "not initialised" in (
            await no_pm._cmd_run_playbook({"playbook_id": "pb"})
        )["error"]


# ===========================================================================
# run_playbook — pipeline dispatch + _run_pipeline_playbook
# ===========================================================================


def _pipeline_playbook() -> MagicMock:
    pb = MagicMock()
    pb.id = "pipe"
    pb.version = 3
    pb.enabled = True
    pb.to_dict.return_value = {"kind": "pipeline", "nodes": {}}
    return pb


class TestRunPipelinePlaybook:
    async def test_pipeline_kind_dispatches_to_the_deterministic_runner(self):
        pb = _pipeline_playbook()
        handler = _make_handler(playbooks={"pipe": pb})

        with patch("src.playbooks.pipeline_runner.PipelineRunner") as pr_cls:
            inst = pr_cls.return_value
            inst.run_id = "pr-1"
            inst.run = AsyncMock(
                return_value=SimpleNamespace(status="completed", error=None)
            )
            result = await handler._cmd_run_playbook({"playbook_id": "pipe"})

        assert result["kind"] == "pipeline"
        assert result["run_id"] == "pr-1"
        assert result["status"] == "completed"
        assert "error" not in result
        # The run row was persisted and stamped with the outcome.
        handler.db.create_playbook_run.assert_awaited_once()
        update_kwargs = handler.db.update_playbook_run.await_args.kwargs
        assert update_kwargs["status"] == "completed"
        assert update_kwargs["error"] is None

    async def test_pipeline_refuses_to_run_without_a_persisted_row(self):
        pb = _pipeline_playbook()
        handler = _make_handler(playbooks={"pipe": pb})
        handler.db.create_playbook_run = AsyncMock(side_effect=RuntimeError("dup"))

        with patch("src.playbooks.pipeline_runner.PipelineRunner") as pr_cls:
            pr_cls.return_value.run_id = "pr-2"
            pr_cls.return_value.run = AsyncMock()
            result = await handler._cmd_run_playbook({"playbook_id": "pipe"})

        assert result == {"error": "Could not record pipeline run: dup"}
        pr_cls.return_value.run.assert_not_awaited()

    async def test_pipeline_runner_exception_is_recorded_as_failed(self):
        pb = _pipeline_playbook()
        handler = _make_handler(playbooks={"pipe": pb})

        with patch("src.playbooks.pipeline_runner.PipelineRunner") as pr_cls:
            inst = pr_cls.return_value
            inst.run_id = "pr-3"
            inst.run = AsyncMock(side_effect=RuntimeError("stage blew up"))
            result = await handler._cmd_run_playbook({"playbook_id": "pipe"})

        assert result["status"] == "failed"
        assert result["error"] == "stage blew up"
        update_kwargs = handler.db.update_playbook_run.await_args.kwargs
        assert update_kwargs["status"] == "failed"
        assert update_kwargs["error"] == "stage blew up"

    async def test_outcome_write_failure_is_swallowed(self):
        pb = _pipeline_playbook()
        handler = _make_handler(playbooks={"pipe": pb})
        handler.db.update_playbook_run = AsyncMock(side_effect=RuntimeError("db gone"))

        with patch("src.playbooks.pipeline_runner.PipelineRunner") as pr_cls:
            inst = pr_cls.return_value
            inst.run_id = "pr-4"
            inst.run = AsyncMock(
                return_value=SimpleNamespace(status="completed", error=None)
            )
            result = await handler._cmd_run_playbook({"playbook_id": "pipe"})

        assert result["status"] == "completed"


# ===========================================================================
# resume_playbook — the human-in-the-loop resume and timeout paths
# ===========================================================================


def _paused_run(**overrides) -> FakePlaybookRun:
    defaults = dict(
        run_id="run-p",
        playbook_id="pb",
        status="paused",
        current_node="gate",
        paused_at=time.time() - 10,
        pinned_graph=json.dumps({"nodes": {"gate": {"prompt": "review"}}}),
        completed_at=None,
    )
    defaults.update(overrides)
    return FakePlaybookRun(**defaults)


class TestResumePlaybook:
    async def test_resume_success_via_pinned_graph(self):
        handler = _make_handler()
        handler.db.get_playbook_run = AsyncMock(return_value=_paused_run())

        with patch(
            "src.playbooks.runner.PlaybookRunner.resume", new_callable=AsyncMock
        ) as resume:
            resume.return_value = _run_result(run_id="run-p")
            result = await handler._cmd_resume_playbook(
                {"run_id": "run-p", "human_input": "approved"}
            )

        assert result == {
            "resumed": "run-p",
            "playbook_id": "pb",
            "status": "completed",
            "tokens_used": 42,
        }
        assert resume.await_args.kwargs["human_input"] == "approved"
        assert resume.await_args.kwargs["graph"] == {
            "nodes": {"gate": {"prompt": "review"}}
        }

    async def test_resume_falls_back_to_the_active_playbook_graph(self):
        pb = _make_playbook(playbook_id="pb")
        handler = _make_handler(playbooks={"pb": pb})
        handler.db.get_playbook_run = AsyncMock(
            return_value=_paused_run(pinned_graph=None)
        )

        with patch(
            "src.playbooks.runner.PlaybookRunner.resume", new_callable=AsyncMock
        ) as resume:
            resume.return_value = _run_result(run_id="run-p", error="soft fail")
            result = await handler._cmd_resume_playbook(
                {"run_id": "run-p", "human_input": "go"}
            )

        assert result["resumed"] == "run-p"
        assert result["error"] == "soft fail"
        assert resume.await_args.kwargs["graph"] == pb.to_dict()

    async def test_unresolvable_graph_is_refused(self):
        handler = _make_handler(playbooks={})
        handler.db.get_playbook_run = AsyncMock(
            return_value=_paused_run(pinned_graph=None)
        )

        result = await handler._cmd_resume_playbook(
            {"run_id": "run-p", "human_input": "go"}
        )

        assert "Cannot resolve playbook graph" in result["error"]

    async def test_llm_not_configured_is_refused(self):
        handler = _make_handler()
        handler.db.get_playbook_run = AsyncMock(return_value=_paused_run())
        services = handler.orchestrator.playbook_services.return_value
        services.llm.is_configured.return_value = False

        result = await handler._cmd_resume_playbook(
            {"run_id": "run-p", "human_input": "go"}
        )

        assert result == {"error": "LLM is not configured (config.llm)"}

    async def test_resume_exception_becomes_error_envelope(self):
        handler = _make_handler()
        handler.db.get_playbook_run = AsyncMock(return_value=_paused_run())

        with patch(
            "src.playbooks.runner.PlaybookRunner.resume", new_callable=AsyncMock
        ) as resume:
            resume.side_effect = RuntimeError("llm down")
            result = await handler._cmd_resume_playbook(
                {"run_id": "run-p", "human_input": "go"}
            )

        assert result == {"error": "Resume failed: llm down"}


class TestResumePlaybookTimeout:
    async def test_expired_pause_marks_the_run_timed_out(self):
        handler = _make_handler()
        handler.db.get_playbook_run = AsyncMock(
            return_value=_paused_run(paused_at=time.time() - 200000)
        )

        with patch(
            "src.playbooks.runner.PlaybookRunner.handle_timeout",
            new_callable=AsyncMock,
        ) as handle:
            handle.return_value = _run_result(status="timed_out")
            result = await handler._cmd_resume_playbook(
                {"run_id": "run-p", "human_input": "too late"}
            )

        assert "exceeded its pause timeout" in result["error"]
        assert "timed_out" in result["error"]
        # No on_timeout node in the graph — services were never built.
        handler.orchestrator.playbook_services.assert_not_called()

    async def test_on_timeout_transition_builds_services_and_reports_it(self):
        graph = {
            "nodes": {
                "gate": {"prompt": "review", "on_timeout": "escalate"},
                "escalate": {"prompt": "escalate", "terminal": True},
            }
        }
        handler = _make_handler()
        handler.db.get_playbook_run = AsyncMock(
            return_value=_paused_run(
                paused_at=time.time() - 200000, pinned_graph=json.dumps(graph)
            )
        )

        with patch(
            "src.playbooks.runner.PlaybookRunner.handle_timeout",
            new_callable=AsyncMock,
        ) as handle:
            handle.return_value = _run_result(status="completed", error="note")
            result = await handler._cmd_resume_playbook(
                {"run_id": "run-p", "human_input": "late"}
            )

        assert result["timeout_transition"] is True
        assert result["status"] == "completed"
        assert result["error"] == "note"
        handler.orchestrator.playbook_services.assert_called_once()
        assert handle.await_args.kwargs["services"] is not None

    async def test_timeout_handler_failure_falls_back_to_a_direct_mark(self):
        handler = _make_handler()
        handler.db.get_playbook_run = AsyncMock(
            return_value=_paused_run(paused_at=time.time() - 200000)
        )

        with patch(
            "src.playbooks.runner.PlaybookRunner.handle_timeout",
            new_callable=AsyncMock,
        ) as handle:
            handle.side_effect = RuntimeError("no transition")
            result = await handler._cmd_resume_playbook(
                {"run_id": "run-p", "human_input": "late"}
            )

        assert "exceeded its pause timeout" in result["error"]
        update_kwargs = handler.db.update_playbook_run.await_args.kwargs
        assert update_kwargs["status"] == "timed_out"
        assert "Pause timeout exceeded" in update_kwargs["error"]

    async def test_args_timeout_applies_when_no_graph_is_resolvable(self):
        handler = _make_handler(playbooks={})
        handler.db.get_playbook_run = AsyncMock(
            return_value=_paused_run(pinned_graph=None, paused_at=time.time() - 120)
        )

        with patch(
            "src.playbooks.runner.PlaybookRunner.handle_timeout",
            new_callable=AsyncMock,
        ) as handle:
            handle.return_value = _run_result(status="timed_out")
            result = await handler._cmd_resume_playbook(
                {"run_id": "run-p", "human_input": "late", "timeout_seconds": 60}
            )

        assert "(60s)" in result["error"]

    async def test_paused_at_falls_back_to_the_node_trace(self):
        trace = [
            {"node_id": "gate", "started_at": 100.0, "completed_at": time.time() - 200000}
        ]
        handler = _make_handler()
        handler.db.get_playbook_run = AsyncMock(
            return_value=_paused_run(paused_at=None, node_trace=json.dumps(trace))
        )

        with patch(
            "src.playbooks.runner.PlaybookRunner.handle_timeout",
            new_callable=AsyncMock,
        ) as handle:
            handle.return_value = _run_result(status="timed_out")
            result = await handler._cmd_resume_playbook(
                {"run_id": "run-p", "human_input": "late"}
            )

        assert "exceeded its pause timeout" in result["error"]


class TestGetPausedAt:
    def test_prefers_the_last_trace_entry_completed_at(self):
        run = FakePlaybookRun(
            node_trace=json.dumps([{"node_id": "a", "completed_at": 123.0}]),
            started_at=1.0,
        )
        assert _make_handler()._get_paused_at(run) == 123.0

    def test_falls_back_to_started_at_for_empty_or_bad_traces(self):
        handler = _make_handler()
        assert handler._get_paused_at(FakePlaybookRun(node_trace="[]", started_at=5.0)) == 5.0
        assert (
            handler._get_paused_at(FakePlaybookRun(node_trace="not json", started_at=7.0))
            == 7.0
        )


# ===========================================================================
# cancel_playbook_run — unknown status strings and bad trigger JSON
# ===========================================================================


class TestCancelPlaybookRunEdges:
    async def test_unknown_status_and_bad_trigger_json_still_cancel(self):
        run = FakePlaybookRun(
            run_id="run-c", status="mystery", trigger_event="not{json"
        )
        handler = _make_handler()
        handler.db.get_playbook_run = AsyncMock(return_value=run)

        result = await handler._cmd_cancel_playbook_run({"run_id": "run-c"})

        assert result == {
            "cancelled": "run-c",
            "playbook_id": "test-playbook",
            "status": "cancelled",
        }
        update_kwargs = handler.db.update_playbook_run.await_args.kwargs
        assert update_kwargs["status"] == "cancelled"


# ===========================================================================
# recover_workflow
# ===========================================================================


class TestRecoverWorkflow:
    async def test_delegates_to_the_recovery_engine(self):
        handler = _make_handler()
        handler.orchestrator.orphan_workflow_recovery.recover_workflow = AsyncMock(
            return_value={"workflow_id": "wf-1", "action": "resumed"}
        )

        result = await handler._cmd_recover_workflow({"workflow_id": "wf-1"})

        assert result == {"workflow_id": "wf-1", "action": "resumed"}

    async def test_missing_id_and_missing_engine_are_refused(self):
        handler = _make_handler()
        assert "workflow_id" in (await handler._cmd_recover_workflow({}))["error"]

        handler.orchestrator.orphan_workflow_recovery = None
        result = await handler._cmd_recover_workflow({"workflow_id": "wf-1"})
        assert "not initialized" in result["error"]


# ===========================================================================
# compile_playbook — id/path resolution, force coercion, failure envelopes
# ===========================================================================


def _compile_success(pb=None):
    from src.playbooks.compiler import CompilationResult

    return CompilationResult(
        success=True,
        playbook=pb or _make_playbook(),
        errors=[],
        source_hash="hash-1",
        retries_used=0,
        skipped=False,
    )


def _vault(tmp_path, rel: str, content: str = "---\nid: x\n---\n# Body\n"):
    path = tmp_path / "vault" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


class TestCompilePlaybookResolution:
    async def test_playbook_id_resolves_via_the_manager_source_map(self, tmp_path):
        md = _vault(tmp_path, "system/playbooks/pb-a.md")
        handler = _make_handler(compile_result=_compile_success())
        pm = handler.orchestrator.playbook_manager
        pm._source_paths = {"pb-a": str(md)}

        result = await handler._cmd_compile_playbook({"playbook_id": "pb-a"})

        assert result["compiled"] is True
        assert pm.compile_playbook.await_args.kwargs["source_path"] == str(md)

    async def test_playbook_id_falls_back_to_a_vault_scan(self, tmp_path):
        md = _vault(tmp_path, "projects/proj-a/playbooks/pb-b.md")
        handler = _make_handler(compile_result=_compile_success())
        handler.config.data_dir = str(tmp_path)
        pm = handler.orchestrator.playbook_manager
        pm._source_paths = {}

        result = await handler._cmd_compile_playbook({"playbook_id": "pb-b"})

        assert result["compiled"] is True
        assert pm.compile_playbook.await_args.kwargs["source_path"] == str(md)

    async def test_unknown_playbook_id_is_refused(self, tmp_path):
        handler = _make_handler(compile_result=_compile_success())
        handler.config.data_dir = str(tmp_path)
        handler.orchestrator.playbook_manager._source_paths = {}

        result = await handler._cmd_compile_playbook({"playbook_id": "ghost"})

        assert "Unknown playbook_id 'ghost'" in result["error"]

    async def test_bare_name_path_resolves_from_the_vault_dirs(self, tmp_path):
        md = _vault(tmp_path, "system/playbooks/pb-c.md")
        handler = _make_handler(compile_result=_compile_success())
        handler.config.data_dir = str(tmp_path)

        result = await handler._cmd_compile_playbook({"path": "pb-c"})

        assert result["compiled"] is True
        assert handler.orchestrator.playbook_manager.compile_playbook.await_args.kwargs[
            "source_path"
        ] == str(md)

    async def test_force_string_false_is_coerced(self, tmp_path):
        md = _vault(tmp_path, "system/playbooks/pb-d.md")
        handler = _make_handler(compile_result=_compile_success())
        pm = handler.orchestrator.playbook_manager
        pm._source_paths = {"pb-d": str(md)}

        await handler._cmd_compile_playbook({"playbook_id": "pb-d", "force": "false"})

        assert pm.compile_playbook.await_args.kwargs["force"] is False

    async def test_project_scoped_path_derives_the_scope_identifier(self, tmp_path):
        md = _vault(tmp_path, "projects/proj-a/playbooks/pb-e.md")
        handler = _make_handler(compile_result=_compile_success())
        handler.config.vault_root = str(tmp_path / "vault")

        result = await handler._cmd_compile_playbook({"path": str(md)})

        assert result["compiled"] is True
        kwargs = handler.orchestrator.playbook_manager.compile_playbook.await_args.kwargs
        assert kwargs["scope_identifier"] == "proj-a"

    async def test_compile_exception_and_failure_result_envelopes(self, tmp_path):
        from src.playbooks.compiler import CompilationResult

        md = _vault(tmp_path, "system/playbooks/pb-f.md")
        handler = _make_handler(compile_result=_compile_success())
        pm = handler.orchestrator.playbook_manager
        pm._source_paths = {"pb-f": str(md)}

        pm.compile_playbook = AsyncMock(side_effect=RuntimeError("llm down"))
        raised = await handler._cmd_compile_playbook({"playbook_id": "pb-f"})
        assert raised == {"error": "Compilation failed: llm down"}

        pm.compile_playbook = AsyncMock(
            return_value=CompilationResult(
                success=False,
                playbook=None,
                errors=["missing entry node"],
                source_hash="h2",
                retries_used=2,
                skipped=False,
            )
        )
        failed = await handler._cmd_compile_playbook({"playbook_id": "pb-f"})
        assert failed["error"] == "Compilation failed"
        assert failed["errors"] == ["missing entry node"]
        assert failed["retries_used"] == 2


# ===========================================================================
# dry_run_playbook — simulation edges
# ===========================================================================


class TestDryRunPlaybookEdges:
    async def test_non_dict_event_is_rejected(self):
        pb = _make_playbook(playbook_id="pb")
        handler = _make_handler(playbooks={"pb": pb})

        result = await handler._cmd_dry_run_playbook(
            {"playbook_id": "pb", "event": [1, 2]}
        )

        assert "must be a JSON object" in result["error"]

    async def test_simulation_failure_becomes_error_envelope(self):
        pb = _make_playbook(playbook_id="pb")
        handler = _make_handler(playbooks={"pb": pb})

        with patch(
            "src.playbooks.runner.PlaybookRunner.dry_run", new_callable=AsyncMock
        ) as dry:
            dry.side_effect = RuntimeError("cycle detected")
            result = await handler._cmd_dry_run_playbook({"playbook_id": "pb"})

        assert result == {"error": "Dry-run simulation failed: cycle detected"}

    async def test_llm_transitions_are_annotated(self):
        pb = _make_playbook(playbook_id="pb")
        handler = _make_handler(playbooks={"pb": pb})

        with patch(
            "src.playbooks.runner.PlaybookRunner.dry_run", new_callable=AsyncMock
        ) as dry:
            dry.return_value = _run_result(
                node_trace=[
                    {"node_id": "start", "transition_method": "llm"},
                    {"node_id": "end", "transition_method": "goto"},
                ]
            )
            result = await handler._cmd_dry_run_playbook({"playbook_id": "pb"})

        assert result["node_trace"][0]["transition_note"] == (
            "first candidate (LLM skipped in dry-run)"
        )
        assert "transition_note" not in result["node_trace"][1]


# ===========================================================================
# list_playbook_runs / inspect_playbook_run — summary formatting
# ===========================================================================


class TestRunSummaryFormatting:
    async def test_summary_extracts_path_duration_and_error(self):
        trace = [
            {"node_id": "start", "status": "completed", "extra": "ignored"},
            {"node_id": "end", "status": "failed"},
        ]
        run = FakePlaybookRun(
            node_trace=json.dumps(trace),
            started_at=100.0,
            completed_at=160.5,
            error="node 'end' failed",
        )
        handler = _make_handler(db_runs=[run])

        result = await handler._cmd_list_playbook_runs({})

        summary = result["runs"][0]
        assert summary["path"] == [
            {"node_id": "start", "status": "completed"},
            {"node_id": "end", "status": "failed"},
        ]
        assert summary["duration_seconds"] == 60.5
        assert summary["error"] == "node 'end' failed"

    async def test_summary_tolerates_a_corrupt_trace(self):
        run = FakePlaybookRun(node_trace="corrupt{", completed_at=None)
        handler = _make_handler(db_runs=[run])

        result = await handler._cmd_list_playbook_runs({})

        assert result["runs"][0]["path"] == []
        assert "duration_seconds" not in result["runs"][0]

    async def test_inspect_tolerates_corrupt_json_and_reports_pause_state(self):
        run = FakePlaybookRun(
            node_trace="corrupt{",
            conversation_history="also corrupt",
            trigger_event="nope",
            status="paused",
            paused_at=1234.5,
            completed_at=None,
            error="stuck",
        )
        handler = _make_handler()
        handler.db.get_playbook_run = AsyncMock(return_value=run)

        result = await handler._cmd_inspect_playbook_run({"run_id": "run-001"})

        assert result["node_trace"] == []
        assert result["conversation_history"] == []
        assert result["trigger_event"] == {}
        assert result["paused_at"] == 1234.5
        assert result["error"] == "stuck"

    async def test_inspect_enriches_per_node_durations(self):
        trace = [{"node_id": "a", "started_at": 10.0, "completed_at": 12.25}]
        run = FakePlaybookRun(node_trace=json.dumps(trace))
        handler = _make_handler()
        handler.db.get_playbook_run = AsyncMock(return_value=run)

        result = await handler._cmd_inspect_playbook_run({"run_id": "run-001"})

        assert result["node_trace"][0]["duration_seconds"] == 2.25
        assert result["total_duration_seconds"] == 60.0


# ===========================================================================
# playbook_health
# ===========================================================================


class TestPlaybookHealth:
    async def test_computes_health_over_the_run_history(self):
        trace = [
            {"node_id": "start", "status": "completed", "started_at": 1.0, "completed_at": 2.0}
        ]
        runs = [
            FakePlaybookRun(run_id=f"r{i}", node_trace=json.dumps(trace))
            for i in range(3)
        ]
        handler = _make_handler(db_runs=runs)

        result = await handler._cmd_playbook_health({"playbook_id": "test-playbook"})

        assert "error" not in result
        assert isinstance(result, dict) and result  # a real health report

    async def test_invalid_status_is_refused(self):
        handler = _make_handler()
        result = await handler._cmd_playbook_health({"status": "bogus"})
        assert "Invalid status" in result["error"]


# ===========================================================================
# playbook_graph_view
# ===========================================================================


class TestPlaybookGraphView:
    async def test_basic_view_renders_nodes_and_reports_success(self):
        pb = _make_playbook(playbook_id="pb")
        handler = _make_handler(playbooks={"pb": pb})

        result = await handler._cmd_playbook_graph_view({"playbook_id": "pb"})

        assert result["success"] is True

    async def test_string_boolean_options_are_coerced(self):
        pb = _make_playbook(playbook_id="pb")
        handler = _make_handler(playbooks={"pb": pb})

        result = await handler._cmd_playbook_graph_view(
            {
                "playbook_id": "pb",
                "show_prompts": "false",
                "include_live_state": "false",
                "include_metrics": "true",
                "include_history": "true",
                "direction": "LR",
            }
        )

        assert result["success"] is True
        # include_live_state=false means the paused/running queries were skipped
        # but the history/metrics fetch still ran.
        handler.db.list_playbook_runs.assert_awaited()

    async def test_run_overlay_must_exist_and_match_the_playbook(self):
        pb = _make_playbook(playbook_id="pb")
        handler = _make_handler(playbooks={"pb": pb})

        handler.db.get_playbook_run = AsyncMock(return_value=None)
        missing = await handler._cmd_playbook_graph_view(
            {"playbook_id": "pb", "run_id": "ghost"}
        )
        assert "not found" in missing["error"]

        handler.db.get_playbook_run = AsyncMock(
            return_value=FakePlaybookRun(run_id="r1", playbook_id="other-pb")
        )
        mismatch = await handler._cmd_playbook_graph_view(
            {"playbook_id": "pb", "run_id": "r1"}
        )
        assert "belongs to playbook" in mismatch["error"]

    async def test_run_overlay_success(self):
        pb = _make_playbook(playbook_id="pb")
        handler = _make_handler(playbooks={"pb": pb})
        handler.db.get_playbook_run = AsyncMock(
            return_value=FakePlaybookRun(
                run_id="r1",
                playbook_id="pb",
                node_trace=json.dumps([{"node_id": "start", "status": "completed"}]),
            )
        )

        result = await handler._cmd_playbook_graph_view(
            {"playbook_id": "pb", "run_id": "r1"}
        )

        assert result["success"] is True

    async def test_live_state_includes_running_and_paused_runs(self):
        pb = _make_playbook(playbook_id="pb")
        running = FakePlaybookRun(run_id="r-run", status="running", current_node="start")
        paused = FakePlaybookRun(run_id="r-pause", status="paused", current_node="start")

        async def _list_runs(playbook_id=None, status=None, limit=None):
            return {"running": [running], "paused": [paused]}.get(status, [])

        handler = _make_handler(playbooks={"pb": pb})
        handler.db.list_playbook_runs = AsyncMock(side_effect=_list_runs)

        result = await handler._cmd_playbook_graph_view({"playbook_id": "pb"})

        assert result["success"] is True

    async def test_validation_errors(self):
        pb = _make_playbook(playbook_id="pb")
        handler = _make_handler(playbooks={"pb": pb})

        assert "playbook_id" in (await handler._cmd_playbook_graph_view({}))["error"]
        assert "Invalid direction" in (
            await handler._cmd_playbook_graph_view(
                {"playbook_id": "pb", "direction": "UP"}
            )
        )["error"]
        assert "not found" in (
            await handler._cmd_playbook_graph_view({"playbook_id": "ghost"})
        )["error"]


# ===========================================================================
# Source authoring — get/update source, enable toggle, create, delete
# ===========================================================================


def _authoring_handler(tmp_path, *, compile_result=None, playbooks=None):
    handler = _make_handler(
        playbooks=playbooks, compile_result=compile_result or _compile_success()
    )
    handler.config.data_dir = str(tmp_path)
    handler.orchestrator.playbook_manager._source_paths = {}
    return handler


FRONTMATTER_MD = "---\nid: pb-x\nenabled: true\n---\n\n# Steps\n"


class TestGetPlaybookSource:
    async def test_returns_markdown_and_source_hash(self, tmp_path):
        from src.playbooks.compiler import PlaybookCompiler

        _vault(tmp_path, "system/playbooks/pb-x.md", FRONTMATTER_MD)
        handler = _authoring_handler(tmp_path)

        result = await handler._cmd_get_playbook_source({"playbook_id": "pb-x"})

        assert result["markdown"] == FRONTMATTER_MD
        assert result["source_hash"] == PlaybookCompiler._compute_source_hash(
            FRONTMATTER_MD
        )
        assert result["path"].endswith("pb-x.md")

    async def test_missing_playbook_and_missing_id(self, tmp_path):
        handler = _authoring_handler(tmp_path)
        assert "playbook_id" in (await handler._cmd_get_playbook_source({}))["error"]
        assert "not found" in (
            await handler._cmd_get_playbook_source({"playbook_id": "ghost"})
        )["error"]

    async def test_resolution_scans_agent_type_and_project_dirs(self, tmp_path):
        _vault(tmp_path, "agent-types/coding/playbooks/pb-at.md", FRONTMATTER_MD)
        _vault(tmp_path, "projects/proj-a/playbooks/pb-proj.md", FRONTMATTER_MD)
        handler = _authoring_handler(tmp_path)

        assert "error" not in await handler._cmd_get_playbook_source(
            {"playbook_id": "pb-at"}
        )
        assert "error" not in await handler._cmd_get_playbook_source(
            {"playbook_id": "pb-proj"}
        )


class TestUpdatePlaybookSource:
    async def test_writes_atomically_and_compiles(self, tmp_path):
        path = _vault(tmp_path, "system/playbooks/pb-x.md", FRONTMATTER_MD)
        handler = _authoring_handler(tmp_path)
        new_md = FRONTMATTER_MD + "\nMore.\n"

        result = await handler._cmd_update_playbook_source(
            {"playbook_id": "pb-x", "markdown": new_md}
        )

        assert result["compiled"] is True
        assert result["version"] == 1
        assert result["node_count"] == 2
        assert path.read_text(encoding="utf-8") == new_md
        kwargs = handler.orchestrator.playbook_manager.compile_playbook.await_args.kwargs
        assert kwargs["force"] is True

    async def test_optimistic_concurrency_conflict(self, tmp_path):
        from src.playbooks.compiler import PlaybookCompiler

        path = _vault(tmp_path, "system/playbooks/pb-x.md", FRONTMATTER_MD)
        handler = _authoring_handler(tmp_path)

        result = await handler._cmd_update_playbook_source(
            {
                "playbook_id": "pb-x",
                "markdown": "new",
                "expected_source_hash": "stale-hash",
            }
        )

        assert result["error"] == "conflict"
        assert result["reason"] == "vault_changed_underneath"
        assert result["current_source_hash"] == PlaybookCompiler._compute_source_hash(
            FRONTMATTER_MD
        )
        # Conflict means nothing was written.
        assert path.read_text(encoding="utf-8") == FRONTMATTER_MD

    async def test_matching_expected_hash_proceeds(self, tmp_path):
        from src.playbooks.compiler import PlaybookCompiler

        _vault(tmp_path, "system/playbooks/pb-x.md", FRONTMATTER_MD)
        handler = _authoring_handler(tmp_path)

        result = await handler._cmd_update_playbook_source(
            {
                "playbook_id": "pb-x",
                "markdown": "updated",
                "expected_source_hash": PlaybookCompiler._compute_source_hash(
                    FRONTMATTER_MD
                ),
            }
        )

        assert result["compiled"] is True

    async def test_compile_failure_and_exception_envelopes(self, tmp_path):
        from src.playbooks.compiler import CompilationResult

        _vault(tmp_path, "system/playbooks/pb-x.md", FRONTMATTER_MD)
        handler = _authoring_handler(
            tmp_path,
            compile_result=CompilationResult(
                success=False,
                playbook=None,
                errors=["bad graph"],
                source_hash="h",
                retries_used=1,
                skipped=False,
            ),
        )

        failed = await handler._cmd_update_playbook_source(
            {"playbook_id": "pb-x", "markdown": "md"}
        )
        assert failed["compiled"] is False
        assert failed["errors"] == ["bad graph"]

        handler.orchestrator.playbook_manager.compile_playbook = AsyncMock(
            side_effect=RuntimeError("llm down")
        )
        raised = await handler._cmd_update_playbook_source(
            {"playbook_id": "pb-x", "markdown": "md"}
        )
        assert raised == {"error": "Compilation failed: llm down"}

    async def test_validation_errors(self, tmp_path):
        handler = _authoring_handler(tmp_path)
        assert "playbook_id" in (
            await handler._cmd_update_playbook_source({"markdown": "m"})
        )["error"]
        assert "markdown" in (
            await handler._cmd_update_playbook_source({"playbook_id": "x"})
        )["error"]
        assert "not found" in (
            await handler._cmd_update_playbook_source(
                {"playbook_id": "ghost", "markdown": "m"}
            )
        )["error"]


class TestSetPlaybookEnabled:
    async def test_disable_rewrites_frontmatter_and_recompiles(self, tmp_path):
        path = _vault(tmp_path, "system/playbooks/pb-x.md", FRONTMATTER_MD)
        handler = _authoring_handler(tmp_path)

        result = await handler._cmd_set_playbook_enabled(
            {"playbook_id": "pb-x", "enabled": False}
        )

        assert result["enabled"] is False
        assert result["compiled"] is True
        content = path.read_text(encoding="utf-8")
        assert "enabled: false" in content
        assert "# Steps" in content  # body preserved

    async def test_noop_when_already_in_the_desired_state(self, tmp_path):
        _vault(tmp_path, "system/playbooks/pb-x.md", FRONTMATTER_MD)
        handler = _authoring_handler(tmp_path)

        result = await handler._cmd_set_playbook_enabled(
            {"playbook_id": "pb-x", "enabled": True}
        )

        assert result["noop"] is True
        assert result["compiled"] is False
        handler.orchestrator.playbook_manager.compile_playbook.assert_not_awaited()

    async def test_conflict_missing_frontmatter_and_validation(self, tmp_path):
        _vault(tmp_path, "system/playbooks/pb-x.md", FRONTMATTER_MD)
        _vault(tmp_path, "system/playbooks/bare.md", "# no frontmatter\n")
        handler = _authoring_handler(tmp_path)

        conflict = await handler._cmd_set_playbook_enabled(
            {
                "playbook_id": "pb-x",
                "enabled": False,
                "expected_source_hash": "stale",
            }
        )
        assert conflict["error"] == "conflict"

        bare = await handler._cmd_set_playbook_enabled(
            {"playbook_id": "bare", "enabled": False}
        )
        assert "frontmatter" in bare["error"]

        assert "playbook_id" in (
            await handler._cmd_set_playbook_enabled({"enabled": True})
        )["error"]
        assert "enabled" in (
            await handler._cmd_set_playbook_enabled({"playbook_id": "pb-x"})
        )["error"]
        assert "not found" in (
            await handler._cmd_set_playbook_enabled(
                {"playbook_id": "ghost", "enabled": False}
            )
        )["error"]

    async def test_without_a_manager_the_file_still_flips(self, tmp_path):
        path = _vault(tmp_path, "system/playbooks/pb-x.md", FRONTMATTER_MD)
        handler = _make_handler(has_playbook_manager=False)
        handler.config.data_dir = str(tmp_path)

        result = await handler._cmd_set_playbook_enabled(
            {"playbook_id": "pb-x", "enabled": False}
        )

        assert result["enabled"] is False
        assert result["compiled"] is False
        assert "source_hash" in result
        assert "enabled: false" in path.read_text(encoding="utf-8")

    async def test_compile_failure_paths(self, tmp_path):
        from src.playbooks.compiler import CompilationResult

        _vault(tmp_path, "system/playbooks/pb-x.md", FRONTMATTER_MD)
        handler = _authoring_handler(
            tmp_path,
            compile_result=CompilationResult(
                success=False,
                playbook=None,
                errors=["invalid"],
                source_hash="h",
                retries_used=0,
                skipped=False,
            ),
        )
        failed = await handler._cmd_set_playbook_enabled(
            {"playbook_id": "pb-x", "enabled": False}
        )
        assert failed["compiled"] is False
        assert failed["errors"] == ["invalid"]

        _vault(tmp_path, "system/playbooks/pb-y.md", FRONTMATTER_MD)
        handler.orchestrator.playbook_manager.compile_playbook = AsyncMock(
            side_effect=RuntimeError("llm down")
        )
        raised = await handler._cmd_set_playbook_enabled(
            {"playbook_id": "pb-y", "enabled": False}
        )
        assert raised["compiled"] is False
        assert "Compilation failed" in raised["error"]
        # The file itself was still flipped before compilation failed.
        assert raised["enabled"] is False


class TestCreatePlaybook:
    async def test_creates_in_each_scope_directory(self, tmp_path):
        handler = _authoring_handler(tmp_path)
        cases = {
            "system": tmp_path / "vault" / "system" / "playbooks" / "pb-sys.md",
            "project:my-app": tmp_path
            / "vault"
            / "projects"
            / "my-app"
            / "playbooks"
            / "pb-proj.md",
            "agent-type:coding": tmp_path
            / "vault"
            / "agent-types"
            / "coding"
            / "playbooks"
            / "pb-at.md",
        }
        for (scope, expected), pid in zip(
            cases.items(), ("pb-sys", "pb-proj", "pb-at")
        ):
            result = await handler._cmd_create_playbook(
                {"playbook_id": pid, "scope": scope, "markdown": FRONTMATTER_MD}
            )
            assert result["created"] is True, result
            assert result["path"] == str(expected)
            assert expected.read_text(encoding="utf-8") == FRONTMATTER_MD

    async def test_collision_and_malformed_scopes_are_refused(self, tmp_path):
        existing = _vault(tmp_path, "system/playbooks/pb-x.md", FRONTMATTER_MD)
        handler = _authoring_handler(tmp_path)

        collision = await handler._cmd_create_playbook(
            {"playbook_id": "pb-x", "scope": "system", "markdown": "m"}
        )
        assert "already exists" in collision["error"]
        assert str(existing) in collision["error"]

        for scope, fragment in (
            ("project:", "identifier"),
            ("agent-type:", "type"),
            ("galaxy", "Invalid scope"),
        ):
            result = await handler._cmd_create_playbook(
                {"playbook_id": "pb-new", "scope": scope, "markdown": "m"}
            )
            assert fragment in result["error"]

    async def test_validation_errors(self, tmp_path):
        handler = _authoring_handler(tmp_path)
        assert "playbook_id" in (
            await handler._cmd_create_playbook({"scope": "system", "markdown": "m"})
        )["error"]
        assert "scope" in (
            await handler._cmd_create_playbook({"playbook_id": "x", "markdown": "m"})
        )["error"]
        assert "markdown" in (
            await handler._cmd_create_playbook({"playbook_id": "x", "scope": "system"})
        )["error"]


class TestDeletePlaybook:
    async def test_archives_the_source_and_removes_from_the_registry(self, tmp_path):
        path = _vault(tmp_path, "system/playbooks/pb-x.md", FRONTMATTER_MD)
        handler = _authoring_handler(tmp_path)
        pm = handler.orchestrator.playbook_manager
        pm.remove_playbook = AsyncMock(return_value=True)

        result = await handler._cmd_delete_playbook({"playbook_id": "pb-x"})

        assert result["deleted"] is True
        assert result["removed_from_registry"] is True
        assert not path.exists()
        archived = tmp_path / "vault" / "trash" / "playbooks"
        archived_files = list(archived.glob("pb-x.*.md"))
        assert len(archived_files) == 1
        assert result["archived_path"] == str(archived_files[0])
        assert archived_files[0].read_text(encoding="utf-8") == FRONTMATTER_MD

    async def test_registry_removal_failure_still_reports_the_archive(self, tmp_path):
        _vault(tmp_path, "system/playbooks/pb-x.md", FRONTMATTER_MD)
        handler = _authoring_handler(tmp_path)
        pm = handler.orchestrator.playbook_manager
        pm.remove_playbook = AsyncMock(side_effect=RuntimeError("watcher lock"))

        result = await handler._cmd_delete_playbook({"playbook_id": "pb-x"})

        assert "registry remove failed" in result["error"]
        assert "archived_path" in result

    async def test_without_a_manager_the_archive_still_happens(self, tmp_path):
        path = _vault(tmp_path, "system/playbooks/pb-x.md", FRONTMATTER_MD)
        handler = _make_handler(has_playbook_manager=False)
        handler.config.data_dir = str(tmp_path)

        result = await handler._cmd_delete_playbook({"playbook_id": "pb-x"})

        assert result["deleted"] is True
        assert result["removed_from_registry"] is False
        assert not path.exists()

    async def test_validation_errors(self, tmp_path):
        handler = _authoring_handler(tmp_path)
        assert "playbook_id" in (await handler._cmd_delete_playbook({}))["error"]
        assert "not found" in (
            await handler._cmd_delete_playbook({"playbook_id": "ghost"})
        )["error"]


# ===========================================================================
# check_paused_playbook_timeouts — the orchestrator tick sweep
# ===========================================================================


class TestCheckPausedPlaybookTimeouts:
    async def test_no_paused_runs_is_a_noop(self):
        handler = _make_handler(db_runs=[])
        assert await handler.check_paused_playbook_timeouts() == []

    async def test_fresh_pauses_and_graphless_runs_are_skipped(self):
        fresh = _paused_run(run_id="fresh", paused_at=time.time() - 5)
        graphless = _paused_run(
            run_id="graphless", pinned_graph=None, paused_at=time.time() - 200000
        )
        handler = _make_handler(playbooks={}, db_runs=[fresh, graphless])

        with patch(
            "src.playbooks.runner.PlaybookRunner.handle_timeout",
            new_callable=AsyncMock,
        ) as handle:
            results = await handler.check_paused_playbook_timeouts()

        assert results == []
        handle.assert_not_awaited()

    async def test_expired_pause_is_handled_and_reported(self):
        expired = _paused_run(run_id="expired", paused_at=time.time() - 200000)
        handler = _make_handler(db_runs=[expired])

        with patch(
            "src.playbooks.runner.PlaybookRunner.handle_timeout",
            new_callable=AsyncMock,
        ) as handle:
            handle.return_value = _run_result(run_id="expired", status="timed_out")
            results = await handler.check_paused_playbook_timeouts()

        assert len(results) == 1
        assert results[0]["run_id"] == "expired"
        assert results[0]["playbook_id"] == "pb"
        assert results[0]["status"] == "timed_out"
        assert results[0]["timeout_seconds"] > 0
        assert results[0]["on_timeout"] is None
        handle.assert_awaited_once()
        # No on_timeout node — services never built.
        handler.orchestrator.playbook_services.assert_not_called()

    async def test_on_timeout_node_builds_services(self):
        graph = {
            "nodes": {
                "gate": {"prompt": "review", "on_timeout": "escalate"},
                "escalate": {"prompt": "up", "terminal": True},
            }
        }
        expired = _paused_run(
            run_id="expired",
            paused_at=time.time() - 200000,
            pinned_graph=json.dumps(graph),
        )
        handler = _make_handler(db_runs=[expired])

        with patch(
            "src.playbooks.runner.PlaybookRunner.handle_timeout",
            new_callable=AsyncMock,
        ) as handle:
            handle.return_value = _run_result(run_id="expired", status="completed")
            results = await handler.check_paused_playbook_timeouts()

        assert results[0]["on_timeout"] == "escalate"
        handler.orchestrator.playbook_services.assert_called_once()

    async def test_handler_failure_marks_the_run_timed_out_directly(self):
        expired = _paused_run(run_id="expired", paused_at=time.time() - 200000)
        handler = _make_handler(db_runs=[expired])

        with patch(
            "src.playbooks.runner.PlaybookRunner.handle_timeout",
            new_callable=AsyncMock,
        ) as handle:
            handle.side_effect = RuntimeError("no path")
            results = await handler.check_paused_playbook_timeouts()

        assert results == []
        update_kwargs = handler.db.update_playbook_run.await_args.kwargs
        assert update_kwargs["status"] == "timed_out"

    async def test_graph_resolves_from_the_active_manager_when_not_pinned(self):
        pb = _make_playbook(playbook_id="pb")
        expired = _paused_run(
            run_id="expired", pinned_graph=None, paused_at=time.time() - 200000
        )
        handler = _make_handler(playbooks={"pb": pb}, db_runs=[expired])

        with patch(
            "src.playbooks.runner.PlaybookRunner.handle_timeout",
            new_callable=AsyncMock,
        ) as handle:
            handle.return_value = _run_result(run_id="expired", status="timed_out")
            results = await handler.check_paused_playbook_timeouts()

        assert len(results) == 1
        assert handle.await_args.kwargs["graph"] == pb.to_dict()

    async def test_run_with_no_pause_timestamp_at_all_is_skipped(self):
        undated = _paused_run(
            run_id="undated", paused_at=None, node_trace="[]", started_at=None
        )
        handler = _make_handler(db_runs=[undated])

        with patch(
            "src.playbooks.runner.PlaybookRunner.handle_timeout",
            new_callable=AsyncMock,
        ) as handle:
            results = await handler.check_paused_playbook_timeouts()

        assert results == []
        handle.assert_not_awaited()

    async def test_fallback_mark_failure_is_swallowed(self):
        expired = _paused_run(run_id="expired", paused_at=time.time() - 200000)
        handler = _make_handler(db_runs=[expired])
        handler.db.update_playbook_run = AsyncMock(side_effect=RuntimeError("db gone"))

        with patch(
            "src.playbooks.runner.PlaybookRunner.handle_timeout",
            new_callable=AsyncMock,
        ) as handle:
            handle.side_effect = RuntimeError("no path")
            results = await handler.check_paused_playbook_timeouts()

        assert results == []


# ===========================================================================
# Defensive I/O branches — unreadable sources, unwritable directories
# ===========================================================================


class TestSourceIOFailures:
    async def test_compile_playbook_without_a_manager_for_id_resolution(self):
        handler = _make_handler(has_playbook_manager=False)
        result = await handler._cmd_compile_playbook({"playbook_id": "pb"})
        assert "not initialised" in result["error"]

    async def test_stale_source_map_entry_falls_back_to_the_vault_scan(self, tmp_path):
        md = _vault(tmp_path, "system/playbooks/pb-x.md", FRONTMATTER_MD)
        handler = _authoring_handler(tmp_path)
        handler.orchestrator.playbook_manager._source_paths = {
            "pb-x": str(tmp_path / "gone" / "pb-x.md")
        }

        result = await handler._cmd_get_playbook_source({"playbook_id": "pb-x"})

        assert result["path"] == str(md)

    async def test_bare_name_path_resolves_from_a_project_dir(self, tmp_path):
        md = _vault(tmp_path, "projects/proj-a/playbooks/pb-p.md", FRONTMATTER_MD)
        handler = _authoring_handler(tmp_path)

        result = await handler._cmd_compile_playbook({"path": "pb-p.md"})

        assert result["compiled"] is True
        assert handler.orchestrator.playbook_manager.compile_playbook.await_args.kwargs[
            "source_path"
        ] == str(md)

    async def test_unreadable_sources_fail_cleanly(self, tmp_path):
        md = _vault(tmp_path, "system/playbooks/pb-x.md", FRONTMATTER_MD)
        handler = _authoring_handler(tmp_path)
        md.chmod(0o000)
        try:
            compile_result = await handler._cmd_compile_playbook({"path": str(md)})
            get_result = await handler._cmd_get_playbook_source({"playbook_id": "pb-x"})
            update_result = await handler._cmd_update_playbook_source(
                {
                    "playbook_id": "pb-x",
                    "markdown": "m",
                    "expected_source_hash": "h",
                }
            )
            toggle_result = await handler._cmd_set_playbook_enabled(
                {"playbook_id": "pb-x", "enabled": False}
            )
        finally:
            md.chmod(0o644)

        assert "Failed to read file" in compile_result["error"]
        assert "Failed to read source" in get_result["error"]
        assert "Failed to read current source" in update_result["error"]
        assert "Failed to read source" in toggle_result["error"]

    async def test_unwritable_directory_fails_cleanly(self, tmp_path):
        md = _vault(tmp_path, "system/playbooks/pb-x.md", FRONTMATTER_MD)
        handler = _authoring_handler(tmp_path)
        md.parent.chmod(0o500)
        try:
            update_result = await handler._cmd_update_playbook_source(
                {"playbook_id": "pb-x", "markdown": "m"}
            )
            toggle_result = await handler._cmd_set_playbook_enabled(
                {"playbook_id": "pb-x", "enabled": False}
            )
            create_result = await handler._cmd_create_playbook(
                {"playbook_id": "pb-new", "scope": "system", "markdown": "m"}
            )
        finally:
            md.parent.chmod(0o755)

        assert "Failed to write source" in update_result["error"]
        assert "Failed to write source" in toggle_result["error"]
        assert "Failed to write new playbook" in create_result["error"]

    async def test_unwritable_vault_blocks_scope_dir_and_trash_creation(self, tmp_path):
        md = _vault(tmp_path, "system/playbooks/pb-x.md", FRONTMATTER_MD)
        handler = _authoring_handler(tmp_path)
        vault = tmp_path / "vault"
        vault.chmod(0o500)
        try:
            create_result = await handler._cmd_create_playbook(
                {
                    "playbook_id": "pb-proj",
                    "scope": "project:my-app",
                    "markdown": "m",
                }
            )
            delete_result = await handler._cmd_delete_playbook({"playbook_id": "pb-x"})
        finally:
            vault.chmod(0o755)

        assert "Failed to create scope directory" in create_result["error"]
        assert "Failed to create trash directory" in delete_result["error"]
        assert md.exists()  # nothing was archived

    async def test_unmovable_source_blocks_the_archive(self, tmp_path):
        md = _vault(tmp_path, "system/playbooks/pb-x.md", FRONTMATTER_MD)
        handler = _authoring_handler(tmp_path)
        # Trash exists, but the source's own directory refuses the rename.
        (tmp_path / "vault" / "trash" / "playbooks").mkdir(parents=True)
        md.parent.chmod(0o500)
        try:
            result = await handler._cmd_delete_playbook({"playbook_id": "pb-x"})
        finally:
            md.parent.chmod(0o755)

        assert "Failed to archive source" in result["error"]
        assert md.exists()

    async def test_update_playbook_source_without_a_manager(self, tmp_path):
        _vault(tmp_path, "system/playbooks/pb-x.md", FRONTMATTER_MD)
        handler = _make_handler(has_playbook_manager=False)
        handler.config.data_dir = str(tmp_path)

        result = await handler._cmd_update_playbook_source(
            {"playbook_id": "pb-x", "markdown": "m"}
        )

        assert "not initialised" in result["error"]

    async def test_cached_source_path_is_used_when_still_a_file(self, tmp_path):
        md = _vault(tmp_path, "system/playbooks/pb-x.md", FRONTMATTER_MD)
        handler = _authoring_handler(tmp_path)
        handler.orchestrator.playbook_manager._source_paths = {"pb-x": str(md)}

        result = await handler._cmd_get_playbook_source({"playbook_id": "pb-x"})

        assert result["path"] == str(md)

    async def test_failed_atomic_rename_cleans_up_the_tempfile(self, tmp_path):
        md = _vault(tmp_path, "system/playbooks/pb-x.md", FRONTMATTER_MD)
        handler = _authoring_handler(tmp_path)

        with patch("os.replace", side_effect=OSError("target busy")):
            update_result = await handler._cmd_update_playbook_source(
                {"playbook_id": "pb-x", "markdown": "m"}
            )
            toggle_result = await handler._cmd_set_playbook_enabled(
                {"playbook_id": "pb-x", "enabled": False}
            )
            create_result = await handler._cmd_create_playbook(
                {"playbook_id": "pb-new", "scope": "system", "markdown": "m"}
            )

        assert "Failed to write source" in update_result["error"]
        assert "Failed to write source" in toggle_result["error"]
        assert "Failed to write new playbook" in create_result["error"]
        # The tempfiles were unlinked and the original survived untouched.
        assert list(md.parent.glob("*.tmp")) == []
        assert md.read_text(encoding="utf-8") == FRONTMATTER_MD

    async def test_graph_view_metrics_are_computed_from_the_history(self):
        pb = _make_playbook(playbook_id="pb")
        trace = [
            {
                "node_id": "start",
                "status": "completed",
                "started_at": 1.0,
                "completed_at": 2.0,
                "tokens_used": 10,
            }
        ]
        runs = [FakePlaybookRun(run_id="r1", node_trace=json.dumps(trace))]
        handler = _make_handler(playbooks={"pb": pb}, db_runs=runs)

        result = await handler._cmd_playbook_graph_view(
            {
                "playbook_id": "pb",
                "include_live_state": "false",
                "include_metrics": "true",
            }
        )

        assert result["success"] is True
