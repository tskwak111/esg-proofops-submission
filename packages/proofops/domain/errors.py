"""ProofOps domain errors. Pure: no I/O, no environment access."""

from __future__ import annotations


class ProofOpsError(Exception):
    """Base error for the proofops domain package."""


class DomainValidationError(ProofOpsError, ValueError):
    """Raised when an immutable domain value violates its contract."""


class DtoValidationError(ProofOpsError, ValueError):
    """Raised when an API DTO violates the fixed contract boundary."""
