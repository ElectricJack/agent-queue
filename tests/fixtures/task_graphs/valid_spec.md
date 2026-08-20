---
tags: [spec, project]
project: p1
status: approved
---

# Messages Table

## 1. Problem

Prose the humans and agents read.

## 3. Schema

The section spec_ref entries point at.

## 4. Queries

CRUD and delivery queries.

```aq-graph
version: 1
defaults: { profile: coding }
parent: { title: "Messages table" }
nodes:
  - key: schema
    title: Add messages table and migration
    acceptance: ["migration applies on sqlite and postgres"]
    context: [{ type: spec_ref, section: "3. Schema" }]
  - key: queries
    title: Message queries module
    acceptance: ["queries covered by unit tests"]
    needs: [schema]
    context: [{ type: spec_ref, section: "4. Queries" }]
```
