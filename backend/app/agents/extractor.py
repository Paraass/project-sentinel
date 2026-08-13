"""Claim extraction.

Deterministic, line-based — no LLM. Every claim carries provenance
(document_id + source_location) by construction; _validate_provenance is
the explicit gate that rejects anything that doesn't, so provenance is
enforced, not just assumed to always be present.

Document content is DATA ONLY here. This module never interprets a line of
document text as an instruction to itself — every line, no matter what it
says, becomes at most a claim to report on. There is no code path in this
module that branches on document content to change what function runs next;
control flow is fixed regardless of what the text contains.
"""
import re
from dataclasses import dataclass

_KEY_VALUE_PATTERN = re.compile(r"^([A-Za-z][A-Za-z0-9 _-]{2,40}):\s*(.+)$")
_MIN_CLAIM_LENGTH = 8


class ClaimProvenanceError(Exception):
    """Raised when a claim is missing the provenance it must always carry."""


@dataclass(frozen=True)
class ExtractedClaim:
    claim_id: str
    document_id: str
    source_location: str
    text: str
    claim_type: str
    key: str | None = None
    value: str | None = None


def _validate_provenance(claim: ExtractedClaim) -> None:
    """Reject a claim that lacks genuine provenance.

    Called for every claim this module produces, and safe to call again by
    any caller that receives claims from elsewhere (e.g. reconstructed from
    a persisted checkpoint) and wants the same guarantee re-checked.
    """
    if not claim.document_id or not claim.source_location or not claim.text:
        raise ClaimProvenanceError(
            f"claim {claim.claim_id!r} is missing required provenance "
            f"(document_id={claim.document_id!r}, "
            f"source_location={claim.source_location!r}, "
            f"text={claim.text!r})"
        )


def _classify_line(line: str) -> str:
    lowered = line.lower()
    if lowered.startswith("req:") or " shall " in lowered or " must " in lowered:
        return "requirement"
    if lowered.startswith("decision:"):
        return "decision"
    if _KEY_VALUE_PATTERN.match(line):
        return "key_value"
    return "statement"


def extract_claims(document_id: str, content: str) -> list[ExtractedClaim]:
    """Extract one claim per non-trivial line of a document's text content.

    Deterministic: the same content always produces the same claims in the
    same order, with claim_id derived from document_id + line number rather
    than anything random — reproducible across runs and across processes,
    and easy to assert on directly in tests.
    """
    claims: list[ExtractedClaim] = []

    for line_number, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.strip()
        if len(line) < _MIN_CLAIM_LENGTH:
            continue
        if line.startswith("#"):  # markdown-style heading: structure, not a claim
            continue

        claim_type = _classify_line(line)
        key = value = None
        if claim_type == "key_value":
            match = _KEY_VALUE_PATTERN.match(line)
            key = match.group(1).strip().lower()
            value = match.group(2).strip()

        claim = ExtractedClaim(
            claim_id=f"{document_id}:{line_number}",
            document_id=document_id,
            source_location=f"line {line_number}",
            text=line,
            claim_type=claim_type,
            key=key,
            value=value,
        )
        _validate_provenance(claim)
        claims.append(claim)

    return claims
