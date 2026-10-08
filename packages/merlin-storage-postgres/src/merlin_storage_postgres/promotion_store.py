from __future__ import annotations

import json
from typing import Any, Literal

from merlin_contracts import CandidateStatus, PromotionCandidate, PromotionEvaluation
from merlin_contracts.promotion import CURRENT_METRIC_SEMANTICS_VERSION

OperatorDecision = Literal["accepted", "rejected"]


class PromotionRepository:
    """Append-only promotion history with an atomically replaceable active pointer."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def propose(self, candidate: PromotionCandidate) -> PromotionCandidate:
        self.connection.execute(
            """INSERT INTO merlin_storage.promotion_candidates
               (id, kind, version, baseline_version, baseline_digest, content, content_digest,
                proposed_by, profile_id, profile_operation, graph_local_composition)
               VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)""",
            (
                candidate.id,
                candidate.kind,
                candidate.version,
                candidate.baseline_version,
                candidate.baseline_digest,
                json.dumps(candidate.content),
                candidate.content_digest,
                candidate.proposed_by,
                candidate.profile_id,
                candidate.profile_operation,
                candidate.graph_local_composition,
            ),
        )
        self.connection.commit()
        return candidate

    def record_evaluation(self, evaluation: PromotionEvaluation) -> PromotionEvaluation:
        candidate = self.get(evaluation.candidate_id)
        if candidate is None or candidate.content_digest != evaluation.candidate_digest:
            raise ValueError("evaluation candidate digest does not match persisted candidate")
        if candidate.baseline_version != evaluation.baseline_version:
            raise ValueError("evaluation baseline version does not match candidate")
        if candidate.baseline_digest != evaluation.baseline_digest:
            raise ValueError("evaluation baseline digest does not match candidate")
        self.connection.execute(
            """INSERT INTO merlin_storage.promotion_evaluations
               (id, candidate_id, record, passed) VALUES (%s, %s, %s::jsonb, %s)""",
            (
                evaluation.id,
                evaluation.candidate_id,
                evaluation.model_dump_json(),
                evaluation.passed,
            ),
        )
        self.connection.commit()
        return evaluation

    def operator_decide(
        self,
        candidate_id: str,
        decision: OperatorDecision,
        *,
        operator_id: str,
        reason: str,
        actor_kind: str = "operator",
    ) -> None:
        if actor_kind != "operator" or not operator_id.strip():
            raise PermissionError("promotion decisions require an explicit operator")
        if decision not in ("accepted", "rejected") or not reason.strip():
            raise ValueError("operator decision and reason are required")
        self.connection.execute(
            """INSERT INTO merlin_storage.promotion_decisions
               (candidate_id, operator_id, decision, reason) VALUES (%s, %s, %s, %s)""",
            (candidate_id, operator_id, decision, reason),
        )
        self.connection.commit()

    def activate(self, candidate_id: str, *, operator_id: str) -> int:
        with self.connection.transaction():
            candidate = self.get(candidate_id)
            if candidate is None:
                raise ValueError("candidate not found")
            if not candidate.permanently_promotable:
                raise ValueError("graph-local specialist compositions cannot be promoted")
            approved = self.connection.execute(
                """SELECT 1 FROM merlin_storage.promotion_decisions
                   WHERE candidate_id = %s AND decision = 'accepted'""",
                (candidate_id,),
            ).fetchone()
            passed = self.connection.execute(
                """SELECT 1 FROM merlin_storage.promotion_evaluations
                   WHERE candidate_id = %s AND passed = true
                     AND record->>'metric_semantics_version' = %s""",
                (candidate_id, CURRENT_METRIC_SEMANTICS_VERSION),
            ).fetchone()
            if approved is None or passed is None:
                raise ValueError("candidate requires passing evaluation and operator approval")
            active = self.connection.execute(
                """SELECT candidate_id FROM merlin_storage.active_promotions
                   WHERE kind = %s FOR UPDATE""",
                (candidate.kind,),
            ).fetchone()
            previous = active["candidate_id"] if active else None
            row = self.connection.execute(
                """INSERT INTO merlin_storage.promotion_activations
                   (kind, candidate_id, previous_candidate_id, action, operator_id)
                   VALUES (%s, %s, %s, 'activated', %s) RETURNING id""",
                (candidate.kind, candidate.id, previous, operator_id),
            ).fetchone()
            self.connection.execute(
                """INSERT INTO merlin_storage.active_promotions (kind, candidate_id, activation_id)
                   VALUES (%s, %s, %s) ON CONFLICT (kind) DO UPDATE SET
                   candidate_id = EXCLUDED.candidate_id, activation_id = EXCLUDED.activation_id""",
                (candidate.kind, candidate.id, row["id"]),
            )
            return int(row["id"])

    def rollback(self, kind: str, *, operator_id: str) -> int:
        with self.connection.transaction():
            active = self.connection.execute(
                """SELECT candidate_id FROM merlin_storage.active_promotions
                   WHERE kind = %s FOR UPDATE""",
                (kind,),
            ).fetchone()
            if active is None:
                raise ValueError("no active candidate to roll back")
            activation = self.connection.execute(
                """SELECT previous_candidate_id FROM merlin_storage.promotion_activations
                   WHERE kind = %s AND candidate_id = %s AND action = 'activated'
                   ORDER BY id DESC LIMIT 1""",
                (kind, active["candidate_id"]),
            ).fetchone()
            previous = activation["previous_candidate_id"] if activation else None
            if previous is None:
                raise ValueError("active candidate has no prior version")
            row = self.connection.execute(
                """INSERT INTO merlin_storage.promotion_activations
                   (kind, candidate_id, previous_candidate_id, action, operator_id)
                   VALUES (%s, %s, %s, 'rolled_back', %s) RETURNING id""",
                (kind, previous, active["candidate_id"], operator_id),
            ).fetchone()
            self.connection.execute(
                """UPDATE merlin_storage.active_promotions
                   SET candidate_id = %s, activation_id = %s WHERE kind = %s""",
                (previous, row["id"], kind),
            )
            return int(row["id"])

    def get(self, candidate_id: str) -> PromotionCandidate | None:
        row = self.connection.execute(
            "SELECT * FROM merlin_storage.promotion_candidates WHERE id = %s", (candidate_id,)
        ).fetchone()
        if row is None:
            return None
        return PromotionCandidate(
            id=row["id"],
            kind=row["kind"],
            version=row["version"],
            baseline_version=row["baseline_version"],
            baseline_digest=row["baseline_digest"],
            content=row["content"],
            content_digest=row["content_digest"],
            proposed_by=row["proposed_by"],
            profile_id=row["profile_id"],
            profile_operation=row["profile_operation"],
            graph_local_composition=row["graph_local_composition"],
        )

    def active(self, kind: str) -> PromotionCandidate | None:
        row = self.connection.execute(
            "SELECT candidate_id FROM merlin_storage.active_promotions WHERE kind = %s", (kind,)
        ).fetchone()
        return self.get(row["candidate_id"]) if row else None

    def status(self, candidate_id: str) -> CandidateStatus:
        if self.get(candidate_id) is None:
            raise ValueError("candidate not found")
        rolled_back = self.connection.execute(
            """SELECT 1 FROM merlin_storage.promotion_activations
               WHERE previous_candidate_id = %s AND action = 'rolled_back'""",
            (candidate_id,),
        ).fetchone()
        if rolled_back:
            return "rolled_back"
        decision = self.connection.execute(
            "SELECT decision FROM merlin_storage.promotion_decisions WHERE candidate_id = %s",
            (candidate_id,),
        ).fetchone()
        if decision:
            return decision["decision"]
        evaluated = self.connection.execute(
            "SELECT 1 FROM merlin_storage.promotion_evaluations WHERE candidate_id = %s",
            (candidate_id,),
        ).fetchone()
        return "evaluated" if evaluated else "proposed"

    def history(self, kind: str) -> list[dict[str, Any]]:
        return list(
            self.connection.execute(
                """SELECT candidate_id, previous_candidate_id, action, operator_id, activated_at
                   FROM merlin_storage.promotion_activations WHERE kind = %s ORDER BY id""",
                (kind,),
            ).fetchall()
        )

    def list_candidates(self, *, limit: int = 100) -> list[PromotionCandidate]:
        rows = self.connection.execute(
            """SELECT id FROM merlin_storage.promotion_candidates
               ORDER BY proposed_at DESC, id DESC LIMIT %s""",
            (limit,),
        ).fetchall()
        return [candidate for row in rows if (candidate := self.get(row["id"])) is not None]

    def evaluations(self, candidate_id: str) -> list[PromotionEvaluation]:
        rows = self.connection.execute(
            """SELECT record FROM merlin_storage.promotion_evaluations
               WHERE candidate_id = %s ORDER BY recorded_at, id""",
            (candidate_id,),
        ).fetchall()
        return [PromotionEvaluation.model_validate(row["record"]) for row in rows]
