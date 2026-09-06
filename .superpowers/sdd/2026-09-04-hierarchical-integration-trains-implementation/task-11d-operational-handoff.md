# Task11d operational handoff

Read operational-scope-override.md first, then task-11d-brief.md, then the reviewed task-11c-report.md for actual command signatures. The override supersedes the original security/probe/recovery requirements. No whole-plan or11b report read is needed.

## Precise delivery

- Handcrafted `aq integration` group: status, flush, enable, waive-history, resume, abort, retry-cleanup. All requests through existing generic execute client, shared JSON/brief output and errors. Register before auto-command registration; no duplicate operation implementation in CLI.
- Project repository/policy configuration options must reach the guarded11c edit_project fields; inspect existing `project set` generic transport and add only missing options. Rollout modes go exclusively through enable, including disabled/drain requests.
- Doctor read-only operational checks consume11c status/preflight state, expose disabled/desired/effective/draining, functional config/schema blockers, stuck human-required operation/cleanup when relevant. No `probe`, no new credential/protection inspection/certification, no automatic fixes or migrations. Keep existing unreviewed_prs check.
- Guide exact actual command/options and configuration shapes; no pseudocommands. Include schema check then operator-only upgrade outside workers, daemon restart, policy/artifact/class/profile bindings, observe/hierarchy/train, periodic sealed all-eligible-root sweeps, recursive children-first integration and parent verification, no overlapping train per project, ephemeral singleton branches, tested exact-OID main promotion/no post-main audit, cleanup, human escalation/controls and safe disable/drain rollback. Credentials/config changes requiring restart and project changes requiring disabled/drained state need explicit instructions.
- Clearly disclose security certification/probes and broad recovery verification are deferred. Existing runtime authentication/CI/OID/irreversible-write protections remain enforced. No actual production deployment or enablement is part of this task.

## Database backend change limitation

Read-only controller inspection confirms `src/database/migrate_sqlite_to_pg.py` omits integration tables and `scripts/migrate_sqlite_to_pg.py` is an older independent copier. This is not needed to upgrade an existing installation in place on its current backend. Do not expand this operational task into the deferred copy/recovery Task12; warn explicitly in the guide that switching a populated SQLite installation to PostgreSQL with either copier is unsupported for this release and would lose integration state. Existing SQLite or PostgreSQL schema upgrade is distinct from changing database backends. Keep this as an explicit handoff limitation, not a claim that the whole refactor is unconditionally rollout-ready.

## Evidence

Focused CLI transport/JSON/brief/errors and doctor read-only/no-fix tests, guide command parity, applicable project option tests. Use `aq test` for multiple files, one affected-area gate, no broad suite or skipped recovery gates. Tests may fake HTTP/adapters and use scratch DBs only. Ruff changed Python files; git diff --check. Report exact commands/output and downstream instructions in task-11d-report.md. No generated client edits unless public DTO/router surface actually changes.
