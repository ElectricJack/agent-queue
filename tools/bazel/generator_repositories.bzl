"""Fetch only the generator closure recorded in the authoritative npm lock.

No npm install, dependency resolution, lifecycle script or host node runs here.
The closure is independently checked by generator_inputs.py and the parity test.
B3 owns the full npm_translate_lock graph; this repository is a generator tool.
"""

def _npm_impl(ctx):
    lock = json.decode(ctx.read(ctx.attr.lock))
    npm = json.decode(ctx.read(ctx.attr.npm_lock))["packages"]
    for location, package in lock["packages"].items():
        if not location.startswith("node_modules/") or ".." in location.split("/"):
            fail("unsafe generator npm location: " + location)
        for key in ["version", "resolved", "integrity"]:
            if package[key] != npm[location][key]:
                fail("generator npm lock drift: " + location)
        # DefinitelyTyped archives retain their package basename; npm's other
        # archives use "package". Both formats are integrity checked first.
        name = location.split("node_modules/")[-1]
        ctx.download_and_extract(
            url = package["resolved"],
            integrity = package["integrity"],
            stripPrefix = name.split("/")[-1] if name.startswith("@types/") else "package",
            output = location,
        )
    ctx.file("BUILD.bazel", '''
package(default_visibility = ["//visibility:public"])
filegroup(name = "node_modules", srcs = glob(["node_modules/**"]))
exports_files(["node_modules/@hey-api/openapi-ts/bin/index.cjs"])
''')
    return None

_npm = repository_rule(
    implementation = _npm_impl,
    attrs = {
        "lock": attr.label(mandatory = True, allow_single_file = True),
        "npm_lock": attr.label(mandatory = True, allow_single_file = True),
    },
)

def _generators_impl(ctx):
    _npm(
        name = "aq_generator_npm",
        lock = Label("//tools/bazel:generator_npm.lock.json"),
        npm_lock = Label("//:package-lock.json"),
    )
    return ctx.extension_metadata(reproducible = True)

generators = module_extension(implementation = _generators_impl)
