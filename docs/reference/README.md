# Reference

This is the look-up section of the [AQ documentation](../README.md). Read the
[install](../tutorials/install.md) and [first-task](../tutorials/first-task.md)
tutorials first if you are new to AQ; reference pages assume you already know
which question you need answered.

## Find a command, setting, or source module

| Need | Page | What it answers |
| --- | --- | --- |
| AQ vocabulary | [Glossary](glossary.md) | The terms used throughout the documentation. |
| Command-line behaviour | [CLI reference](cli/README.md) | Command groups, flags, contracts, worker tools, and `aq prime`. |
| Generated command list | [CLI command inventory](cli-command-inventory.md) | The current Click surface and its generated JSON source. |
| HTTP, WebSocket, or client use | [API reference](api/README.md) | REST routes, events, conventions, and generated Python/TypeScript clients. |
| Configuration and vault state | [Configuration reference](configuration.md) | Settings, defaults, and when AQ reads them. |
| PostgreSQL state | [Database reference](database/README.md) | Tables, query modules, migrations, and retention. |
| A source path | [Module catalog](modules/README.md) | Every production module’s purpose, component page, and focused tests. |
| Profiles and classes | [Profiles and intelligence classes](profiles-and-classes.md) | Profile fields, intelligence classes, and precedence. |
| Claims and terminals | [Terminals and claims](terminals-and-claims.md) | Session ownership, claim epochs, and recovery vocabulary. |
| Token accounting | [Usage accounting](usage-accounting.md) | Estimated usage versus provider-reported quota. |

## Keeping reference current

The [reference maintenance guide](reference-maintenance.md) names the local,
dependency-free checks for links, catalogs, CLI inventory, and configuration
schema. It also distinguishes a shipped generated artifact from optional local
policy and proposed work.

## Related pages

* [Documentation home](../README.md) — the reading order and concept pages.
* [Module catalog](modules/README.md) — the reverse index from source to prose.
* [Contributing](../contributing/README.md) — setup and focused checks before a change reaches `main`.
