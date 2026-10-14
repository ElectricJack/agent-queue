"""Observe an existing playbook suspension without creating a second predicate."""

import json

from src.database.tables import playbook_artifacts, playbook_v2_runs, playbook_waits
from src.waits.adapters.base import Adapter, fields, row, string
from src.waits.conditions import Condition, NodeKey, Observation, Snapshot, SourceKey


class PlaybookConditionAdapter(Adapter):
    kind = "playbook_condition"

    def normalise(self, raw):
        fields(raw, required={"run_id", "wait_id"})
        return Condition(
            self.kind, {k: string(raw[k]) for k in ("run_id", "wait_id")}, raw.get("generation")
        )

    async def snapshot(self, conn, cond):
        p = cond.parameters
        run = await row(conn, playbook_v2_runs, playbook_v2_runs.c.run_id == p["run_id"])
        wait = await row(
            conn,
            playbook_waits,
            playbook_waits.c.wait_id == p["wait_id"],
            playbook_waits.c.run_id == p["run_id"],
        )
        if not run or not wait:
            return None
        artifact = await row(
            conn, playbook_artifacts, playbook_artifacts.c.artifact_sha256 == run["artifact_sha256"]
        )
        context = json.loads(run["snapshot"]).get("context", {})
        event = context.get("event", {})
        project = (
            artifact["scope_identifier"]
            if artifact["scope"] == "project"
            else context.get("project_id") or event.get("project_id")
        )
        blockers = ()
        match = json.loads(wait["match"])
        if wait["kind"] == "agent_task" and match.get("task_id"):
            blockers = (NodeKey(f"task:{match['task_id']}", project),)
        return Snapshot(
            project,
            wait["snapshot_version"],
            Observation(
                f"{wait['state']}:{wait['claimed_event_id']}",
                "unsatisfied" if wait["state"] == "active" else "satisfied",
                f"playbook:{run['run_id']}:wait:{wait['wait_id']}",
                {"state": wait["state"], "event_id": wait["claimed_event_id"]},
            ),
            "playbook",
            blockers,
        )

    def source_key(self, cond):
        return SourceKey(
            self.kind, f"{cond.parameters['run_id']}:{cond.parameters['wait_id']}", cond.generation
        )
