# CLI command inventory

> **Looking for prose?** This page explains the generated artifact and its
> acceptance statuses. The written reference for the same surface is
> [the CLI reference](cli/README.md) — start with
> [Commands](cli/commands.md) for what each group does,
> [Command contracts](cli/contracts.md) for what a playbook may call, and
> [Agent-facing tools](cli/agent-tools.md) for the tool presentation of it.

The maintained machine-readable inventory is
[`cli-command-inventory.json`](cli-command-inventory.json). It is generated from the live
Click command tree, so its totals follow the current source rather than pinning the 318-command
audit baseline.

Regenerate and verify it with:

```bash
python scripts/generate-cli-command-inventory.py
python scripts/generate-cli-command-inventory.py --check
aq test tests/test_cli_inventory.py tests/test_cli_conformance.py
```

Each leaf records whether it is handwritten, schema-generated, or supplied by an installed
CLI extension; whether its provider is core, an in-tree plugin, or an external plugin; its
backend command; supported aliases and deprecations; a compact parameter contract; and its
evidence level. It also records one conservative final-audit status:

- `working`: focused behavioral evidence exists; stateful operations receive this label only
  when the disposable-daemon suite exercises the command through the public CLI.
- `broken`: a deterministic acceptance check currently fails.
- `obsolete`: retained only as a tested compatibility/deprecation surface.
- `unsupported`: deliberately unavailable, with a disposition.
- `untested`: registered or mock-dispatched, but without sufficient behavioral evidence.

The artifact's `historical_commands` ledger preserves removed spellings and operations from the
2026-09-08 audit. Those rows are not counted as current Click leaves, but their explicit
`obsolete` or `unsupported` disposition prevents removal from looking like an inventory gap.

The lower-level evidence values remain useful for diagnosing how a leaf was discovered:

- `registration`: the live Click tree proves the leaf is registered.
- `dispatch`: the generated-command conformance suite supplies required values, omits optional
  values, invokes the callback, and checks the exact backend dispatch for every generated leaf.
- `behavioral`: focused tests cover payload, rendering, alias/deprecation, or exit behavior.

The reproducible artifact excludes environment-specific `aq.plugins` CLI extensions. Inventory
code can include them explicitly for startup tests; those leaves are labeled `plugin-extension`
with their entry-point owner. Schema-advertised commands implemented by `aq-files`, `aq-git`,
`aq-notes`, `aq-vibecop`, or `aq-memory` are labeled plugin-owned and are not reported as missing
core handlers. A newly advertised generated operation with neither a core handler nor a named
plugin provider fails generation unless it is added to the explicit unsupported ledger with a
reason. Stale exceptions fail too.

`aq test` deliberately reserves only `--aq-*` wrapper flags. Use `aq test --aq-help` for wrapper
help; `-h` and `--help` are passed through to pytest.
