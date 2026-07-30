"""
Deterministic, no-LLM composer. Used when ANTHROPIC_API_KEY isn't set, the
API call fails/times out, or LLM output fails hard validation twice. This
guarantees the bot NEVER returns an empty/malformed body — protecting the
operational-penalty side of scoring — even in a fully offline environment.

Not as sharp as the LLM path, but still respects: real numbers only,
category voice, single CTA in the last sentence, hi/en mix when applicable.
"""
from __future__ import annotations


def _hi_en(merchant: dict) -> bool:
    langs = merchant.get("identity", {}).get("languages", [])
    return "hi" in langs


def _name(merchant: dict) -> str:
    return merchant.get("identity", {}).get("name", "there")


def compose_fallback(category: dict, merchant: dict, trigger: dict, customer: dict | None,
                      anchor: dict) -> dict:
    kind = trigger.get("kind", "")
    name = _name(merchant)
    hi = _hi_en(merchant)
    fact = anchor.get("fact") or {}

    if customer:
        cust_name = customer.get("identity", {}).get("name", "there")
        offers = [o for o in merchant.get("offers", []) if o.get("status") == "active"]
        offer_line = offers[0]["title"] if offers else "our latest offer"
        if hi:
            body = (f"Hi {cust_name}, {name} here. Aapke liye {offer_line} available hai — "
                    f"reply karke batayein kab convenient rahega aapke liye?")
        else:
            body = (f"Hi {cust_name}, this is {name}. We have {offer_line} available for you — "
                    f"reply and let us know a time that works?")
        return {"body": body, "cta": "open_ended", "send_as": "merchant_on_behalf",
                "rationale": f"Fallback template composition for trigger kind={kind} (no LLM available)."}

    perf = merchant.get("performance", {}) or {}
    peer = category.get("peer_stats", {}) or {}
    views = perf.get("views")
    ctr = perf.get("ctr")
    peer_ctr = peer.get("avg_ctr")

    if kind in ("research_digest", "category_research_digest_release"):
        item = (fact.get("digest_item") or {}) if isinstance(fact, dict) else {}
        title = item.get("title") or "a relevant update"
        source = item.get("source", "")
        if hi:
            body = (f"{name}, ek update hai jo aapke liye relevant ho sakta hai: {title}"
                    f"{f' ({source})' if source else ''}. Chahenge ki main details bhej doon?")
        else:
            body = (f"{name}, there's an update relevant to you: {title}{f' ({source})' if source else ''}. "
                    f"Want me to send the details?")
        return {"body": body, "cta": "open_ended", "send_as": "vera",
                "rationale": "Fallback: surfaced the real category digest item as the anchor."}

    if kind in ("perf_dip", "perf_spike"):
        direction = "up" if kind == "perf_spike" else "down"
        if hi:
            body = (f"{name}, aapke listing ke views is mahine {views} hain (CTR {ctr}), peer average "
                    f"{peer_ctr} hai. Kya main is par ek quick suggestion bhej doon?")
        else:
            body = (f"{name}, your listing had {views} views this month (CTR {ctr}) vs peer avg {peer_ctr}. "
                    f"Want a quick suggestion to act on this?")
        return {"body": body, "cta": "open_ended", "send_as": "vera",
                "rationale": f"Fallback: anchored on real performance numbers ({direction} movement)."}

    if kind == "renewal_due":
        sub = merchant.get("subscription", {}) or {}
        days = sub.get("days_remaining")
        if hi:
            body = f"{name}, aapki subscription mein {days} din baaki hain. Renew karne mein madad karoon?"
        else:
            body = f"{name}, your subscription has {days} days remaining. Want help renewing now?"
        return {"body": body, "cta": "binary", "send_as": "vera",
                "rationale": "Fallback: anchored on real subscription days_remaining."}

    active_offers = [o for o in merchant.get("offers", []) if o.get("status") == "active"]
    offer_line = active_offers[0]["title"] if active_offers else None
    if hi:
        body = (f"{name}, kuch naya try karna chahenge is hafte? "
                + (f"Aapka {offer_line} offer abhi active hai — isko highlight karoon?"
                   if offer_line else "Bata dijiye aapka sabse zyada pucha jaane wala service kaunsa hai is hafte?"))
    else:
        body = (f"{name}, quick one for this week — "
                + (f"your {offer_line} offer is live, want me to give it a push?"
                   if offer_line else "what's the most-asked-about service at your place this week?"))
    return {"body": body, "cta": "open_ended", "send_as": "vera",
            "rationale": f"Fallback generic template for trigger kind={kind}; anchored on real offer/ask-lever."}
