---
tags: [spec, chat-providers, logged, llm, observability]
---

# LoggedChatProvider — Specification

Wraps any [[chat-providers/base|ChatProvider]] implementation. See also: [[specs/llm-logging]].

Defined in `src/chat_providers/logged.py`. Class: `LoggedChatProvider`.

A decorator (wrapper) that wraps any `ChatProvider` instance with timing and LLM logging. It delegates all `create_message` calls to the wrapped provider while recording request/response data to the `LLMLogger` subsystem.

This is used by the system to transparently add logging to whichever provider is active, without the provider needing to know about logging concerns.
