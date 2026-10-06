# Worker pane usage exhaustion

Task: `vivid-stone-92`. Extends provider failover D2/D3/D13.

OpenCode Zen can park a live worker on `Free limit reached` / `Free usage
exceeded, subscribe to Go [retrying in 15h 2m]`. The countdown repaints, so
pane activity cannot be the gate for detecting this screen.

* Recognise OpenCode's retry footer only on harnesses running the OpenCode
  executable, including operator-named recipes such as `opencode-zen`.
  Match the recent pane tail, reject diff, grep, quoted and composer text,
  and require the bracketed retry status. Preserve existing Claude/Codex
  matcher strictness and their inactivity grace.
* An explicit free-usage exhaustion footer is sufficient to exhaust the
  provider. Unlike a bare `429`, it is a blocking quota statement accompanied
  by the CLI's retry status. Long retry footers (at least one hour) for
  recognised provider error messages also take the worker through the
  rate-limit path; without explicit usage exhaustion, keep D3's two-session
  corroboration rule. Brief ordinary server retries keep running.
* Parse the retry duration (weeks, days, hours, minutes, seconds) into an absolute
  reset deadline when recording evidence. Use that deadline rather than the
  default capped backoff, including on subsequent observations and failed
  recovery canaries. Do not release the known exhaustion deadline on an
  unrelated launch acknowledgement. Operator overrides and fresh low usage
  snapshots retain their existing recovery semantics.
* Inspect a running OpenCode holder's retry footer before trusting pane
  activity. Respect question/wait leases, instance fences and failover mode:
  active teardown remains enforce-only. Idle unclaimed pool workers retain
  their guarded recycle path in observe/enforce modes.
* Stop the writer before handing off. Reuse the existing failover checkpoint,
  push, handoff and release path; spend no retry/restart budget. A failed
  checkpoint push keeps the local checkpoint and holds the task.

Regression evidence covers the observed footer, duration parsing, ordinary
output false positives, repainting activity, idle workers, provider suppression
through reset and real Git preservation on both successful and failed pushes.

OpenCode's upstream [retry footer](https://github.com/anomalyco/opencode/blob/dev/packages/tui/src/component/prompt/index.tsx)
and [duration formatter](https://github.com/anomalyco/opencode/blob/dev/packages/tui/src/util/format.ts)
define the spinner, attempt suffix and approximate day/week forms accepted here.
