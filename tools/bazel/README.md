# H1 toolchain substrate

This is the first Bazel substrate from the approved Bazel-for-AQ plan. It does
not switch `aq test`, CI, code generation or dashboard builds to Bazel. H2 owns
the general isolated launcher. The dashboard shadow graph is described below.

## Supported execution platform and pins

Only Linux x86_64 with GNU libc >=2.28,<3.0 is supported. The archive baseline
requires Linux kernel >=4.18. macOS, ARM and musl use the existing pytest/npm
backend. The preflight and native repository reject unsupported OS/CPU/libc;
there is no host PostgreSQL, git, tmux, Python or Node fallback.

`toolchains.lock.json` is the reviewed authority for archive URLs, SHA-256,
exact versions and the native dependency closure:

| Component | Pin |
|---|---|
| Bazel | 8.3.1 |
| rules_python | 1.8.2 |
| aspect_rules_js / aspect_rules_ts | 3.5.1 / 3.10.1 |
| Python standalone | 3.12.12, build 20251031 |
| Node | 24.10.0 |
| TypeScript smoke compiler | 5.7.3, integrity from root package-lock.json |
| PostgreSQL / git / tmux | 18.1 / 2.51.2 / 3.6a |
| uv / micromamba | 0.12.17 / 2.3.3 |

The requested four-version baseline resolves together unchanged. Direct
rules_cc 0.2.16 and platforms 1.1.0 match the resolved transitive graph.
`MODULE.bazel.lock` records Bzlmod resolution; `.bazelrc` requires error mode.
Native archives come from conda-forge, include shared libraries and shell
utilities, and are SHA-verified before an offline, explicit micromamba install.
No solver runs in repository resolution or test actions. Archive fetches require
network during repository resolution; smoke actions use declared tools only.
Bazel's own bootstrap and repository installation still rely on the supported
host OS. H1 does not claim H2's environment/network isolation or cache parity.

The native toolchain exposes `files`, `binaries` and `abi` through
`//tools/bazel:native_toolchain_type`. Consumers must carry the complete `files`
closure in runfiles; executable labels alone omit shared libraries and helpers.
The smoke wrappers use only that closure for PATH/LD_LIBRARY_PATH, handling both
Bazel's initial execroot and its later runfiles working directory. XML generation
stays inside the test action so it sees the declared utilities. rules_js uses its
native hermetic launcher. rules_ts 3.10.1 reports a directory-input warning for
its hash-pinned TypeScript archive; this is recorded as a limitation for B3
graph/cache validation, not suppressed or treated as parity evidence. A prebuilt-only C++ metadata toolchain satisfies
rules_python's header metadata: every compiler path deliberately fails closed.
Adding native compilation requires a separately pinned compiler toolchain.

## Lock regeneration

Run these from the repository root, using the executable whose SHA is recorded
in the manifest (the lock compiler also verifies the uv binary):

```bash
python tools/bazel/lock.py --compile --uv /absolute/path/to/locked/uv
python tools/bazel/lock.py --render-module
/path/to/bazel-8.3.1 mod deps --lockfile_mode=update
python tools/bazel/lock.py
```

Python input derives from root pyproject.toml dependencies, dev/cli extras,
build-system requirements, and generated-client dependency/build metadata.
The generated client remains source in this repository. Nothing installs it
editable or changes the shared environment. The lock contains exact versions
and wheel hashes; `pip.parse` uses `--require-hashes --only-binary=:all:`.
Review input/hash changes, generated MODULE and Bzlmod lock together. Updating
a native version requires an explicit Linux-64 conda-forge solve outside Bazel,
then reviewing and recording every closure URL, SHA-256, MD5, version, build and
dependency. MD5 is only the installer's format; Bazel verifies SHA-256 first.

## Admitted verification

B1 supplies the supervisor-authorized H1/H2 prerequisite for native execution:
`sysroot_linux-64` 2.28 `he3f20f0_9` and its exact kernel-header dependency are
URL/SHA-256 pinned in `toolchains.lock.json`, installed offline with the remaining
native closure, and exposed through `@aq_native//:runtime`. The sysroot provides
the ELF loader and glibc which were absent from the original H1 lock. Package
metadata is available from https://api.anaconda.org/package/conda-forge/sysroot_linux-64.
The launcher maps only its declared library directories at conventional ELF ABI
paths; native actions never mount the host's libraries as a fallback.

On a shared AQ box, run all smoke execution inside `aq test`, not direct Bazel:

```bash
POSTGRES_TEST_DSN=<disposable-test-dsn> \
AQ_BAZEL_SMOKE=/absolute/path/to/bazel-8.3.1 \
aq test tests/test_bazel_manifest.py -p no:xdist -s
```

The outer pytest fixtures require the disposable DSN. It is excluded from the
Bazel child environment. Each of two independent scratch roots has a fresh
HOME, module/repository cache and output base. It resolves in lockfile error
mode, then runs Python/generated-client imports, TypeScript compile/typecheck
and Node execution, native version checks and an isolated git commit. Bazel
uses one job while holding AQ's test admission slot. The first root also proves
that a missing module lock and a corrupt native archive digest are refused.
Fast tests cover dependency intent, npm integrity, module pins, missing native
libraries, archive corruption and unsupported ABI.
Without `AQ_BAZEL_SMOKE`, the two acceptance cases explicitly skip; that is not
evidence of toolchain acceptance. This smoke does not start PostgreSQL or tmux
sessions and does not establish parity for the full application test suite.

## B2 deterministic generator actions

`//checks:generated` regenerates the tracked projections in a writable action
scratch tree and compares filenames and bytes with the checked-in outputs.
It also compares the ignored TypeScript sources with a separate invocation of
the locked legacy generator. The reference runs the offline Python client script
and umbrella `--check`, then requires an empty Git diff. Recorded playbooks keep
their approved inputs and provenance; generation never reauthors approval data.

The public `//tools/bazel:typescript` directory artifact contains
`packages/aq-ts-client/src/**`. B3 can depend on its `DefaultInfo` files or
`OutputGroupInfo.tree`; `OutputGroupInfo.manifest` provides relative filenames
and SHA-256 digests. It depends on the generated OpenAPI artifact, locked Node,
and the hash-verified npm closure. It does not install packages or mutate the
shared editable Python environment. Python generation uses the pinned wheel
closure, including openapi-python-client and Ruff, and declared native tools.

`generator_inputs.bzl` follows transitive Python imports, discovery registries,
scanned test/resources, templates, locks and recorded inputs. Its drift check
rejects an outdated inventory. After intentional input/lock changes, run:

```bash
python tools/bazel/generator_inputs.py --write
```

`generator_npm.lock.json` is a derived subset of the authoritative root npm lock,
including nested dependency locations. Repository resolution checks it against
that lock and downloads exact integrity-verified archives, without a package
solver or lifecycle scripts. This is only the generator closure; B3 owns the
dashboard dependency graph described below.

On a shared AQ box, admit the two-root acceptance through the existing gate:

```bash
POSTGRES_TEST_DSN=<disposable-test-dsn> \
AQ_BAZEL_GENERATORS=/absolute/path/to/bazel-8.3.1 \
AQ_BAZEL_GENERATOR_EVIDENCE=/absolute/path/to/evidence \
aq test tests/test_bazel_manifest.py -k clean_root_generator -p no:xdist -s
```

Each root has a fresh HOME, repository cache and output base, consumes the same
source snapshot, resolves in lockfile error mode and runs
`bazel test --config=ci --jobs=1 //checks:generated`. Logs, BEP files and both
normalized TypeScript manifests are retained in the evidence directory. Missing
opt-in settings skip acceptance explicitly. Actions clear inherited credentials
and select declared tools; broader H2 launch/network isolation remains separate.

## B3 dashboard shadow checks

`//checks:dashboard` mirrors `npm --prefix dashboard run typecheck` and
`npm --prefix dashboard run build`. It compiles the existing test sources but
does not add a Vitest execution gate. The `@aq/ts-client` workspace consumes
`//tools/bazel:dashboard_typescript`, which uses the committed `openapi.json`
just like npm's `pretypecheck` and `prebuild` scripts. Dashboard actions cannot
use ignored generated sources from a developer checkout. Backend schema
regeneration remains in `//tools/bazel:typescript` and `//checks:generated`;
unrelated Python edits do not invalidate frontend compilation. npm remains the
reference backend.

The root `package-lock.json` is authoritative. `import_npm.py` is the pinned,
versioned importer: it projects npm's installed-location graph to pnpm v9 lock
syntax without registry queries or version resolution. This preserves aliases,
nested versions, optional dependencies, peer edges, archive URLs and SHA-512
integrities. Conflicting contexts or unsupported archives fail closed. The
committed projection also records every npm installed location and the npm
lock's SHA-256. A trial with stock pnpm 10.11.0 changed the `react-is` alias from
19.2.8 to 19.3.1, dropped nested semver 7.7.4, and required registry metadata even
for an offline import. This versioned, solver-free projector replaces that stock
import step to meet the approved package/version/integrity equivalence contract.

After an intentional npm dependency update, run:

```bash
python tools/bazel/import_npm.py --write
python tools/bazel/lock.py --render-module
/path/to/bazel-8.3.1 mod deps --lockfile_mode=update
python tools/bazel/import_npm.py
```

`npm_translate_lock` consumes the reviewed projection with updates and lifecycle
hooks disabled. The adjacent workspace file explicitly permits no lifecycle
builds. All archive downloads retain npm's integrity checks. Actions copy declared
sources into writable scratch, reconstruct npm's locked installed locations from
declared package artifacts (including hoisted types), and use pinned
Node and native tools with an allowlisted environment. Ambient `NODE_PATH`,
`NODE_OPTIONS`, user npm configuration and ancestor `node_modules` are excluded.
The declared dashboard inputs include scenario/layout files and environment
configuration: Tailwind scans beyond the TypeScript project, so omitting a
scenario class changes CSS and the resulting Vite chunk hashes.

Admit the complete two-root comparison through AQ's test slot:

```bash
POSTGRES_TEST_DSN=<disposable-test-dsn> \
AQ_BAZEL_DASHBOARD=/absolute/path/to/bazel-8.3.1 \
AQ_BAZEL_DASHBOARD_EVIDENCE=/absolute/path/to/new-evidence-directory \
aq test tests/test_bazel_manifest.py -k clean_root_dashboard_acceptance -p no:xdist -s
```

This retains two independent checkouts, HOME/repository/output roots, BEP and
logs. Both run dashboard checks with poisoned ambient Node paths, consuming the
declared committed-spec TypeScript artifact.
The first also installs directly from the npm lock and runs the existing npm
commands using the pinned Node 24 archive. TypeScript filename/byte manifests
and Vite output manifests must match across both Bazel roots and npm. Missing
opt-in settings explicitly skip this acceptance, rather than establishing parity.
