"""Real persistence harness for mandatory triage completion tests."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import src.triage as triage
from src.agents.execution_types import execution_type_key, ExecutionIdentity
from src.intelligence_classes import IntelligenceClass
from src.models import Agent, AgentProfile, PlaybookRun, Project, SessionRecord, Task
from src.sessions.harness_parser import Harness
from src.sessions.harness_registry import HarnessRegistry
from src.sessions.spec import SessionSpecBuilder
from src.triage.models import TriagePrincipal


@dataclass
class TriageCase:
    service: object
    principal: TriagePrincipal
    task_id: str
    type_key: str
    revision: int
    gate_id: str
    builder: SessionSpecBuilder
    harnesses: HarnessRegistry

    @classmethod
    async def create(cls, db, *, project_id: str = "p") -> "TriageCase":
        assert hasattr(triage, "TriageService"), "Task 2 TriageService is not implemented"
        await db.create_project(Project(project_id, "Project"))
        await db.create_profile(
            AgentProfile(
                id="worker",
                name="Worker",
                harness="codex",
                default_class="standard",
                needs_workspace=False,
            )
        )
        await db.create_agent(Agent("worker-1", "Worker 1", "worker"))
        classes = {
            "standard": IntelligenceClass(
                "standard", "Standard", "", {"codex": {"model": "fixture-model"}}
            )
        }
        harnesses = HarnessRegistry()
        harnesses.upsert(
            Harness(id="codex", name="Codex", command="codex", model_flag="--model")
        )
        builder = SessionSpecBuilder(SimpleNamespace(), intelligence_classes=classes)
        run_id = "triage-run"
        session_id = "triage-session"
        token = "triage-instance"
        await db.create_playbook_run(
            PlaybookRun(
                run_id,
                "mandatory-triage",
                7,
                project_id=project_id,
                role="triage",
            )
        )
        await db.create_session(
            SessionRecord(
                id=session_id,
                project_id=project_id,
                profile_id="triage",
                harness="codex",
                provider="fake",
                name=session_id,
                lifecycle="playbook",
                work_dir="/tmp/triage",
                epoch="test",
                instance_token=token,
                started_at=1,
                state="running",
                playbook_run_id=run_id,
                playbook_node_id="inspect",
            )
        )
        await db.update_playbook_run(run_id, owner_session_id=session_id)
        task_id = "target"
        await db.create_task(Task(task_id, project_id, "Target", "Needs a route"))
        gate_id, _ = await db.create_gate(
            project_id, "routing", "Choose execution type", waiter_task_ids=[task_id]
        )
        identity = ExecutionIdentity(
            "worker", "codex", "openai", "fixture-model", "standard", ""
        )
        service = triage.TriageService(
            db,
            builder=builder,
            harness_registry=harnesses,
        )
        return cls(
            service=service,
            principal=TriagePrincipal(project_id, run_id, session_id, token),
            task_id=task_id,
            type_key=execution_type_key(identity),
            revision=1,
            gate_id=gate_id,
            builder=builder,
            harnesses=harnesses,
        )
