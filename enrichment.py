"""
Resolves a concrete "anchor fact" for a (category, merchant, trigger, customer?) tuple.

Why this exists: in the provided dataset, roughly half of the generated (non-seed)
triggers only carry a placeholder payload — {"placeholder": true, "metric_or_topic": kind}
— with no real number/date/headline attached. The brief is explicit that the bot must
NEVER fabricate a fact that isn't present in the contexts. So instead of inventing one,
this module derives a real, verifiable fact from data that genuinely exists elsewhere
in the merchant/category contexts, keyed off what the trigger *kind* is about.

The output is handed to the LLM as a trusted "resolved_anchor" block so it has something
concrete to anchor on even when trigger.payload is sparse.
"""
from __future__ import annotations


def _peer(category: dict) -> dict:
    return category.get("peer_stats", {}) or {}


def _perf(merchant: dict) -> dict:
    return merchant.get("performance", {}) or {}


def _first_digest(category: dict):
    digest = category.get("digest", []) or []
    return digest[0] if digest else None


def resolve_anchor(category: dict, merchant: dict, trigger: dict, customer: dict | None) -> dict:
    kind = trigger.get("kind", "")
    payload = trigger.get("payload", {}) or {}
    is_placeholder = bool(payload.get("placeholder"))
    name = merchant.get("identity", {}).get("name", "your business")
    perf = _perf(merchant)
    peer = _peer(category)

    anchor = {"kind": kind, "source": "trigger_payload", "fact": None, "note": None}

    if not is_placeholder and payload:
        # Trust the real payload; just surface it.
        anchor["fact"] = payload
        return anchor

    # --- Placeholder fallback: derive something real from merchant/category data ---
    anchor["source"] = "derived_from_merchant_or_category_context"

    if kind in ("perf_dip", "perf_spike"):
        delta = perf.get("delta_7d", {}) or {}
        views_pct = delta.get("views_pct")
        calls_pct = delta.get("calls_pct")
        anchor["fact"] = {
            "views_30d": perf.get("views"), "calls_30d": perf.get("calls"),
            "ctr": perf.get("ctr"), "views_delta_7d_pct": views_pct, "calls_delta_7d_pct": calls_pct,
            "peer_avg_ctr": peer.get("avg_ctr"),
        }
        anchor["note"] = "Use the real 30d numbers and 7d deltas above as the specific hook."

    elif kind == "milestone_reached":
        anchor["fact"] = {
            "total_unique_ytd": merchant.get("customer_aggregate", {}).get("total_unique_ytd"),
            "views_30d": perf.get("views"), "peer_avg_reviews": peer.get("avg_review_count"),
        }
        anchor["note"] = "Frame around whichever real number is milestone-worthy (round number, above-peer)."

    elif kind == "dormant_with_vera":
        last_ts = None
        hist = merchant.get("conversation_history", []) or []
        if hist:
            last_ts = hist[-1].get("ts")
        anchor["fact"] = {"last_conversation_ts": last_ts, "signals": merchant.get("signals", [])}
        anchor["note"] = "Re-engage referencing real signals (e.g. stale_posts, ctr_below_peer) if present."

    elif kind == "review_theme_emerged":
        themes = merchant.get("review_themes", []) or []
        anchor["fact"] = {"review_themes": themes}
        anchor["note"] = "Anchor on a real review_themes entry if present; otherwise fall back to rating/review count."

    elif kind == "competitor_opened":
        # No real competitor data exists in this dataset for placeholder triggers.
        # Do NOT invent a competitor name/distance. Redirect the anchor to the
        # merchant's own standing vs peers instead — still real, still verifiable.
        anchor["fact"] = {
            "own_views": perf.get("views"), "own_ctr": perf.get("ctr"),
            "peer_avg_ctr": peer.get("avg_ctr"), "peer_avg_views": peer.get("avg_views_30d"),
        }
        anchor["note"] = ("No verified competitor data available — do NOT name a competitor or invent "
                           "a distance/opening date. Anchor instead on the merchant's real standing vs "
                           "the real peer benchmark (visibility gap), phrased as 'worth tightening your "
                           "listing' rather than referencing a specific rival.")

    elif kind in ("renewal_due",):
        sub = merchant.get("subscription", {}) or {}
        anchor["fact"] = {"days_remaining": sub.get("days_remaining"), "plan": sub.get("plan"),
                           "status": sub.get("status")}
        anchor["note"] = "Use the real days_remaining/plan/status as the anchor."

    elif kind == "curious_ask_due":
        anchor["fact"] = {"offers": merchant.get("offers", []), "signals": merchant.get("signals", [])}
        anchor["note"] = ("This is a low-stakes 'ask the merchant a question' cadence trigger — use the "
                           "'asking the merchant' compulsion lever, e.g. ask what their most-asked service "
                           "this week is. No need for a big number here; low-friction curiosity is the point.")

    elif kind in ("recall_due", "customer_lapsed_soft", "appointment_tomorrow", "chronic_refill_due",
                  "trial_followup"):
        if customer:
            rel = customer.get("relationship", {}) or {}
            anchor["fact"] = {
                "last_visit": rel.get("last_visit"), "visits_total": rel.get("visits_total"),
                "services_received": rel.get("services_received"),
                "state": customer.get("state"), "active_offers": merchant.get("offers", []),
            }
            anchor["note"] = "Use the customer's real visit history + merchant's real active offer/price."
        else:
            anchor["fact"] = {"customer_aggregate": merchant.get("customer_aggregate", {})}
            anchor["note"] = "No specific customer attached; use the aggregate lapsed/retention numbers instead."

    elif kind in ("festival_upcoming", "weather_heatwave", "local_news_event"):
        anchor["fact"] = {"note": "External event trigger with sparse payload; keep the reference generic "
                                   "to the event type only (no fabricated date/numbers) and pivot quickly "
                                   "to the merchant's real offers/signals."}

    elif kind in ("research_digest", "category_research_digest_release"):
        top = _first_digest(category)
        anchor["fact"] = {"digest_item": top}
        anchor["note"] = "Use the real category.digest[0] item as the anchor (title, source, trial_n if present)."

    elif kind == "category_trend_movement":
        trends = category.get("trend_signals", []) or []
        anchor["fact"] = {"trend": trends[0] if trends else None}

    elif kind == "regulation_change":
        compliance_items = [d for d in category.get("digest", []) or [] if d.get("kind") == "compliance"]
        anchor["fact"] = {"compliance_item": compliance_items[0] if compliance_items else None}

    else:
        # Generic fallback: expose whatever is most concrete on the merchant.
        anchor["fact"] = {
            "performance": perf, "signals": merchant.get("signals", []),
            "peer_stats": peer,
        }
        anchor["note"] = "Unrecognized/sparse trigger kind — anchor on real performance/signals instead."

    anchor["merchant_name"] = name
    return anchor
