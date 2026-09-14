"""The Mediator: the only component in AiS permitted to touch real files."""

from ais.mediator.mediator import Mediator, MediationError, ScopeViolation

__all__ = ["Mediator", "MediationError", "ScopeViolation"]
