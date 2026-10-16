"""TAIFEX trading calendar: sessions, trading day, settlement (3rd Wednesday) and rollover."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

TPE = timezone(timedelta(hours=8))
DAY_OPEN, DAY_CLOSE = time(8, 45), time(13, 45)
NIGHT_OPEN, NIGHT_CLOSE = time(15, 0), time(5, 0)
MONTH_CODES = "ABCDEFGHIJKL"  # Jan..Dec


def now_tpe() -> datetime:
  return datetime.now(TPE)


def next_weekday(d: date) -> date:
  d += timedelta(days=1)
  while d.weekday() >= 5:
    d += timedelta(days=1)
  return d


def prev_weekdays(d: date, n: int) -> date:
  while n > 0:
    d -= timedelta(days=1)
    if d.weekday() < 5:
      n -= 1
  return d


def session_of(ts: datetime) -> tuple[str | None, datetime | None, datetime | None]:
  """Returns (name, start, end) for the session containing ts, or (None, None, None)."""
  ts = ts.astimezone(TPE)
  d, t, wd = ts.date(), ts.time(), ts.weekday()
  if wd < 5 and DAY_OPEN <= t < DAY_CLOSE:
    return "day", datetime.combine(d, DAY_OPEN, TPE), datetime.combine(d, DAY_CLOSE, TPE)
  if wd < 5 and t >= NIGHT_OPEN:
    return "night", datetime.combine(d, NIGHT_OPEN, TPE), datetime.combine(d + timedelta(days=1), NIGHT_CLOSE, TPE)
  prev = d - timedelta(days=1)
  if t < NIGHT_CLOSE and prev.weekday() < 5:
    return "night", datetime.combine(prev, NIGHT_OPEN, TPE), datetime.combine(d, NIGHT_CLOSE, TPE)
  return None, None, None


def trading_day(ts: datetime) -> date:
  """TAIFEX assigns a night session to the next business day."""
  name, start, _ = session_of(ts)
  if name == "night":
    return next_weekday(start.date())
  return ts.astimezone(TPE).date()


def third_wednesday(year: int, month: int) -> date:
  d = date(year, month, 1)
  first_wed = d + timedelta(days=(2 - d.weekday()) % 7)
  return first_wed + timedelta(days=14)


def active_month(td: date, roll_days_before: int = 1) -> tuple[int, int]:
  """Near month, rolled to the next month `roll_days_before` business days before settlement."""
  settle = third_wednesday(td.year, td.month)
  if td >= prev_weekdays(settle, roll_days_before):
    return (td.year + 1, 1) if td.month == 12 else (td.year, td.month + 1)
  return td.year, td.month


def contract_code(product: str, year: int, month: int) -> str:
  """e.g. ('TMF', 2026, 10) -> 'TMFJ6' (TAIFEX / Shioaji code format)."""
  return f"{product}{MONTH_CODES[month - 1]}{year % 10}"


def rollover_info(ts: datetime, product: str, roll_days_before: int = 1) -> dict:
  td = trading_day(ts)
  y, m = active_month(td, roll_days_before)
  settle = third_wednesday(y, m)
  near_settle = third_wednesday(td.year, td.month)
  roll_day = prev_weekdays(near_settle, roll_days_before)
  return {
      "trading_day": td.isoformat(),
      "contract": contract_code(product, y, m),
      "delivery_month": f"{y}{m:02d}",
      "settlement_date": settle.isoformat(),
      "next_roll_day": (roll_day if td < roll_day else prev_weekdays(settle, roll_days_before)).isoformat(),
      "rolled_this_month": td >= roll_day,
  }
