# Generated task instruction templates

Generated task descriptions and automatic repair/reopen feedback use editable Markdown
templates under `<data_dir>/vault/templates/tasks/`. Code supplies observed facts and selects
the appropriate template for the deterministic state; wording and worker instructions live
in the template. Template selection does not change routing, budgets, authorization, task
identity, publication fences, or acceptance gates.

Shipped defaults live in `src/prompts/task_templates/`. Vault initialization copies missing
files only; upgrades and repeated initialization preserve operator edits. The daemon binds
the template directory from its loaded `data_dir` before starting subsystems. The binding
is local to its async context and inherited by child tasks, so concurrent daemon/test
contexts do not share configuration. Standalone builders without a daemon binding use
shipped defaults. Missing installed files also use the shipped default; unreadable files
and invalid placeholders fail visibly rather than silently discarding operator edits.

Rendering reads the file afresh each time. A supervisor can edit the installed template
through its granted file tools; the next generated description uses the new wording
without a deploy, reload, or restart. Existing task descriptions remain historical snapshots.
Existing file capabilities admit the task-template directory for local operators and
elevated supervisor sessions. Worker and playbook principals gain no additional path access;
other vault directories and escaping symlinks remain outside this exception. Both file-tool
path validators apply the same boundary. No new command or capability grant is introduced.

Templates use `{{variable}}` placeholders. Replacement is one pass, with no evaluation,
escaping, recursive interpolation, or executable template language. Facts containing braces,
Markdown, check summaries, or code are preserved verbatim. An unknown placeholder raises a
named error. Whitespace is preserved, including paragraph separators in composed sections.
The development repair generation marker stays a factual code-owned prefix because the
existing retry budget reads it. Wording edits cannot reset that budget. Some prompts compose
optional sections (conflict details, CI state, prior progress); each
instruction section remains independently editable. Titles and factual diagnostic labels
can remain in code. The inventory and editing guide is
`docs/guides/generated-task-templates.md`.
