# Optional supervisor hourly reports

The deterministic digest remains the default. Narrative authoring requires both
`reports.hourly.enabled: true` and a healthy system activation of the reviewed
`supervisor-hourly-report` playbook. It is neither a required policy nor a default
activation. The shipped source has `enabled: false`. The reviewed bundle is seeded
into `vault/reviewed-playbooks/supervisor-hourly-report/`; explicitly import and
activate that bundle through the playbook operator surfaces when enabling it.
Review sample narratives before enabling it for general use.

```bash
aq playbook v2-import --path reviewed-playbooks/supervisor-hourly-report
aq playbook activate --playbook-id supervisor-hourly-report --artifact-sha256 <returned-hash>
```

Enable the hourly config after the reviewed activation is ready. Existing installs
receive new supervisor grants additively on reload; this feature uses the already
shipped `report_brief` and `report_submit` grants.

Authored reports require `reports.hourly.full_fleet_visibility: true` and an empty
`discord.digest.project_ids`: the global supervisor may author only for a full
fleet destination. Restricted destinations use scoped deterministic rendering.
The grace, daily cap, quiet hours and install-wide `reports.timezone` remain in
configuration. Quiet hours suppress author wakes, not deterministic information.
Disabling the feature or its authoring activation releases unclaimed fallback;
claimed payloads drain without rewriting them.

The two reviewed rules handle `digest.window_ready` and `timer.1m` by calling the
bounded `report_reconcile` command. Only install-wide services and capability
granted playbooks may request authoring. It queues one deterministic message per
reserved request. An event loss, replay or daemon restart cannot create another
message or lose the reservation. The supervisor has only the existing
`report_brief` and `report_submit` grants; it cannot select a Discord destination.
Its profile documents evidence, length and deadline discipline. No inline model
or worker task authors the report.

## Delivery library for report consumers

`src/delivery/message.py` owns stable markers, history reconciliation and bounded
retry/backoff. `src/delivery/dispatch.py` owns escalation-first dispatch and the
rate guard. It claims one item at a time, up to 20 per batch, checking priority
between sends. The daemon's digest pump selects oldest due records across the
hourly and shared outboxes; shared delivery still runs with the digest disabled.
Escalations and reviews retain their domain records and existing delivery services.

`digest_windows` keeps its established identity and `aq-dig` markers. New morning
and conversation domain commands reserve `outbound_deliveries` through
`reserve_outbound_delivery(owner_kind, owner_id, dedup_key, destination, payload,
due_at, now)`. Destination is a frozen `{transport: "discord", channel_id,
thread_id?}` mapping; payload includes `text`. Reservation freezes both and a
SHA-256 hash. Replays reuse the row; reuse of a dedup key for different content,
route or owner is refused. Consumer commands own visibility authorization and
server-resolved links before reservation. There is no arbitrary Discord-post
command. Morning summaries allow 1,500 characters including marker, conversation
messages 2,000, and hourly submissions retain their 1,200-character budget.

`claim_report_deliveries` leases the globally oldest due rows. Typed adapters use
the same message delivery engine and record receipts through guarded finish
operations. A crashed lease is reconciled before any network write. Missing
history permission or an unreconciled ambiguous outcome ends in visible `unknown`,
which the automatic dispatcher never resends. Permission faults back off with a
bounded attempt budget; they neither spin nor block escalations. Inspect stored
owner rows using `get_outbound_delivery` / `list_outbound_deliveries`; transport
receipts are separate from report content and evidence coverage.

The new schema is Alembic revision `a00000000031`. Its create is idempotent and
rollback preserves the ledger. Operator upgrades use the normal daemon migration
path; workers never migrate the operator database.
