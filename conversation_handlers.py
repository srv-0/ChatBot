"""
Optional deliverable (challenge-brief.md §7.4) — a standalone multi-turn
handler independent of the HTTP server, for demonstrating/replaying
conversations offline (e.g. against judge_simulator-style scenarios) without
needing bot.py's in-memory server state.

    from conversation_handlers import ConversationState, respond

    state = ConversationState(category=cat, merchant=merch, trigger=trg, customer=None)
    state.turns.append({"from": "vera", "body": first_message_body})
    result = respond(state, "Aapki jaankari ke liye bahut-bahut shukriya...")
    # -> {"action": "end"|"send"|"wait", ...}
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import composer


@dataclass
class ConversationState:
    category: dict
    merchant: dict
    trigger: dict
    customer: Optional[dict] = None
    turns: list[dict] = field(default_factory=list)  # [{"from": "vera"|"merchant"|"customer", "body": str}]
    ended: bool = False


def respond(state: ConversationState, merchant_message: str) -> dict:
    """
    Given the conversation so far + the merchant's (or customer's) latest
    message, produce the bot's next move and mutate `state` in place.
    Returns a dict: {"action": "send"|"wait"|"end", "body"?, "cta"?,
    "wait_seconds"?, "rationale"}.
    """
    if state.ended:
        return {"action": "end", "rationale": "Conversation already ended; ignoring further input."}

    prior_turns = list(state.turns)
    state.turns.append({"from": "merchant" if not state.customer else "customer", "body": merchant_message})

    result = composer.compose_reply(state.category, state.merchant, state.trigger, state.customer,
                                     prior_turns, merchant_message)

    if result.get("action") == "send":
        state.turns.append({"from": "vera", "body": result["body"]})
    elif result.get("action") == "end":
        state.ended = True

    return result
