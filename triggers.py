"""
Fraud-risk triggers: the "verification events" that end a fraud
----------------------------------------------------------------
The weekly scan finds the SETUP (Beneish M-Score, accruals, Altman Z, Piotroski in screener.py,
counted by playbook.bear_score). A setup alone says nothing about WHEN: Wirecard looked bad from
2015 but only collapsed in June 2020, when its auditors could no longer avoid checking the cash.

This module watches free SEC EDGAR filings for the events where a company is forced to be checked
by someone independent, and fails or dodges that check:

  event                         where it shows up                          weight
  ---------------------------   ----------------------------------------   ------
  Restatement / non-reliance    8-K Item 4.02 (past accounts unreliable)   3  strong
  Auditor resigned              8-K Item 4.01 + "resigned"/"declined"      3  strong
  Annual report overdue         no 10-K for 15+ months (was filing them)   3  strong
  Late annual report notice     NT 10-K / NT 20-F                          2
  Auditor dismissed / replaced  8-K Item 4.01 (company changed auditor)    1  (often routine rotation)
  Delisting warning             8-K Item 3.01 (fails a listing rule)       2
  Late quarterly report notice  NT 10-Q                                    1
  CFO / chief accountant leaves 8-K Item 5.02 naming the finance chief     1

A trigger has "fired" when the events in the look-back window add up to TRIG["fire_weight"]
or more. Every event links to the filing so you can read it yourself before acting.

Uses the same polite SEC session and cache as smartmoney.py (no extra download for most checks:
the 8-K item numbers are in the submissions index; only 4.01 and 5.02 filings are opened).
"""
import re
from datetime import date, timedelta

TRIG = {
    "lookback_days": 365,      # events older than this no longer count
    "fresh_days": 45,          # an event this recent is "new": it is what opens a position
    "fire_weight": 2,          # total weight needed for the trigger to fire
    "overdue_days": 455,       # ~15 months without a 10-K = annual report overdue
}

EVENTS = {
    "restatement":    (3, "restatement: past accounts can no longer be relied on (8-K 4.02)"),
    "auditor_quit":   (3, "auditor resigned or declined to stand again (8-K 4.01)"),
    "overdue_10k":    (3, "annual report overdue"),
    "nt_10k":         (2, "late annual report notice (NT 10-K)"),
    "auditor_change": (1, "auditor dismissed or replaced (8-K 4.01)"),
    "delisting":      (2, "exchange delisting warning (8-K 3.01)"),
    "nt_10q":         (1, "late quarterly report notice (NT 10-Q)"),
    "cfo_exit":       (1, "chief financial / accounting officer leaving (8-K 5.02)"),
}

NT_ANNUAL = {"NT 10-K", "NT 10-K/A", "NT 20-F", "NT 20-F/A", "NT 10-KT"}
NT_QUARTER = {"NT 10-Q", "NT 10-Q/A"}
ANNUAL = {"10-K", "10-K405", "10-KT", "20-F", "40-F"}
EIGHT_K = {"8-K", "8-K/A"}

QUIT_WORDS = re.compile(r"\b(resign\w*|declin\w* to stand|will not stand for re-?election|"
                        r"not to stand for re-?(?:appointment|election))\b", re.I)
FINANCE_CHIEF = re.compile(r"chief financial officer|principal financial officer|principal accounting officer|"
                           r"chief accounting officer|\bCFO\b", re.I)
LEAVING = re.compile(r"\b(resign\w*|depart\w*|step(?:s|ped)? down|terminat\w*|retire\w*|leave the company|"
                     r"separat\w* from)\b", re.I)


def strip_html(txt):
    txt = re.sub(r"(?is)<(script|style).*?</\1>", " ", txt or "")
    txt = re.sub(r"(?s)<[^>]+>", " ", txt)
    txt = re.sub(r"&nbsp;|&#160;|&#xa0;", " ", txt)
    return re.sub(r"\s+", " ", txt)


def classify_401(text):
    """Item 4.01 text -> 'auditor_quit' when the auditor walked away, else 'auditor_change'."""
    t = strip_html(text)
    m = re.search(r"Item\s*4\.01(.{0,4000})", t, re.I)
    section = m.group(1) if m else t[:6000]
    return "auditor_quit" if QUIT_WORDS.search(section) else "auditor_change"


def is_cfo_exit(text):
    t = strip_html(text)
    m = re.search(r"Item\s*5\.02(.{0,3000})", t, re.I)
    section = m.group(1) if m else t[:5000]
    # look for the finance chief and a leaving word close together
    for f in FINANCE_CHIEF.finditer(section):
        window = section[max(0, f.start() - 300): f.end() + 300]
        if LEAVING.search(window):
            return True
    return False


class Triggers:
    def __init__(self, smart_money):
        """smart_money: a smartmoney.SmartMoney (reuses its submissions() and document() caches)."""
        self.sm = smart_money

    def check(self, cik, today=None):
        today = today or date.today()
        rec = self.sm.submissions(cik).get("filings", {}).get("recent", {})
        forms, accs = rec.get("form", []), rec.get("accessionNumber", [])
        dates, docs = rec.get("filingDate", []), rec.get("primaryDocument", [])
        items = rec.get("items", []) or [""] * len(forms)
        since = (today - timedelta(days=TRIG["lookback_days"])).isoformat()
        fresh_since = (today - timedelta(days=TRIG["fresh_days"])).isoformat()

        events, last_annual, ever_annual = [], None, False
        for f, a, d, doc, it in zip(forms, accs, dates, docs, items):
            if f in ANNUAL:
                ever_annual = True
                last_annual = max(last_annual or d, d)
            if d < since:
                continue
            url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{a.replace('-', '')}/{doc}"
            kinds = []
            if f in NT_ANNUAL:
                kinds.append("nt_10k")
            elif f in NT_QUARTER:
                kinds.append("nt_10q")
            elif f in EIGHT_K:
                its = {x.strip() for x in (it or "").split(",")}
                if "4.02" in its:
                    kinds.append("restatement")
                if "3.01" in its:
                    kinds.append("delisting")
                if "4.01" in its:
                    try:
                        kinds.append(classify_401(self.sm.document(cik, a, doc)))
                    except Exception:
                        kinds.append("auditor_change")
                if "5.02" in its:
                    try:
                        if is_cfo_exit(self.sm.document(cik, a, doc)):
                            kinds.append("cfo_exit")
                    except Exception:
                        pass
            for k in kinds:
                w, label = EVENTS[k]
                events.append({"kind": k, "date": d, "weight": w, "label": label, "url": url,
                               "key": f"{a}:{k}"})

        if ever_annual and last_annual:
            age = (today - date.fromisoformat(last_annual)).days
            if age > TRIG["overdue_days"]:
                w, label = EVENTS["overdue_10k"]
                due = (date.fromisoformat(last_annual) + timedelta(days=TRIG["overdue_days"])).isoformat()
                events.append({"kind": "overdue_10k", "date": due, "weight": w,
                               "label": f"{label}: last one filed {last_annual} ({age // 30} months ago)",
                               "url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=10-K",
                               "key": f"overdue:{last_annual}"})

        # one of each kind counts toward the score (three NT 10-Qs are not 3x the evidence)
        by_kind = {}
        for e in sorted(events, key=lambda e: e["date"], reverse=True):
            by_kind.setdefault(e["kind"], e)
        score = sum(e["weight"] for e in by_kind.values())
        fired = score >= TRIG["fire_weight"]
        strong = any(e["weight"] >= 3 for e in by_kind.values())
        fresh = [e for e in events if e["date"] >= fresh_since]
        events.sort(key=lambda e: (e["date"], e["weight"]), reverse=True)
        if events:
            text = "; ".join(f"{e['label']} {e['date']}" for e in events[:3]) + (" ..." if len(events) > 3 else "")
        else:
            text = f"no warning filings in {TRIG['lookback_days']} days"
        return {"fired": fired, "strong": strong, "score": score, "fresh": bool(fresh) and fired,
                "events": events[:6], "keys": [e["key"] for e in events], "text": text}
