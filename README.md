# Vera Challenge Submission

## Approach

Deterministic, rule-based composer — **no LLM call** in this version. A
dispatch table maps each of the 24 trigger kinds found in the dataset to a
dedicated handler function; an unmapped or data-thin kind falls back to a
generic, honest check-in rather than fabricating specificity.

Why rule-based instead of LLM-backed: this submission prioritizes getting
the *contract* (endpoints, idempotency, suppression, anti-repetition, reply
state machine) fully correct and testable first. An LLM composer is a
drop-in replacement for the dispatch table in `compose.py` — the interface
(`compose(category, merchant, trigger, customer) -> dict`) doesn't change.

## Key finding that shaped the design

75 of the 100 triggers in the generated dataset carry a **placeholder-only**
payload (`{"placeholder": true, "metric_or_topic": "..."}`) — only the
original 25 seed triggers have real facts in `trigger.payload`. 13 of the 30
canonical test pairs land on a placeholder trigger.

This means the composer cannot treat `trigger.payload` as the sole source of
specificity. Every handler is written to pull real facts from
`merchant`/`customer`/`category` context first, and only uses
`trigger.payload` fields when they're actually populated. When *no* real
data exists anywhere in context for a kind (e.g. `appointment_tomorrow` —
`CustomerContext` has no booking/time field at all), the bot sends an
honest, generic message rather than inventing a time or service — see
`_h_appointment_tomorrow` in `compose.py`.

## What's implemented

- All 5 required endpoints + optional `POST /v1/teardown`
- `/v1/context`: idempotent by `(scope, context_id, version)`, atomic
  version replacement, 409 on stale version
- `/v1/tick`: dedup via `suppression_key` (won't re-message the same
  underlying event even with different wording), 20-actions/tick cap enforced
- `/v1/reply`: small deterministic state machine —
  - auto-reply detection (2-signal rule: try once, exit on repeat — matches
    the brief's own Pattern B example exactly)
  - explicit positive intent → routes straight to action, never re-asks a
    qualifying question (this is Anti-pattern D in the brief)
  - explicit decline → graceful `end`
  - deferral → `wait` (1800s)
  - hostile message → one polite redirect, `end` if it recurs
  - anti-repetition safety net on the `send` path
- Validated against **all 30 canonical test pairs** (100% coverage, 0
  errors, deterministic — see `run_test_pairs.py`)
- Full HTTP-level test suite exercising warmup → idempotency → tick →
  dedup → reply → teardown against the actual FastAPI app (`test_bot_e2e.py`)

## Known limitations / open items

- **No hi-en code-mix on merchant-facing messages.** Customer-facing
  messages (`recall_due`) do light hi-en mixing when the category voice and
  customer's `language_pref` both support it; merchant-facing messages stay
  English throughout. This matches the brief's own gold examples (Appendix
  A is English, Appendix B is hi-en) but doesn't implement the general case.
- **No LLM.** Reply handling is keyword/regex-based, not semantic — it will
  miss paraphrased intent or nuanced hostility that an LLM would catch.
- **`template_params` is left empty** in tick actions; `body` already
  carries the composed specifics directly rather than templated slots.

## Files

- `compose.py` — the composer (24 kind handlers + fallback)
- `store.py` — in-memory context store (idempotency, suppression, conversation log)
- `reply.py` — `/v1/reply` state machine
- `bot.py` — FastAPI app wiring it all together
- `run_test_pairs.py` — validates `compose()` against all 30 canonical pairs
- `test_bot_e2e.py` — full HTTP-level test of the running app
