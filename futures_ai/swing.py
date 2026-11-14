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


def simulate_swing(cfg: dict, p: dict, sessions: list, record: bool = False,
                   start_td=None, close_at_end: bool = False, schedule: list | None = None) -> dict:
  """Runs the swing strategy over chronologically ordered sessions (each with 1-min bars).

  schedule: optional [(start_datetime, params)] sorted by time; from each session whose first bar ends at or
  after start_datetime the matching params apply (open positions carry over, as in the live engine after a
  rule change). p applies before the first entry.
  p["sl_slip"]: points beyond the stop (or the gapped open) at which a stop fills; default 1.
  """
  base_p = p
  pv = cfg["point_value"]
  capital = cfg["capital_per_ai_twd"] if "capital_per_ai_twd" in cfg else round(cfg["initial_margin_twd"] * 1.5)
  maint = cfg.get("maintenance_margin_twd", 26900)
  h60: list[Bar] = []  # completed 60-minute bars across sessions
  closes: list[float] = []
  trs: list[float] = []
  pos = None  # dict(side, entry, stop, best, cost_in, init_sl_pts)
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

  def push_60(bar60: Bar):
    prev_c = h60[-1].c if h60 else bar60.o
    trs.append(max(bar60.h - bar60.l, abs(bar60.h - prev_c), abs(bar60.l - prev_c)))
    h60.append(bar60)
    closes.append(bar60.c)

  for si, s in enumerate(sessions):
    if schedule and s.bars:
      p = base_p
      for ts, q in schedule:
        if s.bars[0].end >= ts:
          p = q
    month = active_month(s.td)
    prev_month = month
    nxt_month = active_month(sessions[si + 1].td) if si + 1 < len(sessions) else month
    start = s.bars[0].end - timedelta(minutes=1)
    start = start.replace(hour=8, minute=45, second=0, microsecond=0) if s.name == "day" else \
        start.replace(hour=15, minute=0, second=0, microsecond=0)
    bucket = None
    agg = None
    for i, x in enumerate(s.bars):
      # ---- manage open position on every 1-minute bar ----
      if pos:
        d = 1 if pos["side"] == "B" else -1
        hit = x.l <= pos["stop"] if d == 1 else x.h >= pos["stop"]
        tp = pos.get("tp")
        if not hit and tp and ((d == 1 and x.h >= tp) or (d == -1 and x.l <= tp)):
          slip = 0 if p.get("tp_limit") else 1
          close_pos(tp - d * slip, "take_profit", x.end)
          continue
        if hit:
          slip = p.get("sl_slip", 1)
          px = (min(pos["stop"], x.o) - slip) if d == 1 else (max(pos["stop"], x.o) + slip)
          close_pos(px, "trail_stop", x.end)
        else:
          worst = x.l if d == 1 else x.h
          worst_pnl = (worst - pos["entry"]) * d * pv - pos["cost_in"]
          if capital + worst_pnl < maint:
            margin_calls += 1
            close_pos(worst - d * 2, "margin_call", x.end)
          else:
            pos["best"] = max(pos["best"], x.h) if d == 1 else min(pos["best"], x.l)
            atr = trs and sum(trs[-14:]) / len(trs[-14:]) or 50
            trail_dist = p["atr_k"] * atr
            if p.get("max_sl_pts"):
              trail_dist = min(trail_dist, p["max_sl_pts"])
            new_stop = round(pos["best"] - d * trail_dist)
            be_r = p.get("be_r", 0.7)
            if be_r:
              init_sl = pos.get("init_sl_pts", trail_dist)
              if (pos["best"] - pos["entry"]) * d >= be_r * init_sl:
                be_px = round(pos["entry"] + d * 6)
                new_stop = max(new_stop, be_px) if d == 1 else min(new_stop, be_px)
            if p.get("gb_keep") and pos.get("tp"):  # Give-back exit: lock gb_keep of the peak profit.
              peak_fav = (pos["best"] - pos["entry"]) * d
              if peak_fav >= p.get("gb_act", 0.5) * abs(pos["tp"] - pos["entry"]):
                lock = round(pos["entry"] + d * p["gb_keep"] * peak_fav)
                new_stop = max(new_stop, lock) if d == 1 else min(new_stop, lock)
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
          init_sl = round(p["atr_k"] * atr)
          if p.get("max_sl_pts"):
            init_sl = min(init_sl, round(p["max_sl_pts"]))
          pos = {"side": pending["side"], "entry": px, "stop": round(px - d * init_sl), "best": px,
                 "init_sl_pts": init_sl, "cost_in": cost(cfg, px), "ts": x.end,
                 "tp": round(px + d * p["tp_atr"] * atr) if p.get("tp_atr") else None}
          pending = None
        elif x.end >= pending["expires"]:
          pending = None
      # ---- build 60-minute bars aligned to the session start ----
      b_idx = max(0, int((x.end - start).total_seconds() - 1) // 3600)
      b_end = min(s.close, start + timedelta(hours=b_idx + 1))
      if agg is not None and b_idx != bucket:
        push_60(agg)
        agg = None
      if agg is None:
        agg, bucket = Bar(b_end, x.o, x.h, x.l, x.c, x.v), b_idx
      else:
        agg = Bar(b_end, agg.o, max(agg.h, x.h), min(agg.l, x.l), x.c, agg.v + x.v)
      complete = (x.end >= b_end) or (i == len(s.bars) - 1)
      if not complete:
        continue
      push_60(agg)
      agg = None
      if start_td is not None and s.td < start_td:
        continue
      if len(h60) < max(60, p["lookback"] + 1):
        continue
      last_session_bar = (x.end >= s.close) or (i == len(s.bars) - 1)
      if last_session_bar:
        continue  # Closing 60-min bar updates h60 only; no new orders at session close.
      ef, es = ema_series(closes[-200:], 20)[-1], ema_series(closes[-200:], 60)[-1]
      atr = sum(trs[-14:]) / len(trs[-14:])
      if pos or pending:
        continue
      hi = max(b.h for b in h60[-p["lookback"] - 1:-1])
      lo = min(b.l for b in h60[-p["lookback"] - 1:-1])
      side = None
      used_reentry = False
      if reentry:
        if reentry == "B" and ef > es:
          side = "B"
          used_reentry = True
        elif reentry == "S" and ef < es and p["direction"] == "both":
          side = "S"
          used_reentry = True
        reentry = None
      if side is None:
        if h60[-1].c > hi and ef > es:
          side = "B"
        elif h60[-1].c < lo and ef < es and p["direction"] == "both":
          side = "S"
      if side is None:
        continue
      min_er = p.get("min_er", 0.20)
      if not used_reentry and min_er and len(closes) >= 11:
        er_w = closes[-11:]
        path = sum(abs(er_w[k] - er_w[k - 1]) for k in range(1, len(er_w)))
        er10 = abs(er_w[-1] - er_w[0]) / path if path else 0.0
        if er10 < min_er:
          continue
      max_ext = p.get("max_ext_atr", 3.5)
      if not used_reentry and max_ext and atr > 0 and abs(h60[-1].c - ef) > max_ext * atr:
        continue
      d = 1 if side == "B" else -1
      if p.get("entry") == "limit":
        pending = {"side": side, "price": round(h60[-1].c - d * p["limit_atr"] * atr), "expires": x.end + timedelta(minutes=60)}
      else:
        px = h60[-1].c + d * 2
        init_sl = round(p["atr_k"] * atr)
        if p.get("max_sl_pts"):
          init_sl = min(init_sl, round(p["max_sl_pts"]))
        pos = {"side": side, "entry": px, "stop": round(px - d * init_sl), "best": px,
               "init_sl_pts": init_sl, "cost_in": cost(cfg, px), "ts": x.end,
               "tp": round(px + d * p["tp_atr"] * atr) if p.get("tp_atr") else None}
    # ---- end of session: cancel unfilled limit orders & handle contract roll ----
    pending = None
    if pos and nxt_month != month:
      side = pos["side"]
      last = s.bars[-1]
      close_pos(last.c - (2 if side == "B" else -2), "roll", last.end)
      reentry = side
  if close_at_end and pos and sessions and sessions[-1].bars:
    last = sessions[-1].bars[-1]
    close_pos(last.c - (2 if pos["side"] == "B" else -2), "window_end", last.end)
  return {"pnl": pnl, "trades": trades, "wins": wins, "maxdd": mdd, "margin_calls": margin_calls,
          "open_pos": pos, "log": log}


def swing_grid() -> list[dict]:
  return [dict(lookback=lb, atr_k=k, direction=dr, entry=en, limit_atr=0.3)
          for lb, k, dr, en in itertools.product([10, 20, 40], [2.0, 3.0, 4.0], ["both", "long"], ["market", "limit"])]
