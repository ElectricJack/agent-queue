# aq-vibecop

Vibecop static analysis plugin for Agent Queue. Wraps the vibecop CLI to provide deterministic code quality scanning for AI agents.

## Structure

- `aq_vibecop/plugin.py` — Plugin class, tool/command registration, handlers
- `aq_vibecop/runner.py` — Async subprocess wrapper for vibecop CLI
- `aq_vibecop/formatter.py` — JSON-to-text output formatting
- `prompts/findings-summary.md` — Template for summarizing scan results (uses `$var` substitution)
- `prompts/pre-complete-check.md` — Agent guidance for running vibecop before task completion

## Development

```bash
# Install in dev mode (from aq-vibecop/ directory)
pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Lint
ruff check aq_vibecop/
```

## Key Patterns

- All CLI execution goes through `VibeCopRunner._run()` using `asyncio.create_subprocess_exec`
- Always use `--format json` when calling vibecop CLI
- Runner uses fallback chain: configured path -> npx -> global vibecop
- Findings are normalized in `runner._normalize_finding()` to handle format variations
- Formatter respects context window limits (`_MAX_DETAIL_CHARS`, `_MAX_SUMMARY_CHARS`)
- Rule injection via `execute_command("save_rule", ...)` — passive rule for prompt injection
- Rule is created on `initialize()` and removed on `shutdown()`, controlled by `enforce_vibecop_checkout`

## Tools Provided

| Tool | Purpose |
|------|---------|
| `vibecop_scan` | Scan directory, optional `--diff` for changed-only |
| `vibecop_check` | Check specific files |
| `vibecop_status` | Report installation status, version, detectors |

## Auto-Scan & Events

- **Task completion scan:** When `auto_scan_on_complete` is enabled (default), the plugin automatically scans the task workspace when a task completes, diffing against the base branch.
- **Weekly project scan:** A `@cron` job runs Monday 6 AM (configurable via `weekly_scan_schedule`) and scans all active projects. Creates tasks for error-severity findings.
- **Custom events:** Emits `vibecop.scan_completed` and `vibecop.findings_detected` for other plugins to consume.
- **Discord notifications:** Posts findings summaries to the project's Discord channel with error/warning/info counts and top findings.

## Config

Set via `aq plugin config vibecop key=value`:

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `node_path` | string | (system) | Path to Node.js binary |
| `vibecop_path` | string | (auto) | Path to vibecop binary |
| `default_severity` | string | warning | Severity threshold |
| `auto_install` | bool | false | Auto-install vibecop if missing |
| `scan_timeout` | int | 60 | Command timeout in seconds |
| `enforce_vibecop_checkout` | bool | true | Inject rule requiring vibecop scan before task completion |
| `auto_scan_on_complete` | bool | true | Auto-scan workspace on task completion |
| `weekly_scan_schedule` | string | 0 6 * * 1 | Cron schedule for weekly full scan |
