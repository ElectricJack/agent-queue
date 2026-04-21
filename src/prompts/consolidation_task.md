# Memory Consolidation — Project: {project_name}

You are performing a nightly consolidation pass over the auto-extracted
insights for project `{project_id}`. The goal: fewer, better-deduped,
cross-linked memories, with stable facts promoted from `insights/` into the
curated `knowledge/` subdirectory.

## Context

- Project id: `{project_id}`
- Insights directory: `{insights_dir}`
- Knowledge directory: `{knowledge_dir}` (create if it does not exist)
- Last consolidated: `{last_consolidated}`
- Insights added/updated since then: `{churn_count}`

Budget: prioritize high-churn topics first. Stop cleanly if you run out of
context; partial progress is fine — the next nightly run continues the work.

## How you work

Edit the markdown files in the vault directly. Do **not** reach for the
`memory_*` MCP commands — they're a thin wrapper around Milvus that will
drift from whatever you do to the files, and the vault watcher re-indexes
what you leave behind. Your toolkit:

- `Glob` / `Bash ls` to enumerate insight files.
- `Read` to inspect a file's frontmatter + body.
- `Edit` (or `Write` for a full rewrite) to change content, tags, topic,
  or append a `## See Also` section.
- `Bash rm` to delete garbage or non-canonical sibling files.
- `Bash mv` to promote a file from `insights/` to `knowledge/`, followed
  by `Edit` to tidy its frontmatter.
- `Write` for brand-new knowledge entries and the consolidation marker.

Each insight file follows this shape:

```markdown
---
tags: ["auto-extracted", "insight", "<topic-tag>"]
topic: <topic_slug>
source_task: <task-id-or-chat-id>
source_playbook: memory-extractor
created: <iso-date>
updated: <iso-date>
last_retrieved: <iso-date>
retrieval_count: <int>
---
<body content>
```

Filenames use the pattern `<slug>-<hash>.md` — keep the hash suffix stable
when you edit in place so vault-watcher diffs stay small.

## Steps — do these in order

### 1. Inventory

Glob `{insights_dir}/*.md`. Read each file's frontmatter and body. Group
by the `topic` field where present. For each topic-cluster, hold the
list of filenames + body preview + tags + `retrieval_count` +
`source_task` so you can reason about the cluster as a whole.

If the directory is huge, process the highest-churn topics first (files
with recent `updated` timestamps) and stop cleanly when budget is tight
— the next run continues.

### 2. Cull garbage

An insight is garbage if any of the following apply. **Delete the file
(`Bash rm <path>`) without merging.**

- Starts with a greeting (`Hi …,`, `Hello …,`, `Hey …,`, `Dear …,`) —
  these are reply bodies that leaked through extraction.
- Is raw JSON (`{ ... }` or `[ ... ]` parseable) — a payload, not
  abstracted knowledge.
- Is mostly a URL with little accompanying text.
- Contains conspicuous HTML entity noise (`&#39;`, `&quot;`, etc.).
- Fewer than 8 words of actual content, or longer than ~400 words.
- Is a trivial restatement of an ongoing chat (`the user asked…`,
  `the agent responded…`).

Log each deletion with the reason in your own working notes so the
marker file's log line can summarize them.

### 3. Dedup within clusters

For each topic-cluster with multiple similar insights:

1. Pick a canonical file — prefer the entry with the highest
   `retrieval_count`, else the longest non-garbage body, else the most
   recently updated.
2. If needed, `Edit` the canonical file to incorporate specific wording
   from siblings (more precise field names like `gmail_thread_id`,
   explicit thresholds like `priority: 100`).
3. Append siblings' `source_task` values into the canonical file's
   `source_task` frontmatter field (comma-separated) so provenance is
   preserved.
4. `Bash rm` each non-canonical sibling file.

Treat "near-duplicates" generously: wording variations that restate the
same rule are duplicates. Don't preserve a sibling just because it
phrased things differently.

### 4. Add cross-links

For each remaining insight that relates to another insight or a
knowledge entry, append or update a `## See Also` section in the body
with `[[wiki-link]]` references. Example:

```markdown
## See Also
- [[projects/{project_id}/memory/knowledge/email-task-creation|Email → Task Creation]]
- [[projects/{project_id}/memory/insights/<sibling-slug>-<hash>|Sibling insight title]]
```

Use `Edit` to append — don't rewrite the whole file when a single
section change suffices.

### 5. Promote stable insights to knowledge

An insight is stable enough to promote when:

- It has `retrieval_count >= 2`, OR
- Its `updated` timestamp is older than 7 days AND it survived the
  garbage cull, AND
- It represents a durable project truth (a rule, a business fact, a
  convention) — not a transient observation.

To promote:

1. `Bash mv {insights_dir}/<slug>-<hash>.md {knowledge_dir}/<slug>-<hash>.md`
   (create `{knowledge_dir}` first with `mkdir -p` if needed).
2. `Edit` the moved file to update its frontmatter:
   - Add `knowledge` and `curated` to the `tags` list.
   - Set `topic` to a canonical cluster slug (e.g.
     `email-task-creation`, `compliance-filings`, `document-naming`).
   - Optionally rewrite the body to merge the canonical insight plus
     related insights into one clean knowledge statement.
3. If you merged a cluster into the promoted file, delete the
   non-merged siblings first (step 3 above) so the final knowledge
   entry is truly canonical.

### 6. Update the consolidation marker

Write the marker for **this** project at
`{insights_dir}/../consolidation.md` (i.e.
`vault/projects/{project_id}/memory/consolidation.md`). The file is
project-scoped (one per project — do not touch any other project's
marker). Use `Write` if it does not exist, `Edit` to append a new log
line and update the frontmatter timestamp if it does.

Frontmatter:

```yaml
---
last_consolidated: "<current-iso-timestamp>"
consolidation_stats:
  last_kept: <int>
  last_promoted: <int>
  last_deleted: <int>
  last_merged: <int>
---

# Memory Consolidation Log — {project_name}

- <current-iso-timestamp>: kept=<int> promoted=<int> deleted=<int> merged=<int>
```

Append the new log line below any existing ones so run history is
preserved. Overwrite the frontmatter in place.

## Judgement notes

- **Prefer deletion over hoarding.** A clean vault of a dozen canonical
  insights beats a cluttered vault of forty near-duplicates. The
  extraction pipeline will rebuild anything important.
- **Prefer promotion over leaving things as insights** when a fact is
  durable. `knowledge/` is where the supervisor looks first.
- **Never invent content.** If two siblings say slightly different
  things, merge literally — do not synthesize a cleaner-sounding version
  that nobody wrote.
- **Preserve specific identifiers.** Form numbers (LLC-12), account
  numbers (00257684), URLs, field names (`gmail_thread_id`), and
  explicit constants (`priority: 100`) survive consolidation verbatim.
