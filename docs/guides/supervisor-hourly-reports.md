# Supervisor hourly and morning reports

## Optional hourly authoring

The deterministic digest remains the default. Narrative authoring requires both
`reports.hourly.enabled: true` and a healthy system activation of the reviewed
`supervisor-hourly-report` playbook. It is neither a required policy nor a default
activation. The reviewed source has `enabled: false` and is no longer copied into
`vault/system/playbooks/` at startup. The reviewed bundle is available in
`src/prompts/reviewed_playbooks/supervisor-hourly-report/` in the installed
checkout. If absent from the vault, copy that directory to
`vault/reviewed-playbooks/supervisor-hourly-report/`, then explicitly import and
activate it through the playbook operator surfaces when enabling it. Restart
does not install the bundle or change an existing activation.
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

A request names the `supervisor-global` address, never a session. Both commands
accept the elevated token of whichever `n-supervisor--global` launch is live and
still holds its row's instance token, so a daemon restart (which adopts the
launch) or a relaunch needs no rebinding. Every other caller, a per-project
supervisor included, is refused as `out_of_scope`.

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
messages 2,000, and hourly submissions use the digest budget (`MAX_CHARS` in
`src/digest/render.py`, currently 600 characters including footer and marker).

`claim_report_deliveries` leases the globally oldest due rows. Typed adapters use
the same message delivery engine and record receipts through guarded finish
operations. A crashed lease is reconciled before any network write. Missing
history permission or an unreconciled ambiguous outcome ends in visible `unknown`,
which the automatic dispatcher never resends. Permission faults back off with a
bounded attempt budget; they neither spin nor block escalations. Inspect stored
owner rows using `get_outbound_delivery` / `list_outbound_deliveries`; transport
receipts are separate from report content and evidence coverage.

The new schema is Alembic revision `a00000000032`. Its create is idempotent and
rollback preserves the ledger. Operator upgrades use the normal daemon migration
path; workers never migrate the operator database.

## Optional morning reports

Morning reporting is separate from both hourly authoring and the P3
`discord.digest.supervisor_authored` switch. It defaults to disabled. These are
the shipped settings; the example leaves sending disabled:

```yaml
reports:
  timezone: UTC
  morning:
    enabled: false
    time: "07:00"
    late_cutoff_minutes: 120
    max_lookback_hours: 72
    author_deadline_minutes: 15
    project_ids: []
    destination: ""
    full_fleet_visibility: false
```

Use an IANA timezone for local scheduling. An empty destination uses
`discord.channel_id`; an explicit destination is `discord:<channel id>`.
`project_ids` restricts collection before evidence is read. Supervisor authoring
requires `full_fleet_visibility: true`, no project filter and the live global
supervisor. Restricted destinations use scoped deterministic fallback. Do not
represent a restricted or failed collection as complete fleet coverage.

The shipped morning-report playbook consumes `report.morning_due` and drives
`morning_report_tick`; inspect its installed activation alongside configuration.
The emitter fires at the planned local time once per local day in a running
process, with a stable event identity so restart replay cannot reserve a second
report. The tick reserves the day using DST-safe boundaries, collects database
and frozen-SHA git evidence read-only, and freezes a brief plus deterministic
fallback before requesting an author turn. The 15-minute author deadline and
120-minute late cutoff bound publishing. Disabling the feature lets the tick
cancel pending work; the due event alone is not permission to send.

Preview from an operator shell without reserving a report, waking an author,
writing state or sending anything:

```bash
aq report morning --dry-run --project-id PROJECT --max-lookback-hours 72 --json
aq report morning --dry-run --since 1792623600 --until 1792710000 --json
aq report list --limit 50 --offset 0 --json
aq report show REPORT_ID --json
```

`--since` and `--until` are UTC epoch seconds. Preview defaults to the preceding
24 hours ending at the last zoned 07:00; explicit bounds allow reproducible
inspection. Check source coverage, truncation and read failures as well as the
summary. Missing evidence is a gap, not proof that nothing happened.

For an already reserved author request, the live global supervisor reads the
bounded pages and submits the brief hash and version it actually read:

```bash
aq report brief REQUEST_ID --offset 0 --limit 20 --json
aq report submit REQUEST_ID --file /tmp/morning-report.json \
  --brief-hash BRIEF_HASH --expected-version VERSION --evidence-ref EVIDENCE_REF
```

Morning files contain version-1 JSON, hourly files UTF-8 prose; both are bounded
to 32 KiB at the CLI. Morning validation requires evidence-grounded project
content and URL-free author prose; the server supplies coverage and report links.
`aq report request REQUEST_ID` is service-only and cannot be used by an author to
select a destination or create an arbitrary request.

The full report and evidence coverage remain in the database and dashboard. Its
Discord summary is at most 1,500 characters including the stable marker and one
server-generated report link. Confirmed receipts finish the shared outbox row;
ambiguous sends reconcile against history or remain `unknown` without automatic
reposting. Read the stored report separately from delivery status before claiming
that the report reached its audience.
