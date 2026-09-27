"""
compose.py — deterministic, rule-based message composer for Vera.

compose(category, merchant, trigger, customer=None) -> dict with:
    body, cta, send_as, suppression_key, rationale

Design rules (from challenge-brief.md §5 constraints + §11 anti-patterns):
  - Never invent a fact. Every number/date/name in `body` must trace back to
    category/merchant/customer/trigger payload — never to trigger.payload
    when it's a placeholder ({"placeholder": true, ...}), since that carries
    no real data.
  - send_as is derived from trigger.scope, not guessed per-kind:
        scope == "customer" -> "merchant_on_behalf" (talking to a customer)
        scope == "merchant" -> "vera"                (talking to the merchant)
  - Single CTA, lands in the last sentence.
  - No fabricated translations: merchant-facing stays English (matches
    Appendix A gold example); customer-facing uses light hi-en touches only
    where the category's code_mix voice allows AND the customer's own
    language_pref says so (matches Appendix B gold example). This is a
    deliberate scope limit, not an oversight — see README.
"""

from __future__ import annotations
from typing import Optional


class ComposeError(Exception):
    pass


# --------------------------------------------------------------------------
# Small shared helpers
# --------------------------------------------------------------------------

def _digest_item(category: dict, item_id: Optional[str]) -> Optional[dict]:
    if not item_id:
        return None
    return next((d for d in category.get("digest", []) if d.get("id") == item_id), None)


def _is_placeholder(trigger: dict) -> bool:
    return bool(trigger.get("payload", {}).get("placeholder"))


def _merchant_name(merchant: dict) -> str:
    return merchant["identity"]["name"]


def _owner_salutation(merchant: dict, category: dict) -> str:
    """'Dr. Meera' for clinical categories, business name otherwise."""
    tone = category.get("voice", {}).get("tone", "")
    owner = merchant["identity"].get("owner_first_name")
    if "clinical" in tone and owner:
        return f"Dr. {owner}"
    return owner or _merchant_name(merchant)


def _active_offer(merchant: dict) -> Optional[dict]:
    offers = [o for o in merchant.get("offers", []) if o.get("status") == "active"]
    return offers[0] if offers else None


def _pct(x: float) -> str:
    sign = "+" if x >= 0 else ""
    return f"{sign}{round(x * 100)}%"


def _hi_en_ok(category: dict, customer: Optional[dict]) -> bool:
    if category.get("voice", {}).get("code_mix") != "hindi_english_natural":
        return False
    if not customer:
        return False
    return "hi" in (customer.get("identity", {}).get("language_pref") or "")


def _cust_name(customer: dict) -> str:
    return customer["identity"]["name"]


def _suppression(trigger: dict) -> str:
    key = trigger.get("suppression_key")
    if not key:
        raise ComposeError("trigger.suppression_key missing — required for dedup")
    return key


def _send_as(trigger: dict) -> str:
    return "merchant_on_behalf" if trigger.get("scope") == "customer" else "vera"


# --------------------------------------------------------------------------
# Merchant-facing handlers (send_as = "vera")
# --------------------------------------------------------------------------

def _h_research_digest(category, merchant, trigger, customer):
    item = _digest_item(category, trigger.get("payload", {}).get("top_item_id"))
    if not item:
        raise ComposeError("research_digest: digest item not found")

    salutation = _owner_salutation(merchant, category)
    pub = item["source"].split(",")[0].strip()   # "JIDA Oct 2026, p.14" -> "JIDA Oct 2026"
    segment = item.get("patient_segment")
    signals = set(merchant.get("signals", []))
    matched = segment == "high_risk_adults" and "high_risk_adult_cohort" in signals

    relevance = "One item relevant to your high-risk adult patients" if matched \
        else "One item from this week's digest"
    trial = f"{item['trial_n']:,}-patient trial" if item.get("trial_n") else "recent study"

    body = (
        f"{salutation}, {pub} landed. {relevance} — {trial} found: "
        f"{item['title']}. Worth a look. Want me to pull it + draft a patient-ed "
        f"WhatsApp you can share? — {item['source']}"
    )
    rationale = (
        f"External research_digest; anchored on '{item['id']}' from {item['source']}; "
        f"{'matched high_risk_adult_cohort signal' if matched else 'no direct segment match, sent as general FYI'}; "
        f"open-ended CTA — info trigger, not a binary decision."
    )
    return body, "open_ended", rationale


def _h_regulation_change(category, merchant, trigger, customer):
    item = _digest_item(category, trigger.get("payload", {}).get("top_item_id"))
    if not item:
        raise ComposeError("regulation_change: digest item not found")
    deadline = trigger.get("payload", {}).get("deadline_iso", "")[:10]
    salutation = _owner_salutation(merchant, category)
    # item['title'] already states the effective date in most cases; only add
    # a separate deadline clause when the trigger's deadline isn't already
    # visible in the title text, to avoid redundant repetition.
    deadline_clause = "" if (deadline and deadline in item["title"]) else f" Deadline: {deadline}."
    body = (
        f"{salutation}, heads up — {item['title']}.{deadline_clause} "
        f"Want me to send you a one-page compliance checklist so nothing's missed?"
    )
    rationale = (
        f"External regulation_change trigger, urgency={trigger.get('urgency')}; "
        f"anchored on compliance digest item '{item['id']}'; binary-leaning but kept "
        f"open-ended since the ask (checklist) is a value-add, not a commitment."
    )
    return body, "open_ended", rationale


def _h_cde_opportunity(category, merchant, trigger, customer):
    item = _digest_item(category, trigger.get("payload", {}).get("digest_item_id"))
    if not item:
        raise ComposeError("cde_opportunity: digest item not found")
    p = trigger["payload"]
    salutation = _owner_salutation(merchant, category)
    fee = "free for members" if p.get("fee") == "free_for_members" else p.get("fee", "")
    body = (
        f"{salutation}, {item['title']} — {p.get('credits')} CDE credits, {fee}. "
        f"Want me to block the slot on your calendar?"
    )
    rationale = f"External cde_opportunity anchored on '{item['id']}'; binary CTA since it's a bookable slot."
    return body, "binary_yes_stop", rationale


def _h_competitor_opened(category, merchant, trigger, customer):
    p = trigger.get("payload", {})
    salutation = _owner_salutation(merchant, category)
    if _is_placeholder(trigger) or not p.get("competitor_name"):
        body = (
            f"{salutation}, a new competitor opened near you recently — I don't have "
            f"the specifics yet, but want me to pull the comparison once details land?"
        )
        rationale = "competitor_opened fired without real payload (placeholder); avoided fabricating a name/offer, offered a low-friction follow-up instead."
        return body, "open_ended", rationale

    dist = p["distance_km"]
    body = (
        f"{salutation}, {p['competitor_name']} opened {dist}km from you on "
        f"{p['opened_date']} — running \"{p['their_offer']}\". Want me to check how "
        f"your listing compares on price + visibility?"
    )
    rationale = (
        f"External competitor_opened trigger; specifics from payload ({p['competitor_name']}, "
        f"{dist}km, opened {p['opened_date']}); loss-aversion framing, open CTA to a value-add check."
    )
    return body, "open_ended", rationale


def _h_perf_dip(category, merchant, trigger, customer):
    p = trigger.get("payload", {})
    salutation = _owner_salutation(merchant, category)
    metric, delta, window, baseline = p.get("metric"), p.get("delta_pct"), p.get("window"), p.get("vs_baseline")
    if not metric or delta is None:
        raise ComposeError("perf_dip: missing metric/delta_pct in payload")
    body = (
        f"{salutation}, your {metric} are down {_pct(delta)} over the last {window} "
        f"(vs your usual ~{baseline}/day). Want me to check what changed — listing, "
        f"offers, or reviews?"
    )
    rationale = (
        f"Internal perf_dip, urgency={trigger.get('urgency')}; {metric} {_pct(delta)} "
        f"over {window}; loss-aversion + effort-externalization ('I'll check'), open CTA."
    )
    return body, "open_ended", rationale


def _h_perf_spike(category, merchant, trigger, customer):
    p = trigger.get("payload", {})
    salutation = _owner_salutation(merchant, category)
    metric, delta, baseline = p.get("metric"), p.get("delta_pct"), p.get("vs_baseline")
    driver = p.get("likely_driver")
    if not metric or delta is None:
        raise ComposeError("perf_spike: missing metric/delta_pct in payload")
    driver_clause = f" — looks driven by your {driver.replace('_', ' ')}" if driver else ""
    body = (
        f"{salutation}, nice — your {metric} are up {_pct(delta)} this week "
        f"(vs ~{baseline}/day usual){driver_clause}. Want me to double down with a "
        f"follow-up post while it's hot?"
    )
    rationale = f"Internal perf_spike; {metric} {_pct(delta)}; social-proof-adjacent good news + momentum CTA."
    return body, "open_ended", rationale


def _h_milestone_reached(category, merchant, trigger, customer):
    p = trigger.get("payload", {})
    salutation = _owner_salutation(merchant, category)
    metric = (p.get("metric") or "").replace("_", " ")
    value_now, target = p.get("value_now"), p.get("milestone_value")
    if value_now is None or target is None:
        raise ComposeError("milestone_reached: missing value_now/milestone_value")
    gap = target - value_now
    body = (
        f"{salutation}, you're at {value_now} {metric} — {gap} away from {target}. "
        f"Want me to draft a \"help us hit {target}\" post to your recent customers?"
    )
    rationale = f"Internal milestone_reached (imminent={p.get('is_imminent')}); curiosity + social-proof framing around a real, verifiable count."
    return body, "open_ended", rationale


def _h_dormant_with_vera(category, merchant, trigger, customer):
    p = trigger.get("payload", {})
    salutation = _owner_salutation(merchant, category)
    days = p.get("days_since_last_merchant_message")
    topic = (p.get("last_topic") or "").replace("_", " ")
    if days is None:
        body = f"{salutation}, haven't heard from you in a while — anything I can help with on your listing this week?"
        rationale = "dormant_with_vera fired without real payload; generic but honest re-engagement, no invented gap length."
    else:
        topic_clause = f" — last we spoke about {topic}" if topic else ""
        body = f"{salutation}, it's been {days} days since we last chatted{topic_clause}. Anything I can help with this week?"
        rationale = f"Internal dormant_with_vera; {days}d gap, last_topic='{topic}'; reciprocity framing, single open ask."
    return body, "open_ended", rationale


def _h_festival_upcoming(category, merchant, trigger, customer):
    p = trigger.get("payload", {})
    salutation = _owner_salutation(merchant, category)
    festival, days_until = p.get("festival"), p.get("days_until")
    offer = _active_offer(merchant)
    if not festival or days_until is None:
        raise ComposeError("festival_upcoming: missing festival/days_until")
    offer_clause = f" Your \"{offer['title']}\" offer is live — want me to draft a {festival} post around it?" \
        if offer else f" Want me to draft a {festival} post for your page?"
    body = f"{salutation}, {festival} is {days_until} days out.{offer_clause}"
    rationale = f"External festival_upcoming; {days_until}d to {festival}; {'anchored on real active offer' if offer else 'no active offer to anchor on, kept generic'}."
    return body, "open_ended", rationale


def _h_category_seasonal(category, merchant, trigger, customer):
    p = trigger.get("payload", {})
    salutation = _owner_salutation(merchant, category)
    trends = p.get("trends", [])
    if not trends:
        raise ComposeError("category_seasonal: missing trends")
    top = trends[0].replace("_", " ")
    season = p.get("season", "this season").replace("_", " ")
    body = (
        f"{salutation}, {season} shift incoming — {top} "
        f"leading the category. Want me to flag which shelf items to push this week?"
    )
    rationale = f"External category_seasonal; top trend='{top}' of {len(trends)} signals; effort-externalization CTA."
    return body, "open_ended", rationale


def _h_gbp_unverified(category, merchant, trigger, customer):
    p = trigger.get("payload", {})
    salutation = _owner_salutation(merchant, category)
    uplift = p.get("estimated_uplift_pct")
    uplift_clause = f" — verified listings see ~{round(uplift*100)}% more visibility" if uplift else ""
    path_labels = {"postcard_or_phone_call": "postcard or phone call"}
    path = path_labels.get(p.get("verification_path"), (p.get("verification_path") or "verification").replace("_", " "))
    body = (
        f"{salutation}, your Google Business Profile isn't verified yet{uplift_clause}. "
        f"Want me to walk you through the {path} verification — takes 5 min?"
    )
    rationale = f"Internal gbp_unverified, urgency={trigger.get('urgency')}; loss-aversion + effort-externalization (5-min framing), binary-friendly action."
    return body, "binary_yes_stop", rationale


def _h_ipl_match_today(category, merchant, trigger, customer):
    p = trigger.get("payload", {})
    salutation = _owner_salutation(merchant, category)
    match, venue, city = p.get("match"), p.get("venue"), p.get("city")
    if not match:
        raise ComposeError("ipl_match_today: missing match")
    body = (
        f"{salutation}, {match} is on tonight in {city} ({venue}) — expect footfall/orders "
        f"to spike nearby. Want me to push a quick match-night offer post now?"
    )
    rationale = f"External ipl_match_today, urgency={trigger.get('urgency')}; real match/venue/city; urgency lever (same-day), single CTA."
    return body, "open_ended", rationale


def _h_active_planning_intent(category, merchant, trigger, customer):
    p = trigger.get("payload", {})
    salutation = _owner_salutation(merchant, category)
    topic = (p.get("intent_topic") or "").replace("_", " ")
    last_msg = p.get("merchant_last_message")
    if not topic:
        raise ComposeError("active_planning_intent: missing intent_topic")
    body = (
        f"{salutation}, following up on {topic} — you said \"{last_msg}\". "
        f"Want me to draft the outline now so you can just tweak it?"
    )
    rationale = f"Internal active_planning_intent, urgency={trigger.get('urgency')}; picks up the merchant's own stated interest verbatim, moves straight to action (per anti-pattern D: don't re-qualify)."
    return body, "open_ended", rationale


def _h_curious_ask_due(category, merchant, trigger, customer):
    p = trigger.get("payload", {})
    salutation = _owner_salutation(merchant, category)
    templates = {
        "what_service_in_demand_this_week": "What's been your most-requested service this week?",
        "what_customers_ask_most": "What's the #1 thing customers ask you about lately?",
    }
    ask = templates.get(p.get("ask_template"), "What's on top of your mind for the business this week?")
    body = f"{salutation}, quick one — {ask}"
    rationale = "Internal curious_ask_due (scheduled cadence); uses the 'asking the merchant' compulsion lever, which the brief flags as underused in production Vera."
    return body, "open_ended", rationale


def _h_review_theme_emerged(category, merchant, trigger, customer):
    p = trigger.get("payload", {})
    salutation = _owner_salutation(merchant, category)
    theme, n, quote = (p.get("theme") or "").replace("_", " "), p.get("occurrences_30d"), p.get("common_quote")
    if not theme or n is None:
        raise ComposeError("review_theme_emerged: missing theme/occurrences_30d")
    quote_clause = f" One review said: \"{quote}\"." if quote else ""
    body = (
        f"{salutation}, {n} reviews this month mention {theme}.{quote_clause} "
        f"Want me to draft a reply template so you can respond to these fast?"
    )
    rationale = f"Internal review_theme_emerged; {n} occurrences of '{theme}', trend={p.get('trend')}; specific, verifiable, effort-externalized CTA."
    return body, "open_ended", rationale


def _h_renewal_due(category, merchant, trigger, customer):
    p = trigger.get("payload", {})
    salutation = _owner_salutation(merchant, category)
    days, plan, amt = p.get("days_remaining"), p.get("plan"), p.get("renewal_amount")
    if days is None:
        raise ComposeError("renewal_due: missing days_remaining")
    body = f"{salutation}, your {plan} plan renews in {days} days (₹{amt}). Reply YES to auto-renew, or STOP to let it lapse."
    rationale = f"Internal renewal_due, urgency={trigger.get('urgency')}; real days/plan/amount; single binary CTA as required for action triggers."
    return body, "binary_yes_stop", rationale


def _h_winback_eligible(category, merchant, trigger, customer):
    p = trigger.get("payload", {})
    salutation = _owner_salutation(merchant, category)
    days, added = p.get("days_since_expiry"), p.get("lapsed_customers_added_since_expiry")
    body = (
        f"{salutation}, it's been {days} days since your plan lapsed — {added} more "
        f"customers have gone quiet since. Want me to show what reactivating would fix first?"
    )
    rationale = f"Internal winback_eligible; real days_since_expiry + lapsed count; loss-aversion, open CTA."
    return body, "open_ended", rationale


def _h_seasonal_perf_dip(category, merchant, trigger, customer):
    p = trigger.get("payload", {})
    salutation = _owner_salutation(merchant, category)
    metric, delta, note = p.get("metric"), p.get("delta_pct"), p.get("season_note", "").replace("_", " ")
    if not metric or delta is None:
        raise ComposeError("seasonal_perf_dip: missing metric/delta_pct")
    body = (
        f"{salutation}, {metric} are down {_pct(delta)} — looks seasonal ({note}), "
        f"not a red flag. Want a quick idea to soften it anyway?"
    )
    rationale = f"Internal seasonal_perf_dip; flagged is_expected_seasonal=True to avoid false alarm, still offers optional help."
    return body, "open_ended", rationale


def _h_supply_alert(category, merchant, trigger, customer):
    p = trigger.get("payload", {})
    salutation = _owner_salutation(merchant, category)
    molecule, batches, mfr = p.get("molecule"), p.get("affected_batches", []), p.get("manufacturer")
    if not molecule:
        raise ComposeError("supply_alert: missing molecule")
    batch_str = ", ".join(batches)
    body = (
        f"{salutation}, recall alert — {molecule} batches {batch_str} ({mfr}) flagged. "
        f"Check your stock and pull these today. Reply YES once cleared."
    )
    rationale = f"External supply_alert, urgency={trigger.get('urgency')} (max); safety-critical, specific batch numbers, binary confirm."
    return body, "binary_yes_stop", rationale


# --------------------------------------------------------------------------
# Customer-facing handlers (send_as = "merchant_on_behalf")
# --------------------------------------------------------------------------

def _h_recall_due(category, merchant, trigger, customer):
    if not customer:
        raise ComposeError("recall_due needs a customer context")
    p = trigger["payload"]
    name = _cust_name(customer)
    merch_name = _merchant_name(merchant).split("'")[0]  # "Dr. Meera's Dental Clinic" -> "Dr. Meera"
    service = p.get("service_due", "").replace("_", " ")
    slots = p.get("available_slots", [])
    offer = _active_offer(merchant)
    hi_en = _hi_en_ok(category, customer)

    slot_str = " or ".join(s["label"] for s in slots[:2]) if slots else "a time that works for you"
    offer_clause = f" {offer['title']}." if offer else ""

    if hi_en:
        body = (
            f"Hi {name}, {merch_name}'s clinic here 🦷 Aapka {service} due hai. "
            f"Slots: {slot_str}.{offer_clause} Reply 1 or 2, ya koi aur time batayein."
        )
    else:
        body = (
            f"Hi {name}, {merch_name}'s clinic here. Your {service} is due. "
            f"Available: {slot_str}.{offer_clause} Reply 1 or 2, or suggest a time."
        )
    rationale = (
        f"Internal recall_due (scope=customer); service='{service}', {len(slots)} real slots, "
        f"active offer={'yes' if offer else 'no'}; multi-choice slot pick allowed for booking flows per constraint §5.3."
    )
    return body, "menu_choice", rationale


def _h_chronic_refill_due(category, merchant, trigger, customer):
    if not customer:
        raise ComposeError("chronic_refill_due needs a customer context")
    p = trigger["payload"]
    name = _cust_name(customer)
    merch_name = _merchant_name(merchant)
    molecules = p.get("molecule_list", [])
    runs_out = (p.get("stock_runs_out_iso") or "")[:10]
    delivery = p.get("delivery_address_saved")
    if not molecules:
        raise ComposeError("chronic_refill_due: missing molecule_list")
    mol_str = ", ".join(m.capitalize() for m in molecules)
    delivery_clause = "Should I ship to your saved address?" if delivery else "Want to share a delivery address, or pick up in store?"
    body = (
        f"Hi {name}, {merch_name} here. Your regular refill ({mol_str}) runs out around "
        f"{runs_out}. {delivery_clause} Reply YES to confirm."
    )
    rationale = f"Internal chronic_refill_due; real molecule list + runs-out date; binary confirm on a recurring, low-friction action."
    return body, "binary_yes_stop", rationale


def _h_appointment_tomorrow(category, merchant, trigger, customer):
    if not customer:
        raise ComposeError("appointment_tomorrow needs a customer context")
    name = _cust_name(customer)
    merch_name = _merchant_name(merchant)
    # No real appointment time/service exists anywhere in context for this kind
    # (trigger payload is placeholder-only, and CustomerContext carries no
    # booking object). Confirm honestly without inventing a time — see README.
    body = (
        f"Hi {name}, {merch_name} here — just confirming your appointment tomorrow. "
        f"Reply YES to confirm, or let us know if you need to reschedule."
    )
    rationale = (
        "appointment_tomorrow trigger carries no real time/service data (placeholder "
        "payload, and CustomerContext has no booking fields) — deliberately did not "
        "invent a time; kept the confirm generic and honest rather than fabricating specificity."
    )
    return body, "binary_yes_stop", rationale


def _h_customer_lapsed_soft(category, merchant, trigger, customer):
    if not customer:
        raise ComposeError("customer_lapsed_soft needs a customer context")
    name = _cust_name(customer)
    merch_name = _merchant_name(merchant).split("'")[0]
    last_visit = customer["relationship"].get("last_visit")
    services = customer["relationship"].get("services_received", [])
    last_service = services[-1] if services else None
    offer = _active_offer(merchant)
    offer_clause = f" We've got \"{offer['title']}\" running right now if that's useful." if offer else ""
    service_clause = f" for {last_service}" if last_service else ""
    body = (
        f"Hi {name}, {merch_name}'s here — noticed it's been a while since your last visit"
        f"{service_clause} ({last_visit}).{offer_clause} Want me to hold a slot for you?"
    )
    rationale = (
        f"customer_lapsed_soft (scope=customer, trigger payload placeholder); grounded "
        f"instead on real CustomerContext fields (last_visit={last_visit}, last_service='{last_service}') "
        f"— trigger only tells us *why now*, the specifics came from customer data, not the trigger."
    )
    return body, "open_ended", rationale


def _h_customer_lapsed_hard(category, merchant, trigger, customer):
    p = trigger["payload"]
    if not customer:
        raise ComposeError("customer_lapsed_hard needs a customer context")
    name = _cust_name(customer)
    merch_name = _merchant_name(merchant)
    days, focus, months = p.get("days_since_last_visit"), (p.get("previous_focus") or "").replace("_", " "), p.get("previous_membership_months")
    body = (
        f"Hi {name}, {merch_name} here — it's been {days} days. You were working on "
        f"{focus} with us for {months} months before that. Want to pick back up, or should we check in later?"
    )
    rationale = f"customer_lapsed_hard; real days/focus/tenure; low-pressure re-engagement (offers an easy 'not now' exit, matching anti-spam intent)."
    return body, "open_ended", rationale


def _h_trial_followup(category, merchant, trigger, customer):
    if not customer:
        raise ComposeError("trial_followup needs a customer context")
    p = trigger["payload"]
    name = _cust_name(customer)
    merch_name = _merchant_name(merchant)
    options = p.get("next_session_options", [])
    if not options:
        raise ComposeError("trial_followup: missing next_session_options")
    opt_str = " or ".join(o["label"] for o in options[:2])
    body = (
        f"Hi {name}, {merch_name} here — hope you enjoyed the trial! "
        f"Next session: {opt_str}. Want me to lock it in?"
    )
    rationale = f"trial_followup; real trial_date + real next-session options; single binary CTA to convert trial to booking."
    return body, "binary_yes_stop", rationale


def _h_wedding_package_followup(category, merchant, trigger, customer):
    if not customer:
        raise ComposeError("wedding_package_followup needs a customer context")
    p = trigger["payload"]
    name = _cust_name(customer)
    merch_name = _merchant_name(merchant)
    days = p.get("days_to_wedding")
    next_step = (p.get("next_step_window_open") or "").replace("_", " ")
    body = (
        f"Hi {name}, {merch_name} here — {days} days to go! Your {next_step} window is "
        f"open now, worth booking before it gets tight closer to the date. Want me to check availability?"
    )
    rationale = f"wedding_package_followup; real days_to_wedding + next-step window; urgency from a real countdown, not invented."
    return body, "open_ended", rationale


# --------------------------------------------------------------------------
# Generic fallback for any kind without a dedicated handler
# --------------------------------------------------------------------------

def _h_generic_fallback(category, merchant, trigger, customer):
    kind = trigger.get("kind", "unknown")
    if customer:
        name = _cust_name(customer)
        merch_name = _merchant_name(merchant)
        body = f"Hi {name}, {merch_name} here — just checking in. Let us know if there's anything we can help with."
    else:
        salutation = _owner_salutation(merchant, category)
        body = f"{salutation}, wanted to check in — anything I can help with on your account this week?"
    rationale = (
        f"No dedicated handler for trigger kind '{kind}' and payload carried no usable "
        f"real data; fell back to a generic, honest check-in rather than fabricating "
        f"specificity — restraint over hallucination per anti-pattern rules."
    )
    return body, "open_ended", rationale


# --------------------------------------------------------------------------
# Dispatch table + public entrypoint
# --------------------------------------------------------------------------

_HANDLERS = {
    # merchant-facing
    "research_digest": _h_research_digest,
    "regulation_change": _h_regulation_change,
    "cde_opportunity": _h_cde_opportunity,
    "competitor_opened": _h_competitor_opened,
    "perf_dip": _h_perf_dip,
    "perf_spike": _h_perf_spike,
    "milestone_reached": _h_milestone_reached,
    "dormant_with_vera": _h_dormant_with_vera,
    "festival_upcoming": _h_festival_upcoming,
    "category_seasonal": _h_category_seasonal,
    "gbp_unverified": _h_gbp_unverified,
    "ipl_match_today": _h_ipl_match_today,
    "active_planning_intent": _h_active_planning_intent,
    "curious_ask_due": _h_curious_ask_due,
    "review_theme_emerged": _h_review_theme_emerged,
    "renewal_due": _h_renewal_due,
    "winback_eligible": _h_winback_eligible,
    "seasonal_perf_dip": _h_seasonal_perf_dip,
    "supply_alert": _h_supply_alert,
    # customer-facing
    "recall_due": _h_recall_due,
    "chronic_refill_due": _h_chronic_refill_due,
    "appointment_tomorrow": _h_appointment_tomorrow,
    "customer_lapsed_soft": _h_customer_lapsed_soft,
    "customer_lapsed_hard": _h_customer_lapsed_hard,
    "trial_followup": _h_trial_followup,
    "wedding_package_followup": _h_wedding_package_followup,
}


def compose(category: dict, merchant: dict, trigger: dict, customer: Optional[dict] = None) -> dict:
    kind = trigger.get("kind")
    handler = _HANDLERS.get(kind, _h_generic_fallback)

    try:
        body, cta, rationale = handler(category, merchant, trigger, customer)
    except ComposeError:
        # Even a broken/incomplete real-data handler should degrade to the
        # honest fallback rather than raise all the way up to the API layer.
        body, cta, rationale = _h_generic_fallback(category, merchant, trigger, customer)

    return {
        "body": body,
        "cta": cta,
        "send_as": _send_as(trigger),
        "suppression_key": _suppression(trigger),
        "rationale": rationale,
    }
