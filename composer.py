"""
Core composition logic — the brain behind both:
  - the standalone `compose()` function required by challenge-brief.md §7.1
  - the /v1/tick and /v1/reply HTTP handlers in bot.py

Pipeline: enrich trigger -> build prompt -> call LLM (temperature=0) ->
validate -> (re-prompt once on hard failure) -> fallback template if needed.
"""
from __future__ import annotations

import hashlib

import enrichment
import llm_client
import prompts
import validators
import fallback_templates


def _suppression_key(trigger: dict, merchant: dict, customer: dict | None) -> str:
    key = trigger.get("suppression_key")
    if key:
        return key
    parts = [trigger.get("kind", "unknown"), merchant.get("merchant_id", "unknown")]
    if customer:
        parts.append(customer.get("customer_id", ""))
    return ":".join(parts)


def _first_message(merchant: dict) -> bool:
    return not (merchant.get("conversation_history") or [])


def compose(category: dict, merchant: dict, trigger: dict, customer: dict | None = None,
            prior_bodies: list[str] | None = None) -> dict:
    """
    The exact contract required by challenge-brief.md §7.1.
    Returns dict with keys: body, cta, send_as, suppression_key, rationale.
    """
    prior_bodies = prior_bodies or [
        t.get("body", "") for t in (merchant.get("conversation_history") or []) if t.get("from") == "vera"
    ]
    anchor = enrichment.resolve_anchor(category, merchant, trigger, customer)
    is_first = _first_message(merchant)
    send_as = "merchant_on_behalf" if customer else "vera"

    result = _compose_via_llm(category, merchant, trigger, customer, anchor, is_first, prior_bodies)
    if result is None:
        result = fallback_templates.compose_fallback(category, merchant, trigger, customer, anchor)

    result["send_as"] = result.get("send_as") or send_as
    result["cta"] = validators.normalize_cta(result.get("cta"))
    result["suppression_key"] = _suppression_key(trigger, merchant, customer)
    result.setdefault("rationale", "Composed message.")
    result["body"] = (result.get("body") or "").strip()
    return result


def _compose_via_llm(category, merchant, trigger, customer, anchor, is_first, prior_bodies,
                      _is_retry: bool = False) -> dict | None:
    user_prompt = prompts.build_user_prompt(category, merchant, trigger, customer, anchor,
                                             is_first, prior_bodies)
    parsed = llm_client.complete_json(prompts.SYSTEM_PROMPT, user_prompt)
    if not parsed or "body" not in parsed:
        return None

    body = (parsed.get("body") or "").strip()
    taboo = category.get("voice", {}).get("vocab_taboo", [])
    hard_failures = validators.check_hard_failures(body, prior_bodies)
    hard_failures += [f"taboo:{w}" for w in validators.check_taboo_vocab(body, taboo)]

    if hard_failures and not _is_retry:
        # One repair attempt: tell the model exactly what was wrong.
        repair_note = (f"\n\nYour previous draft failed these checks: {hard_failures}. "
                       f"Previous draft was: {body!r}. Fix these specific issues and "
                       f"respond again with ONLY the corrected JSON object.")
        parsed2 = llm_client.complete_json(prompts.SYSTEM_PROMPT, user_prompt + repair_note)
        if parsed2 and parsed2.get("body", "").strip():
            body2 = parsed2["body"].strip()
            if not validators.check_hard_failures(body2, prior_bodies):
                return {"body": body2, "cta": parsed2.get("cta"), "rationale": parsed2.get("rationale", "")}
        return None  # fall through to deterministic fallback template

    if hard_failures and _is_retry:
        return None

    return {"body": body, "cta": parsed.get("cta"), "rationale": parsed.get("rationale", "")}


def compose_reply(category: dict, merchant: dict, trigger: dict, customer: dict | None,
                   conversation: list[dict], latest_message: str) -> dict:
    
    """
    Multi-turn reply composer. conversation: list of {"from": ..., "body": ...} dicts,
    oldest first, NOT including latest_message.
    Returns dict with keys: action ("send"|"wait"|"end"), body?, cta?, wait_seconds?, rationale.
    """
    import signals as signals_mod

    sig = signals_mod.analyze_incoming(latest_message, conversation)

    # Fast, deterministic safety net for the clearest cases (also protects us
    # if the LLM call fails entirely) — the LLM path below still gets a shot
    # at nuance first for anything not clear-cut.
    if sig["is_probable_auto_reply"] and sig["auto_reply_repeat_count"] >= 1:
        return {"action": "end",
                "rationale": "Confirmed repeat/self-declared auto-reply; exiting gracefully to avoid "
                              "wasting turns on a non-human responder."}

    prior_bodies = [t.get("body", "") for t in conversation if t.get("from") == "vera"]

    parsed = _compose_reply_via_llm(category, merchant, trigger, customer, conversation,
                                     latest_message, sig, prior_bodies)
    if parsed is not None:
        return parsed

    # Deterministic fallback if LLM unavailable/failed.
    if sig["is_decline"]:
        return {"action": "end", "rationale": "Fallback: merchant/customer declined; graceful exit."}
    if sig["is_wait_request"]:
        return {"action": "wait", "wait_seconds": 3600, "rationale": "Fallback: asked for time; backing off."}
    if sig["is_probable_auto_reply"]:
        return {"action": "send", "cta": "binary",
                "body": "Samajh gayi. Kya aap khud 2 minute mein dekh sakte hain — chalega?",
                "rationale": "Fallback: first suspected auto-reply, one low-effort human check."}
    if sig["is_intent_agreement"]:
        return {"action": "send", "cta": "open_ended",
                "body": "Great — starting this now, will confirm here once it's done.",
                "rationale": "Fallback: detected explicit intent agreement; moved to action mode."}
    anchor = enrichment.resolve_anchor(category, merchant, trigger, customer)
    fb = fallback_templates.compose_fallback(category, merchant, trigger, customer, anchor)
    fb_body = fb["body"]
    if fb_body in prior_bodies:
        fb_body += " "  # trivial de-dup guard
    return {"action": "send", "cta": fb.get("cta", "open_ended"), "body": fb_body,
            "rationale": "Fallback: generic continuation, LLM unavailable."}


def _compose_reply_via_llm(category, merchant, trigger, customer, conversation, latest_message,
                            sig, prior_bodies) -> dict | None:
    user_prompt = prompts.build_reply_user_prompt(category, merchant, trigger, customer,
                                                   conversation, latest_message, sig)
    parsed = llm_client.complete_json(prompts.REPLY_SYSTEM_PROMPT, user_prompt)
    if not parsed or "action" not in parsed:
        return None
    action = parsed.get("action")
    if action not in ("send", "wait", "end"):
        return None
    if action == "send":
        body = (parsed.get("body") or "").strip()
        if not body:
            return None
        taboo = category.get("voice", {}).get("vocab_taboo", [])
        hard_failures = validators.check_hard_failures(body, prior_bodies)
        hard_failures += [f"taboo:{w}" for w in validators.check_taboo_vocab(body, taboo)]
        if hard_failures:
            return None  # let outer function fall back to deterministic path
        return {"action": "send", "body": body, "cta": validators.normalize_cta(parsed.get("cta")),
                "rationale": parsed.get("rationale", "")}
    if action == "wait":
        return {"action": "wait", "wait_seconds": int(parsed.get("wait_seconds") or 1800),
                "rationale": parsed.get("rationale", "")}
    return {"action": "end", "rationale": parsed.get("rationale", "")}
