"""Consolidation.

Combines extracted claims into one grounded deliverable. With no LLM in
this batch, "consolidating" cannot mean rewriting or summarizing claims in
prose without risking inventing unsupported facts — so this deliverable is
every claim, organized and still carrying its original provenance, not a
paraphrase of them. That is the honest thing a deterministic consolidator
can do; prose synthesis is a later batch's job, once there's a model doing
it under the same evidence-over-assumptions constraint.

Deliberately does not drop or resolve contradictory claims — that is
conflict_scanner's job, run separately. Consolidation's only job is to
organize what was extracted, not to judge it.
"""
from dataclasses import dataclass

from app.agents.extractor import ExtractedClaim


@dataclass(frozen=True)
class ConsolidatedStatement:
    claim_id: str
    document_id: str
    text: str
    source_location: str
    claim_type: str


def consolidate(claims: list[ExtractedClaim]) -> list[ConsolidatedStatement]:
    """Build the deliverable: one statement per claim, in stable order.

    Order is deterministic (insertion order of the input, which is itself
    deterministic per extractor.py) — not sorted or scored, since there is
    no criterion here that wouldn't just be a disguised invented judgment.
    """
    return [
        ConsolidatedStatement(
            claim_id=claim.claim_id,
            document_id=claim.document_id,
            text=claim.text,
            source_location=claim.source_location,
            claim_type=claim.claim_type,
        )
        for claim in claims
    ]
