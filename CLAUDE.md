# CLAUDE.md

Agent Queue — task queue and orchestrator for AI coding agents on throttled plans. Discord-controlled, SQLite-backed, fully async Python.

## Quick Reference

- **Entry point:** `src/main.py` → orchestrator + Discord bot
- **Core files:** `orchestrator.py`, `command_handler.py`, `supervisor.py`, `database.py`, `models.py`
- **Subsystems:** `src/adapters/`, `src/discord/`, `src/git/`, `src/tokens/`, `src/chat_providers/`, `src/plugins/`, `src/memory.py`, `src/prompt_builder.py`, `src/tool_registry.py`, `src/rule_manager.py`, `src/reflection.py`, `src/schedule.py`
- **Specs:** `specs/` (source of truth — specs first, then code)
- **Config:** `~/.agent-queue/config.yaml`

## Development

```bash
pip install -e ".[dev]"
pytest tests/                          # all tests
pytest tests/test_orchestrator.py -v   # specific
./run.sh start                         # start daemon
```

- Python 3.12+, ruff (line-length 100, py312), pytest-asyncio (auto mode)
- Async-first: use `GitManager` async API (`a`-prefixed), never sync `subprocess.run()` in production
- Commands return `{"success": bool, ...}` dicts
- All state changes go through `CommandHandler` (single entry point for Discord + chat tools)

## Detailed Context

See **[profile.md](profile.md)** for full architecture, codebase map, design decisions, and conventions.
