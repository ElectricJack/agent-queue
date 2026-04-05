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

Future phases will add consolidation and bootstrap prompts here.
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
