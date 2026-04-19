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

## Tools you will use

- `memory_search` — semantic search within this project's scope. Use empty
  or broad queries with topic filters to enumerate clusters.
- `memory_update` — rewrite content, tags, or topic of an existing entry
  by `chunk_hash`. Use to make a cluster's canonical version clearer or
  more complete before deleting the siblings.
- `memory_delete` — remove an insight from both Milvus and the vault file.
  Use for garbage entries and for non-canonical cluster siblings after
  their content is merged into the canonical entry.
- `memory_promote_to_knowledge` — move a stable insight into the
  `knowledge/` subdirectory with the `knowledge` tag. Optionally rewrite
  the content when promoting (useful for merging a cluster into one
  canonical knowledge entry).
- `memory_store` — for creating the consolidation marker update (or any
  brand-new knowledge entry not derived from an existing insight).

## Steps — do these in order

### 1. Inventory

Run broad `memory_search` queries to enumerate insights. Group by the
`topic` field where present. For each topic-cluster, hold the list of
`chunk_hash` + `content` + `tags` + `retrieval_count` + `source_task`
fields so you can reason about the cluster as a whole.

### 2. Cull garbage

An insight is garbage if any of the following apply. **Delete it
(`memory_delete`) without merging.**

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
marker file's `deleted_reasons` can summarize them.

### 3. Dedup within clusters

For each topic-cluster with multiple similar insights:

1. Pick a canonical `chunk_hash` — prefer the entry with the highest
   `retrieval_count`, else the longest non-garbage content, else the
   most recently updated.
2. If needed, `memory_update` the canonical entry to incorporate any
   specific wording from siblings (e.g. more precise field names like
   `gmail_thread_id`, explicit thresholds like `priority: 100`).
3. Concatenate siblings' `source_task` values into the canonical
   entry's `source_task` so provenance is preserved.
4. `memory_delete` each non-canonical sibling.

Treat "near-duplicates" generously: wording variations that restate the
same rule are duplicates. Don't preserve a sibling just because it
phrased things differently.

### 4. Add cross-links

For each remaining insight that relates to another insight or a
knowledge entry, append or update a `## See Also` section in the file
body with `[[wiki-link]]` references. Example:

```markdown
## See Also
- [[projects/{project_id}/memory/knowledge/email-task-creation|Email → Task Creation]]
- [[projects/{project_id}/memory/insights/<sibling-slug>-<hash>|Sibling insight title]]
```

Use `memory_update` to save the rewritten content.

### 5. Promote stable insights to knowledge

An insight is stable enough to promote when:

- It has `retrieval_count >= 2`, OR
- Its `updated` timestamp is older than 7 days AND it survived the
  garbage cull, AND
- It represents a durable project truth (a rule, a business fact, a
  convention) — not a transient observation.

For promotion, `memory_promote_to_knowledge` with:
- `chunk_hash` of the source insight
- `topic` set to the canonical cluster topic (e.g. `email-task-creation`,
  `compliance-filings`, `document-naming`)
- Optionally `content` rewritten to merge the canonical insight plus any
  related insights into one clean knowledge statement

When you promote a merged cluster, delete the non-merged siblings first
(step 3) so the final knowledge entry is truly canonical.

The promoted entry lands at
`{knowledge_dir}/<slug>-<hash>.md` with the `knowledge` and `curated`
tags applied automatically.

### 6. Update the consolidation marker

Write the marker for **this** project at
`vault/projects/{project_id}/memory/consolidation.md`. The file is
project-scoped (one per project — do not touch any other project's
marker). Use a direct file write; the frontmatter should carry the
ISO-8601 timestamp of this run and the run's counts:

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
