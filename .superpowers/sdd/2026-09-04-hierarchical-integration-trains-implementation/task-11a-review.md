# Task11a independent review — 3de33b0a

/root/review_11a_astra: Spec compliance Needs fixes; task quality Needs fixes.
Critical0/Important5/Minor1.11b–11d transport/probe/cutover enforcement cannot be
verified in this foundation and remain assigned downstream, not silently waived.

## Important findings (verbatim)

1. **Allowed configuration fields still accept and expose inline secrets.**
`src/config.py:1536` validates the private-key reference only as a nonempty string; `src/config.py:1606` checks field names without validating their values. An inline PEM in `negative_private_key_path` passes both validators, and `src/commands/system_commands.py:220` subsequently returns it through `get_config`. An isolated diagnostic confirmed this leak. The generated schema also permits any string. Validate safe scalar reference/identity shapes at every boundary, including before and after substitution. The repository-name check at `src/config.py:1548` additionally accepts spaces, query strings, and embedded newlines; require valid exact owner/repository components.

2. **SQLite status reads do not share a consistent database snapshot.**
`src/integration/status.py:75` calls SQLAlchemy `conn.begin()`, but the existing SQLite engine uses legacy transaction behavior without an explicit `BEGIN` for SELECTs (`src/database/engine.py:264`). A diagnostic using that engine confirmed the underlying connection remains outside a transaction after begin plus SELECT. Concurrent commits can therefore produce mixed project, batch, membership, and evidence state. Establish an actual SQLite read transaction and test an intervening writer.

3. **Parent readiness assumes receipt uniqueness and ignores established receipt applicability.**
`src/integration/status.py:367` selects every receipt for a child/parent pair and calls `scalar_one_or_none()` at line 375. Receipt history permits multiple rows, so legitimate repeated deliveries can crash project status and task explain. A single historical receipt is also accepted without current head, repository, branch, episode, disposition, or carry-forward validation; FAILED children are silently skipped at line 363. These checks contradict the existing readiness contract in `src/integration/parent_completion.py:370`, especially lines 491–540. Reuse or extract that connection-owned applicability logic and map its outcomes into the shared blocker vocabulary.

4. **Repair-budget blockers cannot recognize real persisted policies.**
`src/integration/status.py:454` and line 577 read `attempts`/`max_attempts` from stage policy. Actual stages persist `RepairPolicy`, whose limits are `primary_attempts` and `debug_attempts` (`src/integration/repair.py:212`, `src/integration/repair.py:331`). Consequently, genuine exhausted budgets produce no blocker. Deadline exhaustion is also never evaluated. Use the typed policy and current active stage, with focused exhausted-attempt/deadline fixtures.

5. **The no-mutation test does not measure service mutations.**
`tests/test_integration_controls.py:307` and line 322 read SQLite `total_changes()` on separate connections. File-backed engines use `NullPool`, so both fresh connections report zero regardless of writes performed by the service; the assertion at line 329 is ineffective. Replace it with durable before/after state comparison or observation/rejection of mutating SQL during service execution.

## Minor

task-11a-report.md241 reports eleven warnings without their sources/disposition.
Record categories and distinguish inherited warnings from new; passing is not pristine.

## Evidence and strengths

Conn-owned CAS/audit primitives44; dual-dialect immutable guards migration200 and
pre-DDL refusal269; handler920 nested redaction; status545 absent-preflight blocking.
Reviewer checked SQLite engine, serialization/schema, receipt cardinality/applicability
and repair policy seams. Isolated diagnostics confirmed absent SQLite BEGIN and PEM
serialization. No reported suite rerun, edits, Git reconstruction, network or operator
mutations.
