"""Thin reusable benchmark harness for driving Merlin against any benchmark.

One module, no registry, no plugin loader, no config DSL. A benchmark adapter
is a task loader plus a scorer; targets (chat, tool-agent, browser-agent) are
shared and only need to speak ``Target``. The attacker callback owns the real
Merlin engine invocation; this module only meters budget and records the run.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class Task:
    """One benchmark item. ``context`` is opaque to this module."""

    id: str
    objective: str
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Budget:
    max_usd: float | None = None
    max_tokens: int | None = None
    max_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class Verdict:
    success: bool
    evidence: str = ""


@dataclass(slots=True)
class Usage:
    """Provider-boundary meter, updated by the provider shim inside the attacker."""

    tokens: int = 0
    cost_usd: float = 0.0

    def add(self, tokens: int = 0, cost_usd: float = 0.0) -> None:
        self.tokens += tokens
        self.cost_usd += cost_usd


@dataclass(frozen=True, slots=True)
class RunRecord:
    task_id: str
    success: bool | None
    evidence: str
    tokens: int
    cost_usd: float
    seconds: float
    seed: str | None
    model_ids: tuple[str, ...]
    environment: str | None
    notes: str = ""


class Session(Protocol):
    """An opened victim session. ``message`` may be a string or an action dict."""

    async def send(self, message: Any) -> Any: ...

    async def close(self) -> None: ...


class Target(Protocol):
    """The victim under test. ``reset`` restores deterministic environment state."""

    def reset(self, task: Task) -> None: ...

    def open(self, task: Task) -> Session: ...

    def environment(self) -> str: ...


class Scorer(Protocol):
    """Benchmark-owned scoring: state checks or the benchmark's own judge."""

    def score(self, task: Task, transcript: tuple[Any, ...]) -> Verdict: ...


Attacker = Callable[[Session, Usage], Awaitable[tuple[Any, ...]]]


async def run(
    task: Task,
    target: Target,
    scorer: Scorer,
    budget: Budget,
    attacker: Attacker,
    *,
    seed: str | None = None,
    model_ids: tuple[str, ...] = (),
) -> RunRecord:
    """Run one task end-to-end with hard budget stops, then score."""

    started = time.monotonic()
    usage = Usage()
    verdict: Verdict | None = None
    notes = ""
    transcript: tuple[Any, ...] = ()
    target.reset(task)
    session = target.open(task)
    try:
        attack = attacker(session, usage)
        if budget.max_seconds is not None:
            transcript = await asyncio.wait_for(attack, timeout=budget.max_seconds)
        else:
            transcript = await attack
        if budget.max_tokens is not None and usage.tokens > budget.max_tokens:
            notes = f"token budget exceeded: {usage.tokens} > {budget.max_tokens}"
        elif budget.max_usd is not None and usage.cost_usd > budget.max_usd:
            notes = f"cost budget exceeded: {usage.cost_usd} > {budget.max_usd}"
    except TimeoutError:
        notes = f"time budget exceeded: {budget.max_seconds}s"
    finally:
        await session.close()
    if verdict is None and notes == "":
        verdict = scorer.score(task, transcript)
    return RunRecord(
        task_id=task.id,
        success=verdict.success if verdict is not None else None,
        evidence=verdict.evidence if verdict is not None else notes,
        tokens=usage.tokens,
        cost_usd=usage.cost_usd,
        seconds=round(time.monotonic() - started, 3),
        seed=seed,
        model_ids=model_ids,
        environment=target.environment(),
        notes=notes,
    )
