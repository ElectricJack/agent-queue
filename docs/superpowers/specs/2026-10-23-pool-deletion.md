# Pool deletion

`aq pool delete --profile-id <id>` and the pool settings Delete action remove
the global pool profile, including its bounds and enabled state. Busy sessions,
routed tasks, provider holds and profile-owned vault content are consequences
to warn about, never reasons to refuse a valid deletion.

One confirmation dialog offers `depth: profile` (default: remove profile.md,
preserve other profile content) or `depth: directory` (remove the complete
vault/agent-types/<id> directory). Both record a retirement tombstone before
removing files. Seeding, rung derivation and vault sync honor that tombstone;
explicit profile-reseed is the supported restoration path.

All pool instances, across projects, receive stopped intent and go through the
existing instance-fenced pool teardown. This interrupts held work; confirmed
stops release/requeue claims through existing ownership guards. An unconfirmed
stop retains its resources and is retried by the reconciler, including after a
restart. No task is closed or declared successful by deletion.

Deleting the database profile atomically invalidates its task routes and
provider intent, preserving route history. Queued tasks (including derived
provider holds) return to the bound router. Automatic provider pauses are
resumed; manual pauses and unrelated holds remain intact. No route can select
the deleted profile. The command emits an audited pool.deleted event and
returns warnings about incomplete physical cleanup without undoing deletion.

The dashboard removes the confirmed deleted row immediately and invalidates
pool, profile, task, session and provider views. Errors remain visible in the
dialog; cleanup warnings remain visible after a successful delete.
