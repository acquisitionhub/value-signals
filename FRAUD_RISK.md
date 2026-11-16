# Fraud-risk module

The idea comes from the Wirecard case. Bad accounts tell you **what** is wrong. A filing that shows
the company failing (or dodging) an independent check tells you **when** to act.

## 1. Setup: the accounts look wrong (weekly scan, already built)

Calculated in `screener.py` from SEC XBRL data (`forensic()` and `altman()`), counted in
`playbook.bear_score()`:

| Measure | Warning level | What it catches |
|---|---|---|
| Beneish M-Score | above -1.78 (grey from -2.22) | earnings manipulation: receivables outgrowing sales, rising accruals, falling asset quality |
| Accruals ratio (Sloan) | above 10% of assets | profit that isn't arriving as cash |
| Altman Z-Score | below 1.81 | bankruptcy risk over the next 1–2 years |
| Piotroski F-Score | 2 or less | weak and worsening financial health |

The weekly scan writes stocks with 2 or more warning signs to `output/bearish.txt`. The daily run
then watches them.

## 2. Trigger: a warning filing (new: `triggers.py`)

Checked on every run for each holding, watchlist stock and scan candidate, using EDGAR filings:

| Event | Filing | Weight |
|---|---|---|
| Restatement / non-reliance | 8-K Item 4.02 | 3 strong |
| Auditor resigned or declined to stand | 8-K Item 4.01 + wording check | 3 strong |
| Annual report overdue | no 10-K for 15+ months | 3 strong |
| Late annual report notice | NT 10-K / NT 20-F | 2 |
| Delisting warning | 8-K Item 3.01 | 2 |
| Auditor dismissed or replaced | 8-K Item 4.01 | 1 (often routine) |
| Late quarterly report notice | NT 10-Q | 1 |
| Finance chief leaving | 8-K Item 5.02 naming the CFO / chief accounting officer | 1 |

Rules: the look-back window is 365 days, and each kind of event counts once. The trigger
**fires** at a total weight of 2 or more. It is **strong** if any single event has weight 3.
The settings are in `TRIG` at the top of `triggers.py`.

## 3. What happens when a trigger fires

| Situation | Result |
|---|---|
| You hold it (or the paper account does), strong trigger | Signal turns **RED**, urgent phone alert, paper account sells |
| Green / amber stock, strong trigger | **RED**: stay out |
| Green stock, normal trigger | Downgraded to **AMBER**: don't buy until it's resolved |
| Bearish setup (3+ signs) **and** trigger fired | **Setup + trigger**: put idea priced straight away, without waiting for the weekly chart to turn; urgent alert |
| Any new warning filing | Daily phone alert the day it appears (each filing alerts once) |

Every event links to the actual filing on the dashboard. **Read it before you act.**

## 4. The Wirecard timeline mapped onto the rules

| Date | Event | Module response |
|---|---|---|
| 2015–2019 | FT articles, short-selling ban | Setup only: watch, no trade |
| 28 Apr 2020 | KPMG special audit can't verify most profits; annual report postponed | Trigger fires (late annual report): put idea priced |
| May–Jun 2020 | Report postponed again (4 June, then 18 June) | Still firing: alert again on each new filing |
| 18 Jun 2020 | EY refuses to sign; €1.9bn missing | Strong trigger: RED, exit any long position |

Wirecard was German, so it never filed with the SEC. The table shows what the US equivalents
(NT 10-K, 8-K 4.01/4.02) would have looked like.

## 5. Limits

- Most flagged companies are not frauds. A trigger is a reason to read the filing, not proof.
- NT 10-K filings by small companies are often routine. Weights are tuned for large caps.
- Options lose 100% of the premium if the move comes after expiry. Ideas are priced so that the
  expiry falls after the event window (put spreads at 75–200 days, LEAPS puts at 12–24 months).
- Paper-trade for at least 3 months before real money. Not investment advice.

## Possible next steps

- Auditor quality: a small or unknown audit firm signing a large company's accounts.
- Credit warning: bond prices falling before the shares.
- Related-party transactions and receivables days, from the 10-K text.
