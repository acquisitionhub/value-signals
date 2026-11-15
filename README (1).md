# Value signals (prototype)

- **screener.py** finds undervalued US companies and writes thesis drafts
- **signals.py** sends phone notifications: a weekly summary plus daily alerts when a signal changes
- **paper.py** (via `signals.py --paper`) trades the signals on a free Alpaca demo account to test them
- **dashboard.py** builds `docs/index.html`, a web page with the signals, paper portfolio vs the S&P 500, trade journal, screener and thesis drafts
- **.github/workflows/signals.yml** runs it all on GitHub's servers on schedule, so your computer doesn't need to be on

Research aid only: it never trades real money and isn't investment advice.

---

# Running it on GitHub (recommended)

GitHub runs everything in the cloud for free and hosts the dashboard as a web page.

## 1. Create the repository
1. On github.com click **New repository**, name it e.g. `value-signals`.
2. **Public or private?** Free GitHub Pages (the dashboard web page) needs a **public** repo. Public means anyone with the link can see your watchlist, signals and paper trades, but **never your keys** (they're stored as encrypted Secrets, and `settings.txt` is excluded by `.gitignore`). The page is also marked not to appear in search engines.
   If you'd rather keep it private, the scheduled runs and phone alerts still work; you'd just open `docs/index.html` from the repo instead of a web link.
3. Upload every file and folder from this zip (drag and drop works), **including the hidden `.github` folder and `.gitignore`**. If drag-and-drop skips `.github`, create the file manually: Add file → Create new file → name it `.github/workflows/signals.yml` and paste in the contents.

## 2. Add your secrets
Repo → **Settings → Secrets and variables → Actions → New repository secret**. Add:
| Name | Value |
|---|---|
| `NTFY_TOPIC` | A long private name for your phone alerts, e.g. `signals-` followed by random letters. Subscribe to the same name in the ntfy app |
| `ALPACA_KEY_ID` | Your Alpaca **paper** API key ID (turns on paper trading and reliable price data) |
| `ALPACA_SECRET_KEY` | Your Alpaca **paper** secret key |
Optional for email: `SMTP_USER`, `SMTP_PASS`, `EMAIL_TO`.

## 3. Turn on the dashboard page
Repo → **Settings → Pages** → Source: *Deploy from a branch* → Branch: `main`, folder: `/docs` → Save.
After the first run, your dashboard is at `https://YOUR-USERNAME.github.io/value-signals/` (Pages shows the exact link).

## 4. First run
Repo → **Actions** tab → enable workflows if asked → **Value signals** → **Run workflow** → choose *weekly* → Run.
It takes 2–5 minutes. Then you should have a phone notification, results committed to the `output/` folder, and the dashboard live.

## Schedule (automatic after that)
- **Daily** Tue–Sat 07:00 UTC (7am UK in winter, 8am in summer): notifies only when a signal changes, and places paper trades
- **Weekly** Saturday 08:00 UTC: refreshes the screener and theses, sends the weekly summary

GitHub sometimes starts scheduled runs 5–30 minutes late. That's normal and doesn't matter here.

## Changing things
Edit files directly on github.com (pencil icon): `watchlist.txt` for your stocks, `SCREEN`/`DCF` at the top of `screener.py`, `RULES` in `signals.py`, `PAPER` in `paper.py`. Changes apply from the next run.

Note: GitHub pauses scheduled workflows in repos with no activity for 60 days. The bot's own commits normally keep it active; if it ever stops, click *Enable workflow* on the Actions tab.

---

# Running it on your own computer

Copy `settings.example.txt` to `settings.txt` and fill it in (it stays private and isn't uploaded). Then use the commands below. `python dashboard.py` rebuilds the page, which you can open in your browser from `docs/index.html`.

---

## screener.py

Steps 1–2 of your research agent: it pulls US company financials from SEC EDGAR (free), screens them, values each one with a simple DCF, and writes a one-page thesis draft for every company that passes.

## Set up (one time, about 5 minutes)
1. Install Python 3 from python.org if you don't have it.
2. Unzip this folder somewhere, open a terminal in it, and run:
   ```
   pip install requests yfinance pandas
   ```
   (yfinance fetches share prices automatically. Without it, put prices in `prices.csv`.)
3. Open `screener.py` and check `USER_AGENT` near the top. The SEC requires a name and email, and it's set to your CAH address.

## Run it
```
python screener.py                     # screens the tickers in watchlist.txt
python screener.py --tickers KO PFE VZ # quick ad-hoc list
python screener.py --top 300           # roughly the 300 largest US filers (takes a few minutes)
python screener.py --all-theses        # write a thesis for every company, not just passes
```
Results land in `output/`:
- `screen_DATE.csv`: every company, ranked by margin of safety, with the reason it failed if it did
- `theses/TICKER_DATE.md`: the one-page drafts (open in any text editor, or paste into Claude)

## Weekly scan (finds new candidates for you)
Every Saturday the workflow runs `python screener.py --top 500`. It checks the **~500 largest US companies** on SEC EDGAR (one per company, so no duplicate share classes) plus everything in `watchlist.txt`:
- Banks, insurers, brokers and REITs (SEC industry codes 6000–6799) are skipped, because a free-cash-flow valuation doesn't fit them. If you put one on your watchlist it shows grey with that reason.
- Companies that pass the screen go into `output/discovered.txt`, and the daily signals check them alongside your watchlist. They're labelled "found by scan" on the dashboard.
- The list is rewritten each week. A stock that drops out stops being checked, unless the paper account holds it.
- Thesis drafts are written for every pass and for your own watchlist stocks.
Change `500` in `.github/workflows/main.yml` for a bigger or smaller scan. 1,000 takes roughly twice as long.

## The screen (edit `SCREEN` at the top of screener.py)
A company passes if all of these are true:
- revenue grew at least 5% last year
- free cash flow was positive in each of the last 3 years
- free cash flow is improving **or** debt is falling
- base-case value is at least 25% above the share price
- share count isn't growing more than 3% a year

## The valuation (edit `DCF`)
- Starts from 3-year average free cash flow (operating cash flow minus capex)
- Growth = historical revenue growth, capped at 12%, held 5 years, then fading to 2.5% over 5 years
- 10% discount rate; debt deducted, cash added; divided by diluted shares
- Bear = half the growth, bull = 1.5× the growth

This is deliberately simple. Treat it as a filter that tells you where to look, not a price target.

## Optional: Claude drafts the thesis section
Set an Anthropic API key, then add `--ai`:
```
export ANTHROPIC_API_KEY=sk-ant-...      (Windows: set ANTHROPIC_API_KEY=sk-ant-...)
python screener.py --ai
```
It fills in what the business does, why it might be mispriced, catalysts, and what would break the thesis. Without a key, the section is left as prompts for you to fill in, or you can paste the file into a Claude chat.

---

# Signals on your phone (signals.py)

Every stock in `watchlist.txt` gets a light, and so does anything in `holdings.csv` (what you own):

| Light | Meaning |
|---|---|
| 🟢 Green | Passes the fundamental screen **and** at least 3 of 5 daily technical confirmations |
| 🟢★ / ★★ Green with stars | As above, plus ★ if the weekly chart agrees and ★ if insiders or an activist are buying |
| 🟡 Amber | Passes fundamentals, but fewer than 3 confirmations, so wait |
| ⚪ Grey | Fails fundamentals, ignored whatever the chart says |
| 🔴 Red (watchlist) | Good fundamentals, but the price is breaking down, so avoid for now |
| 🔴 Red (holdings) | Exit signal: target value reached, thesis broken (FCF negative, revenue falling, debt up 20%+), price breakdown, or stop-loss hit. Sent as a high-priority alert |
| 🔵 Hold | You own it and there's no exit signal |

**Daily checks (5 confirmations, each from a different indicator family):**
1. Trend: price above its 200-day average, or just crossed back above
2. RSI: recovering from below 35, or between 40 and 65 and rising
3. MACD: crossed above its signal line, or above it and rising
4. Volume: up days carry 1.2x+ the volume of down days (last 20 days)
5. Relative strength: beating the S&P 500 over 3 months

**Smart money check (SEC filings, free):**
- **Insider cluster:** 2+ different officers/directors bought shares on the open market in the last 90 days, at least $50k in total (Form 4, filed within 2 business days of the trade). Sales, share grants and pre-scheduled 10b5-1 plan purchases are ignored.
- **Activist stake:** a Schedule 13D (an investor crossing 5% with intent to influence the company) filed in the last 180 days. That's a potential catalyst.
Settings are in `SMART` at the top of `smartmoney.py`.

**Stars on a green:** ★ = the weekly chart agrees, ★ = smart money is buying. ★★ is the highest-conviction setup.
You also get an alert the day smart money starts buying any non-grey stock on your list, and the paper account sizes positions by stars (2.5% / 5% / 7.5%).

**Weekly chart check (one of the stars):** price above its 30-week average, that average rising, and weekly MACD positive. The daily checks time the entry, and the weekly check confirms the bigger trend is with you.

## Two modes
| | Weekly (`python signals.py`) | Daily (`python signals.py --mode daily`) |
|---|---|---|
| Sends | Summary notification of every green and red | A notification **only if a signal changed** |
| Triggers | Every time | New green, new red, a green becoming strong, a green lost, a stop-loss hit |
| Quiet days | n/a | Nothing sent |
| Full report | `output/signals_weekly_DATE.html` | `output/signals_daily_DATE.html` |

The first daily run only records a baseline. Alerts start from the next run.

## Set up notifications (one time, 2 minutes)
1. Install the free **ntfy** app on your phone (App Store or Google Play). No account needed.
2. Run `python signals.py` once on your computer. It creates a private topic name (like `signals-2ba8...`), saves it in `settings.txt` and prints it.
3. In the ntfy app tap **+**, enter that exact topic name, and subscribe. You'll get the first summary straight away the next time you run it.
4. Treat the topic name like a password, since anyone who knows it can read your alerts.

Email is optional: fill in `SMTP_PASS` in `settings.txt` (a Google app password) if you also want the full report by email. `--no-send` just saves the report without notifying.

## Your holdings
Add rows to `holdings.csv` when you buy: `KO,58.20,52.00` (ticker, entry price, optional stop-loss). With no stop, it uses 20% below entry. Remove the row when you sell.

## Run it automatically
US markets close at 9pm UK time, so the checks run the next morning on final data:
- **Daily:** Tuesday to Saturday, 7am (covers Monday to Friday trading days)
- **Weekly:** Saturday, 8am

**Windows:** Task Scheduler → Create Basic Task, twice:
1. "Daily signals": Weekly → tick Tue, Wed, Thu, Fri, Sat → 07:00 → Start a program → Program `python`, Arguments `signals.py --mode daily`, **Start in** = this folder's full path
2. "Weekly signals": Weekly → Saturday → 08:00 → same, with Arguments `signals.py`

**Mac:** `crontab -e`, then add (change the path):
```
0 7 * * 2-6 cd /Users/you/value-screener && /usr/bin/python3 signals.py --mode daily >> output/log.txt 2>&1
0 8 * * 6   cd /Users/you/value-screener && /usr/bin/python3 signals.py >> output/log.txt 2>&1
```
The computer needs to be on at those times. If it often isn't, tell Claude and we'll move it to a cloud schedule.

SEC data is cached for 7 days (`cache/` folder), so daily runs are quick and only prices are re-downloaded.

## Changing the rules
At the top of `signals.py`, `RULES` sets how many confirmations make a green (default 3) and the default stop-loss (20%). The fundamental screen is still controlled by `SCREEN` in `screener.py`.

**Before trusting it with real money:** run it for a couple of months and compare its lights with what the stocks actually did. A backtest over past years is the next step worth building.

---

# Paper trading (Alpaca demo account)

Test the signals with **fake money and real prices** before risking anything. Add `--paper` and the signals trade a free Alpaca paper account:

- **BUY** every green stock you don't hold: 2.5% of the account, 5% with ★, 7.5% with ★★, max 15 positions
- **SELL** any position that turns red: target value reached, thesis broken, price breakdown, or 20% stop-loss
- Your paper positions are used as holdings (`holdings.csv` is ignored in paper mode, so keep that for real trades)
- Every order goes in `output/paper_journal.csv` with the reason
- Each notification ends with the account value and its return vs the S&P 500 since you started

Orders are market orders that fill at the next US open (2:30pm UK). Shares only for now.
The script refuses to run against anything except Alpaca's **paper** endpoint, so it can't touch real money.

## Set up (5 minutes)
1. Sign up free at **alpaca.markets** with your email. A paper-only account works from the UK and starts with $100,000 of fake money.
2. In the dashboard, make sure you're on the **Paper** account, then go to API Keys → Generate.
3. Paste both keys into `settings.txt` (`ALPACA_KEY_ID`, `ALPACA_SECRET_KEY`).
4. Test it: `python signals.py --paper`. You should get a notification listing any paper buys.

## Schedule (use these instead of the earlier ones)
**Windows Task Scheduler:** same two tasks as above, with Arguments `signals.py --mode daily --paper` and `signals.py --paper`.

**Mac crontab:**
```
0 7 * * 2-6 cd /Users/you/value-screener && /usr/bin/python3 signals.py --mode daily --paper >> output/log.txt 2>&1
0 8 * * 6   cd /Users/you/value-screener && /usr/bin/python3 signals.py --paper >> output/log.txt 2>&1
```

## Reading the results
- Give it **at least 3 months** before judging. A handful of trades proves nothing either way.
- Compare against the S&P 500 line in each notification. If the system can't beat simply holding the index, it isn't adding value.
- Open `paper_journal.csv` monthly: which reasons led to the best and worst trades? Adjust `SCREEN`, `RULES` or `PAPER` (top of `paper.py`) one at a time.
- Paper fills are kinder than real ones (no slippage, perfect fills), so real results will be a little worse.

To start over: delete the paper account in the Alpaca dashboard, create a new one with new keys, and delete `output/paper_start.json`, `output/state_paper.json` and `output/paper_journal.csv`.

---

## Known limits
- US companies filing 10-Ks only (no UK/EU, no ADR-only filers).
- Banks, insurers and REITs are excluded automatically (see Weekly scan).
- Companies use different XBRL tags, so some fields may show n/a. The tool tries the common ones.
- Debt covers reported long-term debt only (not leases).
- Always check the numbers against the actual 10-K before acting. This is a research aid, not advice.
