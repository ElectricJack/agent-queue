# Retiring legacy `opus` / `sonnet` profiles

> Applies only if your deployment already has ``opus`` or ``sonnet``
> agent profiles. Fresh installs skip this doc entirely — the new
> ``claude-opus`` and ``claude-sonnet`` defaults get auto-installed by
> ``ensure_vault_layout`` on first startup.

## Why this doc exists

Before the Claude-family default profile shipped, some deployments
created custom ``opus`` and ``sonnet`` profiles manually. The new
shipped defaults are named ``claude-opus`` and ``claude-sonnet`` and
share a single memory scope (``agenttype_claude``) via the
``memory_scope_id`` mechanism.

The legacy ``opus`` / ``sonnet`` profiles don't get auto-renamed — you
retire them explicitly so the shipped defaults take over cleanly.

## Prerequisites

- Database migration applied (``alembic upgrade head``) — adds the
  ``memory_scope_id`` column.
- Daemon can start (the ``discord.py<2.6`` pin in ``pyproject.toml``
  should cover the Python 3.12 incompatibility that older versions
  introduce).

## Steps

1. **Stop the daemon.**

   ```bash
   aq stop
   ```

2. **Delete the legacy profiles.** The CLI clears any FK references
   before deletion — tasks that had ``profile_id='opus'`` fall back to
   the project's default profile or to ``claude-code``.

   ```bash
   aq agent delete-profile --profile-id opus
   aq agent delete-profile --profile-id sonnet
   ```

3. **Remove the stale vault directories.** Each legacy profile had its
   own ``memory/`` subdirectory; once the new Claude-family profiles
   route memory through the shared ``agenttype_claude`` scope, these
   are orphaned.

   ```bash
   rm -rf ~/.agent-queue/vault/agent-types/opus
   rm -rf ~/.agent-queue/vault/agent-types/sonnet
   ```

4. **Start the daemon.** ``ensure_vault_layout`` creates the new
   profile directories, ``claude/memory/`` (shared scope), and syncs
   the new profile markdown into the database with
   ``memory_scope_id: claude`` already set.

   ```bash
   aq start
   ```

5. **Verify.**

   ```bash
   aq --json agent list-profiles
   ```

   Expected ids: ``claude-code``, ``claude-opus``, ``claude-sonnet``,
   ``supervisor`` (and any custom profiles you've kept). The
   ``claude-opus`` and ``claude-sonnet`` entries should show
   ``memory_scope_id: claude``.

   On disk:

   ```bash
   ls ~/.agent-queue/vault/agent-types/
   # claude  claude-code  claude-opus  claude-sonnet  supervisor
   ```

## Existing memory in `agenttype_opus` / `agenttype_sonnet`

The legacy Milvus collections aren't auto-migrated. If either
contained meaningful data you want to keep, dump them into the new
shared collection before step 4:

```python
from pymilvus import MilvusClient

client = MilvusClient("~/.agent-queue/memsearch/milvus.db")
for source in ("aq_agenttype_opus", "aq_agenttype_sonnet"):
    if source in client.list_collections():
        rows = client.query(collection_name=source, filter="", output_fields=["*"])
        if rows:
            client.insert(collection_name="aq_agenttype_claude", data=rows)
        client.drop_collection(collection_name=source)
```

For most users these collections are empty or stale; skip this step if
``find ~/.agent-queue/vault/agent-types/opus/memory -type f`` returns
nothing substantive.

## Rollback

No one-click rollback is provided. To revert:

1. Re-create the ``opus`` / ``sonnet`` profiles manually via
   ``aq agent create-profile``.
2. Restore the ``vault/agent-types/{opus,sonnet}/`` dirs from your
   backup.
3. Update any tasks that had their ``profile_id`` cleared during
   ``aq agent delete-profile``.

If you haven't run steps 2–3 yet, you can bail out simply by skipping
them. The new Claude-family profiles co-exist with the legacy ones —
they just end up with separate memory scopes.
