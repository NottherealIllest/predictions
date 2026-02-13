from flask import Flask
from . import config, models, routes, tasks, commands

def create_app(test_config=None):
    app = Flask(__name__)
    
    # Configure app
    app.config["SQLALCHEMY_DATABASE_URI"] = config.SQLALCHEMY_DATABASE_URI
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = config.SQLALCHEMY_TRACK_MODIFICATIONS
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = config.SQLALCHEMY_ENGINE_OPTIONS
    # Ensure config values are present in app.config for access elsewhere if needed
    app.config["STARTING_BALANCE"] = config.STARTING_BALANCE
    app.config["DAILY_TOPUP"] = config.DAILY_TOPUP
    app.config["BALANCE_CAP"] = config.BALANCE_CAP
    app.config["DEFAULT_LOCK"] = config.DEFAULT_LOCK
    app.config["DEFAULT_LIQUIDITY_B"] = config.DEFAULT_LIQUIDITY_B
    app.config["TASK_SECRET"] = config.TASK_SECRET
    
    if test_config:
        app.config.update(test_config)
    
    # Initialize extensions
    models.db.init_app(app)
    
    # Register blueprints
    app.register_blueprint(routes.bp)
    app.register_blueprint(tasks.bp)
    
    return app

# Expose key symbols for easier imports (and tests)
db = models.db
PredictionsError = commands.PredictionsError

# Re-export command handling functions for tests if they import directly from app
from .models import (
    get_or_create_user as lookup_or_create_user, # alias for test compatibility if needed
    get_or_create_cycle,
    get_or_create_usercycle,
    ensure_daily_topup_for_usercycle,
    get_market_or_raise,
    get_outcomes,
    market_is_closed
)
from .utils import now, dt_to_string
from .commands import (
    help, list, show, create, buy, sell, balance, leaderboard, resolve, cancel, close_month,
    commands as command_registry
)

predict = buy # Alias for backward compatibility if tests expect it

# For backward compatibility with tests that might import 'app' and expect 'app' instance
# But better to fix tests to use create_app or just import app from wsgi or similar.
# However, "import app" will import this package.
# If tests expect "app.app", I can instantiate one here, but better not to have side effects on import.
# I will fix tests.
