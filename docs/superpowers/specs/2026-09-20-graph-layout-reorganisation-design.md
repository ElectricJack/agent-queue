# Task-graph layout that reorganises itself

**Status:** design, no code. Revised 2026-09-20 after adversarial review
(`.superpowers/sdd/2026-09-19-graph-visibility-markers-subtasks-implementation/t20-review.md`,
verdict *approve with changes*) and the controller's rulings. Companion to
`docs/superpowers/specs/2026-09-01-task-graph-spatial-layout-design.md` (the engine spec;
"§4.4" below refers to it) and to
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

**Scope of the first slice.** Three changes, no schema change, no new engine mode:
a row target that balances a scope's aspect ratio; convergence of existing installs
through the `layout_jobs` ledger; and activity-aware ordering at the **tidy seed** only.
Everything else the first draft proposed is recorded in §6 as deferred, with the reason it
does not work as written.

---

## 1. How placement works today, from the code

### 1.1 The three coordinates a node has

`LayoutRow` (`src/task_graph/layout/model.py:19-42`) carries, per `(project_id, variant,
task_id)`:

| Field | Meaning | Written by |
|---|---|---|
| `rank` | the node's **layer** inside its container: `rank(dependent) >= rank(blocker) + 1`, longest-path over sibling `blocks`/`waits-for`/`conditional-blocks` edges | `layering.py:72-95` (`minimal_ranks_acyclic`) |
| `order_key` | a fractional string key ordering siblings **within one rank**, left to right | `order_key.py:16` (`between`) |
| `depth` | depth below the project root; a level-of-detail input only (`view.py:83-91`), never a placement input | `engine.py:153` |
| `path` | `/a/b/<id>/`, the subtree prefix used for translations and deletes | `engine.py:152` |

`ordinal` is `(rank, order_key)` (`model.py:40-42`). Everything else — `rel_x/rel_y`,
`abs_x/abs_y`, `w/h` — is *derived* from the ordinals plus the children's sizes by
`flow_container`, on every pass. **Ordinals are the persistent decision; coordinates are
recomputed each time.**

### 1.2 Rank assignment, and why the root is one flat rank

`minimal_ranks_acyclic` (`layering.py:72-95`) gives every node with no sibling blocker
`rank = 0`. Root-level tasks rarely block each other, so in a typical project **every root
epic and every loose root task sits in rank 0** — one layer that the flow step then wraps
into lines.

Phases are a partial exception and a smaller one than it looks. `phase_create` adds a
`blocks` edge onto every earlier sibling phase **that has not COMPLETED**
(`src/commands/phase_commands.py:160-168`: `task.id for task, _ in siblings if
task.status is not TaskStatus.COMPLETED`). So phase 3, created after phases 1 and 2 have
completed, receives **no** gate edge and lands at rank 0 beside them. Phases therefore do
*not* reliably rank themselves; see §6.4.

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
`engine.py:194-227`) — a horizontal choice inside the dependent's own rank.

**No code path consults `status`, `is_blocked`, aggregates or `task_metadata.phase.order`
when choosing a rank or an order key.** The engine's only status inputs are `_visible`
(`driver.py:94-171`), which decides *presence*, and `_sizes` (`engine.py:89-96`), which
makes a stub card-sized.

Stronger still: **a task starting work does not even wake the layout engine.** A dirty
mark is written only when finished-ness flips
(`src/database/queries/task_queries.py:1130-1148`: `if was_finished != now_finished`,
reason `status.finished` / `status.reopened`). `READY → IN_PROGRESS` writes no mark at
all, so no pass runs and nothing could re-key even if it wanted to. This is the fact that
kills the first draft's "float running work" trigger (§6.1).

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
   (`constants.py:17,47-55`; `flow.py:94-100`). The **allocated** size is what the canvas
   draws, so an epic renders 6 or 12 or 24 units wide whatever it contains. The root is
   never banded (`flow.py:97-98`).

A run of consecutive singleton ranks linked blocker→dependent is exempted and folded into
a serpentine (`_serial_chains` `engine.py:99-133`, `_flow_serpentine_chain`
`flow.py:126-175`) — the one place today's flow already re-shapes rather than stacks.
Both halves of that mechanism are keyed to the same `target`: `_chain_overflows`
(`flow.py:104-106`) decides *whether* to fold by comparing the chain's width to it, and
the fold right-aligns its reverse lines at `x = target` (`flow.py:165`) so the turn
connector is short. §3.1 has to keep that carve-out honest.

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
12.2 wide while that card line is 6.75, so the row reads as running off the right edge of
everything else.

```
# six finished epics, each banded to 6.0 x 4.0, at the root
e0 (0.0, 0.00)  e1 (0.0, 4.22)  e2 (0.0, 8.44)  e3 (0.0, 12.66) ...
content = 6.20 x 25.65   lines_per_rank = [6]
```

Every epic is 6 units wide and the root target is 7, so **two epics never fit on a line**.
Six epics become a 25.65-unit-tall single-file column in `created_at` order. That is the
second screenshot.

And inside an epic (content size, and the allocated box the canvas actually draws):

| children | content w × h | lines | allocated | allocated area |
|---|---|---|---|---|
| 12 cards | 4.65 × 3.99 | 3 | 6 × 6 | 36 u² |
| 20 cards | 4.65 × 6.43 | 5 | 6 × 12 | 72 u² |
| 40 cards | 4.65 × 12.53 | 10 | 6 × 24 | 144 u² |
| 60 cards | 4.65 × 18.63 | 15 | 6 × 24 | 144 u² |
| 120 cards | 4.65 × 36.93 | 30 | 6 × 48 | 288 u² |

Width is pinned at 4.65 forever; height is linear in the child count. **That is the
"grows downward" the operator is describing** — not the root stacking alone, but every
scope in the project having a fixed width and an unbounded height.

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

One property matters for §3.2: `build_full_write_set` computes the whole project's
aggregates at `driver.py:238`, **before** the first `lay()` call. On the tidy path,
per-child aggregates are therefore available and fresh at seeding time. (On the
incremental path they are not — `_refresh_aggregates` runs after `_drain`,
`driver.py:837-849` — which is why §6.1 is deferred and this is not.)

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
| Task **starts running** | No pass runs at all | no dirty mark is written (`task_queries.py:1130-1148`) |
| Leaf **finishes**, `all` variant | No, by design | `_aggregates_only` returns `True` (`driver.py:589-591`): the node is still present, only ancestor counters change |
| Leaf **finishes**, `active` variant | **No — a permanent hole** | `_aggregates_only` (`driver.py:568-602`) returns `True`, so `run()` deletes the row (`driver.py:842-847`) **without re-laying the parent**. The comment is explicit: re-flowing a 5,000-child root costs 1.4 s, so "the next ordinary pass over that container — a created, moved or reopened sibling, or a tidy job — closes the gap" |
| Container **finishes** and becomes a stub / is dropped | Yes | excluded from `_aggregates_only` (`driver.py:594-602`), so the parent is re-laid |
| Viewer **collapses** a container | Height only, never width | `compaction.py:140-177` |
| `reconcile` sweep (900 s) | Presence only | "It deliberately does not chase *geometry* drift — §4.6's finished-leaf fold leaves an empty slot behind on purpose" (`driver.py:350-351`) |

Compaction deserves its own line. `_pack` (`compaction.py:140-177`) groups children into
lines by their **persisted `rel_y`** (`compaction.py:152-154`) and slides each child left
by what its earlier line-mates gave up. It never re-wraps — §3.5 says so outright, and
`test_lines_are_inherited_not_re_wrapped` pins it. So collapsing the six-epic column above
turns it into a column of six 1×1 cards: 25.65 units of height come back, the width stays 1.

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
epics become N lines. (ii) Order within that layer is `order_key` (`engine.py:85`), pure
creation order — appended at the end for new nodes (`engine.py:246-250`, `:391-404`) and
re-seeded from `created_at` even by Tidy (`engine.py:353-360`). The newest work is
therefore always **last**, and since each epic owns a line, "last" means "furthest down".
Nothing in the engine reads status, and nothing even wakes it when work starts (§1.3).

**Symptom 3 — "space is not reclaimed / it does not reorganise".** Three separate
non-reclaims, in ascending order of importance: (i) in `active`, a finished leaf's row is
deleted but its container is never re-flowed (`driver.py:568-602`) and `reconcile`
deliberately ignores geometry drift (`driver.py:350-351`), so the hole is permanent until
an unrelated sibling event or a Tidy; (ii) compaction reclaims height but never width
(§3.5, `compaction.py:152-154`); (iii) the real one — **a scope's width is a constant, so
every scope's aspect ratio degrades linearly with its child count** (4.65 × 18.63 at 60
children, 4.65 × 36.93 at 120). Even a perfectly reclaimed canvas grows downward, because
the flow has no notion of "this container should be roughly as wide as it is tall".

Only (iii) is addressed in the first slice. (i) and (ii) are §6.2 and remain as they are.

---

## 3. The first slice

### 3.1 An aspect-balanced row target, aligned to the growth ladder

Replace the constant at `flow.py:44` with a target computed once per container pass from
the children's sizes:

```
ROW_ASPECT = 1.3                           # landscape-ish scopes

def row_target(sizes, *, is_root) -> float:
    floor = TARGET_ROW_WIDTH_ROOT if is_root else TARGET_ROW_WIDTH     # 7.0 / 4.5
    area  = Σ_i (w_i + SIBLING_GAP) · (h_i + LINE_GAP)
    want  = max(sqrt(area) · ROW_ASPECT, max_i w_i)
    if want <= floor:
        return floor                       # today's geometry, byte for byte
    b = smallest band in GROWTH_BANDS (then doubling) with b - 2·PADDING >= want
    return b - 2·PADDING
```

Four properties, each answering a specific finding:

- **`sqrt(area) · aspect` targets a square-ish scope** instead of a fixed-width strip.
  That is the fix for symptom 3(iii).
- **`max_i w_i`** is symptom 1's fix: the target is never narrower than the widest child,
  so a 12-unit epic no longer leaves its line-mates packed into 7.
- **The floor is returned unchanged when the ideal is below it**, so every small scope
  (≲8 unit cards, and every scope in the existing engine tests) keeps today's coordinates
  exactly. 4.5 and 7.0 remain the first rungs of the ladder.
- **Bands are aligned to the growth ladder** (`b − 2·PADDING`), so a scope's content lands
  *just under* the growth band it will be allocated at, and `band_up(content_w) == b`
  exactly. This is the fix for the review's S5: without it a wider content box snaps up a
  growth band and the drawn box doubles in area.

Measured (`flow_container` with the banded target; content, lines, and the **allocated**
box the canvas draws):

| children | today content → allocated (area) | target | new content → allocated (area) |
|---|---|---|---|
| 12 cards | 4.65 × 3.99 → 6 × 6 (36 u²) | 5.8 | 5.80 × 3.99 → 6 × 6 (**36 u²**) |
| 20 cards | 4.65 × 6.43 → 6 × 12 (72 u²) | 11.8 | 11.55 × 2.77 → 12 × 3 (**36 u²**) |
| 40 cards | 4.65 × 12.53 → 6 × 24 (144 u²) | 11.8 | 11.55 × 5.21 → 12 × 6 (**72 u²**) |
| 60 cards | 4.65 × 18.63 → 6 × 24 (144 u²) | 23.8 | 23.05 × 3.99 → 24 × 6 (**144 u²**) |
| 120 cards | 4.65 × 36.93 → 6 × 48 (288 u²) | 23.8 | 23.05 × 7.65 → 24 × 12 (**288 u²**) |
| 500 cards | 4.65 × 152.83 → 6 × 192 (1152 u²) | 47.8 | 47.20 × 16.19 → 48 × 24 (**1152 u²**) |

**The allocated area never grows; at 20 and 40 children it halves.** S5 is answered rather
than argued away — but only *for these homogeneous scopes*. See the clamp below, which is
what makes the statement true in general.

And the operator's first screenshot, re-flowed: the 12-unit epic plus 8 cards becomes a
single line, `epic (0, 0)`, `c0..c7` at `x = 12.15 … 20.20, y = 0` — content 21.40 × 6.55
in one line instead of 12.20 × 8.99 in three ragged ones.

**The serpentine carve-out (review S3).** Because `max_i w_i` can widen the target far
past the floor, the chain machinery must not follow it: an 8-card chain that folds at 4.5
would stop folding at 11.8 and become an 8-rank vertical stack, and a fold that
right-aligned at 23.8 would draw a long diagonal instead of a short turn. So
`flow_container` takes **two** targets — `target` for rank wrapping, `chain_target` for
`_chain_overflows` and the reverse-line right-align — and `chain_target` is always the
floor constant. Serpentine behaviour is then bit-identical to today at every scope size,
which `test_engine_incremental.py:36` and `test_compaction.py:279` already pin.

**Alternative rejected: a dedicated `ROW_TARGET_BANDS` ladder** (4.5, 7.0, 10.0, 14.0, …).
It gives a nicer aspect ratio (60 cards → 13.85 × 6.43 rather than 23.05 × 3.99) but the
content no longer lands under a growth band, so the 60-card epic's drawn box goes from
144 u² to 288 u². Rejected per S5. The residual cost of the adopted rule is that the
ladder doubles, so aspect snaps coarsely — a 60-card scope is 5.8:1 landscape rather than
~2:1. A finer growth ladder is the remedy and is an open question (§7.3), not bundled here.

**The never-grows clamp (t24 finding F1).** The table above is unit cards. For a
*heterogeneous* scope — one tall child plus small ones, which is exactly what a container
holding a container looks like, i.e. this change's whole blast radius — the ideal can buy
width that the growth ladder then charges a whole band for, and the drawn box **doubles**:
`{pkg (3.0, 6.0), 4 unit cards}` goes from `6 × 12` (72 u²) at the floor to `12 × 12`
(144 u²) at the ideal 11.8, as does `{(3,3), (1,1), (1,3), (1,6)}`. A 4,000-scope random
sweep found growth in ~8% of non-root scopes, worst ×2, and a fatter child widens its
parent's target, so it cascades upward.

So the property is **enforced, not hoped for**. `clamp_row_target(ordered, sizes, …,
target)` steps the ideal down the row ladder until the flow's allocated area is no bigger
than the **floor** target's for the same children in the same ordering; the floor always
qualifies, because it is the baseline. It runs **once per container pass**, on the
ordering that is actually published — so "the drawn box never grows" is true by
construction rather than by sampling — and costs at most
`1 + len(row_target_rungs(floor, up_to=target))` flow passes, i.e. O(log scope width), and
nothing at all when the target is already the floor. The unit-card table above is
unaffected: the clamp never fires there.

**The root is exempt, on purpose.** It is never banded — `allocated is content`
(`flow.py:97-98`) — so there is no band to snap up and F1's failure mode does not exist
for it, and it has no parent to push. Area is also the wrong measure for the root: trading
width for height is precisely symptom 1's fix, and the operator's own screenshot gains
area by doing it (12.20 × 8.99 in three ragged lines → 21.40 × 6.55 in one). Clamping the
root by area would step that straight back to 12.20 × 7.77 in two lines, i.e. throw the
headline fix away. So the clamp is applied to containers, which are what "the drawn box"
means, and F1's own evidence — both counterexamples and the ~8% sweep — is non-root.

The tidy sweep keeps evaluating against the *unclamped* ideal, which depends on `sizes`
alone. That is deliberate: the sweep's cost landscape stays continuous and independent of
the candidate ordering, and only the single publishing flow pays for the clamp.

**Alternative rejected: a viewport-derived target.** The persisted layout is shared and
must not depend on who is looking (§3.5).

**Complexity.** `row_target` is one pass over the children, O(k). It is computed **once
per `layout_container` call** and threaded through `_evaluate`, never recomputed inside it
— `_tidy_sweep` calls `_evaluate` thousands of times (`engine.py:309-331`). It depends
only on `sizes`, never on the candidate ordering, so the cost landscape stays continuous
and G5 is untouched. `clamp_row_target` adds O(log scope width) flow passes, also once per
`layout_container` call and also outside `_evaluate`. Net asymptotic change: none.

**Guarantees.** G1, G2, G5, G6, G7 unchanged. **G3 is relaxed, bounded**: crossing a
row-target band re-wraps one scope. Because the ladder is the growth ladder, a row-target
crossing can only happen at a growth-band crossing, so it is the *same* O(log n) event
G4 already bounds — no new class of propagation. Between crossings the target is constant
and G3 holds verbatim.

**Cost, priced by the publish (review S1).** The engine evaluation is not the expensive
half; the publish is. A row-target crossing at the root emits one
`UPDATE task_layouts … WHERE path LIKE :prefix` per translated sibling subtree, re-selects
those subtrees and rewrites their `task_layout_cells` rows (`publish_layout`,
`src/database/queries/layout_queries.py:484`). That is exactly the cost
`test_root_band_crossing_publish_under_1s` already pins, and §5 Task 1 adds a sibling test
for the row-target case at the same seed scale. A wider scope also covers more 8×8 cells
(`flow.py:178-184`) — but the clamp guarantees the allocated box never grows, so the cell
count cannot grow either.

### 3.2 Ordering that surfaces active work — at the tidy seed only

**What changes.** One sort key, at one site: `engine.py:353-360`, the tidy seeding loop.

```
(phase_order, activity_class, created_at, id)      # replaces (created_at, id)
```

- `phase_order` = `task_metadata["phase"]["order"]` when present, else a sentinel that
  sorts **after** every real phase order (a non-phase sibling never jumps ahead of phase 1).
- `activity_class(child, agg)`:
  - `0` — a leaf whose `status` is in `RUNNING_STATUSES` (`constants.py:37`), or a
    container whose `agg["running"] > 0`;
  - `1` — otherwise not in `FINISHED_STATUSES` (`constants.py:36`), or a container whose
    `agg["active"] > 0`;
  - `2` — otherwise (finished, including a finished-but-context stub).
- `created_at, id` remain as the final tie-breaks, so **a scope whose siblings are all one
  class produces byte-identical output to today** — which is why all three existing tidy
  tests pass unmodified (every task in them is `READY`).

**Why the aggregates are safe here and nowhere else.** `build_full_write_set` computes the
project's aggregates at `driver.py:238`, before the first `lay()` (§1.6). The tidy path
can hand each scope its children's fresh aggregates. The incremental path cannot — it
refreshes them after `_drain` (`driver.py:837-849`) — which is review finding B5, and is
why §6.1 is deferred rather than shipped alongside.

**Phases stay top-to-bottom.** Where a gate edge exists, `minimal_ranks_acyclic` already
puts phase *N+1* below phase *N*, and rank **is** the engine's time axis; laying phases
left-to-right would mean fighting the layering or widening every root row to hold six
phases side by side. Where the gate edge is *absent* — phase 3 created after phases 1–2
completed (§1.2) — the phases share rank 0 and the new `phase_order` term at least keeps
them in phase order left to right. Making the rank itself respect phase order is §6.4.

**What this does and does not deliver.** It delivers the operator's ordering on every
Tidy — including the automatic one §3.3 triggers on every existing install, which is how
the change reaches them with no operator action. It does **not** re-order between tidies:
new work still appends to the end of its rank (`engine.py:246-250`). Accepted for the
first slice; the operator looks at the result before §6.1 is re-scoped.

**Guarantees.** G1–G6 untouched: this changes only `mode="tidy"`, which G7 already exempts.

### 3.3 Convergence of existing installs, with no schema change

The problem: `reconcile` compares presence, `container_id` and `kind`
(`driver.py:368-382`) and says so in its own docstring — "It deliberately does not chase
*geometry* drift". The 2026-09-20 `_visible` change reached installs for free precisely
because it changed **presence**. §3.1 and §3.2 change geometry and ordinals only, which
`reconcile` is blind to by construction. Without a new mechanism an existing project keeps
its 4.65-wide columns indefinitely.

**Rejected: a `project_layout_meta.engine_rules_version` column.** `publish_layout`
(`layout_queries.py:484`) runs on every 5 s incremental publish and would stamp the
current version within seconds of the upgrade, so the 900 s sweep would never see a stale
pair. Only a *full* layout may record convergence — and once that is true, the column buys
nothing a ledger row does not. Dropped, with the Alembic revision. **No schema change.**

**Adopted: the `layout_jobs` ledger.** `layout_jobs` rows are never trimmed and `kind` is
free-text `Text` with no constraint (`src/database/tables.py`, the `layout_jobs` table).
So the ledger *is* the convergence record:

- `ENGINE_RULES_VERSION = 1` in `constants.py`, bumped by hand in any change to `flow.py`,
  to ordinal assignment in `engine.py`, or to the geometry constants.
- The job kind is the literal string `f"rules:{ENGINE_RULES_VERSION}"` — `"rules:1"` for
  this slice. `next_layout_job` / `full_layout` ignore `kind`
  (`layout_step.py:113-120` passes only `project_id` and `variant`), so a rules job *is* a
  tidy, with a label.
- In the existing `sweep_due` branch of `_layout_step_body` (`layout_step.py:137-146`),
  after the reconcile loop: for each project, for `variant in ("active", "all")` —
  **`active` first**, because it is the variant the canvas shows by default — skip the
  pair if it has no meta row (nothing published yet; its first full layout will use the
  new rules anyway), skip it if the ledger already holds a non-`failed` job of that kind,
  otherwise enqueue one and **return** — at most **one** `(project, variant)` per sweep
  (review S2: `next_layout_job` claims one job per 5 s step and `full_layout` is a
  CPU-bound thread with a 60 s budget, so a fan-out of 10 projects × 2 variants would put
  a long tail of full rebuilds in front of every project's ordinary incremental work).
- `enqueue_layout_job` de-duplicates on `(project_id, variant, status in
  ('queued','running'))` **regardless of kind** and returns the existing row. So the
  caller must check `job["kind"] == kind`; if an unrelated tidy is in flight the pair is
  left stale and retried on a later sweep. Self-limiting and idempotent.
- A `failed` rules job is not treated as convergence, so it is retried on a later sweep —
  but the walk is **two passes**: never-attempted pairs first, then failed ones, capped at
  `MAX_RULES_ATTEMPTS = 3` failures per `(project, variant, version)`, after which the pair
  is skipped with one WARNING naming it and its last error. Without the split, one pair
  that fails deterministically would be re-picked every sweep and nothing else would ever
  converge; bumping the version starts a new ledger and a fresh budget.
- Nothing else resets a `running` job (`next_layout_job` claims only `queued`) and shutdown
  waits 30 s while a tidy may run 60, so a restart mid-rebuild could leave a row `running`
  forever — which would wedge the fleet-wide stand-down *and* count as convergence for its
  pair. The sweep therefore begins with a reaper: any job `running` for more than
  `4 x tidy_job_budget_seconds` is marked `failed` ("orphaned: daemon stopped mid-job") and
  logged once at WARNING. The job runner also records a cancelled job as failed in a
  `finally`, for the case the process survives the cancellation. Both cover operator tidies
  and backfills too; neither needs a schema change (`layout_jobs.started_at` already exists).
- Convergence walks **ACTIVE** projects only — a paused or archived project must not spend a
  CPU-bound full layout. (The reconcile loop above it is unchanged and still walks all.)
- Cost: **five statements per sweep, flat**, whatever the project count — the reaper, the
  ledger (`layout_job_ledger`, one row-set for the whole fleet), the published-pair set
  (`published_layout_variants`), the active-project list, and at most one enqueue.

**What an operator sees.** `enqueue_layout_job` de-duplicates per `(project, variant)`
whatever the kind, so an `aq graph tidy` (or the dashboard's Tidy button) issued while a
`rules:<n>` job is already queued for that pair is served by that job and the response
shows `kind: rules:<n>` — the rebuild is the same full layout either way. And because the
job queue is FIFO, an operator Tidy may wait behind at most **one** rules rebuild, since
convergence never queues a second while one is in flight.

**Blast radius, stated plainly (review S4).** `row_target` depends on the children's
allocated sizes, so **any container holding a container child changes** — i.e. essentially
every project with epics re-lays, and the rebuild is a Tidy, which §3.4 already defines as
a deliberate break of spatial memory. At one pair per 900 s sweep a ten-project install
converges over about five hours of daemon uptime, invisibly. Manual pins
(`LayoutCanvas.tsx:446`) are **left exactly as they are** in this slice; see §6.3.

One accepted inefficiency: a project first laid out *after* the upgrade has no ledger row
either, so the next sweep enqueues one redundant rebuild for it. Once per project per
rules version, on a project that is by definition small.

---

## 4. What is explicitly unchanged in the first slice

- `compaction.py` — no change; it still never re-wraps.
- `_aggregates_only` and the `active` finished-leaf hole — no change.
- `reconcile` — no change; it still chases presence only.
- The incremental path's ordinal assignment — no change; new work still appends.
- `manual_positions` and the dashboard — no change at all.
- The API surface and the generated clients — no change. No `src/api/models/` edit, so no
  `regenerate-api-client.sh` step in any of the three tasks.

---

## 5. Task breakdown — three tasks

TDD throughout: write the failing test, watch it fail, implement, watch it pass, commit.
`aq test <files>` only; never bare `pytest tests/`; never raise `-n`; exit code 75 means
no slot, retry. `ruff check <changed paths>`, line length 100. Commit only the paths the
task owns, explicit paths, never `git add -A`.

---

### Task 1 — Aspect-balanced row target, aligned to the growth ladder

**Files**

- Modify `src/task_graph/layout/constants.py`: add `ROW_ASPECT = 1.3`. `TARGET_ROW_WIDTH`
  (4.5) and `TARGET_ROW_WIDTH_ROOT` (7.0) keep their values and become the **floors**.
- Modify `src/task_graph/layout/flow.py`: add `row_target`; give `flow_container` the two
  new keyword arguments; thread `chain_target` into `_chain_overflows` and
  `_flow_serpentine_chain`.
- Modify `src/task_graph/layout/engine.py`: compute both targets once in
  `layout_container` (before any `_evaluate`) and pass them through `_evaluate` into
  `flow_container`.

**Interfaces — produces**

```python
# constants.py
ROW_ASPECT: float = 1.3

# flow.py
def row_target(
    sizes: Mapping[str, tuple[float, float]], *, is_root: bool
) -> float: ...
#   floor = TARGET_ROW_WIDTH_ROOT if is_root else TARGET_ROW_WIDTH
#   area  = sum((w + SIBLING_GAP) * (h + LINE_GAP) for w, h in sizes.values())
#   want  = max(sqrt(area) * ROW_ASPECT, max(w for w, _ in sizes.values()))
#   if want <= floor: return floor
#   b = smallest value of GROWTH_BANDS (then repeated doubling) with b - 2*PADDING >= want
#   return b - 2 * PADDING
#   Empty `sizes` returns the floor.

def flow_container(
    ordered: list[list[str]],
    sizes: dict[str, tuple[float, float]],
    *,
    is_root: bool,
    serpentine_chains: tuple[tuple[str, ...], ...] = (),
    target: float | None = None,        # None -> the floor constant (today's behaviour)
    chain_target: float | None = None,  # None -> the floor constant, ALWAYS
) -> FlowResult: ...
```

`target` governs the rank-wrap test at `flow.py:78` and nothing else. `chain_target`
governs `_chain_overflows` and the reverse-line right-align at `flow.py:165`, and callers
never pass anything but the floor — it is a parameter only so the tests can assert the
separation. Defaulting both to `None` keeps every existing direct caller and test working
unchanged.

**Tests — `tests/task_graph/layout/test_flow.py`**

- `test_row_target_returns_the_floor_for_a_small_scope` — 4 and 8 unit cards, root and
  non-root, return exactly 4.5 / 7.0.
- `test_row_target_balances_the_aspect_ratio` — the six rows of §3.1's table: for
  12/20/40/60/120/500 unit cards assert the target, the content size to 2 dp, the line
  count, **and the allocated size**, and assert the allocated area is `<=` today's value
  from §1.5's table. This is the test that pins S5.
- `test_row_target_is_never_narrower_than_the_widest_child` — one 12-wide child plus 8
  cards at the root flows on a single line, `epic` at `(0, 0)` and `c7` at `(20.20, 0.0)`.
- `test_content_lands_under_its_growth_band` — for each of the six sizes,
  `band_up(content_w) == content_w rounded up`, i.e. `content_w <= band_up(content_w)` and
  `band_up(content_w) - content_w < 2 * PADDING + CARD_W`.
- `test_serpentine_threshold_uses_the_floor_not_the_widened_target` — an 8-card chain in a
  scope whose `row_target` is 11.8 still folds at 4.5: assert the fold happened
  (`len({y for …}) > 1`) and that the reverse line right-aligns at
  `TARGET_ROW_WIDTH - CARD_W`, not at the widened target. **This is the S3 regression test.**

**Tests — `tests/task_graph/layout/test_engine_incremental.py`**

- `test_target_is_computed_once_per_container_pass` — monkeypatch `flow.row_target` with a
  counting wrapper and run `layout_container(..., mode="tidy")` over a 20-node fixture with
  edges; assert the counter is 1 although `_evaluate` ran many times. **This is the perf
  guard**; without it the change is a hot-loop regression.
- `test_incremental_trajectory_is_stable` (review's incremental-trajectory item) — drive
  `_IncrementalBatch` over a real DB through **create → start → finish → create-next** for
  both variants, and assert at each step: the start transition writes **no** `layout_dirty`
  row at all (`task_queries.py:1130-1148`); every leaf status change yields
  `changed_ordinals == set()`; the new sibling changes no existing ordinal. Belongs in
  `tests/task_graph/test_layout_driver.py` if a DB fixture is easier there.
- `test_published_positions_move_only_on_a_band_crossing` (review's stability metric) —
  after each published version in that trajectory, compute
  `Σ |Δabs_x| + |Δabs_y|` over all rows against the previous version and assert it is
  `0.0` except on versions where the scope's `row_target` or `band_up` output changed;
  on those, assert the moved set is confined to the crossing scope and its later siblings.
- `test_deterministic_under_shuffled_size_dicts` — build `child_sizes` in two different
  insertion orders; assert identical ordinals **and** identical coordinates.

**Existing tests that pin today's constants — the expected verdict for each**

| Test | Verdict |
|---|---|
| `test_flow.py:12` `test_comfortable_spacing_constants_are_dense_but_positive` | **Passes unmodified** — `TARGET_ROW_WIDTH` is still 4.5. Add one assertion for `ROW_ASPECT == 1.3`. |
| `test_flow.py:30` `test_long_serial_chain_wraps_back_and_forth_with_short_turn` | **Must pass unmodified** — it is the S3 guard; it calls `flow_container` with default targets. |
| `test_engine_incremental.py:36` `test_long_serial_dependencies_fold_into_serpentine_rows…` | **Must pass unmodified.** Verified: 8 root unit cards give `want = 4.35 <= 7.0`, so the target is the floor, and the chain path uses `chain_target` anyway. If this test needs editing, the carve-out is wrong. |
| `tests/task_graph/layout/test_compaction.py:278` (`assert len({rows[f"c{i}"].rel_y …}) > 1  # rank wrapped`) | **Expected to fail and to need widening.** Verified by reproducing the fixture's geometry: epic `e` holds 7 unit cards plus `pkg` allocated `(6.0, 3.0)`, so `want = max(5.44·1.3, 6.0) = 7.07 → target 11.8`, and all 7 cards land on one line (`rel_y` distinct count 1) with `pkg` wrapping below. Re-seed the fixture with enough cards to wrap at the new target (≈12), or assert on a scope whose target is the floor. Line `:279` (`# chain folded`) passes unmodified — the chain is inside `pkg`, whose own target is the floor. |

Also re-run `tests/task_graph/test_layout_driver.py` (it asserts `rel_x` at
`:461,:479,:503,:523` on two- and three-child scopes, all below the floor, so they should
pass) and `tests/test_api_graph_layout.py`.

**Perf (review S1)** — add to `tests/perf/test_layout_statements.py`, modelled line for
line on `test_root_band_crossing_publish_under_1s`:
`test_row_target_band_crossing_publish_under_1s` — grow one epic across a row-target band
at the committed `seed_layout_perf` scale and assert the publish transaction is under 1 s.
Run deliberately: `AQ_PERF_STRICT=1 aq test -m perf -p no:xdist -s
tests/perf/test_layout_statements.py`, and record the measured numbers in the commit body.

**Run** `aq test tests/task_graph/layout/ tests/task_graph/test_layout_driver.py
tests/test_api_graph_layout.py` →
commit `feat(layout): aspect-balanced row target aligned to the growth ladder`.

---

### Task 2 — Convergence through the `layout_jobs` ledger

No schema change, no Alembic revision, no API change.

**Files**

- Modify `src/task_graph/layout/constants.py`: `ENGINE_RULES_VERSION = 1`.
- Modify `src/database/queries/layout_queries.py`: add `layout_job_exists`.
- Modify `src/orchestrator/layout_step.py`: the convergence step inside `sweep_due`.

**Interfaces — produces**

```python
# constants.py
ENGINE_RULES_VERSION: int = 1
#: Bump by hand in any change to flow.py, to ordinal assignment in engine.py,
#: or to the geometry constants above. The job kind is f"rules:{ENGINE_RULES_VERSION}".

# layout_queries.py (LayoutQueriesMixin)
async def layout_job_exists(self, project_id: str, variant: str, kind: str) -> bool:
    """True when a job of this (project, variant, kind) exists and did not fail."""
    # SELECT 1 FROM layout_jobs
    #  WHERE project_id=:p AND variant=:v AND kind=:k AND status != 'failed' LIMIT 1
```

```python
# layout_step.py, at the end of the existing `if sweep_due:` block,
# inside the loop that already walks `await self.db.list_projects()`:
kind = f"rules:{ENGINE_RULES_VERSION}"
for project in projects:
    for variant in ("active", "all"):            # active first: the default canvas
        if await self.db.get_layout_meta(project.id, variant) is None:
            continue                             # nothing published; its first layout
                                                 # will already use the new rules
        if await self.db.layout_job_exists(project.id, variant, kind):
            continue
        job = await self.db.enqueue_layout_job(project.id, variant, kind)
        if job["kind"] != kind:
            continue    # an unrelated tidy is in flight; retry on a later sweep
        return          # at most ONE stale pair per sweep (review S2)
```

`enqueue_layout_job` already de-duplicates on `(project_id, variant, status in
('queued','running'))` **regardless of kind** and returns the pre-existing row — hence the
`job["kind"]` check. `next_layout_job` and `full_layout` are untouched; `kind` is a ledger
label only.

**Tests — `tests/task_graph/test_layout_step.py`**

- `test_the_sweep_enqueues_one_stale_pair_per_sweep_active_first` — three projects, each
  with both variants published and no `rules:1` row; one sweep enqueues exactly one job,
  and it is `variant="active"` of the first project. **This is the one-job-per-sweep test.**
- `test_successive_sweeps_walk_every_stale_pair` — six sweeps converge all three projects,
  each job appearing exactly once.
- `test_a_converged_pair_is_never_re_enqueued` — a `done` `rules:1` job for a pair means
  the next sweep skips it and moves to the next pair.
- `test_a_failed_rules_job_is_retried` — `status="failed"` is not convergence.
- `test_an_unrelated_tidy_in_flight_defers_the_pair` — pre-queue a plain `"tidy"` job for
  the pair; the sweep writes no `rules:1` row and the pair is still stale next sweep.
- `test_a_project_with_no_meta_row_is_skipped`.
- `test_the_sweep_is_bounded_when_nothing_is_stale` — with every pair converged, the
  convergence step issues at most one `layout_job_exists` read per pair and enqueues
  nothing.

**Tests — `tests/task_graph/test_layout_queries.py`**

- `test_layout_job_exists_ignores_failed_and_other_kinds`.
- `test_a_rules_job_runs_a_full_layout` — `next_layout_job` returns the `rules:1` row and
  `full_layout` republishes the variant; assert `layout_version` advanced and the rows
  carry the new geometry.

**Run** `aq test tests/task_graph/test_layout_step.py
tests/task_graph/test_layout_queries.py tests/task_graph/test_layout_driver.py` →
commit `feat(layout): converge existing installs through the layout_jobs ledger`.

---

### Task 3 — Activity-aware tidy seed

Depends on Task 1 landing first (they touch adjacent lines in `engine.py`); independent of
Task 2, but Task 2 is what delivers it to existing installs.

**Files**

- Create `src/task_graph/layout/ordering.py`.
- Modify `src/task_graph/layout/model.py`: `SnapTask` gains
  `phase_order: int | None = None` (appended after `title`, so positional construction in
  existing tests is unaffected); `ContainerScope` gains
  `child_aggregates: dict[str, dict[str, int]] = field(default_factory=dict)`.
- Modify `src/task_graph/layout/engine.py:353-360`: the tidy seed key.
- Modify `src/task_graph/layout/driver.py`: `build_full_write_set` passes
  `child_aggregates={k: aggs[k] for k in kids if k in aggs}` into each `ContainerScope`
  (the aggregates computed at `driver.py:238`, i.e. before any `lay()` call).
- Modify `src/database/queries/layout_queries.py:load_project_snapshot`: one additional
  `task_metadata` read for `key == "phase"`, populating `SnapTask.phase_order`. It is the
  same shape as the existing `CONTAINER_KEY` read directly above it and adds one statement
  to the full-layout path only.

**Interfaces — produces**

```python
# ordering.py
NO_PHASE: int = 1 << 30   #: sorts after every real phase order

def activity_class(
    task: SnapTask, agg: Mapping[str, int] | None = None
) -> int:
    """0 = running, 1 = unfinished, 2 = finished. Lower sorts first.

    A container is classed by its subtree rollup when `agg` is given:
    agg["running"] > 0 -> 0; agg["active"] > 0 -> 1; else 2.
    A leaf (or a container with no aggregate) is classed by its own status
    against RUNNING_STATUSES / FINISHED_STATUSES.
    """

def tidy_seed_key(
    task: SnapTask, agg: Mapping[str, int] | None = None
) -> tuple[int, int, float, str]:
    """(phase_order or NO_PHASE, activity_class, created_at, id)."""
```

`engine.py:353-360` becomes
`key=lambda c: tidy_seed_key(scope.children[c], scope.child_aggregates.get(c))`.

**Tests — new `tests/task_graph/layout/test_ordering.py`**

- `test_activity_class_orders_running_before_open_before_finished` — every status in
  `RUNNING_STATUSES` and `FINISHED_STATUSES` plus `READY`/`BLOCKED`/`DEFINED`.
- `test_a_container_is_classed_by_its_subtree_not_its_own_status` — a `DEFINED` container
  with `running > 0` classes 0; a `COMPLETED` container with `active > 0` classes 1.
- `test_phase_order_outranks_activity` — phase 1 (finished) sorts before phase 2 (running).
- `test_a_non_phase_sibling_never_precedes_a_phase`.
- `test_equal_class_falls_back_to_created_at_then_id` — the key reduces exactly to today's
  `(created_at, id)`.
- `test_key_is_deterministic_under_shuffled_aggregate_dicts`.

**Tests — `tests/task_graph/layout/test_engine_tidy.py`**

- `test_tidy_puts_running_work_first_and_finished_last` — one rank of 6 siblings
  (2 finished, 3 ready, 1 running, interleaved `created_at`); after `mode="tidy"` the
  `rel_x` order is running, then the three ready in `created_at` order, then the two
  finished in `created_at` order.
- `test_tidy_output_is_unchanged_when_every_sibling_is_one_class` — the existing
  `test_tidy_untangles_a_reversed_ladder`, `test_tidy_pinned_bound_on_fixture` and
  `test_tidy_is_deterministic` **must pass unmodified**: every task in them is `READY`, so
  the key reduces to `(created_at, id)`. Add an explicit assertion that a 12-node all-READY
  fixture produces ordinals identical to those from the pre-change seed.

**Tests — `tests/task_graph/test_layout_driver.py`**

- `test_full_layout_seeds_containers_with_fresh_aggregates` — a project where an epic's
  aggregates were stale in the DB; the tidy classes it from the snapshot, not the stored row.
- `test_a_finished_epic_sorts_after_a_running_sibling_after_a_full_layout` — the end-to-end
  assertion, in both variants.
- `test_incremental_work_still_appends_between_tidies` — after the tidy, create a new
  sibling; it appends at the end of the rank and changes no existing ordinal (the
  first-slice limitation, pinned so it is a decision and not a surprise).

**Run** `aq test tests/task_graph/layout/ tests/task_graph/test_layout_driver.py
tests/task_graph/test_layout_queries.py` →
commit `feat(layout): tidy seeds siblings by phase then activity`.

---

### Acceptance scenario (run after all three)

3 phases; 6 epics across them; 80 leaf tasks of which 60 finished, 2 running, 18 open. In a
new `tests/task_graph/layout/test_reorganisation_acceptance.py` (no `perf` marker), after a
full layout of both variants assert:

1. **No scope's content is more than 6:1 in either direction** (today's 40-task epic is
   1:2.7, the 60-task one 1:4.0, the 120-task one 1:7.9).
2. **Every scope's allocated area is ≤ what today's engine allocates** for the same
   children.
3. **Phase order holds**: where gate edges exist, by `rank`; where they do not, by
   `order_key` within rank 0.
4. **In each rank, every `agg_running > 0` sibling has a lower `order_key` than every
   `agg_active == 0` sibling.**
5. **Idempotence**: two consecutive `full_layout` calls produce identical rows (G5).
6. **`active` holds at most 22 rows** (18 open + 2 running + context containers) — the
   §4.8 drop still doing its job.

### Order and parallelism

Task 1 → Task 3 (adjacent lines in `engine.py`). Task 2 is independent of both and can run
in parallel, but should land **last** so the auto-tidy it triggers rebuilds installs with
both changes present rather than only one. Acceptance last.

---

## 6. Second slice (deferred), with the reason each is not shippable as drafted

Each item below was in the first draft and is deferred by controller ruling, to be
re-scoped after the operator has looked at the result of the first slice. The corrections
are recorded so the second slice starts from an accurate account.

### 6.1 A `reorder` engine mode and its triggers

Drafted as: a new mode that re-keys one scope by activity, triggered when a container's
status enters the finished set, when a container is stubbed or dropped, and on Tidy.

Why it does not work as drafted:

- **It does not serve the goal.** "See what is being worked on" needs work to float when it
  **starts**. No dirty mark is written on `READY → IN_PROGRESS`
  (`task_queries.py:1130-1148`), so no pass runs and no trigger could fire.
- **In `active` the trigger is largely a no-op.** A container that settles with no
  unfinished descendant is *dropped* from the variant by `_visible` in the very same pass
  (`driver.py:131-138`), so there is nothing left to sink. It would only have an effect in
  `all`.
- **Two of the three triggers are the same mark.** "Container status finished" and
  "container entering `stubs`/`dropped`" are both driven by the one `status.finished` mark.
- **It would sort on stale aggregates.** `ContainerScope` carries none, and the
  incremental path refreshes them only after `_drain` (`driver.py:837-849`). The tidy path
  does not have this problem (§1.6), which is why §3.2 ships and this does not.
- If a third mode is ever added, the queue's mode precedence in `_drain`
  (`driver.py:740-757`, deepest-first, `incremental` short-circuits on `processed`) must be
  specified explicitly rather than inherited (review S7).

### 6.2 Deferred `reflow` marks for the `active` finished-leaf hole

Drafted as: write a low-priority `reflow` mark that the 900 s sweep drains.

Why it does not work as drafted: `clear_layout_dirty`
(`src/database/queries/layout_queries.py:163-168`) deletes **by `seq`**, with no reason
filter, so the very next incremental batch would delete the deferred marks before the
sweep ever saw them. It would also keep the project permanently listed by
`dirty_layout_projects()`, paying a snapshot load every 5 s cycle for work that never runs.
A second slice needs either a reason-aware clear or a separate table.

### 6.3 The pinned-position reset banner

Drafted as: after a Tidy or a rules rebuild, offer to clear `manual_positions`.

Why it does not work as drafted: the client cannot tell a rebuild from any other publish —
`layout_version` is bumped by `publish_layout` on every incremental pass
(`layout_queries.py:484`), which is several times a minute on an active project. The banner
would either never fire or fire constantly. A second slice needs a distinguishable signal
(for example the `layout_jobs` row surfaced through `extent`).

Until then, manual pins keep working exactly as they do today
(`LayoutCanvas.tsx:446`, `useGraphHierarchy.ts:247-252`) and are simply stale after a
rebuild. Note that §11 of the engine spec lists pinned nodes as out of scope while the
shipped canvas implements them; that disagreement is §7.5.

### 6.4 A phase rank floor

Drafted as: phases rank themselves through their gate edges.

That claim was **wrong** and is removed from §1.2 and §3.2. `phase_create` adds a `blocks`
edge only onto siblings that have **not** COMPLETED
(`src/commands/phase_commands.py:160-168`), so a phase created after its predecessors have
completed gets no edge and lands at rank 0 beside them. §3.2's `phase_order` term keeps
such phases in order *within* a rank; giving them a rank floor (`rank >= phase_order`) is a
layering change and belongs to the second slice.

### 6.5 A finer growth ladder

Not in the first draft; raised by the S5 analysis. See §7.3.

---

## 7. Open questions for the operator

1. **Should a finished-but-still-context stub sink, or stay put?** §3.2 sinks it (activity
   class 2) — carried from the previous ruling. The counter-argument is spatial memory: an
   epic whose position the operator has learned moves once, on the next Tidy.
2. **`ROW_ASPECT = 1.3`** targets a mildly landscape scope. On an ultrawide monitor 2.0
   would be better; on a laptop 1.0. It is a persisted, shared value — it cannot be
   per-viewer. Confirm 1.3, or name a number.
3. **A finer growth ladder.** Because `GROWTH_BANDS` doubles, the aspect ratio snaps
   coarsely: the 60-card epic lands at 23.05 × 3.99 rather than the ~2:1 a finer ladder
   would give. A ×1.4 step above 12 fixes the shape and costs roughly 2.4× more band
   crossings — i.e. more translation publishes, the cost `test_root_band_crossing_publish_under_1s`
   measures. Worth it?
4. **Is a five-hour silent convergence acceptable?** One `(project, variant)` per 900 s
   sweep. The alternative is an explicit `aq graph tidy --all` the operator runs once and
   watches.
5. **Manual pins.** The engine spec says out of scope; the canvas implements them; after a
   rebuild they are stale. Keep them as they are (this slice), build §6.3's banner, or
   retire pins now that the server owns geometry?
6. **Ordering between tidies.** The first slice reorders only when a Tidy runs. If the
   operator wants running work to float the moment it starts, that needs a layout dirty
   mark on the start transition plus §6.1 — a bigger change, and one that moves cards under
   the pointer. Confirm that Tidy-time ordering is enough to judge by.
