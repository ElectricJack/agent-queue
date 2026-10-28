"""Explicit, evidence-fenced recovery of historical stopped task holders.

This is deliberately separate from reconciliation: stopped rows are provenance,
not by themselves permission to release a writer or discard unpublished work.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from pathlib import Path

from sqlalchemy import and_, or_, select, update

from src.claim_file import claim_file_identity, read_claim_file
from src.commands.supervisor_authority import operator_or_global_supervisor
from src.commands.workspace_checkout_repair import _identity
from src.database.queries.continuation_queries import retire_task_waits
from src.database.tables import (
    agents, continuations, integration_branch_owners, integration_candidate_ref_mutations,
    jobs, job_workspace_pins, projects, repos, sessions, task_session_attempts, tasks,
    wait_records, workspaces,
)
from src.git.manager import GitError, commit_identity, is_valid_git_oid, repository_urls_match
from src.integration.delegate_release import live_integration_owner
from src.integration.finished_owners import stop_confirmer_for
from src.jobs.identity import processes, require_readable_identity
from src.jobs.policy import TERMINAL
from src.orchestrator.task_checkpoint import capture_checkpoint
from src.orchestrator.worktree_manager import WorktreeSlotManager
from src.sessions.proctable import workspace_quiescent
from src.wait_runtime.restart import restart_lock
from src.waits.graph import lock_graph


class Unproved(ValueError):
    """No ownership changes may be made."""


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


async def _rows(conn, table, predicate, locked):
    statement = select(table).where(predicate).order_by(table.c.id)
    if locked:
        statement = statement.with_for_update()
    return [dict(r) for r in (await conn.execute(statement)).mappings()]


async def _snapshot(conn, args, *, locked=False):
    task_rows = await _rows(conn, tasks, tasks.c.id == args['task_id'], locked)
    project_rows = await _rows(conn, projects, projects.c.id == args['project_id'], locked)
    if not task_rows or not project_rows or task_rows[0]['project_id'] != args['project_id']:
        raise Unproved('Task and project must exist and match')
    task, project = task_rows[0], project_rows[0]
    named = await _rows(conn, sessions, sessions.c.id == args['session_id'], False)
    if not named:
        raise Unproved('Historical session identity is missing')
    if task['status'] in ('ASSIGNED', 'IN_PROGRESS'):
        raise Unproved('Active task status requires normal stop/recovery first')
    waits = await _rows(conn, wait_records, and_(wait_records.c.owner_id == task['id'],
        wait_records.c.owner_kind.in_(('task', 'scheduler'))), locked)
    intents = await _rows(conn, continuations, continuations.c.task_id == task['id'], locked)
    stages = [(i['facts'] or {}).get('fresh_workspace', {}).get('workspace_id') for i in intents]
    slots = await _rows(conn, workspaces, or_(
        workspaces.c.locked_by_task_id == task['id'],
        workspaces.c.workspace_path == named[0]['work_dir'],
        workspaces.c.id.in_([s for s in stages if s] + [args.get('workspace_id', '')]),
    ), locked)
    if args.get('workspace_id') and args['workspace_id'] not in {s['id'] for s in slots}:
        raise Unproved('Requested workspace is missing')
    if any(s['project_id'] != task['project_id'] or
           s['locked_by_task_id'] not in (None, task['id']) for s in slots):
        raise Unproved('Workspace has a foreign project or successor task')
    owners = await _rows(conn, integration_branch_owners, or_(
        integration_branch_owners.c.owner_id == task['id'],
        integration_branch_owners.c.workspace_id.in_([s['id'] for s in slots]),
        integration_branch_owners.c.confirmed_workspace_id.in_([s['id'] for s in slots]),
    ), locked)
    owners = [o for o in owners if o['handoff_state'] != 'released' or o['holder']]
    repositories = await _rows(conn, repos, repos.c.id.in_(list(filter(None, (
        task['repo_id'], project['integration_repository_id'],
    )))), locked)
    bases = await _rows(conn, workspaces, workspaces.c.id.in_(
        [s['base_workspace_id'] for s in slots if s['base_workspace_id']],
    ), locked)
    if owners and await conn.scalar(select(integration_candidate_ref_mutations.c.id).where(
        integration_candidate_ref_mutations.c.state == 'reserved', or_(*[
            and_(integration_candidate_ref_mutations.c.repository_id == o['repository_id'],
                 integration_candidate_ref_mutations.c.branch == o['ref'].removeprefix('refs/heads/'))
            for o in owners
        ]),
    ).limit(1)):
        raise Unproved('A reserved publication retains the branch recovery fence')
    pin_query = select(job_workspace_pins).where(
        job_workspace_pins.c.workspace_id.in_([s['id'] for s in slots]),
    ).order_by(job_workspace_pins.c.job_id)
    if locked:
        pin_query = pin_query.with_for_update()
    pins = [dict(r) for r in (await conn.execute(pin_query)).mappings()]
    job_rows = await _rows(conn, jobs, or_(jobs.c.id.in_([p['job_id'] for p in pins]),
        and_(jobs.c.state.not_in(TERMINAL), or_(jobs.c.task_id == task['id'],
            jobs.c.workspace_id.in_([s['id'] for s in slots])))), locked)
    relevant_ids = {args['session_id']} | {w['session_id'] for w in waits}
    relevant_ids |= {i['new_session_id'] for i in intents}
    relevant_ids |= {o['session_id'] for o in owners} | {j['submitter_session_id'] for j in job_rows}
    session_rows = await _rows(conn, sessions, or_(
        sessions.c.id.in_([s for s in relevant_ids if s]), sessions.c.task_id == task['id'],
        sessions.c.agent_id == task['assigned_agent_id'] if task['assigned_agent_id'] else False,
        sessions.c.work_dir.in_([s['workspace_path'] for s in slots]),
    ), locked)
    if not {s for s in relevant_ids if s} <= {s['id'] for s in session_rows}:
        raise Unproved('A retained holder has no session identity')
    attempts = await _rows(conn, task_session_attempts,
                           task_session_attempts.c.task_id == task['id'], locked)
    agent_rows = await _rows(conn, agents, or_(
        agents.c.id.in_([s['agent_id'] for s in session_rows if s['agent_id']]),
        agents.c.current_task_id == task['id'],
    ), locked)
    if await live_integration_owner(conn, [task['id']]):
        raise Unproved('A live integration operation owns the task')
    return dict(task=task, project=project, workspaces=slots, owners=owners, pins=pins,
                jobs=job_rows, sessions=session_rows, attempts=attempts, agents=agent_rows,
                waits=waits, continuations=intents, repositories=repositories, bases=bases,
                reason=args['reason'])


def _bindings(snapshot, args):
    task = snapshot['task']
    by_session = {s['id']: s for s in snapshot['sessions']}
    holder = by_session[args['session_id']]

    def bound(s):
        return (s['project_id'] in (None, task['project_id']) and (
            s['task_id'] == task['id'] or any(
                a['session_id'] == s['id'] and a['project_id'] == task['project_id']
                and a['session_started_at'] == s['started_at'] for a in snapshot['attempts'])))

    if not bound(holder):
        raise Unproved('Historical session has no positive task binding or attempt')
    if task['assigned_agent_id'] and (
        task['assigned_agent_id'] != holder['agent_id'] or
        holder['last_claim_epoch'] != task['claim_epoch']
    ):
        raise Unproved('Assignment is not the exact historical session claim')
    for s in snapshot['sessions']:
        if (s['state'] != 'stopped' or s['desired_state'] != 'stopped'
                or not s['instance_token'] or s['ended_at'] is None):
            raise Unproved(f"Session {s['id']} is not positively stopped")
        if s['task_id'] not in (None, task['id']):
            raise Unproved(f"Session {s['id']} now belongs to another task")
    if any(a['current_task_id'] not in (None, task['id']) for a in snapshot['agents']):
        raise Unproved('A retained agent now holds a successor task')
    slots = {s['id']: s for s in snapshot['workspaces']}
    for slot in slots.values():
        if not any(bound(s) and s['work_dir'] == slot['workspace_path']
                   for s in snapshot['sessions']):
            raise Unproved('Workspace has no positive historical task/session binding')
        if slot['locked_by_agent_id'] and not any(
            bound(s) and s['agent_id'] == slot['locked_by_agent_id']
            and s['work_dir'] == slot['workspace_path'] for s in snapshot['sessions']
        ):
            raise Unproved(f"Workspace {slot['id']} has no exact historical session")
        if slot['job_pin_count'] != sum(p['workspace_id'] == slot['id'] for p in snapshot['pins']):
            raise Unproved('Workspace pin count is inconsistent')
    branch = 'refs/heads/' + str(task['branch_name'] or '').removeprefix('refs/heads/')
    for owner in snapshot['owners']:
        s = by_session.get(owner['session_id'])
        if (owner['owner_id'] != task['id'] or owner['owner_role'] not in ('worker', 'repair')
                or owner['repository_id'] not in {r['id'] for r in snapshot['repositories']}
                or owner['ref'].removeprefix('refs/heads/') != branch.removeprefix('refs/heads/')
                or owner['handoff_state'] != 'attached'
                or owner['fence'] is not None or owner['holder'] or owner['delegation']
                or s is None or not bound(s) or owner['workspace_id'] not in slots
                or s['work_dir'] != slots[owner['workspace_id']]['workspace_path']):
            raise Unproved('Branch owner needs its own authority/delegation recovery fence')
    for job in snapshot['jobs']:
        slot, s = slots.get(job['workspace_id']), by_session.get(job['submitter_session_id'])
        if (job['state'] not in TERMINAL or job['cleanup_blocked'] or job['ended_at'] is None
                or job['owner_kind'] != 'task' or job['owner_id'] != task['id']
                or job['task_id'] != task['id'] or job['project_id'] != task['project_id']
                or job['claim_epoch'] != task['claim_epoch'] or not job['runner_nonce']
                or s is None or not bound(s) or slot is None
                or job['workspace_generation'] != slot['generation']):
            raise Unproved('Job is live, unproved, foreign, or requires cleanup reconciliation')
    for pin in snapshot['pins']:
        if pin['generation'] != slots[pin['workspace_id']]['generation']:
            raise Unproved('Job pin generation does not match workspace')
    return holder


def _file_hash(path):
    if path.is_symlink():
        return {'symlink': str(path.readlink())}
    if not path.exists():
        return None
    if not path.is_file():
        raise Unproved(f'Cannot snapshot special file or submodule {path}')
    return {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'mode': path.stat().st_mode}


async def _source(git, path):
    identity = await _identity(git, path)
    listing = await git.arun_git_result(
        ['ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=path,
    )
    if listing.returncode:
        raise Unproved('Cannot inventory source files')
    names = listing.stdout
    index = Path(await git._arun(['rev-parse', '--git-path', 'index'], cwd=path))
    if not index.is_absolute():
        index = Path(path) / index
    return {**identity, 'head': await git._arun(['rev-parse', '--verify', 'HEAD'], cwd=path),
            'branch': await git._arun(['rev-parse', '--symbolic-full-name', 'HEAD'], cwd=path),
            'git_dir': await git._arun(['rev-parse', '--absolute-git-dir'], cwd=path),
            'index': await asyncio.to_thread(_file_hash, index),
            'files': await asyncio.to_thread(lambda: {
                n: _file_hash(Path(path) / n) for n in sorted(set(names.split('\0'))) if n}),
            'claim': read_claim_file(path), 'claim_inode': claim_file_identity(path)}


async def _evidence(handler, snapshot, args):
    holder = _bindings(snapshot, args)
    git = handler.orchestrator.git
    sources = {}
    paths = {s['workspace_path'] for s in snapshot['workspaces']}
    paths.add(holder['work_dir'])
    if paths != {s['workspace_path'] for s in snapshot['workspaces']}:
        raise Unproved('Every recovery source must have a registered workspace')
    for path in sorted(paths):
        sources[path] = await _source(git, path)
        authorized = snapshot['project']['repo_url'] or next((
            r['url'] for r in snapshot['repositories']
            if r['id'] == (snapshot['task']['repo_id'] or
                           snapshot['project']['integration_repository_id'])
        ), None)
        slot = next((s for s in snapshot['workspaces'] if s['workspace_path'] == path), None)
        if slot:
            kind = await handler.db.resolve_workspace_kind(args['project_id'],
                                                           slot['kind_id'] or 'project-repo')
            authorized = kind.repo_url or authorized if kind else authorized
            if slot['slot_index'] is not None:
                sentinel = WorktreeSlotManager.read_sentinel(path)
                if (sentinel is None or sentinel.project_id != args['project_id']
                        or sentinel.workspace_id not in ('', slot['id'])
                        or sentinel.base_workspace_id != slot['base_workspace_id']
                        or sentinel.slot_index != slot['slot_index']
                        or sentinel.task_id not in (None, args['task_id'])):
                    raise Unproved('Workspace sentinel does not prove this source')
                sources[path]['sentinel'] = sentinel.to_dict()
                base = next((b for b in snapshot['bases']
                             if b['id'] == slot['base_workspace_id']), None)
                if base is None or base['project_id'] != args['project_id']:
                    raise Unproved('Workspace base registration is unavailable')
                base_identity = await _identity(git, base['workspace_path'])
                if any(base_identity[k] != sources[path][k] for k in (
                    'common_directory', 'objects_device', 'objects_inode',
                )):
                    raise Unproved('Workspace does not share its registered base object store')
                sources[path]['base_identity'] = base_identity
        if not authorized or not repository_urls_match(
            sources[path]['resolved_origin'], authorized, base=path,
        ):
            raise Unproved('Source origin does not match the authorized project repository')
        claim = sources[path]['claim']
        if claim and (claim.get('task_id') != args['task_id']
                      or claim.get('session_id') not in {s['id'] for s in snapshot['sessions']}
                      or claim.get('claim_epoch') != snapshot['task']['claim_epoch']):
            raise Unproved('Source has a foreign or successor claim marker')
        if (Path(path) / '.aq/claim.json').exists() and not claim:
            raise Unproved('Source has an unreadable claim marker')
    branch = snapshot['task']['branch_name']
    refs = {}
    for path in sources:
        if branch:
            ref = 'refs/heads/' + branch.removeprefix('refs/heads/')
            listing = await git._arun(
                ['for-each-ref', '--format=%(refname) %(objectname)', ref], cwd=path,
            )
            tips = [line.partition(' ')[2] for line in listing.splitlines()
                    if line.partition(' ')[0] == ref]
            value = tips[0] if len(tips) == 1 else None
            if value:
                if not is_valid_git_oid(value):
                    raise Unproved('Task branch tip is ambiguous')
                refs[f'{path}:{ref}'] = {'path': path, 'oid': value}
    for intent in snapshot['continuations']:
        if intent['saved_head']:
            oid = intent['saved_head']
            if not is_valid_git_oid(oid):
                raise Unproved('Saved continuation commit is invalid')
            found = None
            for path in sources:
                try:
                    if await git._arun(['rev-parse', '--verify', f'{oid}^{{commit}}'], cwd=path) == oid:
                        found = path
                        break
                except GitError:
                    continue
            if found is None:
                raise Unproved('Saved continuation commit is unavailable; retain ownership')
            refs[f"continuation:{intent['id']}"] = {'path': found, 'oid': oid}
    return {'sources': sources, 'commits': refs}


async def _prove_stopped(handler, snapshot, evidence):
    confirm = stop_confirmer_for(handler.orchestrator)
    if confirm is None:
        raise Unproved('No provider stop confirmation is available')
    paths = tuple(evidence['sources']) + tuple(s['git_dir'] for s in evidence['sources'].values())
    for session in snapshot['sessions']:
        if await confirm(session) is not True:
            raise Unproved(f"Provider cannot confirm stopped session {session['id']}")
        if await workspace_quiescent(paths, session['instance_token']) is not True:
            raise Unproved('Source has a writer or local process proof is unavailable')
    for job in snapshot['jobs']:
        if job['pid'] is not None:
            await require_readable_identity({**job, 'nonce': job['runner_nonce']})
        if await processes(job['runner_nonce']):
            raise Unproved('A terminal job still has a runner process')


async def repair(handler, args):
    operator, refusal = await operator_or_global_supervisor(handler.db)
    if refusal:
        return {'success': False, 'error_code': 'global_operator_required', 'error': refusal}
    required = {'project_id', 'task_id', 'session_id', 'reason'}
    if (not required <= args.keys() or args.keys() - required - {
        'workspace_id', 'apply', 'expected_proof',
    } or any(not isinstance(args[k], str) or not args[k].strip() for k in required)
            or not isinstance(args.get('apply', False), bool)):
        return {'success': False, 'error_code': 'invalid_arguments', 'error': 'Exact identities required'}
    apply = args.get('apply', False)
    if apply and not args.get('expected_proof'):
        return {'success': False, 'error_code': 'proof_required', 'error': 'Preview before applying'}
    try:
        async with restart_lock(handler, args['task_id']):
            async with handler.db._engine.connect() as conn:
                snapshot = await _snapshot(conn, args)
            evidence = await _evidence(handler, snapshot, args)
            await _prove_stopped(handler, snapshot, evidence)
            proof = _digest({'snapshot': snapshot, 'evidence': evidence})
            if apply and proof != args['expected_proof']:
                return {'success': False, 'error_code': 'proof_changed',
                        'error': 'Repair evidence changed; preview again', 'proof': proof}
            if not apply:
                return dict(success=True, preview=True, changed=False, proof=proof,
                            snapshot=snapshot, evidence=evidence, checkpoints=[])
            receipts = []
            git = handler.orchestrator.git

            async def validate():
                if await _evidence(handler, snapshot, args) != evidence:
                    raise Unproved('Git source changed during preservation')

            async def save(saved):
                receipts.append(saved)

            with commit_identity(git.resolve_commit_identity(await handler.db.get_project(
                args['project_id'],
            ))):
                for path in evidence['sources']:
                    await capture_checkpoint(handler.db, git, args['task_id'], path,
                                             validate=validate, save=save)
            for key, commit in evidence['commits'].items():
                ref = f'refs/aq/retained-holder/{uuid.uuid4().hex}'
                await git._arun(['update-ref', ref, commit['oid'], '0' * len(commit['oid'])],
                                cwd=commit['path'])
                receipts.append({'ref': ref, **commit, 'source_identity': key})
            # External effects only add preservation refs. A stale/failed transaction
            # leaves all reservations and files intact; those extra refs are harmless.
            async with handler.db.immediate() as conn:
                await lock_graph(conn)
                current = await _snapshot(conn, args, locked=True)
                if current != snapshot:
                    raise Unproved('Database ownership changed during preservation')
                await validate()
                await _prove_stopped(handler, current, evidence)
                operator, refusal = await operator_or_global_supervisor(handler.db)
                if refusal:
                    raise Unproved(refusal)
                now = time.time()
                for owner in snapshot['owners']:
                    await conn.execute(update(integration_branch_owners).where(
                        integration_branch_owners.c.id == owner['id'],
                    ).values(handoff_state='released', session_id=None, workspace_id=None,
                             confirmed_workspace_id=owner['workspace_id'],
                             fence_token=owner['fence_token'] + 1, updated_at=now))
                for pin in snapshot['pins']:
                    await handler.db._release_job_pin(conn, pin['job_id'])
                for slot in snapshot['workspaces']:
                    await conn.execute(update(workspaces).where(workspaces.c.id == slot['id']).values(
                        locked_by_task_id=None, locked_by_agent_id=None, locked_at=None,
                        lock_mode=None, generation=slot['generation'] + 1, enabled=False,
                        disabled_reason='operator: retained holder work preserved for salvage',
                        disabled_at=now,
                    ))
                await retire_task_waits(conn, args['task_id'], reason='retained_holder_recovery',
                                        now=now)
                for intent in snapshot['continuations']:
                    await conn.execute(update(continuations).where(
                        continuations.c.id == intent['id'],
                    ).values(facts={**intent['facts'], 'restart_released_at': now,
                                    'retained_holder_proof': proof}))
                await conn.execute(update(tasks).where(tasks.c.id == args['task_id']).values(
                    assigned_agent_id=None,
                ))
                for session in snapshot['sessions']:
                    if (session['task_id'] == args['task_id'] or (
                        session['task_id'] is None
                        and session['last_claim_epoch'] == snapshot['task']['claim_epoch']
                        and session['agent_id'] == snapshot['task']['assigned_agent_id']
                    )):
                        await conn.execute(update(sessions).where(
                            sessions.c.id == session['id'],
                        ).values(task_id=None, claim_phase=None, claim_phase_at=None,
                                 last_claim_result='released'))
                for agent in snapshot['agents']:
                    if agent['current_task_id'] == args['task_id']:
                        await conn.execute(update(agents).where(agents.c.id == agent['id']).values(
                            current_task_id=None, state='IDLE',
                        ))
                event_id = await handler.db.log_event(
                    'project.retained_holder_recovered', project_id=args['project_id'],
                    task_id=args['task_id'], conn=conn, payload=json.dumps({
                        'operator_id': operator, 'proof': proof, 'snapshot': snapshot,
                        'evidence': evidence, 'checkpoints': receipts,
                    }),
                )
            return dict(success=True, preview=False, changed=True, proof=proof,
                        snapshot=snapshot, evidence=evidence, checkpoints=receipts, event_id=event_id)
    except (Unproved, GitError, OSError, RuntimeError) as exc:
        return {'success': False, 'error_code': 'retained_holder_unproved', 'error': str(exc)}
