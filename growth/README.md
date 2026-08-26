# Proposal Reach Machine

A small, deterministic Growth Machine for one narrow audience: **Proposal Managers / Proposal Operations Leads handling government RFP responses and compliance packages**.

## Why this audience

The Task 3 research identified government RFP response and compliance-package production as the clearest first wedge. The workflow repeatedly moves between solicitations, attachments, requirements, compliance matrices, approved content, controlled edits, review and export. The machine therefore focuses on that document job rather than generic "AI writing".

## What the machine does

1. Loads a fresh batch of public or synthetic company-level research targets.
2. Scores each target against explicit RFP/compliance/proposal/document signals.
3. Produces a short enrichment rationale and personalization hook.
4. Generates a review-only outreach draft tied to the document workflow.
5. Writes machine metrics and artifacts to a run directory.
6. Can process another fresh batch without changing the code.

## Safety boundary

This is an **outbound preparation machine, not a sending bot**. It contains no email provider, no DM integration, no contact lookup, and no send function. `messages_sent` is intentionally always zero. Every draft is marked for human review.

Real companies in the research data are company-level examples only. No real people or contact details are stored. Synthetic targets are clearly labeled.

## Run

```bash
python -m venv .venv
# activate the environment for your OS
pip install -r requirements.txt
python run.py
pytest -q
```

The example command runs both fresh batches and produces:

- `examples/run1/selected_targets.csv`
- `examples/run1/drafts.md`
- `examples/run1/metrics.json`
- `examples/run2/selected_targets.csv`
- `examples/run2/drafts.md`
- `examples/run2/metrics.json`

## Architecture

`input batch -> deterministic fit scoring -> enrichment -> personalized draft -> human review queue -> metrics/artifacts`

The machine is intentionally small. The assignment rewards a working channel over three speculative channels, so this implementation does one outbound workflow end to end.

## What is deliberately not claimed

The machine does not prove buying intent, current demand, or conversion. It also does not send outreach. The research treats company fit as a hypothesis and keeps that boundary intact.
