"""Stage 13 of the math pipeline: Confidence Engine.

Turns the validator's issue list (stage 7, and again after any LLM repair)
into a single score the pipeline can act on: skip repair for clean formulas,
attempt repair for shaky ones, and flag the rest for human review rather
than silently shipping a guess.
"""

_CRITICAL_MARKERS = ("unclosed brace", "unmatched closing brace", "mismatch")
_ISSUE_PENALTY = 0.15
_CRITICAL_PENALTY = 0.35
REVIEW_THRESHOLD = 0.6


def score(ok, issues):
    if ok and not issues:
        return 1.0, False

    confidence = 1.0
    for issue in issues:
        if any(marker in issue for marker in _CRITICAL_MARKERS):
            confidence -= _CRITICAL_PENALTY
        else:
            confidence -= _ISSUE_PENALTY
    confidence = max(0.0, min(1.0, confidence))

    needs_human_review = (not ok) or confidence < REVIEW_THRESHOLD
    return confidence, needs_human_review
