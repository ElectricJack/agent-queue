"""Dashboard checks using the locked npm graph and B2's generated TS artifact."""

load("@aspect_rules_js//js:providers.bzl", "JsInfo")
load("@aspect_rules_js//npm:providers.bzl", "NpmPackageInfo")


def _dashboard_impl(ctx):
    output = ctx.actions.declare_directory(ctx.label.name + ".tree")
    manifest = ctx.actions.declare_file(ctx.label.name + ".manifest.json")
    inventory = ctx.actions.declare_file(ctx.label.name + ".inputs.json")
    source_files = ctx.files.srcs
    npm_sets = []
    for dep in ctx.attr.deps:
        npm_sets.append(dep[DefaultInfo].default_runfiles.files)
        if JsInfo in dep:
            npm_sets.append(dep[JsInfo].npm_sources)
    npm_files = depset(transitive = npm_sets)
    ctx.actions.write(inventory, json.encode({
        "srcs": [[f.path, f.short_path] for f in source_files],
        "npm": [[f.path, f.short_path] for f in npm_files.to_list()],
    }))
    native = ctx.toolchains["//tools/bazel:native_toolchain_type"]
    node = ctx.toolchains["@rules_nodejs//nodejs:toolchain_type"].nodeinfo.node
    if node == None:
        fail("Dashboard actions require the pinned Node toolchain")
    args = ctx.actions.args()
    args.add("--kind", ctx.attr.kind)
    args.add("--inputs", inventory)
    args.add("--output", output.path)
    args.add("--manifest", manifest)
    args.add("--node", node)
    args.add("--native", native.binaries["bash"].dirname + "/..")
    transitive = [native.files, npm_files]
    if ctx.attr.generated:
        tree = ctx.attr.generated[OutputGroupInfo].tree
        args.add("--generated", tree.to_list()[0].path)
        transitive.append(tree)
    ctx.actions.run(
        executable = ctx.executable._runner,
        tools = [ctx.attr._runner[DefaultInfo].files_to_run],
        inputs = depset(source_files + [inventory, node], transitive = transitive),
        outputs = [output, manifest],
        arguments = [args],
        env = {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC"},
        use_default_shell_env = False,
        execution_requirements = {"block-network": "1"},
        mnemonic = "AqDashboard",
        progress_message = "Checking dashboard %s with locked Node/npm inputs" % ctx.attr.kind,
    )
    providers = [
        DefaultInfo(files = depset([output]), runfiles = ctx.runfiles(files = [output, manifest])),
        OutputGroupInfo(tree = depset([output]), manifest = depset([manifest])),
    ]
    if ctx.attr.kind == "package":
        providers.append(NpmPackageInfo(
            package = "@aq/ts-client",
            version = "0.0.1",
            src = output,
            npm_package_store_infos = depset(),
        ))
    return providers


aq_dashboard = rule(
    implementation = _dashboard_impl,
    attrs = {
        "kind": attr.string(mandatory = True, values = ["package", "typecheck", "build"]),
        "srcs": attr.label_list(allow_files = True),
        "deps": attr.label_list(),
        "generated": attr.label(),
        "_runner": attr.label(default = "//tools/bazel:dashboard_runner", executable = True, cfg = "exec"),
    },
    toolchains = ["//tools/bazel:native_toolchain_type", "@rules_nodejs//nodejs:toolchain_type"],
)
