"""Serial module tests with declared toolchains and exact node obligations."""
load("@rules_python//python:defs.bzl", "py_test")
load("@pypi//:requirements.bzl", "all_requirements")


def _spec_impl(ctx):
    output = ctx.actions.declare_file(ctx.label.name + ".json")
    ctx.actions.write(output, ctx.attr.content)
    return [DefaultInfo(files = depset([output]))]

_spec = rule(implementation = _spec_impl, attrs = {"content": attr.string(mandatory = True)})


def _runtime_impl(ctx):
    python = ctx.toolchains["@rules_python//python:toolchain_type"].py3_runtime
    native = ctx.toolchains["//tools/bazel:native_toolchain_type"]
    node = ctx.toolchains["@rules_nodejs//nodejs:toolchain_type"].nodeinfo.node
    if not python.interpreter or not node:
        fail("AQ tests require declared Python and Node executables")
    output = ctx.actions.declare_file(ctx.label.name + ".json")
    ctx.actions.write(output, json.encode({
        "python": python.interpreter.short_path,
        "native_git": native.binaries["git"].short_path,
        "node": node.short_path,
    }))
    files = depset([output, python.interpreter, node], transitive = [python.files, native.files])
    return [DefaultInfo(files = depset([output]), runfiles = ctx.runfiles(transitive_files = files))]

_runtime = rule(
    implementation = _runtime_impl,
    toolchains = [
        "@rules_python//python:toolchain_type",
        "@rules_nodejs//nodejs:toolchain_type",
        "//tools/bazel:native_toolchain_type",
    ],
)


def aq_python_runtime(name):
    _runtime(name = name, visibility = ["//visibility:public"])


def aq_py_module(name, nodes):
    _spec(name = name, content = json.encode({"nodes": nodes}))


def aq_py_test(name, module, suite, nodes, module_spec, marker, postgres = True,
               perf_strict = False, deps = [], data = [], tags = []):
    _spec(
        name = name + "_obligation",
        content = json.encode({
            "schema_version": 1,
            "module": module,
            "suite": suite,
            "nodes": nodes,
            "marker": marker,
            "postgres": postgres,
            "perf_strict": perf_strict,
            "tags": tags,
            "shard_isolation": "",
        }),
    )
    repo = Label("@aq_native//:runtime").workspace_name
    runfiles = "$(BINDIR)/" + native.package_name() + "/" + name + ".runfiles/"
    roots = [runfiles + repo + "/runtime", "../" + repo + "/runtime"]
    py_test(
        name = name,
        main = "//tools/bazel:pytest_main.py",
        srcs = ["//tools/bazel:pytest_main.py"],
        deps = deps + ["//tools/bazel:pytest_launcher"] + all_requirements,
        data = data + [module_spec, ":" + name + "_obligation", "//tools/bazel:pytest_runtime"],
        args = ["--spec", "$(rootpath :" + name + "_obligation)",
                "--module-spec", "$(rootpath " + module_spec + ")",
                "--runtime", "$(rootpath //tools/bazel:pytest_runtime)"],
        imports = ["..", "../packages/aq-client"],
        env = {
            "PATH": ":".join([root + "/bin" for root in roots]),
            "LD_LIBRARY_PATH": ":".join([root + "/lib" for root in roots]),
        },
        size = "medium",
        timeout = "moderate",
        flaky = False,
        shard_count = 1,
        # H2 owns user/mount/network/PID namespaces. Bazel's outer Linux sandbox
        # prevents the nested namespace setup; H2 still refuses every launch
        # whose isolation self-test fails.
        tags = tags + ["no-sandbox"],
        legacy_create_init = False,
    )


def aq_module_tests(name, module, nodes, postgres, obligations, deps = [], data = []):
    """Share each module's collection and resource declarations across arms."""
    aq_py_module(name = name + "_collection", nodes = nodes)
    for obligation in obligations:
        aq_py_test(
            module = module,
            module_spec = ":" + name + "_collection",
            postgres = postgres,
            deps = deps,
            data = data,
            **obligation
        )
