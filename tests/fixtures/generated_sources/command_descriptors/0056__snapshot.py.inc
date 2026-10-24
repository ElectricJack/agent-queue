def _snapshot(*, handlers=(), explicit=(), fallback=None, typed=None, plugins=()):
    return build_descriptor_snapshot(
        handlers=handlers, explicit_definitions=explicit, fallback_schemas=fallback or {},
        typed_contracts=typed or {}, plugins=plugins,
    )


