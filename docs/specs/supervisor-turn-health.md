# Supervisor turn health and recovery

Named supervisor health depends on completed turns and unanswered inbound work,
independently of process or pane activity. `supervisor.turn_timeout_seconds`
defaults to 900. Exact-conversation Codex and Claude observations read at most
256 KiB and exclude records before the current launch. Missing or partial
transcripts are unknown evidence. Structured provider capacity, rate-limit and
authentication errors are failures even when the terminal process remains live.

The named-session reconciler stores its last successful completion, health
observation, attempts and next retry time in `sessions.supervisor_health`.
It nudges failed or overdue work twice, with exponential backoff starting at
`supervisor.retry_backoff_seconds` (60), capped at 1800 seconds. A subsequent
failure restarts fresh through the existing serialized, instance-fenced lens:
stop intent, confirmed stop, token revocation, new row and new token. Stop
uncertainty prevents replacement. Successful turns clear the failure budget;
daemon restarts and conversation replacement do not.

Recovery can select another installed harness that resolves a model in the
same intelligence class, respecting `provider_failover` mode, per-class hold
policy, provider order and availability keyed by harness login. Explicit
individual model pins remain authoritative. Recovery never edits an installed profile or class. If
there is no allowed alternative, it retries fresh with the configured route.
A durable handoff lists the old conversation and outstanding request IDs;
the global supervisor receives a notice (global failures notify the dashboard).

Unanswered worker requests, including inbox-consumed and archived requests,
escalate after `supervisor.question_timeout_seconds` (900). The original
request's provenance records escalation atomically with its notice, once per
request across restarts. The notice preserves the thread and original message
pointer; it is not a reply and does not satisfy a worker wait. New supervisors
inherit requests addressed to prior instance UUIDs. Successful turns discharge
ordinary status reports; native questions and verified pending message waits
remain obligations until answered. Native question sources that are no longer
open are excluded.

Idle completed turns rotate fresh at `supervisor.max_session_age_seconds`
(86400) or `supervisor.context_ceiling_tokens` (160000 measured request input).
Active or unknown turns do not authorize rotation. Age bounds long-lived
conversations even when automatic compaction keeps their context small.

Session list/show expose the persisted health sample. Stall-sweep reports
`SUPERVISOR-UNHEALTHY` from fresh samples, even when process activity is current,
and `SUPERVISOR-UNKNOWN` for missing or stale samples. It never repairs sessions.

`aq supervisor restart --name supervisor-<project>` accepts the local operator,
the live global supervisor, or that project's live named supervisor. Other
projects, workers and stale supervisors are refused. Fresh remains the default;
explicit resume retains its existing compatibility and session-ID fences.
