# Large session prompts and startup evidence

All session launch paths must keep prompt contents out of argv once they exceed
the harness's `max_argv_prompt_bytes`, with a hard ceiling of 16 KiB even if an
operator raises that setting. Store the complete UTF-8 prompt under the session
checkout's `.aq/tmp/` and pass a short instruction to read that file before acting.
Preserve interactive harness operation, prompt flags and literal path characters.
Do not expand the file back into an argument or redirect the interactive CLI's stdin.

Batch fixers retain their complete frozen input in `.aq/tmp/batch-fixer-input.json`.
Their startup prompt contains the repair protocol, a bounded preview of the batch
identity, refs, members, brief and check/group counts, and a pointer to that file.
Failure groups repeat frozen member ownership proofs; these belong in the evidence
file rather than the startup preview. Stored grouping and attribution stay complete.

The tmux launcher records exec failures (including the errno name and an E2BIG
explanation) directly in `start-stderr.log` before exiting. Readiness failure adds
its reason and available pane text without overwriting that record. An unavailable
or empty pane capture must still leave a nonempty diagnostic. The subprocess
provider likewise writes process-creation errors into its startup log.

Regression checks execute generated launch commands with prompts larger than 2 MiB
for Claude and Codex shapes, verify exact file contents and prompt flag placement,
and exercise exec failure plus missing-pane diagnostics without invoking an LLM.
