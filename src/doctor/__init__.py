"""``aq doctor`` — one entry point that answers "is this install healthy?".

Doctor owns the registry, the concurrent runner and the generic checks;
subsystems own their own checks and register them at startup
(``docs/specs/design/trust-and-ops.md`` §5.5).  Reserved ids for checks whose
owning subsystem has not landed yet are listed in
:data:`~src.doctor.models.RESERVED_CHECK_IDS`, together with the registration
contract those owners must follow.
"""

from src.doctor.builtin import builtin_checks
from src.doctor.capability_checks import capability_checks
from src.doctor.db_checks import db_checks
from src.doctor.formula_checks import formula_checks
from src.doctor.hierarchy_checks import hierarchy_checks
from src.doctor.integration_checks import integration_checks
from src.doctor.intelligence_class_checks import intelligence_class_checks
from src.doctor.models import (
    RESERVED_CHECK_IDS,
    CheckResult,
    DoctorCheck,
    DoctorContext,
    Severity,
)
from src.doctor.playbook_v2_checks import playbook_v2_checks
from src.doctor.pool_checks import pool_checks
from src.doctor.project_checks import project_checks
from src.doctor.profile_checks import profile_checks
from src.doctor.resource_checks import resource_checks
from src.doctor.runner import DoctorRegistry, exit_code_for, run_doctor
from src.doctor.session_checks import session_checks
from src.doctor.task_checks import task_checks
from src.doctor.workspace_checks import workspace_checks

__all__ = [
    "RESERVED_CHECK_IDS",
    "CheckResult",
    "DoctorCheck",
    "DoctorContext",
    "DoctorRegistry",
    "Severity",
    "builtin_checks",
    "capability_checks",
    "db_checks",
    "default_registry",
    "exit_code_for",
    "formula_checks",
    "integration_checks",
    "intelligence_class_checks",
    "playbook_v2_checks",
    "project_checks",
    "profile_checks",
    "resource_checks",
    "run_doctor",
    "session_checks",
    "task_checks",
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
    for check in task_checks():
        registry.register(check)
    for check in formula_checks():
        registry.register(check)
    for check in intelligence_class_checks():
        registry.register(check)
    for check in resource_checks():
        registry.register(check)
    for check in session_checks():
        registry.register(check)
    for check in integration_checks():
        registry.register(check)
    for check in capability_checks():
        registry.register(check)
    for check in workspace_checks():
        registry.register(check)
    for check in profile_checks():
        registry.register(check)
    for check in db_checks():
        registry.register(check)
    for check in playbook_v2_checks():
        registry.register(check)
    for check in project_checks():
        registry.register(check)
    return registry
