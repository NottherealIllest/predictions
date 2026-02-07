# Predictions Bot - AI Coding Instructions

## Project Overview
A Slack-integrated **Polymarket-style prediction betting bot** with play money. Users create markets (sports, events), trade outcome shares using an LMSR AMM, and compete monthly. Markets lock trading before events, balances top up daily, and monthly winners are determined by balance (with participation threshold).

## Architecture

### Core Components
- **Flask + PostgreSQL**: Single `app.py` monolith handling all routes, models, and business logic (~1000 lines)
- **Slack Integration**: Commands via `/predict` slash command with token verification
- **LMSR AMM Pricing**: Logarithmic Market Scoring Rule for continuous pricing (no order book)
- **Monthly Cycles**: Isolated user-cycle data for leaderboards and resets
- **Background Tasks**: Daily topups and monthly closures via scheduled HTTP endpoints

### Data Model
- **User**: Slack ID + name (immutable slack_id is unique key)
- **Cycle**: Monthly periods (e.g. "2026-02" with London tz boundaries)
- **UserCycle**: User's balance + bet_count + last_topup_date (monthly snapshot)
- **Market**: Single outcome per question with name (unique), event_time, close_time, LMSR liquidity param `b`
- **Outcome**: Belongs to market, tracks quantity `q` (used for LMSR pricing)
- **Position**: User shares held in outcome (one per user-market-outcome combo)
- **Trade**: Audit log of buy/sell with side, shares, amount

**Key Pattern**: No real-time UI state sync—Slack responses rebuild state from DB queries each command.

## Critical Developer Workflows

### Testing
```bash
pytest test_predictions.py  # Creates/destroys postgres:///predictionstest
# Or export TEST_DATABASE_URL=postgres://...
```
Tests use fixtures and `run()` helper to invoke commands with mock users and rollback after each test.

### Local Development
```bash
# virtualenv.sh sets FLASK_ENV=development, DATABASE_URL
source virtualenv.sh
python app.py  # Runs on :5000, auto-creates tables
```

### Deployment
```bash
./deploy.sh  # Automated Heroku setup (creates app, postgres addon, env vars)
# Or manual: heroku create, heroku addons:create heroku-postgresql:mini, heroku config:set
```
**Critical env vars**: `SLACK_TOKEN`, `DATABASE_URL`, optional `LMSR_B`, `STARTING_BALANCE`, `DAILY_TOPUP`, `BALANCE_CAP`, `ADMIN_SLACK_IDS`, `TASK_SECRET`

## Project-Specific Patterns

### Command Definition & Routing
- All user-facing commands use `@command` decorator (Python closures capture decorator scope)
- Commands registered in `commands` dict with function names as keys
- Router inspects function signature to validate argument count (line 956: `inspect.signature()`)
- Shorthand: `/predict <market> <outcome> <spend>` auto-routes to `buy` command
- **Error handling**: Raise `PredictionsError` for user-facing errors; logs + responds with error text

### LMSR AMM Implementation
- `lmsr_cost(qs, b)` computes cum cost for outcome quantities `qs` with liquidity `b`
- `lmsr_prices(qs, b)` returns implied probabilities (softmax of normalized exp)
- `buy_cost()` and `sell_refund()` use binary search to find shares for target spend (30 iterations for precision)
- Prices update immediately; no slippage limits (user absorbs impact)

### Time Handling
- All DB datetimes stored as **UTC-naive** (via `utc_naive()` helper)
- Event times parsed in **Europe/London** timezone (natural language + explicit format)
- Markets close = event_time - lock_delta (e.g., lock 15m before kick-off)
- Daily topup checks `last_topup_date` (naive date) vs `now(LONDON_TZ).date()` to enforce once-per-day

### Transaction Management
- Flask-SQLAlchemy auto-session per request
- Commands commit on success; `db.session.rollback()` on error
- Task endpoints (daily_topup, monthly_close) manually commit/rollback
- Health check uses `text('SELECT 1')` for pool testing

### Leaderboard & Competition Rules
- **Eligibility**: `bet_count > median(all_bet_counts)` that month
- **Winner**: Max balance among eligible players
- Month-end calls `close_month()` (admin-only); sets `cycle.median_bets`, `winner_slack_id`
- Monthly reset: New cycle created per `cycle_key_for_dt()` (YYYY-MM format in London tz)

## Integration Points & External Deps

### Slack
- Command text from `request.form.get('text')`, parsed by `shlex.split()` for quoted args
- Token verified via `os.environ['SLACK_TOKEN']`
- Responses as JSON `{'response_type': 'in_channel'|'ephemeral', 'text': ...}`
- User mentions: `<@slack_id>` format in response text

### PostgreSQL
- Connection pooling: `pool_pre_ping=True`, `pool_recycle=300s`, `max_overflow=0`
- Heroku uses `heroku-postgresql` addon (DATABASE_URL auto-set)
- Local: `postgresql:///predictionslocal` (create with createdb)

### Dependencies
- **Flask** 2.3, **SQLAlchemy** 3.0, **psycopg2**, **parsedatetime** (natural language dates), **pytz** (timezone math), **gunicorn** (production WSGI)

## Common Modification Points

### Add New Command
1. Define function with `@command` decorator: `def cmd(user, arg1, arg2):`
2. Get cycle + usercycle: `cycle = get_or_create_cycle(); uc = get_or_create_usercycle(cycle, user)`
3. Raise `PredictionsError` for user errors
4. Return string (posted to Slack)
5. Function name auto-registered in `commands` dict

### Extend Market Resolution
- Current: Winners get shares as balance credits (line 746: `uc.balance += p.shares`)
- Custom payouts: Modify loop in `resolve()` to adjust uc.balance differently

### Change Game Parameters
- `STARTING_BALANCE`, `DAILY_TOPUP`, `BALANCE_CAP`, `DEFAULT_LOCK`, `LMSR_B` are env-configurable
- Cycle boundaries: Modify `month_bounds_london()` to support weekly/daily cycles

### Fix Floating-Point Precision
- Buy cost uses epsilon: `if cost > uc.balance + 1e-6:` (line 534)
- LMSR overflow protection: max/exp normalization in `lmsr_prices()` prevents underflow
