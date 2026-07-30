"""
Post-LLM validation. Cheap, regex-level checks for the anti-patterns the
brief explicitly says the judge penalizes (§11). We don't re-prompt for
every soft issue (adds latency); we re-prompt once for hard failures
(missing body, verbatim repeat, multi-CTA, taboo word) and otherwise just
log warnings that show up in `rationale` debugging.
"""
from __future__ import annotations

import re

GENERIC_OFFER_PATTERNS = [
    r"\bflat\s+\d+%\s*off\b",
    r"\b\d+%\s*off\b(?!.*₹)",
    r"\bincrease your sales\b",
    r"\bincrease sales\b",
    r"\bgrow your business\b",
    r"\bgrow your sales\b",
    r"\bamazing deal\b",
    r"\bbest in (the )?city\b",
    r"\bdiscount campaign\b",
    r"\bboost your (business|sales|visibility)\b",
]

MULTI_CTA_PATTERNS = [
    r"reply\s+(yes|1)\s+for.*reply\s+(no|2)\s+for",
    r"\byes\b.*\bno\b.*\bmaybe\b",
]

LONG_PREAMBLE_PATTERNS = [
    r"^i hope (you'?re|you are) doing well",
    r"^i hope this message finds you well",
    r"^i am reaching out today to",
]


def check_hard_failures(body: str, prior_bodies: list[str]) -> list[str]:
    """Failures serious enough to warrant a single re-prompt."""
    problems = []
    if not body or not body.strip():
        problems.append("empty_body")
        return problems
    normalized = body.strip()
    if normalized in [p.strip() for p in prior_bodies]:
        problems.append("verbatim_repeat")
    for pat in MULTI_CTA_PATTERNS:
        if re.search(pat, normalized, re.IGNORECASE):
            problems.append("multiple_ctas")
            break
    # Generic hype phrasing with no accompanying number/₹ figure is exactly the
    # published anti-pattern ("Hi Doctor, want to run a discount campaign today
    # to increase sales?") — treat as a hard failure so it gets one repair pass,
    # since a specific number in the body usually signals real anchoring is
    # already present alongside the generic phrase (not a false positive).
    has_digit = bool(re.search(r"\d", normalized))
    if not has_digit:
        for pat in GENERIC_OFFER_PATTERNS:
            if re.search(pat, normalized, re.IGNORECASE):
                problems.append("generic_hype_no_specifics")
                break
    return problems


def check_taboo_vocab(body: str, taboo_words: list[str]) -> list[str]:
    hits = []
    low = body.lower()
    for w in taboo_words or []:
        if w.lower() in low:
            hits.append(w)
    return hits


def check_soft_warnings(body: str) -> list[str]:
    """Non-fatal style warnings, useful for logging/rationale enrichment only."""
    warnings = []
    low = body.lower()
    for pat in GENERIC_OFFER_PATTERNS:
        if re.search(pat, low):
            warnings.append("generic_offer_language")
            break
    for pat in LONG_PREAMBLE_PATTERNS:
        if re.search(pat, low):
            warnings.append("long_preamble")
            break
    if len(body) > 900:
        warnings.append("too_long")
    return warnings


def normalize_cta(cta: str | None) -> str:
    if cta in ("binary", "open_ended", "none"):
        return cta
    return "open_ended"
