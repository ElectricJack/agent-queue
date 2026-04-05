"""Prompts for the memory consolidation system.

Phase 1: Project Factsheet — structured YAML-frontmatter metadata layer
that surfaces key project facts (URLs, tech stack, contacts, etc.) for
instant lookup.

Phase 2: Post-task fact extraction — after each completed task, extract
structured facts (URLs, tech stack decisions, architectural patterns, etc.)
into staging JSON files for later consolidation into the project factsheet
and knowledge base.

Phase 3: Knowledge Base Topic Files — topic-organized knowledge files
(``knowledge/architecture.md``, etc.) that contain sourced facts with
deep-dive links back to originating tasks and notes.

Phase 4: Daily Consolidation Process — scheduled process that reads
accumulated staging files, updates the project factsheet YAML, and merges
relevant facts into the appropriate knowledge base topic files.
"""

# ---------------------------------------------------------------------------
# Phase 1: Project Factsheet
# ---------------------------------------------------------------------------

FACTSHEET_SEED_TEMPLATE = """\
---
# Auto-maintained by consolidation system
# Manual edits are preserved; consolidation merges, not overwrites
last_updated: "{last_updated}"
consolidation_version: 1

project:
  name: "{project_name}"
  id: "{project_id}"
  description: ""

urls:
  github: {github_url}
  docs: null
  ci: null
  deploy: null

tech_stack:
  language: null
  framework: null
  build_system: null
  test_framework: null
  key_dependencies: []

environments: []

contacts:
  owner: null

key_paths:
  source: null
  tests: null
  config: null
  entry_point: null
---

# {project_name} — Quick Reference

## What It Does
*(to be filled in by consolidation or manually)*

## Current State
*(to be filled in by consolidation or manually)*

## Recent Focus Areas
*(to be filled in by consolidation or manually)*
"""

FACTSHEET_EXTRACTION_PROMPT = """\
You are updating a project factsheet with newly discovered facts. The \
factsheet uses YAML frontmatter for structured data and a markdown body \
for human-readable summary.

Given the current factsheet and a set of new facts, produce an updated \
YAML frontmatter block. Rules:
1. MERGE new values into existing structure — never remove manually-set fields.
2. Only update fields where the new information is more specific or more \
   current than the existing value.
3. null fields should be filled in if a fact provides the information.
4. For list fields (key_dependencies, environments), append new entries \
   and deduplicate.
5. Output ONLY the updated YAML frontmatter (between --- delimiters), \
   nothing else.
"""

FACT_EXTRACTION_SYSTEM_PROMPT = """\
You are a fact extraction engine for a software project. Your job is to \
extract concrete, structured facts from a completed task summary. These \
facts will be stored and later consolidated into a project knowledge base.

Extract ONLY verifiable, specific facts — not opinions, not vague \
observations. Each fact must belong to exactly one category.

Categories:
- **url** — Any URL discovered or referenced (repo, docs, API endpoint, \
  dashboard, CI/CD link, etc.)
- **tech_stack** — Technology, library, framework, language, or tool used \
  or added to the project.
- **decision** — A concrete technical decision made during the task, with \
  brief rationale.
- **convention** — A coding convention, naming pattern, or project standard \
  established or reinforced.
- **architecture** — An architectural fact about the system (component \
  relationships, data flow, deployment topology, etc.)
- **config** — Configuration values, environment variables, feature flags, \
  or settings that were set or changed.
- **contact** — A person, team, or service account referenced with a role.

Rules:
1. Only extract facts that are clearly stated or strongly implied by the \
   task summary. Do NOT hallucinate or infer facts not present.
2. Each fact must have a short, descriptive ``key`` (snake_case, max 50 chars) \
   and a ``value`` that captures the essential information.
3. For URLs, the value should be the URL itself. For decisions, include the \
   rationale. For tech_stack, include the version if mentioned.
4. Skip trivial or obvious facts (e.g. "uses Python" for a Python project).
5. If the task summary contains no extractable facts, return an empty array.
6. Respond with ONLY a JSON array — no markdown fences, no preamble.

Example output:
[
  {{"category": "tech_stack", "key": "jwt_library", "value": "PyJWT 2.8.0 for token signing"}},
  {{"category": "decision", "key": "auth_token_storage", "value": "Store refresh tokens in httponly cookies, not localStorage, for XSS protection"}},
  {{"category": "url", "key": "auth_docs", "value": "https://docs.example.com/auth"}},
  {{"category": "convention", "key": "api_error_format", "value": "All API errors return {{\\"error\\": str, \\"code\\": str}} JSON body"}}
]
"""

FACT_EXTRACTION_USER_PROMPT = """\
## Completed Task
**ID:** {task_id}
**Title:** {task_title}
**Type:** {task_type}
**Project:** {project_id}

### Summary
{task_summary}

### Files Changed
{files_changed}

Extract structured facts from this task. Respond with a JSON array.
"""

# ---------------------------------------------------------------------------
# Phase 3: Knowledge Base Topic Files
# ---------------------------------------------------------------------------

# Seed templates for each knowledge topic file.  These are written to
# ``knowledge/{topic}.md`` when the topic file is first created (e.g. during
# the daily consolidation or when an agent explicitly writes a fact).  Each
# template establishes the heading structure and explains what goes in the
# file so the consolidation LLM can follow the pattern.

KNOWLEDGE_TOPIC_SEED_TEMPLATES: dict[str, str] = {
    "architecture": """\
# Architecture Knowledge

> Last consolidated: {last_updated} | Sources: 0 tasks, 0 notes

## Core Architecture
*(System components, overall design, and high-level structure)*

## Data Flow
*(How data moves through the system — request/response paths, event flows)*

## Key Components
*(Major modules, services, or subsystems with brief descriptions)*
""",
    "api-and-endpoints": """\
# API & Endpoints Knowledge

> Last consolidated: {last_updated} | Sources: 0 tasks, 0 notes

## API Routes
*(REST/GraphQL/gRPC endpoints with methods and brief descriptions)*

## Protocols & Formats
*(Wire formats, authentication schemes, API versioning)*

## Integrations
*(External services, webhooks, third-party API usage)*
""",
    "deployment": """\
# Deployment Knowledge

> Last consolidated: {last_updated} | Sources: 0 tasks, 0 notes

## Environments
*(Dev, staging, production — URLs, access, configuration)*

## CI/CD Pipeline
*(Build steps, test gates, deployment process)*

## Infrastructure
*(Hosting, containers, cloud services, monitoring)*
""",
    "dependencies": """\
# Dependencies Knowledge

> Last consolidated: {last_updated} | Sources: 0 tasks, 0 notes

## Runtime Dependencies
*(Key libraries, frameworks, and their versions)*

## Development Dependencies
*(Build tools, test frameworks, linters)*

## Version Constraints
*(Pinned versions, compatibility notes, upgrade blockers)*
""",
    "gotchas": """\
# Gotchas & Known Issues

> Last consolidated: {last_updated} | Sources: 0 tasks, 0 notes

## Known Issues
*(Bugs, limitations, or broken functionality to be aware of)*

## Workarounds
*(Temporary fixes or manual steps required)*

## Pitfalls
*(Common mistakes, non-obvious behavior, foot-guns)*
""",
    "conventions": """\
# Conventions & Standards

> Last consolidated: {last_updated} | Sources: 0 tasks, 0 notes

## Coding Standards
*(Style rules, linting config, formatting requirements)*

## Naming Conventions
*(File naming, variable naming, module naming patterns)*

## Workflow Patterns
*(Branching strategy, PR process, commit message format)*
""",
    "decisions": """\
# Technical Decisions

> Last consolidated: {last_updated} | Sources: 0 tasks, 0 notes

## Architecture Decisions
*(Major structural choices with rationale)*

## Technology Choices
*(Why specific tools, libraries, or services were chosen)*

## Design Trade-offs
*(Explicit trade-offs made and their justification)*
""",
}

# ---------------------------------------------------------------------------
# Phase 4: Daily Consolidation Process
# ---------------------------------------------------------------------------

# Mapping from fact categories to the knowledge topic they should update.
# A single fact category may map to multiple topics (e.g. "decision" facts
# are relevant to both "decisions" and "architecture").
FACT_CATEGORY_TO_TOPIC: dict[str, list[str]] = {
    "url": ["api-and-endpoints", "deployment"],
    "tech_stack": ["dependencies"],
    "decision": ["decisions", "architecture"],
    "convention": ["conventions"],
    "architecture": ["architecture"],
    "config": ["deployment"],
    "contact": [],  # contacts go to factsheet only, not knowledge topics
}

DAILY_CONSOLIDATION_SYSTEM_PROMPT = """\
You are a project knowledge consolidation engine. Your job is to merge \
newly extracted facts into existing knowledge artifacts: a YAML factsheet \
and topic-organized markdown knowledge files.

You will receive:
1. The current project factsheet (YAML frontmatter + markdown body).
2. The current content of each relevant knowledge topic file.
3. A batch of new facts extracted from recently completed tasks.

Your output must be a JSON object with two keys:

- **"factsheet_yaml"** — The updated YAML frontmatter block (between --- \
  delimiters). Merge new facts into existing fields. Never remove manually \
  set values. Fill in null fields when facts provide the information. For \
  list fields, append new entries and deduplicate.

- **"knowledge_updates"** — A JSON object mapping topic slugs (e.g. \
  "architecture", "decisions") to their updated markdown content. Only \
  include topics that actually changed. Preserve existing content and \
  append or update sections with the new facts. Each fact should include \
  a source reference like "(from task: {task_id})".

Rules:
1. MERGE, never overwrite — preserve existing manually-set content.
2. Deduplicate — if a fact is already present (same key or equivalent \
   meaning), skip it. Newer facts take precedence over older ones when \
   they conflict.
3. Source attribution — every new fact added to a knowledge topic must \
   include "(from task: {task_id})" at the end of the bullet.
4. Keep factsheet YAML structure identical — same keys, same nesting.
5. Update the "last_updated" field in the factsheet to the current timestamp.
6. For knowledge topics, update the "Last consolidated" line in the \
   blockquote header with the current timestamp and source count.
7. Output ONLY valid JSON — no markdown fences, no commentary.
"""

DAILY_CONSOLIDATION_USER_PROMPT = """\
## Current Factsheet
```yaml
{current_factsheet_yaml}
```

## Knowledge Topics
{knowledge_topics_section}

## New Facts to Consolidate
{facts_section}

## Timestamp
Current time: {timestamp}

Produce the consolidated output as a JSON object with "factsheet_yaml" \
and "knowledge_updates" keys.
"""

# ---------------------------------------------------------------------------
# Phase 6: Weekly Deep Consolidation
# ---------------------------------------------------------------------------

DEEP_CONSOLIDATION_SYSTEM_PROMPT = """\
You are a project knowledge curator. Your job is to review the entire \
knowledge base for a project and perform deep maintenance:

1. **Prune stale facts** — Remove facts that are clearly outdated, \
   superseded, or contradicted by newer information. Mark anything \
   uncertain as "(needs verification)".
2. **Resolve conflicts** — When two facts in the same topic or across \
   topics contradict each other, keep the more recent or more \
   authoritative version.
3. **Regenerate the factsheet summary** — Update the markdown body \
   sections ("What It Does", "Current State", "Recent Focus Areas") \
   to reflect the current state of the knowledge base.
4. **Consolidate knowledge topics** — Remove duplicate entries within \
   topics, merge related facts, and ensure consistent formatting.

You will receive:
1. The current project factsheet (YAML frontmatter + markdown body).
2. All knowledge topic files with their current content.
3. Metadata about fact ages (when available).

Your output must be a JSON object with three keys:

- **"factsheet_yaml"** — The updated YAML frontmatter block. Only modify \
  fields that need updating based on your review.

- **"factsheet_body"** — The updated markdown body for the factsheet \
  (everything after the YAML frontmatter). Rewrite the summary sections \
  to accurately reflect the project's current state based on all available \
  knowledge.

- **"knowledge_updates"** — A JSON object mapping topic slugs to their \
  cleaned-up markdown content. Include ALL topics that you reviewed, even \
  if unchanged (so we can verify). Omit topics that truly had no content.

- **"pruned_facts"** — A JSON array of strings describing facts that were \
  removed or flagged, for audit logging. Each string should briefly explain \
  what was pruned and why.

Rules:
1. Preserve manually-set values — never remove content that appears to be \
   hand-written rather than auto-generated.
2. When in doubt, keep the fact but add "(needs verification)".
3. Source references "(from task: ...)" should be preserved.
4. Update "Last consolidated" timestamps in topic headers.
5. Output ONLY valid JSON — no markdown fences, no commentary.
"""

DEEP_CONSOLIDATION_USER_PROMPT = """\
## Current Factsheet
### YAML Frontmatter
```yaml
{current_factsheet_yaml}
```

### Markdown Body
```markdown
{current_factsheet_body}
```

## Knowledge Topics
{knowledge_topics_section}

## Processed Staging File Count
{processed_count} staging files have been consolidated previously.

## Timestamp
Current time: {timestamp}

Review the entire knowledge base, prune stale facts, resolve conflicts, \
and regenerate the factsheet summary. Output a JSON object with \
"factsheet_yaml", "factsheet_body", "knowledge_updates", and \
"pruned_facts" keys.
"""

# ---------------------------------------------------------------------------
# Phase 6: Bootstrap Consolidation
# ---------------------------------------------------------------------------

BOOTSTRAP_SYSTEM_PROMPT = """\
You are bootstrapping a project knowledge base from existing task memories. \
The project has completed tasks but no structured factsheet or knowledge \
base yet. Your job is to synthesize the task history into:

1. A **factsheet YAML** — Extract concrete metadata (URLs, tech stack, \
   contacts, key paths, etc.) from the task summaries. Only include facts \
   that are clearly stated.

2. A **factsheet body** — Write concise markdown summary sections:
   - "What It Does" — one-paragraph project description
   - "Current State" — current development status
   - "Recent Focus Areas" — what the team has been working on recently

3. **Knowledge topic updates** — For each relevant topic category, \
   extract and organize facts from the task history. Include source \
   references like "(from task: {task_id})".

You will receive:
1. The project's existing profile (if any) — a synthesized overview.
2. A sample of task memory files — summaries of completed tasks.
3. The seed factsheet YAML template (empty fields to fill in).
4. The list of available knowledge topics.

Your output must be a JSON object with:

- **"factsheet_yaml"** — Populated YAML data (as a dict/object). Fill in \
  as many fields as the task history supports. Leave fields as null if \
  no evidence exists.

- **"factsheet_body"** — Markdown body for the factsheet.

- **"knowledge_updates"** — A JSON object mapping topic slugs to markdown \
  content. Only include topics where you found relevant facts.

Rules:
1. Only extract facts that are clearly stated in the task summaries.
2. Do NOT hallucinate or infer facts not present in the source material.
3. Prefer recent tasks over older ones when they conflict.
4. Keep entries concise — this is a reference, not documentation.
5. Output ONLY valid JSON — no markdown fences, no commentary.
"""

BOOTSTRAP_USER_PROMPT = """\
## Project Info
- **Project ID:** {project_id}
- **Project Name:** {project_name}
- **Repository URL:** {repo_url}

## Existing Profile
```markdown
{existing_profile}
```

## Seed Factsheet Template
```yaml
{seed_yaml}
```

## Available Knowledge Topics
{available_topics}

## Task History ({task_count} tasks)
{task_summaries}

Bootstrap the project knowledge base from this task history. Output a \
JSON object with "factsheet_yaml", "factsheet_body", and \
"knowledge_updates" keys.
"""
