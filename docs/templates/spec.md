---
title: <title>
status: draft
spec_kind: implementation
---

# <title>

Choose `spec_kind: design` for goals, alternatives and architecture that still
need implementation planning. Choose `implementation` only after inspecting
the current code. Approval of either kind starts deep-high spec ingestion:
design creates one implementation-spec authoring task; implementation creates
the phased work graph. The agent checks the content as well as the field.

## Goal and boundaries

State the desired behavior, defaults for open questions and exclusions.

## Approved specs and goal accounting

Identify approved specs this document supersedes or overlaps, including the
approved design behind an implementation spec. Record each source path or
review ID, approved revision and approval evidence. Search related approved
specs even when this draft does not declare `Supersedes`. If none apply, state
that and what you checked.

List every goal of each predecessor, including goals outside the overlapping
area. A goal retained elsewhere still needs a `kept` row pointing there.

| Approved spec / revision / goal | Disposition (`kept`, `changed`, `dropped`) | Replacement section or retained spec | Reason and behavior change | Jack's sign-off for a drop |
|---|---|---|---|---|
| <source and goal> | <disposition> | <evidence> | <reason> | <decision reference, date and revision; or pending> |

`kept` preserves the goal; `changed` explains how the replacement differs.
Removing an outcome is `dropped`, even if labelled a simplification, exclusion
or deferral inside a changed goal. Each drop needs Jack's explicit sign-off
identifying the goal and proposed replacement revision. Pending sign-off
blocks approval and implementation of the affected work. Flag every drop in
the review, including signed-off drops. A delegated approval or an unrelated
approval is not sign-off. Follow the [review checklist](../guides/reviews.md#spec-supersession-checklist).

## Current code and changes

Name the relevant files, functions, schema and integration points. A design
spec can describe alternatives here; an implementation spec must select one
and explain the concrete change against the code.

## Phases and ownership

One epic per phase or deliverable, with child work split by file/module
ownership. Describe each real prerequisite and its reason. Dependencies connect
children, never epics; independent children start together.

## Acceptance and rollout

Name the tests owned by each child, exact check commands, migration/rollout
constraints and compatibility defaults. Each child must carry its section,
files, checks and this spec's path without relying on sibling descriptions.

Submit the document with `aq review submit --task-id <held-task> --file <draft>
--kind spec --title "<title>"`. Do not commit standalone authored review drafts.
