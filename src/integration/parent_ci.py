"""CI-only parent snapshot refs leave frozen delivery branches untouched."""
from __future__ import annotations

import hashlib
import logging

from sqlalchemy import select

from src.database.tables import (
    integration_repair_operations, projects, task_integration_checkpoints, tasks,
)
from src.integration.ci import AuthenticatedGitHubObserver, CIService, ParentCISubject
from src.integration.outbox import enqueue_integration_event

logger = logging.getLogger(__name__)


async def ensure_parent_store(git, store):
    store.parent.mkdir(parents=True, exist_ok=True)
    if not store.exists():
        result = await git.arun_git_result(
            ['init', '--bare', '--template=', str(store)], cwd=str(store.parent),
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr or 'parent CI store initialization failed')


async def publish_parent_snapshot(git, client, store, branch, head_sha, current):
    """Create an immutable CI ref; a conflicting remote ref is never overwritten."""
    if not await current():
        return False
    remote = await client.exact_head_ref(branch)
    if remote is not None:
        return remote == head_sha
    token = await client.installation_token()
    imported = await git.afetch_exact_oid_with_app_auth(
        str(store), repository=client.repository, token=token, oid=head_sha,
        destination_ref='refs/aq/parent-ci/' + hashlib.sha256(branch.encode()).hexdigest(),
    )
    if imported != head_sha or not await current():
        return False
    await git.apush_oid_with_app_auth(
        str(store), repository=client.repository, token=token,
        tip_oid=head_sha, branch=branch, expected_old_oid='0' * 40,
    )
    return True


class ParentCIService:
    def __init__(self, attestation, repository_binding_resolver):
        self.attestation = attestation
        self.db = attestation.db
        self.resolve_binding = repository_binding_resolver
        self.after = ''
        self.next_poll = 0.0

    async def tick(self, now):
        if now < self.next_poll:
            return
        self.next_poll = now + 30.0
        checkpoint = task_integration_checkpoints
        operation = integration_repair_operations
        statement = (
            select(
                tasks.c.id.label('task_id'), tasks.c.project_id,
                checkpoint.c.repository_id, checkpoint.c.generation,
                checkpoint.c.checkpoint_sha.label('head_sha'),
                operation.c.id.label('operation_id'), operation.c.policy_snapshot,
            )
            .select_from(tasks)
            .join(checkpoint, checkpoint.c.task_id == tasks.c.id)
            .join(operation, (operation.c.parent_task_id == tasks.c.id)
                  & (operation.c.episode_id == checkpoint.c.episode_id))
            .join(projects, projects.c.id == tasks.c.project_id)
            .where(
                tasks.c.id > self.after, checkpoint.c.state == 'verifying',
                checkpoint.c.current_verification_id.is_(None),
                operation.c.target_kind == 'parent',
                operation.c.state.in_(['active', 'escalated']),
                projects.c.status == 'ACTIVE',
                projects.c.hierarchical_integration_mode.in_(['hierarchy', 'train']),
                projects.c.integration_repository_id == checkpoint.c.repository_id,
            ).order_by(tasks.c.id).limit(20)
        )
        async with self.db._engine.connect() as conn:
            rows = (await conn.execute(statement)).mappings().all()
        self.after = rows[-1]['task_id'] if rows else ''
        for row in rows:
            try:
                await self.handle(dict(row))
            except Exception:
                logger.exception('Parent CI remains retryable for %s', row['task_id'])

    async def handle(self, row):
        repo = await self.db.get_repo(row['repository_id'])
        if repo is None:
            return
        binding = await self.resolve_binding(repo)
        if binding is None:
            return
        await ensure_parent_store(self.attestation.git, self.attestation._store(repo.id))
        required = row['policy_snapshot']['parent']['required_checks']
        subject = ParentCISubject(
            operation_id=row['operation_id'], parent_task_id=row['task_id'],
            generation=row['generation'], head_sha=row['head_sha'],
        )
        trust, client = await self.attestation._load_trust({
            'canonical_repository_id': repo.id,
            'repository_numeric_id': binding.repository_id,
            'repository_full_name': binding.full_name,
            'policy_snapshot': row['policy_snapshot'],
            'batch_id': row['operation_id'], 'revision': row['generation'],
            'candidate_sha': row['head_sha'],
            'required_check_version': required['version'],
            'required_check_names': tuple(required['names']),
            'ci_producer_id': required['producer_id'],
        }, boundary='parent')
        ci = CIService(self.db, trust, AuthenticatedGitHubObserver(client))

        async def current():
            async with self.db.immediate() as conn:
                return await ci._lock_parent_subject_on(conn, subject) is not None

        # One immutable ref per verification subject: retries observe the same
        # push, and late CI cannot become evidence for a changed generation.
        identity = hashlib.sha256(row['operation_id'].encode()).hexdigest()[:32]
        branch = f"aq/parent/{row['task_id']}/{identity}/{row['generation']}/{row['head_sha']}"
        if not await publish_parent_snapshot(
            self.attestation.git, client, self.attestation._store(repo.id),
            branch, row['head_sha'], current,
        ):
            return
        observed = await ci.observe_parent(subject)
        if observed['outcome'] not in {'green', 'red'}:
            return
        evidence_ids = observed['evidence_ids']
        if not evidence_ids:
            return
        event_id = 'parent-ci-' + hashlib.sha256(':'.join(evidence_ids).encode()).hexdigest()
        async with self.db.immediate() as conn:
            if await ci._lock_parent_subject_on(conn, subject) is None:
                return
            await enqueue_integration_event(
                conn, event_id=event_id, dedup_key=event_id,
                event_type='integration.ci_completed', project_id=row['project_id'],
                payload={
                    'operation_id': row['operation_id'], 'target_kind': 'parent',
                    'task_id': row['task_id'], 'generation': row['generation'],
                    'head_sha': row['head_sha'], 'evidence_ids': evidence_ids,
                    'evidence_id': evidence_ids[0],
                    'conclusion': 'success' if observed['outcome'] == 'green' else 'failure',
                }, available_at=self.attestation.clock(),
            )
