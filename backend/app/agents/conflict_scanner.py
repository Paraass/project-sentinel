"""Conflict detection.

Deterministic — no LLM. Scoped narrowly and honestly: without semantic
understanding, this module can only reliably detect one shape of
disagreement — two different documents asserting different values for the
same named fact (a "key: value" line, e.g. "Status: Approved" in one
document and "Status: Rejected" in another). That is the fact pattern the
extractor already tags as claim_type == "key_value", so this module reads
exactly that subset and nothing else.

Never resolves a conflict — only surfaces it. A corpus with no such
disagreements is a valid, successful, zero-conflict result, not treated as
incomplete.
"""
from collections import defaultdict
from dataclasses import dataclass

from app.agents.extractor import ExtractedClaim


@dataclass(frozen=True)
class Conflict:
    conflict_id: str
    key: str
    claim_ids: list[str]
    document_ids: list[str]
    values: list[str]


def scan_conflicts(claims: list[ExtractedClaim]) -> list[Conflict]:
    """Group key_value claims by key; flag any key with more than one
    distinct value across different documents as a conflict.

    Two claims from the *same* document repeating the same key aren't
    treated specially here — this is about disagreement between sources,
    so what matters is distinct values, regardless of how many documents
    contributed them.
    """
    by_key: dict[str, list[ExtractedClaim]] = defaultdict(list)
    for claim in claims:
        if claim.claim_type == "key_value" and claim.key is not None:
            by_key[claim.key].append(claim)

    conflicts: list[Conflict] = []
    for key in sorted(by_key):  # sorted: deterministic conflict ordering
        group = by_key[key]
        distinct_values = sorted({c.value for c in group if c.value is not None})
        if len(distinct_values) <= 1:
            continue  # everyone agrees (or there's only one assertion) — no conflict

        conflicts.append(
            Conflict(
                conflict_id=f"conflict:{key}",
                key=key,
                claim_ids=[c.claim_id for c in group],
                document_ids=sorted({c.document_id for c in group}),
                values=distinct_values,
            )
        )

    return conflicts
