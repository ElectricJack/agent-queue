# H1 toolchain substrate

This is the first Bazel substrate from the approved Bazel-for-AQ plan. It does
not switch `aq test`, CI, code generation or dashboard builds to Bazel. H2 owns
the general isolated launcher; B3 owns the complete npm/pnpm dependency graph.

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
