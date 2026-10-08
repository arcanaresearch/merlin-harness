"""Digest-bound declarative automation with a deterministic, non-agent executor.

Only artifact kinds with an executable runtime are accepted or advertised:
`SUPPORTED_ARTIFACT_KINDS` is exactly the key set of `ARTIFACT_KIND_RUNTIMES`.
A `bounded_procedure` kind is deliberately unimplemented — it has no reviewed
runtime, so it is rejected at the request and artifact layers until one exists.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import urlsplit


class AutomationError(ValueError):
    """Raised when an automation artifact violates its sealed contract."""


class ArtifactState(StrEnum):
    AUTHORED = "authored"
    AUDITOR_ACCEPTED = "auditor_accepted"
    AUDITOR_REJECTED = "auditor_rejected"
    ADMITTED = "admitted"


class RunState(StrEnum):
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    AMBIGUOUS = "ambiguous"
    CANCELLED = "cancelled"


AUTOMATION_ARTIFACT_SCHEMA = "merlin.declarative-flow.v2"
ARTIFACT_KIND_RUNTIMES = {
    "declarative_browser_flow": AUTOMATION_ARTIFACT_SCHEMA,
}
SUPPORTED_ARTIFACT_KINDS = frozenset(ARTIFACT_KIND_RUNTIMES)
_SLEEP_OPERATION_SUFFIXES = ("sleep", "wait", "delay", "pause")
_WAIT_CONDITION_KEYS = frozenset(
    {"until", "wait_for", "condition", "observe", "observation", "selector", "event"}
)
_SELECTOR_STEP_MARKERS = ("fill", "click")


@dataclass(frozen=True, slots=True)
class AutomationStep:
    step_id: str
    operation: str
    arguments: Mapping[str, Any]
    evidence_keys: tuple[str, ...] = ()
    cleanup: bool = False

    def __post_init__(self) -> None:
        if not self.step_id.strip() or not self.operation.strip():
            raise AutomationError("automation step identity and operation are required")
        _canonical_json(dict(self.arguments))
        if len(self.evidence_keys) != len(set(self.evidence_keys)):
            raise AutomationError("automation evidence keys must be unique")


@dataclass(frozen=True, slots=True)
class AutomationArtifact:
    request_id: str
    owning_attempt_id: str
    author_identity: str
    author_model: str
    origin: str
    steps: tuple[AutomationStep, ...]
    max_steps: int
    artifact_kind: str = "declarative_browser_flow"
    schema: str = AUTOMATION_ARTIFACT_SCHEMA
    digest: str = ""
    state: ArtifactState = ArtifactState.AUTHORED

    def __post_init__(self) -> None:
        required = (
            self.request_id,
            self.owning_attempt_id,
            self.author_identity,
            self.author_model,
            self.origin,
        )
        if any(not value.strip() for value in required):
            raise AutomationError(
                "artifact identity, owner, author, model, and origin are required"
            )
        if self.schema != AUTOMATION_ARTIFACT_SCHEMA:
            raise AutomationError("artifact does not use the current digest schema")
        if self.artifact_kind not in SUPPORTED_ARTIFACT_KINDS:
            raise AutomationError("artifact kind is unsupported")
        if not self.steps or self.max_steps <= 0 or len(self.steps) > self.max_steps:
            raise AutomationError("artifact steps must fit the positive sealed step bound")
        ids = tuple(step.step_id for step in self.steps)
        if len(ids) != len(set(ids)):
            raise AutomationError("artifact step IDs must be unique")
        expected = artifact_digest(self)
        if not self.digest:
            object.__setattr__(self, "digest", expected)
        elif self.digest != expected:
            raise AutomationError("artifact digest does not match immutable content")


@dataclass(frozen=True, slots=True)
class AutomationAudit:
    artifact_digest: str
    auditor_identity: str
    auditor_model: str
    decision: str
    reasons: tuple[str, ...]
    static_check_refs: tuple[str, ...] = ()
    fixture_evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.decision not in {"accept", "reject", "revise"}:
            raise AutomationError("unsupported automation audit decision")
        if not self.artifact_digest.startswith("sha256:"):
            raise AutomationError("audit must bind an artifact digest")
        if not self.auditor_identity.strip() or not self.auditor_model.strip():
            raise AutomationError("audit identity and model are required")
        if not self.reasons:
            raise AutomationError("audit must include attributable reasons")
        if self.decision == "accept" and (
            not self.static_check_refs or not self.fixture_evidence_refs
        ):
            raise AutomationError("accepted audit requires static-check and fixture evidence")


@dataclass(frozen=True, slots=True)
class ArtifactAdmission:
    artifact_digest: str
    owning_attempt_id: str
    origin: str
    allowed_operations: frozenset[str]
    environment_digest: str
    fixture_evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.allowed_operations:
            raise AutomationError("admission must name allowed operations")
        if not self.environment_digest.startswith("sha256:"):
            raise AutomationError("admission requires a sealed environment digest")


@dataclass(frozen=True, slots=True)
class ArtifactEvent:
    step_id: str
    operation: str
    status: str
    evidence_refs: tuple[str, ...] = ()
    effect_state: str = "none"


@dataclass(frozen=True, slots=True)
class ArtifactRun:
    run_id: str
    artifact_digest: str
    owning_attempt_id: str
    state: RunState
    events: tuple[ArtifactEvent, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    error: str | None = None


@dataclass(frozen=True, slots=True)
class AutomationRequest:
    request_id: str
    owning_attempt_id: str
    objective: str
    origin: str
    allowed_operations: frozenset[str]
    evidence_expectations: tuple[str, ...]
    cleanup_required: bool = True
    artifact_kind: str = "declarative_browser_flow"

    def __post_init__(self) -> None:
        if any(
            not value.strip()
            for value in (
                self.request_id,
                self.owning_attempt_id,
                self.objective,
                self.origin,
                *self.allowed_operations,
                *self.evidence_expectations,
            )
        ):
            raise AutomationError("automation request fields must not be blank")
        if not self.allowed_operations or not self.evidence_expectations:
            raise AutomationError("automation request requires operations and evidence")
        if self.artifact_kind not in SUPPORTED_ARTIFACT_KINDS:
            raise AutomationError("automation request names an unsupported artifact kind")


@dataclass(frozen=True, slots=True)
class AutomationOutcome:
    request_id: str
    owning_attempt_id: str
    stage: str
    state: str
    artifact_digest: str | None = None
    run: ArtifactRun | None = None
    error: str | None = None


class AutomationJournal(Protocol):
    def record_artifact(self, artifact: AutomationArtifact) -> None: ...
    def record_audit(self, audit: AutomationAudit) -> None: ...
    def record_admission(self, admission: ArtifactAdmission) -> None: ...
    def record_run(self, run: ArtifactRun) -> None: ...


class MemoryAutomationJournal:
    """Reference journal used by tests and embedders; production may persist the same records."""

    def __init__(self) -> None:
        self.artifacts: dict[str, AutomationArtifact] = {}
        self.audits: dict[str, AutomationAudit] = {}
        self.admissions: dict[str, ArtifactAdmission] = {}
        self.runs: dict[str, ArtifactRun] = {}

    def record_artifact(self, artifact: AutomationArtifact) -> None:
        _insert_immutable(self.artifacts, artifact.digest, artifact)

    def record_audit(self, audit: AutomationAudit) -> None:
        _insert_immutable(self.audits, audit.artifact_digest, audit)

    def record_admission(self, admission: ArtifactAdmission) -> None:
        _insert_immutable(self.admissions, admission.artifact_digest, admission)

    def record_run(self, run: ArtifactRun) -> None:
        current = self.runs.get(run.run_id)
        if current is not None and (
            current.artifact_digest != run.artifact_digest
            or current.owning_attempt_id != run.owning_attempt_id
        ):
            raise AutomationError("artifact run identity cannot be rebound")
        self.runs[run.run_id] = run


class AutomationPipeline:
    def __init__(self, journal: AutomationJournal) -> None:
        self._journal = journal

    def author(self, artifact: AutomationArtifact) -> AutomationArtifact:
        self._journal.record_artifact(artifact)
        return artifact

    def audit(self, artifact: AutomationArtifact, audit: AutomationAudit) -> AutomationArtifact:
        if audit.artifact_digest != artifact.digest:
            raise AutomationError("audit does not bind the supplied artifact")
        if audit.auditor_identity == artifact.author_identity:
            raise AutomationError("artifact author cannot audit their own artifact")
        self._journal.record_audit(audit)
        state = (
            ArtifactState.AUDITOR_ACCEPTED
            if audit.decision == "accept"
            else ArtifactState.AUDITOR_REJECTED
        )
        return replace(artifact, state=state)

    def admit(
        self, artifact: AutomationArtifact, audit: AutomationAudit, admission: ArtifactAdmission
    ) -> AutomationArtifact:
        if audit.decision != "accept" or artifact.state != ArtifactState.AUDITOR_ACCEPTED:
            raise AutomationError("only an independently accepted artifact can be admitted")
        if len({artifact.digest, audit.artifact_digest, admission.artifact_digest}) != 1:
            raise AutomationError("artifact, audit, and admission digests differ")
        if admission.owning_attempt_id != artifact.owning_attempt_id:
            raise AutomationError("admission cannot rebind the owning technique attempt")
        if admission.origin != artifact.origin:
            raise AutomationError("admission cannot change the reviewed origin")
        operations = {step.operation for step in artifact.steps}
        if not operations.issubset(admission.allowed_operations):
            raise AutomationError("artifact requests an operation outside admission")
        validate_step_admission(artifact)
        self._journal.record_admission(admission)
        return replace(artifact, state=ArtifactState.ADMITTED)


DispatchResult = Mapping[str, Any]
ArtifactDispatcher = Callable[[AutomationStep], DispatchResult | Awaitable[DispatchResult]]
ArtifactAuthor = Callable[[AutomationRequest], AutomationArtifact | Awaitable[AutomationArtifact]]
ArtifactAuditor = Callable[
    [AutomationArtifact, tuple[str, ...], tuple[str, ...]],
    AutomationAudit | Awaitable[AutomationAudit],
]
FixtureRunner = Callable[[AutomationArtifact], Sequence[str] | Awaitable[Sequence[str]]]


class ArtifactExecutor:
    """Execute an admitted flow exactly; this class contains no model invocation."""

    def __init__(self, journal: AutomationJournal, dispatch: ArtifactDispatcher) -> None:
        self._journal = journal
        self._dispatch = dispatch

    async def execute(
        self, run_id: str, artifact: AutomationArtifact, admission: ArtifactAdmission
    ) -> ArtifactRun:
        if artifact.state != ArtifactState.ADMITTED or admission.artifact_digest != artifact.digest:
            raise AutomationError("executor requires the exact admitted artifact")
        if admission.owning_attempt_id != artifact.owning_attempt_id:
            raise AutomationError("executor admission is bound to another technique attempt")
        run = ArtifactRun(
            run_id=run_id,
            artifact_digest=artifact.digest,
            owning_attempt_id=artifact.owning_attempt_id,
            state=RunState.EXECUTING,
        )
        self._journal.record_run(run)
        events: list[ArtifactEvent] = []
        refs: list[str] = []
        terminal = RunState.COMPLETED
        error: str | None = None
        cleanup_steps = tuple(step for step in artifact.steps if step.cleanup)
        execution_steps = tuple(step for step in artifact.steps if not step.cleanup)

        async def dispatch_step(step: AutomationStep) -> ArtifactEvent:
            result = self._dispatch(step)
            if inspect.isawaitable(result):
                result = await result
            evidence = result.get("evidence", {})
            if not isinstance(evidence, Mapping):
                raise AutomationError("artifact dispatcher evidence must be a mapping")
            missing = set(step.evidence_keys).difference(evidence)
            if missing:
                raise AutomationError(
                    "artifact step omitted required evidence: " + ", ".join(sorted(missing))
                )
            evidence_refs = tuple(str(evidence[key]) for key in step.evidence_keys)
            evidence_refs += tuple(str(ref) for ref in result.get("evidence_refs", ()))
            return ArtifactEvent(
                step_id=step.step_id,
                operation=step.operation,
                status=str(result.get("status", "failed")),
                evidence_refs=tuple(dict.fromkeys(evidence_refs)),
                effect_state=str(result.get("effect_state", "none")),
            )

        cancelled_error: BaseException | None = None
        try:
            for step in execution_steps:
                event = await dispatch_step(step)
                events.append(event)
                refs.extend(event.evidence_refs)
                if event.effect_state == "ambiguous":
                    terminal, error = RunState.AMBIGUOUS, "ambiguous target effect"
                    break
                if event.status != "completed":
                    terminal, error = RunState.FAILED, "step failed"
                    break
        except BaseException as exc:
            cancelled = isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit))
            terminal = RunState.CANCELLED if cancelled else RunState.FAILED
            error = f"{type(exc).__name__}: {exc}"
            if cancelled:
                cancelled_error = exc
        finally:
            for step in cleanup_steps:
                try:
                    event = await dispatch_step(step)
                except BaseException as exc:
                    event = ArtifactEvent(
                        step_id=step.step_id,
                        operation=step.operation,
                        status="failed",
                    )
                    if error is None:
                        terminal = RunState.FAILED
                        error = f"cleanup {type(exc).__name__}: {exc}"
                events.append(event)
                refs.extend(event.evidence_refs)
                if event.effect_state == "ambiguous":
                    terminal, error = RunState.AMBIGUOUS, "ambiguous cleanup effect"
                elif event.status != "completed" and error is None:
                    terminal, error = RunState.FAILED, "cleanup step failed"
            final = ArtifactRun(
                run_id=run_id,
                artifact_digest=artifact.digest,
                owning_attempt_id=artifact.owning_attempt_id,
                state=terminal,
                events=tuple(events),
                evidence_refs=tuple(dict.fromkeys(refs)),
                error=error,
            )
            self._journal.record_run(final)
        if cancelled_error is not None:
            raise cancelled_error
        return final


class ArtifactFixtureRunner:
    """Execute an authored flow against a controlled fixture dispatcher, never the real broker.

    Every trial must produce identical, digest-bound receipts; a non-deterministic fixture
    or a missing evidence kind fails admission instead of being retried away.
    """

    def __init__(self, dispatch: ArtifactDispatcher, *, trials: int = 3) -> None:
        if trials <= 0:
            raise AutomationError("fixture trials must be positive")
        self._dispatch = dispatch
        self._trials = trials

    async def run(self, artifact: AutomationArtifact) -> tuple[str, ...]:
        receipts: list[str] = []
        baseline: str | None = None
        for trial in range(self._trials):
            events: list[Mapping[str, Any]] = []
            for step in artifact.steps:
                result = self._dispatch(step)
                if inspect.isawaitable(result):
                    result = await result
                if str(result.get("status", "failed")) != "completed":
                    raise AutomationError(f"fixture step {step.step_id} did not complete")
                evidence = result.get("evidence", {})
                if not isinstance(evidence, Mapping):
                    raise AutomationError("fixture dispatcher evidence must be a mapping")
                missing = set(step.evidence_keys).difference(evidence)
                if missing:
                    raise AutomationError(
                        "fixture omitted required evidence kinds: " + ", ".join(sorted(missing))
                    )
                produced = {key: str(evidence[key]) for key in step.evidence_keys}
                if any(not value.strip() for value in produced.values()):
                    raise AutomationError("fixture produced blank evidence")
                events.append(
                    {
                        "step_id": step.step_id,
                        "operation": step.operation,
                        "evidence": produced,
                        "effect_state": str(result.get("effect_state", "none")),
                    }
                )
            receipt = _record_hash({"artifact_digest": artifact.digest, "events": events})
            if baseline is None:
                baseline = receipt
            elif receipt != baseline:
                raise AutomationError("fixture execution is non-deterministic across trials")
            receipts.append(f"fixture-run:{trial}:{receipt}")
        return tuple(receipts)


class AutomationCoordinator:
    """Run the separated author, audit, fixture, admission, and executor stages."""

    def __init__(
        self,
        *,
        pipeline: AutomationPipeline,
        author: ArtifactAuthor,
        auditor: ArtifactAuditor,
        fixture_runner: FixtureRunner,
        executor: ArtifactExecutor,
        environment_digest: str,
    ) -> None:
        if not environment_digest.startswith("sha256:"):
            raise AutomationError("automation coordinator requires a sealed environment")
        self._pipeline = pipeline
        self._author = author
        self._auditor = auditor
        self._fixture_runner = fixture_runner
        self._executor = executor
        self._environment_digest = environment_digest

    async def execute(self, request: AutomationRequest, *, run_id: str) -> AutomationOutcome:
        stage = "author"
        artifact: AutomationArtifact | None = None
        try:
            artifact = await _resolve(self._author(request))
            self._validate_authored_request(request, artifact)
            artifact = self._pipeline.author(artifact)

            stage = "static_check"
            static_refs = self._static_check(artifact, request)

            stage = "fixture"
            fixture_refs = tuple(str(ref) for ref in await _resolve(self._fixture_runner(artifact)))
            if not fixture_refs or any(not ref.strip() for ref in fixture_refs):
                raise AutomationError("fixture evaluation produced no attributable evidence")

            stage = "audit"
            audit = await _resolve(self._auditor(artifact, static_refs, fixture_refs))
            artifact = self._pipeline.audit(artifact, audit)
            if artifact.state != ArtifactState.AUDITOR_ACCEPTED:
                raise AutomationError("automation auditor did not accept the artifact")

            stage = "admission"
            admission = ArtifactAdmission(
                artifact_digest=artifact.digest,
                owning_attempt_id=request.owning_attempt_id,
                origin=request.origin,
                allowed_operations=request.allowed_operations,
                environment_digest=self._environment_digest,
                fixture_evidence_refs=fixture_refs,
            )
            artifact = self._pipeline.admit(artifact, audit, admission)

            stage = "execution"
            run = await self._executor.execute(run_id, artifact, admission)
            return AutomationOutcome(
                request_id=request.request_id,
                owning_attempt_id=request.owning_attempt_id,
                artifact_digest=artifact.digest,
                stage=stage,
                state=run.state.value,
                run=run,
                error=run.error,
            )
        except Exception as exc:
            return AutomationOutcome(
                request_id=request.request_id,
                owning_attempt_id=request.owning_attempt_id,
                artifact_digest=None if artifact is None else artifact.digest,
                stage=stage,
                state="failed",
                error=f"{type(exc).__name__}: {exc}",
            )

    @staticmethod
    def _validate_authored_request(
        request: AutomationRequest, artifact: AutomationArtifact
    ) -> None:
        if artifact.request_id != request.request_id:
            raise AutomationError("authored artifact is bound to another request")
        if artifact.owning_attempt_id != request.owning_attempt_id:
            raise AutomationError("authored artifact is bound to another technique attempt")
        if artifact.origin != request.origin:
            raise AutomationError("authored artifact changed the reviewed origin")
        if artifact.artifact_kind != request.artifact_kind:
            raise AutomationError("authored artifact changed the requested artifact kind")
        if artifact.artifact_kind not in ARTIFACT_KIND_RUNTIMES:
            raise AutomationError("artifact kind has no executable runtime")
        if request.cleanup_required and not any(step.cleanup for step in artifact.steps):
            raise AutomationError("authored artifact omitted required cleanup")

    @staticmethod
    def _static_check(artifact: AutomationArtifact, request: AutomationRequest) -> tuple[str, ...]:
        operations = {step.operation for step in artifact.steps}
        if not operations.issubset(request.allowed_operations):
            raise AutomationError("authored artifact exceeds requested operations")
        validate_step_admission(artifact)
        evidence_keys = {key for step in artifact.steps for key in step.evidence_keys}
        missing = set(request.evidence_expectations).difference(evidence_keys)
        if missing:
            raise AutomationError(
                "authored artifact omits requested evidence: " + ", ".join(sorted(missing))
            )
        expected_origin = urlsplit(request.origin)
        sealed_origin = (expected_origin.scheme.lower(), expected_origin.netloc.lower())
        for step in artifact.steps:
            for value in _string_values(step.arguments):
                parsed = urlsplit(value)
                if (
                    parsed.scheme in {"http", "https"}
                    and (parsed.scheme.lower(), parsed.netloc.lower()) != sealed_origin
                ):
                    raise AutomationError("authored artifact references an unreviewed origin")
        receipt = {
            "artifact_digest": artifact.digest,
            "operations": sorted(operations),
            "evidence_keys": sorted(evidence_keys),
            "cleanup": any(step.cleanup for step in artifact.steps),
        }
        return ("static-check:" + _record_hash(receipt),)


def validate_step_admission(artifact: AutomationArtifact) -> None:
    """Reject blind sleeps and unstable selectors before fixture or executor dispatch."""
    for step in artifact.steps:
        operation = step.operation.casefold()
        is_sleep = (
            operation in _SLEEP_OPERATION_SUFFIXES
            or operation.split("_")[-1] in _SLEEP_OPERATION_SUFFIXES
        )
        if is_sleep:
            conditioned = any(
                key in step.arguments and _has_content(step.arguments[key])
                for key in _WAIT_CONDITION_KEYS
            )
            if not conditioned:
                raise AutomationError(
                    f"step {step.step_id} is an unconditional fixed sleep; "
                    "an observation or wait condition is required"
                )
        if artifact.artifact_kind == "declarative_browser_flow" and any(
            marker in operation for marker in _SELECTOR_STEP_MARKERS
        ):
            if not _stable_selector(step.arguments.get("selector")):
                raise AutomationError(
                    f"step {step.step_id} must reference a reviewed selector with a fallback"
                )


def _has_content(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return any(_has_content(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_content(item) for item in value)
    return value is not None


def _stable_selector(value: Any) -> bool:
    if isinstance(value, Mapping):
        primary = value.get("primary")
        fallbacks = value.get("fallbacks", value.get("fallback"))
        return _has_content(primary) and (
            _has_content(fallbacks)
            and (not isinstance(fallbacks, (list, tuple)) or bool(fallbacks))
        )
    if isinstance(value, (list, tuple)):
        return len(value) >= 2 and all(isinstance(item, str) and item.strip() for item in value)
    return False


def artifact_digest(artifact: AutomationArtifact) -> str:
    payload = {
        "schema": artifact.schema,
        "artifact_kind": artifact.artifact_kind,
        "request_id": artifact.request_id,
        "owning_attempt_id": artifact.owning_attempt_id,
        "author_identity": artifact.author_identity,
        "author_model": artifact.author_model,
        "origin": artifact.origin,
        "max_steps": artifact.max_steps,
        "steps": [
            {
                "step_id": step.step_id,
                "operation": step.operation,
                "arguments": dict(step.arguments),
                "evidence_keys": step.evidence_keys,
                "cleanup": step.cleanup,
            }
            for step in artifact.steps
        ],
    }
    return "sha256:" + hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


async def _resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _record_hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise AutomationError("automation content must be canonical JSON") from exc


def _insert_immutable(store: dict[str, Any], key: str, value: Any) -> None:
    current = store.get(key)
    if current is not None and current != value:
        raise AutomationError("immutable automation record conflicts with existing content")
    store[key] = value


def _string_values(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _string_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _string_values(item)
