"""
One ranked action list: the strongest, most urgent signals on top
-----------------------------------------------------------------
Every stock lands in exactly one tier. Tiers are checked in this order, so protecting money you
already have always comes before looking for new trades:

  1  EXIT NOW        a stock you hold has turned red (sell rules or a strong warning filing)
  2  BEARISH: TRIGGER  bad accounts (3+ warning signs) AND a warning filing has fired
  3  BUY: STRONGEST  green ★★: fundamentals + daily chart + weekly chart + insiders/activist buying
  4  BUY             green or green ★
  5  BEARISH: SETUP  bad accounts + falling chart, with a priced capped-risk put idea
  6  WATCH           something is building, no trade yet (warning filing alone, smart money on
                     amber, bearish setup waiting for the chart, held stock losing its trend)
  7  HOLD            held, nothing to do
  8  IGNORE          everything else (hidden from the action list)

Inside a tier, the higher strength score goes first (stars, reward-to-risk, number of warning
signs, weight of the warning filing, smart money).
"""

TIERS = {
    1: ("EXIT NOW", "🚨", "Sell or close the position (paper account does this automatically)."),
    2: ("BEARISH: TRIGGER", "🔻", "Read the warning filing. If it confirms the bad accounts, take the capped-risk put idea."),
    3: ("BUY: STRONGEST", "🟢", "Read the thesis. Full position, or the long-dated call idea."),
    4: ("BUY", "🟢", "Smaller position. More stars = more conviction."),
    5: ("BEARISH: SETUP", "🔻", "Small put spread only, or wait for a warning filing to upgrade it."),
    6: ("WATCH", "👀", "No trade yet. Check again when it changes tier."),
    7: ("HOLD", "🔵", "Nothing to do. The sell rules are watching it."),
    8: ("IGNORE", "⚪", ""),
}


def _idea_ok(r):
    oi = r.get("option_idea")
    return bool(oi and oi.get("meets_rule"))


def rank(r):
    """r = one signals result. Returns {tier, name, icon, action, strength, reason}."""
    sig = r["signal"]
    trig = r.get("trig") or {}
    fired = bool(trig.get("fired"))
    smart = bool(r.get("smart") and r["smart"].get("confirms"))
    bear = r.get("bear_score", 0) or 0
    stars = r.get("stars", 0) or 0
    rr = (r.get("option_idea") or {}).get("reward_risk") or 0

    if r.get("holding") and sig == "RED":
        tier = 1
    elif r.get("catalyst"):
        tier = 2
    elif sig == "GREEN" and stars >= 2:
        tier = 3
    elif sig == "GREEN":
        tier = 4
    elif bear >= 3 and _idea_ok(r):
        tier = 5
    elif fired or (sig == "AMBER" and smart) or bear >= 3 or \
            (r.get("holding") and not (r.get("t") or {}).get("weekly", {}).get("confirms", True)):
        tier = 6
    elif r.get("holding"):
        tier = 7
    else:
        tier = 8

    strength = (stars * 10 + (10 if trig.get("strong") else 0) + (trig.get("score") or 0) * 2
                + bear * 2 + (5 if smart else 0) + min(rr, 10) + (3 if _idea_ok(r) else 0))
    reason = ""
    if tier == 1:
        reason = (r.get("why") or [""])[0]
    elif tier == 2 or (tier == 6 and fired):
        ev = (trig.get("events") or [{}])[0]
        reason = f"{ev.get('label', 'warning filing')} {ev.get('date', '')}".strip()
        if tier == 2:
            reason += f" + {bear} warning signs in the accounts"
    elif tier in (3, 4):
        bits = ["weekly chart agrees" if (r.get("t") or {}).get("weekly", {}).get("confirms") else "",
                "smart money buying" if smart else "", "options idea priced" if _idea_ok(r) else ""]
        reason = ", ".join(b for b in bits if b) or "fundamentals + daily chart"
    elif tier == 5:
        reason = "; ".join((r.get("bear_signs") or [])[:2])
    elif tier == 6:
        reason = ("smart money buying, waiting for the chart" if sig == "AMBER" and smart
                  else "bad accounts, chart still rising: wait" if bear >= 3
                  else "held, weekly trend weakening")
    name, icon, action = TIERS[tier]
    return {"tier": tier, "name": name, "icon": icon, "action": action,
            "strength": round(strength, 1), "reason": reason.replace("<b>", "").replace("</b>", "")}


def order(results):
    """Rank every result in place (adds r['rank']) and return them strongest-first."""
    for r in results:
        r["rank"] = rank(r)
    return sorted(results, key=lambda r: (r["rank"]["tier"], -r["rank"]["strength"], r["ticker"]))
