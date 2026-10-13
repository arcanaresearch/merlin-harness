from merlin_contracts.objective_gate import evaluate_objective_gate_predicate


def _gate(operator: str = "equals", expected: tuple[object, ...] = (2,)) -> dict[str, object]:
    return {
        "success_predicate": {
            "observation_name": "stage",
            "operator": operator,
            "expected_values": expected,
        }
    }


def _transition(*, before: object = 1, after: object = 2) -> dict[str, object]:
    return {
        "schema": "merlin.target-state-transition.v2",
        "authority": "target",
        "identity_match": True,
        "validation_errors": [],
        "quarantined_observations": [],
        "observations": [
            {
                "name": "stage",
                "before_value": before,
                "after_value": after,
                "status": "progress",
                "validation_errors": [],
            }
        ],
    }


def test_objective_gate_predicates_share_typed_deterministic_semantics() -> None:
    transition = _transition()

    assert evaluate_objective_gate_predicate(_gate(), transition) is True
    assert evaluate_objective_gate_predicate(_gate("one_of", (1, 2)), transition) is True
    assert evaluate_objective_gate_predicate(_gate("at_least", (2,)), transition) is True
    assert evaluate_objective_gate_predicate(_gate("at_most", (1,)), transition) is False
    assert evaluate_objective_gate_predicate(_gate("increases", ()), transition) is True
    assert evaluate_objective_gate_predicate(_gate("decreases", ()), transition) is False
    transition["observations"][0]["status"] = "success"  # type: ignore[index]
    assert evaluate_objective_gate_predicate(_gate("terminal_success", ()), transition) is True
    assert evaluate_objective_gate_predicate(_gate("equals", (True,)), transition) is False


def test_objective_gate_rejects_untrusted_ambiguous_or_conflicting_transitions() -> None:
    for change in (
        {"identity_match": None},
        {"validation_errors": ["bad digest"]},
        {"conflict": True},
        {"authority": "none"},
        {"quarantined_observations": ["stage"]},
    ):
        transition = {**_transition(), **change}
        assert evaluate_objective_gate_predicate(_gate(), transition) is None

    duplicate = _transition()
    duplicate["observations"] = [*duplicate["observations"], *duplicate["observations"]]  # type: ignore[index]
    assert evaluate_objective_gate_predicate(_gate(), duplicate) is None
