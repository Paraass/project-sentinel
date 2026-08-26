from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
import csv
import re


@dataclass(frozen=True)
class Target:
    company: str
    target_type: str  # public or synthetic
    buyer_role: str
    workflow: str
    evidence: str
    channel: str
    trigger: str


@dataclass(frozen=True)
class EnrichedTarget:
    target: Target
    fit_score: int
    fit_reasons: tuple[str, ...]
    personalization: str


@dataclass(frozen=True)
class OutreachDraft:
    company: str
    buyer_role: str
    subject: str
    body: str
    safety: str


ALLOWED_BUYER = "Proposal Manager / Proposal Operations Lead"
WEDGE = "government RFP response and compliance-package production"


def load_targets(path: str | Path) -> list[Target]:
    rows: list[Target] = []
    with Path(path).open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append(Target(**{k: row[k].strip() for k in Target.__dataclass_fields__}))
    return rows


def _score(t: Target) -> tuple[int, tuple[str, ...]]:
    text = " ".join([t.workflow, t.evidence, t.trigger]).lower()
    score = 0
    reasons: list[str] = []
    checks = [
        ("rfp" in text or "solicitation" in text, 30, "RFP or solicitation work is explicit"),
        ("compliance" in text or "matrix" in text, 25, "Compliance or traceability work is explicit"),
        ("proposal" in text or "bid" in text, 20, "Proposal production is explicit"),
        ("attachment" in text or "document" in text, 10, "Document-heavy workflow is explicit"),
        ("deadline" in text or "amendment" in text, 10, "Recurring or deadline-driven change is visible"),
        (t.target_type == "public", 5, "Public research target can be verified without private data"),
    ]
    for ok, points, reason in checks:
        if ok:
            score += points
            reasons.append(reason)
    return min(score, 100), tuple(reasons)


def enrich(t: Target) -> EnrichedTarget:
    score, reasons = _score(t)
    personalization = (
        f"The relevant workflow at {t.company} is framed around {t.workflow.lower()}. "
        f"The useful trigger is {t.trigger.lower()}, so the opening should focus on the document artifact "
        f"rather than generic AI writing."
    )
    return EnrichedTarget(t, score, reasons, personalization)


def select_targets(targets: list[Target], minimum_score: int = 55) -> list[EnrichedTarget]:
    enriched = [enrich(t) for t in targets]
    return sorted(
        [e for e in enriched if e.fit_score >= minimum_score],
        key=lambda e: (-e.fit_score, e.target.company.lower()),
    )


def draft(e: EnrichedTarget) -> OutreachDraft:
    # Deliberately produces drafts only. It never sends, queues, or contacts anyone.
    subject = "A tighter way to maintain proposal documents"
    body = (
        f"Hi {e.target.buyer_role},\n\n"
        f"I was looking at the recurring document work around {e.target.workflow.lower()}. "
        f"The interesting problem is not generating another block of prose. It is keeping the existing "
        f"response artifact aligned when requirements, attachments, approved content, or amendments change.\n\n"
        f"For a team doing this work, I would test a small workflow: source requirement → affected response location "
        f"→ controlled edit → review → export. The point is to make the change traceable without replacing the "
        f"proposal owner's judgment.\n\n"
        f"I built a small synthetic/public-data demo around that workflow. It is designed to be reviewed before any "
        f"real outreach, so nothing is being sent from this system.\n\n"
        f"If this workflow is relevant, the useful next step would be reviewing the artifact rather than taking a sales call."
    )
    return OutreachDraft(e.target.company, e.target.buyer_role, subject, body, "DRAFT ONLY: no send action exists in this machine.")


def run(input_csv: str | Path, output_dir: str | Path, minimum_score: int = 55) -> dict:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    targets = load_targets(input_csv)
    selected = select_targets(targets, minimum_score)
    drafts = [draft(e) for e in selected]

    with (output / "selected_targets.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["company", "target_type", "buyer_role", "workflow", "fit_score", "fit_reasons", "personalization"])
        writer.writeheader()
        for e in selected:
            writer.writerow({
                "company": e.target.company,
                "target_type": e.target.target_type,
                "buyer_role": e.target.buyer_role,
                "workflow": e.target.workflow,
                "fit_score": e.fit_score,
                "fit_reasons": " | ".join(e.fit_reasons),
                "personalization": e.personalization,
            })

    with (output / "drafts.md").open("w", encoding="utf-8") as f:
        f.write("# Outreach drafts\n\n")
        f.write("These are review-only drafts. This machine has no sending capability.\n\n")
        for d in drafts:
            f.write(f"## {d.company}\n\n**Subject:** {d.subject}\n\n{d.body}\n\n> {d.safety}\n\n")

    metrics = {
        "input_targets": len(targets),
        "selected_targets": len(selected),
        "selection_rate": round(len(selected) / len(targets), 3) if targets else 0,
        "public_targets_selected": sum(e.target.target_type == "public" for e in selected),
        "synthetic_targets_selected": sum(e.target.target_type == "synthetic" for e in selected),
        "average_fit_score": round(sum(e.fit_score for e in selected) / len(selected), 1) if selected else 0,
        "drafts_created": len(drafts),
        "messages_sent": 0,
        "human_review_required": len(drafts),
    }
    (output / "metrics.json").write_text(__import__("json").dumps(metrics, indent=2), encoding="utf-8")
    return metrics
