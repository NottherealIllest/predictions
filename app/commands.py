from __future__ import annotations

import logging
import math
import inspect
import os
from typing import Dict, Any

from . import config, utils, lmsr
from .models import (
    db, User, Market, Outcome, Position, Trade, Cycle, UserCycle,
    get_market_or_raise, get_outcomes, market_is_closed,
    get_or_create_cycle, get_or_create_usercycle, ensure_daily_topup_for_usercycle
)
# Correcting imports: lmsr functions are in lmsr module
from .lmsr import lmsr_prices, lmsr_cost, buy_cost, sell_refund
from .exceptions import PredictionsError

logger = logging.getLogger(__name__)

# Command registry
commands: Dict[str, Dict[str, Any]] = {}

def command(fn):
    """
    Decorator to register a command and extract its parameter names.
    
    Stores function + parameter list for safe routing without introspection at request time.
    Detects VAR_POSITIONAL (*rest) to allow variable argument counts.
    """
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


@command
def help(user: User) -> str:
    """Show help text."""
    return f"""/predict list
/predict show <market-name>
/predict create <market-name> <question> <event-time> [lock] <outcomes_csv>
  - event-time examples: "tomorrow 10am", "2026-01-13 10:00"
  - lock examples: 15m, 1h (defaults to {config.DEFAULT_LOCK})
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
    current_time = utils.now()
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
    close_line = f"Closes {utils.dt_to_string(m.when_closes)} ({m.when_closes} UTC)\n"
    return f"{m.question}\nStatus: {status}\n{close_line}{board}{pos_text}"


@command
def create(user: User, market_name: str, question: str, event_time: str, *rest) -> str:
    """Create a new market."""
    if Market.query.filter(Market.name == market_name).first():
        raise PredictionsError(f"A market named {market_name} already exists")
    
    if len(rest) == 1:
        lock = config.DEFAULT_LOCK
        outcomes_csv = rest[0]
    elif len(rest) == 2:
        lock = rest[0]
        outcomes_csv = rest[1]
    else:
        raise PredictionsError("usage is create <market-name> <question> <event-time> [lock] <outcomes_csv>")
    
    event_dt_london = utils.parse_natural_event_time(event_time)
    close_dt_london = event_dt_london - utils.parse_lock_delta(lock)
    when_closes = utils.utc_naive(close_dt_london)
    
    m = Market(
        name=market_name,
        question=question,
        creator_user_id=user.user_id,
        when_closes=when_closes,
        status="open",
        b=config.DEFAULT_LIQUIDITY_B,
    )
    db.session.add(m)
    db.session.flush()
    
    symbols = [s.strip() for s in outcomes_csv.split(",") if s.strip()]
    if len(symbols) < 2:
        raise PredictionsError('need at least 2 outcomes (e.g. "TEAM_A,TEAM_B" or "A,B,DRAW")')
    
    for sym in symbols:
        db.session.add(Outcome(market_id=m.market_id, symbol=sym, q=0.0))
    
    return f"Created market {market_name}. Trading locks {utils.dt_to_string(when_closes)} ({when_closes} UTC)"


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
    qs = [float(o.q) for o in outcomes]
    b = float(m.b)
    
    logger.info(f"BUY: qs raw from db: {[(o.symbol, o.q, type(o.q)) for o in outcomes]}")
    logger.info(f"BUY: qs converted to float: {qs}")
    logger.info(f"BUY: b={b}, type(b)={type(b)}")
    
    if b <= 0:
        raise PredictionsError("Invalid market params")
    
    # Test LMSR directly
    test_cost = lmsr_cost(qs, b)
    logger.info(f"BUY: lmsr_cost(qs, b) initial = {test_cost}")
    
    if math.isinf(test_cost):
        raise PredictionsError(f"Market state invalid: lmsr_cost returned inf")
    
    logger.info(f"BUY: market state qs={qs}, b={b}, spend={spend_f}")
    
    # Find a suitable high bound by trying small seeds then doubling.
    def safe_buy_cost(qs, b, idx, dq_val):
        try:
            return buy_cost(qs, b, idx, dq_val)
        except Exception:
            logger.exception("BUY: buy_cost raised")
            return math.inf

    # Try a set of increasing seed values to find any finite cost
    seed_values = [1e-12, 1e-9, 1e-6, 1e-4, 1e-2, 1e-1, 1.0]
    low = 0.0
    high = None
    best_dq = 0.0
    best_cost = 0.0

    for seed in seed_values:
        c = safe_buy_cost(qs, b, target_idx, seed)
        logger.info(f"BUY: seed try seed={seed}, cost={c}")
        if math.isfinite(c) and c > 0:
            if c > spend_f:
                high = seed
                break
            low = seed
            best_dq = seed
            best_cost = c
            high = seed * 2.0
            break

    if high is None:
        # Nothing worked from seeds; market parameters likely invalid
        raise PredictionsError("Market invalid or illiquid (cannot compute trade costs)")

    # Expand high until cost(high) > spend_f or high reaches a safety cap
    cap = 1e12
    while True:
        c = safe_buy_cost(qs, b, target_idx, high)
        logger.info(f"BUY: expand high={high}, cost={c}")
        if math.isinf(c):
            # try halving once; if still inf, fail
            high = high / 2.0
            if high <= low or high < 1e-18:
                raise PredictionsError("Market parameters lead to invalid cost calculations")
            break
        if c > spend_f:
            break
        if high > cap:
            break
        low = high
        best_dq = high
        best_cost = c
        high = high * 2.0

    # Binary search between low and high
    for iteration in range(60):
        mid = (low + high) / 2.0
        cost = safe_buy_cost(qs, b, target_idx, mid)
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
        if u:
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
    m.when_resolved = utils.now()
    
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
    
    m.when_cancelled = utils.now()
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
        cycle.when_closed = utils.now()
        cycle.median_bets = 0
        cycle.winner_slack_id = None
        return f"🏁 Closed {cycle.key} (no participants)"
    
    from statistics import median
    bet_counts = [r.bet_count for r in rows]
    med_val = int(median(sorted(bet_counts))) if bet_counts else 0
    
    eligible = [r for r in rows if r.bet_count > med_val]
    winner = max(eligible, key=lambda r: r.balance, default=None)
    
    cycle.median_bets = med_val
    cycle.when_closed = utils.now()
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
        if u:
            eligible_mark = "✅" if r.bet_count > med_val else "—"
            lines.append(f"{i}. <@{u.slack_id}>  {r.balance:.2f}  (bets {r.bet_count}) {eligible_mark}")
    
    return "\n".join(lines)
