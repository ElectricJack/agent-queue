"""Declared, scratch-tree legacy generation; TS directory artifacts are public deps."""

def _generate_impl(ctx):
    output = ctx.actions.declare_directory(ctx.label.name + ".tree")
    manifest = ctx.actions.declare_file(ctx.label.name + ".manifest.json")
    inputs = ctx.actions.declare_file(ctx.label.name + ".inputs.json")
    ctx.actions.write(inputs, json.encode([[f.path, f.short_path] for f in ctx.files.srcs]))
    native = ctx.toolchains["//tools/bazel:native_toolchain_type"]
    arguments = ctx.actions.args()
    arguments.add("--inputs", inputs)
    arguments.add("--native", native.binaries["bash"].dirname + "/..")
    arguments.add("--kind", ctx.attr.kind)
    arguments.add("--output", output.path)
    arguments.add("--manifest", manifest)
    ruff_files = ctx.attr._ruff[DefaultInfo].files
    ruff = [f for f in ruff_files.to_list() if f.path.endswith("/bin/ruff")]
    if len(ruff) != 1:
        fail("The locked Ruff wheel must supply exactly one bin/ruff")
    arguments.add("--ruff", ruff[0])
    transitive = [native.files, ruff_files]
    if ctx.attr.upstream:
        arguments.add("--upstream", ctx.attr.upstream[OutputGroupInfo].tree.to_list()[0].path)
        transitive.append(ctx.attr.upstream[OutputGroupInfo].tree)
    if ctx.attr.npm:
        transitive.append(ctx.attr.npm[DefaultInfo].files)
        arguments.add("--npm", ctx.file.npm_entry.dirname + "/../../..")
        node = ctx.toolchains["@rules_nodejs//nodejs:toolchain_type"].nodeinfo.node
        if node == None:
            fail("Generator actions require the pinned Node toolchain, not node_path")
        arguments.add("--node", node)
        transitive.append(depset([node]))
    ctx.actions.run(
        executable = ctx.executable._runner,
        tools = [ctx.attr._runner[DefaultInfo].files_to_run],
        inputs = depset(ctx.files.srcs + [inputs] + ctx.files.npm_entry, transitive = transitive),
        outputs = [output, manifest],
        arguments = [arguments],
        env = {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC"},
        use_default_shell_env = False,
        mnemonic = "AqGenerate",
        progress_message = "Generating %s with locked tools in a scratch tree" % ctx.attr.kind,
    )
    return [
        DefaultInfo(files = depset([output]), runfiles = ctx.runfiles(files = [output, manifest])),
        OutputGroupInfo(tree = depset([output]), manifest = depset([manifest])),
    ]

aq_generate = rule(
    implementation = _generate_impl,
    attrs = {
        "srcs": attr.label_list(allow_files = True),
        "kind": attr.string(mandatory = True),
        "upstream": attr.label(),
        "npm": attr.label(),
        "npm_entry": attr.label(allow_single_file = True),
        "_runner": attr.label(default = "//tools/bazel:generate", executable = True, cfg = "exec"),
        "_ruff": attr.label(default = "@pypi//ruff:extracted_whl_files", cfg = "exec"),
    },
    toolchains = ["//tools/bazel:native_toolchain_type", "@rules_nodejs//nodejs:toolchain_type"],
)
