"""Hash-verified native tools; repository resolution never runs a solver."""

load("@bazel_tools//tools/build_defs/repo:http.bzl", "http_archive")

def _checked_execute(ctx, args):
    result = ctx.execute(args, environment = {
        "HOME": str(ctx.path(".home")),
        "MAMBA_ROOT_PREFIX": str(ctx.path(".mamba")),
        "MAMBA_NO_BANNER": "1",
    }, timeout = 600, quiet = True)
    if result.return_code:
        fail("Locked native installation failed: " + result.stderr + result.stdout)
    return result.stdout

def _native_impl(ctx):
    lock = json.decode(ctx.read(ctx.attr.lock))
    if ctx.os.name != "linux" or ctx.os.arch not in ["amd64", "x86_64"]:
        fail("AQ Bazel supports linux x86_64 only; use the legacy pytest/npm backend")
    installer = lock["micromamba"]
    ctx.download_and_extract(
        url = installer["url"],
        sha256 = installer["sha256"],
        output = ".bootstrap",
    )
    mamba = str(ctx.path(".bootstrap/bin/micromamba"))
    info = json.decode(_checked_execute(ctx, [mamba, "--no-rc", "--no-env", "info", "--json"]))
    glibc = None
    for item in info["virtual packages"]:
        if item.startswith("__glibc="):
            glibc = item.split("=")[1]
    if not glibc:
        fail("AQ native toolchain requires the declared GNU libc ABI; no musl/host fallback")
    version = [int(p) for p in glibc.split(".")[:2]]
    lower = [int(p) for p in lock["platform"]["glibc_min"].split(".")]
    upper = [int(p) for p in lock["platform"]["glibc_max_exclusive"].split(".")]
    if version < lower or version >= upper:
        fail("AQ native toolchain requires glibc >=2.28,<3.0, got " + glibc)

    lines = ["@EXPLICIT"]
    for package in lock["native"]["packages"]:
        filename = package["url"].split("/")[-1]
        path = ".archives/" + filename
        ctx.download(url = package["url"], sha256 = package["sha256"], output = path)
        lines.append("file://" + str(ctx.path(path)) + "#" + package["md5"])
    ctx.file(".explicit.txt", "\n".join(lines) + "\n")
    _checked_execute(ctx, [
        mamba, "--no-rc", "--no-env", "create", "--yes", "--offline", "--always-copy",
        "--root-prefix", str(ctx.path(".mamba")),
        "--prefix", str(ctx.path("runtime")), "--file", str(ctx.path(".explicit.txt")),
    ])
    ctx.file("BUILD.bazel", """
load({rule}, "native_toolchain")
package(default_visibility = ["//visibility:public"])
filegroup(name = "runtime", srcs = glob(["runtime/**"], exclude = ["runtime/conda-meta/**"]))
exports_files(["runtime/bin/git", "runtime/bin/tmux", "runtime/bin/postgres", "runtime/bin/initdb", "runtime/bin/pg_ctl", "runtime/bin/psql", "runtime/bin/bash"])
native_toolchain(
    name = "implementation",
    runtime = ":runtime",
    binaries = {{name: "runtime/bin/" + name for name in ["git", "tmux", "postgres", "initdb", "pg_ctl", "psql", "bash"]}},
    abi = "linux-x86_64-glibc-2.28",
)
toolchain(
    name = "toolchain",
    toolchain = ":implementation",
    toolchain_type = {toolchain_type},
    exec_compatible_with = ["@platforms//os:linux", "@platforms//cpu:x86_64"],
    target_compatible_with = ["@platforms//os:linux", "@platforms//cpu:x86_64"],
)
""".format(
        rule = repr(str(Label("//tools/bazel:native_toolchain.bzl"))),
        toolchain_type = repr(str(Label("//tools/bazel:native_toolchain_type"))),
    ))
    for directory in [".bootstrap", ".archives", ".mamba", ".home", ".explicit.txt"]:
        ctx.delete(directory)

_native_repository = repository_rule(
    implementation = _native_impl,
    attrs = {"lock": attr.label(mandatory = True, allow_single_file = True)},
)

def _tools_impl(ctx):
    lock_label = Label("//tools/bazel:toolchains.lock.json")
    lock = json.decode(ctx.read(lock_label))
    _native_repository(name = "aq_native", lock = lock_label)
    uv = lock["uv"]
    http_archive(
        name = "aq_uv",
        urls = [uv["url"]],
        sha256 = uv["sha256"],
        strip_prefix = uv["strip_prefix"],
        build_file_content = 'exports_files(["uv"], visibility = ["//visibility:public"])',
    )
    return ctx.extension_metadata(reproducible = True)

tools = module_extension(implementation = _tools_impl)
