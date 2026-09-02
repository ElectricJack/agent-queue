"""``aq doctor`` — one entry point that answers "is this install healthy?".

Doctor owns the registry, the concurrent runner and the generic checks;
subsystems own their own checks and register them at startup
(``docs/specs/design/trust-and-ops.md`` §5.5).  Reserved ids for checks whose
owning subsystem has not landed yet are listed in
:data:`~src.doctor.models.RESERVED_CHECK_IDS`, together with the registration
contract those owners must follow.
"""

from src.doctor.builtin import builtin_checks
from src.doctor.formula_checks import formula_checks
from src.doctor.hierarchy_checks import hierarchy_checks
from src.doctor.integration_checks import integration_checks
from src.doctor.models import (
    RESERVED_CHECK_IDS,
    CheckResult,
    DoctorCheck,
    DoctorContext,
    Severity,
)
from src.doctor.pool_checks import pool_checks
from src.doctor.resource_checks import resource_checks
from src.doctor.runner import DoctorRegistry, exit_code_for, run_doctor
from src.doctor.workspace_checks import workspace_checks

__all__ = [
    "CheckResult",
    "DoctorCheck",
    "DoctorContext",
    "DoctorRegistry",
    "RESERVED_CHECK_IDS",
    "Severity",
    "builtin_checks",
    "default_registry",
    "exit_code_for",
    "formula_checks",
    "resource_checks",
    "integration_checks",
    "run_doctor",
    "workspace_checks",
]


def default_registry() -> DoctorRegistry:
    """A registry pre-populated with every built-in check."""
    registry = DoctorRegistry()
    for check in builtin_checks():
        registry.register(check)
    for check in hierarchy_checks():
        registry.register(check)
    for check in pool_checks():
        registry.register(check)
    for check in formula_checks():
        registry.register(check)
    for check in resource_checks():
        registry.register(check)
    for check in integration_checks():
        registry.register(check)
    for check in workspace_checks():
        registry.register(check)
    return registry
