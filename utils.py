"""Utility functions for the Todo Bot."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pymongo.collection import Collection
from config import DEFAULT_TZ, READABLE_DATE_FORMAT
from database import users_col


def parse_deadline(date_str: str, tz_str: str) -> datetime:
    """Parse a datetime string 'YYYY-MM-DD HH:MM' in user's timezone and return a datetime
    normalized to UTC."""
    try:
        dt = datetime.strptime(date_str, '%Y-%m-%d %H:%M')
    except ValueError:
        raise ValueError("Format harus: YYYY-MM-DD HH:MM")

    try:
        tz = ZoneInfo(tz_str)
    except Exception:
        raise ValueError(f"Timezone tidak dikenali: {tz_str}")

    local_dt = dt.replace(tzinfo=tz)
    utc_dt = local_dt.astimezone(ZoneInfo('UTC'))
    return utc_dt


def ensure_aware_utc(dt: datetime) -> datetime:
    """Ensure a datetime is timezone-aware in UTC.

    PyMongo returns naive datetimes (no tzinfo). Treat naive datetimes as UTC and
    return an aware datetime in UTC so arithmetic with aware datetimes works.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ZoneInfo('UTC'))
    return dt.astimezone(ZoneInfo('UTC'))


def ensure_aware_tz(dt: datetime) -> datetime:
    """Ensure a datetime is timezone-aware in the default timezone.

    PyMongo returns naive datetimes (no tzinfo). Treat naive datetimes as in
    the default timezone and return an aware datetime in that timezone.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ZoneInfo(DEFAULT_TZ))
    return dt.astimezone(ZoneInfo(DEFAULT_TZ))


def human_delta(delta: timedelta) -> str:
    """Format a timedelta to a human-readable string like '2d 5h 30m'."""
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        return 'sudah lewat'
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return ' '.join(parts)


def format_date(dt: datetime) -> str:
    """Format datetime to 'Senin 30 Desember 2025' format (Indonesian)."""
    # Indonesian day names
    days_id = ['Senin', 'Selasa', 'Rabu', 'Kamis', 'Jumat', 'Sabtu', 'Minggu']
    # Indonesian month names
    months_id = [
        'Januari', 'Februari', 'Maret', 'April', 'Mei', 'Juni',
        'Juli', 'Agustus', 'September', 'Oktober', 'November', 'Desember'
    ]
    
    day_name = days_id[dt.weekday()]
    month_name = months_id[dt.month - 1]
    
    return f"{day_name} {dt.day} {month_name} {dt.year}"


async def get_user_timezone(user_id: int) -> str:
    """Get user's timezone from database, or return default timezone."""
    doc = users_col.find_one({'user_id': user_id})
    return doc.get('timezone', DEFAULT_TZ) if doc else DEFAULT_TZ
