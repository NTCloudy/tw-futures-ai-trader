"""Weekly self-learning for the slower AIs (Nova: night opening-range breakout, Omega: 60-min swing).

They trade too rarely for the 10-day practice / 5-day exam walk-forward used by Alpha and Sigma, so they use
longer rolling windows (Nova 40 -> 20 days, Omega 60 -> 20 days). In every window the best grid parameters
on the practice days are tested on the following unseen exam days; the summed exam P&L is compared with the
CURRENT parameters on exactly the same exam days. The AI switches only if the trained choice scored better on
unseen data AND the newest recommendation differs from the current parameters.
"""

from __future__ import annotations

import itertools

from . import wfo
from .swing import simulate_swing

NOVA_BASE = dict(anchor="night", regimes=("trend", "chop"), flip=(), limit_atr=0.0, limit_min=5, fill_through=1)
OMEGA_BASE = dict(direction="both", entry="limit", fill_through=1)
LIVE_DEFAULTS = {
    "nova-nightorb": dict(range_min=30, window=600, sl_frac=1.0, tp_mult=3.0),
    "omega-swing": dict(lookback=10, atr_k=4.0, limit_atr=0.3, tp_atr=10.0),
}
WINDOWS = {"nova-nightorb": (40, 20), "omega-swing": (60, 20)}
NAMES = {"nova-nightorb": "Nova 夜盤開盤突破", "omega-swing": "Omega 波段順勢"}


def grid(ai: str) -> list[dict]:
  if ai == "nova-nightorb":
    return [dict(range_min=r, window=w, sl_frac=f, tp_mult=t)
            for r, w, f, t in itertools.product([15, 30], [300, 600], [0.75, 1.0], [2.0, 3.0, 4.0])]
  return [dict(lookback=lb, atr_k=k, limit_atr=0.3, tp_atr=tp)
          for lb, k, tp in itertools.product([10, 20], [3.0, 4.0, 5.0], [6.0, 10.0, 15.0])]


def _sim(cfg: dict, ai: str, live: dict, sessions: list) -> dict:
  if ai == "nova-nightorb":
    return wfo.simulate(cfg, "anchor-orb", {**NOVA_BASE, **live}, sessions)
  return simulate_swing(cfg, {**OMEGA_BASE, **live}, sessions)


def _score(r: dict) -> float:
  return -1e9 if r["trades"] < 2 else r["pnl"] - 0.5 * r["maxdd"]


def train(cfg: dict, ai: str, sessions: list, current: dict | None) -> dict:
  current = {**LIVE_DEFAULTS[ai], **(current or {})}
  train_days, test_days = WINDOWS[ai]
  days = sorted({s.td for s in sessions})
  by_day = {d: [s for s in sessions if s.td == d] for d in days}
  pick = lambda ds: [s for d in ds for s in by_day[d]]
  oos = cur_oos = 0.0
  trades = wins = 0
  start = train_days
  while start + test_days <= len(days):
    tr, te = pick(days[start - train_days:start]), pick(days[start:start + test_days])
    best = max(grid(ai), key=lambda p: _score(_sim(cfg, ai, p, tr)))
    r = _sim(cfg, ai, best, te)
    oos += r["pnl"]
    trades += r["trades"]
    wins += r["wins"]
    cur_oos += _sim(cfg, ai, current, te)["pnl"]
    start += test_days
  latest = max(grid(ai), key=lambda p: _score(_sim(cfg, ai, p, pick(days[-train_days:]))))
  return {"ai": ai, "oos_pnl": round(oos), "cur_oos": round(cur_oos), "trades": trades, "wins": wins,
          "latest": latest, "current": current,
          "switch": oos > cur_oos and any(latest.get(k) != current.get(k) for k in latest)}
