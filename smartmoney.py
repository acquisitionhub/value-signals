"""
Smart money: following what insiders and activist investors are buying
-----------------------------------------------------------------------
All from free SEC EDGAR filings:

  * Form 4 (filed within 2 business days of a trade): open-market PURCHASES by
    officers and directors (transaction code "P"). Sales, grants and option
    exercises are ignored: sales are mostly noise, grants aren't a decision.
    A "cluster" = at least 2 different insiders buying within the window.
  * Schedule 13D (filed within 5 business days of crossing 5%): an investor taking
    a large stake with intent to influence the company, often an activist. A catalyst.

Research shows clustered insider buying and activist stakes have historically been
followed by above-market returns on average. Not a guarantee for any single stock.
"""
import json, re, time
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path

SMART = {
    "window_days": 90,         # look-back for insider purchases
    "min_buyers": 2,           # distinct insiders buying = cluster
    "min_total_usd": 50_000,   # ignore token purchases
    "activist_days": 180,      # how long a new 13D counts as a live catalyst
}

HERE = Path(__file__).parent
FORM13D = {"SC 13D", "SCHEDULE 13D"}
FORM13D_A = {"SC 13D/A", "SCHEDULE 13D/A"}


class SmartMoney:
    def __init__(self, edgar):
        """edgar: a screener.Edgar (shares its polite SEC session / offline dir)."""
        self.ed = edgar
        self.offline = edgar.offline_dir
        self.cache = HERE / "cache" / "filings"
        if not self.offline:
            self.cache.mkdir(parents=True, exist_ok=True)

    # ---------- raw access ----------
    def _get_text(self, url):
        time.sleep(0.12)
        r = self.ed.s.get(url, timeout=30)
        r.raise_for_status()
        return r.text

    def submissions(self, cik):
        if self.offline:
            p = self.offline / f"submissions_CIK{cik:010d}.json"
            return json.loads(p.read_text()) if p.exists() else {"filings": {"recent": {}}}
        p = HERE / "cache" / f"submissions_CIK{cik:010d}.json"
        if p.exists() and time.time() - p.stat().st_mtime < 6 * 3600:
            return json.loads(p.read_text())
        data = json.loads(self._get_text(f"https://data.sec.gov/submissions/CIK{cik:010d}.json"))
        p.write_text(json.dumps(data))
        return data

    def document(self, cik, accession, doc):
        """Filing documents never change, so cache them forever."""
        raw_doc = re.sub(r"^xsl[^/]+/", "", doc)          # Form 4 XML lives without the xsl prefix
        key = f"{accession}_{raw_doc}".replace("/", "_")
        if self.offline:
            p = self.offline / key
            return p.read_text() if p.exists() else ""
        p = self.cache / key
        if p.exists():
            return p.read_text()
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/{raw_doc}"
        txt = self._get_text(url)
        p.write_text(txt)
        return txt

    # ---------- parsing ----------
    @staticmethod
    def parse_form4(xml_text):
        """Return list of open-market purchases: {owner, role, shares, price, value, date, plan}."""
        out = []
        try:
            root = ET.fromstring(xml_text.encode() if isinstance(xml_text, str) else xml_text)
        except ET.ParseError:
            return out
        v = lambda el, path: (el.findtext(path) or "").strip() if el is not None else ""
        owners = root.findall("reportingOwner")
        name = v(owners[0], "reportingOwnerId/rptOwnerName") if owners else "Unknown"
        rel = owners[0].find("reportingOwnerRelationship") if owners else None
        roles = []
        if rel is not None:
            if v(rel, "officerTitle"):
                roles.append(v(rel, "officerTitle"))
            elif v(rel, "isOfficer") in ("1", "true"):
                roles.append("Officer")
            if v(rel, "isDirector") in ("1", "true"):
                roles.append("Director")
            if v(rel, "isTenPercentOwner") in ("1", "true"):
                roles.append("10% owner")
        plan = v(root, "aff10b5One") in ("1", "true")   # pre-scheduled trading plan = weaker signal
        for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
            if v(tx, "transactionCoding/transactionCode") != "P":
                continue
            if v(tx, "transactionAmounts/transactionAcquiredDisposedCode/value") not in ("A", ""):
                continue
            try:
                shares = float(v(tx, "transactionAmounts/transactionShares/value") or 0)
                price = float(v(tx, "transactionAmounts/transactionPricePerShare/value") or 0)
            except ValueError:
                continue
            out.append({"owner": name.title(), "role": ", ".join(roles) or "Insider", "shares": shares,
                        "price": price, "value": shares * price, "plan": plan,
                        "date": v(tx, "transactionDate/value")})
        return out

    def _activist_name(self, cik, accession, doc):
        try:
            txt = self.document(cik, accession, doc)
        except Exception:
            return None
        m = re.search(r"<(?:\w+:)?(?:reportingPersonName|filerName|nameOfReportingPerson)>\s*([^<]{3,80})<", txt)
        if not m:
            return None
        name = m.group(1).strip().title()
        for x in ("Lp", "Llc", "Ltd", "Inc", "L.P.", "Plc"):
            name = re.sub(rf"\b{re.escape(x)}$", x.upper() if x not in ("Inc", "Ltd") else x, name)
        return name

    # ---------- the check ----------
    def check(self, cik):
        today = date.today()
        rec = self.submissions(cik).get("filings", {}).get("recent", {})
        forms, accs = rec.get("form", []), rec.get("accessionNumber", [])
        dates, docs = rec.get("filingDate", []), rec.get("primaryDocument", [])
        since = (today - timedelta(days=SMART["window_days"])).isoformat()
        since13d = (today - timedelta(days=SMART["activist_days"])).isoformat()

        buys, activist = [], None
        for f, a, d, doc in zip(forms, accs, dates, docs):
            if f == "4" and d >= since:
                try:
                    buys += self.parse_form4(self.document(cik, a, doc))
                except Exception:
                    continue
            elif f in FORM13D and d >= since13d and activist is None:
                activist = {"date": d, "who": self._activist_name(cik, a, doc),
                            "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{a.replace('-', '')}/"}

        buys = [b for b in buys if not b["date"] or b["date"] >= since]
        discretionary = [b for b in buys if not b["plan"]]
        by_owner = {}
        for b in discretionary:
            o = by_owner.setdefault(b["owner"], {"owner": b["owner"], "role": b["role"], "value": 0.0, "last": ""})
            o["value"] += b["value"]; o["last"] = max(o["last"], b["date"])
        buyers = sorted(by_owner.values(), key=lambda x: -x["value"])
        total = sum(o["value"] for o in buyers)
        cluster = len(buyers) >= SMART["min_buyers"] and total >= SMART["min_total_usd"]

        parts = []
        if buyers:
            who = ", ".join(f"{o['owner']} ({o['role']}) ${o['value']:,.0f}" for o in buyers[:3])
            parts.append(f"{len(buyers)} insider{'s' if len(buyers) > 1 else ''} bought ${total:,.0f} "
                         f"in {SMART['window_days']} days: {who}" + (" ..." if len(buyers) > 3 else ""))
        else:
            parts.append(f"no insider buying in {SMART['window_days']} days")
        if activist:
            parts.append(f"activist 13D filed {activist['date']}" + (f" by {activist['who']}" if activist["who"] else ""))
        return {"cluster": bool(cluster), "buyers": len(buyers), "total": total, "buyers_detail": buyers[:5],
                "activist": activist, "confirms": bool(cluster or activist), "text": "; ".join(parts)}
