"""H1 bootstrap smoke with declared shell utilities, independent of host PATH."""
load("@rules_python//python:defs.bzl", "py_test")
load("@aspect_rules_js//js:defs.bzl", "js_test")

def _environment(executable):
    # Bazel's setup starts in execroot, then enters the main runfiles directory.
    # Both paths identify the same declared closure; neither searches host PATH.
    repo = Label("@aq_native//:runtime").workspace_name
    runfiles = "$(BINDIR)/" + native.package_name() + "/" + executable + ".runfiles/"
    roots = [runfiles + repo + "/runtime", "../" + repo + "/runtime"]
    return {
        "PATH": ":".join([root + "/bin" for root in roots]),
        "LD_LIBRARY_PATH": ":".join([root + "/lib" for root in roots]),
    }

def py_smoke(name, data = [], **kwargs):
    py_test(
        name = name,
        data = data + ["@aq_native//:runtime"],
        env = _environment(name),
        **kwargs
    )

def js_smoke(name, data = [], **kwargs):
    js_test(
        name = name,
        data = data + ["@aq_native//:runtime"],
        no_copy_to_bin = ["@aq_native//:runtime"],
        env = _environment(name + "_/" + name),
        **kwargs
    )
