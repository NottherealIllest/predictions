# Predictions (Slack Polymarket-Style Betting Bot)

A Polymarket-style prediction/betting bot for Slack. Create markets (e.g. match outcomes or team bets), trade outcome shares (buy/sell), and see live implied probabilities. Markets **expire/lock** before an event (e.g. “tomorrow 10am”), balances **top up daily** to keep play going, and a **monthly competition** crowns a winner among active participants.

> **Important:** This project is designed for **play money** (credits). Do not use this for real-money gambling without proper legal/compliance review.

---

## Features

- **Markets with expiry (trade lock):** Create a market with an event time and a lock window (e.g. lock 15 minutes before the event).
- **Multi-outcome markets:** Team A / Team B / Draw (or any set of outcomes).
- **Trading + live “probability” prices:** Uses an AMM-style model (LMSR) for continuous prices.
- **Balances + daily top-ups:** Everyone can keep playing; balances top up once per day (with a cap).
- **Monthly cycles + leaderboard:** Winner is the **highest balance among users with bet_count > median(bet_count)**.
- **Month-end reset:** New month = new cycle (fresh stats).

---

## Commands

### Help
```bash
/predict help

## List active markets
/predict list

## Create a market (with expiry lock before event)
/predict create <market-name> <question> <event-time> [lock] <outcomes_csv>

Examples

/predict create latecomer "Who is most likely to come late?" "tomorrow 10am" 15m "HABEEB,JOSH,TAYO"

/predict create ars_liv "Arsenal vs Liverpool" "2026-01-16 19:45" 15m "ARS,LIV,DRAW"

Notes

event-time supports natural language like "tomorrow 10am" and also "YYYY-MM-DD HH:MM".
Times are interpreted as Europe/London.
lock defaults to 10m if omitted (configurable).

## Show a market (prices + your position)
/predict show <market-name>

## Buy outcome shares (spend credits)
/predict buy <market-name> <outcome> <spend>

Example
/predict buy ars_liv ARS 50

## Shorthand buy
/predict <market-name> <outcome> <spend>

Example
/predict ars_liv ARS 50

## Sell outcome shares
/predict sell <market-name> <outcome> <shares>

Example 
/predict sell ars_liv ARS 10

## Check your balance + bets this month
/predict balance

## Leaderboard (current month)
/predict leaderboard

## Resolve a market (creator-only by default)
/predict resolve <market-name> <outcome>

Example
/predict resolve ars_liv ARS

## Cancel a market (creator-only)
/predict cancel <market-name>

## Close month (admin-only; declares winner)
/predict close_month

Monthly Winner Rules

At month-end:

Compute each user’s bet_count for the month.
Compute the median bet_count across all users in the cycle.
Eligible users are those with bet_count > median.
Winner is the eligible user with the highest balance.
A new month automatically starts a new cycle.
This prevents non-participants from winning by default.


Installation
Server Setup

You need:

A server Slack can reach (public HTTPS URL)

A Postgres database

Python version matching runtime.txt

Dependencies from requirements.txt

Heroku is the easiest deployment (web + Postgres managed for you). You can also run on a VPS.

Environment Variables

Required

SLACK_TOKEN — Slack verification token used to validate requests

Recommended

DATABASE_URL — Postgres connection string (defaults to postgres:///predictionslocal)

STARTING_BALANCE — initial credits per user (default 1000)

DAILY_TOPUP — credits added daily (default 200)

BALANCE_CAP — max balance after topups (default 2000)

DEFAULT_LOCK — default lock window before event (default 10m)

LMSR_B — AMM liquidity parameter (default 100)

ADMIN_SLACK_IDS — comma-separated Slack user IDs allowed to close month (optional)

TASK_SECRET — secret token to protect scheduled task endpoints (optional but recommended)

Slack Setup (Slash Command)

To enable /predict:

Visit https://api.slack.com/apps?new_app=1

Create an app (e.g. Predictions)

Go to Slash Commands → Create New Command

Command: /predict

Request URL: https://YOUR_SERVER/

Short Description: prediction market

Usage Hint: /predict help

Save the command.

Copy the app’s Verification Token and set it as SLACK_TOKEN on your server.

Install the app to your workspace.

Test in Slack:

`/predict help`

## Development
Local setup
`git clone <your-fork>
cd predictions
source virtualenv.sh
workon predictions
pip install -r requirements.txt
`

Run locally

Terminal 1
`createdb predictionslocal
SLACK_TOKEN=1 python app.py
`

Terminal 2:
`curl -d 'token=1&user_id=U_TEST&user_name=test&text=help' localhost:5000/`