"""Keep the Git objects behind a delivery observation, independently of branches.

These refs are historical evidence, never permission to publish or a substitute
for checking containment against the current target. Metadata commits retain
both sides of a squash and the source base, so GC cannot erase the proof.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

from src.git.identity import LEDGER_IDENTITY
from src.git.manager import GitError, RemoteRefState
from src.integration.delivery_truth import DeliveryState
from src.integration.provenance import GitProvenance

PREFIX = "refs/aq/provenance/delivery/"


async def retain_objects(snapshot, oids):
    """Keep orphaned history without asserting that it was ever delivered."""
    provenance = GitProvenance(
        snapshot.git, snapshot.store, repository_url=snapshot.repository_url,
    )
    refs = {}
    for oid in sorted(set(filter(None, oids))):
        await provenance.exact(oid)
        refs["refs/aq/provenance/retained/" + oid] = oid
    if not refs:
        return {}
    observed = await snapshot.git.als_remote_qualified_refs(
        snapshot.store, list(refs), repository_url=snapshot.repository_url,
    )
    for ref, oid in refs.items():
        remote = observed[ref]
        if remote.state is RemoteRefState.ERROR or (
            remote.state is RemoteRefState.PRESENT and remote.oid != oid
        ):
            raise GitError("retained object remote cannot be verified")
    missing = {ref: oid for ref, oid in refs.items()
               if observed[ref].state is RemoteRefState.ABSENT}
    if missing:
        await snapshot.git.apush_new_refs(
            snapshot.store, missing, qualified=True, repository_url=snapshot.repository_url,
        )
    verified = await snapshot.git.als_remote_qualified_refs(
        snapshot.store, list(refs), repository_url=snapshot.repository_url,
    )
    for ref, oid in refs.items():
        if verified[ref].state is not RemoteRefState.PRESENT or verified[ref].oid != oid:
            raise GitError("retained object publication was not confirmed")
        await provenance.run("update-ref", ref, oid)
    return refs


async def retain_delivery(snapshot, evidence) -> dict:
    """Publish an immutable witness before repairing any disposable projection.

    The observation time belongs in the database receipt, not the content
    address: retrying an uncertain push must produce the very same ref/object.
    """
    if evidence.state not in {DeliveryState.CONTAINED, DeliveryState.NO_CHANGE}:
        raise ValueError("only Git containment can be retained as delivery")
    if (evidence.target_oid != snapshot.target_oid or
            evidence.request.repository_id != snapshot.repository_id or
            evidence.request.project_id != snapshot.project_id or
            evidence.request.target_ref != snapshot.target_ref):
        raise ValueError("delivery witness does not belong to this snapshot")
    provenance = GitProvenance(
        snapshot.git, snapshot.store, repository_url=snapshot.repository_url,
    )
    parents = list(dict.fromkeys(filter(None, (
        evidence.source_oid, evidence.target_oid, getattr(evidence, "source_base", None),
    ))))
    if not evidence.source_oid or not evidence.target_oid:
        raise ValueError("delivery requires exact source and target objects")
    for oid in parents:
        await provenance.exact(oid)
    record = {
        "version": 1, "kind": "delivery_observation",
        "request": asdict(evidence.request),
        "source_oid": evidence.source_oid, "target_oid": evidence.target_oid,
        "source_base": getattr(evidence, "source_base", None),
        "method": evidence.reason,
    }
    body = json.dumps(record, sort_keys=True, separators=(",", ":"))
    ref = PREFIX + hashlib.sha256(body.encode()).hexdigest()
    tree = await provenance.run("rev-parse", evidence.target_oid + "^{tree}")
    args = ["commit-tree", tree]
    for oid in parents:
        args.extend(("-p", oid))
    marker = await provenance.run(*args, stdin=body + "\n", env={
        **LEDGER_IDENTITY.env(),
        "GIT_AUTHOR_DATE": "@0 +0000", "GIT_COMMITTER_DATE": "@0 +0000",
    })
    remote = (await snapshot.git.als_remote_qualified_refs(
        snapshot.store, [ref], repository_url=snapshot.repository_url,
    ))[ref]
    if remote.state is RemoteRefState.ERROR:
        raise GitError("delivery witness remote is unavailable")
    if remote.state is RemoteRefState.PRESENT and remote.oid != marker:
        raise GitError("immutable delivery witness disagrees with remote")
    if remote.state is RemoteRefState.ABSENT:
        await snapshot.git.apush_new_refs(
            snapshot.store, {ref: marker}, qualified=True,
            repository_url=snapshot.repository_url,
        )
    verified = (await snapshot.git.als_remote_qualified_refs(
        snapshot.store, [ref], repository_url=snapshot.repository_url,
    ))[ref]
    if verified.state is not RemoteRefState.PRESENT or verified.oid != marker:
        raise GitError("delivery witness publication was not confirmed")
    await provenance.run("update-ref", ref, marker)
    return {**record, "ref": ref, "oid": marker}
