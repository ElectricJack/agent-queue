"""Declared native runtime closure, consumed by the isolated test launcher."""

def _native_toolchain_impl(ctx):
    return [platform_common.ToolchainInfo(
        files = depset(ctx.files.runtime),
        binaries = {name: target.files.to_list()[0] for name, target in ctx.attr.binaries.items()},
        abi = ctx.attr.abi,
    )]

native_toolchain = rule(
    implementation = _native_toolchain_impl,
    attrs = {
        "runtime": attr.label(allow_files = True, mandatory = True),
        "binaries": attr.string_keyed_label_dict(allow_files = True),
        "abi": attr.string(mandatory = True),
    },
)
