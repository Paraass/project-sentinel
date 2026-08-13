"""Rule validation.

Deterministic, data-driven — no LLM. A small, fixed set of rule TYPES (the
"verbs"): forbidden_keyword, required_keyword, required_key_value. WHICH
keywords/keys/allowed-values to check is pure data read from a Rule row's
parameters, never hardcoded per-rule branching — adding a new rule never
means writing new Python, only a new row (see workflow_service.create_rule_set).

Document/claim content is DATA ONLY here, exactly like extractor.py.
Nothing in this module treats claim or statement text as an instruction to
itself — a rule is checked the same way regardless of what the evidence
text says, including text that reads like an instruction to ignore the
rule. There is no code path here that branches on document/claim content
to change which function runs; only rule_type (a value the *rule*, not the
*document*, supplies) selects the checker.
"""
import enum
from dataclasses import dataclass


class FindingType(str, enum.Enum):
    VIOLATION = "VIOLATION"
    CANNOT_EVALUATE = "CANNOT_EVALUATE"


@dataclass(frozen=True)
class RuleInput:
    """What the validator needs to know about one rule. Deliberately not
    the ORM Rule object itself — agents take data in, return data out, no
    ORM/session dependency, per the architecture's Agent Module boundary.
    """

    rule_id: str
    identifier: str
    rule_type: str
    parameters: dict


@dataclass(frozen=True)
class Finding:
    rule_id: str
    finding_type: FindingType
    explanation: str
    evidence: str
    affected_claim_id: str | None = None


def _check_forbidden_keyword(rule: RuleInput, statements: list[dict], claims: list[dict]) -> Finding | None:
    keyword = rule.parameters["keyword"]
    lowered_keyword = keyword.lower()
    for statement in statements:
        if lowered_keyword in statement["text"].lower():
            return Finding(
                rule_id=rule.rule_id,
                finding_type=FindingType.VIOLATION,
                explanation=f"Forbidden keyword '{keyword}' was found in the deliverable",
                evidence=statement["text"],
                affected_claim_id=statement.get("claim_id"),
            )
    return None


def _check_required_keyword(rule: RuleInput, statements: list[dict], claims: list[dict]) -> Finding | None:
    keyword = rule.parameters["keyword"]
    lowered_keyword = keyword.lower()
    for statement in statements:
        if lowered_keyword in statement["text"].lower():
            return None  # satisfied — no finding
    return Finding(
        rule_id=rule.rule_id,
        finding_type=FindingType.VIOLATION,
        explanation=f"Required keyword '{keyword}' was not found anywhere in the deliverable",
        evidence="(absent from all statements in the deliverable)",
    )


def _check_required_key_value(rule: RuleInput, statements: list[dict], claims: list[dict]) -> Finding | None:
    key = rule.parameters["key"].lower()
    allowed_values = set(rule.parameters["allowed_values"])

    matches = [c for c in claims if c.get("claim_type") == "key_value" and c.get("key") == key]

    if not matches:
        # No claim anywhere asserts a value for this key at all — this is
        # genuinely undecidable from the evidence available, not a pass
        # and not a violation. Never silently treated as either.
        return Finding(
            rule_id=rule.rule_id,
            finding_type=FindingType.CANNOT_EVALUATE,
            explanation=f"No claim asserts a value for '{rule.parameters['key']}' — insufficient evidence to evaluate this rule",
            evidence="(no matching key found among extracted claims)",
        )

    for claim in matches:
        if claim.get("value") not in allowed_values:
            return Finding(
                rule_id=rule.rule_id,
                finding_type=FindingType.VIOLATION,
                explanation=(
                    f"'{rule.parameters['key']}' has value '{claim.get('value')}', "
                    f"which is not among the allowed values {sorted(allowed_values)}"
                ),
                evidence=claim["text"],
                affected_claim_id=claim.get("claim_id"),
            )

    return None  # every matching claim had an allowed value — satisfied


_CHECKS = {
    "forbidden_keyword": _check_forbidden_keyword,
    "required_keyword": _check_required_keyword,
    "required_key_value": _check_required_key_value,
}


def validate(
    rules: list[RuleInput], statements: list[dict], claims: list[dict]
) -> list[Finding]:
    """Run every rule against the deliverable/claims.

    Returns only findings (violations and cannot-evaluates) — a satisfied
    rule contributes nothing to the result, so a clean corpus/rule set
    legitimately produces an empty list, not a list padded with "passed"
    placeholders.
    """
    findings: list[Finding] = []
    for rule in rules:
        checker = _CHECKS.get(rule.rule_type)
        if checker is None:
            # An unrecognized rule_type is exactly as undecidable as
            # missing evidence — this validator has no way to check it, so
            # it says so rather than skipping the rule silently.
            findings.append(
                Finding(
                    rule_id=rule.rule_id,
                    finding_type=FindingType.CANNOT_EVALUATE,
                    explanation=f"Unknown rule_type '{rule.rule_type}' — no checker registered for it",
                    evidence="(no checker available for this rule type)",
                )
            )
            continue

        result = checker(rule, statements, claims)
        if result is not None:
            findings.append(result)

    return findings
