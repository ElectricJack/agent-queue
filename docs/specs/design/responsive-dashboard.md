# One responsive dashboard

Accepted task: grand-ember-55, 2026-10-25. This replaces the separate Focus
surface described in older mobile and Discord design records.

Every screen uses the main dashboard shell. Below 768px its rail is a menu
drawer and its secondary surfaces are sheets. Pages must fit a 390px viewport,
wrap long identifiers and prose, scroll vertically, and expose usable touch
controls. Wide code, tables and terminals may scroll inside their own region.

## Feature inventory before removing Focus

| Focus behavior | Main dashboard destination |
| --- | --- |
| Filtered task cards and task actions | Project Tasks & Knowledge, task detail |
| Live agent and pool sessions | Agents and project Sessions |
| Provider quotas and age | Header usage bars and Metrics |
| Report and task evidence | Report page and task detail |
| Needs-you, waiting and history, including pagination | `/inbox`, shared AskInbox also used by Reviews and Activity |
| Quick ask replies, decisions and receipts | `/asks/:id`, shared AskDetail and discussion/decision controls |
| Review/escalation audience-checked resolution | `/reviews/:id`, `/escalations/:id` resolving to the canonical ask |
| Batch delivery state, PR and member tasks | `/batches/:id` in the main shell |
| Conversation picker/history and quick replies | `/conversations?conversation=:id` using existing GlobalChat |
| Process-pinned terminal and restart notice | `/sessions/:id?started=:epoch`; explicit selection before following a restart |
| Ended session explanation and current-session link | Session detail, alongside its historical transcript |
| Cold-link navigation, safe areas and phone controls | Main shell Back/menu, dynamic viewport and safe-area utilities |

Posted links use these canonical routes at every width. All former `/focus`
routes redirect with replacement history, preserving query strings, encoded
identifiers and fragments. `/focus` and unknown Focus paths return to Command
Center; `/focus/inbox` becomes `/inbox`, and conversation detail becomes the
selected conversation query. There is no separate mobile mode or link policy.

Validation uses dashboard typecheck, build and Vitest, Python posted-link and
doctor tests, and the browser layout harness at 390px (including menu behavior,
task, ask, review, session and conversation screens).

The CI repair updates the reviewed escalation handler audits for the canonical
`/asks` URLs. Integration ownership follows the surviving batch scan, member
schema, projection types and reference helper into `BatchDetail.tsx`. Their
charged footprint falls from 59 to 33 lines, so the exact transition ceiling
falls by 26 to 157,899 owned lines; engine lines remain 82,649 and tables remain
45. Historical grants and baseline measurements stay intact, and the size tests
retain their equality assertions and zero remaining headroom.

Merging dev's separately approved historical root PR diagnostics adds its
recorded 86 owned/engine lines to that reduced ceiling: 157,985 owned lines and
82,735 engine lines, still with 45 tables and zero remaining headroom. Both
composition records retain their original measurements and decision evidence.

The next dev merge retains the approved PR retirement snapshot grant (+41
owned/engine lines) and terminal hibernation hold grant (+25 shared owned
lines). Composed with this dashboard's unchanged 26-line reduction, the exact
ceiling is 158,051 owned lines and 82,776 engine lines, with 45 tables and zero
remaining headroom. All historical composition records remain unchanged.

The subsequent dev merge retains the approved tick snapshot sharing grant
(+116 owned lines, including +101 engine lines). With the same dashboard
reduction, the exact ceiling is 158,167 owned lines and 82,877 engine lines,
with 45 tables and zero remaining headroom. The grant's original measurements
and decision evidence remain unchanged.

The next dev refresh composes its approved retained-fixer authority (+124
owned / +78 engine), changed-ref fetches (+87 / +86), aborted-batch branch
retirement (+63 / +55), and checkout reuse (+67 / +67) grants with the same
26-line dashboard reduction. The exact ceiling is 158,508 owned lines and
83,163 engine lines, with 45 tables and zero remaining headroom. Every prior
grant and merge measurement remains intact.

The following dev refresh retains its approved wait-listener coalescing grant
(+17 owned lines), reconciliation no-op grant (+20 owned/engine lines), and
originless backfill provenance grant (+38 owned/engine lines). Composing these
with the unchanged dashboard reduction gives 158,583 owned lines and 83,221
engine lines, with 45 tables and zero remaining headroom. The three grants and
all earlier historical measurements remain verbatim.

The next dev merge preserves the approved deleted-epic child recovery (+103
owned/engine), missing-target abort (+59 owned/engine), and Git truth phase
profiling (+77 owned / +35 engine) grants. With the unchanged 26-line dashboard
reduction, the exact ceiling is 158,822 owned lines and 83,418 engine lines,
with 45 tables and zero remaining headroom. All historical grant fields remain
unchanged.
