from __future__ import annotations

from merlin_contracts import PromotionCandidate

from .adapter_evaluator import AdapterCaseEvaluator
from .browser_evaluator import BrowserAdapterCaseEvaluator
from .controller_evaluator import CONTROLLER_STRATEGIES, ControllerCaseEvaluator
from .provider_evaluator import ProviderCaseEvaluator
from .service import EvaluationBinding, EvaluationCase


class CandidateCaseEvaluator:
    """Dispatch frozen cases only to the executor matching the candidate domain."""

    def __init__(
        self,
        provider: ProviderCaseEvaluator,
        adapter: AdapterCaseEvaluator,
        browser: BrowserAdapterCaseEvaluator | None = None,
        controller: ControllerCaseEvaluator | None = None,
    ) -> None:
        self._provider = provider
        self._adapter = adapter
        self._browser = browser
        self._controller = controller

    async def __call__(
        self,
        candidate: PromotionCandidate,
        case: EvaluationCase,
        binding: EvaluationBinding,
    ):
        if candidate.kind == "routing":
            if candidate.content.get("strategy_id") in CONTROLLER_STRATEGIES:
                if self._controller is None:
                    raise ValueError("controller evaluator is unavailable")
                return await self._controller(candidate, case, binding)
            return await self._provider(candidate, case, binding)
        if candidate.kind == "adapter":
            if candidate.content.get("interface") == "browser":
                if self._browser is None:
                    raise ValueError("browser adapter evaluator is unavailable")
                return await self._browser(candidate, case, binding)
            return await self._adapter(candidate, case, binding)
        raise ValueError(f"no production evaluator is available for {candidate.kind} candidates")
