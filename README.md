# Telegram-Stocks: a daily Bursa Malaysia briefing bot

Every weekday evening (default **7:00 pm Malaysia time**, after Bursa closes) this bot sends you a Telegram briefing.
Every section is written by an AI in **plain words for a retail investor**: what happened *and what it means*.

| # | Section | What you get |
|---|---------|--------------|
| 1 | **Global news** | AI, oil/energy, semiconductors, trade policy, supply chain, rates, China demand, merged into 3-5 themes with the Malaysian angle |
| 2 | **Forward calendar** | BNM, Fed, OPEC+, PBoC dates; DOSM data (GDP/CPI/trade/IPI); Budget 2027 / 13MP / RON95 subsidy tracker; MSCI and FTSE Bursa (KLCI-50) index events; companies expected to report results soon |
| 3 | **Company news** | UMA queries, take-overs, disposals, related-party deals, profit warnings, PN17/GN3, contract wins, director and substantial-shareholder trades (parsed from the filings: who, how many shares, how much), results with YoY/QoQ growth, plus The Edge Malaysia / Bernama headlines |
| 4 | **Government contracts** | Data-centre approvals, NETR/energy transition, ECRL, MRT3, other infrastructure awards, linked to listed names |
| 5 | **Thesis check** | Scores the day's news: does it **support / weaken / leave unchanged** each of your theses? Includes the **strongest counter-argument** and a 7-day trend |
| 6 | **Technical alerts** | Daily candles: EMA20/50/200, WMA20, RSI14, Stochastic 14-3-3, Bollinger 20/2, volume spikes. For stocks in the news, your watchlist, and the market's top high-volume names |
| 7 | **Key stock cards** | For the most important stocks: What happened, Why this stock, Direction (Bull/Bear/Neutral), Confidence, Time horizon, Technicals, What to check next |

> **Not financial advice.** Output is information to help your own research. AI can be wrong. Always read the filing.

## How it works (all free)

```
GitHub Actions (cron, 7pm MYT)
   |
   +-- Bursa filings .......... KLSE Screener (bursamalaysia.com blocks bots)
   +-- News ................... Google News RSS (incl. site:theedgemalaysia.com), Bernama RSS
   +-- Prices ................. Yahoo Finance (<code>.KL), daily candles
   +-- Calendar ............... config/calendar.yaml (verified dates) + generated recurring events
   |
   +-- Rules first ............ tag and score filings (UMA, take-over, PN17...) so nothing important is skipped
   +-- AI second .............. Gemini (free) -> Groq (free) -> OpenAI (optional) fallback chain
   |
   +-- Telegram ............... digest split into <4096-char messages
```

If every AI provider is down, you still get the digest with raw facts (no plain-English layer) plus a notice.
If the whole run crashes, you get a Telegram message saying so.

## Setup (about 15 minutes)

### 1. Create the Telegram bot
1. In Telegram, message **@BotFather** -> `/newbot` -> copy the **token**.
2. Open your new bot and press **Start** (send any message).
3. Find your chat id: `python -m stockbot chatid` (after step 3 below) or message **@userinfobot**.

### 2. Get a free AI key
- **Gemini** (primary): https://aistudio.google.com/apikey
- **Groq** (backup, optional but recommended): https://console.groq.com/keys

Free-tier limits and model names change often. The bot uses about **7 AI calls per day**, and
`python -m stockbot models` lists the models your key can currently use. Override with `GEMINI_MODEL_FAST`,
`GEMINI_MODEL_SMART`, `GROQ_MODEL`. If a configured Gemini model is retired the bot auto-selects a working Flash model.

### 3. Try it locally
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in the keys; .env is git-ignored
python -m stockbot check      # verifies Telegram, AI keys and every data source
python -m stockbot run --dry-run   # prints the digest instead of sending it
python -m stockbot run             # sends it to Telegram
```

### 4. Run it daily for free (GitHub Actions)
1. Repo -> **Settings -> Secrets and variables -> Actions -> New repository secret**. Add:
   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `GEMINI_API_KEY`, and optionally `GROQ_API_KEY`, `OPENAI_API_KEY`.
2. The workflow `.github/workflows/daily-brief.yml` runs Mon-Fri at **11:00 UTC = 7:00 pm MYT**
   (GitHub can delay scheduled runs by up to ~30 min, still inside 6-9 pm). Edit the `cron` line to change the time.
3. Test it: **Actions -> Daily Bursa Brief -> Run workflow** (tick *Dry run* first to see the output in the log).

Notes: scheduled workflows run from the **default branch**. GitHub pauses schedules on repos with no activity for
60 days; if that happens, re-enable it in the Actions tab. Run state is stored on an auto-created `bot-state` branch.

## Make it yours (all in `config/`)

| File | What to edit |
|------|--------------|
| `watchlist.yaml` | Your Bursa stock codes. They always get a technical read and a report-date check |
| `theses.yaml` | Your theses. A sharper **AI-bubble thesis** is pre-filled (3-link logic chain, what supports/weakens it, Malaysian channels). Add more theses by copying the block |
| `topics.yaml` | News search queries for sections 1, 2 and 3 |
| `projects.yaml` | Government/mega-projects tracked in section 4 |
| `calendar.yaml` | Scheduled events. **Review each quarter.** Unconfirmed dates are marked `approx: true` and shown with a `~` |
| `settings.yaml` | Indicator periods, thresholds, limits |

## What is verified vs. estimated

- **Verified from official sources (Nov 2026):** FOMC 2026-27 dates, BNM next MPC (21 Jan 2027), OPEC+ ministerial (29 Nov 2026),
  MSCI review dates to 2027, DOSM Q3 GDP release (18 Nov), FBM KLCI expansion to 50 stocks (phase 1 effective 21 Dec 2026).
- **Estimated (shown with `~`):** DOSM monthly CPI/trade/IPI days, FTSE Russell cut-off and announcement dates for the KLCI review,
  Budget 2027 committee-stage finish. BNM's remaining 2027 meeting dates are not yet in the file: add them when BNM publishes them.
- **Report dates** are estimated from the date the company reported the *same quarter last year*, capped at Bursa's 2-month deadline.
  They are estimates, not company announcements.

## Limitations (please read)

- **Scraped sources can break.** Bursa filings come via KLSE Screener's website, and prices via Yahoo Finance's unofficial feed. If a layout
  changes, that part degrades gracefully (and `python -m stockbot check` will show it) but needs a code fix. Keep usage personal and light.
- **Bursa's own site is not used** (it blocks automated access).
- **Insider values marked `(est.)`** use the current share price because the filing gave no price.
- **Technical signals lag** and fail often. They are prompts to research, never trade instructions.
- **AI can hallucinate or mis-rank.** Facts (prices, percentages, filing details) are parsed by code; the AI only explains them.
  Direction calls (Bull/Bear) are readings of those facts, not forecasts.
- **Push only:** the bot sends the daily digest; it does not answer chat commands (that needs an always-on server).

## Commands

```
python -m stockbot check              # verify keys + data sources
python -m stockbot chatid             # discover your Telegram chat id
python -m stockbot models             # list Gemini models your key can use
python -m stockbot calendar           # print the next 60 days of catalysts
python -m stockbot run --dry-run      # preview (also: --only technical,summary  --hours 72  --html)
python -m stockbot run                # send
```
