"""Monthly report (LINE): each AI's P&L for the calendar month, futures and crypto, sent right after the
last futures session that starts in that month closes (e.g. the Friday night session ending Saturday 05:00).

Monthly P&L = change in the AI's equity (realized + unrealized) from the start of the month to now, so
positions held across the month boundary (Omega, BTC AIs) are counted in the month they moved.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .calendar import TPE, next_weekday

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


def _equity_change(rows: list[tuple[str, float]], start: datetime) -> float:
  before = [v for ts, v in rows if _ts(ts) < start]
  base = before[-1] if before else 0.0
  return (rows[-1][1] - base) if rows else 0.0


def futures_month(ledger, names: dict, start: datetime) -> list[tuple[str, float]]:
  out = []
  for ai_id, name in names.items():
    rows = [(r["ts"], r["realized_total"] + r["unrealized"])
            for r in ledger.rows("SELECT ts, realized_total, unrealized FROM equity WHERE ai=? ORDER BY ts", (ai_id,))]
    out.append((name, _equity_change(rows, start)))
  return out


def crypto_month(start: datetime) -> list[tuple[str, float]]:
  if not BTC_DB.exists():
    return []
  c = sqlite3.connect(BTC_DB)
  out = []
  for ai_id, name in BTC_NAMES.items():
    rows = list(c.execute("SELECT ts, value FROM equity WHERE ai=? ORDER BY ts", (ai_id,)))
    out.append((name, _equity_change(rows, start)))
  return out


def monthly_text(ledger, names: dict, month: date) -> str:
  start = datetime.combine(month.replace(day=1), time(0, 0), TPE)
  fut = futures_month(ledger, names, start)
  cry = crypto_month(start)
  lines = [f"【{month.month} 月月報】", "📊 期貨（NT$）"]
  lines += [f"{n.replace(' AI', '')}：{v:+,.0f}" for n, v in fut]
  lines.append(f"合計：{sum(v for _, v in fut):+,.0f}")
  if cry:
    lines += ["", "₿ 虛擬貨幣（USDT）"]
    lines += [f"{n}：{v:+,.0f}" for n, v in cry]
    lines.append(f"合計：{sum(v for _, v in cry):+,.0f}")
  return "\n".join(lines)
