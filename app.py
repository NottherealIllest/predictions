import os
import math
import json
import pytz
import shlex
import tzlocal
import inspect
import datetime
import parsedatetime
import re
from statistics import median
from collections import defaultdict
from flask import Flask, request, Response
from flask.ext.sqlalchemy import SQLAlchemy  # keep old import so we don't break existing env

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
    'DATABASE_URL', 'postgres:///predictionslocal'
)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# -----------------------------
# Time / timezone helpers
# -----------------------------
now = datetime.datetime.utcnow
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
    """
    Parse "15m", "2h", "1d" to timedelta.
    """
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
    """
    Parse natural language like "tomorrow 10am" in Europe/London.
    Also supports explicit "YYYY-MM-DD HH:MM".
    """
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
    m = max(qs) / b
    return b * (m + math.log(sum(math.exp((q / b) - m) for q in qs)))

def lmsr_prices(qs, b):
    exps = [math.exp(q / b) for q in qs]
    s = sum(exps) or 1.0
    return [e / s for e in exps]

def buy_cost(qs, b, idx, dq):
    qs2 = list(qs)
    qs2[idx] += dq
    return lmsr_cost(qs2, b) - lmsr_cost(qs, b)

def sell_refund(qs, b, idx, dq):
    qs2 = list(qs)
    qs2[idx] -= dq
    return lmsr_cost(qs, b) - lmsr_cost(qs2, b)

# -----------------------------
# Game / cycle settings
# -----------------------------
STARTING_BALANCE = float(os.environ.get("STARTING_BALANCE", "1000"))
DAILY_TOPUP = float(os.environ.get("DAILY_TOPUP", "200"))
BALANCE_CAP = float(os.environ.get("BALANCE_CAP", "2000"))
DEFAULT_LOCK = os.environ.get("DEFAULT_LOCK", "10m")  # lock trading before event
DEFAULT_LIQUIDITY_B = float(os.environ.get("LMSR_B", "100"))

TASK_SECRET = os.environ.get("TASK_SECRET", "")

class PredictionsError(Exception):
    pass

# -----------------------------
# Models
# -----------------------------
class User(db.Model):
    user_id = db.Column(db.Integer, primary_key=True)
    slack_id = db.Column(db.UnicodeText, unique=True, nullable=False)  # Slack user_id (e.g. U123)
    slack_name = db.Column(db.UnicodeText, nullable=True)

    def __init__(self, slack_id, slack_name=None):
        self.slack_id = slack_id
        self.slack_name = slack_name

    def __repr__(self):
        return '<User %s>' % (self.slack_id)

class Cycle(db.Model):
    cycle_id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.UnicodeText, unique=True, nullable=False)  # "YYYY-MM"
    starts_at = db.Column(db.DateTime, nullable=False)  # naive UTC
    ends_at = db.Column(db.DateTime, nullable=False)    # naive UTC
    median_bets = db.Column(db.Integer, nullable=True)
    winner_slack_id = db.Column(db.UnicodeText, nullable=True)
    when_closed = db.Column(db.DateTime, nullable=True)

class UserCycle(db.Model):
    user_cycle_id = db.Column(db.Integer, primary_key=True)
    cycle_id = db.Column(db.Integer, db.ForeignKey('cycle.cycle_id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.user_id'), nullable=False)

    balance = db.Column(db.Float, nullable=False, default=STARTING_BALANCE)
    bet_count = db.Column(db.Integer, nullable=False, default=0)
    last_topup_date = db.Column(db.Date, nullable=True)

    __table_args__ = (db.UniqueConstraint('cycle_id', 'user_id', name='uniq_cycle_user'),)

class Market(db.Model):
    market_id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.UnicodeText, unique=True, nullable=False)  # keep old "contract-name" feel
    question = db.Column(db.UnicodeText, nullable=False)           # display text
    creator_user_id = db.Column(db.Integer, db.ForeignKey('user.user_id'), nullable=False)

    # trading close (expiry)
    when_closes = db.Column(db.DateTime, nullable=False)  # naive UTC
    when_created = db.Column(db.DateTime, nullable=False, default=now)

    status = db.Column(db.UnicodeText, nullable=False, default='open')  # open|closed|resolved|cancelled
    resolved_outcome_id = db.Column(db.Integer, nullable=True)
    when_resolved = db.Column(db.DateTime, nullable=True)
    when_cancelled = db.Column(db.DateTime, nullable=True)

    b = db.Column(db.Float, nullable=False, default=DEFAULT_LIQUIDITY_B)

class Outcome(db.Model):
    outcome_id = db.Column(db.Integer, primary_key=True)
    market_id = db.Column(db.Integer, db.ForeignKey('market.market_id'), nullable=False)
    symbol = db.Column(db.UnicodeText, nullable=False)  # TEAM1, TEAM2, DRAW
    q = db.Column(db.Float, nullable=False, default=0.0)  # LMSR outstanding shares

    __table_args__ = (db.UniqueConstraint('market_id', 'symbol', name='uniq_market_symbol'),)

class Position(db.Model):
    position_id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.user_id'), nullable=False)
    market_id = db.Column(db.Integer, db.ForeignKey('market.market_id'), nullable=False)
    outcome_id = db.Column(db.Integer, db.ForeignKey('outcome.outcome_id'), nullable=False)
    shares = db.Column(db.Float, nullable=False, default=0.0)

    __table_args__ = (db.UniqueConstraint('user_id', 'market_id', 'outcome_id', name='uniq_position'),)

class Trade(db.Model):
    trade_id = db.Column(db.Integer, primary_key=True)
    cycle_id = db.Column(db.Integer, db.ForeignKey('cycle.cycle_id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.user_id'), nullable=False)
    market_id = db.Column(db.Integer, db.ForeignKey('market.market_id'), nullable=False)
    outcome_id = db.Column(db.Integer, db.ForeignKey('outcome.outcome_id'), nullable=False)
    side = db.Column(db.UnicodeText, nullable=False)  # buy|sell
    shares = db.Column(db.Float, nullable=False)
    amount = db.Column(db.Float, nullable=False)      # cost (buy) or refund (sell)
    when_created = db.Column(db.DateTime, nullable=False, default=now)

# -----------------------------
# Cycle logic
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

def get_or_create_cycle(session):
    now_london = datetime.datetime.now(LONDON_TZ)
    key = cycle_key_for_dt(now_london)
    cyc = session.query(Cycle).filter(Cycle.key == key).one_or_none()
    if cyc:
        return cyc

    start_london, end_london = month_bounds_london(now_london.year, now_london.month)
    cyc = Cycle(
        key=key,
        starts_at=utc_naive(start_london),
        ends_at=utc_naive(end_london),
    )
    session.add(cyc)
    session.flush()
    return cyc

def get_or_create_user(session, slack_user_id, slack_user_name=None):
    u = session.query(User).filter(User.slack_id == slack_user_id).one_or_none()
    if u:
        if slack_user_name and u.slack_name != slack_user_name:
            u.slack_name = slack_user_name
        return u
    u = User(slack_id=slack_user_id, slack_name=slack_user_name)
    session.add(u)
    session.flush()
    return u

def get_or_create_usercycle(session, cycle, user):
    uc = session.query(UserCycle).filter(
        UserCycle.cycle_id == cycle.cycle_id,
        UserCycle.user_id == user.user_id
    ).one_or_none()
    if uc:
        return uc
    uc = UserCycle(
        cycle_id=cycle.cycle_id,
        user_id=user.user_id,
        balance=STARTING_BALANCE,
        bet_count=0
    )
    session.add(uc)
    session.flush()
    return uc

def ensure_daily_topup_for_usercycle(uc):
    today = datetime.datetime.now(LONDON_TZ).date()
    if uc.last_topup_date == today:
        return
    uc.balance = min(BALANCE_CAP, uc.balance + DAILY_TOPUP)
    uc.last_topup_date = today

# -----------------------------
# Command registry
# -----------------------------
commands = {}
def command(fn):
    commands[fn.__name__] = fn
    return fn

@command
def help(session, user):
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

class _MarketNotFound(Exception):
    pass

def get_market_or_raise(session, market_name):
    m = session.query(Market).filter(Market.name == market_name).one_or_none()
    if not m:
        raise PredictionsError('unknown market %s' % market_name)
    return m

def get_outcomes(session, market):
    return session.query(Outcome).filter(Outcome.market_id == market.market_id).order_by(Outcome.outcome_id).all()

def market_is_closed(market):
    if market.status != 'open':
        return True
    if market.when_cancelled is not None:
        return True
    if market.when_closes < now():
        return True
    return False

# -----------------------------
# Commands
# -----------------------------
@command
def list(session, user):
    r = []
    for m in session.query(Market).filter(
        Market.status == 'open',
        Market.when_cancelled == None
    ).order_by(Market.when_created.desc()):
        r.append(m.name)
    if not r:
        return 'no active markets'
    return '\n'.join(r)

@command
def show(session, user, market_name):
    m = get_market_or_raise(session, market_name)
    outcomes = get_outcomes(session, m)
    qs = [o.q for o in outcomes]
    prices = lmsr_prices(qs, m.b) if outcomes else []

    if m.when_cancelled is not None:
        status = 'Cancelled'
    elif m.status == 'resolved':
        win = session.query(Outcome).filter(Outcome.outcome_id == m.resolved_outcome_id).one_or_none()
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
        pos = session.query(Position).filter(
            Position.user_id == user.user_id,
            Position.market_id == m.market_id,
            Position.outcome_id == o.outcome_id
        ).one_or_none()
        if pos and pos.shares > 0:
            pos_lines.append('%s shares: %.2f' % (o.symbol, pos.shares))

    pos_text = ''
    if pos_lines:
        pos_text = '\n\nYour position:\n' + '\n'.join(pos_lines)

    return '%s\nStatus: %s\n%s\n%s%s' % (
        m.question, status, close_line, '\n'.join(board) if board else '(no outcomes)', pos_text
    )

@command
def create(session, user, market_name, question, event_time, *rest):
    """
    /predict create <market-name> <question> <event-time> [lock] <outcomes_csv>

    Examples:
      /predict create latecomer "Who is most likely to come late to tomorrow's 10am meeting?" "tomorrow 10am" 15m "HABEEB,JOSH,TAYO"
      /predict create ars_liv "Arsenal vs Liverpool" "2026-01-16 19:45" 15m "ARS,LIV,DRAW"
    """
    if session.query(Market).filter(Market.name == market_name).one_or_none():
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
    session.add(m)
    session.flush()

    symbols = [s.strip() for s in outcomes_csv.split(',') if s.strip()]
    if len(symbols) < 2:
        raise PredictionsError('need at least 2 outcomes (e.g. "TEAM_A,TEAM_B" or "A,B,DRAW")')

    for sym in symbols:
        session.add(Outcome(market_id=m.market_id, symbol=sym, q=0.0))

    return 'Created market %s. Trading locks %s (%s UTC)' % (
        market_name, dt_to_string(when_closes), when_closes
    )

@command
def buy(session, user, market_name, outcome_symbol, spend):
    m = get_market_or_raise(session, market_name)
    if market_is_closed(m):
        raise PredictionsError('market %s is closed' % market_name)

    try:
        spend = float(spend)
    except ValueError:
        raise PredictionsError('%s is not a valid float' % spend)

    if spend <= 0:
        raise PredictionsError('spend must be > 0')

    cycle = get_or_create_cycle(session)
    uc = get_or_create_usercycle(session, cycle, user)
    ensure_daily_topup_for_usercycle(uc)

    if uc.balance < spend:
        raise PredictionsError('insufficient balance (balance %.2f, need %.2f)' % (uc.balance, spend))

    outcomes = get_outcomes(session, m)
    if not outcomes:
        raise PredictionsError('market has no outcomes')

    idx = None
    for i, o in enumerate(outcomes):
        if o.symbol == outcome_symbol:
            idx = i
            break
    if idx is None:
        raise PredictionsError('unknown outcome %s' % outcome_symbol)

    qs = [o.q for o in outcomes]
    b = m.b

    # Find dq such that cost ~= spend
    low, high = 0.0, 10000.0
    for _ in range(24):
        mid = (low + high) / 2.0
        if buy_cost(qs, b, idx, mid) > spend:
            high = mid
        else:
            low = mid
    dq = low
    cost = buy_cost(qs, b, idx, dq)

    if cost <= 0:
        raise PredictionsError('trade failed (invalid cost)')
    if cost > uc.balance + 1e-9:
        raise PredictionsError('insufficient balance after pricing (balance %.2f, cost %.2f)' % (uc.balance, cost))

    # Apply trade
    uc.balance -= cost
    uc.bet_count += 1

    outcomes[idx].q += dq

    pos = session.query(Position).filter(
        Position.user_id == user.user_id,
        Position.market_id == m.market_id,
        Position.outcome_id == outcomes[idx].outcome_id
    ).one_or_none()
    if not pos:
        pos = Position(user_id=user.user_id, market_id=m.market_id, outcome_id=outcomes[idx].outcome_id, shares=0.0)
        session.add(pos)
        session.flush()
    pos.shares += dq

    session.add(Trade(
        cycle_id=cycle.cycle_id,
        user_id=user.user_id,
        market_id=m.market_id,
        outcome_id=outcomes[idx].outcome_id,
        side='buy',
        shares=dq,
        amount=cost
    ))

    prices = lmsr_prices([o.q for o in outcomes], b)
    return '✅ Bought %.2f shares of %s in %s | Price now %.2f%% | Balance %.2f | Bets %d' % (
        dq, outcome_symbol, market_name, prices[idx] * 100, uc.balance, uc.bet_count
    )

@command
def sell(session, user, market_name, outcome_symbol, shares):
    m = get_market_or_raise(session, market_name)
    if market_is_closed(m):
        raise PredictionsError('market %s is closed' % market_name)

    try:
        shares = float(shares)
    except ValueError:
        raise PredictionsError('%s is not a valid float' % shares)

    if shares <= 0:
        raise PredictionsError('shares must be > 0')

    cycle = get_or_create_cycle(session)
    uc = get_or_create_usercycle(session, cycle, user)
    ensure_daily_topup_for_usercycle(uc)

    outcomes = get_outcomes(session, m)
    if not outcomes:
        raise PredictionsError('market has no outcomes')

    idx = None
    out = None
    for i, o in enumerate(outcomes):
        if o.symbol == outcome_symbol:
            idx = i
            out = o
            break
    if idx is None:
        raise PredictionsError('unknown outcome %s' % outcome_symbol)

    pos = session.query(Position).filter(
        Position.user_id == user.user_id,
        Position.market_id == m.market_id,
        Position.outcome_id == out.outcome_id
    ).one_or_none()
    if not pos or pos.shares < shares:
        raise PredictionsError('not enough shares to sell (you have %.2f)' % (pos.shares if pos else 0.0))

    qs = [o.q for o in outcomes]
    b = m.b

    # Ensure market state allows the sell (q cannot go negative)
    if out.q < shares:
        raise PredictionsError('market has insufficient liquidity to sell that many shares')

    refund = sell_refund(qs, b, idx, shares)
    if refund < 0:
        raise PredictionsError('trade failed (invalid refund)')

    # Apply
    pos.shares -= shares
    out.q -= shares

    uc.balance += refund
    uc.bet_count += 1

    session.add(Trade(
        cycle_id=cycle.cycle_id,
        user_id=user.user_id,
        market_id=m.market_id,
        outcome_id=out.outcome_id,
        side='sell',
        shares=shares,
        amount=refund
    ))

    prices = lmsr_prices([o.q for o in outcomes], b)
    return '✅ Sold %.2f shares of %s in %s | Refund %.2f | Price now %.2f%% | Balance %.2f | Bets %d' % (
        shares, outcome_symbol, market_name, refund, prices[idx] * 100, uc.balance, uc.bet_count
    )

@command
def balance(session, user):
    cycle = get_or_create_cycle(session)
    uc = get_or_create_usercycle(session, cycle, user)
    ensure_daily_topup_for_usercycle(uc)
    return '💰 Balance: %.2f | Bets this month: %d | Cycle: %s' % (uc.balance, uc.bet_count, cycle.key)

@command
def leaderboard(session, user):
    cycle = get_or_create_cycle(session)
    rows = session.query(UserCycle).filter(UserCycle.cycle_id == cycle.cycle_id).all()
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
        u = session.query(User).get(r.user_id)
        eligible = '✅' if r.bet_count > med else '—'
        lines.append('%d. <@%s>  %.2f  (bets %d) %s' % (i, u.slack_id, r.balance, r.bet_count, eligible))
    return '\n'.join(lines)

@command
def resolve(session, user, market_name, winning_outcome):
    m = get_market_or_raise(session, market_name)

    if m.status == 'resolved':
        raise PredictionsError('market %s is already resolved' % market_name)
    if m.when_cancelled is not None:
        raise PredictionsError('market %s was cancelled' % market_name)

    # Keep old behavior: only creator can resolve
    if m.creator_user_id != user.user_id:
        creator = session.query(User).get(m.creator_user_id)
        raise PredictionsError('Only %s can resolve %s' % (creator.slack_id if creator else 'creator', market_name))

    out = session.query(Outcome).filter(
        Outcome.market_id == m.market_id,
        Outcome.symbol == winning_outcome
    ).one_or_none()
    if not out:
        raise PredictionsError('unknown outcome %s' % winning_outcome)

    # Payout: 1 credit per share of winning outcome
    # Note: payout applies to current cycle balances (month competition)
    cycle = get_or_create_cycle(session)

    positions = session.query(Position).filter(Position.market_id == m.market_id, Position.outcome_id == out.outcome_id).all()
    for p in positions:
        uc = session.query(UserCycle).filter(
            UserCycle.cycle_id == cycle.cycle_id,
            UserCycle.user_id == p.user_id
        ).one_or_none()
        if not uc:
            # if someone never checked balance this month, create their cycle row
            u = session.query(User).get(p.user_id)
            uc = get_or_create_usercycle(session, cycle, u)
        ensure_daily_topup_for_usercycle(uc)
        uc.balance += p.shares

    m.status = 'resolved'
    m.resolved_outcome_id = out.outcome_id
    m.when_resolved = now()

    return '🏁 Market %s resolved: %s' % (market_name, winning_outcome)

@command
def cancel(session, user, market_name):
    m = get_market_or_raise(session, market_name)

    if m.when_cancelled is not None:
        raise PredictionsError('market %s was already cancelled' % market_name)

    if m.creator_user_id != user.user_id:
        creator = session.query(User).get(m.creator_user_id)
        raise PredictionsError('Only %s can cancel %s' % (creator.slack_id if creator else 'creator', market_name))

    m.when_cancelled = now()
    m.status = 'cancelled'
    return 'Market %s cancelled' % market_name

@command
def close_month(session, user):
    """
    Admin-ish command: closes the current cycle and declares winner:
    eligible = bet_count > median(bet_count), winner = max balance among eligible.
    """
    # optional: restrict to a list of admin slack IDs
    admins = [x.strip() for x in os.environ.get("ADMIN_SLACK_IDS", "").split(",") if x.strip()]
    if admins and user.slack_id not in admins:
        raise PredictionsError('Only admins can close the month')

    cycle = get_or_create_cycle(session)
    if cycle.when_closed is not None:
        raise PredictionsError('cycle %s already closed' % cycle.key)

    rows = session.query(UserCycle).filter(UserCycle.cycle_id == cycle.cycle_id).all()
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
        u = session.query(User).get(winner.user_id)
        cycle.winner_slack_id = u.slack_id
    else:
        cycle.winner_slack_id = None

    # Build final leaderboard (top 10)
    top = sorted(rows, key=lambda r: r.balance, reverse=True)[:10]
    lines = [
        '🏁 *Month closed:* %s' % cycle.key,
        'Median bets: *%d* (eligible if bets > %d)' % (med_val, med_val),
    ]
    if winner:
        u = session.query(User).get(winner.user_id)
        lines.append('🏆 Winner: <@%s> — %.2f (bets %d)' % (u.slack_id, winner.balance, winner.bet_count))
    else:
        lines.append('🏆 Winner: No eligible winner (not enough participation).')

    lines.append('\nTop balances:')
    for i, r in enumerate(top, 1):
        u = session.query(User).get(r.user_id)
        eligible_mark = '✅' if r.bet_count > med_val else '—'
        lines.append('%d. <@%s>  %.2f  (bets %d) %s' % (i, u.slack_id, r.balance, r.bet_count, eligible_mark))

    return '\n'.join(lines)

# -----------------------------
# Scheduled task endpoints
# (use server cron to hit these)
# -----------------------------
@app.route('/tasks/daily_topup', methods=['GET'])
def task_daily_topup():
    if TASK_SECRET and request.args.get('secret') != TASK_SECRET:
        return Response('forbidden', status=403)

    session = db.session
    try:
        cycle = get_or_create_cycle(session)
        today = datetime.datetime.now(LONDON_TZ).date()

        rows = session.query(UserCycle).filter(UserCycle.cycle_id == cycle.cycle_id).all()
        for uc in rows:
            if uc.last_topup_date == today:
                continue
            uc.balance = min(BALANCE_CAP, uc.balance + DAILY_TOPUP)
            uc.last_topup_date = today

        session.commit()
        return 'topped up %d users for %s' % (len(rows), cycle.key)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

@app.route('/tasks/monthly_close', methods=['GET'])
def task_monthly_close():
    if TASK_SECRET and request.args.get('secret') != TASK_SECRET:
        return Response('forbidden', status=403)

    session = db.session
    try:
        # Use a dummy "system" user for closing if needed
        sys_user = session.query(User).filter(User.slack_id == 'SYSTEM').one_or_none()
        if not sys_user:
            sys_user = User(slack_id='SYSTEM', slack_name='system')
            session.add(sys_user)
            session.flush()

        msg = close_month(session, sys_user)
        session.commit()
        return msg
    except PredictionsError as e:
        session.rollback()
        return 'Error: %s' % str(e)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

# -----------------------------
# Slack request handling
# -----------------------------
def lookup_or_create_user(session, slack_user_id, slack_user_name):
    return get_or_create_user(session, slack_user_id, slack_user_name)

@app.route('/', methods=['POST'])
def handle_request():
    if request.form['token'] != os.environ['SLACK_TOKEN']:
        raise Exception('invalid token')

    args = shlex.split(request.form.get('text', '').strip())
    if not args:
        args = ['help']

    session = db.session
    try:
        slack_user_id = request.form.get('user_id') or request.form.get('user_name')
        slack_user_name = request.form.get('user_name')

        user = lookup_or_create_user(session, slack_user_id, slack_user_name)

        internal_args = [session, user]
        command_str = 'buy'  # default behavior is trading, but we'll route properly below

        # If first token is a command, use it.
        # Otherwise, treat it like: <market> <outcome> <spend>  (a friendly shorthand for buy)
        if args[0] in commands:
            command_str = args[0]
            args = args[1:]
        else:
            # shorthand: /predict <market> <outcome> <spend>
            command_str = 'buy'

        selected_command = commands[command_str]
        expected_args = [x for x in inspect.signature(
            selected_command).parameters][len(internal_args):]

        if len(args) != len(expected_args):
            raise PredictionsError('usage is %s %s' % (
                command_str, ' '.join('<%s>' % arg for arg in expected_args)
            ))

        response = selected_command(*internal_args, *args)
        session.commit()
    except Exception as e:
        session.rollback()
        if isinstance(e, PredictionsError):
            return Response(json.dumps(dict(response_type='ephemeral', text='Error: %s' % str(e))),
                            mimetype='application/json')
        else:
            raise
    finally:
        session.close()

    return Response(json.dumps(dict(response_type='in_channel', text=response)),
                    mimetype='application/json')

if __name__ == '__main__':
    app.debug = True
    db.create_all()
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
