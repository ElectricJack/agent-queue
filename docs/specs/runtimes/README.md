# Runtime specs (historical)

<!-- aq:historical -->
> **Retired design record.** These specs describe an in-process runtime layer
> that does not ship. `src/runtimes/` keeps the `Runtime` ABC and a registry that
> registers nothing; it is an injection seam for tests. Every agent runs as an
> external CLI inside a tmux session, and a profile's `harness` field
> (`claude`, `codex`, `gemini`) is the only selector. Read
> [Sessions](../../concepts/sessions.md) and
> [the harness reference](../../reference/harnesses.md).

These are kept because the rename history is still visible in the code: the
"platform" vocabulary, the `Capability` enum and `requires_workspace` all come
from here.

See [the historical index](../../history/README.md) and
[the disposition ledger](../../history/disposition-ledger.md).
