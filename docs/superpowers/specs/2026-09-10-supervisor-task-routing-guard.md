# Supervisor task-routing guard

> **Superseded in part by mandatory task routing (2026-09-28,
> `projects/agent-queue/specs/2026-09-28-mandatory-task-routing.md`, a vault spec):** the
> project-default fallback. A supervisor filing no longer resolves a project worker default
> or a system fallback: there is no project default profile, and every filing (the
> supervisor's included) carries hints only and is routed by the project's router; no filer,
> worker or otherwise, inherits its own profile. The rule that `supervisor` is never a task
> route still stands.

## Decision

`supervisor` is a named control-plane profile and is not an executable task
route. Creation, graph validation, edits, manual routing, and project default
updates reject it before task insertion or routing writes.

When a supervisor creates work without a profile, AQ resolves the configured
project worker default. If none is configured, it uses the existing
deterministic system worker fallback only when one is eligible; otherwise it
returns an actionable configuration error. Explicit requested worker routes
remain subject to the existing capability-subset and class/provider checks.
Ordinary worker-created subtasks still inherit their caller profile.

## Legacy rows

Existing tasks carrying `profile_id = supervisor` are retained for audit and
manual rerouting. Both the push scheduler and pool claim frontier exclude
them, so they cannot claim capacity or hide independent worker-routed work.
`aq task explain` reports `supervisor_profile` with the remediation.

## Capability policy

The shipped supervisor profile explicitly carries the AQ and plugin capability
entries needed to delegate to the standard and deep shipped worker profiles.
The normal namespace-by-namespace subset check remains unchanged: an AQ grant
does not imply a plugin grant, and no wildcard broadening is introduced.
