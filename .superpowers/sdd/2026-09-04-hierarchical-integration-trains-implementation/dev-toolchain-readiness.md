# Development toolchain readiness triage

Read-only /root/merge_main_sol follow-up; no edits/installs/tests. Production audit
already0 after64169b8a. Full development audit retains12 advisories including2critical.

- GHSA-2w6w-674q-4c4q: packages/aq-ts-client dev @hey-api/openapi-ts0.61.3 →
  handlebars4.7.8; patched4.7.9. Vulnerable sink is attacker-controlled pre-parsed AST
  passed to compile(). Observed generator uses package-owned precompiled template
  specs via template(), not compile(PR OpenAPI input).
  https://github.com/advisories/GHSA-2w6w-674q-4c4q
- GHSA-23hp-3jrh-7fpw: same generator → c12@2.0.1 → giget@1.2.5 → tar@6.2.1.
  Patched line starts7.5.19; giget requires^6.2.1, so no supported leaf update.
  c12 imports giget/tar for configured extends with a giget prefix. Repository has
  no openapi-ts.config.*; normal checked-in OpenAPI generation does not extract
  archives, but an unreviewed PR adding config can activate that path locally.
  https://github.com/advisories/GHSA-23hp-3jrh-7fpw

Entry points: npm workspace @aq/ts-client generate; dashboard predev/pretypecheck/
prebuild; root generate:ts-client/generate/typecheck/build; regenerate-ts-client.sh.
Observed tests.yml and docs.yml run Python/mkdocs, not this generator. Not proven a
product-runtime/current-CI release blocker; do not call the full audit clean.

Recommended separate scoped generator upgrade (audit proposed0.99.0) removes the
dependency graph but requires regenerating/reviewing client and API import changes.
Do not force tar6→7 via unreviewed override. Until remediated, avoid generation on
unreviewed PRs introducing generator config. Recheck future CI paths if they change.
