"""Provider-neutral fixture adapters over the real K08 context service (K09).

The evaluation runner (``scripts/evaluate-knowledge.py``) is deliberately
provider-neutral: it hands one fixture request and one harness label to an
adapter and judges the observation it gets back. Its default adapter replays a
sealed snapshot, which proves the contract but never touches the service.

These adapters close that gap. They own an isolated disposable database, drive
the daemon's own ``ContextService`` — prepare, thin harness delivery,
acknowledgment, citation readback — and normalize what actually happened into
the runner's observation schema. They never select, rank, summarize, verify or
authorize: every byte they report came out of the record store.

``LocalModelFixtureAdapter`` is the local-model contract adapter. A local model
CLI has neither a SessionStart hook nor an argv prompt channel, so it delivers
through explicit startup guidance; it proves contract compatibility and nothing
about local-model quality (plan §13).
"""

from __future__ import annotations

import copy
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.commands.principal import TRUSTED_LOCAL
from src.config import AppConfig
from src.database.tables import (
    knowledge_citations,
    knowledge_context_deliveries,
    record_source_artifacts,
)
from src.knowledge.context import ContextService
from src.knowledge.delivery import STARTUP_GUIDANCE, plan, startup_guidance
from src.knowledge.service import KnowledgeService
from src.prime.hook_envelopes import HOOK_ENVELOPE_HARNESSES
from tests.record_helpers import knowledge_config, snapshot, worker_principal

__all__ = [
    "ContextBundleFixtureAdapter",
    "FixtureAdapterError",
    "LocalModelFixtureAdapter",
    "fixture_uuid",
]

#: Fixture identities are labels, never production ids. Deriving a stable UUID
#: from the label keeps a fixture reproducible across runs and databases while
#: the observations report the label the manifest sealed.
FIXTURE_NAMESPACE = "aq-knowledge-fixture"
PROJECT_ID = "p"

GRANTS = [
    "knowledge_show",
    "knowledge_search",
    "knowledge_cite",
    "knowledge_context_deliver",
]

#: Manifest record kind -> snapshot category. An adapter seeds the record store
#: from sealed fixture data; it never invents a category the fixture did not name.
CATEGORIES = {
    "procedure": "procedure",
    "evidence": "incident",
    "guidance": "policy",
    "context": "note",
    "diagnostic": "fact",
    "note": "note",
}


class FixtureAdapterError(RuntimeError):
    """The adapter could not observe the service it was pointed at."""


def _artifact_id(record: dict):
    """The retained artifact a fixture record's evidence label points at."""
    return fixture_uuid(f"{record['record_id']}:{record['evidence']}")


def fixture_uuid(label: str):
    """A stable UUID for a fixture label, derived without touching the database."""
    if not isinstance(label, str) or not label:
        raise ValueError("fixture labels are nonempty strings")
    return uuid5(uuid5(NAMESPACE_URL, FIXTURE_NAMESPACE), label)


def _config(budget: dict) -> AppConfig:
    """Map a manifest budget onto the service's own caps.

    The manifest declares one provider-visible allowance; the service splits it
    into input, knowledge and reserve terms. With an empty required prompt the
    two accountings agree exactly, which is what lets the runner's oracle judge
    the observed rendering instead of re-deriving it.
    """
    config = AppConfig()
    config.knowledge = knowledge_config()
    config.memory.enabled = True
    config.memory.context_max_tokens = budget["max_tokens"]
    context = config.knowledge.context
    context.enabled = True
    context.input_max_tokens = budget["max_tokens"]
    context.output_reserve_tokens = budget["reserved_tokens"]
    context.wrapper_reserve_tokens = 0
    context.max_tokens = budget["max_tokens"]
    context.max_bytes = budget["max_bytes"]
    context.discovery_max_tokens = 0
    context.discovery_max_items = 0
    return config


def _seed_snapshot(record: dict) -> dict:
    """Build the snapshot an integrated fixture record describes.

    Every field the service normalizes is present, because the sealed
    ``revision_sha256`` in the manifest is the hash of exactly these bytes.
    """
    excerpts = dict(record["excerpts"])
    verification = record.get("verification", "unverified")
    lifecycle = record.get("lifecycle", "active")
    if verification != "unverified":
        # A verified revision needs named evidence plus actor and time, which
        # this manifest format does not model. Refusing is the honest answer:
        # the adapter must not invent provenance the fixture did not seal.
        raise FixtureAdapterError("integrated fixtures model unverified revisions only")
    # The evidence label rides a retained artifact descriptor, so it is the
    # record store — not the fixture — that reports it, and it appears in the
    # rendered payload the oracle inspects.
    artifact = _artifact_id(record)
    doc = snapshot(
        title=f"Fixture {record['record_id']}",
        body=excerpts.get("body") or next(iter(excerpts.values())),
        summary=excerpts.get("summary"),
        category=CATEGORIES[record["kind"]],
        lifecycle=lifecycle,
        retirement_reason="retired by fixture" if lifecycle == "retired" else None,
        verification=verification,
        sources=[{
            "source_id": record["evidence"],
            "kind": "artifact",
            "artifact_id": str(artifact),
            "sha256": artifact.hex * 2,
        }],
    )
    recheck = record.get("recheck_at")
    if recheck:
        doc["recheck_at"] = recheck
    valid_from = record.get("valid_from")
    if valid_from:
        doc["valid_from"] = valid_from
    return doc


class ContextBundleFixtureAdapter:
    """Drives the real context service once per requested harness label."""

    name = "context_bundle"

    #: Harnesses with a structured SessionStart contract wrap the payload; every
    #: other label carries the identical bytes through the startup prompt.
    supports_hooks = True
    prompt_mode = "arg"

    def __init__(self, observations: dict[tuple[str, str], dict], transports: dict):
        self._observations = observations
        #: ``(fixture_id, harness) -> transport`` — what each label's launch
        #: provisioned. Kept out of the observation, which must stay identical
        #: across labels; the tests assert this matrix directly.
        self.transports = transports

    # -- runner protocol ---------------------------------------------------

    def observe(self, inputs: dict, *, harness: str, role: str) -> dict:
        key = (str(inputs["fixture_id"]), str(harness))
        if key not in self._observations:
            raise FixtureAdapterError(f"no observation recorded for {key}")
        if inputs["delivery_rules"]["role"] != role:
            raise FixtureAdapterError("role label does not match the sealed delivery rules")
        return copy.deepcopy(self._observations[key])

    # -- observation -------------------------------------------------------

    @classmethod
    async def run(
        cls, db, manifest: dict, *, harnesses=None, identity_suffix: str = ""
    ) -> ContextBundleFixtureAdapter:
        """Prepare, deliver and read back one fixture against a real database.

        One pass per harness label, through the same thin adapter a launch uses,
        so a harness that changed the payload — or the transport it delivered it
        on — fails the runner's parity check instead of passing silently.

        *identity_suffix* namespaces the execution identity for a runner that
        reuses one database across runs; the reported owner is always the sealed
        fixture label, never this name.
        """
        records = manifest["records"]
        rules = manifest["delivery_rules"]
        budget = manifest["budget"]
        supervisor = rules["role"] == "supervisor"
        claim_epoch = None if supervisor else rules["claim_epoch"]
        config = _config(budget)
        # One execution identity per fixture, so two fixtures in one database
        # are two attempts rather than one attempt claimed twice.
        principal = await worker_principal(
            db,
            f"{'supervisor' if supervisor else 'worker'}-{manifest['fixture_id']}"
            f"{identity_suffix}",
            elevated=supervisor,
            grants=list(GRANTS),
        )
        service = KnowledgeService(db, config.knowledge)

        seeded: dict[tuple[str, str], dict] = {}
        async with db.immediate() as conn:
            scope_key = await db.ensure_record_scope_on(project_id=PROJECT_ID, conn=conn)
            for record in records:
                # A retained artifact descriptor, so the sealed evidence label is
                # a real source the record store validates and the rendered
                # payload carries. No bytes are written anywhere: nothing in
                # this scenario reads the artifact, and a fixture must not touch
                # the operator's vault.
                await conn.execute(pg_insert(record_source_artifacts).values(
                    artifact_id=_artifact_id(record),
                    scope_key=scope_key,
                    content_sha256=_artifact_id(record).hex * 2,
                    byte_size=len(record["excerpts"].get("body") or ""),
                    media_type="text/plain",
                    storage_key=f"record-artifacts/{_artifact_id(record)}",
                ).on_conflict_do_nothing(index_elements=["artifact_id"]))
        for record in records:
            doc = _seed_snapshot(record)
            created = await service.create(
                principal=TRUSTED_LOCAL, project_id=PROJECT_ID,
                idempotency_key=f"fixture:{manifest['fixture_id']}:{record['record_id']}",
                snapshot=doc,
            )
            key = (str(created["record_id"]), str(created["revision_id"]))
            seeded[key] = record
            sealed = record.get("revision_sha256")
            if sealed and created["content_sha256"] != sealed:
                raise FixtureAdapterError(
                    f"{record['record_id']}@{record['revision_id']} does not match its "
                    "sealed revision hash"
                )

        context = ContextService(db, config)
        bundle = await context.prepare(
            principal=principal,
            required="",
            query="",
            project_ids=[PROJECT_ID],
            claim_epoch=claim_epoch,
            pins=[
                {"identity": f"record:{record_id}", "revision_id": revision_id}
                for record_id, revision_id in seeded
            ],
        )
        rendered = bundle.to_markdown()

        # The delivery ledger is keyed by the sealed transport events, not by the
        # harness label, so it is replayed once; the label loop below then proves
        # that every harness carries the identical payload.
        observation = await cls._observe(
            db, context, principal, bundle, manifest, claim_epoch, seeded, rendered,
        )
        transports: dict[tuple[str, str], str] = {}
        observations: dict[tuple[str, str], dict] = {}
        for harness in harnesses or manifest["labels"]["harnesses"]:
            delivery = plan(
                rendered,
                bundle_id=bundle.bundle_id,
                harness=harness,
                supports_hooks=cls.supports_hooks and harness in HOOK_ENVELOPE_HARNESSES,
                prompt_mode=cls.prompt_mode,
                source="startup",
                session_id=principal.session_id,
                claim_epoch=claim_epoch,
            )
            # The payload leaves every transport unchanged; a harness that
            # reordered, re-wrapped or truncated it fails here.
            if delivery.payload != rendered or delivery.suppressed:
                raise FixtureAdapterError(f"harness {harness} did not carry the payload intact")
            if rendered and delivery.transport == STARTUP_GUIDANCE:
                guidance = startup_guidance(
                    rendered, harness=harness, bundle_id=bundle.bundle_id
                )
                if guidance.count(rendered.strip()) != 1:
                    raise FixtureAdapterError(f"harness {harness} guidance lost the payload")
            transports[(str(manifest["fixture_id"]), str(harness))] = delivery.transport
            observations[(str(manifest["fixture_id"]), str(harness))] = copy.deepcopy(
                observation
            )
        return cls(observations, transports)

    @classmethod
    async def _observe(cls, db, service, principal, bundle, manifest, claim_epoch, seeded,
                       rendered) -> dict:
        rules = manifest["delivery_rules"]
        budget = manifest["budget"]
        owner = {key: rules[key] for key in ("owner_kind", "owner_id", "claim_epoch")}
        selected = []
        for item in bundle.items:
            record = seeded.get((item.record_id, item.revision_id))
            if record is None:
                raise FixtureAdapterError("selection is not a sealed fixture record")
            selected.append({
                "record_id": record["record_id"],
                "revision_id": record["revision_id"],
                "kind": record["kind"],
                "evidence": (item.sources[0]["source_id"] if item.sources else record["evidence"]),
                "excerpt": item.text,
                "content_sha256": item.content_sha256,
                "authority": item.authority,
                "freshness": item.freshness,
                "verification": item.verification,
                "lifecycle": item.lifecycle,
            })
        omissions = []
        for omission in bundle.omissions:
            record = seeded.get((omission["record_id"], omission["revision_id"]))
            if record is None:
                raise FixtureAdapterError("omission names a record outside the fixture")
            omissions.append({
                "identity": f"{record['record_id']}@{record['revision_id']}:{record['kind']}",
                "reason": omission["reason"],
            })

        receipts = []
        for event in rules["events"]:
            async with db.immediate() as conn:
                existed = bool(await conn.scalar(
                    select(func.count()).select_from(knowledge_context_deliveries).where(
                        knowledge_context_deliveries.c.transport_key == event["transport_key"],
                    )
                ))
            result = await service.observe_delivery(
                bundle_id=bundle.bundle_id, principal=principal, transport=event["transport"],
                transport_key=event["transport_key"], rendered_sha256=bundle.content_sha256,
                state=event["final_state"], claim_epoch=claim_epoch,
            )
            receipts.append({
                "bundle_id": rules["bundle_id"],
                "transport": event["transport"],
                "transport_key": event["transport_key"],
                "final_state": result["state"],
                "new": not existed,
            })

        citations = await _citations(db, bundle, seeded, owner)
        byte_count = len(rendered.encode("utf-8"))
        diagnostic = bundle.budget.get("diagnostic")
        return {
            "selected": selected,
            "omissions": omissions,
            "rendered": rendered,
            "rendered_sha256": bundle.content_sha256,
            "owner": owner,
            "citations": citations,
            "deliveries": receipts,
            "usage": {
                "method": "upper_bound_bytes:utf8",
                "tokens": byte_count + budget["reserved_tokens"],
                "bytes": byte_count + budget["reserved_bytes"],
            },
            "diagnostics": [diagnostic] if diagnostic else [],
            # No clock, no bundle id, no transport: the report stays byte-stable
            # and identical across harness labels, which is what parity means.
            "metadata": {"format_version": bundle.format_version, "synthetic": True},
        }


class LocalModelFixtureAdapter(ContextBundleFixtureAdapter):
    """Local-model contract adapter: no hook, no argv prompt channel.

    A local model CLI is launched interactively with neither a SessionStart hook
    nor a prompt argument, so this adapter proves the payload still reaches the
    session — through explicit startup guidance — under the identical selection,
    budget and citation contract. It measures nothing about the model itself.
    """

    name = "local_model_contract"
    supports_hooks = False
    prompt_mode = "none"


async def _citations(db, bundle, seeded, owner) -> list[dict]:
    """Read this bundle's injected citations back, in the bundle's item order.

    ``knowledge_citations`` carries no ordering column, so the bundle's
    selection order is the only stable one. Rows are never collapsed: a second
    row for the same revision must reach the oracle as a second entry, which is
    exactly the duplicate the ``duplicate-citation`` negative fixture catches.

    A citation pins its exact revision through a composite foreign key and
    stores no content hash, so the reported hash is the one the prepared
    revision carries; a row naming any other revision fails here instead of
    being reported as agreement.
    """
    async with db.immediate() as conn:
        rows = (await conn.execute(
            select(knowledge_citations).where(
                knowledge_citations.c.bundle_id == UUID(bundle.bundle_id),
            )
        )).mappings().all()
    by_revision = {item.revision_id: item for item in bundle.items}
    order = {item.revision_id: index for index, item in enumerate(bundle.items)}
    ranked = sorted(rows, key=lambda row: order.get(str(row["revision_id"]), len(order)))
    citations = []
    for row in ranked:
        item = by_revision.get(str(row["revision_id"]))
        if (
            item is None
            or item.record_id != str(row["record_id"])
            or (str(row["record_id"]), str(row["revision_id"])) not in seeded
        ):
            raise FixtureAdapterError("citation names a revision outside the selection")
        if row["kind"] != "injected":
            raise FixtureAdapterError("delivery produced a non-injected citation")
        citations.append({
            "record_id": seeded[(str(row["record_id"]), str(row["revision_id"]))]["record_id"],
            "revision_id": seeded[(str(row["record_id"]), str(row["revision_id"]))]["revision_id"],
            "content_sha256": item.content_sha256,
            "kind": row["kind"],
            "owner": dict(owner),
        })
    return citations
