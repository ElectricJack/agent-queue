# CLI command inventory

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
evidence level:

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
