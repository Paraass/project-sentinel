"""Document classification.

Deterministic and keyword-based — no LLM, no external API. Per the
architecture's "Agent Modules never perform I/O" boundary, this module
takes already-decoded text as input and returns data; reading the document's
stored bytes is the caller's (graph node's) job.
"""
import enum
from dataclasses import dataclass

CONFIDENCE_THRESHOLD = 0.4


class DocumentType(str, enum.Enum):
    PRD = "PRD"
    SRS = "SRS"
    ARCHITECTURE = "ARCHITECTURE"
    API_SPEC = "API_SPEC"
    MEETING_NOTES = "MEETING_NOTES"
    UNKNOWN = "UNKNOWN"


# Each type's signal keywords. Confidence is the fraction of a type's
# keywords found in the document, so more specific/longer keyword lists
# make a match harder to reach by accident — deliberate, not tunable per
# document, so results are reproducible across runs and across processes.
_KEYWORDS: dict[DocumentType, list[str]] = {
    DocumentType.PRD: [
        "product requirements",
        "user story",
        "as a user,",
        "acceptance criteria",
        "target audience",
        "user persona",
    ],
    DocumentType.SRS: [
        "software requirements specification",
        "shall support",
        "functional requirement",
        "non-functional requirement",
        "use case diagram",
    ],
    DocumentType.ARCHITECTURE: [
        "system architecture",
        "component diagram",
        "data flow",
        "microservice",
        "deployment diagram",
    ],
    DocumentType.API_SPEC: [
        "endpoint:",
        "request body",
        "response body",
        "status code",
        "http method",
    ],
    DocumentType.MEETING_NOTES: [
        "attendees:",
        "agenda:",
        "action items",
        "meeting notes",
        "minutes of meeting",
    ],
}


@dataclass(frozen=True)
class ClassificationResult:
    document_id: str
    document_type: DocumentType
    confidence: float
    evidence: str
    is_uncertain: bool


def classify_document(document_id: str, content: str) -> ClassificationResult:
    """Classify one document's text content.

    If the best-scoring type's confidence is below CONFIDENCE_THRESHOLD,
    is_uncertain is True — the caller must not treat document_type as a
    settled fact in that case, only as a best guess exposed for a human (or
    a later batch's escalation path) to look at. This function never
    silently upgrades a low-confidence guess into a certain one; that
    judgment call belongs one layer up, not buried in a scoring formula.
    """
    lowered = content.lower()

    scores: dict[DocumentType, tuple[float, list[str]]] = {}
    for doc_type, keywords in _KEYWORDS.items():
        matched = [kw for kw in keywords if kw in lowered]
        confidence = len(matched) / len(keywords)
        scores[doc_type] = (confidence, matched)

    best_type = max(scores, key=lambda t: scores[t][0])
    best_confidence, best_matches = scores[best_type]

    if best_confidence == 0.0:
        return ClassificationResult(
            document_id=document_id,
            document_type=DocumentType.UNKNOWN,
            confidence=0.0,
            evidence="no classification keywords matched",
            is_uncertain=True,
        )

    evidence = f"matched: {', '.join(best_matches)}"
    return ClassificationResult(
        document_id=document_id,
        document_type=best_type,
        confidence=best_confidence,
        evidence=evidence,
        is_uncertain=best_confidence < CONFIDENCE_THRESHOLD,
    )
