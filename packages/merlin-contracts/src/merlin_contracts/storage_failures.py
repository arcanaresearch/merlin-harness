from __future__ import annotations

from .observability import InfrastructureFailureCategory


def storage_failure_category(error: BaseException) -> InfrastructureFailureCategory | None:
    """Recognize PostgreSQL disk-full by SQLSTATE, including explicit wrappers."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if getattr(current, "sqlstate", None) == "53100":
            return InfrastructureFailureCategory.STORAGE_EXHAUSTED
        current = current.__cause__
    return None
