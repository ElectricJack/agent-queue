# Platform test-coverage implementation

<!-- aq:historical -->
> **Historical working note.** Dated scratch material, kept for the record and
> never revised. Not instructions. Start at [the documentation
> home](docs/README.md); see [historical material](docs/history/README.md).

## Tests added

- `RuntimeRegistry` returns daemon-wide singleton runtimes unchanged and
  includes their names in the available-runtime set.
- `RuntimeRegistry` forwards its configured application policy to runtimes
  whose constructors explicitly accept `config`, while retaining profile and
  logger forwarding.
- `default_registry(config=...)` preserves the configuration for future
  plugin-provided runtime construction.

## Bugs fixed

None. The new contracts passed against the existing runtime-registry
implementation.

## Deferred

The runtime constructor signature-inspection fallback for exotic non-inspectable
callables remains intentionally excluded by its production `pragma: no cover`.
It is a defensive compatibility branch rather than a supported runtime API.
