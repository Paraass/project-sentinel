"""Impact analysis for Movement 3 (the living document).

Deterministic — no LLM. For every statement in the current committed
report, decides whether any newly extracted claim is relevant to it.
Relevance has two confident outcomes and one default:

- Confidently RELATED: both are key_value claims sharing the same key —
  a direct, unambiguous match on the same named fact.
- Confidently UNRELATED: no shared key and no significant word overlap at
  all in the claim text.
- Anything else is ambiguous, and ambiguous always counts as affecting the
  section — never silently unaffected. Every new claim is checked against
  every baseline statement; nothing is skipped.

Document/claim content is DATA ONLY here, same as every other agent in
this codebase.
"""
from dataclasses import dataclass, field

_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
    "is", "are", "this", "that", "must", "shall", "will",
}


def _significant_words(text: str) -> set[str]:
    return {
        cleaned
        for word in text.split()
        if len(cleaned := word.strip(".,:;!?").lower()) >= 4 and cleaned not in _STOPWORDS
    }


@dataclass(frozen=True)
class ImpactResult:
    affected_claim_ids: list[str]
    unaffected_claim_ids: list[str]
    # For each affected baseline claim_id, which new claim_id(s) triggered it.
    triggers: dict[str, list[str]] = field(default_factory=dict)


def analyze_impact(new_claims: list[dict], baseline_statements: list[dict]) -> ImpactResult:
    """new_claims are shaped like extractor.ExtractedClaim dicts.
    baseline_statements come from the current Report's content — each one
    carries claim_id/text/claim_type and, where available, key/value.
    """
    affected: set[str] = set()
    triggers: dict[str, list[str]] = {}

    for baseline in baseline_statements:
        baseline_words = _significant_words(baseline["text"])
        for new_claim in new_claims:
            related = False

            if (
                baseline.get("claim_type") == "key_value"
                and new_claim.get("claim_type") == "key_value"
                and baseline.get("key")
                and baseline.get("key") == new_claim.get("key")
            ):
                related = True
            else:
                new_words = _significant_words(new_claim["text"])
                if baseline_words & new_words:
                    related = True  # ambiguous overlap -> counts as affecting

            if related:
                affected.add(baseline["claim_id"])
                triggers.setdefault(baseline["claim_id"], []).append(new_claim["claim_id"])

    all_ids = {b["claim_id"] for b in baseline_statements}
    unaffected = sorted(all_ids - affected)

    return ImpactResult(
        affected_claim_ids=sorted(affected),
        unaffected_claim_ids=unaffected,
        triggers=triggers,
    )
