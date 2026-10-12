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
