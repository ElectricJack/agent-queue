"""PR identity comes from the watcher, with an expected-head fence."""

from dataclasses import replace

from src.waits.adapters.base import fields, integer, invalid, string, unresolved
from src.waits.adapters.ci import RepositoryAdapter, sha
from src.waits.conditions import Condition, Observation, SourceKey


class PullRequestAdapter(RepositoryAdapter):
    kind = "pull_request"

    def normalise(self, raw):
        fields(
            raw,
            required={"repository_id", "pr_number", "expected_head_sha"},
            optional={"predicate"},
        )
        if raw.get("predicate", "merged") != "merged":
            invalid("pull request predicate must be merged")
        return Condition(
            self.kind,
            {
                "repository_id": string(raw["repository_id"]),
                "pr_number": integer(raw["pr_number"], 1),
                "expected_head_sha": sha(raw["expected_head_sha"]),
                "predicate": "merged",
            },
            raw.get("generation"),
        )

    async def snapshot(self, conn, cond):
        snapshot = await super().snapshot(conn, cond)
        if snapshot:
            facts = snapshot.observation.facts
            if facts.get("pr_number") != cond.parameters["pr_number"]:
                unresolved("watcher did not verify the requested PR")
            if (
                facts.get("head_sha") != cond.parameters["expected_head_sha"]
                or facts.get("state") == "closed"
                and not facts.get("merged")
            ):
                return replace(
                    snapshot,
                    observation=Observation(
                        snapshot.observation.revision,
                        "unavailable",
                        facts={"reason": "source_changed"},
                    ),
                )
        return snapshot

    def source_key(self, cond):
        p = cond.parameters
        return SourceKey(
            self.kind,
            f"{p['repository_id']}:{p['pr_number']}:{p['expected_head_sha']}",
            0,
        )
