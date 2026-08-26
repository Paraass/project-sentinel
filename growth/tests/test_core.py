from pathlib import Path
from growth_machine.core import load_targets, select_targets, run

ROOT = Path(__file__).parents[1]


def test_fresh_batches_have_same_pipeline_contract():
    a = load_targets(ROOT / "data" / "targets_run1.csv")
    b = load_targets(ROOT / "data" / "targets_run2.csv")
    assert a and b
    assert {t.buyer_role for t in a + b} == {"Proposal Manager / Proposal Operations Lead"}
    assert all(t.company for t in a + b)


def test_selection_is_deterministic():
    targets = load_targets(ROOT / "data" / "targets_run1.csv")
    first = [(e.target.company, e.fit_score) for e in select_targets(targets)]
    second = [(e.target.company, e.fit_score) for e in select_targets(targets)]
    assert first == second
    assert first


def test_run_never_sends_messages(tmp_path):
    metrics = run(ROOT / "data" / "targets_run1.csv", tmp_path)
    assert metrics["messages_sent"] == 0
    assert metrics["drafts_created"] == metrics["human_review_required"]
    assert (tmp_path / "selected_targets.csv").exists()
    assert (tmp_path / "drafts.md").exists()
    assert (tmp_path / "metrics.json").exists()
