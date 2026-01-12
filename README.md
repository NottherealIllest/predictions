# Predictions (Slack Polymarket-Style Betting Bot)

A Polymarket-style prediction/betting bot for Slack. Create markets (e.g. match outcomes or team bets), trade outcome shares (buy/sell), and see live implied probabilities. Markets **expire/lock** before an event (e.g. "tomorrow 10am"), balances **top up daily** to keep play going, and a **monthly competition** crowns a winner among active participants.

> **Important:** This project is designed for **play money** (credits). Do not use this for real-money gambling without proper legal/compliance review.

## ✨ Features

- **Markets with expiry (trade lock):** Create a market with an event time and a lock window (e.g. lock 15 minutes before the event).
- **Multi-outcome markets:** Team A / Team B / Draw (or any set of outcomes).
- **Trading + live "probability" prices:** Uses an AMM-style model (LMSR) for continuous prices.
- **Balances + daily top-ups:** Everyone can keep playing; balances top up once per day (with a cap).
- **Monthly cycles + leaderboard:** Winner is the **highest balance among users with bet_count > median(bet_count)**.
- **Month-end reset:** New month = new cycle (fresh stats).

## 🚀 Quick Start

### Option 1: Automated Deploy (Recommended)

```bash
git clone <this-repo>
cd predictions
chmod +x deploy.sh
./deploy.sh
```

Then follow the Slack setup instructions that appear.

### Option 2: Manual Setup

See [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md) for detailed step-by-step instructions.

## 📱 Commands

### Help
```bash
/predict help
```

### List active markets
```bash
/predict list
```

### Create a market (with expiry lock before event)
```bash
/predict create <market-name> <question> <event-time> [lock] <outcomes_csv>
```

**Examples:**
```bash
/predict create latecomer "Who is most likely to come late?" "tomorrow 10am" 15m "HABEEB,JOSH,TAYO"

/predict create ars_liv "Arsenal vs Liverpool" "2026-01-16 19:45" 15m "ARS,LIV,DRAW"
```

**Notes:**
- `event-time` supports natural language like "tomorrow 10am" and also "YYYY-MM-DD HH:MM"
- Times are interpreted as Europe/London timezone
- `lock` defaults to 10m if omitted (configurable)

### Show a market (prices + your position)
```bash
/predict show <market-name>
```

### Buy outcome shares (spend credits)
```bash
/predict buy <market-name> <outcome> <spend>

# Example
/predict buy ars_liv ARS 50
```

### Shorthand buy
```bash
/predict <market-name> <outcome> <spend>

# Example
/predict ars_liv ARS 50
```

### Sell outcome shares
```bash
/predict sell <market-name> <outcome> <shares>

# Example 
/predict sell ars_liv ARS 10
```

### Check your balance + bets this month
```bash
/predict balance
```

### Leaderboard (current month)
```bash
/predict leaderboard
```

### Resolve a market (creator-only by default)
```bash
/predict resolve <market-name> <outcome>

# Example
/predict resolve ars_liv ARS
```

### Cancel a market (creator-only)
```bash
/predict cancel <market-name>
```

### Close month (admin-only; declares winner)
```bash
/predict close_month
```

## 🏆 Monthly Winner Rules

At month-end:

1. Compute each user's bet_count for the month
2. Compute the median bet_count across all users in the cycle  
3. Eligible users are those with bet_count > median
4. Winner is the eligible user with the highest balance
5. A new month automatically starts a new cycle

This prevents non-participants from winning by default.

## ⚙️ Configuration

All configuration is done via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `SLACK_TOKEN` | *required* | Slack verification token |
| `DATABASE_URL` | auto-set | PostgreSQL connection string |
| `STARTING_BALANCE` | 1000 | Initial credits per user |
| `DAILY_TOPUP` | 200 | Credits added daily |
| `BALANCE_CAP` | 2000 | Max balance after topups |
| `DEFAULT_LOCK` | 10m | Default lock window before event |
| `LMSR_B` | 100 | AMM liquidity parameter |
| `ADMIN_SLACK_IDS` | none | Comma-separated Slack user IDs for admin commands |
| `TASK_SECRET` | none | Secret token for scheduled task endpoints |

## 🛠️ Development

### Local Setup

```bash
git clone <this-repo>
cd predictions
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### Run Locally

```bash
# Terminal 1: Setup database
createdb predictionslocal
export SLACK_TOKEN=test-token
python app.py
```

```bash
# Terminal 2: Test
curl -d 'token=test-token&user_id=U_TEST&user_name=test&text=help' localhost:5000/
```

### Run Tests

```bash
pytest
```

## 🏗️ Architecture

- **Backend**: Flask + PostgreSQL with SQLAlchemy ORM
- **Market Maker**: LMSR (Logarithmic Market Scoring Rule) for automated pricing
- **Deployment**: Designed for Heroku with Gunicorn
- **Scheduling**: Optional Heroku Scheduler for daily/monthly tasks

## 🔒 Security Notes

- Uses Slack verification tokens for request authentication
- Optional TASK_SECRET for scheduled endpoint protection
- All trading is with play money only
- No real financial transactions

## 📈 Hosting Costs

**Free Option:**
- Heroku free tier (app sleeps after 30min inactivity)
- PostgreSQL free tier (10,000 rows)

**Paid Option (~$16/month):**
- Heroku Hobby dyno: $7/month (always-on)
- PostgreSQL Essential: $9/month (10M rows)

## 🐛 Troubleshooting

**Bot not responding?**
- Check logs: `heroku logs --tail`
- Verify `SLACK_TOKEN` is set correctly
- Ensure Slack app Request URL matches your Heroku URL

**Database errors?**
- Run: `heroku run python -c "from app import app, db; app.app_context().push(); db.create_all()"`

See [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md) for more troubleshooting tips.

## 📄 License

MIT License - see [LICENSE](LICENSE) file for details.

## 🤝 Contributing

1. Fork the project
2. Create a feature branch
3. Make your changes  
4. Add tests if applicable
5. Submit a pull request

---

Happy predicting! 🎯
