"""
Vera++ — magicpin AI Challenge submission.

Implements:
  - the standalone `compose()` function (challenge-brief.md §7.1)
  - the 5-endpoint HTTP contract (challenge-testing-brief.md §2)

Run locally:
    uvicorn bot:app --host 0.0.0.0 --port 8080

Env vars:
    GEMINI_API_KEY      — required for LLM-quality composition (falls back to
                           deterministic templates if unset, so the bot never
                           crashes/times out without it).
    VERA_MODEL          — defaults to "gemini-flash-lite-latest".
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from fastapi import FastAPI
from pydantic import BaseModel

import composer
import signals

app = FastAPI(title="Vera++ (magicpin AI Challenge)")
START_TIME = time.time()

# ---------------------------------------------------------------------------
# In-memory state (fine per the spec: "storing in memory is fine; just don't
# restart between calls"). Keyed by (scope, context_id) for idempotent context
# pushes, and by conversation_id for in-flight conversations.
# ---------------------------------------------------------------------------
contexts: dict[tuple[str, str], dict] = {}          # (scope, id) -> {"version": int, "payload": dict}
conversations: dict[str, dict] = {}                  # conversation_id -> conv record
active_conversation_by_key: dict[tuple[str, str | None], str] = {}  # (merchant_id, customer_id) -> conv_id
sent_suppression_keys: set[str] = set()
merchant_auto_reply_count: dict[str, int] = {}


def _get(scope: str, ctx_id: str | None) -> dict | None:
    if not ctx_id:
        return None
    entry = contexts.get((scope, ctx_id))
    return entry["payload"] if entry else None


def _category_for_merchant(merchant: dict) -> dict | None:
    if not merchant:
        return None

    category_id = (
        merchant.get("category_slug")
        or merchant.get("category_id")
        or merchant.get("category")
        or merchant.get("category_name")
    )

    return _get("category", category_id) if category_id else None


# ---------------------------------------------------------------------------
# §7.1 — the standalone compose() contract required by the challenge brief.
# Pure function, no server state, so it's independently testable / usable by
# gen_submission.py to produce submission.jsonl.
# ---------------------------------------------------------------------------
def compose(category: dict, merchant: dict, trigger: dict, customer: dict | None) -> dict:
    return composer.compose(category, merchant, trigger, customer)


# ---------------------------------------------------------------------------
# 2.4 GET /v1/healthz
# ---------------------------------------------------------------------------
@app.get("/v1/healthz")
async def healthz():
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _cid) in contexts.keys():
        counts[scope] = counts.get(scope, 0) + 1
    return {"status": "ok", "uptime_seconds": int(time.time() - START_TIME), "contexts_loaded": counts}


# ---------------------------------------------------------------------------
# 2.5 GET /v1/metadata
# ---------------------------------------------------------------------------
@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": "Vera++",
        "team_members": ["Srv"],
        "model": composer.llm_client.MODEL,
        "approach": (
            "Single LLM composer (temperature=0) over the 4-context framework, with a "
            "deterministic anchor-resolution layer that derives a real, non-fabricated "
            "verifiable fact even when trigger payloads are sparse/placeholder, a "
            "post-generation validator (single-CTA / taboo-vocab / anti-repetition) with "
            "one repair re-prompt, and a fully deterministic template fallback so the bot "
            "never times out or returns malformed output even with no LLM access. "
            "Multi-turn replies use regex-based signal detectors (auto-reply, intent "
            "agreement, decline, wait-request, hostile, off-topic) fed into a second LLM "
            "call that decides send/wait/end."
        ),
        "contact_email": "team@example.com",
        "version": "1.0.0",
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# 2.1 POST /v1/context
# ---------------------------------------------------------------------------
class CtxBody(BaseModel):
    scope: Literal["category", "merchant", "customer", "trigger"]
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: Optional[str] = None


@app.post("/v1/context")
async def push_context(body: CtxBody):
    key = (body.scope, body.context_id)
    cur = contexts.get(key)
    if cur and cur["version"] >= body.version:
        return {"accepted": False, "reason": "stale_version", "current_version": cur["version"]}
    contexts[key] = {"version": body.version, "payload": body.payload}
    return {
        "accepted": True,
        "ack_id": f"ack_{body.context_id}_v{body.version}",
        "stored_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# 2.2 POST /v1/tick
# ---------------------------------------------------------------------------
class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = []


MAX_ACTIONS_PER_TICK = 20


@app.post("/v1/tick")
async def tick(body: TickBody):
    actions: list[dict] = []

    # Highest urgency first so we spend the per-tick action budget wisely.
    candidates = []
    for trg_id in body.available_triggers:
        trg = _get("trigger", trg_id)
        if not trg:
            continue
        candidates.append(trg)
    candidates.sort(key=lambda t: -(t.get("urgency") or 1))

    for trg in candidates:
        if len(actions) >= MAX_ACTIONS_PER_TICK:
            break

        suppression_key = trg.get("suppression_key")
        if suppression_key and suppression_key in sent_suppression_keys:
            continue  # already sent this exact trigger before — avoid duplicate nudges

        merchant_id = trg.get("merchant_id")
        customer_id = trg.get("customer_id")
        merchant = _get("merchant", merchant_id)
        if not merchant:
            continue
        category = _category_for_merchant(merchant)
        if not category:
            continue
        customer = _get("customer", customer_id) if customer_id else None

        conv_key = (merchant_id, customer_id)
        if conv_key in active_conversation_by_key:
            continue  # restraint: don't start a second parallel conversation with the same party

        result = composer.compose(category, merchant, trg, customer)
        if not result.get("body"):
            continue

        conversation_id = f"conv_{merchant_id}_{trg['id']}"
        conversations[conversation_id] = {
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "trigger_id": trg["id"],
            "turns": [{"from": "vera", "body": result["body"], "ts": body.now}],
            "ended": False,
        }
        active_conversation_by_key[conv_key] = conversation_id
        if suppression_key:
            sent_suppression_keys.add(suppression_key)

        actions.append({
            "conversation_id": conversation_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": result["send_as"],
            "trigger_id": trg["id"],
            "template_name": f"vera_{trg.get('kind', 'generic')}_v1",
            "template_params": [merchant.get("identity", {}).get("name", ""), trg.get("kind", "")],
            "body": result["body"],
            "cta": result["cta"],
            "suppression_key": result["suppression_key"],
            "rationale": result["rationale"],
        })

    return {"actions": actions}


# ---------------------------------------------------------------------------
# 2.3 POST /v1/reply
# ---------------------------------------------------------------------------
class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: Optional[str] = None
    turn_number: Optional[int] = None


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    conv = conversations.get(body.conversation_id)
    if conv is None:
        # Defensive: judge referenced a conversation we don't have on record.
        # Reconstruct a minimal one so we can still respond sensibly.
        conv = {
            "merchant_id": body.merchant_id, "customer_id": body.customer_id,
            "trigger_id": None, "turns": [], "ended": False,
        }
        conversations[body.conversation_id] = conv
        if body.merchant_id:
            active_conversation_by_key[(body.merchant_id, body.customer_id)] = body.conversation_id

    merchant = _get("merchant", conv.get("merchant_id") or body.merchant_id)
    category = _category_for_merchant(merchant) if merchant else None
    trigger = _get("trigger", conv.get("trigger_id")) or {}
    customer = _get("customer", conv.get("customer_id")) if conv.get("customer_id") else None

    conv["turns"].append({"from": body.from_role, "body": body.message, "ts": body.received_at})

    # Merchant-level auto-reply tracking, IN ADDITION to composer.compose_reply's
    # own per-conversation logic below. This exists because a canned auto-reply
    # bot on the merchant's side may show up across several different
    # conversation_ids for the same merchant (e.g. multiple triggers firing
    # independently) rather than repeating within a single conversation — so
    # relying on per-conversation history alone can miss the pattern entirely.
    # First occurrence: fall through as normal (composer produces a one-time
    # nudge). Second+ occurrence for this merchant: end immediately, regardless
    # of which conversation_id it arrived on. Any non-auto-reply message from
    # the merchant resets the counter.
    merchant_key = conv.get("merchant_id") or body.merchant_id or "unknown"
    if signals.is_auto_reply_phrase(body.message):
        merchant_auto_reply_count[merchant_key] = merchant_auto_reply_count.get(merchant_key, 0) + 1
        if merchant_auto_reply_count[merchant_key] >= 2:
            conv["ended"] = True
            key = (conv.get("merchant_id"), conv.get("customer_id"))
            if active_conversation_by_key.get(key) == body.conversation_id:
                del active_conversation_by_key[key]
            return {"action": "end",
                    "rationale": "Repeated auto-reply pattern detected for this merchant "
                                  "(seen again across conversations); exiting gracefully."}
    else:
        merchant_auto_reply_count.pop(merchant_key, None)

    if not merchant or not category:
        # Can't compose safely without base context — end politely rather than
        # crashing on a None merchant/category downstream.
        return {"action": "end", "rationale": "Missing merchant/category context for this conversation."}

    prior_turns = conv["turns"][:-1]  # everything except the message we just appended
    result = composer.compose_reply(category, merchant, trigger, customer, prior_turns, body.message)

    action = result.get("action", "end")
    if action == "send":
        conv["turns"].append({"from": "vera", "body": result["body"], "ts": None})
        return {"action": "send", "body": result["body"], "cta": result.get("cta", "open_ended"),
                "rationale": result.get("rationale", "")}
    if action == "wait":
        return {"action": "wait", "wait_seconds": result.get("wait_seconds", 1800),
                "rationale": result.get("rationale", "")}

    # action == "end"
    conv["ended"] = True
    key = (conv.get("merchant_id"), conv.get("customer_id"))
    if active_conversation_by_key.get(key) == body.conversation_id:
        del active_conversation_by_key[key]
    return {"action": "end", "rationale": result.get("rationale", "")}


# ---------------------------------------------------------------------------
# Optional teardown (privacy §11): wipe all in-memory state at end of test.
# ---------------------------------------------------------------------------
@app.post("/v1/teardown")
async def teardown():
    contexts.clear()
    conversations.clear()
    active_conversation_by_key.clear()
    sent_suppression_keys.clear()
    merchant_auto_reply_count.clear()
    return {"ok": True}

