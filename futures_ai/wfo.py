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


KBAR_FILES = [ROOT / "data" / "kbars" / "TMF_1min.csv", ROOT / "data" / "kbars_repo" / "TMF_1min.csv"]


def sessions_from_kbars(path: Path) -> list[Session]:
  """Builds sessions from a Shioaji 1-minute K-bar export (continuous near month, Taipei time)."""
  import csv
  from .calendar import next_weekday
  rows = []
  with path.open(encoding="utf-8") as f:
    for r in csv.DictReader(f):
      ts = datetime.strptime(r["ts_taipei"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TPE)
      rows.append((ts, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), float(r["volume"])))
  if not rows:
    return []
  # Shioaji may label a bar by its start or its end minute; detect from the day-session open.
  start_labeled = any(ts.hour == 8 and ts.minute == 45 for ts, *_ in rows[:5000])
  groups: dict[tuple, dict[datetime, Bar]] = {}
  for ts, o, h, l, c, v in rows:
    end = ts + timedelta(minutes=1) if start_labeled else ts
    t = end.time()
    if time(8, 46) <= t <= time(13, 45):
      key = (end.date(), "day", end.date())
    elif t >= time(15, 1) or t <= time(5, 0):
      start_date = end.date() if t >= time(15, 1) else end.date() - timedelta(days=1)
      key = (next_weekday(start_date), "night", start_date)
    else:
      continue
    groups.setdefault(key, {})[end] = Bar(end, o, h, l, c, v)
  out = []
  for key, bar_map in groups.items():
    if len(bar_map) < 30:
      continue
    bars = sorted(bar_map.values(), key=lambda b: b.end)
    if key[1] == "day":
      close = datetime.combine(key[0], time(13, 45), TPE)
    else:
      close = datetime.combine(key[2] + timedelta(days=1), time(5, 0), TPE)
    sess = Session(key[0], key[1], "TMFR1", bars, close)
    precompute(sess)
    out.append(sess)
  return out


def load_sessions(cfg: dict) -> list[Session]:
  from .barstore import sessions_from_zip_cached
  live_1m = ROOT / "data" / "live_1m.csv"
  by_sess: dict[tuple, Session] = {}
  # Priority: Daily_*.zip (highest) > KBAR_FILES > live_1m.csv (lowest, fills recent live sessions).
  for f in sorted(HIST.glob("Daily_*.zip")):
    td = datetime.strptime(f.stem[6:], "%Y_%m_%d").date()
    for name, code, bars in sessions_from_zip_cached(f, cfg["product"], cfg["roll_days_before"]):
      start = bars[0].end - timedelta(minutes=1)
      skey = (start.date(), name)
      close = datetime.combine(start.date(), time(13, 45), TPE) if name == "day" else \
          datetime.combine(start.date() + (timedelta(days=1) if start.hour >= 15 else timedelta()), time(5, 0), TPE)
      bmap = {b.end: b for b in bars}
      s = Session(td, name, code, sorted(bmap.values(), key=lambda b: b.end), close)
      by_sess[skey] = s
  for f in [*KBAR_FILES, live_1m]:
    if f.exists():
      for sess in sessions_from_kbars(f):
        start_d = (sess.bars[0].end - timedelta(minutes=1)).date()
        skey = (start_d, sess.name)
        if skey not in by_sess:
          by_sess[skey] = sess
        else:
          cur = by_sess[skey]
          bmap = {b.end: b for b in sess.bars}
          bmap.update({b.end: b for b in cur.bars})  # Higher-priority bars win on overlap.
          if len(bmap) > len(cur.bars):
            cur.bars = sorted(bmap.values(), key=lambda b: b.end)
  out = list(by_sess.values())
  for s in out:
    if s.vwap is None or len(s.vwap) != len(s.bars):
      precompute(s)
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


from .calendar import tod_bucket, us_dst, us_open_at  # noqa: E402,F401  (shared with live trading)


# ---------------------------------------------------------------- signals
def ema_tail(values: list[float], n: int) -> float:
  k, out = 2 / (n + 1), values[0]
  for v in values[1:]:
    out = v * k + out * (1 - k)
  return out


def clamp(x, lo, hi):
  return max(lo, min(hi, x))


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
    s_start = (s.bars[0].end - timedelta(minutes=1)).replace(second=0, microsecond=0)
    s_open = s_start.replace(hour=8, minute=45) if 8 <= s_start.hour < 15 else s_start.replace(hour=15, minute=0)
    r_end = s_open + timedelta(minutes=rb * 5)
    if x.end <= r_end:
      return None
    rng = [y for y in b[:k] if s_open < y.end <= r_end]
    if len(rng) < max(1, rb - 1):
      return None
    hi = max(y.h for y in rng)
    lo = min(y.l for y in rng)
    width = hi - lo
    if width < p.get("min_width", 40) or width > p.get("max_width", 260):
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
    min_er = p.get("min_er", 0.30)
    if min_er and s.er[i] < min_er:
      return None
    max_vwap_pts = p.get("max_vwap_pts", 200.0)
    if max_vwap_pts and abs(x.c - vw) > max_vwap_pts:
      return None
    bh = p.get("bonus_hours")
    hf = p.get("hours_free")
    if p.get("hours") and not (hf and hf[0] <= x.end.isoformat() < hf[1]):
      tb = tod_bucket(x.end)
      if tb == "day" and p.get("day_min_atr") and a < p["day_min_atr"]:
        return None
      if bh and tb in bh and a < p.get("bonus_min_atr", 35.0):
        return None
    sl = clamp(a * p["sl_atr"], 20, 150)
    tp = sl * p["tp_mult"]
    trail = a * p["trail_atr"] if p.get("trail_atr") else None
    rng_b = x.h - x.l
    mw = p.get("max_wick", 0.25)
    if s.direction[i] > 0 and x.l <= vw + p["pull_atr"] * a and x.c > vw and x.c > x.o:
      if mw is not None and (rng_b <= 0 or (x.h - x.c) / rng_b > mw):
        return None
      return "B", sl, tp, trail
    if s.direction[i] < 0 and x.h >= vw - p["pull_atr"] * a and x.c < vw and x.c < x.o:
      if mw is not None and (rng_b <= 0 or (x.c - x.l) / rng_b > mw):
        return None
      return "S", sl, tp, trail
    return None
  if ai == "anchor-orb":
    anchor_kind = p["anchor"]
    ts = x.end
    if anchor_kind == "us":
      if ts.time() >= time(5, 1) and ts.time() < time(15, 0):
        return None
      anchor = us_open_at(ts)
    else:  # night session open 15:00
      d0 = ts.date() if ts.time() >= time(15, 0) else ts.date() - timedelta(days=1)
      if time(5, 1) <= ts.time() < time(15, 0):
        return None
      anchor = datetime.combine(d0, time(15, 0), TPE)
    dst_skip = p.get("dst_skip", (150, 150))
    if dst_skip and anchor_kind != "us" and not p.get("hours"):
      if anchor + timedelta(minutes=dst_skip[0]) < ts <= us_open_at(ts) - timedelta(minutes=dst_skip[1]):
        return None
    r_end = anchor + timedelta(minutes=p["range_min"])
    if not (r_end < ts <= r_end + timedelta(minutes=p["window"])):
      return None
    rng = [y for y in b[:k] if anchor < y.end <= r_end]
    if len(rng) < max(1, p["range_min"] // 5 - 1):
      return None
    hi, lo = max(y.h for y in rng), min(y.l for y in rng)
    width = hi - lo
    if width < p.get("min_width", 30):
      return None
    min_body = p.get("min_body_atr", 0.25)
    if min_body and abs(x.c - x.o) < min_body * a:
      return None
    reach_r = p.get("reach_r", 2.5)
    after_rng = [y for y in b[:k] if y.end > r_end] if reach_r else []
    sl = clamp(width * p["sl_frac"], 30, 150)
    tp = sl * p["tp_mult"]
    if x.c > hi + 3 and prev.c <= hi + 3 and x.c > vw:
      if reach_r and after_rng and max(y.h for y in after_rng) >= hi + reach_r * width:
        return None
      return "B", sl, tp, None
    if x.c < lo - 3 and prev.c >= lo - 3 and x.c < vw:
      if reach_r and after_rng and min(y.l for y in after_rng) <= lo - reach_r * width:
        return None
      return "S", sl, tp, None
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


def simulate(cfg: dict, ai: str, p: dict, sessions: list[Session], record: bool = False) -> dict:
  if p.get("off"):
    return {"pnl": 0.0, "trades": 0, "wins": 0, "maxdd": 0.0, "curve": []}
  pv = cfg["point_value"]
  slip_default = (cfg.get("sim_sl_slip_pts") or {}).get(ai, 1)
  cap_entries = min(cfg["max_entries_per_session"], p.get("max_entries", 99))
  pnl = peak = maxdd = 0.0
  trades = wins = 0
  tlog = []
  day_pnl: dict[date, float] = {}
  locked_days: set[date] = set()
  for s in sessions:
    b = s.bars
    pos = None
    entries = 0
    last_exit_i = None
    block_side = None
    block_i = None
    chase_side = None  # set after a take-profit: allow trend-continuation re-entries
    chase_k = None
    pend = None  # limit entry: (side, limit_px, sl_pts, tp_pts, trail, expires_end)
    gb_peak = 0.0  # best favorable excursion of the open position (give-back exit)
    sl_count = 0  # Lesson rules (learned from losing-trade reviews): consecutive stop-losses in this session,
    last_sl = None  # and (side, time) of the most recent stop-loss.
    s_start = b[0].end - timedelta(minutes=1) if b else None
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
          sl_slip = p.get("sl_slip", slip_default)  # Points beyond the stop (or gapped open) at which a stop fills.
          exit_px, kind = ((min(sl, x.o) - sl_slip) if d == 1 else (max(sl, x.o) + sl_slip)), "SL"
        elif tp_hit:
          slip = 0 if p.get("tp_limit") else 1
          exit_px, kind = (tp - slip) if d == 1 else (tp + slip), "TP"
        elif mtc <= cfg["flatten_minutes_before_close"] or i == len(b) - 1:
          exit_px, kind = (x.c - 2) if d == 1 else (x.c + 2), "END"
        elif p.get("flat_at") and s.name == "night" and x.end.hour >= 15 and x.end.strftime("%H:%M") >= p["flat_at"]:
          exit_px, kind = (x.c - 2) if d == 1 else (x.c + 2), "TIME"  # Flat at a fixed evening time.
        elif p.get("flat_us_min") is not None and s.name == "night" and \
            x.end >= us_open_at(x.end) + timedelta(minutes=p["flat_us_min"]):
          exit_px, kind = (x.c - 2) if d == 1 else (x.c + 2), "TIME"  # Flat N minutes after the US cash open.
        be = p.get("be_r")  # Breakeven: once profit >= be_r x initial stop, move stop to entry (+cost cover).
        if exit_px is None and be:
          init = pos[6] if len(pos) > 6 else abs(entry - sl)
          fav = (x.h - entry) if d == 1 else (entry - x.l)
          be_px = entry + d * 6
          if fav >= be * init and ((d == 1 and sl < be_px) or (d == -1 and sl > be_px)):
            pos = (side, entry, be_px, tp, cost_in, trail, init) + tuple(pos[7:])
            sl = be_px
        gb_keep = p.get("gb_keep")  # Give-back exit: once peak profit >= gb_act x TP distance, lock gb_keep of it.
        if exit_px is None and gb_keep:
          fav = (x.h - entry) if d == 1 else (entry - x.l)
          gb_peak = max(gb_peak, fav)
          if gb_peak >= p.get("gb_act", 0.5) * abs(tp - entry):
            lock = round(entry + d * gb_keep * gb_peak)
            cur = pos[2]
            if (d == 1 and lock > cur) or (d == -1 and lock < cur):
              pos = (side, entry, lock) + tuple(pos[3:])
        if exit_px is None and trail:  # Ratchet the stop after this bar (conservative ordering).
          new_sl = round(x.h - trail) if d == 1 else round(x.l + trail)
          sl = pos[2]
          if (d == 1 and new_sl > sl) or (d == -1 and new_sl < sl):
            pos = (side, entry, new_sl, tp, cost_in, trail) + tuple(pos[6:])
        if exit_px is not None:
          net = (exit_px - entry) * d * pv - cost_in - side_cost(cfg, exit_px)
          if record:
            tlog.append((pos[7] if len(pos) > 7 else None, x.end, side, net, kind))
          pnl += net
          trades += 1
          wins += net > 0
          peak = max(peak, pnl)
          maxdd = max(maxdd, peak - pnl)
          day_pnl[s.td] = day_pnl.get(s.td, 0.0) + net
          if cfg.get("enforce_daily_loss") and day_pnl[s.td] <= -cfg["max_daily_loss_twd"]:
            locked_days.add(s.td)
            pend = None
          # Protected exit: the stop had been moved to the entry price or beyond (breakeven / trailing) and the
          # fill stayed within 30 points of entry (classification by stop position, independent of costs).
          protected = d * (pos[2] - entry) >= 0 and (exit_px - entry) * d >= -30
          pos = None
          last_exit_i = i
          if kind == "TP" and p.get("chase"):
            chase_k = max([kk for ii, kk in s.k_at.items() if ii <= i] or [0])
            chase_side = side
          if net > 0:
            sl_count = 0
            last_sl = None
          if kind == "SL":
            chase_side = None
            # Comparison options (default off = live behaviour): a protected exit that loses only costs + slippage
            # is not counted as a stop for max_sl / rev_cool (p["sl_real_only"]) and/or for Sigma's stop-block
            # (p["sl_real_block"]).
            if net < 0 and not (p.get("sl_real_only") and protected):
              sl_count += 1
              last_sl = (side, x.end)
            if net < 0 and not (p.get("sl_real_block") and protected):
              if ai in ("sigma-pullback", "sigma-fixed"):
                block_side, block_i = side, i
      if pend and not pos:  # Working limit entry from an earlier signal.
        p_side, p_px, p_sl, p_tp, p_tr, p_exp = pend
        if x.end > p_exp or mtc <= cfg["no_entry_minutes_before_close"] or s.td in locked_days:
          pend = None
        elif (p_side == "B" and x.l <= p_px - p.get("fill_through", 0)) or (p_side == "S" and x.h >= p_px + p.get("fill_through", 0)):
          d = 1 if p_side == "B" else -1
          pos = (p_side, p_px, round(p_px - d * p_sl), round(p_px + d * p_tp), side_cost(cfg, p_px), p_tr, abs(p_sl), x.end)
          entries += 1
          gb_peak = 0.0
          pend = None
          continue
        elif x.end >= p_exp:
          pend = None
      if pos or pend or entries >= cap_entries or mtc <= cfg["no_entry_minutes_before_close"] or s.td in locked_days:
        continue
      if i not in s.k_at:  # Decisions only when a 5-minute bar completes.
        continue
      hf = p.get("hours_free")  # (start_iso, end_iso): period in which the live AI traded at any time.
      if p.get("hours") and tod_bucket(x.end) not in p["hours"] and \
          not (p.get("hours_until") and x.end.isoformat() >= p["hours_until"]) and \
          not (hf and hf[0] <= x.end.isoformat() < hf[1]):
        continue
      ue = p.get("us_entry_min")  # Entries only within N minutes after the US cash open (DST-aware).
      if ue is not None:
        o = us_open_at(x.end)
        if not (o < x.end <= o + timedelta(minutes=ue)):
          continue
      ew = p.get("entry_window")  # ("HH:MM", "HH:MM") wall-clock entry window (may cross midnight).
      if ew:
        hm = x.end.strftime("%H:%M")
        if not ((ew[0] <= hm < ew[1]) if ew[0] < ew[1] else (hm >= ew[0] or hm < ew[1])):
          continue
      if p.get("max_sl") and sl_count >= p["max_sl"]:
        continue  # Lesson: stop for the session after N losing stop-outs.
      if p.get("open_delay") and x.end < s_start + timedelta(minutes=p["open_delay"]):
        continue  # Lesson: no entries in the first N minutes of the session.
      if p.get("min_er") and s.er[i] < p["min_er"]:
        continue  # Lesson: minimum 5-minute efficiency ratio filter.
      if p.get("max_vwap_atr") and abs(x.c - s.vwap[i]) > p["max_vwap_atr"] * max(s.atr5[s.k_at[i]], 5.0):
        continue  # Lesson: do not chase overextended distance from VWAP.
      if p.get("max_vwap_pts") and abs(x.c - s.vwap[i]) > p["max_vwap_pts"]:
        continue  # Lesson: do not chase > N points away from VWAP.
      sig = signal5(ai, p, s, s.k_at[i], i)
      if sig and p.get("rev_cool") and last_sl and sig[0] != last_sl[0] and \
          x.end < last_sl[1] + timedelta(minutes=p["rev_cool"]):
        continue  # Lesson: no reversal entry within N minutes after a stop-loss.
      if not sig and chase_side and p.get("chase"):
        k = s.k_at[i]
        n = p["chase"]["lookback"]
        if k - chase_k > p["chase"]["expire"]:
          chase_side = None
        elif k >= n and s.regime[i] == "trend":
          xb = s.b5[k]
          prior = s.b5[k - n:k]
          if chase_side == "B" and s.direction[i] > 0 and xb.c > max(y.h for y in prior):
            sig = ("B", p["sl_pts"], p["tp_pts"], None)
          elif chase_side == "S" and s.direction[i] < 0 and xb.c < min(y.l for y in prior):
            sig = ("S", p["sl_pts"], p["tp_pts"], None)
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
      if p.get("limit_atr") is not None:  # 方向 B: wait for a small pullback with a limit order (no slippage).
        d = 1 if side == "B" else -1
        lim = round(x.c - d * p["limit_atr"] * s.atr5[s.k_at[i]])
        pend = (side, lim, sl_pts, tp_pts, trail, x.end + timedelta(minutes=p.get("limit_min", 10)))
        continue
      entry = x.c + 2 if side == "B" else x.c - 2
      d = 1 if side == "B" else -1
      pos = (side, entry, round(entry - d * sl_pts), round(entry + d * tp_pts), side_cost(cfg, entry), trail, abs(sl_pts), x.end)
      entries += 1
      gb_peak = 0.0
  return {"pnl": pnl, "trades": trades, "wins": wins, "maxdd": maxdd, "log": tlog}


# ---------------------------------------------------------------- grids & walk-forward
def grid(ai: str) -> list[dict]:
  if ai == "alpha-orb":
    g = [dict(range_bars=r, sl_frac=f, tp_mult=t, regimes=("trend", "chop"), flip=())
         for r, f, t in itertools.product([3, 6], [0.5, 0.75], [2.0, 3.0])]
  elif ai == "sigma-pullback":
    g = [dict(pull_atr=pa, sl_atr=sa, tp_mult=4.0, trail_atr=None, regimes=("trend",), flip=())
         for pa, sa in itertools.product([0.2, 0.5], [1.5, 2.0])]
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
  if r.get("off"):
    return 0.0
  if r["trades"] < 3:
    return -1e9  # Too few trades to trust.
  return r["pnl"] - 0.5 * r["maxdd"]


def _pick_best(cfg: dict, ai: str, candidates: list[dict], train_sessions: list[Session]) -> tuple[float, dict, dict]:
  scored = []
  for p in candidates:
    if p.get("off"):
      continue
    r = simulate(cfg, ai, p, train_sessions)
    scored.append((score(r), p, r))
  best_score, best_p, best_r = max(scored, key=lambda t: t[0])
  if best_r["trades"] < 3 or best_r["pnl"] <= 0:
    off_p = {"off": True}
    return 0.0, off_p, simulate(cfg, ai, off_p, train_sessions)
  return best_score, best_p, best_r


def describe(ai: str, p: dict) -> str:
  if p.get("off"):
    return "暫停（所有參數組合在練習期都不賺）"
  reg = "+".join({"trend": "趨勢", "chop": "盤整"}[r] for r in p["regimes"])
  fl = ("，" + "+".join({"trend": "趨勢", "chop": "盤整"}[r] for r in p["flip"]) + "盤反向") if p.get("flip") else ""
  core = {k: v for k, v in p.items() if k not in ("regimes", "flip")}
  return f"{core}｜只做{reg}盤{fl}"


LIVE_TO_SIM = {"alpha-orb": "alpha-orb", "sigma-vwap": "sigma-pullback", "gamma-scalp": "gamma-trend"}


def walk_forward(cfg: dict, train_days: int, test_days: int, sessions: list[Session] | None = None, log=print,
                 ais: tuple = ("alpha-orb", "sigma-pullback", "gamma-trend"), base: dict | None = None,
                 cur_params: dict | None = None) -> dict:
  """base: optional {sim_ai: dict} of live execution settings (limit entry, hours, ...) merged under every grid
  point, so training simulates the same execution as the live AI (only the grid keys are searched)."""
  sessions = sessions or load_sessions(cfg)
  base = base or {}
  cur_params = cur_params or {}
  def grid_(ai):
    return [p if p.get("off") else {**base.get(ai, {}), **p} for p in grid(ai)]
  days = sorted({s.td for s in sessions})
  by_day = {d: [s for s in sessions if s.td == d] for d in days}
  report = {"days": [str(days[0]), str(days[-1])] if days else None, "train_days": train_days,
            "test_days": test_days, "ais": {}}
  for ai in ais:
    windows, oos_pnl, oos_trades, oos_wins = [], 0.0, 0, 0
    last_active = ({**base.get(ai, {}), **cur_params[ai]} if ai in cur_params else None)
    start = train_days
    while start + test_days <= len(days):
      train = [s for d in days[start - train_days: start] for s in by_day[d]]
      test = [s for d in days[start: start + test_days] for s in by_day[d]]
      best_score, best_p, best_r = _pick_best(cfg, ai, grid_(ai), train)
      if not best_p.get("off"):
        last_active = best_p
      sim_p = (last_active if (best_p.get("off") and last_active is not None) else best_p)
      t = simulate(cfg, ai, sim_p, test)
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
    latest = _pick_best(cfg, ai, grid_(ai), latest_train)[1]
    report["ais"][ai] = {"oos_pnl": round(oos_pnl), "oos_trades": oos_trades, "oos_wins": oos_wins,
                         "windows": windows, "latest_params": latest, "latest_desc": describe(ai, latest)}
    log(f"{ai}: 考試期總損益 NT$ {oos_pnl:+,.0f}（{oos_trades} 筆，勝 {oos_wins}）｜最新建議：{describe(ai, latest)}")
  return report


def save_report(report: dict) -> Path:
  out = ROOT / "data" / "wfo_report.json"
  out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
  return out
