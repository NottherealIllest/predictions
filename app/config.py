import os
import pytz

# Database
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql:///predictionslocal")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

SQLALCHEMY_DATABASE_URI = DATABASE_URL
SQLALCHEMY_TRACK_MODIFICATIONS = False
SQLALCHEMY_ENGINE_OPTIONS = {
    "pool_pre_ping": True,
    "pool_recycle": 300,
    "pool_timeout": 20,
    "max_overflow": 0,
}

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
