"""Project onboarding support (design: ``docs/superpowers/specs/2026-09-03-project-onboarding-design.md``).

Modules here are deliberately independent of ``src.config`` and the database
so that the browse / preflight / mutation layers can import them without
pulling in the daemon; they receive already-resolved values from their
callers.  See :mod:`src.projects.paths` for root-relative path validation.
"""
