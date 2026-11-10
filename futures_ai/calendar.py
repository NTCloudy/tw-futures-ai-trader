"""TAIFEX trading calendar: sessions, trading day, settlement (3rd Wednesday) and rollover."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

TPE = timezone(timedelta(hours=8))
DAY_OPEN, DAY_CLOSE = time(8, 45), time(13, 45)
NIGHT_OPEN, NIGHT_CLOSE = time(15, 0), time(5, 0)
MONTH_CODES = "ABCDEFGHIJKL"  # Jan..Dec

# Taiwan Futures Exchange (TAIFEX) scheduled market holidays (weekday closures).
CLOSED_DAYS: frozenset[str] = frozenset({
    # 2025
    "2025-01-01", "2025-01-23", "2025-01-24", "2025-01-27", "2025-01-28", "2025-01-29",
    "2025-01-30", "2025-01-31", "2025-02-28", "2025-04-03", "2025-04-04", "2025-05-01",
    "2025-05-30", "2025-09-29", "2025-10-06", "2025-10-10", "2025-10-24", "2025-12-25",
    # 2026
    "2026-01-01", "2026-02-12", "2026-02-13", "2026-02-16", "2026-02-17", "2026-02-18",
    "2026-02-19", "2026-02-20", "2026-02-27", "2026-04-03", "2026-04-06", "2026-05-01",
    "2026-06-19", "2026-09-25", "2026-09-28", "2026-10-09", "2026-10-26", "2026-12-25",
    # 2027
    "2027-01-01", "2027-02-03", "2027-02-04", "2027-02-05", "2027-02-08", "2027-02-09",
    "2027-02-10", "2027-03-01", "2027-04-05", "2027-04-06", "2027-04-30", "2027-06-09",
    "2027-09-15", "2027-09-28", "2027-10-11", "2027-10-25", "2027-12-24",
})


def is_business_day(d: date) -> bool:
  return d.weekday() < 5 and d.isoformat() not in CLOSED_DAYS


def now_tpe() -> datetime:
  return datetime.now(TPE)


def next_weekday(d: date) -> date:
  d += timedelta(days=1)
  while not is_business_day(d):
    d += timedelta(days=1)
  return d


def prev_weekdays(d: date, n: int) -> date:
  while n > 0:
    d -= timedelta(days=1)
    if is_business_day(d):
      n -= 1
  return d


def session_of(ts: datetime) -> tuple[str | None, datetime | None, datetime | None]:
  """Returns (name, start, end) for the session containing ts, or (None, None, None)."""
  ts = ts.astimezone(TPE)
  d, t = ts.date(), ts.time()
  if is_business_day(d) and DAY_OPEN <= t < DAY_CLOSE:
    return "day", datetime.combine(d, DAY_OPEN, TPE), datetime.combine(d, DAY_CLOSE, TPE)
  if is_business_day(d) and t >= NIGHT_OPEN:
    return "night", datetime.combine(d, NIGHT_OPEN, TPE), datetime.combine(d + timedelta(days=1), NIGHT_CLOSE, TPE)
  prev = d - timedelta(days=1)
  if t < NIGHT_CLOSE and is_business_day(prev):
    return "night", datetime.combine(prev, NIGHT_OPEN, TPE), datetime.combine(d, NIGHT_CLOSE, TPE)
  return None, None, None


def trading_day(ts: datetime) -> date:
  """TAIFEX assigns a night session to the next business day."""
  name, start, _ = session_of(ts)
  if name == "night":
    return next_weekday(start.date())
  d = ts.astimezone(TPE).date()
  return d if is_business_day(d) else next_weekday(d)


def third_wednesday(year: int, month: int) -> date:
  d = date(year, month, 1)
  first_wed = d + timedelta(days=(2 - d.weekday()) % 7)
  w = first_wed + timedelta(days=14)
  while not is_business_day(w):
    w += timedelta(days=1)
  return w


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


# ---- time of day buckets (shared by live trading and training) ----
def us_dst(d) -> bool:
  import calendar
  if isinstance(d, datetime):
    d = d.date()
  m2 = [day for day in range(8, 15) if calendar.weekday(d.year, 3, day) == 6][0]
  n1 = [day for day in range(1, 8) if calendar.weekday(d.year, 11, day) == 6][0]
  return date(d.year, 3, m2) <= d < date(d.year, 11, n1)


def us_open_at(ts: datetime) -> datetime:
  """US cash open in Taipei time for the night session containing ts (21:30 in US DST, else 22:30)."""
  d = ts.date() if ts.time() >= time(15, 0) else ts.date() - timedelta(days=1)
  hm = (21, 30) if us_dst(d) else (22, 30)
  return datetime.combine(d, time(*hm), TPE)


def tod_bucket(ts: datetime) -> str:
  t = ts.time()
  if time(8, 45) <= t <= time(9, 30):
    return "day_open"
  if time(9, 30) < t <= time(13, 45):
    return "day"
  if time(15, 0) <= t <= time(16, 0):
    return "night_open"
  mins = (ts - us_open_at(ts)).total_seconds() / 60
  if 0 < mins <= 60:
    return "us_open"
  return "night_pre_us" if mins <= 0 else "night_late"


