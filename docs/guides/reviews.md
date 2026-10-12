# Document reviews

AQ has a review surface for documents — specs, plans, design proposals and
research reports — that lives in
the vault, is decided by Jack, and gates the work that depends on it. A
document is a **review**, not a commit: the worker writes it in its checkout
without committing it, submits it with `aq review submit`, and closes its
task. The daemon stores the text in the database and mirrors it to a vault
file you can read and edit in Obsidian. Approval is a decision on a revision
(`aq review decide`), and any task filed with `--after-review <id>` waits on
the review's gate until you approve.

This page is the operator's guide. The design that defines every word on it
is [the document review design spec](../../superpowers/specs/2026-09-21-document-review-design.md).

## What you get

- **The vault holds the live document.** Every review owns a vault file —
  `projects/<pid>/specs/<date>-<slug>.md` for kinds `spec` and `other`, or
  `projects/<pid>/plans/<date>-<slug>.md` for kind `plan`. The database is the source of truth; the file is an
  atomic copy rewritten whenever the review changes, and re-created if
  missing.
- **The database holds the review.** One row with its state, kind, title,
  revision, current content, authoring task, and the principal allowed to
  decide (`decider`). Every revision keeps its full text, so the dashboard
  can show "changes since the previous revision" and `aq review show
  --revision N` can print an old one.
- **A gate.** An open gate row the scheduler refuses to route anything
  through, linked to the review by `gate_id`, plus a `review_gate` context
  row on the task.

## The lifecycle

```
                  submit
   (no review) ───────────────► in_review
                                 ▲  │
              submit --review-id │  │ decide approve
                                 │  ▼
                          changes_requested ───► approved
                                 │
                                 │ withdraw
                                 ▼
                             withdrawn
```

- `in_review` — waiting for a decision on the current revision.
- `changes_requested` — a decision with feedback; the daemon files one new
  revision task carrying that feedback (§ What happens when you reject).
- `approved` — the gate is resolved and dependent tasks become routable.
  For kind `spec` or `plan`, approval also sets the vault status to `approved`
  and emits `spec.approved`, starting one deep-high spec-ingest task per path
  (`spec-ingest:<absolute vault path>`). Other documents do not ingest.
- `withdrawn` — the review's gate is cancelled in the same transaction. Its
  dependent tasks remain held for approval and are flagged for attention;
  their IDs appear in the response and review detail and are sent to the
  project supervisor. The dashboard asks for confirmation before withdrawing.
  The review records the withdrawal actor, surface, timestamp and reason.
- `aq review reopen --review-id <id> --revision N` reopens a withdrawn review
  as revision N+1, copying its last submitted content and any playbook pin.
  It reopens the same gate and keeps all dependent links. Approval is still
  required to release work. Withdrawal audit fields survive reopening.
  Use `aq review submit --review-id <id> --file <draft.md>` afterward to revise
  the text. An approved review cannot be reopened. The dashboard offers the
  same action on the withdrawn review's detail page. Only the author task,
  project supervisor or local operator may reopen it; the displayed revision
  must still be current. Earlier withdrawal events retain the full history.

Every transition checks the review's state and the revision it was issued
for, so two decisions racing on different revisions cannot both succeed.

## Design and implementation documents

Use `spec_kind: design | implementation` in the submitted YAML frontmatter;
this is separate from review kind `spec | plan | other`. The classification
is stored on each revision and survives vault status rewrites and recovery.
A body-only revision retains its previous classification. Invalid values
are refused. Legacy documents may omit it; the ingestion agent checks their
content before deciding whether they are ready for implementation.

A design spec approves goals and architecture. Its ingest creates one epic
containing one deep-high design task to write an implementation spec grounded
in the current repository: files, functions, tests, rollout and chosen defaults.
That task submits the implementation document to Jack's review queue, with
`spec_kind: implementation`; approval starts ingestion again. Design approval
creates no implementation work.

An implementation spec creates phase/deliverable epics with self-contained
children, maximizing safe parallelism by file/module ownership. Dependencies
connect children only; structural parent-child edges connect children to epics.
Spec ingestion validates and commits the whole graph atomically without a
human proposal gate. Ordinary task-batch proposals retain their approval gate.
Ingest authority only creates work; existing-task updates go to the supervisor
as an ordinary, human-approved change set.

Start from the [spec template](../templates/spec.md). On an existing install,
the operator must reconcile the shipped spec-ingest Role and commit grant;
profile seeding does not overwrite local copies. Preserve the installed
harness when updating it.

## The commands in one table

| Command | You | What |
|---|---|---|
| `aq review list` | yes | Everything that exists, newest first. |
| `aq review show --review-id <id> [--revision N] [--comments]` | yes | Print the current (or a given) revision, and with `--comments` the whole comment thread with each comment's quote, heading path, revision, and resolved state. |
| `aq review attach --review-id <id> --revision N --file <image> --caption "…" --view-id <id> --candidate-id <id>` | yes | Append a verified screenshot to the current open revision. |
| `aq review attachments --review-id <id> --revision N` | yes | List one revision's image metadata and download URLs. |
| `aq review download-attachment --review-id <id> --revision N --attachment-id <id> --output <path>` | yes | Download an image after the same packet access check as the API. |
| `aq review decide --review-id <id> --revision N --decision approve --note "…"` | yes | Approve. Resolves the gate, releases dependent tasks, rewrites the vault file. |
| `aq review decide --review-id <id> --revision N --decision request_changes --note "…" [--responder-class <class>]` | yes | Reject the revision and file a new revision task with the note as feedback. The revision task is routed by the project's router; `--responder-class` is its class hint. A responder profile is refused (`routing.choice_forbidden`). |
| `aq review comment --review-id <id> --revision N --body "…" [--quote "…"] [--heading-path "…"]` | yes | Add one anchored comment without deciding. The author sees it in the thread the next time they `aq review show --comments`. |
| `aq review dispatch --review-id <id> [--count N] [--class <class>] [--revision N] [--no-comments] [--focus "…"] [--force]` | yes | Send one fixed revision (the current one by default) to `--count` adversarial reviewer tasks (default 1). Each is filed unrouted with the `--class` hint (default `deep-high`) and the constraint `exclude_providers: [<provider the author revision ran on>]`, so the project's router picks each reviewer's profile on another family. A dispatch never names a profile; `--to` is refused (`routing.choice_forbidden`). The default `--with-comments` includes prior comments; `--no-comments` gives a clean read. A second dispatch of the same revision needs `--force`. |
| `aq review withdraw --review-id <id> --reason "…"` | yes | Cancel the review gate, flag dependent tasks for attention and notify the supervisor. Actor, surface, time and reason are recorded. Other callers also notify the author task. The dashboard requires confirmation. |
| `aq review reopen --review-id <id> --revision N` | yes | Copy the current withdrawn revision into N+1 and reopen the same approval gate. Also available to the author task and project supervisor. |
| `aq review delegate --review-id <id> --to supervisor\|user` | yes (`local_operator_only`) | Change `decider` on one review. `supervisor` lets the live named supervisor session approve it. |
| `aq review import-edits --review-id <id>` | yes (`local_operator_only`) | Turn your out-of-band Obsidian edit into revision N+1. Refuses if the current revision is already decided. |

For workers and the supervisor, plus the `aq task create … --after-review
<id>` and `aq task edit … --after-review <id>` the daemon uses to gate work,
see [§ The worker's side](#the-workers-side).

## Download a document

Open a document in **Reviews**, select its revision, and choose **Download
Markdown** in the revision toolbar. This saves the selected revision's raw
Markdown as UTF-8, including frontmatter and original line breaks. It works
for specs, plans and other review documents, including historical and closed
reviews. Showing a diff does not change the downloaded content. The filename
uses the review title (or its ID) and revision number with a `.md` extension.
The action is unavailable while the selected content is loading.

## Screenshot evidence

Submit the review text first, then attach screenshots to its current revision
before a decision or reviewer dispatch. The Reviews pane has the same upload
form beside the document and shows the images for the selected revision. Each
image records a SHA-256 hash, MIME type, byte count, caption, view ID and
candidate ID. Upload accepts PNG, JPEG, GIF and WebP, verifies the claimed
format, and enforces a 10 MiB cap. There is no replace or delete operation.
A dispatched packet is frozen; a new revision gets its own images and cannot
change an earlier revision's set.

Images live under the daemon's `data_dir/review-attachments`, with database
rows keyed to the review revision. Removing an author task or its worktree
does not remove review evidence. `review_show` and the Reviews pane list only
the selected revision's images. The API command endpoints
`review_attachment_add` and `review_attachment_list` use the same command
scope as the CLI; binary downloads use
`GET /api/reviews/{review_id}/revisions/{revision}/attachments/{attachment_id}`.
The download checks the stored hash before serving bytes. A dispatched
reviewer can read only the review and revision named by its live task's
dispatch record; author and revision workers can upload to their own current
packet.

## Who may decide

Only two principals may approve or request changes: **you** (the local
operator, whether you act from the dashboard or a local CLI) and, if you
delegated the review, the live named supervisor session for that project.
Dispatch gives a worker permission to comment only on the review and revision
recorded for its held task; it never gives that worker decision authority.

| Action | You (dashboard or local CLI) | Supervisor | Other agents |
|---|---|---|---|
| `submit`, `show`, `list` | yes | yes | yes (`submit` only from their own task) |
| `withdraw` | yes | yes | the authoring task only |
| `decide` | yes | **only when `decider` is `user_or_supervisor`** | no |
| `comment` | yes | **only when `decider` is `user_or_supervisor`** | dispatched reviewer on its pinned revision |
| `dispatch` | yes | yes | no |
| `delegate`, `import-edits` | yes | no | no |

A delegated decision is recorded as `decided_by: "supervisor (delegated)"`
plus the session id, so the audit trail always shows who actually pressed the
button.

Dispatch leaves the review state, decider, and gate unchanged. `aq review show`
lists each dispatch's class hint, pinned revision, comment mode, task id, and task
state. The reviewer reads the exact revision from its task instructions,
anchors each finding with `--quote` or `--heading-path`, then closes its task
with a verdict summary. The operator still decides the gate.

**Delegation, set once per project.** You can delegate all new reviews in a
project to the supervisor with `aq project set <project-id> review-delegate-to supervisor`
(revert with `aq project set <project-id> review-delegate-to user`, the default). That writes the
nullable `projects.review_delegate_to` column (CHECK `user` | `supervisor`)
and sets the initial `decider` of every new review in that project. Only a
local operator may change it; the elevated supervisor is refused
(`local_operator_only`) so it cannot grant itself the setting.

## Edits outside the review

You can edit the vault file directly in Obsidian at any time. On every read
the daemon compares the body hash (frontmatter excluded) with the current
revision's `content_sha256`. If it differs:

- the dashboard shows a banner, *"This file was edited outside the review"*,
  with an **Import my edits** button;
- `aq review decide` is refused with code `vault_diverged`, so you cannot
  approve text that is not the current revision;
- `aq review import-edits --review-id <id>` promotes your edit to revision
  N+1, authored by `jack`; and
- the daemon never overwrites a diverged file. If the file is simply
  missing, `import-edits` rewrites it from the current revision.

## Playbook reviews

A review that asks approval for a Playbook V2 policy pins the compiled
artifact to the revision, so an approval always ends in something that can be
activated. (Before this, approving a playbook review left only prose, and the
artifact had to be compiled and imported by hand afterwards.)

- **Submitting.** The author writes the playbook's Markdown source into the
  vault, then submits the review document with the playbook attached:
  `aq review submit --task-id <task> --file <draft.md> --kind other --title "…" --playbook-id <id> --playbook-body <body.json> [--activate-on-approval]`.
  The body is JSON with exactly the proposal's `rules` and `steps`
  (`semantic_body_path` names a vault file instead). The daemon compiles the
  vault source with it, exactly as `aq playbook v2-propose` does, and refuses
  the submission — `playbook_invalid` with the diagnostics — unless the
  artifact is activatable. A playbook review is always kind `other`: a spec
  approval would also start spec ingest.
- **What is pinned.** The revision stores the artifact's canonical bytes and
  its metadata: `playbook_id`, `artifact_sha256`, `source_sha256`, scope and
  `activate_on_approval` (`aq review show` prints the metadata; the vault
  file's frontmatter names the playbook and the hash). Nothing reaches the
  artifact store before approval, so an unapproved artifact cannot be
  activated.
- **Revising.** Every revision pins again. `--playbook-body` sends a new body;
  without it the previous artifact's rules and steps are recompiled against
  the current source. `activate_on_approval` carries forward unless the
  revision sets it. `import-edits` keeps the pin: the artifact comes from the
  source, not from the document's prose.
- **Approving.** After the approval commits, the daemon stores exactly the
  pinned bytes, revalidated against the live registries as an import is. If
  the review asked for it, it then activates that hash as the decider (the
  usual `playbook_activate` checks apply). The supervisor's approval notice,
  and the decision's `playbook` result, say what happened: stored and
  activated, stored with the `aq playbook activate --playbook-id <id> --artifact-sha256 <hash>` command to run, or not
  stored and why. A failure never undoes the approval.
- **Checking.** `aq doctor --check reviews.playbook_artifacts` lists the most
  recent approved review of each playbook whose artifact is `not_stored`
  (`--fix` stores it, never activates) or `not_activated`. A review approved
  before revisions could pin an artifact names none and is not listed.

A reviewed bundle that is already in the vault — a shipped one, or one
compiled by hand — is imported with
`aq playbook import --path reviewed-playbooks/<id> [--activate]`.

## What happens when you reject

Rejection files new work; it never reopens the author. Every `request_changes`
creates **one new revision task**, whatever state the authoring task is in:

- **Title and place.** *"Revise \<title> (review \<id>)"*, under the authoring
  task's parent — live or archived — or at the project root when there is no
  authoring task or its parent is gone.
- **Deduplicated per revision.** The task carries the dedup key
  `review-revision:<review>:<revision>`, so one decision on one revision files
  one task, however often the hook runs.
- **Route.** The revision task is filed unrouted and the project's router
  routes it (mandatory routing). `--responder-class` on the decision is its
  class hint; a responder profile is refused. `aq review show` previews the
  route before you decide (`response_route`). The task is `class_only`, never
  `pinned`, so the revision can fail over to the same class on another
  provider.
- **Description.** Your note, the route line, every unresolved anchored
  comment with its id and anchor, and the `aq review submit --review-id … --resolves …`
  command to answer with.
- **Who may answer.** While the review is `changes_requested`, only the
  session holding that revision task may resubmit; any other worker gets
  `not_your_task`. An authoring task still `in_review` (its worker resubmitting
  before it closes) may keep revising its own draft.

## The worker's side

From the worker's point of view the workflow is short:

1. **Write** the document in the checkout, do not commit it.
2. **Submit** with `aq review submit --task-id <task> --file <draft> --kind
   spec\|plan\|other --title "…"`. The CLI reads the file locally and sends
   the content; the daemon returns the review id and the vault path.
3. **Close** the task as pass with the review id and vault path in the
   summary. The worker does not wait for the decision; the work that depends
   on it waits (via `--after-review`). A whole build graph takes it too:
   `aq task create --from-spec <path> --after-review <id>` (or `--graph`)
   attaches every node to the review's gate in the graph's own transaction.

   The close gate enforces the submit. A task that declares a `review`
   deliverable (`--deliverable '{"id":"proposal","kind":"review","target":"spec"}'`;
   target `spec`, `plan`, `other` or `any`) is refused a passing close with
   `deliverables.unmet` until it has authored a review, or submitted a
   revision of one, that is not withdrawn. Every `research` or `design` task
   carries such an item — `{"id": "review", "kind": "review", "target":
   "any"}` — whether or not its author declared one, read from the task's
   type at close time. Prime lists it under **Deliverables** with the submit
   command. A task that honestly produced no document waives it visibly with
   `--deliverable-unmet 'review: <reason>'`, and a dispatched adversarial
   reviewer (research-typed, but answering with comments) is exempt. This is
   what keeps a proposal from finishing on a branch nobody reads: research
   task `crisp-orbit-37` committed its design proposal, closed as pass, and
   never reached the Reviews tab until the supervisor submitted it by hand.
4. **Revise** if you hold a revision task (*"Revise \<title> (review \<id>)"*):
   `aq review show --review-id <id> --comments`, then resubmit with
   `aq review submit --review-id <id> --file <draft> --changes "…"
   [--resolves <comment-id> …]`, and close it.

This is wired into every shipped worker profile's `## Rules` section as
*Documents go to review, not the repo*, and the shipped supervisor profile
mirrors the operator side (§ Who may decide above) and tells the supervisor
to file document-returning work as `research`/`design` or with a `review`
deliverable, never with "commit the document". Shipped profiles are
write-if-absent, so an existing install picks the new wording up only after
the operator refreshes its vault copies. The rule
replaces the superpowers default of committing specs to
`docs/superpowers/specs/`.

Implementers read an approved document with `aq review show --review-id
<id>`; task descriptions cite the review id, not a file path.

## Adversarial review across model families

One model family writes a document, a different family attacks it, and the
two go round for a few revisions — say Fable writing and Astra attacking.
Every verb this needs already exists, so it is a recipe, not a feature. It is
**opt-in**: the [software-factory policy](../concepts/factory-policy.md) rules
out mandatory multi-pass review chains, so run it on a document that warrants
it, never as a default. The design is review `rev-calm-meadow`.

**Who runs it.** You, or a supervisor you delegated the review to. Dispatch is
refused as `operator_only` to anything but the local operator or an elevated
session, and only the decider may request changes or approve. Workers play the
author and the reviewer; they never dispatch or decide.

### Rung names come from the installed ladder

"Fable" and "Astra" are model families, not profile ids. Read the rungs off
the install before reading a route:

```bash
aq system list-intelligence-classes
aq agent list-profiles
```

On the shipped ladder Fable is the `anthropic` slice of `deep-high`
(`claude-fable-5`, rung `deep-high-claude`), and Astra (`gpt-6-astra`) is the
OpenAI-only class `astra-high` on rung `astra-high-codex`; `deep-high-codex`
runs `gpt-5.6-sol`. An install can differ: where `deep-high`'s OpenAI slice is
`gpt-6-astra`, Astra *is* `deep-high-codex`. Nobody picks those rungs: the
project's router does, from a task's kind and class hint, so the loop steers
families through kinds and the reviewers' provider exclusion. The examples
below assume that install — a code-design author routed to `deep-high-claude`
(Fable), and reviewers, which exclude Anthropic, routed to `deep-high-codex`
(Astra). On the shipped ladder that reviewer runs `gpt-5.6-sol`; record the
model that actually ran.

### Family diversity is observed, not configured

No flag guarantees which family runs a turn:

- `aq review dispatch` names no profile. Each reviewer task excludes the
  provider the author revision was observed on, and the router picks among the
  other families; it is `class_only`, so it can fail over.
- `aq review decide --decision request_changes` files the revision task
  unrouted with the class hint, `class_only` — never `pinned`.
- The author task carries a kind, not a profile: `--type design` routes it to
  the code-design lane (Claude first, then Codex) and `--type art` to the
  art-design lane (Codex, held). Only the local operator or a live supervisor
  session may pin it to one family, with `aq task route-override` on a human's
  word; nothing pins the later reviewer or revision tasks.
- Failover inside one class crosses families. During an Anthropic outage a
  task on `deep-high-claude` can be re-routed to `deep-high-codex`, which on
  the install above is Astra — author and reviewer on one family, with no
  profile name changing. An OpenAI outage does the reverse.

So a round's cross-family claim comes from what actually ran. For every
writing and reviewing task in the round:

```bash
aq task show <task-id>                         # route, provider_intent, rerouted_from
aq task recent-activity --hours 48 --json      # items[].attempts[].model: what really ran
```

If the required family was unavailable, or any material writing or review
turn failed over or reports no model, label the round **inconclusive for
cross-family coverage** in your round record. Do not report coverage you did
not observe. You can wait for the provider to recover and dispatch the same
revision again deliberately (see `--force` below). Dispatch and decide take no
profile and no pin: the reviewers' provider exclusion is the only family
control they carry.

### Before the first round

File the author task with the kind whose lane is the author's family, never a
profile. On the shipped routing policy `--type design` (code design) runs at
`deep-high` on Claude first and falls back to Codex, and `--type art`
(art-heavy design) runs on Codex and holds for it:

```bash
aq task create --project <project> --title "Write spec: <topic>" --description "<brief>" --type design
```

Check the route the router wrote (`aq task show <author-task>`). If the human
requires a family the router did not pick, you or the supervisor may pin that
one task on the human's word with `aq task route-override --task-id
<author-task> --profile-id <profile> --reason "..."`; otherwise take the family
that ran and record it.

Then choose the review, a cap of at most **three critique rounds**, and a
deadline (default: two working days). Record them, with the author's observed
family, as a comment on the author task, and keep appending each round's result
there:

```bash
aq task comment <author-task> --body "Adversarial review plan: author on deep-high-claude (routed, Fable), reviewers exclude its provider, cap 3 rounds, deadline <date>."
```

The revision number is **not** the round counter. An imported vault edit or
an author re-draft moves it without a round, and a replaced round dispatches
the same revision twice. Count rounds in your record.

### One round

1. **Author.** Before round 1 only, the author task filed above writes the
   draft. It submits with `aq review submit --task-id <task> --file <draft>
   --kind spec --title "…"` and closes with the review id and the revision's
   content hash (`revision.content_sha256` in `aq review show --review-id <id>
   --json`) in its summary. Closing the task approves nothing.
2. **Dispatch the current revision.** Read the revision from `aq review show
   --review-id <id>` and dispatch that explicit number to the other family.
   Round 1 is a clean-room read (`--no-comments`); later rounds keep the
   default `--with-comments`, so the reviewer sees earlier findings and the
   author's dispositions.

   ```bash
   aq review dispatch --review-id <id> --revision <n> --class deep-high --no-comments --focus "Verify claims against code; label each finding [blocking] or [nit]"
   ```

3. **Wait for the reviewer task to end.** It is an ordinary task:
   `aq task show <task-id>`, and `aq review show` lists every dispatch with its
   task state. A failed or cancelled reviewer is an **incomplete review, not
   "no findings"**. Check attribution (above), then read the anchored findings
   and the verdict in the reviewer's close summary:

   ```bash
   aq review show --review-id <id> --revision <n> --comments
   ```

4. **Request changes** for material defects, on that exact current revision,
   naming the responder class (a hint; the router routes the revision task):

   ```bash
   aq review decide --review-id <id> --revision <n> --decision request_changes --note "Round 1 of 3: address or explicitly rebut every [blocking] finding" --responder-class deep-high
   ```

   The daemon files the revision task ([What happens when you
   reject](#what-happens-when-you-reject)); find it in `aq task list --project
   <project>` by its title. When it has resubmitted, check its attribution and
   record in the round comment every finding's disposition and the new
   revision's content hash.
5. **Stop** when no unresolved `[blocking]` finding remains **and** the
   decider judges the acceptance criteria adequate — then approve as usual. Or
   stop at the round cap or the deadline: leave the review `in_review` or
   `changes_requested` and record the unresolved findings in the round
   comment. **Never approve because the cap was reached.** Only you, or a
   supervisor you delegated the review to, decides the gate.

### Rules that keep the loop honest

- **No routine `--force`.** `duplicate_dispatch` for the same revision means
  the dispatch already exists — deduplication working. Pass `--force` only to
  deliberately replace a failed or inconclusive round, and record why.
- **`stale_revision` means re-read.** The review moved: `aq review show`, then
  act on the current revision. Never change the number blindly.
- **`vault_diverged` means someone edited the vault file.** Promote the edit
  with `aq review import-edits --review-id <id>` (local operator), or undo it.
  The recipe never overwrites a diverged mirror.
- **Delegation is explicit.** `aq review delegate` is local-operator only and
  optional. A delegated supervisor may decide; a worker or a playbook may not
  assume it has been delegated.
- **Fresh eyes first.** Use a fresh reviewer context for the first pass where
  practical. Later rounds review the whole revised document plus the prior
  dispositions, not only the diff (`--diff-from` is an aid, not the scope).
- **Swap families between documents, not mid-round.** Changing who authors or
  reviews mid-round needs a recorded new assignment.
- **Three rounds is a cap, not a quota.**

### Findings: `[blocking]` and `[nit]`

A dispatched reviewer must anchor every comment with `--quote` or
`--heading-path` (else `anchor_required`), and each comment begins with its
severity:

- `[blocking]` — a contradiction, a missing invariant, an unsafe failure path,
  a claim the code does not support, or an acceptance criterion that cannot be
  tested.
- `[nit]` — wording or readability.

The body gives the claim, the evidence (`path:symbol` and the revision), a
concrete failure scenario, and the correction asked for or a viable
alternative:

```bash
aq review comment --review-id <id> --revision 2 --quote "the daemon reopens the authoring task" --body "[blocking] Claim: rejection reopens the author. Evidence: src/commands/review_commands.py:_on_review_changes_requested (rev 2) always files a new task keyed review-revision:<review>:<revision>. Failure: the operator waits for a reopen that never comes. Fix: describe the new revision task."
```

No one has to manufacture a defect. A verdict of no material defects is valid
when it names what was checked. Severity lives in the comment text; there is
no severity column.

### Dispositions: `fixed`, `rebutted`, `deferred`

The revision task answers every finding in its `--changes` note, one entry per
comment: the comment id, the disposition, the rationale and the evidence.

```bash
aq review submit --review-id <id> --file <draft> --changes "cmt-1a2b fixed: section 4 now describes the new revision task (review_commands.py:_on_review_changes_requested). cmt-3c4d rebutted: the dedup key is per revision, so a repeated hook files nothing (find_task_by_dedup_key). cmt-5e6f deferred: section 5 now excludes pinning and its acceptance list says so." --resolves cmt-1a2b --resolves cmt-3c4d --resolves cmt-5e6f
```

- `fixed` — the document changed; say where.
- `rebutted` — the finding is wrong; show the evidence. `--resolves` marks the
  comment resolved in the new revision, but that is **not acceptance**: the
  decider checks every rebuttal, and the next round's reviewer sees it.
- `deferred` — for a `[blocking]` finding, only when the document itself now
  records the exclusion and updates its acceptance criteria. An unrecorded
  deferral is an unresolved finding.

The same two severities and three dispositions are the shared vocabulary for
critique elsewhere (mini-projects reuse it); outside a dispatched review they
go in ordinary artifacts such as task comments, not review comments.

### A worked read-through

Two rounds on one spec, as the round comment on the author task would record
them:

| Round | Revision | What happened | Record |
|---|---|---|---|
| 1 | 1 | Dispatched clean-room to `deep-high-codex`; attempts report `gpt-6-astra`. Two `[blocking]`, one `[nit]`. Request changes with `--responder-class deep-high`; the revision task's attempts report a Fable model and it resubmits revision 2: `fixed`, `rebutted`, `fixed`. | Cross-family. Hash of revision 2. |
| 2 | 2 | Dispatched with comments to `deep-high-codex`; during an OpenAI outage the reviewer task was re-routed and its attempts report a Claude model. | **Inconclusive** for cross-family coverage; the round does not count as a pass. |
| 2 (replaced) | 2 | After recovery, dispatched again with `--force` ("replacing inconclusive round 2"); attempts report `gpt-6-astra`. No `[blocking]` and the rebuttal is not re-raised; the verdict names the sections and code paths checked. | Cross-family. The decider checks the rebuttal and approves revision 2. |

Had round 3 still left a `[blocking]` finding open, the loop would stop there:
the review stays open, and the round comment lists what is unresolved.

### Why this is not a playbook

The loop stays a recipe because a playbook cannot drive it today:

- `review_dispatch`, `review_show` and `review_decide` are not contracted
  commands (nothing in `src/commands/contracts/`), so a V2 `command` step
  cannot call them.
- An `agent_task` step can start a reviewer, but that reviewer is not in the
  dispatch ledger, so its `aq review comment` is refused `not_dispatched`.
- `review.dispatched` is emitted but has no event schema, so a rule triggered
  on it fails validation with `unknown_event`.
- A playbook principal is not a permitted decider.

The outline below is **not runnable**. It shows the shape a future executable
loop would have if operators ask for one; contracting the verbs is that later
feature.

```text
start:   operator or delegated supervisor opens round k on review R (manual)
step 1:  dispatch R at revision n to the reviewer family
step 2:  wait for the dispatch task to end; read attempt models
step 3:  inconclusive or failed round -> wait for recovery, replace it (--force)
step 4:  blocking findings -> request_changes on R at revision n, responder named
step 5:  wait for the revision task's resubmission; record dispositions and hash
step 6:  k < cap and before the deadline -> step 1 at revision n+1
         otherwise stop and record the unresolved list
never:   approve automatically, count rounds by revision number
```

## The refusal codes you will see

Every refusal from the review API carries a named code. The ones you are
most likely to meet:

| Code | Meaning | Fix |
|---|---|---|
| `not_your_task` | The reviewer (worker or supervisor) tried to act on a review in a project they do not hold. | Re-run from the session that holds the task, or have the authoring task resubmit. |
| `not_in_review` | A decision was attempted while the review was not in a state that accepts it (or the `--decision` value was not `approve` / `request_changes`). | `aq review show --review-id <id>` to see the actual state, then `withdraw` or wait. |
| `stale_revision` | `--revision` does not match the review's current revision. | `aq review show --review-id <id>` to read the latest, then decide or comment with that revision. |
| `review_closed` | A `submit --review-id` was called after the review had been approved or withdrawn. | Reopen a withdrawn review with `aq review reopen --review-id <id> --revision N`, then revise it. An approved review requires a new review. |
| `empty` | The file was empty after decoding. | Fill in the draft and retry. |
| `not_utf8` | The file was not valid UTF-8 text. | Convert the source file and retry. |
| `bad_title` / `bad_kind` | `--title` was missing or too short, or `--kind` was not one of `spec`, `plan`, `other`. | Re-run with a sensible title and a known kind. |
| `not_found` | The id does not exist under your principal's scope. | `aq review list` to find the right id. |
| `not_decider` | You tried to `decide` or `comment` on a review whose `decider` does not include your principal. | `aq review delegate --review-id <id> --to user` (if you are Jack) or wait for the supervisor to accept the delegation. |
| `not_dispatched` / `wrong_revision` | A worker tried to comment without holding the matching dispatch task, or on a different revision. | Read the pinned revision in the task description and comment from that task's session. |
| `anchor_required` | A dispatched reviewer commented without `--quote` or `--heading-path`. | Anchor the finding to the text or section it is about. |
| `duplicate_dispatch` | This revision has already been dispatched. | Inspect `aq review show`, or pass `--force` to dispatch more reviewers. |
| `routing.choice_forbidden` | A profile or pin on a filing (`aq task create`), a `--to <profile>` on a dispatch, or a responder profile on a decision: the router routes every task. | Give hints instead: `--type`/`--intelligence-class` to a filing, `--count`/`--class` to dispatch, `--responder-class` to decide. |
| `operator_only` | `aq review dispatch` from a session that is neither the local operator nor elevated. | Dispatch as the local operator or from the supervisor. |
| `invalid_responder` / `invalid_responder_class` | Responder routing on an approval, or an unknown class. | `aq system list-intelligence-classes`, then name a class that exists. |
| `local_operator_only` | A session-scoped principal tried to `delegate` or `import-edits`, or the supervisor tried to set `review_delegate_to`. | Re-run the command as the local operator. |
| `vault_diverged` | The vault file no longer matches the current revision's hash. | `aq review import-edits --review-id <id>` to promote your Obsidian edits, then decide. |
| `review_gate` | The task you filed with `--after-review <id>` hit the open gate and the scheduler refused to route it. | Approve the review, or `aq review withdraw --review-id <id> --reason "…"`. |

## Troubleshooting

Use the consistency check first — `aq doctor --check reviews.consistency` —
because it names the specific repair for every mismatch between the database
and the vault:

- A missing vault file: the check will rewrite it from the current revision
  on `aq doctor --check reviews.consistency --fix`.
- A diverged vault file: the check reports it, never overwrites. Use
  `aq review import-edits` if the edit is yours, or `aq review show
  --review-id <id>` to copy the approved text back into the file.
- A resolved gate on a non-approved review, or an open gate on an approved
  review: the check reports it and `--fix` repairs the safe direction
  (resolving the gate on an approved review, or flagging the unresolved
  case for you to unwind).
- A legacy open gate on a withdrawn review: the check reports its waiters;
  reopen the same review with `aq review reopen --review-id <id> --revision N`
  to preserve its dependent links and submit it for approval again. If the
  dependent work is obsolete, the operator or supervisor decides its fate.

## Discord, and what reviews are not

Review submission and revision are announced to the single configured
Discord channel (`config.discord.channel_id`) with the title, kind,
revision, "what changed" note, and a deep link to the review. A revision is
posted at most once — `doc_reviews.notified_revision` tracks the last
announced revision — and the transport retries are fire-and-forget: a
failed send never blocks or fails a submission. Decision is not taken in
Discord.

Reviews are not merges, not gates on *code* changes, and not a replacement
for the escalation flow. A review gates *documents*; the code that
implements an approved document is ordinary work, subject to ordinary
review the same way anything else is. The design spec's out-of-scope list
(§13) is the authoritative boundary if you are tempted to extend reviews to
something else.

## Related pages

- [The document review design spec](../../superpowers/specs/2026-09-21-document-review-design.md)
  — the source for every claim on this page.
- [The `aq-reviews` skill](../../src/skills/aq-reviews/SKILL.md) — the
  agent-facing version of this page: the author, the dispatched reviewer and
  the operator's side of the cross-family recipe.
- [Quick mini-project ideation](mini-projects.md) — an explicitly requested
  facilitator session that produces one plan for this review surface.
- [Provider failover](../specs/provider-failover.md) — why a routed task can
  change provider among its route candidates, and where attempt attribution is
  recorded.
- [Escalations and the hourly digest](escalations.md) — the other "human
  in the loop" surface; reviews share its local-operator-vs-supervisor
  principal model.
- [Worker pools](worker-pools.md) — for the pool-vs-task distinction that
  decides whether a worker's rejection-respawn is a pool respawn or a fresh
  assignment.
- [Software-factory policy](../concepts/factory-policy.md) — the broader
  "one decision surface per change" principle that reviews implement.

## Source and tests

`src/reviews/` (service, diff, vault), `src/commands/review_commands.py`
(API handlers and refusal codes), `src/cli/reviews.py` (the hand-written
submit path; every other `aq review …` command is generated from
`src/tools/definitions.py`), `src/models/` (the review and gate rows),
`src/scheduler/` (gate enforcement on routing), `src/deliverables.py` and
`_cmd_task_close` in `src/commands/session_commands.py` (the `review`
deliverable and its close gate). Tests live in
`tests/test_review_profile_rules.py` for the profile rule invariants,
`tests/test_task_close_summary_enforcement.py` and `tests/test_deliverables.py`
for the close gate, and
under `tests/` for the service, state machine, permissions, and vault
behaviours — see `tests/` for the full set.
