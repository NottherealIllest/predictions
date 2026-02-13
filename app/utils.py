import datetime
import parsedatetime
from . import config
from .exceptions import PredictionsError

def now() -> datetime.datetime:
    """Current UTC time (naive)."""
    return datetime.datetime.utcnow()


def utc_naive(dt_aware: datetime.datetime) -> datetime.datetime:
    """Convert timezone-aware datetime to naive UTC for DB storage."""
    return dt_aware.astimezone(config.UTC_TZ).replace(tzinfo=None)


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
        return config.LONDON_TZ.localize(dt)
    except ValueError:
        pass
    
    # Fall back to natural language parsing
    cal = parsedatetime.Calendar()
    base = datetime.datetime.now(config.LONDON_TZ)
    dt, status = cal.parseDT(event_str, tzinfo=config.LONDON_TZ, sourceTime=base)
    if status == 0:
        raise PredictionsError(f'Couldn\'t interpret "{event_str}" as a datetime')
    if dt.tzinfo is None:
        dt = config.LONDON_TZ.localize(dt)
    return dt

def cycle_key_for_dt(dt_london: datetime.datetime) -> str:
    """Return cycle key (YYYY-MM) for a given London time."""
    return dt_london.strftime("%Y-%m")

def month_bounds_london(year: int, month: int) -> tuple[datetime.datetime, datetime.datetime]:
    """Return (start, end) datetimes for a month in London timezone."""
    start = config.LONDON_TZ.localize(datetime.datetime(year, month, 1))
    if month == 12:
        end = config.LONDON_TZ.localize(datetime.datetime(year + 1, 1, 1))
    else:
        end = config.LONDON_TZ.localize(datetime.datetime(year, month + 1, 1))
    return start, end
