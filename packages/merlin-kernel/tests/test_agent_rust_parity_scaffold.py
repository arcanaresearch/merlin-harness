"""Executable Rust-parity contract for the next agent-service iteration.

The deterministic coordinator is deliberately test-local.  Replace it with the
production boundary as parity behavior lands; the assertions are the acceptance
contract and do not require a model, clock, database, or network.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest


@dataclass(slots=True)
class Agent:
    name: str
    role: str
    scope: frozenset[str]
    parent: str | None = None
    status: str = "idle"
    lease: int = 0
    generation: int = 0


@dataclass(slots=True)
class Task:
    id: str
    required: str
    result: str | None = None
    contributions: list[str] = field(default_factory=list)


class DeterministicParityCoordinator:
    """Small behavioral fake matching the Rust coordinator's public concepts."""

    def __init__(self, *, graph_limit: int = 4) -> None:
        self.now = 0
        self.graph_limit = graph_limit
        self.agents: dict[str, Agent] = {}
        self.tasks: dict[str, Task] = {}
        self.mailbox: dict[str, list[str]] = {}
        self.cancelled: set[str] = set()

    def add(self, name: str, role: str, scope: set[str], parent: str | None = None) -> Agent:
        if parent and not scope <= self.agents[parent].scope:
            raise ValueError("child scope must narrow its parent")
        agent = Agent(name, role, frozenset(scope), parent)
        self.agents[name] = agent
        self.mailbox.setdefault(name, [])
        return agent

    def route(self, required: str) -> Agent:
        candidates = [agent for agent in self.agents.values() if required in agent.scope]
        specialists = [agent for agent in candidates if agent.role == required]
        return min(specialists or candidates, key=lambda agent: (len(agent.scope), agent.name))

    def run_validated(self, explorer: str, validator: str, evidence: str) -> str:
        assert self.agents[explorer].role == "explorer"
        assert self.agents[validator].role == "validator"
        if evidence != "verified":
            raise ValueError("validator rejected evidence")
        return "accepted"

    def temporary_specialist(self, parent: str, name: str, role: str) -> Agent:
        parent_agent = self.agents[parent]
        if role not in parent_agent.scope:
            raise ValueError("temporary specialist exceeds parent scope")
        return self.add(name, role, {role}, parent)

    def digest(self) -> tuple[str, ...]:
        edges = tuple(
            sorted(
                f"{agent.parent}->{agent.name}" for agent in self.agents.values() if agent.parent
            )
        )
        if len(edges) > self.graph_limit:
            raise ValueError("agent graph exceeds configured bound")
        return edges

    def lease(self, name: str, duration: int = 5) -> tuple[str, int]:
        agent = self.agents[name]
        agent.lease = self.now + duration
        agent.generation += 1
        agent.status = "running"
        return name, agent.generation

    def replace_expired(self, name: str) -> tuple[str, int]:
        old = self.agents[name]
        assert self.now > old.lease
        old.status = "expired"
        replacement = Agent(
            name,
            old.role,
            old.scope,
            old.parent,
            "running",
            self.now + 5,
            old.generation + 1,
        )
        self.agents[name] = replacement
        return name, replacement.generation

    def commit(self, lease: tuple[str, int], task_id: str, result: str) -> None:
        name, generation = lease
        if self.agents[name].generation != generation:
            raise ValueError("stale lease")
        if task_id in self.cancelled:
            raise ValueError("task cancelled")
        self.tasks[task_id].contributions.append(result)

    def cancel(self, task_id: str) -> None:
        self.cancelled.add(task_id)
        for agent in self.agents.values():
            if agent.status == "running":
                agent.status = "idle"

    def send(self, recipient: str, message: str) -> None:
        self.mailbox.setdefault(recipient, []).append(message)

    def restart(self) -> DeterministicParityCoordinator:
        restored = DeterministicParityCoordinator(graph_limit=self.graph_limit)
        restored.agents = self.agents.copy()
        restored.tasks = self.tasks.copy()
        restored.mailbox = {name: messages.copy() for name, messages in self.mailbox.items()}
        restored.cancelled = self.cancelled.copy()
        restored.now = self.now
        return restored

    def fan_in(self, task_id: str) -> str:
        task = self.tasks[task_id]
        task.result = "\n".join(task.contributions)
        return task.result


def coordinator() -> DeterministicParityCoordinator:
    service = DeterministicParityCoordinator()
    service.add("lead", "lead", {"explorer", "validator", "network"})
    service.add("explorer", "explorer", {"explorer"}, "lead")
    service.add("validator", "validator", {"validator"}, "lead")
    return service


def test_specialist_routing_explorer_and_validator_gate() -> None:
    service = coordinator()
    assert service.route("explorer").name == "explorer"
    assert service.run_validated("explorer", "validator", "verified") == "accepted"
    with pytest.raises(ValueError, match="rejected"):
        service.run_validated("explorer", "validator", "unverified")


def test_temporary_specialist_is_narrower_and_removed_from_route_when_done() -> None:
    service = coordinator()
    specialist = service.temporary_specialist("lead", "network-1", "network")
    assert specialist.scope < service.agents["lead"].scope
    assert service.route("network") is specialist
    del service.agents[specialist.name]
    assert service.route("network").name == "lead"


def test_graph_digest_is_deterministic_and_bounded() -> None:
    service = coordinator()
    assert service.digest() == ("lead->explorer", "lead->validator")
    service.graph_limit = 1
    with pytest.raises(ValueError, match="bound"):
        service.digest()


def test_expired_lease_is_replaced_and_stale_commit_is_rejected() -> None:
    service = coordinator()
    service.tasks["scan"] = Task("scan", "explorer")
    stale = service.lease("explorer", duration=2)
    service.now = 3
    current = service.replace_expired("explorer")
    with pytest.raises(ValueError, match="stale lease"):
        service.commit(stale, "scan", "old")
    service.commit(current, "scan", "fresh")
    assert service.fan_in("scan") == "fresh"


def test_cancellation_cleans_workers_and_rejects_late_results() -> None:
    service = coordinator()
    service.tasks["scan"] = Task("scan", "explorer")
    lease = service.lease("explorer")
    service.cancel("scan")
    assert service.agents["explorer"].status == "idle"
    with pytest.raises(ValueError, match="cancelled"):
        service.commit(lease, "scan", "late")


def test_recursive_children_must_narrow_every_parent_scope() -> None:
    service = coordinator()
    child = service.add("child", "explorer", {"explorer"}, "lead")
    grandchild = service.add("grandchild", "explorer", {"explorer"}, child.name)
    assert grandchild.scope <= child.scope <= service.agents["lead"].scope
    with pytest.raises(ValueError, match="narrow"):
        service.add("escape", "network", {"network"}, child.name)


def test_mailbox_survives_restart_and_fan_in_preserves_all_results() -> None:
    service = coordinator()
    service.tasks["scan"] = Task("scan", "explorer")
    service.send("explorer", "prioritize auth")
    service.tasks["scan"].contributions.extend(("headers: safe", "body: vulnerable"))

    restored = service.restart()

    assert restored.mailbox["explorer"] == ["prioritize auth"]
    assert restored.fan_in("scan") == "headers: safe\nbody: vulnerable"
