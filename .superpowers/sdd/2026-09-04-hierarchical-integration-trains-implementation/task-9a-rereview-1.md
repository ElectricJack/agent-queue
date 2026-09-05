# Task 9a Re-Review — Fix Round 1

## Finding verdicts

1. **App-auth Git gives a worker-controlled Git process and hooks the installation token FD and the daemon environment — NOT ADDRESSED.** The worker checkout, hooks, configuration, object import, exact destination, minimal environment, and process-group cleanup portions are addressed by the isolated bare repository and command construction in `src/git/manager.py:2891-3031` and `src/git/manager.py:3054-3118`. However, the credential channel still does not meet the required one-consumer boundary. The broker task begins writing the complete token to a pipe before Git is spawned (`src/git/manager.py:3033-3042`), and the pipe's read descriptor number is placed in Git's environment and passed directly to the top-level Git process (`src/git/manager.py:3043-3052`, `src/git/manager.py:3084-3093`). A POSIX descriptor passed this way is available to Git and its spawned process tree, not solely to the packaged password-prompt invocation. The username askpass invocation itself inherits the readable capability even though its current code elects not to consume it (`src/git/askpass_fd.py:10-18`). Any unexpected helper/descendant that drains the pipe first can capture the installation token and deny it to the intended password askpass. A descendant that retains the reader after the leader exits can also keep the writer blocked while the success path awaits it without a separate bound (`src/git/manager.py:3097-3098`). Disabling the currently known hook and credential-helper launch paths is valuable defense, but it does not turn an ambient process-tree capability into the explicitly required one-consumer broker.

   **Severity: Critical.** This leaves pre-populated daemon installation-token bytes readable outside the single intended credential consumer and fails the fix-round's express child-descendant isolation requirement. Preserve the approved FD-backed askpass contract, but make the inherited endpoint a request channel to a daemon broker that withholds the token until one validated password-prompt exchange from the packaged helper, serves it once, then zeroizes and closes both sides. Bound and cancel broker completion on every leader exit, and add a real descendant probe that attempts to drain/retain the channel while the legitimate password askpass must remain the only successful consumer.

2. **CIService does not bind evidence to the current live operation, project repository, or current candidate revision — ADDRESSED.** Both phases resolve without row authority, then acquire the hierarchy-project lock before locking and rechecking the project, subject, and exact episode operation (`src/integration/ci.py:333-380`, `src/integration/ci.py:383-533`). Parent validation now binds active project/mode, task and checkpoint repository, verifying state, generation, head, episode, operation ID/target/state, and the operation-frozen check policy (`src/integration/ci.py:386-457`). Candidate validation binds active project/mode, designated repository, batch lifecycle/current revision, exact candidate SHA/state, episode, operation ID/target/state, and root trust (`src/integration/ci.py:462-533`). Observation occurs outside transactions, and the second locked validation precedes the evidence append in the same transaction (`src/integration/ci.py:349-359`, `src/integration/ci.py:372-380`). The added tests exercise repository, old/completed/wrong operation, extant old revision, mutation during observation, and both-phase lock ordering; the reported `tests/test_integration_ci.py` run passed 32 tests.

3. **Malformed exact-name trusted records are discarded before newest-record ordering, allowing fallback to an older success — ADDRESSED.** Attestation selection, each required-check selection, and publication reuse now first match exact name plus strict trusted App identity, then reject a missing, boolean, string, zero, or negative record ID before numeric maximum selection (`src/integration/ci.py:142-164`, `src/integration/ci.py:666-700`, `src/integration/ci.py:780-816`). Records from another or unprovable App identity remain irrelevant noise. The three parameterized test families cover all four malformed-ID classes, and the report records 12 passing focused cases.

4. **GitHub App configuration accepts and exposes forbidden inline secret fields — ADDRESSED.** `integration.github_app` must now be a mapping with exactly the four non-secret fields, and validation runs on the merged raw configuration before environment substitution (`src/config.py:1450-1472`, `src/config.py:2613-2657`). Dataclass schemas are recursively closed while typed dictionaries retain a value schema (`src/config_editor.py:193-240`). Both dry-run and real editor candidates pass through `load_config` before persistence, and rejected candidates leave the file untouched (`src/commands/system_commands.py:253-325`). Raw config reads validate this section and return a generic value-free error on externally introduced forbidden material (`src/commands/system_commands.py:197-240`). Added tests cover non-mappings, token/key fields, rejected dry-run and real edits, schema shape, unchanged persistence, and sentinel-free raw-get errors.

## New breakage in the fix diff

None beyond the unresolved credential-channel portion of finding 1.

## Out-of-scope observations

None.

## Checks

- Read the prior review, fix report, and complete `ded1788e..989789de` review package once.
- Inspected the current privileged Git implementation/helper, CI authority and persistence path, raw config validation/editor commands, relevant table constraints, and the added focused tests.
- Did not rerun the implementer's reported suites. The report supplies focused RED/GREEN evidence and a final affected-area result of 384 passed with 11 warnings.

## Verdict

**Fix round: Findings remain open.** Open findings: **Critical 1, Important 0**. Finding 1 remains open because the inherited pipe is pre-populated with token bytes before any validated one-time askpass request, so arbitrary Git descendants can drain or retain it. Findings 2–4 are addressed, and no separate Critical/Important regression was found.
