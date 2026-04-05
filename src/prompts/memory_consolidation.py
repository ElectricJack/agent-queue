"""Prompts for the memory consolidation system.

Phase 2: Post-task fact extraction — after each completed task, extract
structured facts (URLs, tech stack decisions, architectural patterns, etc.)
into staging JSON files for later consolidation into the project factsheet
and knowledge base.

Future phases will add consolidation and knowledge-base prompts here.
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
