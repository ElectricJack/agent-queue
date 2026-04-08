---
tags: [api, reference]
---

# API Reference

> **Spec:** [[specs/design/README|Design Specs]]

This section contains auto-generated API documentation from the Agent Queue source code. The documentation is extracted from docstrings, type annotations, and class definitions.

## Core Modules

| Module | Description |
|--------|-------------|
| [Models](models.md) | Data models — Task, Agent, Project, Hook, and enums |
| [Config](config.md) | Configuration loading and dataclasses |
| [Database](database.md) | SQLite persistence layer |
| [Orchestrator](orchestrator.md) | Core task and agent lifecycle management |
| [State Machine](state_machine.md) | Task state transitions and validation |
| [Scheduler](scheduler.md) | Proportional credit-weight task scheduling |
| [Event Bus](event_bus.md) | Async pub/sub event system |
| [Plan Parser](plan_parser.md) | Plan file parsing |
| [Hooks](hooks.md) | Hook engine for automation |
| [Main](main.md) | Application entry point |

## Adapters

| Module | Description |
|--------|-------------|
| [Base Adapter](adapters/base.md) | Abstract adapter interface |
| [Claude Adapter](adapters/claude.md) | Claude Code agent adapter |

## Chat Providers

| Module | Description |
|--------|-------------|
| [Anthropic](chat_providers/anthropic.md) | Anthropic API chat provider |
| [Ollama](chat_providers/ollama.md) | Ollama local LLM chat provider |

## Discord

| Module | Description |
|--------|-------------|
| [Bot](discord/bot.md) | Discord bot core |
| [Commands](discord/commands.md) | Discord slash commands |
| [Notifications](discord/notifications.md) | Discord notification system |

## Git

| Module | Description |
|--------|-------------|
| [Manager](git/manager.md) | Git operations manager |

## Tokens

| Module | Description |
|--------|-------------|
| [Budget](tokens/budget.md) | Token budget management |
| [Tracker](tokens/tracker.md) | Token usage tracking |
