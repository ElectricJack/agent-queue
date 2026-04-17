"""Service layer for agent-queue.

High-level orchestration services that compose core modules (memory, database,
adapters) into cohesive workflows.  Services are intended to be stateful,
long-lived objects that own scheduling, threshold tracking, and cross-cutting
concerns that don't belong inside individual modules.
"""
