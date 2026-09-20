# Task-graph layout that reorganises itself

**Status:** design, no code. Companion to `docs/superpowers/specs/2026-09-01-task-graph-spatial-layout-design.md`
(the engine spec; section numbers below in the form "§4.4" refer to it) and to
`docs/superpowers/specs/2026-09-19-graph-visibility-markers-subtasks-implementation.md`
(phases, standing parents, `active_expansion`, progress bars).

**The complaint.** "The nodes created to track tickets become a mess — they just keep
growing downward and don't automatically reorganize. Originally we did that so it wouldn't
get slow with thousands of nodes, but the real issue is that you don't want to show every
node." On the operator's own data: one project's canvas showed a finished epic on the top
row and, beneath it, a single very wide row of five-plus completed sibling cards running
off the right edge; another stacked finished epics vertically with the only unfinished
tasks at the bottom.

Finished subtrees that are context for nothing are already dropped from the `active`
variant (§4.8, shipped 2026-09-20). This document is about what is left: **how the
remaining nodes are arranged**.

---

## 1. How placement works today, from the code

### 1.1 The three coordinates a node has

`LayoutRow` (`src/task_graph/layout/model.py:19-42`) carries, per `(project_id, variant,
task_id)`:

| Field | Meaning | Written by |
|---|---|---|
| `rank` | the node's **layer** inside its container: `rank(dependent) >= rank(blocker) + 1`, longest-path over sibling `blocks`/`waits-for`/`conditional-blocks` edges | `layering.py:72-95` (`minimal_ranks_acyclic`) |
| `order_key` | a fractional string key ordering siblings **within one rank**, left to right | `order_key.py:16` (`between`) |
| `depth` | depth of the node below the project root; a level-of-detail input only (`view.py:83-91`), never a placement input | `engine.py:153` |
| `path` | `/a/b/<id>/`, the subtree prefix used for translations and deletes | `engine.py:152` |

`ordinal` is `(rank, order_key)` (`model.py:40-42`). Everything else — `rel_x/rel_y`,
`abs_x/abs_y`, `w/h` — is *derived* from the ordinals plus the children's sizes by
`flow_container`, on every pass. **Ordinals are the persistent decision; coordinates are
recomputed each time.**

### 1.2 Rank assignment, and why the root is one flat rank

`minimal_ranks_acyclic` (`layering.py:72-95`) gives every node with no sibling blocker
`rank = 0`. Root-level tasks rarely block each other — phases do (`phase_create` adds a
`blocks` edge onto every earlier unfinished sibling phase), but ordinary epics do not. So
in a typical project **every root epic and every loose root task sits in rank 0**, i.e. in
one single layer that the flow step then wraps into lines.

### 1.3 Ordering inside a rank: creation order, always

`_ordered_from` (`engine.py:77-86`) buckets ids by rank and sorts each bucket by
`order_key` (`engine.py:85`). Keys are handed out in exactly two places:

- **A new node with no blockers appends to the end of its rank.** `_place_new`
  (`engine.py:242-272`) builds a single candidate gap `(last_key, None)` for the
  no-blocker case (`engine.py:246-250`) and breaks out of the rank loop without trying to
  sink it (`engine.py:271-272`). Above `MAX_OPTIMIZED_SIBLINGS = 500` the same append
  happens without any cost evaluation (`engine.py:391-404`). New nodes are processed in
  `created_at` order (`engine.py:386-389`).
- **Tidy re-keys every rank from scratch, seeded by `created_at`**
  (`engine.py:353-360`): `sorted(..., key=lambda c: (scope.children[c].created_at, c))`.

A node with blockers is placed at the barycentre of those blockers (`_barycenter_gap`,
`engine.py:194-227`), which is a *horizontal* choice within the dependent's rank.

**No code path anywhere consults `status`, `is_blocked`, aggregates, or
`task_metadata.phase.order` when choosing a rank or an order key.** The only status input
to the engine is `_visible` (`driver.py:94-171`), which decides *presence*, and `_sizes`
(`engine.py:89-96`), which makes a stub card-sized.

### 1.4 Flow: what makes a row wide, and what makes it wrap

`flow_container` (`flow.py:30-101`) walks each rank left to right and wraps:

```python
target = TARGET_ROW_WIDTH_ROOT if is_root else TARGET_ROW_WIDTH   # flow.py:44
...
if started and x + w > target:                                    # flow.py:78
    y += line_h + LINE_GAP; x = 0.0; started = False
```

with `TARGET_ROW_WIDTH = 4.5`, `TARGET_ROW_WIDTH_ROOT = 7.0`, `CARD_W = CARD_H = 1.0`,
`SIBLING_GAP = 0.15`, `LINE_GAP = 0.22`, `PADDING = 0.1`, `HEADER_H = 0.35`
(`constants.py:5-18`). Three consequences, all load-bearing:

1. **The target is a constant.** It does not depend on how many children the scope has,
   how wide its other children are, or the viewport. A container of 8 cards and a
   container of 800 cards wrap at the same 4.5 world units.
2. **The first item on a line is placed unconditionally** (the `started` guard): a child
   wider than the target overhangs it rather than being clipped or shrunk.
3. **A container's allocated size is its content rounded up to a growth band** — `band_up`
   over `GROWTH_BANDS = (1.5, 3.0, 6.0, 12.0, 24.0, 48.0)` then doubling
   (`constants.py:17,47-55`; `flow.py:94-100`). So an epic is 6 or 12 or 24 units wide
   whatever it actually contains.

A run of consecutive singleton ranks linked blocker→dependent is exempted and folded into
a serpentine (`_serial_chains` `engine.py:99-133`, `_flow_serpentine_chain`
`flow.py:126-175`) — the one place today's flow already re-shapes rather than stacks.

### 1.5 Reproducing the operator's two screenshots from the constants

Both fall straight out of §1.2 + §1.4. Measured by calling `flow_container` directly:

```
# one root rank: one banded 12x6 epic followed by 8 loose cards
epic (0.00, 0.00)                                     content = 12.20 x 8.99
c0..c5 at y=6.22, x = 0, 1.15, 2.30, 3.45, 4.60, 5.75      lines_per_rank = [3]
c6..c7 at y=7.44
```

The epic is 12.2 wide; its line-mates cannot join it because `0 + 12 > 7`, so they wrap
underneath and pack **six to a line** at the root target. That is precisely "a finished
epic on the top row and, beneath it, a single very wide row of five-plus completed sibling
cards": six cards is the widest run the root target allows, and the scope's content is
12.2 units wide while that card line is 6.75, so the row reads as running off the right
edge of everything else.

```
# six finished epics, each banded to 6.0 x 4.0, at the root
e0 (0.0, 0.00)  e1 (0.0, 4.22)  e2 (0.0, 8.44)  e3 (0.0, 12.66) ...
content = 6.20 x 25.65   lines_per_rank = [6]
```

Every epic is 6 units wide and the root target is 7, so **two epics never fit on a line**.
Six epics become a 25.65-unit-tall single-file column in `created_at` order. That is the
second screenshot.

And inside an epic:

| children | today: content w × h | lines |
|---|---|---|
| 12 cards | 4.65 × 3.99 | 3 |
| 40 cards | 4.65 × 12.53 | 10 |
| 60 cards | 4.65 × 18.63 | 15 |
| 120 cards | 4.65 × 36.93 | 30 |

Width is pinned at 4.65 forever; height is linear in the child count. **That is the
"grows downward" the operator is describing** — it is not the root stacking alone, it is
every scope in the project having a fixed width and an unbounded height.

### 1.6 What Tidy does that incremental does not

`layout_container(mode="tidy")` (`engine.py:349-363`):

- **discards every existing ordinal** and re-ranks from `minimal` — all accumulated slack
  (a node sunk one rank by the optimiser) disappears;
- **re-keys every rank** by `(created_at, id)` — short, dense keys again;
- runs `_tidy_sweep` (`engine.py:278-331`): two barycentre down-sweeps and two up-sweeps,
  then greedy adjacent swaps under `TIDY_EVALS = 5000` / `TIDY_SECONDS = 20.0`, skipped
  entirely above `MAX_OPTIMIZED_SIBLINGS = 500` (`engine.py:361-362`).

`build_full_write_set` (`driver.py:204-293`) drives it bottom-up with `existing={}` for
every container, then recomputes absolute coordinates top-down and **deletes every row not
in the new write set** (`driver.py:328-330`). Job budget `TIDY_JOB_SECONDS = 60`; past the
deadline the remaining containers silently fall back to barycentre-only placement
(`driver.py:252-261`).

Tidy runs only when: the operator asks (`graph_tidy`,
`src/commands/graph_commands.py:25`; toolbar in
`dashboard/src/pages/command-center/TaskToolbar.tsx`), a variant has no meta row
(`driver.py:412-415`), or a project's incremental batch has failed three times in a row
(`src/orchestrator/layout_step.py:131-135`).

### 1.7 When space is reclaimed, and when it is not

| Event | Reclaimed? | Why |
|---|---|---|
| Task **deleted / archived** | Yes | `_seed_queue` dirties the holding container (`driver.py:619-629`) and the flow closes the gap without changing any key (`test_removed_node_closes_gap_without_changing_keys`) |
| Task **reparented** | Yes, both scopes | `driver.py:630-642` |
| Leaf **finishes**, `all` variant | No, by design | `_aggregates_only` returns `True` (`driver.py:589-591`): the node is still present, only ancestor counters change |
| Leaf **finishes**, `active` variant | **No — a permanent hole** | `_aggregates_only` (`driver.py:568-602`) returns `True`, so `run()` deletes the row (`driver.py:842-847`) **without re-laying the parent**. The comment is explicit: re-flowing a 5,000-child root costs 1.4 s, so "the next ordinary pass over that container — a created, moved or reopened sibling, or a tidy job — closes the gap" |
| Container **finishes** and becomes a stub / is dropped | Yes | excluded from `_aggregates_only` (`driver.py:594-602`), so the parent is re-laid |
| Viewer **collapses** a container | Height only, never width | `compaction.py:140-177` |
| `reconcile` sweep (900 s) | Presence only | "It deliberately does not chase *geometry* drift — §4.6's finished-leaf fold leaves an empty slot behind on purpose" (`driver.py:350-351`) |

Compaction deserves its own line. `_pack` (`compaction.py:140-177`) groups children into
lines by their **persisted `rel_y`** (`compaction.py:152-154`) and slides each child left
by what its earlier line-mates gave up. It never re-wraps — §3.5 says so outright ("What
compaction deliberately does **not** do is re-wrap … a rank of wide epics collapses into a
narrow column rather than repacking into a grid"), and
`test_lines_are_inherited_not_re_wrapped` pins it. So collapsing the six-epic column above
turns it into a column of six 1×1 cards: 25.65 units of height come back, and the width
stays 1.

### 1.8 The stability guarantees, and the tests that pin them

§3.4, as implemented:

| Guarantee | Code | Test |
|---|---|---|
| **G1** Incremental never changes an existing node's rank or order key; only new nodes are placed | `engine.py:344-407` — `ordinals` is seeded from `scope.existing` and only `_place_new` writes | `test_engine_incremental.py:52` (insert into 1,000 → `changed_ordinals == {"new"}`) |
| **G2** The single exception: a newly infeasible rank pushes that node and its dependents down, taking a **fresh** end-of-rank key | `engine.py:370-383` | `test_engine_incremental.py:88`, `:143` |
| **G3** Nothing changes *lines*: "coordinates may translate when neighbours grow, shrink, appear, or leave, but nothing reorders and nothing changes rows" (§3.4) | consequence of a constant `target` in `flow.py:44` | `test_flow.py:23`, `test_engine_incremental.py:102` |
| **G4** Growth bands bound propagation to O(log n) band crossings per container | `constants.py:47-55`, `flow.py:100` | `tests/perf/test_layout_statements.py::test_root_band_crossing_publish_under_1s` |
| **G5** Determinism: same snapshot + seed ⇒ identical ordinals and coordinates; wall-clock never feeds a layout decision | `engine.py:52-56` | `test_engine_incremental.py:134`, `:163`, `test_engine_tidy.py:43` |
| **G6** Compaction is the identity when nothing is collapsed, and the toggled container is a fixed point | `compaction.py:160-177` | `test_compaction.py:87`, `:114`, `:259` |
| **G7** Tidy deliberately relaxes G1/G3 — it is user-triggered because it breaks spatial memory (§3.4, §4.7) | `engine.py:349-363` | `test_engine_tidy.py:35` (pinned crossing bound) |

---

## 2. Diagnosis

**Symptom 1 — "one very wide row of completed siblings".** `flow.py:44` picks a constant
target; six 1×1 cards is exactly what fits in `TARGET_ROW_WIDTH_ROOT = 7.0`. The row looks
anomalous because the line *above* it holds a banded 12-unit epic that the wrap rule
cannot share (`0 + 12 > 7`, and the first item on a line is placed unconditionally), so
the scope's content is 12.2 wide while every card line is capped at 6.75. The row is not
too wide; **the scope is ragged**, because one child is allowed to exceed a target the
others are held to.

**Symptom 2 — "active work is not near the top".** Two mechanisms compound. (i) Every
edge-free root child is `rank = 0` (`layering.py:86-93`), so the root is one layer that
wraps by width; a finished epic banded to 6 or 12 units consumes a whole root line, so N
epics become N lines. (ii) Order within that layer is `order_key` (`engine.py:85`), which
is pure creation order — appended at the end for new nodes (`engine.py:246-250`,
`:391-404`) and re-seeded from `created_at` even by Tidy (`engine.py:353-360`). The
newest work is therefore always **last**, and since each epic owns a line, "last" means
"furthest down". Nothing in the engine reads status.

**Symptom 3 — "space is not reclaimed / it does not reorganise".** Three separate
non-reclaims, in ascending order of importance: (i) in `active`, a finished leaf's row is
deleted but its container is never re-flowed (`driver.py:568-602`) and `reconcile`
deliberately ignores geometry drift (`driver.py:350-351`), so the hole is permanent until
an unrelated sibling event or a Tidy; (ii) compaction reclaims height but never width
(§3.5, `compaction.py:152-154`); (iii) the real one — **a scope's width is a constant, so
every scope's aspect ratio degrades linearly with its child count** (4.65 × 18.63 at 60
children, 4.65 × 36.93 at 120). Even a perfectly reclaimed canvas grows downward, because
the flow has no notion of "this container should be roughly as wide as it is tall".

---

## 3. Proposed design

Four changes, each independently shippable. (a) and (c) address symptoms 1 and 3; (b)
addresses symptom 2; (d) is the perf and stability envelope.

### 3.1 (a) An aspect-balanced, banded row target

**Recommendation — A1.** Replace the constant in `flow.py:44` with a target computed once
per container pass from the children's sizes, then snapped to a band:

```
ROW_TARGET_BANDS = (4.5, 7.0, 10.0, 14.0, 20.0, 28.0)   # then doubling, like GROWTH_BANDS
ROW_ASPECT = 1.3                                        # landscape viewports

ideal(sizes)  = sqrt(Σ_i (w_i + SIBLING_GAP) · (h_i + LINE_GAP)) · ROW_ASPECT
floor         = TARGET_ROW_WIDTH_ROOT if is_root else TARGET_ROW_WIDTH
target(scope) = band_row(max(floor, ideal(sizes), max_i w_i))
```

- `sqrt(area) · aspect` targets a **square-ish scope** instead of a fixed-width strip.
- `max_i w_i` in the max is symptom 1's fix: the target is never narrower than the widest
  child, so a 12-unit epic no longer leaves its line-mates packed into 7.
- `band_row` is the stability device. The target changes only when the scope crosses a
  band — **O(log n) times over a container's life**, exactly like `band_up` already bounds
  allocation changes (G4). Between crossings the target is a constant and G3 holds
  verbatim.
- The floor keeps small scopes byte-identical to today: `ideal ≤ 4.5` for any scope up to
  ~8 unit cards, so the overwhelming majority of containers are unchanged.

Measured (calling `flow_container` with the banded target):

| children | today w × h (lines) | proposed w × h (lines) |
|---|---|---|
| 12 cards | 4.65 × 3.99 (3) | 6.95 × 2.77 (2) |
| 40 cards | 4.65 × 12.53 (10) | 9.25 × 6.43 (5) |
| 60 cards | 4.65 × 18.63 (15) | 13.85 × 6.43 (5) |
| 120 cards | 4.65 × 36.93 (30) | 19.60 × 10.09 (8) |

The 120-card epic stops being four screens tall.

**Alternative A2 — derive the target from `GROWTH_BANDS` instead** (`target = b - 2·PADDING`
for the smallest growth band `b` that fits the ideal). This wastes *zero* allocation width
— content lands just under the band — but the ladder doubles, so 60 cards snaps to a
23.05 × 3.99 three-line strip: too flat. Rejected for shape.

**Alternative A3 — a viewport-derived target.** Rejected: the persisted layout is shared
and must not depend on who is looking (§3.5).

**Known cost, stated.** With A1 the content width tracks the row band while the allocation
still snaps to `GROWTH_BANDS`, so a 13.85-wide scope is allocated 24.0. That is the
pre-existing §12 limitation ("a box can look roughly twice as large as its contents"),
made more visible. The fix is a finer growth ladder (×1.4 above 12 rather than ×2), which
costs ~2.4× more band crossings and therefore more translations — offered as an **open
question**, not bundled here.

**Complexity.** `ideal` is one pass over the children, O(k). It must be computed **once
per `layout_container` call** and passed into `flow_container` as an argument, not
recomputed inside `_evaluate` — `_tidy_sweep` calls `_evaluate` thousands of times
(`engine.py:309-331`). It depends only on `sizes`, never on the candidate ordering, so the
cost landscape stays continuous and G5 is untouched. Net asymptotic change: none.

### 3.2 (b) Ordering that surfaces active work

Two orthogonal axes: **phases** (structural, vertical) and **activity** (a tie-break
within a rank).

**Phases: keep them top-to-bottom, and make it explicit.** `phase_create` already adds a
`blocks` edge from phase *N+1* onto every earlier unfinished sibling phase, and those edges
survive the phase completing. So `minimal_ranks_acyclic` already gives phase *N* rank *N*
and stacks them vertically with no new code. Rank **is** the engine's time axis, and a
phase is a temporal gate; laying phases left-to-right would mean either fighting
`minimal_ranks` or widening every root row to hold six phases side by side. **Decision:
phases stay vertical.** The one addition is a defensive tie-break: siblings inside a rank
sort by `phase.order` before anything else, so a phase whose gate edge was removed (an
abandoned middle phase deleted, per the `phase_create` semantics) still sorts with its
peers rather than by creation time.

**Activity: a three-class tie-break inside a rank.**

```
activity_class(task, blocked) -> int
  0  status in RUNNING_STATUSES                      (constants.py:37)
  1  status not in FINISHED_STATUSES                 (constants.py:36)   # ready or blocked
  2  otherwise                                                           # finished
```

For a **container**, use the aggregate instead of its own status —
`agg_running > 0 → 0`, `agg_active > 0 → 1`, else `2` — so an epic with running work
floats even while the epic row itself is `DEFINED`. Those aggregates are already computed
per pass (`driver.py:174-201`) and already persisted on the row.

The sort key becomes `(phase_order, activity_class, existing order_key)` — a **stable**
sort, so same-class siblings keep the order they already had and nothing jumps within a
class.

**When it is applied — the crux.** Three options:

- **B1 continuous:** re-key on every pass. Cheap (`O(k log k)`), and it is what the
  operator literally asked for, but it destroys G1 outright: a task moving
  `READY → IN_PROGRESS` would slide sideways under a viewer several times a minute.
  Rejected.
- **B2 tidy-only:** re-key only in `mode="tidy"` (change the seed at `engine.py:353-360`).
  Keeps every guarantee, needs no new mode, but requires the operator to press Tidy, which
  is exactly the manual reorganisation they are complaining about. Insufficient alone.
- **B3 explicit events (recommended):** a new engine mode `reorder`, identical to
  `incremental` except that it re-keys each rank by the sort key above before flowing. The
  driver requests it for **one container** on exactly three triggers:
  1. a **Tidy job** (always — B2 is subsumed);
  2. a child's **own status crossing into `FINISHED_STATUSES`** when that child is a
     container or a phase — i.e. an epic settling. This is once per container per lifetime,
     and it is the moment a finished epic should sink past its live siblings. Implemented
     by adding a `reorder` queue entry in `_seed_queue` (`driver.py:605-654`) when
     `reason == "status.finished"` and `snapshot[tid].is_container`;
  3. a **container being dropped or stubbed** in `active` (`driver.py:131-138`), which
     already re-lays the parent — it just gets `reorder` instead of `incremental`.

  A **leaf** finishing never triggers a reorder. That is the frequent event, and it is the
  one that would make the canvas twitch.

**Guarantees: kept, relaxed, scoped.**

| | Effect |
|---|---|
| G1 (ordinals immutable incrementally) | **Scoped.** Still holds for `mode="incremental"`. Relaxed for the new `mode="reorder"`, which the driver requests only on the three triggers above — never on a leaf status change, never on a create. `ContainerResult.changed_ordinals` reports exactly which keys moved, so the existing assertions stay meaningful. |
| G2 (forced rank repair) | Unchanged — `reorder` re-keys within a rank, never across ranks. |
| G3 (nothing changes lines) | **Relaxed, bounded.** A row-target band crossing re-wraps one scope, at most O(log n) times over its life. Between crossings, identical to today. |
| G4 (growth bands) | Unchanged; the row-target band is a second ladder with the same O(log n) property. |
| G5 (determinism) | Unchanged. Both the target and the sort key are pure functions of the snapshot; ties break on `order_key` then id. |
| G6 (compaction identity) | Unchanged — compaction reads persisted rows and is untouched by all of this. |
| G7 (tidy breaks spatial memory) | Unchanged, and `reorder` is deliberately the weaker cousin: one scope, one re-key, no rank changes, no sweep. |

**Manual positions must be honoured, and the disagreement flagged.** §11 lists "manual
drag-and-drop positioning or pinned nodes" as out of scope, but the shipped canvas does it:
`LayoutCanvas.tsx:446` prefers `manualPositions[projectId][node.id]` over the server
position for any non-stub task node (`:72-79`), persisted in the `command_center_project_view`
dashboard-state document (`src/api/models/dashboard.py:119`,
`useGraphHierarchy.ts:247-252`). A pin is an **absolute** world position, so any reorg
strands it. The design **keeps honouring pins** — silently discarding an operator's
deliberate placement is worse than a stale one — and adds one affordance: after a Tidy or a
rules-version rebuild (§4), the canvas shows a single "N pinned cards may be out of place —
reset?" banner wired to the existing `clearGraphPositions` (`useGraphHierarchy.ts:251`).

### 3.3 (c) Reclaiming the `active` variant's holes

Keep the fast path in `_aggregates_only` (`driver.py:568-602`) — it exists for a measured
1.4 s reason — but stop the hole being permanent. When that path fires in `active`, write
a **deferred** dirty mark on the parent with reason `reflow`. The 5 s incremental pass
ignores `reflow` marks; the reconcile sweep (`layout_step.py:137-146`, every
`reconcile_interval_seconds = 900`) consumes them and re-lays those containers in
`reorder` mode.

Net effect: a hole lives seconds-to-minutes instead of forever, at the cost of one extra
`layout_dirty` row per finished leaf and one container re-flow per sweep per affected
container — both already bounded by `MAX_LAYOUT_PROJECTS_PER_CYCLE = 10`
(`layout_step.py:13`) and by `pop_layout_dirty`'s 1,000-mark cap.

Rejected alternative: teach `reconcile` to diff geometry. It would have to re-run the
whole engine per project per sweep to know what "correct" geometry is — that is a Tidy,
priced as a sweep.

### 3.4 (d) Cost

| Change | Complexity | Bounded by |
|---|---|---|
| Aspect-balanced target | O(k) once per container pass (hoisted out of `_evaluate`); no change to per-eval cost | `tests/perf/test_layout_statements.py::test_full_layout_under_budget` (60 s at 5,000 tasks) |
| Row-target band crossing | one scope re-flow + one translation per later sibling subtree — identical in kind to a growth-band crossing, O(log n) per container | `::test_root_band_crossing_publish_under_1s`, `::test_incremental_batch_of_ten_under_550ms` |
| `reorder` mode | one stable sort per rank, O(k log k), plus one `_evaluate`; equals a single tidy evaluation | `::test_incremental_batch_of_ten_under_550ms` — and the trigger is once per container lifetime, so the steady-state batch is unaffected |
| Deferred `reflow` marks | one extra row per finished leaf; one container re-flow per sweep | `layout_step.py:13` fan-out cap; `::test_full_layout_under_budget` |
| Read path | **unchanged** — no new query, no new column on the hot path | `tests/perf/test_layout_api_statements.py::test_tiles_round_trip_budget` (9 statements / 9 checkouts, deterministic), `::test_tiles_latency_with_big_collapsed_epic_visible`, `::test_tiles_focus_root_latency` |

The one real regression risk is that a wider scope covers more 8×8 cells
(`flow.py:178-184`), so `task_layout_cells` grows. A 24 × 12 epic is 6 cells versus a
6 × 24 epic's 3–6: same order, and `test_tiles_round_trip_budget` pins the statement count
regardless.

### 3.5 Before / after

```
BEFORE (active variant, the operator's project)          AFTER
=============================================            ================================================
+---------------------------------------------+          +------------------+ +---------------------+
| Epic: Playbook V2            [COMPLETED stub]|          | Phase 2  RUNNING | | Epic: Graph vis.    |
+---------------------------------------------+          | +------+ +------+ | | [##--] 2 running    |
[t1][t2][t3][t4][t5][t6]     <- 6 wide, ragged            | | run  | | run  | | +---------------------+
[t7][t8]                                                  | +------+ +------+ |
+-------------------------+                               | +------+ +------+ |   (rank 0: phase 2 and
| Epic: Integration       |  <- 12 x 6 banded             | | rdy  | | blk  | |    the live epic, side
| ... 40 finished cards   |                               | +------+ +------+ |    by side; target is
| 4.65 wide x 12.5 tall   |                               +------------------+    now >= widest child)
+-------------------------+
+-------------------------+                               +----------------------------------------+
| Epic: Swarm [COMPLETED] |                               | Phase 1  COMPLETED  [########] 60/60    |
+-------------------------+                               | [e1][e2][e3][e4]   <- stubs, sunk last  |
+-------------------------+                               +----------------------------------------+
| Epic: Graph visibility  |  <- the only live work,
| 2 RUNNING               |     four screens down          content 4.65 x 18.63  ->  13.85 x 6.43
+-------------------------+                                creation order        ->  running first
```

---

## 4. Migration of existing installs

**`reconcile` is not enough.** It compares presence, `container_id` and `kind`
(`driver.py:368-382`) and says so in its own docstring: "It deliberately does not chase
*geometry* drift". The 2026-09-20 `_visible` change reached installs for free precisely
because it changed **presence**. Everything proposed here changes **geometry and ordinals
only**, which `reconcile` is blind to by construction. Without a new mechanism, an existing
project would keep its 4.65-wide columns until every container happened to be re-laid for
an unrelated reason — i.e. indefinitely.

**Therefore: a persisted engine-rules version is now warranted.**

- `src/task_graph/layout/constants.py`: `ENGINE_RULES_VERSION = 1` — bumped by hand in any
  change to `flow.py`, `engine.py` ordinal assignment, or the constants above.
- `project_layout_meta` (`src/database/tables.py:393-404`) gains
  `Column("engine_rules_version", Integer, nullable=False, server_default="0")`. One
  Alembic revision, inspector-guarded so it is a no-op on a fresh database (the squashed
  baseline builds from live metadata).
- `publish_layout` stamps the current constant on every write.
- `_layout_step_body` (`layout_step.py:111-135`), inside the existing `sweep_due` branch so
  it costs one read per project per 900 s: any `(project, variant)` whose stored version is
  below `ENGINE_RULES_VERSION` gets **one** `enqueue_layout_job(pid, variant, "tidy")`.
  `enqueue_layout_job` already de-duplicates against a `queued`/`running` row
  (`layout_queries.py:188-198`), and `full_layout` republishes with the new stamp, so the
  migration is self-terminating and idempotent.

A rules bump is a deliberate one-off break of spatial memory, on the same footing as a Tidy
the operator pressed — which is what §3.4 already says Tidy is for. It also gives every
future geometry change a migration path for one integer.

`server_default="0"` means an install laid out before this ships is behind and re-tidies
once. Nothing else is needed: layout rows are derived data (§4.10).

---

## 5. Task breakdown

Style follows `2026-09-19-graph-visibility-markers-subtasks-implementation.md`. TDD
throughout: failing test, watch it fail, implement, watch it pass, commit. `aq test <files>`
only; never bare `pytest tests/`; `ruff check` the changed paths.

### Task 1 — Aspect-balanced banded row target

**Files:** `src/task_graph/layout/constants.py` (`ROW_TARGET_BANDS`, `ROW_ASPECT`,
`band_row`), `flow.py` (`flow_container` takes `target: float`; `_chain_overflows` /
`_flow_serpentine_chain` take the same value), `engine.py` (compute once in
`layout_container`, pass through `_evaluate`).
**Tests:** `tests/task_graph/layout/test_flow.py`, `test_engine_incremental.py`,
`test_engine_tidy.py`.

- Failing: `band_row` ladder; `ideal` is `max(floor, sqrt(area)·aspect, widest child)`; a
  scope of ≤8 unit cards reproduces today's coordinates **exactly**; 60 unit cards produce
  ≤6 lines and a content width ≥13; a scope containing one 12-wide child puts the next
  child beside it, not below.
- Implement. Assert in a test that `_evaluate` does **not** recompute the target (pass a
  counting stub) — this is the perf-critical bit.
- Failing: determinism (`test_deterministic`, `test_wallclock_stub_does_not_change_result`)
  still passes; serpentine folding still triggers at the new target.
- `aq test tests/task_graph/layout/` → commit `feat(layout): aspect-balanced banded row target`.

### Task 2 — `activity_class` and the `reorder` engine mode

**Files:** new `src/task_graph/layout/ordering.py` (`activity_class`, `sibling_sort_key`),
`engine.py` (`Mode` gains `"reorder"`; re-key each rank by the stable sort key before
flowing; `changed_ordinals` = keys that actually moved), `model.py`
(`ContainerScope` gains `blocked: frozenset[str]` and `phase_order: dict[str, int]`).
**Tests:** new `tests/task_graph/layout/test_ordering.py`, `test_engine_incremental.py`.

- Failing pure tests: running < unfinished < finished; a container classes by
  `agg_running`/`agg_active`, not its own status; `phase_order` outranks activity; equal
  class preserves the existing `order_key` order (stability); ties break on id.
- Failing engine tests: `mode="reorder"` moves finished siblings to the end of their rank
  and nothing across ranks; `mode="incremental"` is unchanged (`changed_ordinals` still
  `{"new"}` for the 1,000-child insert).
- `aq test tests/task_graph/layout/` → commit `feat(layout): activity-ordered siblings behind a reorder mode`.

### Task 3 — Driver triggers for `reorder`

**Files:** `src/task_graph/layout/driver.py` (`_seed_queue` emits `("reorder")` for a
container whose own `status.finished` mark arrived and for a container entering
`stubs`/`dropped`; `build_full_write_set` passes `mode="tidy"` unchanged; plumb `blocked`
and the phase metadata into `ContainerScope`), `src/database/queries/layout_queries.py`
(`load_project_snapshot` also reads `task_metadata` key `phase`).
**Tests:** `tests/task_graph/test_layout_driver.py`.

- Failing: an epic settling `COMPLETED` re-keys its **parent** scope and the epic ends up
  after its unfinished siblings; a **leaf** finishing does **not** re-key anything
  (`changed_ordinals` empty, no ordinal in the DB moved); a reopened epic floats back;
  `load_project_snapshot` gains no extra round trip beyond the one metadata read.
- `aq test tests/task_graph/test_layout_driver.py tests/task_graph/test_layout_queries.py`
  → commit `feat(layout): reorder a scope when a container settles`.

### Task 4 — Deferred `reflow` marks close the `active` holes

**Files:** `driver.py` (`_aggregates_only` records the parent; `run()` writes a `reflow`
mark), `process_dirty` (skip `reflow` marks), `src/orchestrator/layout_step.py` (the sweep
drains them in `reorder` mode).
**Tests:** `tests/task_graph/test_layout_dirty_marks.py`, `tests/task_graph/test_layout_step.py`.

- Failing: a finished leaf in `active` leaves the parent's geometry alone in the 5 s pass
  **and** writes one `reflow` mark; the sweep re-lays that container and the hole closes;
  marks de-duplicate per container per sweep.
- Commit `feat(layout): reclaim finished-leaf holes on the reconcile sweep`.

### Task 5 — `engine_rules_version` and the no-action migration

**Files:** `constants.py` (`ENGINE_RULES_VERSION`), `src/database/tables.py`, one Alembic
revision (inspector-guarded `add_column`, chained off the current head — check
`get_heads()` first, sibling branches collide on `a0000000000N`),
`src/database/queries/layout_queries.py` (`publish_layout` stamps it; `get_layout_meta`
returns it), `layout_step.py` (enqueue one tidy per stale `(project, variant)` inside
`sweep_due`).
**Tests:** `tests/task_graph/test_layout_queries.py`, `tests/task_graph/test_layout_step.py`,
`tests/alembic_revisions.py`.

- Failing: a meta row at version 0 with `ENGINE_RULES_VERSION = 1` enqueues exactly one
  tidy job per variant and none on the next sweep; a project already at the current version
  enqueues nothing; the migration is a no-op on a fresh database.
- Commit `feat(layout): persist the engine rules version and self-migrate geometry`.

### Task 6 — Phase tie-break end to end

**Files:** `ordering.py` (already), `driver.py` (phase metadata), optional
`src/api/graph_layout.py` if `phase_order` is not already on the row it needs.
**Tests:** `tests/task_graph/test_layout_driver.py`, `tests/test_api_graph_layout.py`.

- Failing: three phases with the `phase_create` gate edges land at ranks 0/1/2; deleting the
  middle phase leaves the remaining two in phase order rather than creation order; a phase
  never sorts by activity ahead of an earlier phase.
- Commit `feat(layout): phases order the canvas top to bottom`.

### Task 7 — Dashboard: pinned-position reconciliation

**Files:** `dashboard/src/pages/command-center/layout-v2/LayoutCanvas.tsx`,
`TaskToolbar.tsx`, `useGraphHierarchy.ts` (expose a pinned count).
**Tests:** `dashboard/src/pages/command-center/layout-v2/__tests__/LayoutCanvas.test.tsx`.

- Failing: after `layout_version` advances via a tidy job while pins exist, one dismissible
  banner appears with the pin count and a reset action calling `clearGraphPositions`; with
  no pins, nothing renders; the banner never appears twice for one version.
- `npx vitest run src/pages/command-center` → commit `feat(graph): offer to reset pinned cards after a rebuild`.

### Task 8 — Acceptance scenario and perf envelope

**Files:** `scripts/seed_layout_perf.py` (a `phases=` shape),
`tests/perf/test_layout_statements.py` (no new budget — prove the existing ones hold).
**Tests:** a new `tests/task_graph/layout/test_reorganisation_acceptance.py` (no `perf`
marker; pure engine + in-memory driver).

**The scenario.** 3 phases; 6 epics distributed across them; 80 leaf tasks of which 60 are
finished, 2 running, 18 open. Assert, on the `active` variant after a full layout:

1. **No scope is more than 2.5× taller than it is wide.** (Today the 40-task epic is
   4.65 × 12.53 — a 2.7 ratio — and the 60-task one is 4.0.)
2. **Every running task is inside the top 40% of the canvas extent** (`extent_h` from the
   meta row).
3. **Phase 1 (COMPLETED) sorts above phase 2 above phase 3** by `rank`, and within the root
   rank holding live work, the epic with `agg_running > 0` has a lower `order_key` than
   every `agg_active == 0` sibling.
4. **The `active` canvas holds at most 22 rows** (18 open + 2 running + the context
   containers), i.e. the §4.8 drop and the reclaim are both doing their job.
5. **Idempotence:** running `full_layout` twice yields identical rows (G5).
6. **`all` is readable too:** the same project in `all` is at most 3× wider than it is tall.

Then `AQ_PERF_STRICT=1 aq test -m perf -p no:xdist -s tests/perf/test_layout_statements.py
tests/perf/test_layout_api_statements.py` on a quiet box and record the numbers in the
commit body. Commit `test(layout): acceptance scenario for a canvas you can read on landing`.

**Order and parallelism.** Task 1 and Task 2 are independent. Task 3 depends on 2; Task 6
depends on 3. Task 4 is independent of all of them. Task 5 should land **after** 1 and 2,
because the version bump is what ships their geometry to existing installs. Task 7 depends
on 5. Task 8 is last.

---

## 6. Open questions for the operator

1. **Should a finished-but-still-context stub sink, or stay put?** The design sinks it (it
   is class 2). The counter-argument is spatial memory: an epic the operator has learned the
   position of moves once, at the moment it settles. Sink, or freeze finished containers
   where they are and only let live work float?
2. **Aspect ratio.** `ROW_ASPECT = 1.3` targets a mildly landscape scope. On an ultrawide
   monitor 2.0 would be better; on a laptop 1.0. It is a persisted, shared value — it cannot
   be per-viewer. What number?
3. **Growth-band waste.** With A1, a 13.85-wide scope is allocated 24.0 units. Adding a
   finer ladder (×1.4 above 12) fixes the visual waste and costs ~2.4× more band crossings,
   i.e. more translations per publish. Worth it, or live with the existing §12 limitation?
4. **Does the rules-version bump auto-tidy every project unprompted?** The design says yes
   (one deliberate break of spatial memory, then never again). The alternative is a
   `layout_stale: true` flag on `extent` and a "Rebuild layout" button — more control, and
   an operator who never presses it never gets the fix.
5. **Manual pins.** §11 says pinned nodes are out of scope; the canvas implements them. Keep
   and reconcile (this design), or retire them now that the server owns geometry?
6. **Continuous activity ordering.** B3 reorders only when a container settles. If the
   operator would rather have running work float the instant it starts — and accept cards
   sliding sideways under the pointer — that is B1, a one-line change to the same trigger
   set. Which trade do they want?
