# Explicit integration publication delegation

Named integration publication is an exceptional, server-issued authority. Task
descriptions, branch names and worker capabilities alone never grant it. This
extends the ordinary held-task publication contract in [github-access.md](../github-access.md) §8;
it does not change ordinary worker push or PR authorization.

## Issue and revoke

`integration_delegate_publication` and `integration_revoke_publication` go through
CommandHandler. Only the existing local operator / live named supervisor check
(`integration_operator`) admits them. Issue is available with Git-first active in
hierarchy/train projects. The operator supplies a unique delegation id, project,
designated repository, held task, session, claim epoch, named target branch,
expected target OID, expected current owner fence (0 for no owner), reason and
TTL (at most 480 seconds). The server captures the session instance, source
branch and repository URL. Both the claim and repository must match.

Issue compares the exact existing owner fence under `BranchLock` exclusion and
refuses any unexpired writer, including an earlier delegation. It acquires a
new finite integration lease with holder `publication-delegation:<id>`. It
does not transfer an active train/repair writer's authority or renew that lease.
Missing owner rows can be created only with expected fence 0. Legacy owner rows
require recovery to the managed protocol first. Default branches, main, dev,
staging and promotion chain branches cannot be delegated by this command.

The grant adds no integration table (the integration storage ratchet,
`tests/test_integration_size.py`). It lives on the target ref's own
`integration_branch_owners` lease row: the nullable JSONB `delegation` column
records the full binding, issuing identity/reason, predecessor fence, granted
fence, deadline and terminal revoke or publication evidence while the row's
`owner_id` is `publication-delegation:<id>`. Release keeps that owner id, so the
audit stays readable; the ref's next acquisition clears it. Reusing an id with
different inputs is refused; an exact retry returns its existing audit without
reviving authority. Once a later owner has taken the ref, the id is unknown:
revoke and publication refuse it, and the earlier grant's evidence is gone with it.
Revocation is idempotent and releases only this delegation's still-current fence.
It does not release a replacement owner's fence. Expiry, claim/session changes,
repository rebinding, revocation or a superseding fence invalidate the grant.

## Worker publication

The worker explicitly selects `aq git push --branch <target>
--publication-delegation-id <id>` (also supported by `push-branch`). The local
source remains the held task's recorded branch. A grant never authorizes a PR,
checkout, arbitrary source ref or access to another workspace. An optional
expected remote OID must equal the issued OID; the server always uses that OID.

The HTTP scope check validates the fresh row, live claim, session instance,
repository and current lease. The plugin independently does the same for native
dispatch. At publication it locks session/task/agent, then target owner and grant,
and repeats validation. These locks and the bounded BranchLock exclusion remain
held until the authenticated push and publication audit commit finish. Revoke,
claim replacement and ref reacquisition serialize with that operation; checks
performed only at dispatch never establish publication authority.

The source resolves once to an immutable OID. Its ancestry must contain the exact
expected target OID. `apush_validated_delivery` applies reserved-path and identity
checks against that base and publishes that same OID with the exact remote lease.
Thus remote movement refuses instead of overwriting an external writer. Objects
must already be available locally (the worker can fetch/merge the target with
`aq git pull --branch <target>`). Successful publication records the resulting
OID/time, consumes the grant and releases its fence. It does not invent delivery
receipts, change task branch/checkpoint identity, waive train gates or close work.

## Delivery after external fast-forward

With `integration.git_first: active`, current source containment on the epic
already releases sibling prerequisite admission without SQL receipts. Advisory
observations can withhold work until their 30-second cache expires; actual claims
take uncached observations. An authorized external fast-forward is subject to
the same freshness and identity guards as a train publication. Shadow mode
continues to require receipts. Diagnosis needs the installed revision, daemon's
actual active/shadow configuration, registered prerequisite observer and current
source/target identities; task completion or a receipt alone is insufficient.
