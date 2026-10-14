"""Service-only admission conditions; the evaluator supplies verified snapshots.

Current streams use generation 0; cycle/external changes are opaque revisions.
This is the supervisor's bounded correction to section 9.1: the baseline has no
scheduler cycle incarnation/rebind protocol. The admission reader and producers
share resource:<blocker>:<identity>; pool/config are producer categories only.

The reader must re-evaluate current scope, instance, config and owner state and
return facts naming blocker, identity, owner_kind and owner_id. Identity comes
from that same admission normalizer, never a guessed profile id. A later real
incarnation requires a coordinated producer/subscriber fencing change.
"""

from src.waits.adapters.base import Adapter, fields, foreign, invalid, string, unresolved
from src.waits.conditions import Condition, Edge, SourceKey

BLOCKERS = frozenset(
    {
        "slot",
        "workspace",
        "provider",
        "backoff",
        "budget",
        "policy_hold",
        "branch_owner",
        "batch_hold",
    }
)


class ResourceAdapter(Adapter):
    kind = "resource"

    def __init__(self, reader=None):
        self.reader = reader

    def normalise(self, raw):
        fields(raw, required={"blocker", "identity", "source_revision"})
        if not isinstance(raw["blocker"], str) or raw["blocker"] not in BLOCKERS:
            invalid("resource condition must name an admission constraint")
        return Condition(
            self.kind,
            {
                k: string(raw[k])
                for k in (
                    "blocker",
                    "identity",
                    "source_revision",
                )
            },
            raw.get("generation"),
        )

    async def authorise(self, conn, cond, scope):
        if scope.principal != "service":
            foreign()
        return await super().authorise(conn, cond, scope)

    async def snapshot(self, conn, cond):
        if self.reader is None:
            unresolved("admission evaluator is unavailable", retry_after=30)
        snapshot = await self.reader(conn, cond)
        if snapshot:
            expected = {
                "blocker": cond.parameters["blocker"],
                "identity": cond.parameters["identity"],
                "owner_kind": cond.context["owner_kind"],
                "owner_id": cond.context["owner_id"],
            }
            if snapshot.generation != 0 or any(
                snapshot.observation.facts.get(key) != value for key, value in expected.items()
            ):
                return None
        if snapshot and snapshot.observation.revision != cond.parameters["source_revision"]:
            # A new evaluator revision is itself the resource change to re-evaluate.
            if not cond.context.get("source_class"):
                unresolved("admission constraint revision has changed")
        return snapshot

    def source_key(self, cond):
        p = cond.parameters
        return SourceKey(self.kind, f"{p['blocker']}:{p['identity']}", 0)

    def edges(self, cond, waiter):
        return [Edge(e.waiter, e.blocker, "resource") for e in super().edges(cond, waiter)]
