"""Monthly report (LINE): each AI's P&L for the calendar month, futures and crypto, sent right after the
last futures session that starts in that month closes (e.g. the Friday night session ending Saturday 05:00).

Monthly P&L = change in the AI's equity (realized + unrealized) from the start of the month to now, so
positions held across the month boundary (Omega, BTC AIs) are counted in the month they moved.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .calendar import TPE, is_business_day, next_weekday

BTC_DB = Path(__file__).resolve().parents[2] / "btc-ai-trader" / "data" / "btc.db"
BTC_NAMES = {"momo": "🚀 Momo", "trendy": "📈 Trendy", "bounce": "🎯 Bounce"}


def month_closes_after(session_start: datetime) -> bool:
  """True if no later futures session starts in the same calendar month (weekday sessions only)."""
  s = session_start.astimezone(TPE)
  if s.hour < 12:  # Day session: the night session of the same day starts at 15:00 (same month).
    return False
  nxt = next_weekday(s.date())  # Next session: the next weekday's day session.
  return nxt.month != s.month


def _ts(s: str) -> datetime:
  t = datetime.fromisoformat(s)
  return t if t.tzinfo else t.replace(tzinfo=TPE)  # Futures equity rows are naive Taipei time.


def _last_session_end_of_month(year: int, month: int) -> datetime:
  """End timestamp of the last futures night session that starts in (year, month): next day 05:00 TPE."""
  if month == 12:
    nxt = date(year + 1, 1, 1)
  else:
    nxt = date(year, month + 1, 1)
  d = nxt - timedelta(days=1)
  while not is_business_day(d):
    d -= timedelta(days=1)
  return datetime.combine(d + timedelta(days=1), time(5, 0), TPE)


def _month_bounds(month: date) -> tuple[datetime, datetime]:
  """Returns (prev_month_close, this_month_close) aligned with futures night-session closes (05:00)."""
  prev_last = month.replace(day=1) - timedelta(days=1)
  start = _last_session_end_of_month(prev_last.year, prev_last.month)
  end = _last_session_end_of_month(month.year, month.month)
  return start, end


def _equity_change(rows: list[tuple[str, float]], start: datetime, end: datetime | None = None) -> float:
  scoped = [(ts, v) for ts, v in rows if end is None or _ts(ts) <= end]
  if not scoped:
    return 0.0
  if start.hour == 0 and start.minute == 0:
    before = [v for ts, v in scoped if _ts(ts) < start]
  else:
    before = [v for ts, v in scoped if _ts(ts) <= start]
  base = before[-1] if before else 0.0
  return scoped[-1][1] - base


def futures_month(ledger, names: dict, start: datetime, end: datetime | None = None) -> list[tuple[str, float]]:
  out = []
  for ai_id, name in names.items():
    rows = [(r["ts"], r["realized_total"] + r["unrealized"])
            for r in ledger.rows("SELECT ts, realized_total, unrealized FROM equity WHERE ai=? ORDER BY ts", (ai_id,))]
    out.append((name, _equity_change(rows, start, end)))
  return out


def crypto_month(start: datetime, end: datetime | None = None) -> list[tuple[str, float]]:
  if not BTC_DB.exists():
    return []
  c = sqlite3.connect(BTC_DB)
  try:
    out = []
    for ai_id, name in BTC_NAMES.items():
      rows = list(c.execute("SELECT ts, value FROM equity WHERE ai=? ORDER BY ts", (ai_id,)))
      out.append((name, _equity_change(rows, start, end)))
    return out
  finally:
    c.close()


def monthly_text(ledger, names: dict, month: date) -> str:
  start, end = _month_bounds(month)
  fut = futures_month(ledger, names, start, end)
  cry = crypto_month(start, end)
  lines = [f"【{month.month} 月月報】", "📊 期貨（NT$）"]
  lines += [f"{n.replace(' AI', '')}：{v:+,.0f}" for n, v in fut]
  lines.append(f"合計：{sum(v for _, v in fut):+,.0f}")
  if cry:
    lines += ["", "₿ 虛擬貨幣（USDT）"]
    lines += [f"{n}：{v:+,.0f}" for n, v in cry]
    lines.append(f"合計：{sum(v for _, v in cry):+,.0f}")
  return "\n".join(lines)


def stock_month_text(month: date) -> str | None:
  """US-stock AI monthly P&L (slow: refreshes prices), sent as a second message from a background thread."""
  from .stocks import stock_lines
  try:
    lines = stock_lines("month", month.strftime("%Y-%m"))
  except Exception as exc:
    lines = ["", f"股票月報讀取失敗：{exc}"]
  return f"【{month.month} 月月報・股票】" + "\n".join(lines) if lines else None


def regime_lines(month: date, sessions: list | None = None) -> list[str]:
  """Market context for the month (so results can later be judged against the kind of market they came from):
  trend days vs. range days, the month's net move and the average daily range vs. the rest of the data.
  A trading day (night + day session) is a trend day if |close - open| >= half of its high-low range."""
  if sessions is None:
    from .config import load_config
    from .wfo import load_sessions
    sessions = load_sessions(load_config())
  days = {}
  for s in sorted(sessions, key=lambda s: s.bars[0].end):
    if s.bars:
      days.setdefault(s.td, []).extend(s.bars)
  stats = {}
  for td, bars in days.items():
    o, c = bars[0].o, bars[-1].c
    h, l = max(b.h for b in bars), min(b.l for b in bars)
    stats[td] = (o, c, h - l, abs(c - o) >= 0.5 * (h - l) and h > l)
  mine = sorted(td for td in stats if td.year == month.year and td.month == month.month)
  if not mine:
    return []
  trend = sum(1 for td in mine if stats[td][3])
  move = stats[mine[-1]][1] - stats[mine[0]][0]
  pct = move / stats[mine[0]][0] * 100
  avg = sum(stats[td][2] for td in mine) / len(mine)
  rest = [stats[td][2] for td in stats if td not in set(mine)]
  base = f"（其他月份平均 {sum(rest) / len(rest):,.0f} 點）" if rest else ""
  return [f"🌡️ 本月行情：趨勢日 {trend} 天／盤整日 {len(mine) - trend} 天（共 {len(mine)} 天）",
          f"月漲跌 {move:+,.0f} 點（{pct:+.1f}%）｜平均每日振幅 {avg:,.0f} 點{base}"]
