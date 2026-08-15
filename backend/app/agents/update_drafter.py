"""Focused update drafting for Movement 3.

Deterministic — no LLM. Produces a diff-shaped proposal covering only
affected sections (baseline claim_ids flagged by impact_analyzer.py),
citing the new source document for every change. A same-key/different-value
pairing is a genuine contradiction and is surfaced as a conflict, never
silently resolved by picking one side. Any other affected-but-not-
contradictory relation is proposed as an update citing what the new claim
actually asserts — this module never claims a fact was replaced when it
only has grounds to say a related claim appeared.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ProposedChange:
    baseline_claim_id: str
    new_claim_id: str
    new_document_id: str
    change_type: str  # "update"
    proposed_text: str
    source_citation: str
    new_claim_type: str
    key: str | None = None
    value: str | None = None


@dataclass(frozen=True)
class DraftConflict:
    key: str
    baseline_claim_id: str
    baseline_value: str
    new_claim_id: str
    new_value: str
    new_document_id: str


@dataclass(frozen=True)
class DraftResult:
    proposed_changes: list[ProposedChange]
    conflicts: list[DraftConflict]


def draft_update(
    affected_claim_ids: list[str],
    triggers: dict[str, list[str]],
    baseline_by_id: dict[str, dict],
    new_claims_by_id: dict[str, dict],
) -> DraftResult:
    """Only claim_ids in affected_claim_ids ever appear in the output —
    unaffected sections are never even inspected here, let alone included.
    """
    proposed_changes: list[ProposedChange] = []
    conflicts: list[DraftConflict] = []

    for baseline_id in affected_claim_ids:
        baseline = baseline_by_id[baseline_id]
        for new_claim_id in triggers.get(baseline_id, []):
            new_claim = new_claims_by_id[new_claim_id]

            is_direct_contradiction = (
                baseline.get("claim_type") == "key_value"
                and new_claim.get("claim_type") == "key_value"
                and baseline.get("key")
                and baseline.get("key") == new_claim.get("key")
                and baseline.get("value") != new_claim.get("value")
            )

            if is_direct_contradiction:
                conflicts.append(
                    DraftConflict(
                        key=baseline["key"],
                        baseline_claim_id=baseline_id,
                        baseline_value=baseline["value"],
                        new_claim_id=new_claim_id,
                        new_value=new_claim.get("value", ""),
                        new_document_id=new_claim["document_id"],
                    )
                )
                continue

            proposed_changes.append(
                ProposedChange(
                    baseline_claim_id=baseline_id,
                    new_claim_id=new_claim_id,
                    new_document_id=new_claim["document_id"],
                    change_type="update",
                    proposed_text=new_claim["text"],
                    source_citation=new_claim["source_location"],
                    new_claim_type=new_claim.get("claim_type", "statement"),
                    key=new_claim.get("key"),
                    value=new_claim.get("value"),
                )
            )

    return DraftResult(proposed_changes=proposed_changes, conflicts=conflicts)
