# Direct completion of fast and smooth integration

On 2026-10-22 the operator requested that the agent-queue project stop while this
work was completed with subagents and delivered directly to `dev`. Scheduling,
the project supervisor and automatic integration were disabled. Other projects
were left running. Implementation used an independent Git worktree.

## Sources and authority

The starting `dev` was `e0b9fcae5`. The epic tip was
`5c77d36e52630475059761ff173ddb4824d39ef3`, containing every completion commit
recorded for `stark-willow-95.1` through `.25`. Its 94 commits absent from `dev`
were merged, preserving current development fixes. The published stuck-check
fix (`597b44ff5`, PR 1776) and unmanaged child-conflict fix (`9d3db06cb`, PR 1755)
were integrated into the same candidate. Migration heads were reconciled to a
single linear chain, ending at `a00000000120`.

Jack's revision-3 and revision-4 decision notes override conflicting revision-2
assumptions. The [authority correction](README.md#authority-correction-2026-10-22)
records the supersession, including the accepted in-place single-family fixer.
Historical reports remain evidence of their original runs, not current results.

## Completed behavior

- The small `fast-smooth-train` V2 policy controls dispatch, repair, finite waits
  and required checks. Its project activation is the switch. Custom policy names
  have an explicit binding; unreadable policy cannot authorize publication.
- Policy changes and disabling serialize with publication. A check-policy change
  applies to the existing candidate without refiling its frozen sources.
- Disabling the policy removes unused integration reservations and permits
  ordinary worker source publication. Live worker ownership is preserved.
- Fresh Git proof retires obsolete, idle repair tasks after their batch was
  delivered elsewhere, releasing dependents without fabricating a passing close.
  Explicit holds, active sessions, locked workspaces and unmerged repair work stay.
- Frozen-source publication tolerates missing or stale origin/checkpoint
  projections when current source identity and Git ancestry are proven. Actual
  review rejections and explicit human intent remain binding.
- A completed Actions job whose required check remains in progress gets a bounded,
  authenticated rerequest. Job steps never substitute for trusted green checks.

The App needs Actions write permission for automatic rerequests. This source
change does not change the operator's App permissions or resume the project.
Existing projects install/activate the shipped project policy deliberately.

## Verification and delivery

Focused regressions use real Git and disposable PostgreSQL. The initial combined
connected-flow run passed six canaries; the isolated daemon smoke passed all
20 scenarios. Dashboard typechecking and 43 focused tests passed. Further combined
area, migration, generated-artifact and hosted checks are recorded with the final
candidate's delivery evidence.

Delivery uses a push-triggered candidate CI run and the existing authenticated
attestation verifier, followed by an expected-old fast-forward of that exact SHA
to `dev`. It does not require an AQ batch, task claim or repair queue. GitHub
protections stay enabled. The project remains stopped for operator resumption;
this report does not attest live policy activation, database deployment, a
30-day watch period or another project's storage relocation.
