"""
Thin wrapper around the Google Gemini API (generateContent).
 
Design goals:
- temperature=0 for determinism (required by the challenge spec).
- Hard timeout well under the 30s per-call budget the judge enforces.
- Never raises out of `complete_json()` — callers get None on any failure and
  fall back to the deterministic template composer, so the bot never
  times out or 500s just because an LLM call hiccuped.
- Self-throttles + retries on 429 so free-tier rate limits don't silently
  degrade every message to the generic fallback template (this was the
  single biggest score-killer in local judge_simulator runs: most calls
  were succeeding logically but getting rate-limited into a template).
"""
from __future__ import annotations
 
import json
import os
import re
import time
import threading
import urllib.request
import urllib.error
 
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL = os.environ.get("VERA_MODEL", "gemini-flash-lite-latest")
API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
TIMEOUT_SECONDS = float(os.environ.get("VERA_LLM_TIMEOUT", "5"))
 
# Free-tier Gemini models cap out around 10-15 requests/minute. Space our own
# calls out so we don't self-inflict 429s during a tick batch (which can fire
# several compose() calls back-to-back). Override via env if you're on a
# paid/higher-quota key.
MIN_CALL_INTERVAL_SECONDS = float(os.environ.get("VERA_LLM_MIN_INTERVAL", "0.3")) 
MAX_RETRIES = int(os.environ.get("VERA_LLM_MAX_RETRIES", "1"))
# Hard ceiling on total wall-clock time complete_json() may spend (throttle +
# request + retries combined), kept comfortably under the judge's 30s
# per-call budget so a string of 429s can never cause an operational timeout.
TOTAL_BUDGET_SECONDS = float(os.environ.get("VERA_LLM_TOTAL_BUDGET", "6"))
 
_last_call_lock = threading.Lock()
_last_call_ts = 0.0
 
 
def _throttle():
    """Block just long enough to keep us under MIN_CALL_INTERVAL_SECONDS between calls."""
    global _last_call_ts
    with _last_call_lock:
        now = time.monotonic()
        wait = MIN_CALL_INTERVAL_SECONDS - (now - _last_call_ts)
        if wait > 0:
            time.sleep(wait)
        _last_call_ts = time.monotonic()
 
 
def _extract_json(text: str) -> dict | None:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            return None
    return None
 
 
def _post_once(system: str, user: str, max_tokens: int):
    """One HTTP attempt. Returns (data_dict, retry_after_seconds_or_None, hard_fail_bool)."""
    body = json.dumps({
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": max_tokens,
            "responseMimeType": "application/json",
        },
    }).encode("utf-8")
 
    req = urllib.request.Request(
        API_URL, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": GEMINI_API_KEY,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode("utf-8")), None, False
    except urllib.error.HTTPError as e:
        retry_after = e.headers.get("Retry-After") if e.headers else None
        try:
            body_text = e.read().decode("utf-8")
        except Exception:
            body_text = ""
        if e.code == 429:
            print(f"[llm_client] 429 rate limited (retry_after={retry_after}): {body_text[:200]}")
            try:
                return None, float(retry_after) if retry_after else None, False
            except ValueError:
                return None, None, False
        print(f"[llm_client] HTTPError {e.code}: {body_text[:300]}")
        return None, None, True  # non-429 HTTP error: don't bother retrying
    except Exception as e:
        print(f"[llm_client] request failed: {e}")
        return None, None, True
 
 
def complete_json(system: str, user: str, max_tokens: int = 700) -> dict | None:
    """Call Gemini with temperature=0 and parse a single JSON object from the reply.
    Self-throttles between calls and retries with backoff on 429 (up to MAX_RETRIES),
    but never spends more than TOTAL_BUDGET_SECONDS total — if the budget runs out
    mid-retry we bail and let the caller fall back to the deterministic template
    rather than risk an operational timeout on the judge's side."""
    if not GEMINI_API_KEY:
        return None
 
    start = time.monotonic()
    backoff = 1.5
    for attempt in range(MAX_RETRIES + 1):
        elapsed = time.monotonic() - start
        remaining = TOTAL_BUDGET_SECONDS - elapsed
        if remaining < 2:
            print("[llm_client] out of time budget, giving up (fallback template will be used)")
            return None
 
        _throttle()
        data, retry_after, hard_fail = _post_once(system, user, max_tokens)
 
        if data is not None:
            try:
                candidates = data.get("candidates", [])
                if not candidates:
                    print(f"[llm_client] no candidates in response: {json.dumps(data)[:300]}")
                    return None
                parts = candidates[0].get("content", {}).get("parts", [])
                raw_text = "\n".join(p.get("text", "") for p in parts if "text" in p)
            except Exception as e:
                print(f"[llm_client] failed to parse response shape: {e}")
                return None
            return _extract_json(raw_text)
 
        if hard_fail or attempt == MAX_RETRIES:
            return None
 
        # 429 — back off and retry (respect Retry-After if the API gave one),
        # capped so we never sleep past the remaining budget.
        elapsed = time.monotonic() - start
        remaining = TOTAL_BUDGET_SECONDS - elapsed
        sleep_for = min(retry_after if retry_after else backoff, max(remaining - 1, 0))
        if sleep_for <= 0:
            return None
        print(f"[llm_client] retrying in {sleep_for:.1f}s (attempt {attempt + 1}/{MAX_RETRIES})")
        time.sleep(sleep_for)
        backoff *= 2
 
    return None
