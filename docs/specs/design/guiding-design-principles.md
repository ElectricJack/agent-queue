---
tags: [design, principles]
---

# Guiding Design Principles

<!-- aq:historical -->
> **Design record — not current documentation.** A spec states the behaviour
> intended when it was approved; it is written before the code and is not
> revised to track it. Where this page and the code disagree, the code is right.
> Start at [the documentation home](../../README.md) for what AQ does today, and
> see [historical material](../../history/README.md) for how this material is
> organised.

These are the core principles behind Agent Queue. When making design decisions,
trade-offs, or implementation choices — refer here.

See also: [playbooks](playbooks.md), [vault](vault.md), [memory-plugin](memory-plugin.md), [memory-scoping](memory-scoping.md),
[profiles](profiles.md), [self-improvement](self-improvement.md), [agent-coordination](agent-coordination.md)

---

## 1. Human-readable files are the source of truth

If a human can't read it, it's a derived artifact. Configuration, knowledge,
automation rules, and agent behavior are all authored as plain text files. Runtime
systems — databases, indices, compiled formats — are caches that can be rebuilt
at any time. When the file and the cache disagree, the file wins. Data flows one
direction: files to runtime, never the reverse.

Applied in: [vault](vault.md) (vault as source of truth), [profiles](profiles.md) (markdown profiles
synced to DB), [playbooks](playbooks.md) (markdown compiled to JSON).

## 2. Everything is visible and editable

The system has no black boxes. Every piece of accumulated knowledge, every
rule it follows, every decision it has made can be found, read, and changed by a
human. If the system learned something wrong, someone can fix it with a text
editor. Transparency is not a feature — it is a prerequisite for trust.

Applied in: [vault](vault.md) (Obsidian integration, vault structure),
[agent-coordination](agent-coordination.md) (coordination playbooks are readable markdown).

## 3. Structure guides, intelligence decides

The system provides agents with defined processes — what to consider, in what
order, with what context. But the agents provide the judgment. Structure without
intelligence is brittle automation. Intelligence without structure is
unpredictable. The goal is processes that are flexible enough to handle novel
situations but defined enough to be understood and debugged.

Applied in: [playbooks](playbooks.md) (directed graphs of LLM decision points),
[agent-coordination](agent-coordination.md) (workflow stages structure multi-agent collaboration).

## 4. The system improves with use

Every task execution should leave the system better prepared for the next one.
Agents reflect on their work, distill patterns, and remember what they learn.
A system that doesn't get smarter over time is just an expensive way to run
scripts. Self-improvement is the core value proposition — not a nice-to-have.

Applied in: [self-improvement](self-improvement.md) (the full loop), [memory-scoping](memory-scoping.md) (scoped
memory accumulation), [playbooks](playbooks.md) (playbook-driven insight extraction).

## 5. Reduce human effort, don't eliminate human judgment

The goal is less human involvement, not zero. Some decisions require judgment the
system hasn't earned yet. Trust is built incrementally as the system proves
itself — not assumed up front. When in doubt, surface for review rather than
act autonomously.

Applied in: [playbooks](playbooks.md) (human-in-the-loop nodes), [agent-coordination](agent-coordination.md)
(human merge gates, review cycles).

## 6. Specificity wins

When the same question has answers at different levels of scope, the most specific
answer wins. A project convention overrides a general default. Local knowledge
outranks global knowledge. The system layers from broad to narrow and never forces
a generic answer when a specific one is available.

Applied in: [memory-scoping](memory-scoping.md) (scope hierarchy, override model, multi-scope
query weighting).

## 7. Communicate through events, not direct coupling

Subsystems don't call into each other. They emit events and respond to events.
This makes behavior observable, extensible, and traceable. New capabilities
subscribe to existing events without modifying existing code. Any behavior in
the system can be understood by following the event chain.

Applied in: [playbooks](playbooks.md) (EventBus triggers, cross-playbook composition via
events), [agent-coordination](agent-coordination.md) (scheduler and playbook communication through
commands and events).

## 8. Plugins own their dependencies

A component brings its own storage and lifecycle. It doesn't reach into another
component's infrastructure or assume resources it doesn't manage. This keeps
components replaceable, testable, and independently understandable. Tight
coupling between subsystems is a design failure, not a convenience.

Applied in: [memory-plugin](memory-plugin.md) (memory plugin v2 owns Milvus via memsearch
fork, no PostgreSQL dependency).

## 9. Simple interfaces, smart routing

The caller says what it needs. The system figures out how to get it. Whether
that means an exact lookup, a semantic search, or a compiled graph execution is
an implementation detail the caller never sees. Complexity belongs behind clean
interfaces, not in front of them.

Applied in: [memory-scoping](memory-scoping.md) (unified memory_get auto-routes between KV
and vector search), [playbooks](playbooks.md) (agents call tools without knowing which
backend serves them).

## 10. Favor fewer moving parts

When two approaches solve the same problem and one requires fewer systems,
dependencies, or storage backends — choose that one. Every additional moving
part is a maintenance burden, a failure mode, and a concept someone has to
understand. Consolidate where possible. Separate only when the benefit is clear.

Applied in: [memory-plugin](memory-plugin.md) (unified Milvus backend for both vectors and
KV instead of separate databases).
