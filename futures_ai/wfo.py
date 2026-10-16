"""Walk-forward optimisation (滾動前進測試 + 參數搜尋) on archived TAIFEX tick data.

For each window: search a small, fixed grid of strategy parameters on the TRAIN days (most recent
`train_days` before the test window), pick the best by risk-adjusted score (or "sit out" if no
candidate is profitable), then run that choice on the next unseen TEST days and record the result
as-is. Concatenated TEST results are the honest estimate of each AI's skill.

This is a fast array-based re-implementation of the live rules (signals, regime/trend filter,
bracket stop/take-profit with the stop assumed first when both are touched within a bar, costs,
entry caps, cool-downs, flatten before close) so that hundreds of candidates can be evaluated.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .calendar import TPE
from .config import ROOT
from .feed import Bar
from .learning import efficiency_ratio, ER_TREND

HIST = ROOT / "data" / "history"


# ---------------------------------------------------------------- data
@dataclass
class Session:
  td: date
  name: str
  code: str
  bars: list[Bar]
  close: datetime
  # precomputed features
  vwap: list[float] = None
  atr: list[float] = None
  regime: list[str] = None
  er: list[float] = None
  direction: list[int] = None
  b5: list = None
  atr5: list = None
  k_at: dict = None


def load_sessions(cfg: dict) -> list[Session]:
  from .pretrain import sessions_from_file
  out = []
  for f in sorted(HIST.glob("Daily_*.zip")):
    td = datetime.strptime(f.stem[6:], "%Y_%m_%d").date()
    for name, code, bars in sessions_from_file(f.read_bytes(), td, cfg["product"], cfg["roll_days_before"]):
      start = bars[0].end - timedelta(minutes=1)
      close = datetime.combine(start.date(), time(13, 45), TPE) if name == "day" else \
          datetime.combine(start.date() + (timedelta(days=1) if start.hour >= 15 else timedelta()), time(5, 0), TPE)
      s = Session(td, name, code, bars, close)
      precompute(s)
      out.append(s)
  out.sort(key=lambda s: s.bars[0].end)
  return out


def precompute(s: Session) -> None:
  b = s.bars
  n = len(b)
  pv = vol = 0.0
  s.vwap, s.atr, s.regime, s.er, s.direction = [], [], [], [], []
  trs = []
  b5: list[list] = []  # [end, o, h, l, c]
  for i, x in enumerate(b):
    pv += (x.h + x.l + x.c) / 3 * x.v
    vol += x.v
    s.vwap.append(pv / vol if vol else x.c)
    prev_c = b[i - 1].c if i else x.o
    trs.append(max(x.h - x.l, abs(x.h - prev_c), abs(x.l - prev_c)))
    w = trs[-14:]
    s.atr.append(sum(w) / len(w))
    key = x.end.replace(second=0, microsecond=0)
    bucket_end = key + timedelta(minutes=(5 - key.minute % 5) % 5)
    if b5 and b5[-1][0] == bucket_end:
      b5[-1][2] = max(b5[-1][2], x.h)
      b5[-1][3] = min(b5[-1][3], x.l)
      b5[-1][4] = x.c
    else:
      b5.append([bucket_end, x.o, x.h, x.l, x.c])
    if len(b5) >= 6:
      win = b5[-13:]
      closes = [y[4] for y in win]
      path = sum(abs(closes[k] - closes[k - 1]) for k in range(1, len(closes)))
      er = abs(closes[-1] - closes[0]) / path if path else 0.0
      d = 1 if closes[-1] > closes[0] else -1 if closes[-1] < closes[0] else 0
      hi, lo = max(y[2] for y in win[:-2]), min(y[3] for y in win[:-2])
      newx = (d > 0 and max(y[2] for y in win[-2:]) >= hi) or (d < 0 and min(y[3] for y in win[-2:]) <= lo)
      trend = er >= 0.30 or (newx and er >= 0.18)
    else:
      er = efficiency_ratio(b[: i + 1])
      cl = [y.c for y in b[max(0, i - 20): i + 1]]
      d = 1 if cl[-1] > cl[0] else -1 if cl[-1] < cl[0] else 0
      trend = er >= ER_TREND
    s.er.append(er)
    s.direction.append(d)
    s.regime.append("trend" if trend else "chop")
  # 5-minute decision points: 1-minute indices that complete a 5-minute bucket.
  s.b5, s.atr5, s.k_at = [], [], {}
  agg = None
  trs5 = []
  for i, x in enumerate(b):
    key = x.end.replace(second=0, microsecond=0)
    bucket_end = key + timedelta(minutes=(5 - key.minute % 5) % 5)
    if agg and agg.end == bucket_end:
      agg = Bar(bucket_end, agg.o, max(agg.h, x.h), min(agg.l, x.l), x.c, agg.v + x.v)
    else:
      agg = Bar(bucket_end, x.o, x.h, x.l, x.c, x.v)
    if x.end == bucket_end:  # bucket complete
      prev_c = s.b5[-1].c if s.b5 else agg.o
      trs5.append(max(agg.h - agg.l, abs(agg.h - prev_c), abs(agg.l - prev_c)))
      w = trs5[-14:]
      s.b5.append(agg)
      s.atr5.append(sum(w) / len(w))
      s.k_at[i] = len(s.b5) - 1


# ---------------------------------------------------------------- signals
def ema_tail(values: list[float], n: int) -> float:
  k, out = 2 / (n + 1), values[0]
  for v in values[1:]:
    out = v * k + out * (1 - k)
  return out


def clamp(x, lo, hi):
  return max(lo, min(hi, x))


def signal(ai: str, p: dict, s: Session, i: int, since_exit: int | None):
  b = s.bars
  x, prev = b[i], b[i - 1] if i else b[i]
  if ai == "alpha-orb":
    rm = p["range_minutes"]
    if i + 1 <= rm + 1 or (since_exit is not None and since_exit < 3):
      return None
    hi = max(y.h for y in b[:rm])
    lo = min(y.l for y in b[:rm])
    width = hi - lo
    if width > 450:
      return None
    sl = clamp(width * p["sl_frac"], 30, 80)
    tp = sl * p["tp_mult"]
    buf = 3
    if x.c > hi + buf and prev.c <= hi + buf and x.c > s.vwap[i]:
      return "B", sl, tp, None
    if x.c < lo - buf and prev.c >= lo - buf and x.c < s.vwap[i]:
      return "S", sl, tp, None
    return None
  if ai == "sigma-vwap":
    if i + 1 < 20 or (since_exit is not None and since_exit < 5):
      return None
    a = max(s.atr[i], 5.0)
    dev = x.c - s.vwap[i]
    sl = clamp(a * p["sl_atr"], 25, 70)
    tp = clamp(abs(dev) * p["tp_frac"], sl, sl * 2)
    if dev >= p["stretch"] * a and x.c < x.o and x.c < prev.c:
      return "S", sl, tp, None
    if dev <= -p["stretch"] * a and x.c > x.o and x.c > prev.c:
      return "B", sl, tp, None
    return None
  if ai == "gamma-scalp":
    if i + 1 < 14 or (since_exit is not None and since_exit < 1):
      return None
    closes = [y.c for y in b[max(0, i - 29): i + 1]]
    fast, slow = ema_tail(closes, 5), ema_tail(closes, 13)
    sl = max(p["sl_min"], round(1.2 * s.atr[i]))
    tp = max(30, round(sl * p["tp_ratio"]))
    if fast > slow and x.c > prev.h and x.c > x.o:
      return "B", sl, tp, None
    if fast < slow and x.c < prev.l and x.c < x.o:
      return "S", sl, tp, None
    return None
  if ai == "sigma-pullback":
    # Trend pullback: 5-min trend, price dips back to VWAP (+k*ATR) and closes back in trend direction.
    if i + 1 < 20 or s.regime[i] != "trend" or s.direction[i] == 0:
      return None
    a = max(s.atr[i], 5.0)
    vw = s.vwap[i]
    sl = clamp(a * p["sl_atr"], 20, 80)
    tp = sl * p["tp_mult"]
    trail = a * p["trail_atr"] if p.get("trail_atr") else None
    if s.direction[i] > 0 and x.l <= vw + p["pull_atr"] * a and x.c > vw and x.c > x.o:
      return "B", sl, tp, trail
    if s.direction[i] < 0 and x.h >= vw - p["pull_atr"] * a and x.c < vw and x.c < x.o:
      return "S", sl, tp, trail
    return None
  if ai == "gamma-trend":
    # Trend breakout: 5-min trend + close beyond the prior N-bar high/low; trailing stop rides the move.
    n = p["lookback"]
    if i < n + 1 or s.regime[i] != "trend":
      return None
    a = max(s.atr[i], 5.0)
    hi = max(y.h for y in b[i - n: i])
    lo = min(y.l for y in b[i - n: i])
    sl = clamp(a * p["sl_atr"], 20, 100)
    tp = sl * 4
    trail = a * p["trail_atr"]
    if s.direction[i] > 0 and x.c > hi and prev.c <= hi:
      return "B", sl, tp, trail
    if s.direction[i] < 0 and x.c < lo and prev.c >= lo:
      return "S", sl, tp, trail
    return None
  raise ValueError(ai)


def signal5(ai: str, p: dict, s: Session, k: int, i: int):
  """Signals on completed 5-minute bars (k = 5-min index, i = 1-min index completing it)."""
  b = s.b5
  x, prev = b[k], b[k - 1] if k else b[k]
  vw = s.vwap[i]
  a = max(s.atr5[k], 5.0)
  if ai == "alpha-orb":
    rb = p["range_bars"]
    if k + 1 <= rb + 1:
      return None
    hi = max(y.h for y in b[:rb])
    lo = min(y.l for y in b[:rb])
    width = hi - lo
    if width > 450:
      return None
    sl = clamp(width * p["sl_frac"], 30, 120)
    tp = sl * p["tp_mult"]
    if x.c > hi + 3 and prev.c <= hi + 3 and x.c > vw:
      return "B", sl, tp, None
    if x.c < lo - 3 and prev.c >= lo - 3 and x.c < vw:
      return "S", sl, tp, None
    return None
  if ai == "sigma-pullback":
    if k + 1 < 6 or s.regime[i] != "trend" or s.direction[i] == 0:
      return None
    sl = clamp(a * p["sl_atr"], 20, 150)
    tp = sl * p["tp_mult"]
    trail = a * p["trail_atr"] if p.get("trail_atr") else None
    if s.direction[i] > 0 and x.l <= vw + p["pull_atr"] * a and x.c > vw and x.c > x.o:
      return "B", sl, tp, trail
    if s.direction[i] < 0 and x.h >= vw - p["pull_atr"] * a and x.c < vw and x.c < x.o:
      return "S", sl, tp, trail
    return None
  if ai == "sigma-fixed":  # Sigma entries with a fixed bracket (take-profit / stop in points).
    g = signal5("sigma-pullback", p["base"], s, k, i)
    return (g[0], p["sl_pts"], p["tp_pts"], None) if g else None
  if ai in ("alpha-inverse", "sigma-inverse"):
    base = "alpha-orb" if ai == "alpha-inverse" else "sigma-pullback"
    g = signal5(base, p["base"], s, k, i)
    if not g:
      return None
    side = "S" if g[0] == "B" else "B"
    sl = g[1] * p["sl_scale"]
    return side, sl, sl * p["tp_mult"], None
  if ai == "gamma-inverse":
    # Take Gamma's breakout signal and trade the opposite direction (fade the breakout).
    g = signal5("gamma-trend", {"lookback": p["lookback"], "sl_atr": p["g_sl_atr"], "trail_atr": 4.0}, s, k, i)
    if not g:
      return None
    side = "S" if g[0] == "B" else "B"
    sl = g[1] * p["sl_scale"]
    return side, sl, sl * p["tp_mult"], None
  if ai == "gamma-trend":
    n = p["lookback"]
    if k < n + 1 or s.regime[i] != "trend":
      return None
    hi = max(y.h for y in b[k - n: k])
    lo = min(y.l for y in b[k - n: k])
    sl = clamp(a * p["sl_atr"], 20, 150)
    trail = a * p["trail_atr"]
    tpm = p.get("tp_mult", 4)
    if s.direction[i] > 0 and x.c > hi and prev.c <= hi:
      return "B", sl, sl * tpm, trail
    if s.direction[i] < 0 and x.c < lo and prev.c >= lo:
      return "S", sl, sl * tpm, trail
    return None
  raise ValueError(ai)


# ---------------------------------------------------------------- simulation
def side_cost(cfg, price):
  return cfg["commission_per_side_twd"] + round(price * cfg["point_value"] * cfg["tax_rate"])


def simulate(cfg: dict, ai: str, p: dict, sessions: list[Session]) -> dict:
  if p.get("off"):
    return {"pnl": 0.0, "trades": 0, "wins": 0, "maxdd": 0.0, "curve": []}
  pv = cfg["point_value"]
  cap_entries = min(cfg["max_entries_per_session"], p.get("max_entries", 99))
  pnl = peak = maxdd = 0.0
  trades = wins = 0
  for s in sessions:
    b = s.bars
    pos = None
    entries = 0
    last_exit_i = None
    block_side = None
    block_i = None
    for i in range(len(b)):
      x = b[i]
      mtc = (s.close - x.end).total_seconds() / 60
      if pos:  # bracket check on this bar
        side, entry, sl, tp, cost_in, trail = pos[:6]
        d = 1 if side == "B" else -1
        exit_px = kind = None
        sl_hit = x.l <= sl if d == 1 else x.h >= sl
        tp_hit = x.h >= tp if d == 1 else x.l <= tp
        if sl_hit:
          exit_px, kind = ((min(sl, x.o) - 1) if d == 1 else (max(sl, x.o) + 1)), "SL"
        elif tp_hit:
          exit_px, kind = (tp - 1) if d == 1 else (tp + 1), "TP"
        elif mtc <= cfg["flatten_minutes_before_close"] or i == len(b) - 1:
          exit_px, kind = (x.c - 2) if d == 1 else (x.c + 2), "END"
        be = p.get("be_r")  # Breakeven: once profit >= be_r x initial stop, move stop to entry (+cost cover).
        if exit_px is None and be:
          init = pos[6] if len(pos) > 6 else abs(entry - sl)
          fav = (x.h - entry) if d == 1 else (entry - x.l)
          be_px = entry + d * 6
          if fav >= be * init and ((d == 1 and sl < be_px) or (d == -1 and sl > be_px)):
            pos = (side, entry, be_px, tp, cost_in, trail, init)
            sl = be_px
        if exit_px is None and trail:  # Ratchet the stop after this bar (conservative ordering).
          new_sl = round(x.h - trail) if d == 1 else round(x.l + trail)
          if (d == 1 and new_sl > sl) or (d == -1 and new_sl < sl):
            pos = (side, entry, new_sl, tp, cost_in, trail) + tuple(pos[6:])
        if exit_px is not None:
          net = (exit_px - entry) * d * pv - cost_in - side_cost(cfg, exit_px)
          pnl += net
          trades += 1
          wins += net > 0
          peak = max(peak, pnl)
          maxdd = max(maxdd, peak - pnl)
          pos = None
          last_exit_i = i
          if kind == "SL" and ai in ("sigma-pullback", "sigma-fixed"):
            block_side, block_i = side, i
          continue
      if pos or entries >= cap_entries or mtc <= cfg["no_entry_minutes_before_close"]:
        continue
      if i not in s.k_at:  # Decisions only when a 5-minute bar completes.
        continue
      sig = signal5(ai, p, s, s.k_at[i], i)
      if not sig:
        continue
      side, sl_pts, tp_pts, trail = sig
      reg = s.regime[i]
      if reg not in p.get("regimes", ("trend", "chop")):
        continue
      if reg in p.get("flip", ()):
        side = "S" if side == "B" else "B"
      dirn = s.direction[i]
      if not ai.endswith("-inverse") and reg == "trend" and ((side == "B" and dirn < 0) or (side == "S" and dirn > 0)):
        continue
      if block_side == side:
        if any(b[k].l <= s.vwap[k] <= b[k].h for k in range(block_i + 1, i + 1)):
          block_side = None
        else:
          continue
      if ai != "sigma-fixed":  # Fixed brackets are not capped.
        remaining = max(1.0, (s.close - x.end).total_seconds() / 300)
        tp_pts = min(tp_pts, max(round(sl_pts * 1.5), round(s.atr5[s.k_at[i]] * remaining ** 0.5)))
      entry = x.c + 2 if side == "B" else x.c - 2
      d = 1 if side == "B" else -1
      pos = (side, entry, round(entry - d * sl_pts), round(entry + d * tp_pts), side_cost(cfg, entry), trail)
      entries += 1
  return {"pnl": pnl, "trades": trades, "wins": wins, "maxdd": maxdd}


# ---------------------------------------------------------------- grids & walk-forward
def grid(ai: str) -> list[dict]:
  if ai == "alpha-orb":
    g = [dict(range_bars=r, sl_frac=f, tp_mult=t, regimes=reg, flip=fl)
         for r, f, t, reg, fl in itertools.product([3, 6], [0.35, 0.5, 0.75], [1.5, 2.0, 3.0],
                                                   [("trend", "chop"), ("trend",)], [(), ("chop",)])
         if not (fl and "chop" not in reg)]
  elif ai == "sigma-pullback":
    g = [dict(pull_atr=pa, sl_atr=sa, tp_mult=tm, trail_atr=ta, regimes=("trend",), flip=())
         for pa, sa, tm, ta in itertools.product([0.2, 0.5], [1.0, 1.5, 2.0], [2.0, 3.0, 4.0], [None, 2.0, 3.0])]
  elif ai == "gamma-trend":
    g = [dict(lookback=lb, sl_atr=sa, trail_atr=ta, regimes=("trend",), flip=())
         for lb, sa, ta in itertools.product([6, 12, 24], [1.5, 2.0, 3.0], [2.0, 3.0, 4.0])]
  elif ai == "gamma-inverse":
    g = [dict(lookback=12, g_sl_atr=2.0, sl_scale=sc, tp_mult=tm, regimes=("trend",), flip=())
         for sc, tm in itertools.product([0.5, 0.75, 1.0], [1.0, 1.5, 2.0, 3.0])]
  else:
    raise ValueError(ai)
  return g + [{"off": True}]


def score(r: dict) -> float:
  if r["trades"] < 3 and not r.get("off"):
    return -1e9  # Too few trades to trust.
  return r["pnl"] - 0.5 * r["maxdd"]


def describe(ai: str, p: dict) -> str:
  if p.get("off"):
    return "暫停（所有參數組合在練習期都不賺）"
  reg = "+".join({"trend": "趨勢", "chop": "盤整"}[r] for r in p["regimes"])
  fl = ("，" + "+".join({"trend": "趨勢", "chop": "盤整"}[r] for r in p["flip"]) + "盤反向") if p.get("flip") else ""
  core = {k: v for k, v in p.items() if k not in ("regimes", "flip")}
  return f"{core}｜只做{reg}盤{fl}"


LIVE_TO_SIM = {"alpha-orb": "alpha-orb", "sigma-vwap": "sigma-pullback", "gamma-scalp": "gamma-trend"}


def walk_forward(cfg: dict, train_days: int, test_days: int, sessions: list[Session] | None = None, log=print,
                 ais: tuple = ("alpha-orb", "sigma-pullback", "gamma-trend")) -> dict:
  sessions = sessions or load_sessions(cfg)
  days = sorted({s.td for s in sessions})
  by_day = {d: [s for s in sessions if s.td == d] for d in days}
  report = {"days": [str(days[0]), str(days[-1])] if days else None, "train_days": train_days,
            "test_days": test_days, "ais": {}}
  for ai in ais:
    windows, oos_pnl, oos_trades, oos_wins = [], 0.0, 0, 0
    start = train_days
    while start + test_days <= len(days):
      train = [s for d in days[start - train_days: start] for s in by_day[d]]
      test = [s for d in days[start: start + test_days] for s in by_day[d]]
      results = [(score(r := {**simulate(cfg, ai, p, train), "off": p.get("off", False)}), p, r) for p in grid(ai)]
      best_score, best_p, best_r = max(results, key=lambda t: t[0])
      t = simulate(cfg, ai, best_p, test)
      oos_pnl += t["pnl"]
      oos_trades += t["trades"]
      oos_wins += t["wins"]
      windows.append({"train": [str(days[start - train_days]), str(days[start - 1])],
                      "test": [str(days[start]), str(days[start + test_days - 1])],
                      "chosen": describe(ai, best_p), "params": best_p,
                      "train_pnl": round(best_r["pnl"]), "test_pnl": round(t["pnl"]),
                      "test_trades": t["trades"], "test_wins": t["wins"]})
      start += test_days
    latest_train = [s for d in days[-train_days:] for s in by_day[d]]
    latest = max(((score({**simulate(cfg, ai, p, latest_train), "off": p.get("off", False)}), p) for p in grid(ai)),
                 key=lambda t: t[0])[1]
    report["ais"][ai] = {"oos_pnl": round(oos_pnl), "oos_trades": oos_trades, "oos_wins": oos_wins,
                         "windows": windows, "latest_params": latest, "latest_desc": describe(ai, latest)}
    log(f"{ai}: 考試期總損益 NT$ {oos_pnl:+,.0f}（{oos_trades} 筆，勝 {oos_wins}）｜最新建議：{describe(ai, latest)}")
  return report


def save_report(report: dict) -> Path:
  out = ROOT / "data" / "wfo_report.json"
  out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
  return out
