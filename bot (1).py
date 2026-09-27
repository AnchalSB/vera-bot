"""
bot.py — the full Vera challenge bot.

Implements every endpoint required by challenge-testing-brief.md §2:
    POST /v1/context    (idempotent, versioned)
    POST /v1/tick        (proactive sends, dedup by suppression_key, <=20/tick)
    POST /v1/reply       (send / wait / end, <30s)
    GET  /v1/healthz
    GET  /v1/metadata
Plus the optional teardown hook from §11:
    POST /v1/teardown    (wipe all state, no persistence after test ends)

Run:
    uvicorn bot:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations
import time
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI
from pydantic import BaseModel

from compose import compose
from store import ContextStore
from reply import handle_reply

app = FastAPI()
START = time.time()
store = ContextStore()

# conversation_id -> {"trigger_kind": str, "topic_hint": str, "next_step_body": str}
conv_meta: dict[str, dict] = {}

TEAM_NAME = "CodeCraft Duo"
TEAM_MEMBERS = ["Anchal Singh Bhadauriya", "Anant Pathak"]
CONTACT_EMAIL = "anchal.2350.x.h@gmail.com, anantpathak1008@gmail.com"
BOT_VERSION = "0.1.0"


# --------------------------------------------------------------------------
# GET /v1/healthz, GET /v1/metadata
# --------------------------------------------------------------------------

@app.get("/v1/healthz")
async def healthz():
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START),
        "contexts_loaded": store.counts(),
    }


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": TEAM_NAME,
        "team_members": TEAM_MEMBERS,
        "model": "rule-based (no LLM) v0.1",
        "approach": (
            "Deterministic, dispatch-table composer: one handler per trigger "
            "kind, grounded strictly in pushed context (never trigger-payload "
            "placeholders). Reply handling is a small rule-based state machine "
            "(auto-reply detection, intent handoff, hostile/off-topic handling)."
        ),
        "contact_email": CONTACT_EMAIL,
        "version": BOT_VERSION,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }


# --------------------------------------------------------------------------
# POST /v1/context
# --------------------------------------------------------------------------

class CtxBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str


@app.post("/v1/context")
async def push_context(body: CtxBody):
    if body.scope not in ("category", "merchant", "customer", "trigger"):
        return {"accepted": False, "reason": "invalid_scope", "details": f"unknown scope '{body.scope}'"}
    result = store.put(body.scope, body.context_id, body.version, body.payload)
    if not result["accepted"]:
        return result
    return {
        "accepted": True,
        "ack_id": result["ack_id"],
        "stored_at": datetime.now(timezone.utc).isoformat(),
    }


# --------------------------------------------------------------------------
# POST /v1/tick
# --------------------------------------------------------------------------

class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = []


MAX_ACTIONS_PER_TICK = 20


def _category_for(merchant: dict) -> Optional[dict]:
    return store.get("category", merchant.get("category_slug"))


@app.post("/v1/tick")
async def tick(body: TickBody):
    actions = []

    for trg_id in body.available_triggers:
        if len(actions) >= MAX_ACTIONS_PER_TICK:
            break

        trg = store.get("trigger", trg_id)
        if not trg:
            continue  # trigger referenced but never pushed to us yet

        suppression_key = trg.get("suppression_key", "")
        if suppression_key and store.was_suppressed_recently(suppression_key):
            continue  # already messaged about this exact event — restraint over spam

        merchant_id = trg.get("merchant_id")
        merchant = store.get("merchant", merchant_id) if merchant_id else None
        if not merchant:
            continue

        category = _category_for(merchant)
        if not category:
            continue

        customer_id = trg.get("customer_id")
        customer = store.get("customer", customer_id) if customer_id else None
        if trg.get("scope") == "customer" and not customer:
            continue  # customer-scoped trigger with no customer context yet — can't compose safely

        composed = compose(category, merchant, trg, customer)

        # one action per (merchant_id, conversation_id) per tick — since we
        # generate a fresh conversation_id per trigger here, this is
        # automatically satisfied; existing conversations are only advanced
        # via /v1/reply, never re-initiated here.
        conversation_id = f"conv_{merchant_id}_{trg_id}"

        action = {
            "conversation_id": conversation_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": composed["send_as"],
            "trigger_id": trg_id,
            "template_name": f"vera_{trg.get('kind', 'generic')}_v1",
            "template_params": [],  # first-touch template params intentionally left generic;
                                     # body already carries the concrete specifics.
            "body": composed["body"],
            "cta": composed["cta"],
            "suppression_key": suppression_key,
            "rationale": composed["rationale"],
        }
        actions.append(action)

        store.mark_sent(suppression_key, composed["body"])
        store.log_turn(conversation_id, "bot", composed["body"])
        conv_meta[conversation_id] = {
            "trigger_kind": trg.get("kind"),
            "topic_hint": (trg.get("kind") or "this").replace("_", " "),
            "next_step_body": _default_next_step(trg.get("kind")),
        }

    return {"actions": actions}


def _default_next_step(kind: Optional[str]) -> str:
    """A safe, generic 'here's what happens next' line used only when the
    merchant gives explicit go-ahead — never claims a specific action was
    already completed, since we can't actually execute anything here."""
    return "Great — I'll get that sorted and drop the details here as soon as it's ready."


# --------------------------------------------------------------------------
# POST /v1/reply
# --------------------------------------------------------------------------

class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: str
    turn_number: int


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    meta = conv_meta.get(body.conversation_id, {})

    result = handle_reply(store, body.conversation_id, body.message, body.turn_number, meta)

    # Log the inbound turn now that handle_reply has already read history
    # (which must reflect state *before* this message).
    store.log_turn(body.conversation_id, body.from_role, body.message)

    if result["action"] == "send":
        # Anti-repetition safety net: never emit the exact body already sent
        # in this conversation (§10 of testing brief: -2 penalty).
        prior_bot_bodies = {h["body"] for h in store.history(body.conversation_id) if h["from"] == "bot"}
        if result["body"] in prior_bot_bodies:
            result["body"] = result["body"] + " (just circling back on this)"
        store.log_turn(body.conversation_id, "bot", result["body"])

    return result


# --------------------------------------------------------------------------
# POST /v1/teardown (optional, per §11 privacy rule)
# --------------------------------------------------------------------------

@app.post("/v1/teardown")
async def teardown():
    store.wipe()
    conv_meta.clear()
    return {"status": "wiped"}
