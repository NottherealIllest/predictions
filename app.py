import os
import math
import json
import pytz
import shlex
import inspect
import datetime
import parsedatetime
import re
import logging
from statistics import median
from collections import defaultdict
from flask import Flask, request, Response
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Database configuration with connection pooling and better error handling
database_url = os.environ.get('DATABASE_URL', 'postgresql:///predictionslocal')
if database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,
    'pool_recycle': 300,
    'pool_timeout': 20,
    'max_overflow': 0
}

# Initialize the database
db = SQLAlchemy(app)

# -----------------------------
# Time / timezone helpers
# -----------------------------
def now():
    return datetime.datetime.utcnow()

LONDON_TZ = pytz.timezone('Europe/London')
UTC_TZ = pytz.utc

def utc_naive(dt_aware):
    """Convert aware dt -> naive UTC for DB storage."""
    return dt_aware.astimezone(UTC_TZ).replace(tzinfo=None)

def dt_to_string(dt):
    dt_now = now()
    delta = abs(dt_now - dt)

    if delta.days:
        s = '%sd' % int(delta.days)
    elif delta.seconds > 60 * 60:
        s = '%shr' % int(delta.seconds / 60 / 60)
    elif delta.seconds > 60:
        s = '%smin' % int(delta.seconds / 60)
    else:
        s = '%ss' % int(delta.seconds)

    if dt_now > dt:
        return '%s ago' % s
    else:
        return '%s from now' % s

def parse_lock_delta(lock_str):
    """Parse "15m", "2h", "1d" to timedelta."""
    lock_str = (lock_str or "").strip().lower()
    m = re.fullmatch(r"(\d+)\s*([mhd])", lock_str)
    if not m:
        raise PredictionsError('lock must look like 15m, 2h, or 1d (got "%s")' % lock_str)
    n = int(m.group(1))
    unit = m.group(2)
    if unit == 'm':
        return datetime.timedelta(minutes=n)
    if unit == 'h':
        return datetime.timedelta(hours=n)
    return datetime.timedelta(days=n)

def parse_natural_event_time(event_str):
    """Parse natural language like "tomorrow 10am" in Europe/London."""
    event_str = (event_str or "").strip()
    if not event_str:
        raise PredictionsError('missing event time (e.g. "tomorrow 10am" or "2026-01-13 10:00")')

    # Try explicit first
    try:
        dt = datetime.datetime.strptime(event_str, "%Y-%m-%d %H:%M")
        aware = LONDON_TZ.localize(dt)
        return aware
    except ValueError:
        pass

    cal = parsedatetime.Calendar()
    base = datetime.datetime.now(LONDON_TZ)
    dt, status = cal.parseDT(event_str, tzinfo=LONDON_TZ, sourceTime=base)
    if status == 0:
        raise PredictionsError('Couldn\'t interpret "%s" as a datetime' % event_str)
    if dt.tzinfo is None:
        dt = LONDON_TZ.localize(dt)
    return dt

# -----------------------------
# Polymarket-like LMSR AMM
# -----------------------------
def lmsr_cost(qs, b):
    if not qs:
        return 0.0
    try:
        max_q = max(qs)
        m = max_q / b
        return b * (m + math.log(sum(math.exp((q / b) - m) for q in qs)))
    except (OverflowError, ValueError):
        return float('inf')

def lmsr_prices(qs, b):
    if not qs or all(q == 0 for q in qs):
        return [1.0 / len(qs) if qs else 1.0] * len(qs)
    
    try:
        max_q = max(qs)
        m = max_q / b
        exps = [math.exp((q / b) - m) for q in qs]
        s = sum(exps)
        if s == 0:
            return [1.0 / len(qs)] * len(qs)
        return [e / s for e in exps]
    except (OverflowError, ValueError):
        return [1.0 / len(qs)] * len(qs)

def buy_cost(qs, b, idx, dq):
    if dq <= 0:
        return 0
    qs2 = list(qs)
    qs2[idx] += dq
    cost = lmsr_cost(qs2, b) - lmsr_cost(qs, b)
    return max(0, cost)

def sell_refund(qs, b, idx, dq):
    if dq <= 0 or qs[idx] < dq:
        return 0
    qs2 = list(qs)
    qs2[idx] -= dq
    refund = lmsr_cost(qs, b) - lmsr_cost(qs2, b)
    return max(0, refund)

# -----------------------------
# Game / cycle settings
# -----------------------------
STARTING_BALANCE = float(os.environ.get("STARTING_BALANCE", "1000"))
DAILY_TOPUP = float(os.environ.get("DAILY_TOPUP", "200"))
BALANCE_CAP = float(os.environ.get("BALANCE_CAP", "2000"))
DEFAULT_LOCK = os.environ.get("DEFAULT_LOCK", "10m")
DEFAULT_LIQUIDITY_B = float(os.environ.get("LMSR_B", "100"))
TASK_SECRET = os.environ.get("TASK_SECRET", "")

class PredictionsError(Exception):
    pass

# -----------------------------
# Models
# -----------------------------
class User(db.Model):
    __tablename__ = 'user'
    user_id = db.Column(db.Integer, primary_key=True)
    slack_id = db.Column(db.Text, unique=True, nullable=False)
    slack_name = db.Column(db.Text, nullable=True)

    def __init__(self, slack_id, slack_name=None):
        self.slack_id = slack_id
        self.slack_name = slack_name

    def __repr__(self):
        return '<User %s>' % (self.slack_id)

class Cycle(db.Model):
    __tablename__ = 'cycle'
    cycle_id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.Text, unique=True, nullable=False)
    starts_at = db.Column(db.DateTime, nullable=False)
    ends_at = db.Column(db.DateTime, nullable=False)
    median_bets = db.Column(db.Integer, nullable=True)
    winner_slack_id = db.Column(db.Text, nullable=True)
    when_closed = db.Column(db.DateTime, nullable=True)

class UserCycle(db.Model):
    __tablename__ = 'user_cycle'
    user_cycle_id = db.Column(db.Integer, primary_key=True)
    cycle_id = db.Column(db.Integer, db.ForeignKey('cycle.cycle_id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.user_id'), nullable=False)
    balance = db.Column(db.Float, nullable=False, default=STARTING_BALANCE)
    bet_count = db.Column(db.Integer, nullable=False, default=0)
    last_topup_date = db.Column(db.Date, nullable=True)

    __table_args__ = (db.UniqueConstraint('cycle_id', 'user_id', name='uniq_cycle_user'),)

class Market(db.Model):
    __tablename__ = 'market'
    market_id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.Text, unique=True, nullable=False)
    question = db.Column(db.Text, nullable=False)
    creator_user_id = db.Column(db.Integer, db.ForeignKey('user.user_id'), nullable=False)
    when_closes = db.Column(db.DateTime, nullable=False)
    when_created = db.Column(db.DateTime, nullable=False, default=now)
    status = db.Column(db.Text, nullable=False, default='open')
    resolved_outcome_id = db.Column(db.Integer, nullable=True)
    when_resolved = db.Column(db.DateTime, nullable=True)
    when_cancelled = db.Column(db.DateTime, nullable=True)
    b = db.Column(db.Float, nullable=False, default=DEFAULT_LIQUIDITY_B)

class Outcome(db.Model):
    __tablename__ = 'outcome'
    outcome_id = db.Column(db.Integer, primary_key=True)
    market_id = db.Column(db.Integer, db.ForeignKey('market.market_id'), nullable=False)
    symbol = db.Column(db.Text, nullable=False)
    q = db.Column(db.Float, nullable=False, default=0.0)

    __table_args__ = (db.UniqueConstraint('market_id', 'symbol', name='uniq_market_symbol'),)

class Position(db.Model):
    __tablename__ = 'position'
    position_id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.user_id'), nullable=False)
    market_id = db.Column(db.Integer, db.ForeignKey('market.market_id'), nullable=False)
    outcome_id = db.Column(db.Integer, db.ForeignKey('outcome.outcome_id'), nullable=False)
    shares = db.Column(db.Float, nullable=False, default=0.0)

    __table_args__ = (db.UniqueConstraint('user_id', 'market_id', 'outcome_id', name='uniq_position'),)

class Trade(db.Model):
    __tablename__ = 'trade'
    trade_id = db.Column(db.Integer, primary_key=True)
    cycle_id = db.Column(db.Integer, db.ForeignKey('cycle.cycle_id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.user_id'), nullable=False)
    market_id = db.Column(db.Integer, db.ForeignKey('market.market_id'), nullable=False)
    outcome_id = db.Column(db.Integer, db.ForeignKey('outcome.outcome_id'), nullable=False)
    side = db.Column(db.Text, nullable=False)
    shares = db.Column(db.Float, nullable=False)
    amount = db.Column(db.Float, nullable=False)
    when_created = db.Column(db.DateTime, nullable=False, default=now)

# -----------------------------
# Helper functions
# -----------------------------
def cycle_key_for_dt(dt_london):
    return "%04d-%02d" % (dt_london.year, dt_london.month)

def month_bounds_london(year, month):
    start = LONDON_TZ.localize(datetime.datetime(year, month, 1, 0, 0, 0))
    if month == 12:
        end = LONDON_TZ.localize(datetime.datetime(year + 1, 1, 1, 0, 0, 0))
    else:
        end = LONDON_TZ.localize(datetime.datetime(year, month + 1, 1, 0, 0, 0))
    return start, end

def get_or_create_cycle():
    now_london = datetime.datetime.now(LONDON_TZ)
    key = cycle_key_for_dt(now_london)
    cyc = Cycle.query.filter(Cycle.key == key).first()
    if cyc:
        return cyc

    start_london, end_london = month_bounds_london(now_london.year, now_london.month)
    cyc = Cycle(
        key=key,
        starts_at=utc_naive(start_london),
        ends_at=utc_naive(end_london),
    )
    db.session.add(cyc)
    db.session.flush()
    return cyc

def get_or_create_user(slack_user_id, slack_user_name=None):
    u = User.query.filter(User.slack_id == slack_user_id).first()
    if u:
        if slack_user_name and u.slack_name != slack_user_name:
            u.slack_name = slack_user_name
        return u
    u = User(slack_id=slack_user_id, slack_name=slack_user_name)
    db.session.add(u)
    db.session.flush()
    return u

def get_or_create_usercycle(cycle, user):
    uc = UserCycle.query.filter(
        UserCycle.cycle_id == cycle.cycle_id,
        UserCycle.user_id == user.user_id
    ).first()
    if uc:
        return uc
    uc = UserCycle(
        cycle_id=cycle.cycle_id,
        user_id=user.user_id,
        balance=STARTING_BALANCE,
        bet_count=0
    )
    db.session.add(uc)
    db.session.flush()
    return uc

def ensure_daily_topup_for_usercycle(uc):
    today = datetime.datetime.now(LONDON_TZ).date()
    if uc.last_topup_date == today:
        return
    uc.balance = min(BALANCE_CAP, uc.balance + DAILY_TOPUP)
    uc.last_topup_date = today

def get_market_or_raise(market_name):
    m = Market.query.filter(Market.name == market_name).first()
    if not m:
        raise PredictionsError('unknown market %s' % market_name)
    return m

def get_outcomes(market):
    return Outcome.query.filter(Outcome.market_id == market.market_id).order_by(Outcome.outcome_id).all()

def market_is_closed(market):
    if market.status != 'open':
        return True
    if market.when_cancelled is not None:
        return True
    if market.when_closes < now():
        return True
    return False

# -----------------------------
# Command registry
# -----------------------------
commands = {}
def command(fn):
    commands[fn.__name__] = fn
    return fn

@command
def help(user, *args):
    return """\
/predict list
/predict show <market-name>
/predict create <market-name> <question> <event-time> [lock] <outcomes_csv>
  - event-time examples: "tomorrow 10am", "2026-01-13 10:00"
  - lock examples: 15m, 1h (defaults to %s)
  - outcomes_csv examples: "ARS,LIV,DRAW" or "TEAM_A,TEAM_B"
/predict buy <market-name> <outcome> <spend>
/predict sell <market-name> <outcome> <shares>
/predict balance
/predict leaderboard
/predict resolve <market-name> <outcome>
/predict cancel <market-name>
""" % DEFAULT_LOCK

# -----------------------------
# Commands
# -----------------------------
@command
def list(user):
    try:
        current_time = now()
        markets = Market.query.filter(
            Market.status == 'open',
            Market.when_cancelled == None,
            Market.when_closes > current_time
        ).order_by(Market.when_created.desc()).all()
        
        if not markets:
            return 'no active markets'
        
        return '\n'.join(m.name for m in markets)
    except Exception as e:
        logger.error(f"Error in list command: {e}")
        raise PredictionsError('Error retrieving markets')

@command
def show(user, market_name):
    try:
        m = get_market_or_raise(market_name)
        outcomes = get_outcomes(m)
        qs = [o.q for o in outcomes]
        prices = lmsr_prices(qs, m.b) if outcomes else []

        if m.when_cancelled is not None:
            status = 'Cancelled'
        elif m.status == 'resolved':
            win = None
            if m.resolved_outcome_id:
                win = Outcome.query.filter(Outcome.outcome_id == m.resolved_outcome_id).first()
            status = 'Resolved (%s)' % (win.symbol if win else 'unknown')
        elif m.when_closes < now():
            status = 'Closed'
        else:
            status = 'Open'

        close_line = ''
        if m.when_closes < now():
            close_line = 'Closed %s\n' % dt_to_string(m.when_closes)
        else:
            close_line = 'Closes %s (%s UTC)\n' % (dt_to_string(m.when_closes), m.when_closes)

        board = []
        for o, p in zip(outcomes, prices):
            board.append('%s: %.2f%%' % (o.symbol, p * 100))

        # user position summary
        pos_lines = []
        for o in outcomes:
            pos = Position.query.filter(
                Position.user_id == user.user_id,
                Position.market_id == m.market_id,
                Position.outcome_id == o.outcome_id
            ).first()
            if pos and pos.shares > 0:
                pos_lines.append('%s shares: %.2f' % (o.symbol, pos.shares))

        pos_text = ''
        if pos_lines:
            pos_text = '\n\nYour position:\n' + '\n'.join(pos_lines)

        return '%s\nStatus: %s\n%s\n%s%s' % (
            m.question, status, close_line, '\n'.join(board) if board else '(no outcomes)', pos_text
        )
    except Exception as e:
        logger.error(f"Error in show command: {e}")
        if isinstance(e, PredictionsError):
            raise
        raise PredictionsError('Error showing market')

@command
def create(user, market_name, question, event_time, *rest):
    try:
        if Market.query.filter(Market.name == market_name).first():
            raise PredictionsError('A market named %s already exists' % market_name)

        if len(rest) == 1:
            lock = DEFAULT_LOCK
            outcomes_csv = rest[0]
        elif len(rest) == 2:
            lock = rest[0]
            outcomes_csv = rest[1]
        else:
            raise PredictionsError('usage is create <market-name> <question> <event-time> [lock] <outcomes_csv>')

        event_dt_london = parse_natural_event_time(event_time)
        close_dt_london = event_dt_london - parse_lock_delta(lock)
        when_closes = utc_naive(close_dt_london)

        m = Market(
            name=market_name,
            question=question,
            creator_user_id=user.user_id,
            when_closes=when_closes,
            status='open',
            b=DEFAULT_LIQUIDITY_B,
        )
        db.session.add(m)
        db.session.flush()

        symbols = [s.strip() for s in outcomes_csv.split(',') if s.strip()]
        if len(symbols) < 2:
            raise PredictionsError('need at least 2 outcomes (e.g. "TEAM_A,TEAM_B" or "A,B,DRAW")')

        for sym in symbols:
            db.session.add(Outcome(market_id=m.market_id, symbol=sym, q=0.0))

        return 'Created market %s. Trading locks %s (%s UTC)' % (
            market_name, dt_to_string(when_closes), when_closes
        )
    except Exception as e:
        logger.error(f"Error in create command: {e}")
        if isinstance(e, PredictionsError):
            raise
        raise PredictionsError('Error creating market')

@command
def buy(user, market_name, outcome_symbol, spend):
    try:
        logger.info(f"Buy command: user={user.slack_id}, market={market_name}, outcome={outcome_symbol}, spend={spend}")
        
        m = get_market_or_raise(market_name)
        if market_is_closed(m):
            raise PredictionsError('market %s is closed' % market_name)

        try:
            spend = float(spend)
        except ValueError:
            raise PredictionsError('%s is not a valid float' % spend)

        if spend <= 0:
            raise PredictionsError('spend must be > 0')

        cycle = get_or_create_cycle()
        uc = get_or_create_usercycle(cycle, user)
        ensure_daily_topup_for_usercycle(uc)

        if uc.balance < spend:
            raise PredictionsError('insufficient balance (balance %.2f, need %.2f)' % (uc.balance, spend))

        outcomes = get_outcomes(m)
        if not outcomes:
            raise PredictionsError('market has no outcomes')

        # Find the outcome
        target_outcome = None
        idx = None
        for i, o in enumerate(outcomes):
            if o.symbol.upper() == outcome_symbol.upper():
                target_outcome = o
                idx = i
                break
        
        if target_outcome is None:
            available = ', '.join(o.symbol for o in outcomes)
            raise PredictionsError('unknown outcome %s. Available: %s' % (outcome_symbol, available))

        qs = [o.q for o in outcomes]
        b = m.b

        # Find dq such that cost ~= spend using binary search
        low, high = 0.0, 10000.0
        best_dq = 0.0
        for _ in range(30):  # More iterations for better accuracy
            mid = (low + high) / 2.0
            cost = buy_cost(qs, b, idx, mid)
            if cost > spend:
                high = mid
            else:
                low = mid
                best_dq = mid
        
        dq = best_dq
        cost = buy_cost(qs, b, idx, dq)

        if cost < 1e-9 or dq < 1e-9:
            raise PredictionsError('trade failed (amount too small)')
        
        if cost > uc.balance + 1e-6:  # Small epsilon for floating point errors
            raise PredictionsError('insufficient balance after pricing (balance %.2f, cost %.2f)' % (uc.balance, cost))

        # Apply trade
        uc.balance -= cost
        uc.bet_count += 1
        target_outcome.q += dq

        # Update or create position
        pos = Position.query.filter(
            Position.user_id == user.user_id,
            Position.market_id == m.market_id,
            Position.outcome_id == target_outcome.outcome_id
        ).first()
        
        if not pos:
            pos = Position(
                user_id=user.user_id, 
                market_id=m.market_id, 
                outcome_id=target_outcome.outcome_id, 
                shares=0.0
            )
            db.session.add(pos)
            db.session.flush()
        
        pos.shares += dq

        # Record trade
        db.session.add(Trade(
            cycle_id=cycle.cycle_id,
            user_id=user.user_id,
            market_id=m.market_id,
            outcome_id=target_outcome.outcome_id,
            side='buy',
            shares=dq,
            amount=cost
        ))

        # Calculate new prices
        new_qs = [o.q for o in outcomes]
        prices = lmsr_prices(new_qs, b)

        return '✅ Bought %.2f shares of %s in %s | Price now %.2f%% | Balance %.2f | Bets %d' % (
            dq, outcome_symbol, market_name, prices[idx] * 100, uc.balance, uc.bet_count
        )
        
    except Exception as e:
        logger.error(f"Error in buy command: {e}", exc_info=True)
        if isinstance(e, PredictionsError):
            raise
        raise PredictionsError('Error processing buy order')

@command
def sell(user, market_name, outcome_symbol, shares):
    try:
        m = get_market_or_raise(market_name)
        if market_is_closed(m):
            raise PredictionsError('market %s is closed' % market_name)

        try:
            shares = float(shares)
        except ValueError:
            raise PredictionsError('%s is not a valid float' % shares)

        if shares <= 0:
            raise PredictionsError('shares must be > 0')

        cycle = get_or_create_cycle()
        uc = get_or_create_usercycle(cycle, user)
        ensure_daily_topup_for_usercycle(uc)

        outcomes = get_outcomes(m)
        if not outcomes:
            raise PredictionsError('market has no outcomes')

        # Find the outcome
        target_outcome = None
        idx = None
        for i, o in enumerate(outcomes):
            if o.symbol.upper() == outcome_symbol.upper():
                target_outcome = o
                idx = i
                break
        
        if target_outcome is None:
            available = ', '.join(o.symbol for o in outcomes)
            raise PredictionsError('unknown outcome %s. Available: %s' % (outcome_symbol, available))

        pos = Position.query.filter(
            Position.user_id == user.user_id,
            Position.market_id == m.market_id,
            Position.outcome_id == target_outcome.outcome_id
        ).first()
        
        if not pos or pos.shares < shares:
            raise PredictionsError('not enough shares to sell (you have %.2f)' % (pos.shares if pos else 0.0))

        qs = [o.q for o in outcomes]
        b = m.b

        if target_outcome.q < shares:
            raise PredictionsError('market has insufficient liquidity to sell that many shares')

        refund = sell_refund(qs, b, idx, shares)
        if refund < 0:
            raise PredictionsError('trade failed (invalid refund)')

        # Apply trade
        pos.shares -= shares
        target_outcome.q -= shares
        uc.balance += refund
        uc.bet_count += 1

        db.session.add(Trade(
            cycle_id=cycle.cycle_id,
            user_id=user.user_id,
            market_id=m.market_id,
            outcome_id=target_outcome.outcome_id,
            side='sell',
            shares=shares,
            amount=refund
        ))

        # Calculate new prices
        new_qs = [o.q for o in outcomes]
        prices = lmsr_prices(new_qs, b)

        return '✅ Sold %.2f shares of %s in %s | Refund %.2f | Price now %.2f%% | Balance %.2f | Bets %d' % (
            shares, outcome_symbol, market_name, refund, prices[idx] * 100, uc.balance, uc.bet_count
        )
        
    except Exception as e:
        logger.error(f"Error in sell command: {e}")
        if isinstance(e, PredictionsError):
            raise
        raise PredictionsError('Error processing sell order')

@command
def balance(user):
    try:
        cycle = get_or_create_cycle()
        uc = get_or_create_usercycle(cycle, user)
        ensure_daily_topup_for_usercycle(uc)
        return '💰 Balance: %.2f | Bets this month: %d | Cycle: %s' % (uc.balance, uc.bet_count, cycle.key)
    except Exception as e:
        logger.error(f"Error in balance command: {e}")
        raise PredictionsError('Error retrieving balance')

@command
def leaderboard(user):
    try:
        cycle = get_or_create_cycle()
        rows = UserCycle.query.filter(UserCycle.cycle_id == cycle.cycle_id).all()
        if not rows:
            return 'No leaderboard yet (no one has interacted this cycle).'

        bet_counts = [r.bet_count for r in rows]
        med = int(median(sorted(bet_counts))) if bet_counts else 0

        # Top by balance
        top = sorted(rows, key=lambda r: r.balance, reverse=True)[:10]
        lines = [
            '🏆 Leaderboard (%s)' % cycle.key,
            'Median bets: %d (eligible if bets > %d)' % (med, med),
            ''
        ]
        for i, r in enumerate(top, 1):
            u = db.session.get(User, r.user_id)  # Use Session.get() instead of Query.get()
            eligible = '✅' if r.bet_count > med else '—'
            lines.append('%d. <@%s>  %.2f  (bets %d) %s' % (i, u.slack_id, r.balance, r.bet_count, eligible))
        return '\n'.join(lines)
    except Exception as e:
        logger.error(f"Error in leaderboard command: {e}")
        raise PredictionsError('Error retrieving leaderboard')

@command
def resolve(user, market_name, winning_outcome):
    try:
        m = get_market_or_raise(market_name)

        if m.status == 'resolved':
            raise PredictionsError('market %s is already resolved' % market_name)
        if m.when_cancelled is not None:
            raise PredictionsError('market %s was cancelled' % market_name)

        if m.creator_user_id != user.user_id:
            creator = db.session.get(User, m.creator_user_id)
            raise PredictionsError('Only %s can resolve %s' % (creator.slack_id if creator else 'creator', market_name))

        out = Outcome.query.filter(
            Outcome.market_id == m.market_id,
            Outcome.symbol == winning_outcome
        ).first()
        if not out:
            raise PredictionsError('unknown outcome %s' % winning_outcome)

        cycle = get_or_create_cycle()
        positions = Position.query.filter(
            Position.market_id == m.market_id, 
            Position.outcome_id == out.outcome_id
        ).all()
        
        for p in positions:
            uc = UserCycle.query.filter(
                UserCycle.cycle_id == cycle.cycle_id,
                UserCycle.user_id == p.user_id
            ).first()
            if not uc:
                u = db.session.get(User, p.user_id)
                uc = get_or_create_usercycle(cycle, u)
            ensure_daily_topup_for_usercycle(uc)
            uc.balance += p.shares

        m.status = 'resolved'
        m.resolved_outcome_id = out.outcome_id
        m.when_resolved = now()

        return '🏁 Market %s resolved: %s' % (market_name, winning_outcome)
    except Exception as e:
        logger.error(f"Error in resolve command: {e}")
        if isinstance(e, PredictionsError):
            raise
        raise PredictionsError('Error resolving market')

@command
def cancel(user, market_name):
    try:
        m = get_market_or_raise(market_name)

        if m.when_cancelled is not None:
            raise PredictionsError('market %s was already cancelled' % market_name)

        if m.creator_user_id != user.user_id:
            creator = db.session.get(User, m.creator_user_id)
            raise PredictionsError('Only %s can cancel %s' % (creator.slack_id if creator else 'creator', market_name))

        m.when_cancelled = now()
        m.status = 'cancelled'
        return 'Market %s cancelled' % market_name
    except Exception as e:
        logger.error(f"Error in cancel command: {e}")
        if isinstance(e, PredictionsError):
            raise
        raise PredictionsError('Error cancelling market')

@command
def close_month(user):
    try:
        admins = [x.strip() for x in os.environ.get("ADMIN_SLACK_IDS", "").split(",") if x.strip()]
        if admins and user.slack_id not in admins:
            raise PredictionsError('Only admins can close the month')

        cycle = get_or_create_cycle()
        if cycle.when_closed is not None:
            raise PredictionsError('cycle %s already closed' % cycle.key)

        rows = UserCycle.query.filter(UserCycle.cycle_id == cycle.cycle_id).all()
        if not rows:
            cycle.when_closed = now()
            cycle.median_bets = 0
            cycle.winner_slack_id = None
            return '🏁 Closed %s (no participants)' % cycle.key

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

        # Build final leaderboard
        top = sorted(rows, key=lambda r: r.balance, reverse=True)[:10]
        lines = [
            '🏁 *Month closed:* %s' % cycle.key,
            'Median bets: *%d* (eligible if bets > %d)' % (med_val, med_val),
        ]
        if winner:
            u = db.session.get(User, winner.user_id)
            lines.append('🏆 Winner: <@%s> — %.2f (bets %d)' % (u.slack_id, winner.balance, winner.bet_count))
        else:
            lines.append('🏆 Winner: No eligible winner (not enough participation).')

        lines.append('\nTop balances:')
        for i, r in enumerate(top, 1):
            u = db.session.get(User, r.user_id)
            eligible_mark = '✅' if r.bet_count > med_val else '—'
            lines.append('%d. <@%s>  %.2f  (bets %d) %s' % (i, u.slack_id, r.balance, r.bet_count, eligible_mark))

        return '\n'.join(lines)
    except Exception as e:
        logger.error(f"Error in close_month command: {e}")
        if isinstance(e, PredictionsError):
            raise
        raise PredictionsError('Error closing month')

# -----------------------------
# Task endpoints
# -----------------------------
@app.route('/tasks/daily_topup', methods=['GET'])
def task_daily_topup():
    if TASK_SECRET and request.args.get('secret') != TASK_SECRET:
        return Response('forbidden', status=403)

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
        return f'topped up {count} users for {cycle.key}'
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error in daily topup: {e}")
        return f'Error: {str(e)}', 500

@app.route('/tasks/monthly_close', methods=['GET'])
def task_monthly_close():
    if TASK_SECRET and request.args.get('secret') != TASK_SECRET:
        return Response('forbidden', status=403)

    try:
        sys_user = User.query.filter(User.slack_id == 'SYSTEM').first()
        if not sys_user:
            sys_user = User(slack_id='SYSTEM', slack_name='system')
            db.session.add(sys_user)
            db.session.flush()

        msg = close_month(sys_user)
        db.session.commit()
        return msg
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error in monthly close: {e}")
        return f'Error: {str(e)}', 500

# -----------------------------
# Main request handler
# -----------------------------
@app.route('/', methods=['POST'])
def handle_request():
    try:
        # Verify token
        if request.form.get('token') != os.environ.get('SLACK_TOKEN'):
            return Response(json.dumps({
                'response_type': 'ephemeral', 
                'text': 'Invalid token'
            }), mimetype='application/json', status=401)

        # Parse command
        text = request.form.get('text', '').strip()
        args = shlex.split(text) if text else ['help']
        
        slack_user_id = request.form.get('user_id')
        slack_user_name = request.form.get('user_name')
        
        logger.info(f"Command received: text='{text}', args={args}, user={slack_user_id}")
        
        if not slack_user_id:
            return Response(json.dumps({
                'response_type': 'ephemeral',
                'text': 'Missing user information'
            }), mimetype='application/json', status=400)

        # Get or create user
        user = get_or_create_user(slack_user_id, slack_user_name)

        # Route command
        command_str = 'help'
        if args and args[0] in commands:
            command_str = args[0]
            args = args[1:]
        elif len(args) >= 3:
            # Shorthand: /predict <market> <outcome> <spend>
            command_str = 'buy'
        
        logger.info(f"Executing command: {command_str} with args: {args}")
        
        selected_command = commands[command_str]
        
        # Get expected arguments (skip 'user' parameter)
        sig = inspect.signature(selected_command)
        param_names = list(sig.parameters.keys())
        expected_args = param_names[1:] if len(param_names) > 1 else []

        logger.info(f"Expected args: {expected_args}, received args: {args}")

        if len(args) != len(expected_args):
            if expected_args:
                usage_str = f'usage is {command_str} {" ".join(f"<{arg}>" for arg in expected_args)}'
            else:
                usage_str = f'usage is {command_str}'
            raise PredictionsError(usage_str)

        # Execute command
        response = selected_command(user, *args)
        db.session.commit()

        return Response(json.dumps({
            'response_type': 'in_channel',
            'text': response
        }), mimetype='application/json')

    except PredictionsError as e:
        db.session.rollback()
        logger.info(f"Predictions error: {e}")
        return Response(json.dumps({
            'response_type': 'ephemeral',
            'text': f'Error: {str(e)}'
        }), mimetype='application/json')
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error in handle_request: {e}", exc_info=True)
        return Response(json.dumps({
            'response_type': 'ephemeral',
            'text': 'Internal error occurred'
        }), mimetype='application/json', status=500)

# Health check
@app.route('/health', methods=['GET'])
def health_check():
    try:
        # Test database connection
        db.session.execute(text('SELECT 1'))
        return 'OK'
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return f'Error: {str(e)}', 500

if __name__ == '__main__':
    with app.app_context():
        try:
            db.create_all()
            logger.info("Database initialized successfully")
        except Exception as e:
            logger.error(f"Database initialization failed: {e}")
    
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)  # Disable debug in production