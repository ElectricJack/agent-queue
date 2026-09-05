"""Project onboarding support (design: ``docs/superpowers/specs/2026-09-03-project-onboarding-design.md``).

The pure :mod:`src.projects.paths` module stays independent of configuration
and persistence.  :mod:`src.projects.onboarding` is the intentional
orchestration boundary that coordinates those dependencies for mutations.
:mod:`src.projects.github` stays independent of configuration and persistence
so callers can reuse its identity validation and async ``gh`` wrapper.
"""
