# Proactive Codebase Inspector — Specification

**Source files:** `src/prompts/default_rules/proactive-codebase-inspector.md` (default rule)
**Related:** `specs/rule-system.md`, `specs/hooks.md`, `specs/chat-observer.md`

---

## 1. Overview

The Proactive Codebase Inspector is a periodic automation that randomly selects
sections of a project's source code, documentation, specs, tests, and configuration
files, then analyzes them for potential improvements, issues, or suggestions that the
team hasn't explicitly identified.

**Key insight:** The current suggestion system is *reactive* — it activates after tasks
are created, after failures, or when Discord conversation triggers observation. The
inspector is *proactive* — it initiates analysis on its own schedule without waiting
for a human to notice a problem.

### How It Differs from Existing Automation

| System | Trigger | Focus |
|--------|---------|-------|
| Post-Action Reflection | After task completion | Did the task succeed? Follow-up needed? |
| Spec Drift Detector | After task completion | Did code changes break spec alignment? |
| Periodic Project Review | Every 30 minutes | Stuck tasks, orphaned hooks (operational health) |
| Weekly Goals Analysis | Weekly | Goals/requirements compliance at macro level |
| Chat Observer | Discord messages | React to team conversations |
| **Proactive Inspector** | **Periodic (configurable)** | **Random deep-dive into actual code/docs/specs** |

The inspector is the only system that *reads and analyzes source artifacts directly*
on a recurring basis without being triggered by a specific event or conversation.

---

## 2. Design

### 2.1 Architecture

The inspector is implemented as a **default global rule** that generates periodic hooks
for each project. It uses the existing rule → hook → supervisor execution pipeline.

```
Rule: proactive-codebase-inspector (global, periodic)
  ↓ (reconciliation)
Hook per project (fires every N hours)
  ↓ (tick)
Supervisor with inspector prompt
  ↓ (tools: shell, file read, task creation)
Random target selection → Read → Analyze → Decide → Suggest or skip
```

### 2.2 Target Selection Strategy

The inspector selects a random "inspection target" from the project workspace. Targets
are individual files or focused sections of larger files.

**Target categories (weighted random selection):**

| Category | Weight | Examples |
|----------|--------|---------|
| Source code | 40% | `src/**/*.py`, `lib/**/*.ts` |
| Specs/docs | 20% | `specs/*.md`, `docs/*.md`, `README.md` |
| Tests | 15% | `tests/**/*.py`, `test/**/*.ts` |
| Configuration | 10% | `pyproject.toml`, `Dockerfile`, CI configs |
| Recently modified | 15% | Files changed in the last 7 days |

**Selection algorithm:**

1. List all eligible files in the workspace (respecting `.gitignore`)
2. Categorize each file
3. Roll weighted random category
4. Pick a random file from that category
5. If the file is large (>300 lines), select a random contiguous section (~100-200 lines)
6. Check inspection history — if this file was inspected in the last N cycles,
   re-roll (up to 3 retries, then accept)

**Exclusions:**
- Binary files, images, fonts
- `node_modules/`, `__pycache__/`, `.git/`, vendor directories
- Generated files (lockfiles, compiled output)
- Files under 5 lines (trivial)

### 2.3 Analysis Dimensions

The inspector evaluates the selected target across multiple dimensions. Not all
dimensions apply to every file type.

**For source code:**
- **Code quality:** Naming clarity, function complexity, dead code, magic numbers,
  overly long functions, duplicated logic
- **Performance:** Obvious inefficiencies, blocking calls in async code, missing
  caching opportunities, N+1 patterns
- **Security:** Hardcoded secrets/credentials, SQL injection risks, missing input
  validation, unsafe deserialization
- **Error handling:** Bare except clauses, swallowed errors, missing error paths
- **Architecture:** Tight coupling, circular dependencies, god classes/functions,
  missing abstractions
- **TODO/FIXME/HACK:** Stale markers that have been sitting around unaddressed
- **Consistency:** Deviations from project conventions (naming, patterns, structure)

**For specs/docs:**
- **Accuracy:** Does the spec match what the code actually does?
- **Completeness:** Missing sections, undocumented behaviors, gaps
- **Staleness:** References to removed features, outdated examples
- **Clarity:** Confusing language, ambiguous requirements

**For tests:**
- **Coverage gaps:** Obvious untested paths, missing edge cases
- **Test quality:** Brittle assertions, missing setup/teardown, flaky patterns
- **Consistency:** Test naming, organization, fixture usage

**For configuration:**
- **Security:** Exposed secrets, overly permissive settings
- **Correctness:** Deprecated options, conflicting settings
- **Completeness:** Missing environments, incomplete CI pipelines

### 2.4 Decision Threshold

Not every inspection should produce a suggestion. The inspector must evaluate whether
a finding is **worth the team's attention**. The LLM is explicitly instructed to:

1. **Skip trivial issues** — Minor style nits that don't affect functionality
2. **Skip known patterns** — Things that are clearly intentional design choices
3. **Skip if uncertain** — When the inspector can't determine if something is actually
   a problem without broader context
4. **Suggest only actionable items** — Every suggestion should have a clear next step

**Decision output:**

```json
{
  "action": "skip" | "suggest",
  "category": "code_quality" | "performance" | "security" | "architecture" |
              "documentation" | "testing" | "maintenance",
  "severity": "low" | "medium" | "high",
  "title": "Short description of the finding",
  "detail": "Explanation with specific file/line references",
  "suggested_action": "What to do about it"
}
```

If `action == "skip"`, no suggestion is posted. The inspection is still logged in the
hook run for transparency.

### 2.5 Suggestion Delivery

When the inspector finds something worth suggesting, it uses the existing suggestion
infrastructure:

1. **Post a suggestion** via the project's Discord channel using the existing
   `chat_analyzer_suggestions` table and `SuggestionView` UI
2. **Suggestion type:** `task` (if it requires code work), `warning` (if it's a risk),
   or `context` (if it's informational)
3. **Include context:** The suggestion embed includes the inspected file path, the
   specific finding, and a proposed action
4. **Deduplication:** Hash-based dedup prevents the same finding from being suggested
   repeatedly (existing `suggestion_hash` mechanism)

### 2.6 Inspection History Tracking

The inspector tracks what it has inspected to ensure broad coverage:

- **Storage:** A lightweight JSON file in project memory:
  `~/.agent-queue/memory/{project_id}/inspector_history.json`
- **Contents:** List of `{file_path, inspected_at, finding_count}` entries
- **Rotation:** Entries older than 30 days are pruned
- **Coverage goal:** Over time, the inspector should touch most of the codebase rather
  than repeatedly inspecting the same popular files

### 2.7 Token Budget

Proactive inspection should not consume excessive tokens. Controls:

- **Per-inspection cap:** The hook's LLM invocation is bounded by the standard hook
  token limits
- **Cooldown:** Default 4 hours between inspections per project (configurable via
  `cooldown_seconds` on the rule)
- **Circuit breaker:** If the hourly reflection token cap is near exhaustion, the
  inspector defers to the next cycle
- **Lightweight first pass:** Read the file, do a quick assessment. Only do deep
  analysis if the quick pass finds something potentially interesting

---

## 3. Rule Definition

The inspector ships as a default global rule installed by `install_defaults()`.
The rule file lives at `src/prompts/default_rules/proactive-codebase-inspector.md`
and is installed as `rule-proactive-codebase-inspector` (global scope).

It fires every 4 hours per project, randomly selects a file using weighted categories
(source 40%, docs 20%, tests 15%, config 10%, recently-modified 15%), reads and
analyzes it across multiple quality dimensions, and posts a suggestion only when a
concrete, actionable finding is discovered. See the rule file for the full logic.

---

## 4. Prompt Design

The supervisor receives a structured prompt when the hook fires:

```
You are performing a proactive codebase inspection for project {{project_name}}.

Your job is to randomly select a section of the codebase and analyze it for
potential improvements that the team may not have noticed. You are NOT looking
for every possible issue — only findings that are genuinely worth the team's
attention.

## Selection
1. Use shell tools to list project files (respecting .gitignore)
2. Randomly select a file using the weighted category approach
3. For large files, select a focused section

## Analysis
Read the selected content carefully. Consider:
- Is there a real problem here, or is this an intentional design choice?
- Would fixing this meaningfully improve the project?
- Is this actionable — can you describe a concrete next step?

## Decision Rules
- SKIP trivial style nits (these are not worth a suggestion)
- SKIP things you're uncertain about (when you'd need more context)
- SKIP if the file looks well-maintained and follows conventions
- SUGGEST only if you found something concrete and actionable

## Output
If you find something worth suggesting:
- Create a suggestion with type "task" (needs code work), "warning" (risk),
  or "context" (informational)
- Include the specific file path and line range
- Explain what you found and why it matters
- Propose a concrete action

If nothing notable: just note what you inspected and that it looked fine.
```

---

## 5. Configuration

The inspector respects project-level configuration overrides:

| Setting | Default | Description |
|---------|---------|-------------|
| `interval_hours` | 4 | Hours between inspections |
| `enabled` | true | Can be disabled per-project |
| `categories` | all | Which file categories to inspect |
| `max_file_lines` | 300 | Threshold for sectional reading |
| `section_size` | 150 | Lines to read when sectioning |
| `history_retention_days` | 30 | How long to keep inspection history |
| `severity_threshold` | low | Minimum severity to post suggestions |

These are configured via the rule's YAML frontmatter or project config overrides.

---

## 6. Integration Points

### With Existing Suggestion UI
- Reuses `SuggestionView` (Accept/Dismiss buttons in Discord)
- Accepted task suggestions create tasks via `CommandHandler`
- Hash-based deduplication prevents repeat suggestions for the same finding

### With Memory System
- Inspection history stored in project memory directory
- Findings that are accepted feed back into project knowledge base
- Dismissed findings are tracked to avoid re-suggesting

### With Reflection Engine
- Inspector hook runs go through standard hook reflection (`hook.completed` trigger)
- Reflection can evaluate whether the inspector's findings are high quality

### With Rule System
- Ships as a global default rule (installed on first run)
- Can be customized or disabled per-project by editing the rule
- Follows standard rule → hook → execution pipeline

---

## 7. Invariants

- Inspector never modifies project files — it is read-only analysis
- Inspector failures do not affect other hooks or task processing
- Token usage is bounded by standard hook limits + cooldown
- An inspection that finds nothing is not a failure — it's a valid outcome
- Deduplication prevents the same suggestion from appearing repeatedly
- The inspector prompt explicitly instructs the LLM to prefer silence over noise
