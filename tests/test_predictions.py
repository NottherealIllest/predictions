"""Comprehensive tests for the Predictions app.

Covers: create, list, show, buy, sell, resolve, cancel, balance, leaderboard,
close_month, multi-user interactions, and edge cases.

Run with:
    python3 -m pytest tests/test_predictions.py -v
"""

import os
import pytest
import datetime
import math

# ============================================================================
# Fixtures
# ============================================================================

from app import create_app, db
import app as app_module
from app.exceptions import PredictionsError
from app.models import (
    User, Market, Outcome, Position, Trade, UserCycle, Cycle,
    get_or_create_user, get_or_create_cycle, get_or_create_usercycle,
    ensure_daily_topup_for_usercycle, get_market_or_raise, get_outcomes, market_is_closed,
)
from app.lmsr import lmsr_cost, lmsr_prices, buy_cost, sell_refund
from app import config


@pytest.fixture(scope="session")
def flask_app():
    """Create the Flask application with an in-memory SQLite DB."""
    application = create_app(test_config={
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_ENGINE_OPTIONS": {},
        "TESTING": True,
    })
    with application.app_context():
        db.create_all()
        yield application


@pytest.fixture(autouse=True)
def app_ctx(flask_app):
    """Push an app context and clean up after each test."""
    with flask_app.app_context():
        yield
        db.session.rollback()
        # Delete all rows for full isolation between tests
        for table in reversed(db.metadata.sorted_tables):
            db.session.execute(table.delete())
        db.session.commit()


def alice():
    """Get or create user Alice (call inside a test, within app context)."""
    return get_or_create_user("ALICE", "alice")


def bob():
    """Get or create user Bob."""
    return get_or_create_user("BOB", "bob")


def charlie():
    """Get or create user Charlie."""
    return get_or_create_user("CHARLIE", "charlie")


def make_market(user, name="test-market", question="Will it happen?",
                event_time="2099-01-01 12:00", lock="1h", outcomes="YES,NO"):
    """Helper to create a market through the command."""
    out = app_module.create(user, name, question, event_time, lock, outcomes)
    db.session.flush()
    return out


# ============================================================================
# HELP
# ============================================================================

class TestHelp:
    def test_help_returns_usage_info(self):
        out = app_module.help(alice())
        assert "/predict list" in out
        assert "/predict create" in out
        assert "/predict buy" in out
        assert "/predict sell" in out
        assert "/predict balance" in out
        assert "/predict leaderboard" in out
        assert "/predict resolve" in out
        assert "/predict cancel" in out


# ============================================================================
# CREATE
# ============================================================================

class TestCreate:
    def test_basic_create(self):
        out = make_market(alice())
        assert "Created market test-market" in out
        assert "Trading locks" in out

    def test_create_with_multiple_outcomes(self):
        out = make_market(alice(), name="multi", outcomes="A,B,C,D")
        assert "Created market multi" in out
        outcomes = get_outcomes(get_market_or_raise("multi"))
        assert len(outcomes) == 4
        assert [o.symbol for o in outcomes] == ["A", "B", "C", "D"]

    def test_create_duplicate_name_raises(self):
        a = alice()
        make_market(a)
        with pytest.raises(PredictionsError, match="already exists"):
            make_market(a)

    def test_create_single_outcome_raises(self):
        with pytest.raises(PredictionsError, match="at least 2 outcomes"):
            make_market(alice(), outcomes="ONLY_ONE")

    def test_create_two_outcome_minimum(self):
        out = make_market(alice(), outcomes="YES,NO")
        assert "Created market" in out

    def test_create_with_default_lock(self):
        """If rest has 1 element, it's treated as outcomes_csv with default lock."""
        out = app_module.create(alice(), "def-lock", "Question?", "2099-01-01 12:00", "YES,NO")
        assert "Created market def-lock" in out

    def test_create_missing_outcomes_raises(self):
        with pytest.raises(PredictionsError):
            app_module.create(alice(), "bad-market", "Q?", "2099-01-01 12:00")


# ============================================================================
# LIST
# ============================================================================

class TestList:
    def test_list_empty(self):
        out = app_module.list(alice())
        assert "no active markets" in out

    def test_list_shows_open_markets(self):
        a = alice()
        make_market(a, name="mkt1")
        make_market(a, name="mkt2")
        out = app_module.list(a)
        assert "mkt1" in out
        assert "mkt2" in out

    def test_list_excludes_cancelled(self):
        a = alice()
        make_market(a, name="alive")
        make_market(a, name="dead")
        app_module.cancel(a, "dead")
        db.session.flush()
        out = app_module.list(a)
        assert "alive" in out
        assert "dead" not in out

    def test_list_excludes_resolved(self):
        a = alice()
        make_market(a, name="resolved-mkt", outcomes="YES,NO")
        app_module.resolve(a, "resolved-mkt", "YES")
        db.session.flush()
        out = app_module.list(a)
        # Resolved markets have status != 'open', so filter excludes them
        assert "resolved-mkt" not in out


# ============================================================================
# SHOW
# ============================================================================

class TestShow:
    def test_show_basic(self):
        a = alice()
        make_market(a, question="Rain tomorrow?", outcomes="YES,NO")
        out = app_module.show(a, "test-market")
        assert "Rain tomorrow?" in out
        assert "YES:" in out
        assert "NO:" in out
        assert "50.00%" in out  # Equal initial probs for 2 outcomes

    def test_show_three_outcomes(self):
        a = alice()
        make_market(a, name="triple", outcomes="A,B,C")
        out = app_module.show(a, "triple")
        assert "33.33%" in out

    def test_show_unknown_market(self):
        with pytest.raises(PredictionsError, match="unknown market"):
            app_module.show(alice(), "nonexistent")

    def test_show_position_after_buy(self):
        a = alice()
        make_market(a, outcomes="YES,NO")
        app_module.buy(a, "test-market", "YES", "50")
        db.session.flush()
        out = app_module.show(a, "test-market")
        assert "Your position:" in out
        assert "YES shares:" in out

    def test_show_resolved_market(self):
        a = alice()
        make_market(a, outcomes="YES,NO")
        app_module.resolve(a, "test-market", "YES")
        db.session.flush()
        out = app_module.show(a, "test-market")
        assert "Resolved" in out

    def test_show_cancelled_market(self):
        a = alice()
        make_market(a, outcomes="YES,NO")
        app_module.cancel(a, "test-market")
        db.session.flush()
        out = app_module.show(a, "test-market")
        assert "Cancelled" in out


# ============================================================================
# BUY
# ============================================================================

class TestBuy:
    def test_basic_buy(self):
        a = alice()
        make_market(a, outcomes="YES,NO")
        out = app_module.buy(a, "test-market", "YES", "100")
        assert "Bought" in out
        assert "YES" in out
        assert "Cost: 100.00" in out

    def test_buy_updates_balance(self):
        a = alice()
        make_market(a, outcomes="YES,NO")
        cycle = get_or_create_cycle()
        uc = get_or_create_usercycle(cycle, a)
        ensure_daily_topup_for_usercycle(uc)
        initial_balance = uc.balance
        app_module.buy(a, "test-market", "YES", "50")
        db.session.flush()
        assert uc.balance == pytest.approx(initial_balance - 50, abs=1)

    def test_buy_creates_position(self):
        a = alice()
        make_market(a, outcomes="YES,NO")
        app_module.buy(a, "test-market", "YES", "10")
        db.session.flush()
        pos = Position.query.filter(Position.user_id == a.user_id).first()
        assert pos is not None
        assert pos.shares > 0

    def test_buy_creates_trade_log(self):
        a = alice()
        make_market(a, outcomes="YES,NO")
        app_module.buy(a, "test-market", "YES", "10")
        db.session.flush()
        trades = Trade.query.filter(Trade.user_id == a.user_id).all()
        assert len(trades) == 1
        assert trades[0].side == "buy"
        assert trades[0].shares > 0
        assert trades[0].amount > 0

    def test_buy_moves_price_up(self):
        a = alice()
        make_market(a, outcomes="YES,NO")
        m = get_market_or_raise("test-market")
        outcomes = get_outcomes(m)
        prices_before = lmsr_prices([o.q for o in outcomes], m.b)

        app_module.buy(a, "test-market", "YES", "50")
        db.session.flush()
        outcomes = get_outcomes(m)
        prices_after = lmsr_prices([o.q for o in outcomes], m.b)

        # YES price should go up, NO price should go down
        assert prices_after[0] > prices_before[0]
        assert prices_after[1] < prices_before[1]

    def test_buy_unknown_market(self):
        with pytest.raises(PredictionsError, match="unknown market"):
            app_module.buy(alice(), "nope", "YES", "10")

    def test_buy_unknown_outcome(self):
        a = alice()
        make_market(a, outcomes="YES,NO")
        with pytest.raises(PredictionsError, match="Unknown outcome"):
            app_module.buy(a, "test-market", "MAYBE", "10")

    def test_buy_zero_spend(self):
        a = alice()
        make_market(a, outcomes="YES,NO")
        with pytest.raises(PredictionsError, match="Spend must be > 0"):
            app_module.buy(a, "test-market", "YES", "0")

    def test_buy_negative_spend(self):
        a = alice()
        make_market(a, outcomes="YES,NO")
        with pytest.raises(PredictionsError, match="Spend must be > 0"):
            app_module.buy(a, "test-market", "YES", "-10")

    def test_buy_invalid_amount_string(self):
        a = alice()
        make_market(a, outcomes="YES,NO")
        with pytest.raises(PredictionsError, match="Invalid amount"):
            app_module.buy(a, "test-market", "YES", "abc")

    def test_buy_insufficient_balance(self):
        a = alice()
        make_market(a, outcomes="YES,NO")
        with pytest.raises(PredictionsError, match="Insufficient balance"):
            app_module.buy(a, "test-market", "YES", "999999")

    def test_buy_case_insensitive_outcome(self):
        a = alice()
        make_market(a, outcomes="YES,NO")
        out = app_module.buy(a, "test-market", "yes", "10")
        assert "Bought" in out

    def test_multiple_buys_accumulate_position(self):
        a = alice()
        make_market(a, outcomes="YES,NO")
        app_module.buy(a, "test-market", "YES", "10")
        db.session.flush()
        pos = Position.query.filter(Position.user_id == a.user_id).first()
        shares1 = pos.shares

        app_module.buy(a, "test-market", "YES", "10")
        db.session.flush()
        assert pos.shares > shares1

    def test_buy_increments_bet_count(self):
        a = alice()
        make_market(a, outcomes="YES,NO")
        cycle = get_or_create_cycle()
        uc = get_or_create_usercycle(cycle, a)
        assert uc.bet_count == 0
        app_module.buy(a, "test-market", "YES", "10")
        db.session.flush()
        assert uc.bet_count == 1
        app_module.buy(a, "test-market", "YES", "10")
        db.session.flush()
        assert uc.bet_count == 2

    def test_buy_on_cancelled_market_raises(self):
        a = alice()
        make_market(a, outcomes="YES,NO")
        app_module.cancel(a, "test-market")
        db.session.flush()
        with pytest.raises(PredictionsError, match="closed"):
            app_module.buy(a, "test-market", "YES", "10")

    def test_buy_on_resolved_market_raises(self):
        a = alice()
        make_market(a, outcomes="YES,NO")
        app_module.resolve(a, "test-market", "YES")
        db.session.flush()
        with pytest.raises(PredictionsError, match="closed"):
            app_module.buy(a, "test-market", "YES", "10")

    def test_buy_small_amount(self):
        a = alice()
        make_market(a, outcomes="YES,NO")
        out = app_module.buy(a, "test-market", "YES", "0.01")
        assert "Bought" in out

    def test_buy_three_outcome_market(self):
        a = alice()
        make_market(a, name="triple", outcomes="A,B,C")
        out = app_module.buy(a, "triple", "B", "50")
        assert "Bought" in out
        assert "B" in out

    def test_predict_alias_works(self):
        """The `predict` alias should map to `buy`."""
        a = alice()
        make_market(a, outcomes="YES,NO")
        out = app_module.predict(a, "test-market", "YES", "10")
        assert "Bought" in out


# ============================================================================
# SELL
# ============================================================================

class TestSell:
    def _buy_first(self, user, market_name="test-market", outcome="YES", spend="100"):
        app_module.buy(user, market_name, outcome, spend)
        db.session.flush()

    def test_basic_sell(self):
        a = alice()
        make_market(a, outcomes="YES,NO")
        self._buy_first(a)
        pos = Position.query.filter(Position.user_id == a.user_id).first()
        shares_to_sell = str(round(pos.shares / 2, 2))
        out = app_module.sell(a, "test-market", "YES", shares_to_sell)
        assert "Sold" in out
        assert "Refund" in out

    def test_sell_updates_balance_and_position(self):
        a = alice()
        make_market(a, outcomes="YES,NO")
        self._buy_first(a)
        cycle = get_or_create_cycle()
        uc = get_or_create_usercycle(cycle, a)
        balance_before = uc.balance
        pos = Position.query.filter(Position.user_id == a.user_id).first()
        shares_before = pos.shares
        sell_amount = round(shares_before / 2, 2)
        app_module.sell(a, "test-market", "YES", str(sell_amount))
        db.session.flush()
        assert uc.balance > balance_before  # Got refund
        assert pos.shares < shares_before   # Shares decreased

    def test_sell_creates_trade_log(self):
        a = alice()
        make_market(a, outcomes="YES,NO")
        self._buy_first(a)
        pos = Position.query.filter(Position.user_id == a.user_id).first()
        app_module.sell(a, "test-market", "YES", str(round(pos.shares / 2, 2)))
        db.session.flush()
        sell_trades = Trade.query.filter(
            Trade.user_id == a.user_id, Trade.side == "sell"
        ).all()
        assert len(sell_trades) == 1

    def test_sell_unknown_market(self):
        with pytest.raises(PredictionsError, match="unknown market"):
            app_module.sell(alice(), "ghost", "YES", "1")

    def test_sell_unknown_outcome(self):
        a = alice()
        make_market(a, outcomes="YES,NO")
        with pytest.raises(PredictionsError, match="unknown outcome"):
            app_module.sell(a, "test-market", "MAYBE", "1")

    def test_sell_zero_shares(self):
        a = alice()
        make_market(a, outcomes="YES,NO")
        with pytest.raises(PredictionsError, match="shares must be > 0"):
            app_module.sell(a, "test-market", "YES", "0")

    def test_sell_negative_shares(self):
        a = alice()
        make_market(a, outcomes="YES,NO")
        with pytest.raises(PredictionsError, match="shares must be > 0"):
            app_module.sell(a, "test-market", "YES", "-5")

    def test_sell_invalid_string(self):
        a = alice()
        make_market(a, outcomes="YES,NO")
        with pytest.raises(PredictionsError, match="not a valid float"):
            app_module.sell(a, "test-market", "YES", "xyz")

    def test_sell_more_shares_than_owned(self):
        a = alice()
        make_market(a, outcomes="YES,NO")
        self._buy_first(a, spend="10")
        pos = Position.query.filter(Position.user_id == a.user_id).first()
        too_many = str(pos.shares + 100)
        with pytest.raises(PredictionsError, match="not enough shares"):
            app_module.sell(a, "test-market", "YES", too_many)

    def test_sell_without_position(self):
        a = alice()
        b = bob()
        make_market(a, outcomes="YES,NO")
        with pytest.raises(PredictionsError, match="not enough shares"):
            app_module.sell(b, "test-market", "YES", "1")

    def test_sell_on_cancelled_market(self):
        a = alice()
        make_market(a, outcomes="YES,NO")
        self._buy_first(a)
        app_module.cancel(a, "test-market")
        db.session.flush()
        with pytest.raises(PredictionsError, match="closed"):
            app_module.sell(a, "test-market", "YES", "1")

    def test_sell_case_insensitive(self):
        a = alice()
        make_market(a, outcomes="YES,NO")
        self._buy_first(a)
        pos = Position.query.filter(Position.user_id == a.user_id).first()
        out = app_module.sell(a, "test-market", "yes", str(round(pos.shares / 2, 2)))
        assert "Sold" in out

    def test_sell_moves_price_down(self):
        a = alice()
        make_market(a, outcomes="YES,NO")
        self._buy_first(a)
        m = get_market_or_raise("test-market")
        outcomes = get_outcomes(m)
        prices_before = lmsr_prices([o.q for o in outcomes], m.b)

        pos = Position.query.filter(Position.user_id == a.user_id).first()
        app_module.sell(a, "test-market", "YES", str(round(pos.shares / 2, 2)))
        db.session.flush()
        outcomes = get_outcomes(m)
        prices_after = lmsr_prices([o.q for o in outcomes], m.b)
        # YES price should go down after sell
        assert prices_after[0] < prices_before[0]


# ============================================================================
# RESOLVE
# ============================================================================

class TestResolve:
    def test_basic_resolve(self):
        a = alice()
        make_market(a, outcomes="YES,NO")
        out = app_module.resolve(a, "test-market", "YES")
        assert "resolved: YES" in out

    def test_resolve_awards_winning_shares(self):
        a = alice()
        b = bob()
        make_market(a, outcomes="YES,NO")
        app_module.buy(b, "test-market", "YES", "50")
        db.session.flush()
        cycle = get_or_create_cycle()
        uc_bob = get_or_create_usercycle(cycle, b)
        balance_before = uc_bob.balance
        app_module.resolve(a, "test-market", "YES")
        db.session.flush()
        assert uc_bob.balance > balance_before

    def test_resolve_sets_status(self):
        a = alice()
        make_market(a, outcomes="YES,NO")
        app_module.resolve(a, "test-market", "YES")
        db.session.flush()
        m = get_market_or_raise("test-market")
        assert m.status == "resolved"
        assert m.resolved_outcome_id is not None
        assert m.when_resolved is not None

    def test_resolve_already_resolved(self):
        a = alice()
        make_market(a, outcomes="YES,NO")
        app_module.resolve(a, "test-market", "YES")
        db.session.flush()
        with pytest.raises(PredictionsError, match="already resolved"):
            app_module.resolve(a, "test-market", "YES")

    def test_resolve_cancelled_market(self):
        a = alice()
        make_market(a, outcomes="YES,NO")
        app_module.cancel(a, "test-market")
        db.session.flush()
        with pytest.raises(PredictionsError, match="cancelled"):
            app_module.resolve(a, "test-market", "YES")

    def test_resolve_wrong_user(self):
        a = alice()
        b = bob()
        make_market(a, outcomes="YES,NO")
        with pytest.raises(PredictionsError, match="Only"):
            app_module.resolve(b, "test-market", "YES")

    def test_resolve_unknown_outcome(self):
        a = alice()
        make_market(a, outcomes="YES,NO")
        with pytest.raises(PredictionsError, match="unknown outcome"):
            app_module.resolve(a, "test-market", "MAYBE")

    def test_resolve_unknown_market(self):
        with pytest.raises(PredictionsError, match="unknown market"):
            app_module.resolve(alice(), "ghost-market", "YES")


# ============================================================================
# CANCEL
# ============================================================================

class TestCancel:
    def test_basic_cancel(self):
        a = alice()
        make_market(a, outcomes="YES,NO")
        out = app_module.cancel(a, "test-market")
        assert "cancelled" in out

    def test_cancel_sets_status(self):
        a = alice()
        make_market(a, outcomes="YES,NO")
        app_module.cancel(a, "test-market")
        db.session.flush()
        m = get_market_or_raise("test-market")
        assert m.status == "cancelled"
        assert m.when_cancelled is not None

    def test_cancel_already_cancelled(self):
        a = alice()
        make_market(a, outcomes="YES,NO")
        app_module.cancel(a, "test-market")
        db.session.flush()
        with pytest.raises(PredictionsError, match="already cancelled"):
            app_module.cancel(a, "test-market")

    def test_cancel_wrong_user(self):
        a = alice()
        b = bob()
        make_market(a, outcomes="YES,NO")
        with pytest.raises(PredictionsError, match="Only"):
            app_module.cancel(b, "test-market")

    def test_cancel_unknown_market(self):
        with pytest.raises(PredictionsError, match="unknown market"):
            app_module.cancel(alice(), "ghost-market")


# ============================================================================
# BALANCE
# ============================================================================

class TestBalance:
    def test_balance_shows_info(self):
        out = app_module.balance(alice())
        assert "Balance:" in out
        assert "Bets this month:" in out
        assert "Cycle:" in out

    def test_balance_decreases_after_buy(self):
        a = alice()
        make_market(a, outcomes="YES,NO")
        out1 = app_module.balance(a)
        app_module.buy(a, "test-market", "YES", "100")
        db.session.flush()
        out2 = app_module.balance(a)
        # Extract numeric balance from "Balance: xxx.xx"
        b1 = float(out1.split("Balance: ")[1].split(" |")[0])
        b2 = float(out2.split("Balance: ")[1].split(" |")[0])
        assert b2 < b1


# ============================================================================
# LEADERBOARD
# ============================================================================

class TestLeaderboard:
    def test_leaderboard_with_players(self):
        a = alice()
        b = bob()
        make_market(a, outcomes="YES,NO")
        app_module.buy(a, "test-market", "YES", "50")
        app_module.buy(b, "test-market", "NO", "30")
        db.session.flush()
        out = app_module.leaderboard(a)
        assert "Leaderboard" in out
        assert "ALICE" in out
        assert "BOB" in out


# ============================================================================
# CLOSE_MONTH
# ============================================================================

class TestCloseMonth:
    def test_close_month_basic(self):
        a = alice()
        out = app_module.close_month(a)
        assert "Closed" in out or "closed" in out

    def test_close_month_with_participants(self):
        a = alice()
        b = bob()
        make_market(a, outcomes="YES,NO")
        app_module.buy(a, "test-market", "YES", "10")
        app_module.buy(b, "test-market", "NO", "10")
        db.session.flush()
        out = app_module.close_month(a)
        assert "closed" in out.lower()

    def test_close_month_already_closed(self):
        a = alice()
        app_module.close_month(a)
        db.session.flush()
        with pytest.raises(PredictionsError, match="already closed"):
            app_module.close_month(a)


# ============================================================================
# MULTI-USER INTERACTIONS
# ============================================================================

class TestMultiUser:
    def test_two_users_buy_opposite_sides(self):
        """Alice buys YES, Bob buys NO — LMSR has order-dependent slippage,
        so prices won't perfectly re-center, but they stay within range."""
        a = alice()
        b = bob()
        make_market(a, outcomes="YES,NO")
        app_module.buy(a, "test-market", "YES", "100")
        db.session.flush()
        app_module.buy(b, "test-market", "NO", "100")
        db.session.flush()
        m = get_market_or_raise("test-market")
        outcomes = get_outcomes(m)
        prices = lmsr_prices([o.q for o in outcomes], m.b)
        # Both prices should still sum to 1
        assert sum(prices) == pytest.approx(1.0, abs=1e-9)
        # Neither price should be extreme
        assert 0.1 < prices[0] < 0.9
        assert 0.1 < prices[1] < 0.9

    def test_three_users_buy_same_outcome_drives_price(self):
        a = alice()
        b = bob()
        c = charlie()
        make_market(a, outcomes="YES,NO")

        app_module.buy(a, "test-market", "YES", "50")
        db.session.flush()
        m = get_market_or_raise("test-market")
        p1 = lmsr_prices([o.q for o in get_outcomes(m)], m.b)[0]

        app_module.buy(b, "test-market", "YES", "50")
        db.session.flush()
        p2 = lmsr_prices([o.q for o in get_outcomes(m)], m.b)[0]

        app_module.buy(c, "test-market", "YES", "50")
        db.session.flush()
        p3 = lmsr_prices([o.q for o in get_outcomes(m)], m.b)[0]

        assert p1 < p2 < p3

    def test_buy_then_sell_roundtrip_loses_some(self):
        """Buy and sell near-all shares — should get back less than spent (slippage)."""
        a = alice()
        make_market(a, outcomes="YES,NO")
        cycle = get_or_create_cycle()
        uc = get_or_create_usercycle(cycle, a)
        ensure_daily_topup_for_usercycle(uc)
        initial_balance = uc.balance

        app_module.buy(a, "test-market", "YES", "100")
        db.session.flush()
        after_buy_balance = uc.balance

        pos = Position.query.filter(Position.user_id == a.user_id).first()
        sell_shares = round(pos.shares * 0.99, 4)
        app_module.sell(a, "test-market", "YES", str(sell_shares))
        db.session.flush()

        assert uc.balance > after_buy_balance
        assert uc.balance < initial_balance  # Slippage loss

    def test_resolve_pays_winner_not_loser(self):
        a = alice()
        b = bob()
        make_market(a, outcomes="YES,NO")
        app_module.buy(a, "test-market", "YES", "50")
        app_module.buy(b, "test-market", "NO", "50")
        db.session.flush()

        cycle = get_or_create_cycle()
        uc_alice = get_or_create_usercycle(cycle, a)
        uc_bob = get_or_create_usercycle(cycle, b)
        balance_alice_before = uc_alice.balance
        balance_bob_before = uc_bob.balance

        app_module.resolve(a, "test-market", "YES")
        db.session.flush()

        # Alice held YES (winner) => gets shares added
        assert uc_alice.balance > balance_alice_before
        # Bob held NO (loser) => gets nothing
        assert uc_bob.balance == pytest.approx(balance_bob_before, abs=0.01)


# ============================================================================
# LMSR UNIT TESTS
# ============================================================================

class TestLMSR:
    def test_cost_empty(self):
        assert lmsr_cost([], 100) == 0.0

    def test_cost_zeros(self):
        c = lmsr_cost([0, 0], 100)
        assert math.isfinite(c)

    def test_prices_sum_to_one(self):
        prices = lmsr_prices([10, 20, 5], 100)
        assert sum(prices) == pytest.approx(1.0, abs=1e-9)

    def test_prices_equal_when_quantities_equal(self):
        prices = lmsr_prices([0, 0], 100)
        assert prices[0] == pytest.approx(0.5, abs=1e-9)
        assert prices[1] == pytest.approx(0.5, abs=1e-9)

    def test_prices_three_way_equal(self):
        prices = lmsr_prices([0, 0, 0], 100)
        for p in prices:
            assert p == pytest.approx(1.0 / 3.0, abs=1e-9)

    def test_buy_cost_positive(self):
        c = buy_cost([0, 0], 100, 0, 10)
        assert c > 0
        assert math.isfinite(c)

    def test_buy_cost_invalid_idx(self):
        c = buy_cost([0, 0], 100, 5, 10)
        assert math.isinf(c)

    def test_buy_cost_zero_dq(self):
        c = buy_cost([0, 0], 100, 0, 0)
        assert math.isinf(c)

    def test_sell_refund_positive(self):
        r = sell_refund([100, 0], 100, 0, 50)
        assert r > 0

    def test_sell_refund_too_many(self):
        r = sell_refund([10, 0], 100, 0, 20)
        assert r == 0.0

    def test_cost_monotonic(self):
        c1 = buy_cost([0, 0], 100, 0, 10)
        c2 = buy_cost([0, 0], 100, 0, 50)
        c3 = buy_cost([0, 0], 100, 0, 100)
        assert c1 < c2 < c3

    def test_higher_b_means_more_liquidity(self):
        """Higher b = deeper liquidity = lower price impact for same trade."""
        c_low_b = buy_cost([0, 0], 10, 0, 10)
        c_high_b = buy_cost([0, 0], 1000, 0, 10)
        assert c_high_b < c_low_b


# ============================================================================
# UTILS TESTS
# ============================================================================

class TestUtils:
    def test_dt_to_string_future(self):
        from app.utils import now, dt_to_string
        future = now() + datetime.timedelta(hours=2, seconds=5)
        s = dt_to_string(future)
        assert "from now" in s

    def test_dt_to_string_past(self):
        from app.utils import now, dt_to_string
        past = now() - datetime.timedelta(hours=2, seconds=5)
        s = dt_to_string(past)
        assert "ago" in s

    def test_dt_to_string_days(self):
        from app.utils import now, dt_to_string
        future = now() + datetime.timedelta(days=3, seconds=5)
        s = dt_to_string(future)
        assert "3d from now" == s

    def test_parse_lock_delta(self):
        from app.utils import parse_lock_delta
        assert parse_lock_delta("15m") == datetime.timedelta(minutes=15)
        assert parse_lock_delta("2h") == datetime.timedelta(hours=2)
        assert parse_lock_delta("1d") == datetime.timedelta(days=1)

    def test_parse_lock_delta_invalid(self):
        from app.utils import parse_lock_delta
        with pytest.raises(PredictionsError, match="lock must look like"):
            parse_lock_delta("abc")

    def test_cycle_key(self):
        from app.utils import cycle_key_for_dt
        dt = datetime.datetime(2026, 2, 13, 10, 0)
        assert cycle_key_for_dt(dt) == "2026-02"
