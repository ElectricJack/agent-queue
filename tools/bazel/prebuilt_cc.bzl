"""Analysis-only C++ metadata for prebuilt wheels; compilation fails closed.

rules_python's runtime headers require a CcToolchainInfo even when no native
code is compiled. Do not discover a host compiler to satisfy that dependency.
A target needing compilation must first introduce a reviewed, pinned compiler.
"""
load("@bazel_tools//tools/cpp:cc_toolchain_config_lib.bzl", "tool_path")

def _config_impl(ctx):
    return cc_common.create_cc_toolchain_config_info(
        ctx = ctx,
        toolchain_identifier = "aq-prebuilt-only",
        host_system_name = "linux-x86_64",
        target_system_name = "linux-x86_64",
        target_cpu = "k8",
        target_libc = "glibc-2.28",
        compiler = "prebuilt-only",
        abi_version = "unknown",
        abi_libc_version = "glibc-2.28",
        tool_paths = [
            tool_path(name = name, path = "NATIVE_COMPILATION_REQUIRES_A_PINNED_TOOLCHAIN")
            for name in ["gcc", "ld", "ar", "cpp", "gcov", "nm", "objdump", "strip"]
        ],
    )

prebuilt_cc_config = rule(
    implementation = _config_impl,
    attrs = {},
    provides = [CcToolchainConfigInfo],
)
