"""Night-session regime filter (user decision 2026-11-18, plan A).

The opening-breakout / pullback AIs assume the night session travels in one direction. When recent night
sessions have been choppy (net move small relative to the range), they stop entering for the whole night.

  efficiency of a session = |last close - first open| / (highest high - lowest low)
  blocked tonight         = mean efficiency of the previous K completed night sessions < MIN

K = 5 and MIN = 0.40 were chosen on the first half of a one-year backtest and confirmed on the second half
(out of sample). Live trading (engine + trader) and the simulator (wfo) use the same definition.
"""

from __future__ import annotations

from datetime import datetime, timedelta

NIGHT_EFF_K = 5
NIGHT_EFF_MIN = 0.40
KEEP = 7  # Previous night efficiencies stored per session (enough for K up to 7).


def session_efficiency(bars) -> float:
  """|last close - first open| / range of one session's bars (0.0 for an empty or flat session)."""
  if not bars:
    return 0.0
  rg = max(b.h for b in bars) - min(b.l for b in bars)
  return abs(bars[-1].c - bars[0].o) / rg if rg else 0.0


def prior_night_effs(bars, before: datetime, keep: int = KEEP) -> list[float]:
  """Efficiencies of the last `keep` completed night sessions that started before `before` (oldest first).
  `bars` = 1-minute bars of several sessions (any order)."""
  from .calendar import session_of
  nights: dict[datetime, list] = {}
  for b in sorted(bars, key=lambda x: x.end):
    name, start, _ = session_of(b.end - timedelta(seconds=1))
    if name == "night" and start is not None and start < before:
      nights.setdefault(start, []).append(b)
  return [session_efficiency(nights[k]) for k in sorted(nights)][-keep:]


def mean_prior(effs: list[float] | None, k: int) -> float | None:
  """Mean of the last k values; None when there is no history (the filter then does not block)."""
  if not effs:
    return None
  last = effs[-k:]
  return sum(last) / len(last)


def blocked(effs: list[float] | None, rule) -> tuple[bool, float | None]:
  """rule = (k, min). Returns (blocked, mean)."""
  if not rule:
    return False, None
  m = mean_prior(effs, rule[0])
  return (m is not None and m < rule[1]), m
