---
tags: [spec, prompts, context]
---

# Prompt Builder

## Purpose

Single entry point for all prompt assembly in the system. Replaces scattered string concatenation across orchestrator, [[specs/adapters/claude]], chat agent, and agent_prompting.

## Concepts

### Five Layers

Every prompt is assembled from up to 5 ordered layers:

1. **Identity** — Who is the LLM acting as? Loaded from a prompt template file in `src/prompts/`.
   - `supervisor` — the Discord-facing [[specs/supervisor|Supervisor]] (from `chat_agent_system.md`)
   - `task-agent` — a Claude Code agent executing a task
   - `hook-executor` — the [[specs/supervisor|Supervisor]] reasoning about a hook result (from `hook_context.md`)

2. **Project Context** — What project is this for? Pulled from the memory system:
   - Project profile (from `profile.md`)
   - Project documentation (CLAUDE.md, README.md)
   - Falls back to empty string if memory unavailable

3. **Relevant Rules** — Deprecated. Previously loaded from `RuleManager` via `load_relevant_rules(query)`. Rules have been replaced by playbooks and vault memory. `load_relevant_rules()` is now a no-op.

4. **Specific Context** — What is the LLM doing right now? Arbitrary named context blocks:
   - `task` — task description
   - `upstream` — completed dependency summaries
   - `task_depth` — depth-aware execution rules (plan generation, controlled splitting, execution focus)
   - `hook` — hook trigger data and context step results
   - `system_metadata` — workspace path, project name, branch
   - `role_instructions` — agent profile system_prompt_suffix

5. **Tools** — What tools are available? A list of JSON Schema tool definitions.

### Template Loading

PromptBuilder loads templates from `src/prompts/*.md` files with YAML frontmatter. It parses frontmatter for metadata (name, variables, category) and renders the body with `{{variable}}` substitution. This absorbs the functionality of `prompt_registry.py` and `prompt_manager.py`.

Templates are cached after first load. `reload()` forces re-read from disk.

### Output

`build()` returns a tuple of `(system_prompt: str, tools: list[dict])`.

- `system_prompt` is the concatenation of all non-empty layers in order, separated by `\n\n---\n\n`
- `tools` is the list of tool definitions set via `set_core_tools()` or `set_tools()`

For task execution prompts (identity="task-agent"), the output is a single prompt string (no separate system prompt), since it goes to the Claude Code CLI as a flat prompt. `build_task_prompt()` returns `str` instead.

## Interfaces

### Constructor

`PromptBuilder(project_id: str | None = None, memory_manager: Any | None = None, rule_manager: Any | None = None)`

- `project_id` scopes project context and rule loading
- `memory_manager` is optional; if None, layer 2 is empty
- `rule_manager` is optional; if None, layer 3 is empty

### Methods

- `set_identity(name: str, variables: dict | None = None)` — Load identity template, render with variables
- `load_project_context()` — async, pulls from memory_manager if available
- `load_relevant_rules(query: str)` — async, queries RuleManager for applicable rules (project + globals). Without memsearch, loads all rules. Gracefully degrades to empty on errors.
- `add_context(name: str, content: str)` — Add a named context block to layer 4
- `add_context_section(name: str, data: dict)` — Add structured context rendered as markdown. When `name="task_depth"`, dispatches to depth-aware template selection (see below)
- `set_core_tools(tools: list[dict])` — Set the tool definitions for layer 5
- `set_tools(tools: list[dict])` — Alias for set_core_tools
- `build() -> tuple[str, list[dict]]` — Assemble and return (system_prompt, tools)
- `build_task_prompt() -> str` — Assemble and return flat prompt string for task execution
- `get_template(name: str) -> str | None` — Load and return raw template body (for backward compat)
- `render_template(name: str, variables: dict | None = None) -> str` — Load, render, return template
- `reload()` — Force re-read templates from disk

### Depth-Aware Task Prompting

When `add_context_section("task_depth", data)` is called, PromptBuilder selects the appropriate execution rules template based on the `depth` and `max_depth` values in `data`:

- Root depth (0): loads `plan-structure-guide` template with `max_steps` variable
- Intermediate depths (0 < depth < max_depth): loads `controlled-splitting` template with `current_depth` and `max_depth` variables
- Max depth (depth >= max_depth): loads `execution-focus` template (no variables)

The rendered template is added as an `execution_rules` context block.

### Template Rendering

- Loads `.md` files from `src/prompts/` directory
- Parses YAML frontmatter (between `---` delimiters)
- Substitutes `{{variable_name}}` with provided values
- Missing required variables raise `ValueError` in strict mode, use empty string in non-strict
- Templates are cached per PromptBuilder instance

## Invariants

- Layer order is always 1 → 2 → 3 → 4 → 5
- Empty layers are omitted (no blank sections in output)
- Multiple `add_context()` calls append in call order within layer 4
- `build()` can be called multiple times (idempotent)
- Template loading never raises on missing files — returns None
- PromptBuilder is not thread-safe (single-use per prompt assembly)
