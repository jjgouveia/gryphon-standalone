"""Contract tests for the Elephant-Goldfish split in the shipped review skills.

The point of the split is that the context that prepares the facts is not
the context that decides what is wrong. If these strings drift, an agent can
quietly collapse the lanes back into one session and still call it a review.
"""

from pathlib import Path

ROOT = Path(__file__).parents[1]
SKILLS = ROOT / "skills"


def read(name: str) -> str:
    return (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")


def test_review_pr_documents_the_packet_and_the_lanes():
    text = read("review-pr")

    assert "## Independence: the elephant and the goldfish" in text
    assert "### The review packet" in text
    assert "### Lanes" in text
    for lane in ("**Correctness**", "**Tests and contracts**", "**Blast radius**"):
        assert lane in text
    assert "single-context" in text
    assert "**not run**" in text


def test_review_pr_keeps_the_orchestrator_out_of_the_review():
    text = read("review-pr")

    assert "It does not decide what is wrong with the diff." in text
    assert "Do not put a suspected bug" in text
    assert "**The verifier** is another fresh subagent." in text


def test_review_pr_resumed_round_writes_two_packets():
    text = read("review-pr")
    flat = " ".join(text.split())

    assert "the **carried packet**" in text
    assert "the **delta packet**" in text
    assert "never the carried findings" in flat


def test_review_multi_keeps_reviewers_blind_and_counts_failures():
    text = read("review-multi")

    assert "fresh contexts" in text
    assert "Neither reviewer sees the other's work." in text
    assert "did **not** review" in text
    assert "single-context" in text
    assert "fresh verifier subagent" in text


def test_review_lanes_do_not_require_the_orchestrator_model():
    text = " ".join(read("review-pr").split())

    assert "Model choice is not part of the split" in text
    assert "does not have to match this session's model" in text
    multi = " ".join(read("review-multi").split())
    assert "do not need this session's model" in multi


def test_review_delta_escalates_a_merge_gate():
    text = read("review-delta")

    assert "not a merge gate" in text
    assert "review-multi" in text
    assert "single-context" in text


def test_review_changes_does_not_chain_reviews_in_one_context():
    text = read("review-changes")

    assert "not a merge gate" in text
    assert "Do not chain a second review in this context" in text
    assert "single-context" in text
