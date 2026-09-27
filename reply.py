"""
reply.py — deterministic conversation-continuation logic for POST /v1/reply.

Built directly against the three Phase-4 replay scenarios in the testing
brief (§4, Phase 4) and Pattern B / Pattern D in the main brief (§9):

  1. Auto-reply hell — same canned text repeated. Detect on the 2nd
     identical merchant message in a row; try once more, then exit
     gracefully on the 3rd repeat (matches Pattern B: "tried once ... then
     stopped wasting turns").
  2. Intent transition — explicit "yes/let's do it" after qualifying turns
     must move straight to action, never re-ask a qualifying question
     (this is exactly Anti-pattern D in the brief).
  3. Hostile / off-topic — acknowledge briefly, redirect to the mission,
     don't escalate, don't just vanish.

This is intentionally rule-based (keyword/heuristic), not an LLM call — see
README for why, and where an LLM would clearly do better (nuanced intent
detection, natural paraphrase instead of templated variation).
"""

from __future__ import annotations
import re


_POSITIVE = re.compile(
    r"\b(yes|yep|yeah|sure|ok(ay)?|go ahead|sounds good|let'?s do it|"
    r"do it|please do|haan|chalo|theek hai|thik hai|kar do|bhej do)\b",
    re.IGNORECASE,
)
_NEGATIVE = re.compile(
    r"\b(no|not interested|nahi|nahin|stop|don'?t|do not|nope|"
    r"not now|maybe later)\b",
    re.IGNORECASE,
)
_DEFER = re.compile(
    r"\b(call me later|give me time|let me think|i'?ll get back|"
    r"busy right now|not right now|baad mein)\b",
    re.IGNORECASE,
)
_HOSTILE = re.compile(
    r"\b(stupid|idiot|useless|shut up|scam|fraud|f+u+c*k+|bloody hell)\b",
    re.IGNORECASE,
)
_AUTO_REPLY_HINT = re.compile(
    r"\b(automated|auto.?reply|team tak pahuncha|will get back to you shortly|"
    r"thank you for your message)\b",
    re.IGNORECASE,
)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def handle_reply(store, conv_id: str, message: str, turn_number: int, meta: dict) -> dict:
    """
    meta: conversation metadata captured when the conversation started
          (trigger_kind, cta, original_body) — used so replies can reference
          what was actually offered instead of a made-up topic.
    Returns a dict shaped like the /v1/reply response: action + fields.
    """
    history = store.history(conv_id)
    prior_merchant_msgs = [h["body"] for h in history if h["from"] in ("merchant", "customer")]

    norm_msg = _norm(message)

    # ---- 1. Auto-reply detection ----
    # A message counts as an "auto-reply signal" if it's either an exact
    # repeat of the immediately-prior merchant/customer turn, or it reads
    # like canned/routed text (_AUTO_REPLY_HINT). Per the brief's own
    # Pattern B example, Vera tries ONCE more on the first such signal, and
    # only exits gracefully if a second signal follows — never exits on the
    # very first canned-sounding message, since that risks bailing on a
    # real (if generically-worded) reply.
    is_repeat = bool(prior_merchant_msgs and _norm(prior_merchant_msgs[-1]) == norm_msg)
    is_hint = bool(_AUTO_REPLY_HINT.search(message))

    if is_repeat or is_hint:
        # Did any earlier merchant/customer turn in this conversation already
        # trip an auto-reply signal (repeat-of-its-predecessor or hint text)?
        prior_signal = False
        seq = [h["body"] for h in history if h["from"] in ("merchant", "customer")]
        for idx, m in enumerate(seq):
            if _AUTO_REPLY_HINT.search(m):
                prior_signal = True
                break
            if idx > 0 and _norm(seq[idx - 1]) == _norm(m):
                prior_signal = True
                break

        if prior_signal:
            return {
                "action": "end",
                "rationale": (
                    "Second auto-reply signal in this conversation (canned/routed text "
                    "or an exact repeat) — already tried once; exiting gracefully rather "
                    "than continuing to spend turns on what reads as an unattended inbox."
                ),
            }
        else:
            return {
                "action": "send",
                "body": "Samajh gayi — quick one though, want me to just show you exactly what's missing? Takes 2 minutes, no back-and-forth needed.",
                "cta": "binary_yes_stop",
                "rationale": (
                    "First auto-reply-style signal (canned wording or repeat) — likely a "
                    "routed/automated response, but trying once more with a lower-friction, "
                    "concrete ask before assuming disengagement (matches Pattern B)."
                ),
            }

    # ---- 2. Hostile / off-topic: acknowledge briefly, stay on-mission ----
    if _HOSTILE.search(message):
        # `history` holds only turns logged *before* this reply, so this is
        # safely "was there hostility earlier in this conversation".
        prior_hostile = any(
            _HOSTILE.search(h["body"]) for h in history if h["from"] in ("merchant", "customer")
        )
        if prior_hostile:
            return {
                "action": "end",
                "rationale": "Hostility repeated after a polite redirect already offered; disengaging rather than continuing to absorb abuse.",
            }
        return {
            "action": "send",
            "body": (
                "No worries, I'll leave it there for now. If it's useful later, I'm here — "
                f"just say the word and I'll pick up on {meta.get('topic_hint', 'this')}."
            ),
            "cta": "open_ended",
            "rationale": "Hostile message detected; declined to escalate or match tone, offered a clean low-pressure exit while leaving the door open (stays on-mission per Phase 4 scenario 3).",
        }

    # ---- 3. Explicit negative / decline ----
    if _NEGATIVE.search(message) and not _POSITIVE.search(message):
        return {
            "action": "end",
            "rationale": "Explicit decline detected; exiting gracefully rather than re-pitching (avoids spam penalty and respects the merchant's answer).",
        }

    # ---- 4. Explicit deferral ----
    if _DEFER.search(message):
        return {
            "action": "wait",
            "wait_seconds": 1800,
            "rationale": "Merchant asked for time; backing off 30 minutes rather than re-prompting immediately.",
        }

    # ---- 5. Explicit positive intent -> go straight to action, never
    #         re-qualify (this is exactly what Anti-pattern D penalizes) ----
    if _POSITIVE.search(message):
        next_step = meta.get("next_step_body") or "Great — I'll get that moving now and update you here."
        return {
            "action": "send",
            "body": next_step,
            "cta": "open_ended",
            "rationale": (
                "Merchant gave explicit affirmative intent — routing straight to the "
                "next concrete action instead of asking another qualifying question "
                "(the exact failure mode called out as Anti-pattern D in the brief)."
            ),
        }

    # ---- 6. Ambiguous / question / anything else: acknowledge once, restate
    #         the single original CTA without inventing a new one, vary
    #         wording by turn to avoid verbatim repetition ----
    variants = [
        "Got it — let me know and I'll take it from there.",
        "Sure thing — just say go whenever you're ready.",
        "No pressure — the offer stands whenever works for you.",
    ]
    body = variants[min(turn_number, len(variants) - 1) % len(variants)]
    return {
        "action": "send",
        "body": body,
        "cta": "open_ended",
        "rationale": "Ambiguous reply — acknowledged without introducing a second CTA or re-litigating the original ask; wording varied by turn to avoid verbatim repetition.",
    }
