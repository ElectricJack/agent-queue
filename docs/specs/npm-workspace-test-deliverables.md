# npm workspace test deliverables

Close-time evaluation never executes a declared command. A command-shaped test
deliverable needs recorded `--test` evidence and existing named paths inside the
held worktree; `--command` evidence alone cannot satisfy it.

The npm working-directory and selector semantics follow the
[npm workspace documentation](https://docs.npmjs.com/cli/v11/using-npm/workspaces/).

For `npm run test`, `npm run-script test`, and `npm test`, one explicit workspace
selector changes the base directory for test paths. Support `-w VALUE`,
`-wVALUE`, `--workspace VALUE`, and `--workspace=VALUE` before the script argument
separator `--`, either before or after the npm subcommand. Resolve the selector
as the exact relative directory or package name of a literal entry in the root
`package.json` workspace list. An optional `./` and trailing slash on a directory
selector are allowed. The root and workspace manifests must be valid JSON
objects, and the workspace must exist inside the worktree.

Check path-like script arguments after `--` against that workspace. Missing
files, absolute or parent-traversing selectors/arguments, escaped symlinks,
unconfigured directories, and unresolved or ambiguous selectors are unmet even
when identical test evidence was recorded. Unsupported workspace globs, parent
selectors, multiple/all-workspace selection, extra npm options, and compound
shell commands are conservatively unmet. This bounded parser does not infer a
working directory for arbitrary shell commands.

Workspace commands require an exact whitespace-insensitive match for the
declared command in recorded test evidence. Per-file substring
evidence from other commands cannot establish workspace execution. Ordinary
root commands, `aq test` directories and pytest node selectors retain their
existing evaluation behavior, including evidence for suites run separately.
