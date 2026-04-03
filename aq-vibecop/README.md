# aq-vibecop

Vibecop static analysis plugin for [Agent Queue](https://github.com/ElectricJack/agent-queue). Integrates [vibecop](https://github.com/bhvbhushan/vibecop) — a deterministic AI code quality linter — so agents can self-check their code changes.

## What is Vibecop?

Vibecop detects antipatterns commonly introduced by AI coding agents using AST-based pattern matching (no LLM calls). It includes 22+ detectors across quality, security, correctness, and testing categories:

- **Quality:** god-function, n-plus-one-query, dead-code-path, excessive-any
- **Security:** sql-injection, token-in-localstorage, insecure-defaults
- **Correctness:** unchecked-db-result, undeclared-import, mixed-concerns
- **Testing:** trivial-assertion, over-mocking

Supports JavaScript, TypeScript, TSX, and Python.

## Requirements

- Agent Queue with plugin support
- Node.js >= 20
- vibecop (`npm install -g vibecop` or use via npx)

## Installation

```bash
# From the agent-queue directory
aq plugin install /path/to/aq-vibecop

# Or from a git URL
aq plugin install https://github.com/your-org/aq-vibecop
```

## Tools

### vibecop_scan

Scan a directory for code quality issues.

```
vibecop_scan(path="./src", diff_ref="main", severity_threshold="warning")
```

### vibecop_check

Check specific files (faster than a full scan).

```
vibecop_check(files=["src/handler.py", "src/utils.py"])
```

### vibecop_status

Check if vibecop is installed and working.

```
vibecop_status()
```

## Configuration

```bash
aq plugin config vibecop vibecop_path=/usr/local/bin/vibecop
aq plugin config vibecop scan_timeout=120
aq plugin config vibecop default_severity=error
```

| Key | Default | Description |
|-----|---------|-------------|
| `node_path` | (system) | Path to Node.js binary |
| `vibecop_path` | (auto-detect) | Path to vibecop binary |
| `default_severity` | `warning` | Default severity threshold |
| `auto_install` | `false` | Auto-install vibecop if missing |
| `scan_timeout` | `60` | Command timeout in seconds |

## License

MIT
