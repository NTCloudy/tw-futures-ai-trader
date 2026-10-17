"""Swing trend-following simulator (方向 A) + limit-order entry option (方向 B).

Strategy (decided on 60-minute bars, built from 1-minute bars inside each session):
- Long when the 60-min close breaks above the highest high of the prior N bars and EMA20 > EMA60;
  short is the mirror (optional). Positions are HELD OVERNIGHT across day/night sessions.
- Exit only by an ATR trailing stop (k x ATR14 on 60-min bars), checked on every 1-minute bar.
  A session that opens beyond the stop fills at the opening price (gap risk is modelled).
- Rollover: the position is closed at the end of the last session of the expiring month and, if the
  trend is still intact (EMA20 vs EMA60), re-opened on the first 60-min bar of the new contract.
- Margin-call guard: if the open loss exceeds capital - maintenance margin, the position is closed.
- Entry: market (+2 points slippage) or limit (pull back limit_atr x ATR within the next 60 minutes,
  filled at the limit price, no slippage; unfilled orders are cancelled).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import timedelta

from .calendar import active_month
from .feed import Bar


def ema_series(values: list[float], n: int) -> list[float]:
  k, out = 2 / (n + 1), []
  for v in values:
    out.append(v if not out else v * k + out[-1] * (1 - k))
  return out


@dataclass
class SwingParams:
  lookback: int = 20
  atr_k: float = 3.0
  direction: str = "both"  # "both" | "long"
  entry: str = "market"  # "market" | "limit"
  limit_atr: float = 0.3


def cost(cfg, price):
  return cfg["commission_per_side_twd"] + round(price * cfg["point_value"] * cfg["tax_rate"])


def simulate_swing(cfg: dict, p: dict, sessions: list, record: bool = False) -> dict:
  """Runs the swing strategy over chronologically ordered sessions (each with 1-min bars)."""
  pv = cfg["point_value"]
  capital = cfg["capital_per_ai_twd"] if "capital_per_ai_twd" in cfg else round(cfg["initial_margin_twd"] * 1.5)
  maint = 26900
  max_open_loss = capital - maint
  h60: list[Bar] = []  # completed 60-minute bars across sessions
  closes: list[float] = []
  trs: list[float] = []
  pos = None  # dict(side, entry, stop, best, cost_in)
  pending = None  # limit order dict(side, price, expires_at)
  reentry = None  # side to re-open after a roll exit
  pnl = peak = mdd = 0.0
  trades = wins = margin_calls = 0
  log = []
  prev_month = None

  def close_pos(px, why, ts):
    nonlocal pos, pnl, peak, mdd, trades, wins
    d = 1 if pos["side"] == "B" else -1
    net = (px - pos["entry"]) * d * pv - pos["cost_in"] - cost(cfg, px)
    pnl += net
    trades += 1
    wins += net > 0
    peak = max(peak, pnl)
    mdd = max(mdd, peak - pnl)
    if record:
      log.append((pos["ts"], ts, pos["side"], pos["entry"], px, net, why))
    pos = None

  for si, s in enumerate(sessions):
    month = active_month(s.td)
    if prev_month is not None and month != prev_month and pos:
      pass  # (closed at the end of the previous session below)
    prev_month = month
    nxt_month = active_month(sessions[si + 1].td) if si + 1 < len(sessions) else month
    start = s.bars[0].end - timedelta(minutes=1)
    bucket = None
    agg = None
    for i, x in enumerate(s.bars):
      # ---- manage open position on every 1-minute bar ----
      if pos:
        d = 1 if pos["side"] == "B" else -1
        hit = x.l <= pos["stop"] if d == 1 else x.h >= pos["stop"]
        tp = pos.get("tp")
        if not hit and tp and ((d == 1 and x.h >= tp) or (d == -1 and x.l <= tp)):
          close_pos(tp, "take_profit", x.end)
          continue
        if hit:
          px = (min(pos["stop"], x.o) - 1) if d == 1 else (max(pos["stop"], x.o) + 1)
          close_pos(px, "trail_stop", x.end)
        else:
          worst = x.l if d == 1 else x.h
          if (pos["entry"] - worst) * d * pv > max_open_loss:
            margin_calls += 1
            close_pos(worst - d * 2, "margin_call", x.end)
          else:
            pos["best"] = max(pos["best"], x.h) if d == 1 else min(pos["best"], x.l)
            atr = trs and sum(trs[-14:]) / len(trs[-14:]) or 50
            new_stop = round(pos["best"] - d * p["atr_k"] * atr)
            if (d == 1 and new_stop > pos["stop"]) or (d == -1 and new_stop < pos["stop"]):
              pos["stop"] = new_stop
      # ---- pending limit entry ----
      if pending and not pos:
        if x.end > pending["expires"]:
          pending = None
        elif (pending["side"] == "B" and x.l <= pending["price"] - p.get("fill_through", 0)) or (
            pending["side"] == "S" and x.h >= pending["price"] + p.get("fill_through", 0)):
          px = pending["price"]
          atr = sum(trs[-14:]) / len(trs[-14:])
          d = 1 if pending["side"] == "B" else -1
          pos = {"side": pending["side"], "entry": px, "stop": round(px - d * p["atr_k"] * atr), "best": px,
                 "cost_in": cost(cfg, px), "ts": x.end, "tp": round(px + d * p["tp_atr"] * atr) if p.get("tp_atr") else None}
          pending = None
      # ---- build 60-minute bars aligned to the session start ----
      b_idx = int((x.end - start).total_seconds() // 3600 - (1 if (x.end - start).total_seconds() % 3600 == 0 else 0))
      if agg is None or b_idx != bucket:
        agg, bucket = Bar(x.end, x.o, x.h, x.l, x.c, x.v), b_idx
      else:
        agg = Bar(x.end, agg.o, max(agg.h, x.h), min(agg.l, x.l), x.c, agg.v + x.v)
      complete = ((x.end - start).total_seconds() % 3600 == 0) or i == len(s.bars) - 1
      if not complete:
        continue
      prev_c = h60[-1].c if h60 else agg.o
      trs.append(max(agg.h - agg.l, abs(agg.h - prev_c), abs(agg.l - prev_c)))
      h60.append(agg)
      closes.append(agg.c)
      agg = None
      if len(h60) < max(60, p["lookback"] + 1):
        continue
      ef, es = ema_series(closes[-200:], 20)[-1], ema_series(closes[-200:], 60)[-1]
      atr = sum(trs[-14:]) / len(trs[-14:])
      last_session_bar = i == len(s.bars) - 1
      if pos or pending:
        continue
      if last_session_bar and nxt_month != month:
        continue  # never open right before a roll
      hi = max(b.h for b in h60[-p["lookback"] - 1:-1])
      lo = min(b.l for b in h60[-p["lookback"] - 1:-1])
      side = None
      if reentry:
        if reentry == "B" and ef > es:
          side = "B"
        elif reentry == "S" and ef < es and p["direction"] == "both":
          side = "S"
        reentry = None
      if side is None:
        if h60[-1].c > hi and ef > es:
          side = "B"
        elif h60[-1].c < lo and ef < es and p["direction"] == "both":
          side = "S"
      if side is None:
        continue
      d = 1 if side == "B" else -1
      if p.get("entry") == "limit":
        pending = {"side": side, "price": round(h60[-1].c - d * p["limit_atr"] * atr), "expires": x.end + timedelta(minutes=60)}
      else:
        px = h60[-1].c + d * 2
        pos = {"side": side, "entry": px, "stop": round(px - d * p["atr_k"] * atr), "best": px,
               "cost_in": cost(cfg, px), "ts": x.end, "tp": round(px + d * p["tp_atr"] * atr) if p.get("tp_atr") else None}
    # ---- end of session: roll handling ----
    if pos and nxt_month != month:
      side = pos["side"]
      last = s.bars[-1]
      close_pos(last.c - (2 if side == "B" else -2), "roll", last.end)
      reentry = side
    pending = None if (pending and nxt_month != month) else pending
  return {"pnl": pnl, "trades": trades, "wins": wins, "maxdd": mdd, "margin_calls": margin_calls, "log": log}


def swing_grid() -> list[dict]:
  return [dict(lookback=lb, atr_k=k, direction=dr, entry=en, limit_atr=0.3)
          for lb, k, dr, en in itertools.product([10, 20, 40], [2.0, 3.0, 4.0], ["both", "long"], ["market", "limit"])]
