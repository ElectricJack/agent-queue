# Task 9a Independent Review

## Verdict

**FAIL.** Open findings: **Critical 1, Important 3, Minor 1**. Task 9a cannot pass until the app-auth Git subprocess boundary is credential-safe and the CI/config trust gaps below fail closed.

## Critical findings

### 1. App-auth Git gives a worker-controlled Git process and hooks the installation token FD and the daemon environment

**Location:** `src/git/manager.py:309-314`, `src/git/manager.py:2862-2927`, and `src/git/askpass_fd.py:10-23`.

**Failure scenario:** `apush_oid_with_app_auth()` runs `git push` from the supplied checkout, inherits `GitManager._SUBPROCESS_ENV` (the complete daemon environment minus only `GH_TOKEN` and `GITHUB_TOKEN`), exposes the token FD number in that environment, and makes the seekable token file descriptor inheritable by Git. It does not disable checkout hooks or isolate local/global/system Git configuration. A worker who controls that checkout can install `.git/hooks/pre-push`; Git executes it before transport, and the hook inherits both `AQ_GIT_APP_TOKEN_FD` and the live descriptor. Because the backing file is seekable, the hook can seek to offset zero and read the installation token before askpass. It can also read every unrelated daemon secret retained in `_SUBPROCESS_ENV`. Mutable Git configuration (`url.*.insteadOf`, proxy/credential-helper settings, and related transport settings) is likewise still authoritative despite the apparently frozen literal HTTPS URL, while `answer_prompt()` returns the token for any password prompt without validating the prompt target.

A focused local diagnostic used a temporary repository, temporary bare remote, dummy token, and a pre-push hook. With the same inherited-FD/environment shape, the hook successfully read the complete dummy installation token: `hook_captured_token=True`. No network or live credential was used.

Cleanup is also incomplete on exceptional paths: only `asyncio.TimeoutError` kills the direct Git PID. Task cancellation or another exception after spawn leaves the privileged Git process running, and timeout cleanup does not terminate/reap its process group or descendants (`src/git/manager.py:2917-2925`). Closing the parent's temporary file does not revoke copies already inherited by Git or its children.

**Requirement impact:** This violates the daemon-only credential boundary, the no-child-descendant/no-unrelated-environment leakage requirements, strict GitHub.com/exact-repository transport authority, and cancellation/timeout cleanup guarantees. The installation token carries `contents:write` and other privileged repository permissions, so exploitation escapes ordinary worker authority.

**Minimal remediation:** Execute privileged pushes only through a daemon-owned, configuration-isolated Git context. Supply a minimal allowlisted environment; disable hooks (`--no-verify` and a pinned null `core.hooksPath`), credential helpers, URL rewrites, proxies, and ambient system/global/local Git configuration; and ensure the actual destination cannot be rewritten from the validated `https://github.com/<exact-full-name>.git`. Replace the generally inherited seekable token FD with a broker/one-shot mechanism that releases the secret only to the intended askpass exchange and cannot be read by arbitrary Git descendants. Start the child in a controlled process group and terminate/reap the whole group on cancellation, timeout, and every post-spawn exception. Add real temporary-Git regressions with a malicious pre-push hook/config plus cancellation and surviving-descendant probes; the current mocked argv/env test cannot establish this boundary.

## Important findings

### 2. CIService does not bind evidence to the current live operation, project repository, or current candidate revision

**Location:** `src/integration/ci.py:330-417`. The established parent locking/context contract is visible at `src/integration/parent_completion.py:1121-1153`; current root identity is stored at `src/database/tables.py:1892-1948` and `src/database/tables.py:1972-1997`.

**Failure scenario:** The parent path locks an operation by caller-supplied ID and a checkpoint by task ID, then checks only target kind/task, checkpoint generation/head, and required-check policy. It never derives/locks the hierarchy project, verifies `operation.episode_id == checkpoint.episode_id`, requires a live operation state, or binds `checkpoint.repository_id` to `trust.canonical_repository_id`. Thus a completed/old episode operation can be reported green for the current checkpoint, and authenticated checks read from repository A can be persisted for a parent targeting repository B when the commit SHA and check policy happen to match. The Task 6 consumer has no repository field in the persisted evidence with which to repair that missing proof.

The candidate path does not load or lock `integration_batches` at all. It therefore does not require the batch's designated repository to match the trust manifest, `batch.current_revision == subject.revision`, a compatible live batch lifecycle, `operation.episode_id == batch.id`, or a live operation state. An older candidate row left in `built`/`testing`, or an operation already completed/cancelled, is accepted as green. Both paths also skip the hierarchy-project lock and use operation-first row ordering, unlike the established hierarchy -> checkpoint/batch -> current operation ordering.

**Requirement impact:** This breaks exact live subject revalidation and repository trust binding. Cross-repository or stale operation/revision evidence can be appended as trusted success and returned as `green`, despite `ParentCISubject`/`CandidateCISubject` being syntactically disjoint.

**Minimal remediation:** Resolve the target task/batch and project first, acquire the hierarchy-project lock, then lock checkpoint/batch, its exact current episode/revision, and the matching operation in the established order. Require enabled project/designated canonical repository identity, exact current episode, permitted live operation/batch states, current batch revision, exact candidate head/state, and frozen check-set equality before and through append. Add wrong-designated-repository, old-episode/completed-operation, and extant-old-revision tests rather than only testing a nonexistent revision number.

### 3. Malformed exact-name trusted records are discarded before newest-record ordering, allowing fallback to an older success

**Location:** `src/integration/ci.py:140-160`, `src/integration/ci.py:548-562`, and `src/integration/ci.py:653-683`.

**Failure scenario:** `select_trusted_attestation()` includes an exact-name, exact-attestation-App record in the ordering set only when its `id` is already a strict positive integer. An otherwise matching record with a missing, boolean, nonpositive, or malformed ID is silently ignored, so an older well-formed success can be selected. Required CI check selection has the same filter-before-order behavior. Publication reuse also drops malformed-ID exact-name records before choosing `max()`, allowing an older byte-identical success to be reused even though the newest trusted record cannot be established safely.

**Requirement impact:** The contract requires strict malformed-input rejection, numeric newest trusted record selection, invalid-newest fail-closed behavior, and publication reuse that cannot mask a newer invalid record. Ignoring a trusted-identity record whose ordering identity is malformed converts “cannot establish newest” into success.

**Minimal remediation:** First partition records by exact check name and strict trusted App identity. If any such candidate lacks a strict positive numeric record ID, fail closed (and never reuse an older publication). Only after validating every ordering ID should the code choose the numeric maximum and validate that record's state/payload. Apply the same rule to required CI checks and add missing/bool/string/nonpositive-ID regressions for read and publication paths.

### 4. GitHub App configuration accepts and exposes forbidden inline secret fields

**Location:** `src/config.py:1422-1447`, `src/config.py:2980-3004`, `src/config_editor.py:219-236`, and `src/commands/system_commands.py:197-229`.

**Failure scenario:** The loader extracts four known keys from a mapping but does not reject additional fields; a non-mapping `github_app` is silently treated as absent. Configuration containing `integration.github_app.private_key: <bytes>` or `token: <bytes>` therefore passes `load_config()` because those keys are ignored by the dataclass, while the secret remains durably present in the YAML. The generated dataclass JSON schema does not set `additionalProperties: false`, so config-editor clients are not told to reject it. `get_config` then returns the raw YAML mapping verbatim, including the ignored secret. The existing test proves only that the loaded dataclass lacks a `private_key` attribute; it does not prove the file/update/get-config round trip rejects or redacts secret material.

**Requirement impact:** This violates the rule that daemon config stores only client/app/installation IDs and a private-key path, never key/token bytes, and it defeats the requested config round-trip/redaction boundary.

**Minimal remediation:** Make `integration.github_app` an exact mapping: reject non-mappings and every key outside `{client_id, app_id, installation_id, private_key_path}`, with explicit rejection of token/key/body-like fields. Mark the schema object closed to additional properties. Default-deny or redact forbidden secret material in raw config reads and reject it in update validation. Add load, dry-run update, persisted round-trip, schema, and `get_config` tests with sentinel key/token values.

## Minor notes

1. `task-9a-report.md:107` records a bare multi-file `pytest` invocation contrary to the repository's `aq test` rule, and the final affected-area runs still report 11 warnings (`task-9a-report.md:162-167`). The report disclosed the command mistake rather than concealing it, but both remain process/quality debt.

## Strengths

- The App JWT uses RS256 with the specified backdated `iat`, bounded `exp`, and client ID issuer; token minting verifies `/app`, narrows to one numeric repository and the requested permissions, and verifies numeric repository ID/full name before use (`src/git/github_app.py:156-220`).
- REST calls pin the requested media type/API version, bound response bodies and pagination, constrain pagination to `https://api.github.com`, and expose safe typed error text rather than raw response bodies.
- The private-key provider opens with no-follow semantics where supported and checks regular-file type, owner, owner-readability, and no group/world access (`src/git/github_app.py:64-83`). The askpass helper is committed executable.
- Trust and attestation models are frozen and forbid unknown payload fields; canonical bytes, external digest, strict integer IDs, lowercase SHA, distinct App IDs, exact ordered checks, suite coverage, and successful workflow attempts are substantially implemented.
- Parent and candidate subject types are disjoint, caller dictionaries are rejected, conclusive red evidence is stored without being returned as green, and the existing append-only evidence table/guards are reused rather than duplicated.
- The actual trust-document path is reserved by the existing delivery-diff/tree guard while only an inactive example manifest is shipped.

## Checks performed

- Read the complete Task 9a brief, shared Task 9 requirements, both CI preflights, implementation report, and packaged `5d8f84cc..5afb085b` diff once.
- Inspected the named existing Git subprocess environment, reserved-path guards, Task 6 parent context/consumer, Task 7 evidence accounting, integration batch/candidate/operation tables, append-only migration guards, and raw config editor/get-config seams.
- Reviewed the official GitHub App token, check-run, workflow-run, and rate-limit documentation to confirm the requested repository/permission response, producer/suite IDs, workflow/check-suite/run-attempt fields, and header semantics.
- Ran one focused local, temporary-Git diagnostic for the inherited-FD hook risk. Result: `hook_captured_token=True`. No live credential, network request, forge mutation, operator database, daemon, index, or runtime source was touched.
- Did not rerun any reported test suite.

## Final assessment

**FAIL — Critical 1, Important 3, Minor 1.** A PASS requires zero open Critical/Important findings.
