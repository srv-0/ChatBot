"""
Cheap, deterministic detectors that run BEFORE the LLM reply-composer sees a
merchant/customer message. These are handed to the LLM as pre-computed
`signals` so it doesn't have to re-derive them, and they also let the bot make
safe, fast decisions (e.g. immediate graceful exit on a 2nd confirmed
auto-reply) without needing an extra model round-trip.
"""
from __future__ import annotations

import re

# Common WhatsApp Business canned auto-reply phrases (Hindi + English), based
# on the patterns called out in challenge-brief.md §9 Pattern B and §12.1.
AUTO_REPLY_PHRASES = [
    r"thank you for (contacting|reaching out|your message)",
    r"we (will|shall) get back to you",
    r"hamari team tak pahuncha",
    r"main aapki (yeh )?(sabhi )?baatein.*team",
    r"i am an automated (assistant|reply)",
    r"main ek automated assistant hoon",
    r"this is an auto[- ]?reply",
    r"currently (unavailable|not available|closed)",
    r"business hours (are|:)",
    r"shukriya.*team",
]

INTENT_AGREEMENT_PHRASES = [
    r"\byes\b", r"\byep\b", r"\bsure\b", r"\bok(ay)?\b", r"\bgo ahead\b",
    r"\blet'?s do it\b", r"\bdo it\b", r"\bsounds good\b", r"\bplease do\b",
    r"\bhaan\b", r"\btheek hai\b", r"\bkar do\b", r"\bkarwa do\b", r"\bchalo\b",
    r"\bmujhe (join|karna|chahiye)\b", r"\bi want to join\b", r"\bi'?m in\b",
    r"\bproceed\b", r"\bconfirm(ed)?\b",
]

DECLINE_PHRASES = [
    r"\bnot interested\b", r"\bstop\b", r"\bno thanks\b", r"\bplease stop\b",
    r"\bunsubscribe\b", r"\bnahi chahiye\b", r"\bnahi karna\b", r"\bleave me alone\b",
]

WAIT_PHRASES = [
    r"\bcall (me )?later\b", r"\bbusy (right now|abhi)\b", r"\babhi busy\b",
    r"\bnot now\b", r"\blater please\b", r"\bfursat mein\b", r"\bbaad mein\b",
]

HOSTILE_PATTERNS = [
    r"\bidiot\b", r"\bstupid\b", r"\bshut up\b", r"\bnonsense\b", r"\bbakwas\b",
    r"\bbewakoof\b", r"\bpagal\b", r"f+u+c+k", r"\bharass", r"\bstop\b.*\bspam\b", r"\bspam\b.*\bstop\b",
]

ON_TOPIC_KEYWORDS = [
    "profile", "google", "review", "offer", "post", "photo", "customer", "gbp",
    "listing", "campaign", "vera", "magicpin", "subscription", "renew", "cleaning",
    "appointment", "booking", "slot", "clinic", "salon", "gym", "restaurant",
    "pharmacy", "dental", "aligner",
]


def _matches_any(text: str, patterns: list[str]) -> bool:
    low = text.lower()
    return any(re.search(p, low) for p in patterns)


def is_auto_reply_phrase(message: str) -> bool:
    """Standalone check: does this single message look like a canned auto-reply,
    independent of any conversation history. Used for cross-conversation tracking
    (e.g. the same merchant auto-responding under different conversation_ids)."""
    return _matches_any(message, AUTO_REPLY_PHRASES)


def analyze_incoming(message: str, conversation_history: list[dict]) -> dict:
    """
    conversation_history: list of {"from": "merchant"|"vera"|"customer", "body": str}
    Returns a signals dict handed both to decision logic and to the LLM reply-composer.
    """
    prior_from_same_role = [
    turn.get("body", turn.get("msg", ""))
    for turn in conversation_history
    if turn.get("from") in ("merchant", "customer")
    ]
    repeat_count = sum(
    1
    for m in prior_from_same_role
    if _matches_any(m, AUTO_REPLY_PHRASES)
)

    is_phrase_auto_reply = _matches_any(message, AUTO_REPLY_PHRASES)
    is_probable_auto_reply = is_phrase_auto_reply or repeat_count >= 1

    is_intent_agreement = _matches_any(message, INTENT_AGREEMENT_PHRASES) and not _matches_any(
        message, DECLINE_PHRASES
    )
    is_decline = _matches_any(message, DECLINE_PHRASES)
    is_wait_request = _matches_any(message, WAIT_PHRASES)
    is_hostile = _matches_any(message, HOSTILE_PATTERNS)
    has_on_topic_keyword = _matches_any(message, ON_TOPIC_KEYWORDS)
    # Off-topic: a real question/statement, not hostile, not declining, and
    # doesn't reference anything in our domain keyword set.
    is_off_topic = (
        len(message.strip()) > 8
        and not has_on_topic_keyword
        and not is_probable_auto_reply
        and not is_intent_agreement
        and not is_decline
        and not is_wait_request
        and "?" in message
    )

    return {
        "is_probable_auto_reply": is_probable_auto_reply,
        "auto_reply_repeat_count": repeat_count,
        "is_intent_agreement": is_intent_agreement,
        "is_decline": is_decline,
        "is_wait_request": is_wait_request,
        "is_hostile": is_hostile,
        "is_off_topic": is_off_topic,
    }
