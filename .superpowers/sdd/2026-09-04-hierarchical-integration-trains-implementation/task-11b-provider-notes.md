# Task11b provider inspection notes

Controller read-only documentation check; no repository API calls or credentials.
Keep current pinned API version unless a tested compatibility change is required.

GitHub's branch-rules endpoint returns effective active rules, including inherited
organization rules. Fetch those plus complete ruleset details; do not infer effective
policy from repository-local rules alone. List/details support includes_parents.
Details can omit bypass_actors when the caller lacks ruleset write access; missing
actors are unverifiable, not an empty allowlist. Numeric Integration actors have
bypass modes including always and pull_request; the latter does not prove direct-push
authority. Unknown/new modes require explicit supported semantics, not permissive
fallback. Canonicalize security-relevant effective facts separately from addressing
IDs so equivalent scratch/production policy can match while receipts bind both
repositories independently. These are implementation inferences, not provider claims.

Source: [GitHub repository rules REST documentation](https://docs.github.com/en/rest/repos/rules?apiVersion=2022-11-28).

Classic protection separately exposes pull-request bypass allowances and push
restrictions; read and combine both. Do not mistake an App slug for numeric identity.
Source: [GitHub branch-protection REST documentation](https://docs.github.com/en/rest/branches/branch-protection?apiVersion=2022-11-28).

Preserve the approved fail-closed blocker contract; never request extra production
permissions or change rules automatically merely to make inspection pass.
