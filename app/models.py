from __future__ import annotations

import datetime
from typing import List, Optional

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
from . import config, utils
from .exceptions import PredictionsError

db = SQLAlchemy()

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
    balance = db.Column(db.Float, nullable=False, default=config.STARTING_BALANCE)
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
    when_created = db.Column(db.DateTime, nullable=False, default=utils.now)
    status = db.Column(db.Text, nullable=False, default="open")  # open, resolved, cancelled
    resolved_outcome_id = db.Column(db.Integer, nullable=True)
    when_resolved = db.Column(db.DateTime, nullable=True)
    when_cancelled = db.Column(db.DateTime, nullable=True)
    b = db.Column(db.Float, nullable=False, default=config.DEFAULT_LIQUIDITY_B)


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
    when_created = db.Column(db.DateTime, nullable=False, default=utils.now)


# ============================================================================
# Helpers
# ============================================================================

def get_or_create_cycle() -> Cycle:
    """Get or create the current month's cycle."""
    now_london = datetime.datetime.now(config.LONDON_TZ)
    key = utils.cycle_key_for_dt(now_london)
    cyc = Cycle.query.filter(Cycle.key == key).first()
    if cyc:
        return cyc
    start_london, end_london = utils.month_bounds_london(now_london.year, now_london.month)
    cyc = Cycle(key=key, starts_at=utils.utc_naive(start_london), ends_at=utils.utc_naive(end_london))
    db.session.add(cyc)
    db.session.flush()
    return cyc


def get_or_create_user(slack_user_id: str, slack_user_name: Optional[str] = None) -> User:
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
    uc = UserCycle(cycle_id=cycle.cycle_id, user_id=user.user_id, balance=config.STARTING_BALANCE, bet_count=0)
    db.session.add(uc)
    db.session.flush()
    return uc


def ensure_daily_topup_for_usercycle(uc: UserCycle):
    """Apply daily top-up if user hasn't been topped up today."""
    today = datetime.datetime.now(config.LONDON_TZ).date()
    if uc.last_topup_date == today:
        return
    uc.balance = min(config.BALANCE_CAP, uc.balance + config.DAILY_TOPUP)
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
    if market.when_closes < utils.now():
        return True
    return False
