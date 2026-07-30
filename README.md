# Vera++ — magicpin AI Challenge submission

## Approach

A single LLM composer (Claude, `temperature=0`) sits on top of a small pipeline:

1. **Anchor resolution** (`enrichment.py`) — resolves one concrete, *non-fabricated* fact to
   anchor the message on. About 40% of the provided dataset's generated (non-seed) triggers
   only carry a `{"placeholder": true}` payload with no real number attached. Rather than let
   the LLM invent one (a scored anti-pattern), this layer derives a real fact from data that
   genuinely exists elsewhere — merchant `performance`/`signals`/`customer_aggregate`, or
   category `peer_stats`/`digest` — keyed off the trigger's `kind`. For `competitor_opened`
   specifically (no competitor data exists anywhere in the dataset), it explicitly instructs
   the model *not* to name a competitor and to anchor on the merchant's real standing vs. the
   real peer benchmark instead.
2. **Composition** (`prompts.py` + `llm_client.py`) — the system prompt encodes the full
   4-context contract, the hard constraints (§5 of the brief), the compulsion levers (§10),
   and the anti-patterns (§11) as explicit rules, plus two few-shot examples lifted from the
   brief's own "good" bar (Appendix A/B) and one anti-pattern (Pattern D). Output is
   constrained to a single JSON object.
3. **Validation + one repair pass** (`validators.py`) — cheap regex checks for the failure
   modes that are automatic penalties: empty body, verbatim repeat of a prior message, taboo
   vocabulary from the category's `vocab_taboo`, multiple CTAs in one message. On a hard
   failure, we re-prompt exactly once with the specific violation named; if that still fails,
   we drop to the deterministic fallback rather than risk sending something worse.
4. **Deterministic fallback** (`fallback_templates.py`) — if there's no `ANTHROPIC_API_KEY`,
   the API call errors/times out, or validation fails twice, a template composer built from
   the same anchor-resolution output guarantees a valid, non-empty, non-generic message. This
   is what protects the "operational penalties" side of scoring (§10 of the testing brief) —
   the bot cannot time out or return malformed JSON just because an LLM call had a bad day.

**Multi-turn replies** (`/v1/reply`, `conversation_handlers.py`) run a second, smaller pipeline:
regex-based signal detectors (`signals.py`) flag auto-reply, explicit intent-agreement, decline,
"give me time", hostile, and off-topic patterns in the incoming message *before* the LLM sees it.
A second LLM call (or the deterministic fallback) then decides `send` / `wait` / `end`:
- Auto-reply is detected either by canned-phrase matching or verbatim repetition. First
  detection → one short, low-effort attempt to reach a human. Second detection (or explicit
  "I'm an automated assistant") → graceful `end`, no more turns wasted (Pattern B).
- Explicit intent agreement ("yes", "let's do it", "haan kar do", "mujhe join karna hai")
  immediately switches to action mode instead of re-asking a qualifying question (avoiding
  Pattern D, the brief's named anti-pattern).
- Hostile messages get a calm, non-escalating redirect; off-topic questions get a brief,
  polite "that's outside what I handle" and a return to the one open CTA.

**State/dedup**: the server tracks one active conversation per `(merchant_id, customer_id)` pair
and a set of already-used `suppression_key`s, so a trigger already acted on is never re-fired,
and a merchant already mid-conversation doesn't get a second parallel nudge in the same tick.

## Tradeoffs

- I prioritized **never sending something worse than silence** over chasing every possible
  point — the fallback path is deliberately conservative rather than clever.
- The anchor-resolution layer is rule-based per trigger `kind`, not learned/retrieved. With
  more time I'd add embedding retrieval over `category.digest` / `patient_content_library` so
  the anchor pick adapts to wording it hasn't seen a hardcoded branch for.
- Multi-turn cadence planning (open challenge #3 — optimal sequence *within* a 24h window) is
  handled only implicitly via the one-active-conversation-per-merchant rule; there's no explicit
  planner for e.g. "wait 2 hours then follow up if silent."
- Validation is regex-level, not semantic — it catches the loud failure modes (empty body,
  taboo word, repeat, multi-CTA) but doesn't verify e.g. "is this number actually present in the
  context" beyond what the anchor-resolution step already guarantees by construction.

## What additional context would have helped most

- Real per-merchant competitor/local-search data for `competitor_opened` — currently the safest
  correct move is to *not* invent a rival, but a real one would unlock a much stronger message.
- A signal for "which language did the merchant's last few real replies actually use" would beat
  the static `identity.languages` field for the language-matching rule, especially for
  code-switching merchants.

## Running locally

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...        # optional — falls back to templates without it
export VERA_MODEL=claude-sonnet-4-5        # optional, this is the default
uvicorn bot:app --host 0.0.0.0 --port 8080
```

Self-test against the provided harness:
```bash
export BOT_URL=http://localhost:8080
python judge_simulator.py
```

Regenerate `submission.jsonl` for the 30 canonical test pairs:
```bash
python gen_submission.py --dataset dataset --out submission.jsonl
```

## Files

| File | Purpose |
|---|---|
| `bot.py` | FastAPI server — all 5 required endpoints + standalone `compose()` |
| `composer.py` | Core compose/compose_reply pipeline (LLM call → validate → fallback) |
| `enrichment.py` | Anchor-fact resolution (handles sparse/placeholder triggers, no fabrication) |
| `prompts.py` | System prompts + few-shot examples for both composition and reply modes |
| `signals.py` | Regex detectors: auto-reply, intent-agreement, decline, wait, hostile, off-topic |
| `validators.py` | Post-LLM checks: empty body, repeat, taboo vocab, multi-CTA |
| `fallback_templates.py` | Deterministic no-LLM composer (used on any failure/no key) |
| `llm_client.py` | Anthropic API wrapper, `temperature=0`, never raises |
| `conversation_handlers.py` | Optional standalone `respond(state, message)` per §7.4 |
| `gen_submission.py` | Produces `submission.jsonl` from `dataset/test_pairs.json` |
| `submission.jsonl` | 30 composed messages for the canonical test pairs |
| `dataset/` | The expanded dataset (via the provided `generate_dataset.py`), for local testing |
