# Explain strict routing preference refusals

Task: clear-falcon-93. Scope: deterministic planner diagnostics.

A strict task preference filters the candidates admitted by the executed
routing policy. It does not override class selection, kind or origin rules,
lanes, reservations, project provider constraints, or workspace requirements.
Pool capacity alone therefore does not establish eligibility.

When no candidate serves a strict preference, retain the `no_candidates`
outcome and `prefer_target_unavailable` reason. Its existing `detail` field
must identify the effective kind/origin rule, selected class and lane, and
explain why matching installed profiles were excluded. Include class raises
or escalation notes when applicable, and name reservations and project
provider constraints with their configured values. Unknown targets must be
distinguished from installed workers excluded by policy.

Diagnostics must use the same admission checks as candidate construction;
they must not alter selection or relax a strict preference. Existing explain
surfaces already display `detail` and should expose the evidence unchanged.

Acceptance: cover profile and harness preferences with available capacity;
class mismatch and reserved deep Claude cells; kind/origin lanes; project
provider and workspace constraints; and missing or disabled workers. An
eligible standard Claude worker still routes successfully. A refusal never
falls back to Codex.
