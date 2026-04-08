# Specification: Plan Parser

## 1. Overview

The plan parser converts markdown implementation plan files into structured task definitions that the [[orchestrator]] can schedule as follow-up work.

When an agent completes a task, it may write a plan file (e.g. `.claude/plan.md`) to its workspace describing the steps required to carry out that work. The plan parser reads this file, identifies the actionable steps, and returns them as an ordered list of `PlanStep` objects wrapped in a `ParsedPlan`. The orchestrator then creates one child task per step, chaining them with dependencies.

The module is split into two files:

- `src/plan_parser.py` — regex-based parser, data models, plan file discovery, and task description builder. No I/O beyond reading the plan file itself.
- `src/plan_parser_llm.py` — async wrapper that sends the raw markdown to a `ChatProvider` (Claude or another configured LLM) and uses tool-use structured output to extract steps. Falls back to the regex parser on any failure.

---

## Source Files
- `src/plan_parser.py`
- `src/plan_parser_llm.py`

---

## 2. Data Models

### `PlanStep`

A single actionable step extracted from a plan. Defined as a dataclass in `src/plan_parser.py`.

| Field | Type | Description |
|---|---|---|
| `title` | `str` | Short, human-readable label for the step. Used as the child task title. |
| `description` | `str` | Full body text of the step — all implementation detail the executing agent needs. |
| `priority_hint` | `int` | 0-based index of the step in the original plan. Preserves ordering when the orchestrator creates tasks. Defaults to `0`. |

### `ParsedPlan`

The result of parsing a plan file. Defined as a dataclass in `src/plan_parser.py`.

| Field | Type | Description |
|---|---|---|
| `source_file` | `str` | Absolute path to the plan file that was parsed. Used for traceability in log messages and task metadata. |
| `steps` | `list[PlanStep]` | Ordered list of extracted steps. Empty list means no actionable steps were found. |
| `raw_content` | `str` | The full, unmodified text of the plan file. Stored so the orchestrator can pass it as context when building task descriptions. Defaults to `""`. |

---

## 3. Plan File Discovery

### Function: `find_plan_file(workspace, patterns=None) -> str | None`

Searches for a plan file inside a workspace directory and returns the path to the first match, or `None` if nothing is found.

#### Default search patterns

When `patterns` is not supplied, the function uses `DEFAULT_PLAN_FILE_PATTERNS` (checked in order):

```
.claude/plan.md
plan.md
docs/plans/*.md
plans/*.md
docs/plan.md
```

#### Pattern resolution

Each pattern is joined with `workspace` using `os.path.join`. Two resolution strategies are applied depending on whether the pattern contains glob characters (`*` or `?`):

- **Plain pattern** (no glob characters) — the joined path is checked with `os.path.isfile`. If the file exists it is returned immediately.
- **Glob pattern** (contains `*` or `?`) — the joined pattern is expanded via `glob.glob`. All results that pass `os.path.isfile` are collected. If any matches exist, the one with the most recent `os.path.getmtime` is returned. This ensures the freshest plan file wins when multiple files match (e.g. date-prefixed filenames).

#### Precedence

Patterns are evaluated in order. The function returns on the first pattern that yields a match. A plain pattern earlier in the list will shadow glob patterns that come later, even if the glob would match a newer file.

---

### Function: `read_plan_file(path) -> str`

Reads the raw contents of a plan file from disk and returns them as a string. Opens the file with UTF-8 encoding. No parsing or transformation is performed — this is a thin I/O wrapper used by callers that have already resolved the path via `find_plan_file`.

---

## 4. Regex Parser

### Entry point: `parse_plan(content, source_file="", max_steps=20) -> ParsedPlan`

Parses raw markdown content into a `ParsedPlan`. Three strategies are attempted in order; the first one that yields at least one step is used.

```
1. Heading-based parsing  (## / ### sections)
2. Numbered-list fallback (1. / 2. top-level items)
3. Single-step fallback   (entire body as one step)
```

The `max_steps` argument caps the number of steps returned. Steps beyond the limit are silently discarded. Empty content returns an empty `ParsedPlan` immediately.

---

### 4.1 Heading-Based Parsing

**Function:** `_parse_heading_sections(content) -> list[PlanStep]`

Splits the document on `##` and `###` headings (depth-2 and depth-3 only; the document title `#` is ignored). Each heading becomes a candidate step.

**Algorithm:**

1. Find all `##` / `###` heading matches using `re.compile(r"^(#{2,3})\s+(.+)$", re.MULTILINE)`.
2. For each heading, extract the title text and run it through `_clean_step_title` (see Section 4.4).
3. Skip the heading if the cleaned title is empty or if its lowercased form appears in `NON_ACTIONABLE_HEADINGS` (see Section 4.3).
4. Extract the body text between the current heading's end and the start of the next heading (or end of file).
5. Skip the step if the body is fewer than 20 characters — too little content to be actionable.
6. Append a `PlanStep` with `priority_hint` equal to the running count of accepted steps.

If no `##` / `###` headings exist at all, this function returns an empty list and the caller falls through to numbered-list parsing.

---

### 4.2 Numbered-List Fallback

**Function:** `_parse_numbered_list(content) -> list[PlanStep]`

Used when the document has no usable heading sections.

Matches top-level numbered list items using `re.compile(r"^(\d+)[.)]\s+(.+)$", re.MULTILINE)`. Both dot (`1.`) and closing-parenthesis (`1)`) terminators are accepted.

**Algorithm:**

1. Collect all numbered-item matches.
2. For each match, extract the item text and run it through `_clean_step_title`.
3. Skip items with an empty cleaned title.
4. The body is the text between the current item's end and the start of the next item (indented continuation lines and sub-items).
5. The step `description` is the title alone when there is no body, or `"{title}\n\n{body_text}"` when a body exists.
6. The step `title` is the cleaned title truncated to 120 characters (with `...` ellipsis).
7. `priority_hint` is the 0-based index of the match in the document.

No non-actionable filtering is applied to numbered-list items.

---

### 4.3 Single-Step Fallback

When both heading-based and numbered-list parsing return empty lists, the entire document body is treated as a single step.

**Algorithm:**

1. Extract the document title using `_extract_title` (the first `# Heading` in the file). If none exists, the title defaults to `"Implementation Plan"`.
2. Strip the title line from the content using `_remove_title`.
3. If the remaining body is non-empty after stripping, create one `PlanStep` with the extracted title and the full remaining body as the description.
4. If the body is also empty, the returned `ParsedPlan` has zero steps.

---

### 4.4 Non-Actionable Heading Filtering

The set `NON_ACTIONABLE_HEADINGS` contains the lowercased strings of headings that are structural or informational rather than actionable. Any `##` / `###` heading whose cleaned title matches a member of this set is silently skipped during heading-based parsing.

Current members (case-insensitive match):

```
overview               summary              background           context
introduction           conclusion           notes                references
appendix               prerequisites        requirements         assumptions
risks                  open questions       future work          out of scope
glossary               table of contents    motivation           goals
non-goals              success criteria     files modified       files to modify
file changes           testing strategy     testing notes        rollback plan
implementation order   dependency graph     design decisions     key decisions
additional observations   identified gaps   verdict              executive summary
problem statement      data flow            file change summary  user workflow examples
```

Matching is exact after lowercasing and after title cleaning. A heading like `## Step 1: Overview` first becomes `Overview` through `_clean_step_title`, then matches `"overview"` in the set and is dropped.

---

### 4.5 Title Cleaning

**Function:** `_clean_step_title(title) -> str`

Applied to every heading title and every numbered-list item text before the step is accepted or filtered.

Transformations applied in order:

1. **Keyword + digit + separator** — removes prefixes matching `Step N:`, `Phase N:`, `Part N:`, `Task N:` (with any of `:`, `.`, `)`, `-`, em-dash, en-dash as the separator). Pattern: `^(?:Step|Phase|Part|Task)\s*\d+\s*[:.)\-\u2014\u2013]\s*` (case-insensitive).

2. **Keyword + digit without separator** — removes prefixes matching `Step 1 Do something` where no separator follows the digit. Pattern: `^(?:Step|Phase|Part|Task)\s+\d+\s+` (case-insensitive).

3. **Leading number + separator** — removes any remaining leading `N.`, `N)`, `N:`, `N-` prefix left after step 1/2. Pattern: `^\d+[.):\-\u2014\u2013]\s*`.

4. **Markdown bold/italic** — strips surrounding `*` or `**` markers from the entire string. Pattern: `\*{1,2}(.+?)\*{1,2}` replaced with the captured group.

5. **Trailing whitespace** — `.strip()` is called on the result.

---

### 4.6 Max Steps Limit

After the winning parsing strategy returns its list, `parse_plan` slices it with `steps[:max_steps]`. The default limit is 20. Steps beyond the limit are discarded without warning.

---

## 5. LLM Parser

### Module: `src/plan_parser_llm.py`

### 5.1 When It Is Used

The LLM parser is an opt-in alternative to the regex parser. The orchestrator selects it when the `auto_task.use_llm_parser` config flag is `True` and a `ChatProvider` instance is available. When those conditions are not met, the regex parser is used directly.

The LLM parser is always async. It uses the `ChatProvider` abstraction so the underlying model (Anthropic Claude, Ollama, etc.) is interchangeable.

---

### 5.2 Entry Point

```python
async def parse_plan_with_llm(
    raw_content: str,
    provider: ChatProvider,
    source_file: str = "",
    max_steps: int = 20,
) -> ParsedPlan:
```

`ChatProvider` is the abstract base class from `src/chat_providers/base.py`. It exposes a single `async create_message(*, messages, system, tools, max_tokens) -> ChatResponse` method.

---

### 5.3 System Prompt

The LLM receives the following fixed system prompt:

```
You are a plan parser. Given a markdown implementation plan, extract ONLY the
actionable implementation steps — concrete coding tasks that an agent should execute.

Skip non-actionable sections: overviews, summaries, background, conclusions,
dependency graphs, file inventories, etc.

Each step title should be an imperative action (e.g. "Add user auth endpoint").
Each step description should contain all implementation details needed.

Return the steps using the extract_plan_steps tool.
```

The raw plan file content is sent as a single user message.

---

### 5.4 Tool Schema (`extract_plan_steps`)

The call is made with `tools=[_TOOL]` and `max_tokens=4096`. The tool schema forces the LLM to return structured JSON rather than free text:

```json
{
  "name": "extract_plan_steps",
  "description": "Extract actionable implementation steps from a plan.",
  "input_schema": {
    "type": "object",
    "properties": {
      "steps": {
        "type": "array",
        "items": {
          "type": "object",
          "properties": {
            "title": {
              "type": "string",
              "description": "Short imperative title for the step."
            },
            "description": {
              "type": "string",
              "description": "Full implementation details for the step."
            }
          },
          "required": ["title", "description"]
        },
        "description": "Ordered list of actionable implementation steps."
      }
    },
    "required": ["steps"]
  }
}
```

Both `title` and `description` are required fields on each step object.

---

### 5.5 Response Processing

The function iterates over `response.tool_uses` looking for a block with `name == "extract_plan_steps"`. When found:

1. The `steps` list is read from `block.input`.
2. Steps beyond `max_steps` are discarded (via slice `raw_steps[:max_steps]`).
3. Each remaining step is converted to a `PlanStep` with `priority_hint` equal to its 0-based index. Steps where either `title` or `description` is empty after stripping are skipped.
4. A `ParsedPlan` is returned with the collected steps, the original `source_file`, and the full `raw_content`.

---

### 5.6 Fallback to Regex Parser

Two conditions cause the function to fall back to the regex-based `parse_plan`:

1. **No `extract_plan_steps` tool-use block in the response** — the LLM returned a text response or called a different tool.
2. **Any exception** — network errors, provider errors, or malformed responses are not caught inside `parse_plan_with_llm`; the caller is expected to wrap the call in a try/except and invoke `parse_plan` directly on failure.

When the fallback is triggered by condition 1, the same `source_file` and `max_steps` arguments are forwarded to `parse_plan`.

---

## 6. Task Description Builder

### Function: `build_task_description(step, parent_task=None, plan_context="") -> str`

Assembles a self-contained task description from a `PlanStep` so that the executing agent has all required context without needing to access any external state.

### Arguments

| Argument | Type | Description |
|---|---|---|
| `step` | `PlanStep` | The parsed step whose title and description are the primary content. |
| `parent_task` | object or `None` | The task that produced the plan. Must have a `.title` attribute if provided. Used to give the agent lineage context. |
| `plan_context` | `str` | Optional preamble text from the plan (e.g. a background section extracted before step parsing). Empty string means no background section is added. |

### Output format

The returned string is built by joining the following parts with `"\n"`:

1. **Bold step title** — `**{step.title}**\n` (always present).

2. **Background context section** — `## Background Context\n{plan_context}\n` — included only when `plan_context` is a non-empty string.

3. **Parent task attribution** — `This task is part of the implementation plan from: **{parent_task.title}**\n` — included only when `parent_task` is not `None` and has a `.title` attribute.

4. **Task details section** — `## Task Details\n{step.description}` (always present).

### Example output (all fields populated)

```
**Add user auth endpoint**

## Background Context
This plan implements the authentication subsystem described in the RFC.

This task is part of the implementation plan from: **Design auth system**

## Task Details
Create a POST /auth/login endpoint that accepts username and password,
validates credentials against the users table, and returns a signed JWT.
```
