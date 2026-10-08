from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Literal

from .sanitize import sanitize

CheckStatus = Literal["ok", "failed"]
Check = Callable[[], object | Awaitable[object]]


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    status: CheckStatus
    detail: str


@dataclass(frozen=True, slots=True)
class DoctorReport:
    checks: tuple[CheckResult, ...]

    @property
    def healthy(self) -> bool:
        return all(check.status == "ok" for check in self.checks)


class Doctor:
    """Runs named production checks without owning their resources."""

    def __init__(self, checks: Iterable[tuple[str, Check]]) -> None:
        self._checks = tuple(checks)

    async def run(self) -> DoctorReport:
        results = []
        for name, check in self._checks:
            try:
                value = check()
                if inspect.isawaitable(value):
                    value = await value
                detail = "ready" if value is None or value is True else str(value)
                results.append(CheckResult(name, "ok", sanitize(detail)))
            except Exception as exc:
                results.append(CheckResult(name, "failed", sanitize(str(exc))))
        return DoctorReport(tuple(results))


def import_check(module: str) -> Check:
    def check() -> str:
        __import__(module)
        return "importable"

    return check


def postgres_check(connect: Callable[[], object]) -> Check:
    def check() -> str:
        connection = connect()
        try:
            connection.execute("SELECT 1").fetchone()  # type: ignore[attr-defined]
            return "connection ready"
        finally:
            connection.close()  # type: ignore[attr-defined]

    return check
