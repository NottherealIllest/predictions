"""Slack Predictions Bot - single-file Flask app (Refactored)

This file replaces the original monolithic `app.py`. It preserves the same endpoints,
data models, and behavior while improving code clarity, structure, and robustness.

Core features:
- Flask + SQLAlchemy monolith
- Slack slash-command at `/` parsing (token-verified)
- LMSR AMM pricing (buy/sell shares in markets)
- Monthly cycle bookkeeping and daily top-ups
- Market creation, resolution, cancellation

Key fixes from original:
- LMSR cost calculation works correctly when all quantities are 0 (fixed 'max_q == 0' early return)
- Buy validation uses epsilon tolerance (1e-9) instead of strict 0 check
- Command routing extracts parameters at decorator time, not at request time
- Better error handling and logging throughout
"""

from __future__ import annotations

import os
import math
import json
import logging
import shlex
import datetime
from typing import List, Dict, Any

import parsedatetime
import pytz
from flask import Flask, request, Response
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================================================
# App initialization and config
# ============================================================================
app = Flask(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql:///predictionslocal")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,
    "pool_recycle": 300,
    "pool_timeout": 20,
    "max_overflow": 0,
}

db = SQLAlchemy(app)

# Game parameters (environment-overridable)
STARTING_BALANCE = float(os.environ.get("STARTING_BALANCE", "1000"))
DAILY_TOPUP = float(os.environ.get("DAILY_TOPUP", "200"))
BALANCE_CAP = float(os.environ.get("BALANCE_CAP", "2000"))
DEFAULT_LOCK = os.environ.get("DEFAULT_LOCK", "10m")
DEFAULT_LIQUIDITY_B = float(os.environ.get("LMSR_B", "100"))
TASK_SECRET = os.environ.get("TASK_SECRET", "")

# Timezones
LONDON_TZ = pytz.timezone("Europe/London")
UTC_TZ = pytz.utc


# ============================================================================
# Exceptions
# ============================================================================
class PredictionsError(Exception):
    """User-facing error for Slack responses."""
    pass


# ============================================================================
# Time utilities
# ============================================================================
def now() -> datetime.datetime:
    """Current UTC time (naive)."""
    return datetime.datetime.utcnow()


def utc_naive(dt_aware: datetime.datetime) -> datetime.datetime:
    """Convert timezone-aware datetime to naive UTC for DB storage."""
    return dt_aware.astimezone(UTC_TZ).replace(tzinfo=None)


def dt_to_string(dt: datetime.datetime) -> str:
    """Human-friendly relative time string (e.g. '2d from now', '1hr ago')."""
    dt_now = now()
    delta = abs(dt_now - dt)
    if delta.days:
        s = f"{int(delta.days)}d"
    elif delta.seconds > 3600:
        s = f"{int(delta.seconds / 3600)}hr"
    elif delta.seconds > 60:
        s = f"{int(delta.seconds / 60)}min"
    else:
        s = f"{int(delta.seconds)}s"
    return f"{s} ago" if dt_now > dt else f"{s} from now"


def parse_lock_delta(lock_str: str) -> datetime.timedelta:
    """Parse lock time like '15m', '2h', '1d' to timedelta."""
    lock_str = (lock_str or "").strip().lower()
    import re
    m = re.fullmatch(r"(\d+)\s*([mhd])", lock_str)
    if not m:
        raise PredictionsError(f'lock must look like 15m, 2h, or 1d (got "{lock_str}")')
    n = int(m.group(1))
    unit = m.group(2)
    if unit == "m":
        return datetime.timedelta(minutes=n)
    if unit == "h":
        return datetime.timedelta(hours=n)
    return datetime.timedelta(days=n)


def parse_natural_event_time(event_str: str) -> datetime.datetime:
    """Parse event time in London timezone from natural language or explicit format."""
    event_str = (event_str or "").strip()
    if not event_str:
        raise PredictionsError('missing event time (e.g. "tomorrow 10am" or "2026-01-13 10:00")')
    
    # Try explicit YYYY-MM-DD HH:MM format first
    try:
        dt = datetime.datetime.strptime(event_str, "%Y-%m-%d %H:%M")
        return LONDON_TZ.localize(dt)
    except ValueError:
        pass
    
    # Fall back to natural language parsing
    cal = parsedatetime.Calendar()
    base = datetime.datetime.now(LONDON_TZ)
    dt, status = cal.parseDT(event_str, tzinfo=LONDON_TZ, sourceTime=base)
    if status == 0:
        raise PredictionsError(f'Couldn\'t interpret "{event_str}" as a datetime')
    if dt.tzinfo is None:
        dt = LONDON_TZ.localize(dt)
    return dt


# ============================================================================
# LMSR AMM (Logarithmic Market Scoring Rule)
# ============================================================================
def lmsr_cost(qs: List[float], b: float) -> float:
    """
    LMSR cost function. Works even when all quantities are zero.
    
    The cost to move from state qs to qs' is:
      cost = b * (max(qs') / b + log(sum(exp((q / b)))))
    """
    if not qs or len(qs) == 0:
        return 0.0
    
    # Ensure all values are floats
    qs = [float(q) for q in qs]
    b = float(b)
    
    try:
        max_q = max(qs) if qs else 0.0
        m = max_q / b
        exps = [math.exp((q / b) - m) for q in qs]
        s = sum(exps)
        if s <= 0:
            return float("inf")
        return b * (m + math.log(s))
    except (OverflowError, ValueError, TypeError) as e:
        return float("inf")


def lmsr_prices(qs: List[float], b: float) -> List[float]:
    """
    LMSR implied probabilities (normalized exponentials).
    
    Returns a list of probabilities (0-1) for each outcome, summing to 1.
    """
    if not qs or len(qs) == 0:
        return []
    
    # Ensure all values are floats
    qs = [float(q) for q in qs]
    b = float(b)
    
    try:
        max_q = max(qs) if qs else 0.0
        m = max_q / b
        exps = [math.exp((q / b) - m) for q in qs]
        s = sum(exps)
        if s <= 0:
            return [1.0 / len(qs)] * len(qs)
        return [e / s for e in exps]
    except (OverflowError, ValueError, TypeError):
        return [1.0 / len(qs)] * len(qs)


def buy_cost(qs: List[float], b: float, idx: int, dq: float) -> float:
    """Cost to buy dq shares of outcome idx. Returns inf if invalid."""
    if dq <= 0 or b <= 0 or not qs or len(qs) == 0 or idx < 0 or idx >= len(qs):
        return float("inf")
    try:
        qs = [float(q) for q in qs]
        b = float(b)
        dq = float(dq)
        
        qs2 = list(qs)
        qs2[idx] += dq
        cost = lmsr_cost(qs2, b) - lmsr_cost(qs, b)
        if cost < 0 or math.isnan(cost) or math.isinf(cost):
            return float("inf")
        return cost
    except Exception as e:
        logger.error(f"buy_cost error: {e}")
        return float("inf")


def sell_refund(qs: List[float], b: float, idx: int, dq: float) -> float:
    """Refund from selling dq shares of outcome idx."""
    if dq <= 0 or qs[idx] < dq:
        return 0.0
    qs2 = list(qs)
    qs2[idx] -= dq
    refund = lmsr_cost(qs, b) - lmsr_cost(qs2, b)
    return max(0.0, refund)


# ============================================================================
# Database Models
# ============================================================================
class User(db.Model):
    """Slack user."""
    __tablename__ = "user"
    user_id = db.Column(db.Integer, primary_key=True)
    slack_id = db.Column(db.Text, unique=True, nullable=False)
    slack_name = db.Column(db.Text, nullable=True)


class Cycle(db.Model):
    """Monthly competition cycle."""
    __tablename__ = "cycle"
    cycle_id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.Text, unique=True, nullable=False)  # e.g. "2026-02"
    starts_at = db.Column(db.DateTime, nullable=False)
    ends_at = db.Column(db.DateTime, nullable=False)
    median_bets = db.Column(db.Integer, nullable=True)  # Eligibility threshold
    winner_slack_id = db.Column(db.Text, nullable=True)
    when_closed = db.Column(db.DateTime, nullable=True)


class UserCycle(db.Model):
    """User's balance and stats within a cycle."""
    __tablename__ = "user_cycle"
    user_cycle_id = db.Column(db.Integer, primary_key=True)
    cycle_id = db.Column(db.Integer, db.ForeignKey("cycle.cycle_id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.user_id"), nullable=False)
    balance = db.Column(db.Float, nullable=False, default=STARTING_BALANCE)
    bet_count = db.Column(db.Integer, nullable=False, default=0)
    last_topup_date = db.Column(db.Date, nullable=True)
    __table_args__ = (db.UniqueConstraint("cycle_id", "user_id", name="uniq_cycle_user"),)


class Market(db.Model):
    """A prediction market."""
    __tablename__ = "market"
    market_id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.Text, unique=True, nullable=False)
    question = db.Column(db.Text, nullable=False)
    creator_user_id = db.Column(db.Integer, db.ForeignKey("user.user_id"), nullable=False)
    when_closes = db.Column(db.DateTime, nullable=False)
    when_created = db.Column(db.DateTime, nullable=False, default=now)
    status = db.Column(db.Text, nullable=False, default="open")  # open, resolved, cancelled
    resolved_outcome_id = db.Column(db.Integer, nullable=True)
    when_resolved = db.Column(db.DateTime, nullable=True)
    when_cancelled = db.Column(db.DateTime, nullable=True)
    b = db.Column(db.Float, nullable=False, default=DEFAULT_LIQUIDITY_B)


class Outcome(db.Model):
    """An outcome within a market."""
    __tablename__ = "outcome"
    outcome_id = db.Column(db.Integer, primary_key=True)
    market_id = db.Column(db.Integer, db.ForeignKey("market.market_id"), nullable=False)
    symbol = db.Column(db.Text, nullable=False)
    q = db.Column(db.Float, nullable=False, default=0.0)
    __table_args__ = (db.UniqueConstraint("market_id", "symbol", name="uniq_market_symbol"),)


class Position(db.Model):
    """User's shares held in an outcome."""
    __tablename__ = "position"
    position_id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.user_id"), nullable=False)
    market_id = db.Column(db.Integer, db.ForeignKey("market.market_id"), nullable=False)
    outcome_id = db.Column(db.Integer, db.ForeignKey("outcome.outcome_id"), nullable=False)
    shares = db.Column(db.Float, nullable=False, default=0.0)
    __table_args__ = (db.UniqueConstraint("user_id", "market_id", "outcome_id", name="uniq_position"),)


class Trade(db.Model):
    """Audit log of all buy/sell transactions."""
    __tablename__ = "trade"
    trade_id = db.Column(db.Integer, primary_key=True)
    cycle_id = db.Column(db.Integer, db.ForeignKey("cycle.cycle_id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.user_id"), nullable=False)
    market_id = db.Column(db.Integer, db.ForeignKey("market.market_id"), nullable=False)
    outcome_id = db.Column(db.Integer, db.ForeignKey("outcome.outcome_id"), nullable=False)
    side = db.Column(db.Text, nullable=False)  # "buy" or "sell"
    shares = db.Column(db.Float, nullable=False)
    amount = db.Column(db.Float, nullable=False)
    when_created = db.Column(db.DateTime, nullable=False, default=now)


# ============================================================================
# Helpers
# ============================================================================
def cycle_key_for_dt(dt_london: datetime.datetime) -> str:
    """Return cycle key (YYYY-MM) for a given London time."""
    return f"{dt_london.year:04d}-{dt_london.month:02d}"


def month_bounds_london(year: int, month: int):
    """Return (start, end) datetimes for a month in London timezone."""
    start = LONDON_TZ.localize(datetime.datetime(year, month, 1, 0, 0, 0))
    if month == 12:
        end = LONDON_TZ.localize(datetime.datetime(year + 1, 1, 1, 0, 0, 0))
    else:
        end = LONDON_TZ.localize(datetime.datetime(year, month + 1, 1, 0, 0, 0))
    return start, end


def get_or_create_cycle() -> Cycle:
    """Get or create the current month's cycle."""
    now_london = datetime.datetime.now(LONDON_TZ)
    key = cycle_key_for_dt(now_london)
    cyc = Cycle.query.filter(Cycle.key == key).first()
    if cyc:
        return cyc
    start_london, end_london = month_bounds_london(now_london.year, now_london.month)
    cyc = Cycle(key=key, starts_at=utc_naive(start_london), ends_at=utc_naive(end_london))
    db.session.add(cyc)
    db.session.flush()
    return cyc


def get_or_create_user(slack_user_id: str, slack_user_name: str | None = None) -> User:
    """Get or create a user by Slack ID."""
    u = User.query.filter(User.slack_id == slack_user_id).first()
    if u:
        if slack_user_name and u.slack_name != slack_user_name:
            u.slack_name = slack_user_name
        return u
    u = User(slack_id=slack_user_id, slack_name=slack_user_name)
    db.session.add(u)
    db.session.flush()
    return u


def get_or_create_usercycle(cycle: Cycle, user: User) -> UserCycle:
    """Get or create a user's entry for a cycle."""
    uc = UserCycle.query.filter(
        UserCycle.cycle_id == cycle.cycle_id, UserCycle.user_id == user.user_id
    ).first()
    if uc:
        return uc
    uc = UserCycle(cycle_id=cycle.cycle_id, user_id=user.user_id, balance=STARTING_BALANCE, bet_count=0)
    db.session.add(uc)
    db.session.flush()
    return uc


def ensure_daily_topup_for_usercycle(uc: UserCycle):
    """Apply daily top-up if user hasn't been topped up today."""
    today = datetime.datetime.now(LONDON_TZ).date()
    if uc.last_topup_date == today:
        return
    uc.balance = min(BALANCE_CAP, uc.balance + DAILY_TOPUP)
    uc.last_topup_date = today


def get_market_or_raise(market_name: str) -> Market:
    """Fetch market by name or raise PredictionsError."""
    m = Market.query.filter(Market.name == market_name).first()
    if not m:
        raise PredictionsError(f"unknown market {market_name}")
    return m


def get_outcomes(market: Market) -> List[Outcome]:
    """Fetch all outcomes for a market, ordered by ID."""
    return Outcome.query.filter(Outcome.market_id == market.market_id).order_by(Outcome.outcome_id).all()


def market_is_closed(market: Market) -> bool:
    """Check if market is closed (trading locked)."""
    if market.status != "open":
        return True
    if market.when_cancelled is not None:
        return True
    if market.when_closes < now():
        return True
    return False


# ============================================================================
# Command registry and decorator
# ============================================================================
commands: Dict[str, Dict[str, Any]] = {}


def command(fn):
    """
    Decorator to register a command and extract its parameter names.
    
    Stores function + parameter list for safe routing without introspection at request time.
    Detects VAR_POSITIONAL (*rest) to allow variable argument counts.
    """
    import inspect
    sig = inspect.signature(fn)
    # Extract positional parameters (skip 'user')
    params = [
        p.name
        for p in sig.parameters.values()
        if p.name != "user"
        and p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    # Check if function accepts *args (VAR_POSITIONAL)
    has_varargs = any(
        p.kind == inspect.Parameter.VAR_POSITIONAL
        for p in sig.parameters.values()
    )
    commands[fn.__name__] = {"fn": fn, "params": params, "has_varargs": has_varargs}
    return fn


# ============================================================================
# Commands
# ============================================================================
@command
def help(user: User) -> str:
    """Show help text."""
    return f"""/predict list
/predict show <market-name>
/predict create <market-name> <question> <event-time> [lock] <outcomes_csv>
  - event-time examples: "tomorrow 10am", "2026-01-13 10:00"
  - lock examples: 15m, 1h (defaults to {DEFAULT_LOCK})
  - outcomes_csv examples: "ARS,LIV,DRAW" or "TEAM_A,TEAM_B"
/predict buy <market-name> <outcome> <spend>
/predict sell <market-name> <outcome> <shares>
/predict balance
/predict leaderboard
/predict resolve <market-name> <outcome>
/predict cancel <market-name>
"""


@command
def list(user: User) -> str:
    """List active markets (open, not cancelled, not closed)."""
    current_time = now()
    markets = (
        Market.query.filter(
            Market.status == "open",
            Market.when_cancelled == None,
            Market.when_closes > current_time,
        )
        .order_by(Market.when_created.desc())
        .all()
    )
    if not markets:
        return "no active markets"
    return "\n".join(m.name for m in markets)


@command
def show(user: User, market_name: str) -> str:
    """Show market details, prices, and user's position."""
    m = get_market_or_raise(market_name)
    outcomes = get_outcomes(m)
    qs = [o.q for o in outcomes]
    prices = lmsr_prices(qs, m.b)
    
    if m.status == "cancelled":
        status = "Cancelled"
    elif m.status == "resolved":
        status = f"Resolved"
    else:
        status = "Open"
    
    board = "\n".join(f"{o.symbol}: {p * 100:.2f}%" for o, p in zip(outcomes, prices))
    
    # User's position summary
    pos_lines = []
    for o in outcomes:
        pos = Position.query.filter(
            Position.user_id == user.user_id,
            Position.market_id == m.market_id,
            Position.outcome_id == o.outcome_id,
        ).first()
        if pos and pos.shares > 0:
            pos_lines.append(f"{o.symbol} shares: {pos.shares:.2f}")
    
    pos_text = "\n\nYour position:\n" + "\n".join(pos_lines) if pos_lines else ""
    close_line = f"Closes {dt_to_string(m.when_closes)} ({m.when_closes} UTC)\n"
    return f"{m.question}\nStatus: {status}\n{close_line}{board}{pos_text}"


@command
def create(user: User, market_name: str, question: str, event_time: str, *rest) -> str:
    """Create a new market."""
    if Market.query.filter(Market.name == market_name).first():
        raise PredictionsError(f"A market named {market_name} already exists")
    
    if len(rest) == 1:
        lock = DEFAULT_LOCK
        outcomes_csv = rest[0]
    elif len(rest) == 2:
        lock = rest[0]
        outcomes_csv = rest[1]
    else:
        raise PredictionsError("usage is create <market-name> <question> <event-time> [lock] <outcomes_csv>")
    
    event_dt_london = parse_natural_event_time(event_time)
    close_dt_london = event_dt_london - parse_lock_delta(lock)
    when_closes = utc_naive(close_dt_london)
    
    m = Market(
        name=market_name,
        question=question,
        creator_user_id=user.user_id,
        when_closes=when_closes,
        status="open",
        b=DEFAULT_LIQUIDITY_B,
    )
    db.session.add(m)
    db.session.flush()
    
    symbols = [s.strip() for s in outcomes_csv.split(",") if s.strip()]
    if len(symbols) < 2:
        raise PredictionsError('need at least 2 outcomes (e.g. "TEAM_A,TEAM_B" or "A,B,DRAW")')
    
    for sym in symbols:
        db.session.add(Outcome(market_id=m.market_id, symbol=sym, q=0.0))
    
    return f"Created market {market_name}. Trading locks {dt_to_string(when_closes)} ({when_closes} UTC)"


@command
def buy(user: User, market_name: str, outcome_symbol: str, spend: str) -> str:
    """Buy shares of an outcome."""
    logger.info(f"BUY: start user={user.user_id}, market={market_name}, outcome={outcome_symbol}, spend={spend}")
    
    # Validate input
    try:
        spend_f = float(spend)
    except ValueError:
        raise PredictionsError(f'Invalid amount: {spend}')
    
    if spend_f <= 0:
        raise PredictionsError("Spend must be > 0")
    
    # Get market
    m = get_market_or_raise(market_name)
    if market_is_closed(m):
        raise PredictionsError(f"Market {market_name} is closed")
    
    # Get user account
    cycle = get_or_create_cycle()
    uc = get_or_create_usercycle(cycle, user)
    ensure_daily_topup_for_usercycle(uc)
    
    if uc.balance < spend_f:
        raise PredictionsError(f"Insufficient balance: {uc.balance:.2f} < {spend_f:.2f}")
    
    # Get outcomes
    outcomes = get_outcomes(m)
    if len(outcomes) < 2:
        raise PredictionsError("Market misconfigured")
    
    # Find outcome
    target_idx = None
    for i, o in enumerate(outcomes):
        if o.symbol.upper() == outcome_symbol.upper():
            target_idx = i
            break
    
    if target_idx is None:
        raise PredictionsError(f"Unknown outcome: {outcome_symbol}")
    
    target_outcome = outcomes[target_idx]
    
    # Get LMSR params
    qs = [o.q for o in outcomes]
    b = m.b
    
    if b <= 0:
        raise PredictionsError("Invalid market params")
    
    logger.info(f"BUY: market state qs={qs}, b={b}, spend={spend_f}")
    
    # Binary search for dq
    low, high = 0.0, 10000.0
    best_dq = 0.0
    best_cost = 0.0
    
    for iteration in range(50):
        mid = (low + high) / 2.0
        cost = buy_cost(qs, b, target_idx, mid)
        
        logger.info(f"BUY: iter={iteration}, low={low}, mid={mid}, high={high}, cost={cost}")
        
        if math.isinf(cost):
            high = mid
            continue
        
        if cost > spend_f:
            high = mid
        else:
            low = mid
            best_dq = mid
            best_cost = cost
    
    dq = best_dq
    cost = best_cost
    
    logger.info(f"BUY: final dq={dq}, cost={cost}")
    
    # Validate
    if dq < 1e-8 or math.isinf(cost) or cost < 0 or cost > spend_f * 1.01:
        logger.error(f"BUY: validation failed dq={dq}, cost={cost}, spend={spend_f}")
        raise PredictionsError(f"Trade calculation failed (dq={dq}, cost={cost})")
    
    if cost > uc.balance + 1e-6:
        raise PredictionsError(f"Insufficient balance: {uc.balance:.2f} < {cost:.2f}")
    
    # Execute
    uc.balance -= cost
    uc.bet_count += 1
    target_outcome.q += dq
    
    # Position
    pos = Position.query.filter(
        Position.user_id == user.user_id,
        Position.market_id == m.market_id,
        Position.outcome_id == target_outcome.outcome_id,
    ).first()
    
    if not pos:
        pos = Position(
            user_id=user.user_id,
            market_id=m.market_id,
            outcome_id=target_outcome.outcome_id,
            shares=0.0,
        )
        db.session.add(pos)
    
    pos.shares += dq
    
    # Trade log
    db.session.add(
        Trade(
            cycle_id=cycle.cycle_id,
            user_id=user.user_id,
            market_id=m.market_id,
            outcome_id=target_outcome.outcome_id,
            side="buy",
            shares=dq,
            amount=cost,
        )
    )
    
    # Prices
    updated_qs = [o.q for o in outcomes]
    prices = lmsr_prices(updated_qs, b)
    new_price = prices[target_idx] * 100
    
    logger.info(f"BUY: success dq={dq}, cost={cost}, new_price={new_price}%")
    
    return f"✅ Bought {dq:.2f} of {outcome_symbol} @ {new_price:.1f}% | Cost: {cost:.2f} | Balance: {uc.balance:.2f}"


@command
def sell(user: User, market_name: str, outcome_symbol: str, shares: str) -> str:
    """Sell shares of an outcome."""
    try:
        shares_f = float(shares)
    except ValueError:
        raise PredictionsError(f"{shares} is not a valid float")
    
    if shares_f <= 0:
        raise PredictionsError("shares must be > 0")
    
    m = get_market_or_raise(market_name)
    if market_is_closed(m):
        raise PredictionsError(f"market {market_name} is closed")
    
    cycle = get_or_create_cycle()
    uc = get_or_create_usercycle(cycle, user)
    ensure_daily_topup_for_usercycle(uc)
    
    outcomes = get_outcomes(m)
    if not outcomes:
        raise PredictionsError("market has no outcomes")
    
    # Find target outcome
    target_outcome = None
    idx = None
    for i, o in enumerate(outcomes):
        if o.symbol.upper() == outcome_symbol.upper():
            target_outcome = o
            idx = i
            break
    
    if target_outcome is None:
        available = ", ".join(o.symbol for o in outcomes)
        raise PredictionsError(f"unknown outcome {outcome_symbol}. Available: {available}")
    
    # Check user has enough shares
    pos = Position.query.filter(
        Position.user_id == user.user_id,
        Position.market_id == m.market_id,
        Position.outcome_id == target_outcome.outcome_id,
    ).first()
    if not pos or pos.shares < shares_f:
        raise PredictionsError(f"not enough shares to sell (you have {pos.shares if pos else 0.0:.2f})")
    
    qs = [o.q for o in outcomes]
    b = m.b
    
    if target_outcome.q < shares_f:
        raise PredictionsError("market has insufficient liquidity to sell that many shares")
    
    refund = sell_refund(qs, b, idx, shares_f)
    if refund < 0:
        raise PredictionsError("trade failed (invalid refund)")
    
    # Execute trade
    pos.shares -= shares_f
    target_outcome.q -= shares_f
    uc.balance += refund
    uc.bet_count += 1
    
    # Audit log
    db.session.add(
        Trade(
            cycle_id=cycle.cycle_id,
            user_id=user.user_id,
            market_id=m.market_id,
            outcome_id=target_outcome.outcome_id,
            side="sell",
            shares=shares_f,
            amount=refund,
        )
    )
    
    # Updated prices
    new_qs = [o.q for o in outcomes]
    prices = lmsr_prices(new_qs, b)
    
    return f"✅ Sold {shares_f:.2f} shares of {outcome_symbol} in {market_name} | Refund {refund:.2f} | Price now {prices[idx]*100:.2f}% | Balance {uc.balance:.2f} | Bets {uc.bet_count}"


@command
def balance(user: User) -> str:
    """Show user's balance and bet count for current cycle."""
    cycle = get_or_create_cycle()
    uc = get_or_create_usercycle(cycle, user)
    ensure_daily_topup_for_usercycle(uc)
    return f"💰 Balance: {uc.balance:.2f} | Bets this month: {uc.bet_count} | Cycle: {cycle.key}"


@command
def leaderboard(user: User) -> str:
    """Show top 10 players by balance and eligibility status."""
    cycle = get_or_create_cycle()
    rows = UserCycle.query.filter(UserCycle.cycle_id == cycle.cycle_id).all()
    if not rows:
        return "No leaderboard yet (no one has interacted this cycle)."
    
    from statistics import median
    bet_counts = [r.bet_count for r in rows]
    med = int(median(sorted(bet_counts))) if bet_counts else 0
    
    top = sorted(rows, key=lambda r: r.balance, reverse=True)[:10]
    lines = [f"🏆 Leaderboard ({cycle.key})", f"Median bets: {med} (eligible if bets > {med})", ""]
    for i, r in enumerate(top, 1):
        u = db.session.get(User, r.user_id)
        eligible = "✅" if r.bet_count > med else "—"
        lines.append(f"{i}. <@{u.slack_id}>  {r.balance:.2f}  (bets {r.bet_count}) {eligible}")
    
    return "\n".join(lines)


@command
def resolve(user: User, market_name: str, winning_outcome: str) -> str:
    """Resolve a market to an outcome (creator-only)."""
    m = get_market_or_raise(market_name)
    
    if m.status == "resolved":
        raise PredictionsError(f"market {market_name} is already resolved")
    if m.when_cancelled is not None:
        raise PredictionsError(f"market {market_name} was cancelled")
    
    if m.creator_user_id != user.user_id:
        creator = db.session.get(User, m.creator_user_id)
        raise PredictionsError(
            f"Only {creator.slack_id if creator else 'creator'} can resolve {market_name}"
        )
    
    out = Outcome.query.filter(
        Outcome.market_id == m.market_id, Outcome.symbol == winning_outcome
    ).first()
    if not out:
        raise PredictionsError(f"unknown outcome {winning_outcome}")
    
    cycle = get_or_create_cycle()
    
    # Award shares to winners
    positions = Position.query.filter(
        Position.market_id == m.market_id, Position.outcome_id == out.outcome_id
    ).all()
    for p in positions:
        uc = UserCycle.query.filter(
            UserCycle.cycle_id == cycle.cycle_id, UserCycle.user_id == p.user_id
        ).first()
        if not uc:
            u = db.session.get(User, p.user_id)
            uc = get_or_create_usercycle(cycle, u)
        ensure_daily_topup_for_usercycle(uc)
        uc.balance += p.shares
    
    m.status = "resolved"
    m.resolved_outcome_id = out.outcome_id
    m.when_resolved = now()
    
    return f"🏁 Market {market_name} resolved: {winning_outcome}"


@command
def cancel(user: User, market_name: str) -> str:
    """Cancel a market (creator-only)."""
    m = get_market_or_raise(market_name)
    
    if m.when_cancelled is not None:
        raise PredictionsError(f"market {market_name} was already cancelled")
    
    if m.creator_user_id != user.user_id:
        creator = db.session.get(User, m.creator_user_id)
        raise PredictionsError(
            f"Only {creator.slack_id if creator else 'creator'} can cancel {market_name}"
        )
    
    m.when_cancelled = now()
    m.status = "cancelled"
    return f"Market {market_name} cancelled"


@command
def close_month(user: User) -> str:
    """Close current month, award winner (admin-only)."""
    admins = [x.strip() for x in os.environ.get("ADMIN_SLACK_IDS", "").split(",") if x.strip()]
    if admins and user.slack_id not in admins:
        raise PredictionsError("Only admins can close the month")
    
    cycle = get_or_create_cycle()
    if cycle.when_closed is not None:
        raise PredictionsError(f"cycle {cycle.key} already closed")
    
    rows = UserCycle.query.filter(UserCycle.cycle_id == cycle.cycle_id).all()
    if not rows:
        cycle.when_closed = now()
        cycle.median_bets = 0
        cycle.winner_slack_id = None
        return f"🏁 Closed {cycle.key} (no participants)"
    
    from statistics import median
    bet_counts = [r.bet_count for r in rows]
    med_val = int(median(sorted(bet_counts))) if bet_counts else 0
    
    eligible = [r for r in rows if r.bet_count > med_val]
    winner = max(eligible, key=lambda r: r.balance, default=None)
    
    cycle.median_bets = med_val
    cycle.when_closed = now()
    if winner:
        u = db.session.get(User, winner.user_id)
        cycle.winner_slack_id = u.slack_id
    else:
        cycle.winner_slack_id = None
    
    top = sorted(rows, key=lambda r: r.balance, reverse=True)[:10]
    lines = [
        f"🏁 *Month closed:* {cycle.key}",
        f"Median bets: *{med_val}* (eligible if bets > {med_val})",
    ]
    
    if winner:
        u = db.session.get(User, winner.user_id)
        lines.append(f"🏆 Winner: <@{u.slack_id}> — {winner.balance:.2f} (bets {winner.bet_count})")
    else:
        lines.append("🏆 Winner: No eligible winner (not enough participation).")
    
    lines.append("\nTop balances:")
    for i, r in enumerate(top, 1):
        u = db.session.get(User, r.user_id)
        eligible_mark = "✅" if r.bet_count > med_val else "—"
        lines.append(f"{i}. <@{u.slack_id}>  {r.balance:.2f}  (bets {r.bet_count}) {eligible_mark}")
    
    return "\n".join(lines)


# ============================================================================
# Task endpoints (for daily top-up and month-end automation)
# ============================================================================
@app.route("/tasks/daily_topup", methods=["GET"])
def task_daily_topup():
    """Endpoint for daily top-up. Secured by TASK_SECRET."""
    if TASK_SECRET and request.args.get("secret") != TASK_SECRET:
        return Response("forbidden", status=403)
    
    try:
        cycle = get_or_create_cycle()
        today = datetime.datetime.now(LONDON_TZ).date()
        rows = UserCycle.query.filter(UserCycle.cycle_id == cycle.cycle_id).all()
        count = 0
        for uc in rows:
            if uc.last_topup_date != today:
                uc.balance = min(BALANCE_CAP, uc.balance + DAILY_TOPUP)
                uc.last_topup_date = today
                count += 1
        db.session.commit()
        return f"topped up {count} users for {cycle.key}"
    except Exception as e:
        db.session.rollback()
        logger.exception("Error in daily topup")
        return f"Error: {str(e)}", 500


@app.route("/tasks/monthly_close", methods=["GET"])
def task_monthly_close():
    """Endpoint for month-end closure. Secured by TASK_SECRET."""
    if TASK_SECRET and request.args.get("secret") != TASK_SECRET:
        return Response("forbidden", status=403)
    
    try:
        sys_user = User.query.filter(User.slack_id == "SYSTEM").first()
        if not sys_user:
            sys_user = User(slack_id="SYSTEM", slack_name="system")
            db.session.add(sys_user)
            db.session.flush()
        msg = close_month(sys_user)
        db.session.commit()
        return msg
    except Exception as e:
        db.session.rollback()
        logger.exception("Error in monthly close")
        return f"Error: {str(e)}", 500


# ============================================================================
# Main Slack request handler
# ============================================================================
@app.route("/", methods=["POST"])
def handle_request():
    """
    Main Slack slash-command handler.
    
    Parses Slack request, routes to command function, and returns JSON response.
    """
    try:
        # Verify Slack token
        if request.form.get("token") != os.environ.get("SLACK_TOKEN"):
            return Response(
                json.dumps({"response_type": "ephemeral", "text": "Invalid token"}),
                mimetype="application/json",
                status=401,
            )
        
        # Parse args
        text = request.form.get("text", "").strip()
        args = shlex.split(text) if text else ["help"]
        slack_user_id = request.form.get("user_id")
        slack_user_name = request.form.get("user_name")
        
        logger.info(f"Command received: text='{text}', args={args}, user={slack_user_id}")
        
        if not slack_user_id:
            return Response(
                json.dumps({"response_type": "ephemeral", "text": "Missing user information"}),
                mimetype="application/json",
                status=400,
            )
        
        # Get or create user
        user = get_or_create_user(slack_user_id, slack_user_name)
        
        # Route command
        command_str = "help"
        if args and args[0] in commands:
            command_str = args[0]
            args = args[1:]
        elif len(args) >= 3:
            # Shorthand: /predict <market> <outcome> <spend>
            command_str = "buy"
        
        logger.info(f"Executing command: {command_str} with args: {args}")
        
        # Get command entry
        entry = commands.get(command_str)
        if not entry:
            raise PredictionsError("unknown command")
        
        selected_command = entry["fn"]
        expected_params = entry.get("params", [])
        has_varargs = entry.get("has_varargs", False)
        
        logger.info(f"Expected args: {expected_params}, has_varargs: {has_varargs}, received args: {args}")
        
        # Validate argument count
        if len(args) < len(expected_params):
            if expected_params:
                usage_str = f"usage is {command_str} {' '.join(f'<{p}>' for p in expected_params)}"
            else:
                usage_str = f"usage is {command_str}"
            raise PredictionsError(usage_str)
        
        # Truncate extra args only if function doesn't accept *args
        if not has_varargs:
            args = args[: len(expected_params)]
        
        # Execute
        response = selected_command(user, *args)
        db.session.commit()
        
        return Response(
            json.dumps({"response_type": "in_channel", "text": response}),
            mimetype="application/json",
        )
    
    except PredictionsError as e:
        db.session.rollback()
        logger.info(f"Predictions error: {e}")
        return Response(
            json.dumps({"response_type": "ephemeral", "text": f"Error: {str(e)}"}),
            mimetype="application/json",
        )
    except Exception as e:
        db.session.rollback()
        logger.exception("Error in handle_request")
        return Response(
            json.dumps({"response_type": "ephemeral", "text": "Internal error occurred"}),
            mimetype="application/json",
            status=500,
        )


# ============================================================================
# Health check
# ============================================================================
@app.route("/health", methods=["GET"])
def health_check():
    """Health check endpoint."""
    try:
        db.session.execute(text("SELECT 1"))
        return "OK"
    except Exception as e:
        logger.exception("Health check failed")
        return f"Error: {str(e)}", 500


# ============================================================================
# Main
# ============================================================================
if __name__ == "__main__":
    with app.app_context():
        try:
            db.create_all()
            logger.info("Database initialized successfully")
        except Exception as e:
            logger.exception("Database initialization failed")
    
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
